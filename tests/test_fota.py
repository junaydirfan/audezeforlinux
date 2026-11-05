"""
Unit tests for the firmware updater (maxwell2/fota.py and FotaEngine in maxwell2/firmware.py).

The engine runs against FakeNode, an in-memory stand-in for one Maxwell 2 node (dongle or
headset). FakeNode answers the update requests the way the real hardware was observed to,
keeps a staging area in RAM, and can be told to misbehave (reject a request, drop a reply,
corrupt a read, disappear) so every failure path can be checked without real hardware.

The engine talks to FakeNode through SimTransport, a Transport whose only replaced parts are
the raw HID read/write calls. Framing, reply reassembly and reply matching are the real code.
"""

import errno
import hashlib
import lzma
import os
import struct
import tempfile
import unittest
from collections import deque
from unittest import mock

from maxwell2 import firmware
from maxwell2 import fota
from maxwell2.firmware import (
    FirmwarePackage,
    FotaEngine,
    FotaInterruptedError,
    FotaSafetyError,
    FotaTransferError,
)
from maxwell2.protocol import (
    CHUNK_MAX,
    InputStream,
    ProtocolError,
    REPORT_LENGTH,
    frame_output_reports,
    parse_race_packet,
)
from maxwell2.transport import DeviceBusyError, Transport

HEADSET_STAGING = (0x004BC000, 0x323000)   # as reported by a real Maxwell 2 headset
DONGLE_STAGING = (0x00263000, 0x194000)    # as reported by a real Maxwell 2 dongle
ROUTE_RELAY, ROUTE_DIRECT = 0x80, 0x00


def make_package(tmpdir, chip_id, name, version="v1.0.5.7.4", dst=0x08013000):
    """Writes a minimal valid firmware package (one partition installed at dst)."""
    body = b"PARTITION_" * 40
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 16384, "lc": 3, "lp": 0, "pb": 2}]
    comp = bytearray(lzma.compress(body, format=lzma.FORMAT_ALONE, filters=filters))
    comp[5:13] = struct.pack("<Q", len(body))

    hdr = bytearray(b"\xFF" * 0x1000)
    hdr[0x100:0x10E] = struct.pack("<HHBBII", 0x0011, 10, 1, 1, 0x1000, len(comp))
    ver = version.encode("ascii") + b"\x00"
    hdr[0x10E:0x12E] = struct.pack("<HH", 0x0013, 28) + ver + b"\xFF" * (28 - len(ver))
    table = struct.pack("<I", 1) + struct.pack("<III", 0x1000, len(body), dst)
    hdr[0x12E:0x12E + 4 + len(table)] = struct.pack("<HH", 0x0012, len(table)) + table
    hashes = struct.pack("<I", 1) + hashlib.sha256(body).digest()
    off = 0x12E + 4 + len(table)
    hdr[off:off + 4 + len(hashes)] = struct.pack("<HH", 0x0014, len(hashes)) + hashes
    chip = chip_id.encode("ascii") + b"\x00"
    off += 4 + len(hashes)
    hdr[off:off + 4 + len(chip)] = struct.pack("<HH", 0x0020, len(chip)) + chip

    full = hdr + comp
    full[0:32] = hashlib.sha256(full[0x100:]).digest()
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as f:
        f.write(full)
    return FirmwarePackage(path)


def response(cmd, payload, head=0x05):
    return struct.pack("<BBHH", head, 0x5B, len(payload) + 2, cmd) + payload


def notification(cmd, payload=b"\x00"):
    return struct.pack("<BBHH", 0x05, 0x5D, len(payload) + 2, cmd) + payload


# What a real Maxwell 2 headset sent, instead of a reply, to an integrity check on a session it
# had ended (after the ~70 s read-back): a device-initiated FOTA stop.
DEVICE_FOTA_STOP = bytes.fromhex("15 5a 05 00 03 1c 00 80 04")

# Requests the device only accepts inside an open update session
SESSION_CMDS = (fota.CMD_FOTA_START_TRANSACTION, fota.CMD_FOTA_WRITE_STATE, fota.CMD_STORAGE_PAGE_PROGRAM,
                fota.CMD_FOTA_INTEGRITY_CHECK, fota.CMD_FOTA_COMMIT)


class FakeNode:
    """
    In-memory model of one node's update side. Flash starts filled with an old image (0xA5),
    erasing sets it to 0xFF, and writes can only clear bits, as on real NOR flash, so a
    missing erase shows up as a read-back mismatch.

    faults maps a command id to a list of actions, used up one per request:
      None (behave), "nak" (status 0x01), "checksum" (status 0x0A), "drop" (no reply),
      "stale" (reply for another address), "corrupt" (page read returns a flipped byte),
      "gone" (the write fails as if the device disappeared), "interrupt" (Ctrl-C),
      "stop" (the device sends FOTA stop instead of replying).

    Like the real headset, it ends the update session once more than idle_reads_limit page
    reads in a row arrive with no other update activity, and from then on answers update
    commands with FOTA stop until a new session is started (0x1C08).
    """

    def __init__(self, staging=HEADSET_STAGING, version="v1.0.5.7.4"):
        self.staging_addr, self.staging_len = staging
        self.flash = bytearray(b"\xA5" * self.staging_len)
        self.version = version
        self.faults = {}
        self.requests = []           # (cmd, head, route)
        self.erases = []             # (address, length)
        self.out_of_range = []       # writes or erases outside the staging area
        self.committed = False
        self.noise_every = 7         # emit an unrelated notification before every 7th reply
        self.session_open = False
        self.idle_reads = 0
        self.idle_reads_limit = 8    # the test packages are ~18 pages, so the read-back ends the session
        self.stops_sent = 0
        self.install_on_commit = True
        self.installed = None

    def fault(self, cmd, *actions):
        self.faults.setdefault(cmd, []).extend(actions)

    def _next_fault(self, cmd):
        queue = self.faults.get(cmd)
        return queue.pop(0) if queue else None

    def _in_staging(self, address, length):
        return self.staging_addr <= address and address + length <= self.staging_addr + self.staging_len

    def handle(self, packet, route):
        """Returns the reply packets for one request."""
        head, _, _, cmd = struct.unpack_from("<BBHH", packet, 0)
        _, body = parse_race_packet(packet)
        self.requests.append((cmd, head, route))
        action = self._next_fault(cmd)
        if cmd == fota.CMD_FOTA_COMMIT and action in ("gone", "drop"):
            self._reply(cmd, head, body, None)   # it committed and rebooted before its reply got out
        if action == "gone":
            raise OSError(errno.ENODEV, "No such device")
        if action == "interrupt":
            raise KeyboardInterrupt
        if action == "drop":
            return []
        if cmd == fota.CMD_FOTA_START:
            self.session_open, self.idle_reads = True, 0
        elif cmd == fota.CMD_STORAGE_PAGE_READ:
            self.idle_reads += 1
            if self.idle_reads > self.idle_reads_limit:
                self.session_open = False
        elif cmd in SESSION_CMDS:
            self.idle_reads = 0
        if action == "stop" or (cmd in SESSION_CMDS and not self.session_open):
            self.session_open = False
            self.stops_sent += 1
            return [DEVICE_FOTA_STOP]
        replies = []
        if self.noise_every and len(self.requests) % self.noise_every == 0:
            replies.append(notification(0x2CB1))
        replies.append(self._reply(cmd, head, body, action))
        return replies

    def _reply(self, cmd, head, body, action):
        status = 0x01 if action == "nak" else 0x00
        if cmd == fota.CMD_FOTA_PARTITION_INFO:
            return response(cmd, struct.pack("<BHII", status, 0, self.staging_addr, self.staging_len))
        if cmd == fota.CMD_STORAGE_PARTITION_ERASE:
            _, length, address = struct.unpack("<BII", body)
            self.erases.append((address, length))
            if not self._in_staging(address, length):
                self.out_of_range.append(("erase", address, length))
                status = 0x01
            elif status == 0:
                start = address - self.staging_addr
                self.flash[start:start + length] = b"\xFF" * length
            return response(cmd, bytes([status]), head)
        if cmd == fota.CMD_STORAGE_PAGE_PROGRAM:
            _, pages = struct.unpack_from("<BB", body, 0)
            first = struct.unpack_from("<I", body, 3)[0]
            for i in range(pages):
                rec = body[2 + i * 261:2 + (i + 1) * 261]
                checksum, address = struct.unpack_from("<BI", rec, 0)
                data = rec[5:]
                if not self._in_staging(address, len(data)):
                    self.out_of_range.append(("write", address, len(data)))
                    status = 0x01
                if checksum != fota.fota_checksum(data) or action == "checksum":
                    status = fota.STATUS_CHECKSUM_ERROR
                if status == 0:
                    start = address - self.staging_addr
                    old = self.flash[start:start + len(data)]
                    self.flash[start:start + len(data)] = bytes(a & b for a, b in zip(old, data))
            if action == "stale":
                first -= fota.PAGE_SIZE
            return response(cmd, struct.pack("<BBBI", status, 0, pages, first), head)
        if cmd == fota.CMD_STORAGE_PAGE_READ:
            _, _, address = struct.unpack("<BBI", body)
            start = address - self.staging_addr
            data = bytearray(self.flash[start:start + fota.PAGE_SIZE])
            if action == "corrupt":
                data[17] ^= 0x01
            if action == "stale":
                address -= fota.PAGE_SIZE
            return response(cmd, struct.pack("<BBHI", status, 0, 0, address) + bytes(data))
        if cmd == fota.CMD_FOTA_COMMIT and status == 0:
            self.committed = True
            if self.install_on_commit:
                # The bootloader installs the package and clears the staging area (seen on both chips)
                self.installed = bytes(self.flash)
                self.flash[:] = b"\xFF" * self.staging_len
        return response(cmd, bytes([status]), head)

    # Helpers for assertions
    def cmds(self):
        return [c for c, _, _ in self.requests]

    def staged(self, size):
        """What was in the staging area when the package was committed (or now, if it wasn't)."""
        return bytes((self.installed if self.installed is not None else self.flash)[:size])


