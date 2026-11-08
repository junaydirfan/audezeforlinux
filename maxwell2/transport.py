"""
Low-level Linux hidraw transport for Audeze Maxwell 2.
Uses Python standard library (os, fcntl, select, glob).
Supports both USB Control GET_REPORT (HIDIOCGINPUT) and Interrupt IN.
Supports --dry-run mode to inspect outgoing packets without transmitting.
"""

import os
import sys
import glob
import time
import errno
import fcntl
import struct
import select
import threading
from functools import wraps
from typing import Optional, List, Tuple

from maxwell2.protocol import (
    REPORT_LENGTH,
    REPORT_ID_OUTPUT,
    REPORT_ID_INPUT,
    InputStream,
    frame_output_reports,
    parse_race_packet,
    ProtocolError,
)

AUDEZE_VID = 0x3329
KNOWN_PIDS = [0x4B18, 0x4B19, 0x4B1A, 0x4B1E, 0x4B28, 0x4B29, 0x4B2A, 0x4B2E, 0x4BA8, 0x4BA9]
DONGLE_PIDS = [0x4B18, 0x4B19, 0x4B28, 0x4B29, 0x4BA8, 0x4BA9]
# Maxwell 2 ANC units. The Xbox dongle (4BA8) was reported in issue #3 and the PlayStation
# dongle (4BA9) in issue #4; the USB-C ID is still unknown.
ANC_PIDS = [0x4BA8, 0x4BA9]

# Linux HIDRAW ioctl definitions:
# HIDIOCGINPUT(len) = _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x0A, len)
IOC_READ_WRITE = 3
IOC_TYPE_H = 72  # ord('H')


def _HIDIOCGINPUT(length: int) -> int:
    return (IOC_READ_WRITE << 30) | (length << 16) | (IOC_TYPE_H << 8) | 0x0A


# usbdevfs USBDEVFS_RESET = _IO('U', 20): a USB port reset, as the host does when a device is plugged in.
USBDEVFS_RESET = (ord("U") << 8) | 20
# How long a reset dongle gets to show up again before giving up
RESET_SETTLE_SEC = 10.0


# Errors a write gets once the hidraw node has gone away. The headset and dongle both
# re-enumerate (with new /dev/hidrawN numbers) when the headset is switched on or off.
DEVICE_GONE_ERRNOS = (errno.ENODEV, errno.EIO, errno.EBADF, errno.ENOENT, errno.EPIPE, errno.ESHUTDOWN)