class SimTransport(Transport):
    """The real Transport with its HID read/write calls pointed at a FakeNode."""

    def __init__(self, node, is_dongle=True, product_id=0x4B28, busy=False):
        super().__init__(hidraw_path="/dev/hidraw-sim")
        self.node = node
        self.is_dongle = is_dongle
        self.product_id = product_id
        self.fd = -1
        self.busy = busy
        self.inbox = deque()
        self.lock_calls = []
        self.reconnects = 0

    def _write_reports(self, race_packet, is_dongle):
        route_flag = self.is_dongle if is_dongle is None else is_dongle
        reports = frame_output_reports(race_packet, is_dongle=route_flag)
        sent = b"".join(r[3:3 + r[1]] for r in reports)
        assert sent == race_packet and all(len(r) == REPORT_LENGTH for r in reports)
        for reply in self.node.handle(sent, reports[0][2]):
            for i in range(0, len(reply), CHUNK_MAX):
                chunk = reply[i:i + CHUNK_MAX]
                self.inbox.append(bytes([0x07, len(chunk), 0x00]) + chunk + b"\x00" * (REPORT_LENGTH - 3 - len(chunk)))
        return reports

    def poll_input_report(self):
        return self.inbox.popleft() if self.inbox else None

    def drain(self, max_rounds=64):
        self.inbox.clear()
        self.input_stream.clear()
        self.pending_notifications.clear()

    def reconnect(self):
        self.reconnects += 1
        raise AssertionError("a firmware transfer must never reconnect")

    def claim_exclusive(self):
        self.lock_calls.append("exclusive")
        if self.busy:
            raise DeviceBusyError("/dev/hidraw-sim is in use by another audezeforlinux process")

    def release_exclusive(self):
        self.lock_calls.append("shared")


class FakeClient:
    """Serves version and battery the way MaxwellClient does; reports the new version once committed."""

    def __init__(self, transport, node, after=None, battery=95, battery_error=None, version_error=None,
                 versions=None, usb_powered=None, reboot_polls=1):
        self.transport = transport
        self.reboot_polls = reboot_polls   # version reads that fail after the commit, while it reboots
        self.node = node
        self.versions = versions
        self.after = after
        # A number, or a list of successive readings (the last one repeats)
        self.readings = list(battery) if isinstance(battery, (list, tuple)) else [battery]
        self.battery_error = battery_error
        self.version_error = version_error
        self.usb_powered = usb_powered

    def get_battery(self):
        if self.battery_error:
            raise self.battery_error
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def is_usb_powered(self):
        # Default: on USB power exactly when talking to the headset's own USB node
        return (not self.transport.is_dongle) if self.usb_powered is None else self.usb_powered

    def get_firmware(self):
        if self.version_error:
            raise self.version_error
        if self.node.committed and self.reboot_polls > 0:
            self.reboot_polls -= 1
            raise OSError(errno.ENODEV, "No such device")
        if self.node.committed and self.after is not None:
            return {"dongle": self.after, "headset": self.after}
        if self.versions:
            return dict(self.versions)
        return {"dongle": self.node.version, "headset": self.node.version}


FAST_TIMEOUTS = {
    "FOTA_COMMAND_TIMEOUT_SEC": 0.05,
    "FOTA_ERASE_TIMEOUT_SEC": 0.05,
    "FOTA_INTEGRITY_TIMEOUT_SEC": 0.05,
    "FOTA_PAGE_TIMEOUT_SEC": 0.05,
    "BATTERY_SETTLE_INTERVAL_SEC": 0,
    "BATTERY_SETTLE_TIMEOUT_SEC": 0.05,
}


class FotaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in FAST_TIMEOUTS.items():
            patcher = mock.patch.object(firmware, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def package(self, target="headset", version="v1.0.5.7.4", chip=None, dst=0x08013000):
        chip = chip or ("AB1585_Headset" if target == "headset" else "AB1565_ULL2_DONGLE")
        return make_package(self.tmp.name, chip, f"Maxwell2_XBOX_{target}_{version}.bin", version, dst)

    def setup_engine(self, target="headset", version="v1.0.5.7.4", via_dongle=True, node=None, pkg=None,
                     after="new", transport_kw=None, engine_kw=None, **client_kw):
        node = node or FakeNode(HEADSET_STAGING if target == "headset" else DONGLE_STAGING)
        transport = SimTransport(node, is_dongle=via_dongle, **(transport_kw or {}))
        pkg = pkg or self.package(target, version)
        client = FakeClient(transport, node, after=pkg.version_string if after == "new" else after, **client_kw)
        kw = dict(experimental=True, readback_timeout_sec=0.1, readback_interval_sec=0)
        kw.update(engine_kw or {})
        engine = FotaEngine(pkg, target=target, client=client, dry_run=False, **kw)
        return engine, node, transport

    def assert_nothing_staged(self, node):
        self.assertEqual(node.erases, [])
        self.assertNotIn(fota.CMD_STORAGE_PAGE_PROGRAM, node.cmds())

    def assert_not_committed(self, node):
        self.assertFalse(node.committed)
        self.assertNotIn(fota.CMD_FOTA_COMMIT, node.cmds())


# ==============================================================================
# Wire format
# ==============================================================================

class TestWireFormat(unittest.TestCase):

    def test_erase_payload_is_length_then_address(self):
        # The one request that could damage installed firmware if its fields were swapped.
        pkt = fota.build_partition_erase(0x004BC000, 0x323000)
        self.assertEqual(pkt, bytes.fromhex("05 5a 0b 00 04 04 00 00 30 32 00 00 c0 4b 00"))
        for bad in ((0x004BC100, 0x1000), (0x004BC000, 0x1001), (0x004BC000, 0)):
            with self.assertRaises(ProtocolError):
                fota.build_partition_erase(*bad)

    def test_fixed_requests(self):
        self.assertEqual(fota.build_partition_info_query(), bytes.fromhex("05 5a 03 00 00 1c 00"))
        self.assertEqual(fota.build_fota_start(), bytes.fromhex("15 5a 04 00 08 1c 01 00"))
        self.assertEqual(fota.build_start_transaction(), bytes.fromhex("15 5a 02 00 0a 1c"))
        self.assertEqual(fota.build_write_state(fota.FOTA_STATE_DOWNLOADED), bytes.fromhex("15 5a 04 00 06 1c 11 02"))
        self.assertEqual(fota.build_integrity_check(), bytes.fromhex("15 5a 05 00 01 1c 01 00 00"))
        self.assertEqual(fota.build_commit(), bytes.fromhex("15 5a 03 00 02 1c 00"))
        self.assertEqual(fota.build_page_read(0x004BC100), bytes.fromhex("05 5a 08 00 03 04 00 01 00 c1 4b 00"))
        with self.assertRaises(ProtocolError):
            fota.build_write_state(b"\x11")

    def test_page_program_layout(self):
        data = bytes(range(256)) + b"\x00" * 256
        pkt = fota.build_page_program(0x004BC000, data)
        self.assertEqual(pkt[:8], bytes.fromhex("15 5a") + struct.pack("<H", 2 + 2 + 2 * 261) + bytes.fromhex("02 04 00 02"))
        self.assertEqual(pkt[8:13], bytes([0xA9]) + struct.pack("<I", 0x004BC000))
        self.assertEqual(pkt[13:269], data[:256])
        self.assertEqual(pkt[269:274], bytes([0x00]) + struct.pack("<I", 0x004BC100))
        for addr, bad in ((0x004BC001, b"\x00" * 256), (0x004BC000, b"\x00" * 255), (0x004BC000, b""),
                          (0x004BC000, b"\x00" * 1024)):
            with self.assertRaises(ProtocolError):
                fota.build_page_program(addr, bad)

    def test_page_checksum(self):
        self.assertEqual(fota.fota_checksum(b"\x00" * 256), 0x00)
        self.assertEqual(fota.fota_checksum(b"\xFF" * 256), 0xB4)
        self.assertEqual(fota.fota_checksum(bytes(range(256))), 0xA9)
        self.assertEqual(fota.fota_checksum(b"PARTITION_" * 25 + b"PARTIT"), 0x65)

    def test_reply_parsers_reject_failures(self):
        ok = response(fota.CMD_FOTA_PARTITION_INFO, struct.pack("<BHII", 0, 0, *HEADSET_STAGING))
        self.assertEqual(fota.parse_partition_info(ok), {"address": 0x004BC000, "length": 0x323000})
        for payload in (struct.pack("<BHII", 1, 0, *HEADSET_STAGING), struct.pack("<BHII", 0, 0, 0x4BC000, 0),
                        struct.pack("<BHII", 0, 0, 0x4BC010, 0x1000), b"\x00\x00"):
            with self.assertRaises(ProtocolError):
                fota.parse_partition_info(response(fota.CMD_FOTA_PARTITION_INFO, payload))

        cmd = fota.CMD_STORAGE_PAGE_PROGRAM
        fota.parse_page_program(response(cmd, struct.pack("<BBBI", 0, 0, 1, 0x4BC000)), 0x4BC000, 1)
        for payload, msg in ((struct.pack("<BBBI", 0x0A, 0, 1, 0x4BC000), "checksum"),
                             (struct.pack("<BBBI", 0x01, 0, 1, 0x4BC000), "rejected"),
                             (struct.pack("<BBBI", 0, 0, 1, 0x4BB000), "expected"),
                             (struct.pack("<BBBI", 0, 0, 2, 0x4BC000), "expected")):
            with self.assertRaisesRegex(ProtocolError, msg):
                fota.parse_page_program(response(cmd, payload), 0x4BC000, 1)

        with self.assertRaisesRegex(ProtocolError, "Expected reply"):
            fota.parse_status_response(response(fota.CMD_FOTA_START, b"\x00"), fota.CMD_FOTA_COMMIT)
        with self.assertRaisesRegex(ProtocolError, "status 0x01"):
            fota.parse_status_response(response(fota.CMD_FOTA_COMMIT, b"\x01"), fota.CMD_FOTA_COMMIT)

        read = response(fota.CMD_STORAGE_PAGE_READ, struct.pack("<BBHI", 0, 0, 0, 0x4BC000) + b"\x5A" * 256)
        self.assertEqual(fota.parse_page_read(read, 0x4BC000), b"\x5A" * 256)
        with self.assertRaises(ProtocolError):
            fota.parse_page_read(read, 0x4BC100)
        with self.assertRaises(ProtocolError):
            fota.parse_page_read(read[:-1], 0x4BC000)

    def test_input_stream_reassembles_fota_replies(self):
        reply = response(fota.CMD_STORAGE_PAGE_PROGRAM, struct.pack("<BBBI", 0, 0, 1, 0x4BC000), head=0x15)
        stream = InputStream()
        noisy = b"\x00\x99" + reply   # leading garbage must be skipped up to the 0x15 head
        out = []
        for i in range(0, len(noisy), 4):
            chunk = noisy[i:i + 4]
            out += stream.feed(bytes([0x07, len(chunk), 0x00]) + chunk + b"\x00" * (REPORT_LENGTH - 3 - len(chunk)))
        self.assertEqual(out, [reply])

    def test_planning_helpers(self):
        self.assertEqual(len(fota.pad_to_pages(b"\x01" * 257)), 512)
        self.assertEqual(fota.pad_to_pages(b"\x01" * 257)[257:], b"\xFF" * 255)
        self.assertEqual(fota.pad_to_pages(b"\x01" * 256), b"\x01" * 256)
        self.assertEqual(fota.erase_length_for(1), 0x1000)
        self.assertEqual(fota.erase_length_for(0x1000), 0x1000)
        self.assertTrue(fota.ranges_overlap((0, 10), (9, 5)))
        self.assertFalse(fota.ranges_overlap((0, 10), (10, 5)))
        # The real headset staging area ends exactly where package partition 4 ends, and before partition 5.
        self.assertFalse(fota.ranges_overlap(HEADSET_STAGING, (0x471000, 0x4B000)))
        self.assertFalse(fota.ranges_overlap(HEADSET_STAGING, (0x7E0000, 0x1000)))


# ==============================================================================
# Successful runs
# ==============================================================================

class TestUpdateSequence(FotaTestCase):

    def test_full_update_headset_via_dongle(self):
        engine, node, transport = self.setup_engine()
        phases = []
        res = engine.execute(progress_callback=lambda p: phases.append(p["phase"]))

        self.assertTrue(res["success"])
        self.assertEqual(res["verification"], "confirmed")
        pages = engine.total_pages
        session = [fota.CMD_FOTA_START, fota.CMD_FOTA_START_TRANSACTION, fota.CMD_FOTA_WRITE_STATE,
                   fota.CMD_FOTA_WRITE_STATE]
        expected = ([fota.CMD_FOTA_PARTITION_INFO] + session + [fota.CMD_STORAGE_PARTITION_ERASE]
                    + [fota.CMD_STORAGE_PAGE_PROGRAM] * pages + [fota.CMD_STORAGE_PAGE_READ] * pages
                    + session  # restarted: the read-back leaves the session idle long enough to end
                    + [fota.CMD_FOTA_INTEGRITY_CHECK, fota.CMD_FOTA_WRITE_STATE, fota.CMD_FOTA_COMMIT]
                    + [fota.CMD_STORAGE_PAGE_READ])  # after the reboot: has the staging area been cleared?
        self.assertEqual(node.cmds(), expected)
        self.assertEqual(node.stops_sent, 0)
        self.assertGreater(pages, node.idle_reads_limit)

        # Head byte per request, and every request relayed to the headset
        plain = {fota.CMD_FOTA_PARTITION_INFO, fota.CMD_STORAGE_PARTITION_ERASE, fota.CMD_STORAGE_PAGE_READ}
        for cmd, head, route in node.requests:
            self.assertEqual(head, 0x05 if cmd in plain else 0x15, hex(cmd))
            self.assertEqual(route, ROUTE_RELAY)

        # Exactly the reported staging area was erased, nothing outside it touched
        self.assertEqual(node.erases, [HEADSET_STAGING])
        self.assertEqual(node.out_of_range, [])
        self.assertEqual(node.staged(len(engine.image)), engine.image)
        self.assertEqual(engine.image[:engine.total_bytes], bytes(engine.package.raw_data))
        self.assertEqual((res["chunks_sent"], res["pages_verified"]), (pages, pages))
        self.assertEqual(res["fota_partition"], {"address": HEADSET_STAGING[0], "length": HEADSET_STAGING[1]})

        # Exclusive use during the run, shared afterwards; auto-reconnect off during, back on after
        self.assertEqual(transport.lock_calls, ["exclusive", "shared"])
        self.assertTrue(transport.auto_reconnect)
        self.assertEqual(transport.reconnects, 0)
        self.assertTrue(transport.is_dongle)  # route restored
        for phase in ("erase", "write", "verify"):
            self.assertIn(phase, phases)

    def test_routes_per_target(self):
        for target, via_dongle, route in (("dongle", True, ROUTE_DIRECT), ("headset", False, ROUTE_DIRECT)):
            with self.subTest(target=target, via_dongle=via_dongle):
                engine, node, _ = self.setup_engine(target, via_dongle=via_dongle)
                self.assertTrue(engine.execute()["success"])
                self.assertEqual({r for _, _, r in node.requests}, {route})
                self.assertEqual(node.erases, [HEADSET_STAGING if target == "headset" else DONGLE_STAGING])

        engine, node, _ = self.setup_engine("dongle", via_dongle=False)
        with self.assertRaisesRegex(FotaSafetyError, "not a dongle"):
            engine.execute()
        self.assertEqual(node.requests, [])

    def test_stage_only_never_commits(self):
        engine, node, transport = self.setup_engine(engine_kw={"stage_only": True})
        res = engine.execute()
        self.assertTrue(res["success"])
        self.assertTrue(res["staged_only"])
        self.assertEqual(res["verification"], "staged (not committed)")
        self.assertEqual(node.cmds()[-1], fota.CMD_FOTA_INTEGRITY_CHECK)
        self.assert_not_committed(node)
        self.assertEqual(node.staged(len(engine.image)), engine.image)
        self.assertEqual(transport.lock_calls, ["exclusive", "shared"])

    def test_multi_page_writes(self):
        for ppw in (2, 3):
            with self.subTest(pages_per_write=ppw):
                engine, node, _ = self.setup_engine(engine_kw={"pages_per_write": ppw})
                self.assertTrue(engine.execute()["success"])
                writes = node.cmds().count(fota.CMD_STORAGE_PAGE_PROGRAM)
                self.assertEqual(writes, -(-engine.total_pages // ppw))
                self.assertEqual(node.staged(len(engine.image)), engine.image)
        for bad in (0, 4):
            with self.assertRaises(ValueError):
                FotaEngine(self.package(), target="headset", pages_per_write=bad)

    def test_unrelated_notifications_are_ignored(self):
        node = FakeNode()
        node.noise_every = 1
        engine, node, transport = self.setup_engine(node=node)
        self.assertTrue(engine.execute()["success"])
        self.assertEqual(node.staged(len(engine.image)), engine.image)
        # Thousands of requests go out without a drain; the notifications kept on the side stay bounded
        for i in range(500):
            transport._keep_notification(notification(0x2CB1, bytes([i % 256])))
        self.assertEqual(len(transport.pending_notifications), Transport.MAX_PENDING_NOTIFICATIONS)
        self.assertEqual(transport.pending_notifications[-1], notification(0x2CB1, bytes([499 % 256])))

    def test_dry_run_sends_nothing(self):
        node = FakeNode()
        transport = SimTransport(node)
        pkg = self.package()
        res = FotaEngine(pkg, target="headset", client=FakeClient(transport, node), dry_run=True).execute()
        self.assertTrue(res["dry_run"])
        self.assertEqual(node.requests, [])


# ==============================================================================
# Recovering from, and stopping on, device misbehaviour
# ==============================================================================

class TestTransferFaults(FotaTestCase):

    def test_page_write_retries_then_succeeds(self):
        for action in ("nak", "checksum", "drop", "stale"):
            with self.subTest(action=action):
                node = FakeNode()
                node.fault(fota.CMD_STORAGE_PAGE_PROGRAM, None, None, action, action, action)
                engine, node, _ = self.setup_engine(node=node)
                self.assertTrue(engine.execute()["success"])
                self.assertEqual(node.staged(len(engine.image)), engine.image)
                self.assertEqual(node.cmds().count(fota.CMD_STORAGE_PAGE_PROGRAM), engine.total_pages + 3)

    def test_page_write_gives_up_without_committing(self):
        node = FakeNode()
        node.fault(fota.CMD_STORAGE_PAGE_PROGRAM, None, None, *["nak"] * firmware.FOTA_PAGE_ATTEMPTS)
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaises(FotaTransferError) as cm:
            engine.execute()
        self.assertNotIsInstance(cm.exception, FotaInterruptedError)
        self.assertEqual(cm.exception.chunks_sent, 2)
        self.assertIn("Nothing was installed", str(cm.exception))
        self.assertIn("v1.0.5.7.4", str(cm.exception))
        self.assert_not_committed(node)

    def test_page_read_retries_and_mismatch(self):
        node = FakeNode()
        node.fault(fota.CMD_STORAGE_PAGE_READ, None, "drop", "stale", "nak")
        engine, node, _ = self.setup_engine(node=node)
        self.assertTrue(engine.execute()["success"])

        # Wrong data read back is not retried away: the staged image is bad, so stop.
        node = FakeNode()
        node.fault(fota.CMD_STORAGE_PAGE_READ, None, None, "corrupt")
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaisesRegex(FotaTransferError, "Read-back mismatch"):
            engine.execute()
        self.assertNotIn(fota.CMD_FOTA_INTEGRITY_CHECK, node.cmds())
        self.assert_not_committed(node)

    def test_missing_erase_is_caught_by_read_back(self):
        node = FakeNode()
        # The device claims the erase worked but leaves old data: writes can't set bits back to 1
        real_reply = node._reply

        def lying_erase(cmd, head, body, action):
            if cmd == fota.CMD_STORAGE_PARTITION_ERASE:
                return response(cmd, b"\x00", head)
            return real_reply(cmd, head, body, action)
        node._reply = lying_erase
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaisesRegex(FotaTransferError, "Read-back mismatch"):
            engine.execute()
        self.assert_not_committed(node)

    def test_start_and_erase_failures(self):
        for cmd, action in ((fota.CMD_FOTA_START, "nak"), (fota.CMD_FOTA_START_TRANSACTION, "drop"),
                            (fota.CMD_FOTA_WRITE_STATE, "nak"), (fota.CMD_STORAGE_PARTITION_ERASE, "nak"),
                            (fota.CMD_STORAGE_PARTITION_ERASE, "drop")):
            with self.subTest(cmd=hex(cmd), action=action):
                node = FakeNode()
                node.fault(cmd, action)
                engine, node, transport = self.setup_engine(node=node)
                with self.assertRaises(FotaTransferError) as cm:
                    engine.execute()
                self.assertIn("Nothing was installed", str(cm.exception))
                if cmd == fota.CMD_STORAGE_PARTITION_ERASE:
                    self.assertIn("partly erased", str(cm.exception))
                else:
                    self.assertIn("Nothing was written", str(cm.exception))
                self.assertEqual(cm.exception.chunks_sent, 0)
                self.assertNotIn(fota.CMD_STORAGE_PAGE_PROGRAM, node.cmds())
                self.assertEqual(transport.lock_calls, ["exclusive", "shared"])

    def test_device_disappears_mid_transfer(self):
        node = FakeNode()
        node.fault(fota.CMD_STORAGE_PAGE_PROGRAM, None, None, None, "gone")
        engine, node, transport = self.setup_engine(node=node)
        with self.assertRaises(FotaTransferError) as cm:
            engine.execute()
        self.assertEqual(cm.exception.chunks_sent, 3)
        self.assertIn("No such device", str(cm.exception))
        self.assertEqual(transport.reconnects, 0)
        self.assertTrue(transport.auto_reconnect)
        self.assertEqual(transport.lock_calls, ["exclusive", "shared"])
        self.assert_not_committed(node)

    def test_cancel(self):
        node = FakeNode()
        node.fault(fota.CMD_STORAGE_PAGE_PROGRAM, None, None, None, None, None, "interrupt")
        engine, node, transport = self.setup_engine(node=node)
        with self.assertRaises(FotaInterruptedError) as cm:
            engine.execute()
        self.assertEqual(cm.exception.chunks_sent, 5)
        self.assertIn("Nothing was installed", str(cm.exception))
        self.assertNotIn("Do NOT unplug", str(cm.exception))
        self.assertEqual(transport.lock_calls, ["exclusive", "shared"])

        node = FakeNode()
        node.fault(fota.CMD_FOTA_START, "interrupt")
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaisesRegex(FotaInterruptedError, "Nothing was written"):
            engine.execute()

        engine, node, _ = self.setup_engine()
        engine.cancelled = True
        with self.assertRaises(FotaInterruptedError):
            engine.execute()
        self.assert_nothing_staged(node)

    def test_device_ends_session(self):
        # A stop in place of a page-write reply fails at once, without retrying that page.
        node = FakeNode()
        node.fault(fota.CMD_STORAGE_PAGE_PROGRAM, None, None, "stop")
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaises(FotaTransferError) as cm:
            engine.execute()
        self.assertIn("0x1C03", str(cm.exception))
        self.assertIn("Nothing was installed", str(cm.exception))
        self.assertEqual(node.cmds().count(fota.CMD_STORAGE_PAGE_PROGRAM), 3)
        self.assert_not_committed(node)

        # Without the restart after the read-back, the integrity check meets a stop, like on real hardware.
        engine, node, _ = self.setup_engine()
        real_open, calls = engine._open_session, []

        def only_first(t):
            calls.append(t)
            if len(calls) == 1:
                real_open(t)
        with mock.patch.object(engine, "_open_session", side_effect=only_first):
            with self.assertRaisesRegex(FotaTransferError, "0x1C03.*0x1C01"):
                engine.execute()
        self.assertEqual(len(calls), 2)
        self.assertEqual(node.stops_sent, 1)
        self.assert_not_committed(node)

    def test_transport_aborts_on_device_command(self):
        node = FakeNode()
        transport = SimTransport(node)
        node.fault(fota.CMD_FOTA_INTEGRITY_CHECK, "stop")
        from maxwell2.transport import DeviceAbortError
        with self.assertRaises(DeviceAbortError):
            transport.exchange(fota.build_integrity_check(), timeout_sec=5, abort_cmds=(fota.CMD_FOTA_STOP,))
        # Without abort_cmds a device command is ignored, and the wait runs out as before
        node.fault(fota.CMD_FOTA_INTEGRITY_CHECK, "stop")
        with self.assertRaises(TimeoutError):
            transport.exchange(fota.build_integrity_check(), timeout_sec=0.05)

    def test_integrity_check_failure(self):
        for action in ("nak", "drop"):
            with self.subTest(action=action):
                node = FakeNode()
                node.fault(fota.CMD_FOTA_INTEGRITY_CHECK, action)
                engine, node, _ = self.setup_engine(node=node)
                with self.assertRaisesRegex(FotaTransferError, "Nothing was installed"):
                    engine.execute()
                self.assert_not_committed(node)

    def test_commit_outcomes(self):
        # Refused: nothing was installed, and the message must not tell the user to wait for an install.
        node = FakeNode()
        node.fault(fota.CMD_FOTA_COMMIT, "nak")
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaises(FotaTransferError) as cm:
            engine.execute()
        self.assertIn("refused the commit", str(cm.exception))
        self.assertIn("Nothing was installed", str(cm.exception))
        self.assertNotIn("Do NOT unplug", str(cm.exception))

        # The device reboots before its reply gets out: carry on to the version check.
        for action in ("drop", "gone"):
            with self.subTest(action=action):
                node = FakeNode()
                node.fault(fota.CMD_FOTA_COMMIT, action)
                engine, node, transport = self.setup_engine(node=node, after="v1.0.5.7.4")
                res = engine.execute()
                self.assertEqual((res["success"], res["verification"]), (True, "confirmed"))
                self.assertTrue(transport.auto_reconnect)

        # The state write just before commit is part of the commit step: losing it isn't "nothing installed".
        node = FakeNode()
        node.fault(fota.CMD_FOTA_COMMIT, "interrupt")
        engine, node, _ = self.setup_engine(node=node)
        with self.assertRaisesRegex(FotaInterruptedError, "Do NOT unplug"):
            engine.execute()

    def test_custom_eq_restored_after_update(self):
        # Seen on a Maxwell 2 headset: the update reset the custom EQ slots to flat.
        freqs = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

        def curve(*gains):
            gains = list(gains) + [0.0] * (10 - len(gains))
            return [{"band_index": 513, "frequency_hz": float(f), "gain_db": float(g), "q": f / 2.0}
                    for f, g in zip(freqs, gains)]

        class EqClient(FakeClient):
            def __init__(self, *a, fail_slot=None, **kw):
                super().__init__(*a, **kw)
                self.eq = {0: curve(12, -12, 8), 1: curve(), 2: curve(), 3: curve(6, 6, 6)}
                self.writes, self.fail_slot = [], fail_slot

            def dump_all(self):
                return {"custom_eq_profiles": {f"custom_slot_{s + 1}": {"bands": [dict(b) for b in self.eq[s]]}
                                               for s in range(4)}}

            def read_custom_eq(self, slot):
                if self.node.committed:            # the install flattened every slot
                    self.eq = {s: curve() if s not in self.restored else self.eq[s] for s in range(4)}
                return {"bands": self.eq[slot]}

            restored = set()

            def write_custom_eq(self, slot, bands, activate=True):
                if slot == self.fail_slot:
                    raise RuntimeError("EQ coefficients did not verify")
                self.writes.append((slot, activate))
                self.restored.add(slot)
                self.eq[slot] = bands

        def run(**kw):
            engine, node, transport = self.setup_engine()
            engine.client = EqClient(transport, node, after=engine.package.version_string, **kw)
            EqClient.restored = set()
            return engine, engine.client

        with mock.patch.object(firmware, "create_backup", lambda action, data: "/tmp/sim-backup.json"):
            engine, client = run()
            res = engine.execute()
            self.assertTrue(res["success"])
            self.assertEqual(res["eq_restored"], [1, 4])               # only the slots that changed
            self.assertEqual(client.writes, [(0, False), (3, False)])  # never switches the preset
            self.assertEqual([b["gain_db"] for b in client.eq[0]][:3], [12.0, -12.0, 8.0])
            self.assertEqual(res["eq_restore_errors"], [])

            engine, client = run(fail_slot=3)
            res = engine.execute()
            self.assertTrue(res["success"])                             # a restore problem doesn't fail it
            self.assertEqual(res["eq_restored"], [1])
            self.assertEqual(len(res["eq_restore_errors"]), 1)
            self.assertIn("slot 4", res["eq_restore_errors"][0])

            engine, client = run()                                       # install not confirmed: leave EQ alone
            engine.client.node.install_on_commit = False
            res = engine.execute()
            self.assertEqual(res["verification"], "still_staged")
            self.assertEqual((client.writes, res["eq_restored"]), ([], []))

            engine, client = run()                                       # stage-only: nothing to restore
            engine.stage_only = True
            res = engine.execute()
            self.assertEqual((client.writes, res["eq_restored"]), ([], []))

    def test_post_update_version_check(self):
        res = self.setup_engine()[0].execute()
        self.assertEqual((res["success"], res["verification"]), (True, "confirmed"))

        res = self.setup_engine(after="v1.0.2.60.2", engine_kw={"allow_downgrade": True},
                                versions={"dongle": "v1.0.5.7.4", "headset": "v1.0.5.7.4"})[0].execute()
        self.assertEqual((res["success"], res["verification"]), (False, "version_mismatch"))
        self.assertEqual(res["reported_version"], "v1.0.2.60.2")

        res = self.setup_engine(after="Unknown")[0].execute()
        self.assertEqual((res["success"], res["verification"]), (False, "no_response"))

        # When the staging area can't be read: with the same version before and after, a match only
        # counts once the device has been seen rebooting, since the old firmware reports that version too.
        no_staging = mock.patch.object(FotaEngine, "_staging_consumed", return_value=None)
        with no_staging:
            engine = self.setup_engine(reboot_polls=0)[0]
            res = engine.execute()
            self.assertEqual(engine.installed_version, engine.package.version_string)
            self.assertEqual((res["success"], res["verification"]), (False, "no_reboot_seen"))

            engine, _, transport = self.setup_engine(reboot_polls=0)
            real_read = engine._read_target_version

            def read_after_reconnect():
                transport.reconnect_count += 1   # what Transport.reconnect() records
                return real_read()
            engine._read_target_version = read_after_reconnect
            self.assertEqual(engine.execute()["verification"], "confirmed")

            # A different version reported afterwards proves the install by itself
            res = self.setup_engine(version="v1.0.5.7.4", reboot_polls=0, node=FakeNode(version="v1.0.2.60.2"))[0].execute()
            self.assertEqual((res["success"], res["verification"]), (True, "confirmed"))

        # A cleared staging area proves it even without a reboot being seen
        res = self.setup_engine(reboot_polls=0)[0].execute()
        self.assertEqual((res["success"], res["verification"]), (True, "confirmed"))

        # Seen on the dongle: it answered with the right version seconds after the commit, then
        # rebooted again to install. Only a cleared staging area proves the install has happened.
        node = FakeNode()
        node.install_on_commit = False
        res = self.setup_engine(node=node)[0].execute()
        self.assertEqual((res["success"], res["verification"]), (False, "still_staged"))

        node = FakeNode()
        node.install_on_commit = False
        engine, node, _ = self.setup_engine(node=node, reboot_polls=2)
        real_read = engine._read_target_version
        polls = []

        def installs_on_third_poll():
            polls.append(1)
            if len(polls) == 3:
                node.flash[:] = b"\xFF" * node.staging_len
            return real_read()
        engine._read_target_version = installs_on_third_poll
        res = engine.execute()
        self.assertEqual(res["verification"], "confirmed")
        self.assertGreaterEqual(len(polls), 3)

        # If the staging area can't be read, the version-and-reboot rule still applies
        engine = self.setup_engine()[0]
        with mock.patch.object(engine, "_staging_consumed", return_value=None):
            self.assertEqual(engine.execute()["verification"], "confirmed")

        # Install progress is reported while waiting, so the CLI isn't silent during the reboot
        phases = []
        self.setup_engine(reboot_polls=3)[0].execute(progress_callback=lambda p: phases.append(p["phase"]))
        self.assertIn("install", phases)


# ==============================================================================
# Preflight: every refusal happens before anything is erased
# ==============================================================================

class TestPreflight(FotaTestCase):

    def test_no_chip_is_verified_for_live_flashing(self):
        self.assertFalse(any(live for _, live in firmware.KNOWN_CHIP_IDS.values()))
        for chip in firmware.FOTA_EXPERIMENTAL_CHIPS:
            self.assertIn(chip, firmware.KNOWN_CHIP_IDS)

    def test_live_flash_needs_experimental(self):
        engine, node, _ = self.setup_engine(engine_kw={"experimental": False})
        with self.assertRaisesRegex(FotaSafetyError, "not supported yet without --experimental"):
            engine.execute()
        self.assertEqual(node.requests, [])

        # experimental does not unlock a chip outside FOTA_EXPERIMENTAL_CHIPS
        pkg = self.package("dongle", chip="AB1568_Dongle")
        engine, node, _ = self.setup_engine("dongle", pkg=pkg)
        with self.assertRaisesRegex(FotaSafetyError, "not supported yet"):
            engine.execute()
        self.assertEqual(node.requests, [])

    def test_staging_area_checks(self):
        pages = self.package().file_size // 256 + 1
        cases = [
            ("too small", FakeNode(staging=(0x004BC000, 0x1000)), None, "does not fit"),
            ("overlaps firmware", FakeNode(staging=(0x08010000, 0x10000)), None, "overlaps"),
            ("query rejected", FakeNode(), "nak", "FOTA partition"),
            ("query unanswered", FakeNode(), "drop", "FOTA partition"),
            ("implausible", FakeNode(staging=(0x004BC000, 0)), None, "FOTA partition"),
        ]
        self.assertGreater(pages * 256, 0x1000)
        for name, node, action, err in cases:
            with self.subTest(name):
                if action:
                    node.fault(fota.CMD_FOTA_PARTITION_INFO, action)
                engine, node, transport = self.setup_engine(node=node)
                with self.assertRaisesRegex(FotaSafetyError, err):
                    engine.execute()
                self.assertEqual(node.cmds(), [fota.CMD_FOTA_PARTITION_INFO])
                self.assertEqual(transport.lock_calls, [])

    def test_device_in_use(self):
        engine, node, transport = self.setup_engine(transport_kw={"busy": True})
        with self.assertRaisesRegex(FotaSafetyError, "in use"):
            engine.execute()
        self.assertEqual(node.cmds(), [fota.CMD_FOTA_PARTITION_INFO])
        self.assertTrue(transport.auto_reconnect)

    def test_fails_closed_on_unreadable_device_state(self):
        cases = [
            ("battery None", dict(battery=None), "battery"),
            ("battery error", dict(battery_error=OSError("timeout")), "battery"),
            ("battery low", dict(battery=30), "30%"),
            ("version unknown", dict(versions={"dongle": "Unknown", "headset": "Unknown"}), "version"),
            ("version error", dict(version_error=OSError("timeout")), "version"),
            ("unknown release", dict(versions={"dongle": "v1.0.5.9", "headset": "v1.0.5.9"}), "not a known release"),
            ("ANC device", dict(versions={"dongle": "v1.0.5.6", "headset": "v1.0.5.6"}), "track mismatch"),
        ]
        for name, kw, err in cases:
            with self.subTest(name):
                engine, node, _ = self.setup_engine(**kw)
                with self.assertRaisesRegex(FotaSafetyError, err):
                    engine.execute()
                self.assertEqual(node.requests, [])

        # The dongle doesn't need the battery check (it's bus powered)
        engine, _, _ = self.setup_engine("dongle", battery=None)
        self.assertTrue(engine.execute()["success"])

        # The ANC dongle PIDs mark the device as ANC even when its version is a regular release.
        for pid in (0x4BA8, 0x4BA9):
            engine, node, _ = self.setup_engine(transport_kw={"product_id": pid})
            with self.assertRaisesRegex(FotaSafetyError, "track mismatch"):
                engine.execute()
            self.assertEqual(node.requests, [])

        pkg = self.package()
        dry = SimTransport(FakeNode())
        dry.dry_run = True
        for client in (None, FakeClient(dry, dry.node)):
            with self.subTest(client=client):
                with self.assertRaisesRegex(FotaSafetyError, "device connection|dry-run"):
                    FotaEngine(pkg, target="headset", client=client, dry_run=False, experimental=True).preflight_check()

    def test_battery_rule_while_charging(self):
        # Readings on the charger run high (85% charging settled at 62% unplugged), so 90% is needed then.
        for via_dongle, usb, battery, ok in (
            (True, False, 50, True),     # on battery, through the dongle: 50% is enough
            (True, False, 49, False),
            (True, True, 85, False),     # charging (headset also on USB-C), flashed through the dongle
            (False, None, 85, False),    # flashed over USB-C: always charging
            (False, None, 90, True),
            (True, None, 50, True),      # FakeClient reports not powered when on the dongle
        ):
            with self.subTest(via_dongle=via_dongle, usb=usb, battery=battery):
                engine, node, _ = self.setup_engine(via_dongle=via_dongle, battery=battery, usb_powered=usb,
                                                    transport_kw={"product_id": 0x4B28 if via_dongle else 0x4B2E})
                if ok:
                    self.assertEqual(engine.preflight_check()["battery_percent"], battery)
                else:
                    with self.assertRaisesRegex(FotaSafetyError, f"{battery}%"):
                        engine.preflight_check()
                    self.assertEqual(node.requests, [])
        engine, _, _ = self.setup_engine(via_dongle=False, battery=85, transport_kw={"product_id": 0x4B2E})
        with self.assertRaisesRegex(FotaSafetyError, "while charging"):
            engine.preflight_check()

        # Unknown power state counts as charging
        self.assertEqual(firmware.required_battery_pct(None), firmware.MIN_HEADSET_BATTERY_PCT_CHARGING)
        self.assertEqual(firmware.required_battery_pct(False), firmware.MIN_HEADSET_BATTERY_PCT)

        class NoUsbInfo:
            transport = type("T", (), {"is_dongle": True})()
        self.assertIsNone(firmware.headset_usb_powered(NoUsbInfo()))

    def test_battery_reading_must_settle(self):
        # Just unplugged: the reading slides down to its real value; preflight uses the settled one.
        engine, _, _ = self.setup_engine(battery=[76, 75, 74, 62, 62])
        self.assertEqual(engine.preflight_check()["battery_percent"], 62)

        engine, node, _ = self.setup_engine(battery=[60, 55, 50, 45, 49])  # settles below 50 -> refused
        with self.assertRaisesRegex(FotaSafetyError, "49%"):
            engine.preflight_check()

        import itertools
        engine, node, _ = self.setup_engine()
        flip = itertools.cycle([80, 81])          # never two equal readings in a row
        engine.client.get_battery = lambda: next(flip)
        with self.assertRaisesRegex(FotaSafetyError, "still changing"):
            engine.preflight_check()
        self.assertEqual(node.requests, [])

    def test_downgrade_needs_confirmation(self):
        node = FakeNode(version="v1.0.5.7.4")
        engine, node, _ = self.setup_engine(version="v1.0.2.60.2", node=node)
        with self.assertRaisesRegex(FotaSafetyError, "OLDER"):
            engine.execute()
        self.assertEqual(node.requests, [])

        engine, node, _ = self.setup_engine(version="v1.0.2.60.2", node=FakeNode(version="v1.0.5.7.4"),
                                            engine_kw={"allow_downgrade": True})
        self.assertTrue(engine.preflight_check()["is_downgrade"])
        self.assertTrue(engine.execute()["success"])

    def test_tampered_package(self):
        pkg = self.package()
        pkg.outer_hash_valid = False
        engine, node, _ = self.setup_engine(pkg=pkg)
        with self.assertRaisesRegex(FotaSafetyError, "INVALID"):
            engine.execute()
        self.assertEqual(node.requests, [])


# ==============================================================================
# The connection changing under a run (headset unplugged from USB-C, device re-enumerated)
# ==============================================================================

def reconnect_to_dongle(transport):
    """What Transport.reconnect() does when the headset's USB node is gone but the dongle is there."""
    transport.hidraw_path, transport.product_id, transport.is_dongle = "/dev/hidraw-dongle", 0x4B28, True


class TestConnectionChanges(FotaTestCase):

    def test_get_firmware_keeps_route_found_by_reconnect(self):
        # Seen on real hardware: the headset's USB node vanished during get_firmware(); the transport
        # reconnected to the dongle, then get_firmware restored the old route flag (0x00), so the
        # "headset" query went to the dongle and the dongle's version was reported as the headset's.
        from maxwell2.client import MaxwellClient
        from maxwell2.protocol import CMD_FIRMWARE

        class StubTransport:
            dry_run = False
            hidraw_path, product_id, is_dongle = "/dev/hidraw-usb", 0x4B2E, False

            def __init__(self):
                self.routes = []

            def query_notification(self, packet, timeout_sec=2.0, is_dongle=None):
                if not self.routes:
                    reconnect_to_dongle(self)   # the first request finds the USB node gone
                self.routes.append(self.is_dongle if is_dongle is None else is_dongle)
                return None

        t = StubTransport()
        MaxwellClient(t).get_firmware()
        self.assertTrue(t.is_dongle)                 # the re-detected route survives
        self.assertEqual(t.routes, [False, True])    # dongle on 0x00, headset relayed on 0x80

    def test_node_change_after_preflight_refuses(self):
        engine, node, transport = self.setup_engine(via_dongle=False, transport_kw={"product_id": 0x4B2E})
        engine.preflight_check()
        reconnect_to_dongle(transport)   # e.g. unplugged while the CONFIRM prompt was open
        with self.assertRaisesRegex(FotaSafetyError, "connection changed"):
            engine.execute()
        # Refused by the second preflight, before the device is even locked
        self.assertEqual(node.cmds(), [fota.CMD_FOTA_PARTITION_INFO])
        self.assertEqual(transport.lock_calls, [])

        # And if it changes after the second preflight, the session can't start on the new node
        engine, node, transport = self.setup_engine(via_dongle=False, transport_kw={"product_id": 0x4B2E})
        real_claim = transport.claim_exclusive

        def claim_then_reconnect():
            real_claim()
            reconnect_to_dongle(transport)
        transport.claim_exclusive = claim_then_reconnect
        with self.assertRaisesRegex(FotaSafetyError, "connection changed"):
            engine.execute()
        self.assertEqual(node.cmds(), [fota.CMD_FOTA_PARTITION_INFO])
        self.assertTrue(transport.auto_reconnect)
        self.assertEqual(transport.lock_calls, ["exclusive", "shared"])

    def test_node_change_during_preflight_refuses(self):
        engine, node, transport = self.setup_engine(via_dongle=False, transport_kw={"product_id": 0x4B2E})
        real_battery = engine.client.get_battery

        def battery_then_reconnect():
            reconnect_to_dongle(transport)
            return real_battery()
        engine.client.get_battery = battery_then_reconnect
        with self.assertRaisesRegex(FotaSafetyError, "connection changed"):
            engine.execute()
        self.assertEqual(node.requests, [])

    def test_reconnect_retried_after_finding_no_device(self):
        # Seen after the first real commit: the headset rebooted and the dongle reset with it, so the
        # first reconnect found no device and left the transport closed. Every later version query then
        # failed with "not open", and the update was never confirmed although it had installed.
        from maxwell2.transport import DeviceNotFoundError

        class RebootingTransport(Transport):
            def __init__(self):
                super().__init__("/dev/hidraw-sim")
                self.fd = -2              # descriptor of the node that just went away
                self.present = False      # nothing enumerated yet
                self.sent = 0

            def reconnect(self):
                self.reconnect_count += 1
                self.fd = None
                if not self.present:
                    raise DeviceNotFoundError("no device yet")
                self.fd = -1

            def _write_reports(self, race_packet, is_dongle):
                if self.fd == -2:
                    raise OSError(errno.ENODEV, "No such device")
                self.sent += 1
                return []

        t = RebootingTransport()
        pkt = fota.build_partition_info_query()
        with self.assertRaises(DeviceNotFoundError):
            t.write_race_packet(pkt)        # device gone, nothing back yet
        with self.assertRaises(DeviceNotFoundError):
            t.write_race_packet(pkt)        # still nothing: tried again, not "not open"
        t.present = True
        t.write_race_packet(pkt)            # back: picked up again
        self.assertEqual((t.sent, t.reconnect_count, t.fd), (1, 3, -1))

        t2 = Transport("/dev/hidraw-sim")   # never opened: still an error, no surprise open
        with self.assertRaisesRegex(RuntimeError, "not open"):
            t2.write_race_packet(pkt)
        t.fd, t.auto_reconnect = None, False  # during a flash: no reopening
        with self.assertRaisesRegex(RuntimeError, "not open"):
            t.write_race_packet(pkt)

    def test_reconnect_prefers_same_kind_of_node(self):
        devices = [{"path": "/dev/hidraw1", "pid": 0x4B2E, "is_dongle": False, "model": "USB"},
                   {"path": "/dev/hidraw7", "pid": 0x4B28, "is_dongle": True, "model": "Dongle"}]
        opened = []

        def fake_open(t):
            if not t.hidraw_path:
                t.hidraw_path = "/dev/hidraw1"      # find_device() prefers a USB-C headset
            dev = next(d for d in devices if d["path"] == t.hidraw_path)
            t.product_id, t.is_dongle, t.node_is_dongle = dev["pid"], dev["is_dongle"], dev["is_dongle"]
            t.fd = -1
            opened.append(t.hidraw_path)

        with mock.patch("maxwell2.transport.list_maxwell_devices", return_value=devices), \
                mock.patch.object(Transport, "open", fake_open), mock.patch.object(Transport, "close", lambda t: None):
            t = Transport("/dev/hidraw10")          # was the dongle, now renumbered to hidraw7
            t.node_is_dongle, t.is_dongle = True, True
            t.reconnect()
            self.assertEqual((t.hidraw_path, t.is_dongle), ("/dev/hidraw7", True))
            t.reconnect(prefer_dongle=False)
            self.assertEqual((t.hidraw_path, t.is_dongle), ("/dev/hidraw1", False))
            t.reconnect()                           # explicit path still held by the same kind: kept
            self.assertEqual(t.hidraw_path, "/dev/hidraw1")

    def test_dongle_version_only_counts_from_a_dongle_node(self):
        # After a dongle update the transport may land on the headset's USB node, where route 0x00
        # reaches the headset: its version must not be taken for the dongle's.
        engine, node, transport = self.setup_engine("dongle", reboot_polls=0)
        real_get = engine.client.get_firmware

        def get_firmware():
            if node.committed:
                transport.is_dongle = False          # reconnected to the headset node
            return real_get()
        engine.client.get_firmware = get_firmware
        with mock.patch.object(firmware, "list_maxwell_devices", return_value=[]):
            res = engine.execute()
        self.assertEqual(res["verification"], "no_response")

        # With a dongle listed, the engine goes back to it and reads the version there
        engine, node, transport = self.setup_engine("dongle", reboot_polls=0)
        real_get = engine.client.get_firmware
        reopened = []

        def back_to_dongle(prefer_dongle=None):
            reopened.append(prefer_dongle)
            transport.is_dongle = True
            transport.reconnect_count += 1

        def first_read_lands_on_headset():
            if node.committed and not reopened:
                transport.is_dongle = False          # the next read must not trust this node
            return real_get()
        transport.reconnect = back_to_dongle
        engine.client.get_firmware = first_read_lands_on_headset
        dongle_listed = [{"path": "/dev/hidraw7", "pid": 0x4B28, "is_dongle": True, "model": "Dongle"}]
        with mock.patch.object(firmware, "list_maxwell_devices", return_value=dongle_listed):
            res = engine.execute()
        self.assertEqual(res["verification"], "confirmed")
        self.assertEqual(reopened, [True])

    def test_partition_query_never_reconnects(self):
        node = FakeNode()
        node.fault(fota.CMD_FOTA_PARTITION_INFO, "gone")
        engine, node, transport = self.setup_engine(node=node)
        with self.assertRaisesRegex(FotaSafetyError, "FOTA partition"):
            engine.execute()
        self.assertEqual(transport.reconnects, 0)
        self.assertTrue(transport.auto_reconnect)


# ==============================================================================
# Device lock between our own processes
# ==============================================================================

class TestDeviceLock(unittest.TestCase):

    def test_exclusive_claim(self):
        with tempfile.NamedTemporaryFile() as node, mock.patch.object(Transport, "drain"), \
                mock.patch("maxwell2.transport.list_maxwell_devices", return_value=[]):
            a, b = Transport(node.name), Transport(node.name)
            a.open()
            b.open()                       # two normal instances share the device
            with self.assertRaises(DeviceBusyError):
                a.claim_exclusive()        # but an update can't start while another is open
            self.assertFalse(a.exclusive)
            b.close()
            a.claim_exclusive()
            c = Transport(node.name)
            with self.assertRaisesRegex(DeviceBusyError, "firmware update"):
                c.open()                   # nothing else can open it during the update
            self.assertIsNone(c.fd)
            a.release_exclusive()
            c.open()
            c.close()
            a.close()


if __name__ == "__main__":
    unittest.main()