def _locked(method):
    """Serialises access to the device, so a background poll can't interleave with a request."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return guarded

class DeviceNotFoundError(Exception):
    """Raised when no compatible Audeze Maxwell device node is found."""
    pass


class DevicePermissionError(Exception):
    """Raised when the user does not have read/write access to /dev/hidraw*."""
    pass


class DeviceBusyError(Exception):
    """Raised when another audezeforlinux process holds the device (e.g. a firmware update)."""
    pass


class DeviceAbortError(ProtocolError):
    """Raised when the device sends a command that ends the exchange (e.g. FOTA stop) instead of a reply."""
    pass


PID_MODELS = {
    0x4B28: "Maxwell 2 (Xbox Dongle)",
    0x4B29: "Maxwell 2 (PlayStation / PC Dongle)",
    0x4B2A: "Maxwell 2 (Direct USB)",
    0x4B2E: "Maxwell 2 (Xbox Direct USB)",
    0x4BA8: "Maxwell 2 ANC (Xbox Dongle)",
    0x4BA9: "Maxwell 2 ANC (PlayStation Dongle)",
    0x4B18: "Maxwell 1 (Xbox Dongle)",
    0x4B19: "Maxwell 1 (PlayStation Dongle)",
    0x4B1A: "Maxwell 1 (PlayStation USB)",
    0x4B1E: "Maxwell 1 (Xbox USB)",
}


# Vendor collection that carries the control protocol (reports 0x06 / 0x07), on every Maxwell.
# HeadsetControl selects it the same way (usage page 0xFF13, usage 1): interface 0 on the original
# Maxwell, interface 5 on the Maxwell 2 dongle.
CONTROL_USAGE_PAGE = 0xFF13


def descriptor_usage_pages(descriptor: bytes) -> List[int]:
    """Usage pages declared in a HID report descriptor (global Usage Page items), in order."""
    pages = []
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item: 0xFE, data size, tag, data
            if i + 1 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        if prefix & 0xFC == 0x04:  # global item, tag 0: Usage Page
            pages.append(int.from_bytes(descriptor[i + 1:i + 1 + size], "little"))
        i += 1 + size
    return pages


def _node_number(path: str) -> int:
    digits = "".join(ch for ch in os.path.basename(path) if ch.isdigit())
    return int(digits) if digits else 0


def list_maxwell_devices(include_all: bool = False) -> List[dict]:
    """
    Scans sysfs to locate all connected Audeze Maxwell hidraw nodes.
    Returns list of dicts: [{"path": "/dev/hidrawX", "pid": pid, "is_dongle": bool, "model": str,
    "interface": "1-3:1.0", "has_control": True/False/None}]
    Nodes whose HID descriptor lacks the control collection (usage page 0xFF13) can't carry the
    protocol and are left out unless include_all; nodes whose descriptor can't be read are kept.
    Sorted by node number (hidraw2 before hidraw10).
    """
    found = []
    for hidraw_dir in sorted(glob.glob("/sys/class/hidraw/hidraw*"), key=_node_number):
        uevent_path = os.path.join(hidraw_dir, "device", "uevent")
        if not os.path.exists(uevent_path):
            continue
        try:
            with open(uevent_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            props = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
            hid_id = props.get("HID_ID", "")
            parts = hid_id.split(":")
            if len(parts) >= 3:
                vid = int(parts[1], 16)
                pid = int(parts[2], 16)
                if vid == AUDEZE_VID and pid in KNOWN_PIDS:
                    node_name = os.path.basename(hidraw_dir)
                    dev_path = f"/dev/{node_name}"
                    is_dongle = pid in DONGLE_PIDS
                    model = PID_MODELS.get(pid, f"Audeze Device (PID 0x{pid:04X})")
                    has_control = None
                    try:
                        with open(os.path.join(hidraw_dir, "device", "report_descriptor"), "rb") as f:
                            has_control = CONTROL_USAGE_PAGE in descriptor_usage_pages(f.read())
                    except OSError:
                        pass
                    try:
                        interface = os.path.basename(os.path.realpath(os.path.join(hidraw_dir, "device", "..")))
                    except OSError:
                        interface = ""
                    if has_control is False and not include_all:
                        continue  # e.g. a separate consumer-control interface: no replies ever come from it
                    found.append({
                        "path": dev_path,
                        "pid": pid,
                        "is_dongle": is_dongle,
                        "model": model,
                        "interface": interface,
                        "has_control": has_control,
                    })
        except Exception:
            continue
    return found


def usb_device_node(hidraw_path: str, sys_root: str = "/sys") -> Optional[str]:
    """
    The /dev/bus/usb/BBB/DDD node of the USB device that a hidraw node belongs to, or None.
    Found by walking up from the HID device in sysfs to the first directory with busnum/devnum.
    """
    hid_dir = os.path.realpath(os.path.join(sys_root, "class", "hidraw", os.path.basename(hidraw_path), "device"))
    d = hid_dir
    while d and d != os.path.dirname(d):
        try:
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(d, "devnum")) as f:
                dev = int(f.read())
            return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
        except (OSError, ValueError):
            d = os.path.dirname(d)
    return None


class Transport:
    """
    Handles bidirectional HID communication with the Audeze Maxwell over /dev/hidraw.
    """

    def __init__(self, hidraw_path: Optional[str] = None, dry_run: bool = False):
        self.dry_run = dry_run
        self.hidraw_path = hidraw_path
        # A path given with --device is kept across reconnects while it still exists
        self.explicit_path = hidraw_path is not None
        self.lock = threading.RLock()
        # Off during a firmware transfer: a device that drops off mid-flash must stop the transfer
        self.auto_reconnect = True
        # flock held on the device node: shared normally, exclusive during a firmware update
        self.exclusive = False
        self.fd: Optional[int] = None
        self.is_dongle = True
        self.product_id: Optional[int] = None
        self.input_stream = InputStream()
        self.pending_notifications: List[bytes] = []
        self.reconnect_count = 0  # lets callers notice the device went away (e.g. a reboot after an update)
        # Kind of node opened (dongle or headset USB), unlike is_dongle which callers may override
        self.node_is_dongle: Optional[bool] = None

    @classmethod
    def find_device(cls) -> Tuple[str, int, bool]:
        """
        Scans sysfs to locate the Audeze Maxwell hidraw node.
        Returns (hidraw_path, product_id, is_dongle).
        """
        devices = list_maxwell_devices()
        if devices:
            # A headset on USB-C can be reached without the wireless link, so prefer it.
            d = next((dev for dev in devices if not dev["is_dongle"]), devices[0])
            return d["path"], d["pid"], d["is_dongle"]

        raise DeviceNotFoundError(
            "No compatible Audeze Maxwell device found on /sys/class/hidraw/.\n"
            "Please ensure the USB dongle or headset is plugged in."
        )

    def open(self):
        """Opens the hidraw device node."""
        if self.dry_run:
            print("[DRY-RUN] Transport initialized in simulated dry-run mode (no hardware access).")
            return

        if not self.hidraw_path:
            self.hidraw_path, self.product_id, self.is_dongle = self.find_device()
        else:
            # An explicit --device still needs the right route byte for that hardware.
            for dev in list_maxwell_devices():
                if dev["path"] == self.hidraw_path:
                    self.product_id, self.is_dongle = dev["pid"], dev["is_dongle"]
        self.node_is_dongle = self.is_dongle

        if not os.path.exists(self.hidraw_path):
            raise DeviceNotFoundError(f"Device node {self.hidraw_path} does not exist.")

        try:
            self.fd = os.open(self.hidraw_path, os.O_RDWR)
        except PermissionError:
            raise DevicePermissionError(
                f"Permission denied accessing {self.hidraw_path}.\n"
                f"To run without sudo, install the udev rule:\n"
                f"  sudo cp udev/70-audeze-maxwell.rules /etc/udev/rules.d/\n"
                f"  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                f"Then unplug and replug your Audeze dongle/headset."
            )

        try:
            self._apply_lock()
        except DeviceBusyError:
            self.close()
            raise

        # Drain any pending incoming reports
        self.drain()

    def _apply_lock(self):
        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(self.fd, mode | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                return  # the lock is an advisory guard between our own processes, not a requirement
            raise DeviceBusyError(
                f"{self.hidraw_path} is in use by another audezeforlinux process"
                + (" (close the dashboard and any other instance before a firmware update)." if self.exclusive
                   else ", probably a firmware update. Wait for it to finish.")
            )

    @_locked
    def claim_exclusive(self):
        """Takes sole use of the device for a firmware update, refusing while another instance has it open."""
        if self.dry_run:
            return
        self.exclusive = True
        try:
            self._apply_lock()
        except DeviceBusyError:
            self.exclusive = False
            self._apply_lock()
            raise

    @_locked
    def release_exclusive(self):
        if self.dry_run or not self.exclusive:
            return
        self.exclusive = False
        if self.fd is not None:
            self._apply_lock()

    def close(self):
        """Closes the hidraw file descriptor."""
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    @_locked
    def reconnect(self, prefer_dongle: Optional[bool] = None):
        """
        Reopens the device after it re-enumerated, picking it up again under its new node.
        prefer_dongle picks the kind of node to look for (default: the kind that was open).
        """
        self.reconnect_count += 1
        was_dongle = self.node_is_dongle if prefer_dongle is None else prefer_dongle
        self.close()
        devices = list_maxwell_devices()
        # Node numbers get reused after re-enumeration, so only keep a path still held by the same
        # kind of Maxwell node. Otherwise prefer a node of the same kind: after a dongle firmware
        # update the dongle has to be found again, not the headset on USB-C, where route 0x00
        # reaches the headset and its version would be mistaken for the dongle's.
        same_path = any(d["path"] == self.hidraw_path and d["is_dongle"] == was_dongle for d in devices)
        if not (self.explicit_path and same_path):
            same_kind = next((d for d in devices if d["is_dongle"] == was_dongle), None) if was_dongle is not None else None
            self.hidraw_path = same_kind["path"] if same_kind else None
        self.open()

    @_locked
    def reset_usb_device(self, settle_sec: float = RESET_SETTLE_SEC):
        """
        Resets the USB device behind the open node (USBDEVFS_RESET), then reopens it.
        This is what plugging the device in does, minus the power cut: Maxwell dongles can get
        stuck answering every query with an empty report, and a replug is the known cure.
        Audio drops for about a second. Needs write access to the /dev/bus/usb node, which the
        udev rule grants (DevicePermissionError otherwise). Refused during a firmware update.
        """
        if self.dry_run:
            return
        if self.exclusive or not self.auto_reconnect:
            raise DeviceBusyError("Not resetting the device during a firmware update.")
        node = usb_device_node(self.hidraw_path) if self.hidraw_path else None
        if not node:
            raise DeviceNotFoundError(f"No USB device found behind {self.hidraw_path}.")
        try:
            fd = os.open(node, os.O_WRONLY)
        except PermissionError:
            raise DevicePermissionError(
                f"No permission to reset {node}. Run 'audezeforlinux setup-udev' once (it adds the rule "
                "that allows it), then replug the dongle."
            )
        was_dongle = self.node_is_dongle
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        except OSError as e:
            # ENODEV: the device re-enumerated instead of resetting in place, which is fine too
            if e.errno != errno.ENODEV:
                raise
        finally:
            os.close(fd)
        # Usually reset in place (same node), but it may re-enumerate under a new one
        deadline = time.time() + settle_sec
        while True:
            time.sleep(0.5)
            if any(d["is_dongle"] == was_dongle for d in list_maxwell_devices()) or time.time() >= deadline:
                break
        self.reconnect(prefer_dongle=was_dongle)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @_locked
    def drain(self, max_rounds: int = 64):
        """
        Drains stale input reports from both GET_REPORT and Interrupt IN buffers, until two
        rounds in a row come back empty. Late replies to earlier requests can pile up in the
        dongle, and a leftover battery notification would otherwise be read as the current level.
        """
        self.input_stream.clear()
        self.pending_notifications.clear()
        if self.dry_run or self.fd is None:
            return

        empty_count = 0
        for _ in range(max_rounds):
            got_data = False
            # Check HIDIOCGINPUT
            buf = bytearray(b"\x07" + b"\x00" * 61)
            try:
                fcntl.ioctl(self.fd, _HIDIOCGINPUT(REPORT_LENGTH), buf)
                if buf[1] > 0:
                    got_data = True
            except Exception:
                pass

            # Check Interrupt IN if select indicates readiness
            r, _, _ = select.select([self.fd], [], [], 0.002)
            if r:
                try:
                    os.read(self.fd, 64)
                    got_data = True
                except Exception:
                    pass

            if got_data:
                empty_count = 0
            else:
                empty_count += 1
                if empty_count >= 2:
                    break
            time.sleep(0.005)

    @_locked
    def write_control_fast(self, subcommand: int, value: int):
        """
        Fast low-latency write for interactive sliders (sidetone, chatmix, noise reduction).
        Transmits CMD_SETTING_WRITE and quickly drains the 0x5B ACK without blocking the UI.
        """
        from maxwell2.protocol import build_write_control_command
        cmd = build_write_control_command(subcommand, value)
        self.write_race_packet(cmd)
        if not self.dry_run:
            time.sleep(0.012)
            self.poll_input_report()

    @_locked
    def write_race_packet(self, race_packet: bytes, is_dongle: Optional[bool] = None,
                          reconnect: bool = True) -> List[bytes]:
        """
        Frames and transmits a RACE packet over HID output reports (Report ID 0x06).
        is_dongle overrides the route for this packet only (True = 0x80 relay, False = 0x00).
        If the device node has disappeared (headset switched on/off, cable replugged), the
        device is reopened once and the packet sent again, unless reconnect is False: a firmware
        transfer must stop rather than carry on against a device that has just rebooted.
        In dry-run mode, prints the outgoing report bytes and returns them.
        """
        if self.dry_run:
            reports = frame_output_reports(race_packet, is_dongle=self.is_dongle if is_dongle is None else is_dongle)
            print(f"[DRY-RUN] Sending RACE packet ({len(race_packet)} bytes):")
            print(f"  RACE Raw: {race_packet.hex(' ')}")
            for idx, rep in enumerate(reports):
                print(f"  HID Report #{idx + 1} (Report ID 0x{rep[0]:02X}, len {len(rep)}): {rep.hex(' ')}")
            return reports

        if self.fd is None:
            if not (reconnect and self.auto_reconnect and self.reconnect_count):
                raise RuntimeError("Transport is not open.")
            # An earlier reconnect found no device (e.g. headset and dongle both re-enumerating after
            # a firmware update reboot) and left the transport closed: try again now. Without this,
            # every later request failed with "not open" and the device was never picked up again.
            self.reconnect()
            return self._write_reports(race_packet, is_dongle)

        try:
            return self._write_reports(race_packet, is_dongle)
        except OSError as e:
            if not (reconnect and self.auto_reconnect) or e.errno not in DEVICE_GONE_ERRNOS:
                raise
        # The route is re-read after reconnecting: the headset may now be on USB-C or the dongle.
        # A packet split over several reports may have been half sent, so it is resent whole.
        self.reconnect()
        return self._write_reports(race_packet, is_dongle)

    def _write_reports(self, race_packet: bytes, is_dongle: Optional[bool]) -> List[bytes]:
        route_flag = self.is_dongle if is_dongle is None else is_dongle
        reports = frame_output_reports(race_packet, is_dongle=route_flag)
        for rep in reports:
            written = os.write(self.fd, rep)
            if written != len(rep):
                raise IOError(f"Incomplete HID write: {written}/{len(rep)} bytes")
        return reports

    @_locked
    def poll_input_report(self) -> Optional[bytes]:
        """Polls one input report via HIDIOCGINPUT or Interrupt IN."""
        if self.dry_run or self.fd is None:
            return None

        # 1. Primary: HIDIOCGINPUT (Control GET_REPORT)
        buf = bytearray(b"\x07" + b"\x00" * 61)
        try:
            fcntl.ioctl(self.fd, _HIDIOCGINPUT(REPORT_LENGTH), buf)
            if buf[1] > 0:
                return bytes(buf)
        except Exception:
            pass

        # 2. Secondary: Interrupt IN
        r, _, _ = select.select([self.fd], [], [], 0.005)
        if r:
            try:
                data = os.read(self.fd, 64)
                if data and len(data) >= 3 and data[0] == REPORT_ID_INPUT and data[1] > 0:
                    return data
            except Exception:
                pass

        return None

    @_locked
    def read_race_packets(self, timeout_sec: float = 1.0) -> List[bytes]:
        """
        Polls incoming HID reports and returns all reassembled complete RACE packets.
        """
        if self.dry_run:
            return []

        packets = []
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            report = self.poll_input_report()
            if report:
                new_packets = self.input_stream.feed(report)
                if new_packets:
                    packets.extend(new_packets)
                    break
            time.sleep(0.02)

        return packets

    @_locked
    def node_answers(self, probe_packet: bytes, timeout_sec: float = 1.5) -> bool:
        """
        Sends a read request to the opened node itself (route 0x00: the dongle, or the headset on
        USB-C) and reports whether anything at all comes back. Any RACE packet counts: a stuck
        dongle sends nothing, not even unrelated notifications.
        """
        if self.dry_run:
            return True
        self.drain()
        try:
            self.write_race_packet(probe_packet, is_dongle=False)
        except (OSError, RuntimeError):
            return False
        stream = InputStream()
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            report = self.poll_input_report()
            if not report:
                time.sleep(0.02)
                continue
            try:
                if stream.feed(report):
                    return True
            except ProtocolError:
                continue
        return False

    MAX_PENDING_NOTIFICATIONS = 64

    def _keep_notification(self, packet: bytes):
        # Bounded: a firmware update sends thousands of requests without a drain, and the
        # dongle keeps emitting status notifications the whole time.
        self.pending_notifications.append(packet)
        del self.pending_notifications[:-self.MAX_PENDING_NOTIFICATIONS]

    @_locked
    def exchange(self, race_packet: bytes, timeout_sec: float = 1.5, drain: bool = True,
                 poll_interval: float = 0.02, abort_cmds: Tuple[int, ...] = (),
                 is_dongle: Optional[bool] = None) -> Optional[bytes]:
        """
        Transmits a RACE packet and synchronously waits for its matching response.
        Any asynchronous notifications (0x5D) received along the way are cached in pending_notifications.
        drain=False and a short poll_interval suit long runs of back-to-back requests (firmware
        pages), where each reply is matched by command anyway and a full drain per request is slow.
        abort_cmds: commands the *device* may send instead of replying (e.g. FOTA stop 0x1C03, sent
        when it has ended an update session); receiving one raises DeviceAbortError straight away.
        """
        if drain:
            self.drain()
        self.write_race_packet(race_packet, is_dongle=is_dongle)
        if self.dry_run:
            return None

        expected_cmd = struct.unpack_from("<H", race_packet, 4)[0]
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            report = self.poll_input_report()
            if report:
                new_packets = self.input_stream.feed(report)
                for p in new_packets:
                    if len(p) >= 6:
                        direction = p[1]
                        cmd = struct.unpack_from("<H", p, 4)[0]
                        if direction == 0x5B and cmd == expected_cmd:
                            return p
                        elif direction == 0x5D:
                            self._keep_notification(p)
                        elif direction in (0x5A, 0x5C) and cmd in abort_cmds:
                            raise DeviceAbortError(
                                f"Device sent command 0x{cmd:04X} ({p[6:].hex(' ')}) instead of replying "
                                f"to 0x{expected_cmd:04X}"
                            )
                continue  # more of a multi-report reply may already be waiting
            time.sleep(poll_interval)

        raise TimeoutError(f"Timed out waiting for device response to command 0x{expected_cmd:04X} ({race_packet.hex(' ')})")

    @_locked
    def query_notification(self, query_packet: bytes, timeout_sec: float = 2.0,
                           is_dongle: Optional[bool] = None) -> Optional[bytes]:
        """
        Sends a query (e.g. Battery 0x0CD6, Firmware 0x1C07) and waits for its 0x5D notification.
        Returns None on timeout, or straight away if the device's ACK reports an error status
        (no notification follows one), instead of waiting out the whole timeout.
        is_dongle overrides the route for this query only (see write_race_packet).
        """
        # drain() also discards pending_notifications, which are all stale by now: an older
        # notification for this command would report an old value as the current one.
        self.drain()
        self.write_race_packet(query_packet, is_dongle=is_dongle)
        if self.dry_run:
            return None

        expected_cmd = struct.unpack_from("<H", query_packet, 4)[0]
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            report = self.poll_input_report()
            if report:
                new_packets = self.input_stream.feed(report)
                for p in new_packets:
                    if len(p) >= 6:
                        direction = p[1]
                        cmd = struct.unpack_from("<H", p, 4)[0]
                        if direction == 0x5D and cmd == expected_cmd:
                            return p
                        elif direction == 0x5B and cmd == expected_cmd and len(p) >= 7 and p[6] != 0:
                            return None
                        elif direction == 0x5D:
                            self._keep_notification(p)
            time.sleep(0.02)

        return None
