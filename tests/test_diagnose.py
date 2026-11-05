"""
Tests for device selection (control collection, node order) and the live connection probe used by
`doctor`, which tells a stuck dongle, a silent headset, permission problems and busy devices apart.
"""

import os
import struct
import tempfile
import unittest
from unittest import mock

from maxwell2 import diagnose
from maxwell2.transport import (
    CONTROL_USAGE_PAGE,
    DeviceBusyError,
    DevicePermissionError,
    descriptor_usage_pages,
    list_maxwell_devices,
)

# The start of a real Maxwell 2 descriptor: Consumer page, then the vendor control collection
MAXWELL_DESCRIPTOR = bytes.fromhex("05 0c 09 01 a1 01 85 0c c0 06 13 ff 09 01 a1 01 85 06 c0")
CONSUMER_ONLY = bytes.fromhex("05 0c 09 01 a1 01 85 01 c0")


class TestDescriptorParsing(unittest.TestCase):

    def test_usage_pages(self):
        self.assertEqual(descriptor_usage_pages(MAXWELL_DESCRIPTOR), [0x0C, CONTROL_USAGE_PAGE])
        self.assertEqual(descriptor_usage_pages(CONSUMER_ONLY), [0x0C])
        # 4-byte usage page item, and a long item that must be skipped whole
        self.assertEqual(descriptor_usage_pages(bytes.fromhex("07 13 ff 00 00 fe 02 10 aa bb 05 01")), [0xFF13, 0x01])
        self.assertEqual(descriptor_usage_pages(b""), [])
        self.assertEqual(descriptor_usage_pages(b"\x06\x13"), [0x13])  # truncated: no crash

    def test_node_selection(self):
        with tempfile.TemporaryDirectory() as root:
            def node(name, pid, descriptor, interface):
                usb_intf = os.path.join(root, "usb", interface)
                hid = os.path.join(usb_intf, "0003:3329:%04X.0001" % pid)
                os.makedirs(hid)
                with open(os.path.join(hid, "uevent"), "w") as f:
                    f.write(f"HID_ID=0003:00003329:0000{pid:04X}\n")
                if descriptor is not None:
                    with open(os.path.join(hid, "report_descriptor"), "wb") as f:
                        f.write(descriptor)
                cls = os.path.join(root, "class", name)
                os.makedirs(cls)
                os.symlink(hid, os.path.join(cls, "device"))

            node("hidraw10", 0x4B19, MAXWELL_DESCRIPTOR, "1-2:1.0")   # Maxwell 1 PS dongle control interface
            node("hidraw9", 0x4B19, CONSUMER_ONLY, "1-2:1.3")         # a second interface without it
            node("hidraw2", 0x4B28, None, "1-3:1.5")                  # descriptor unreadable: kept
            node("hidraw3", 0x1234, MAXWELL_DESCRIPTOR, "1-4:1.0")    # not an Audeze product id
            real_glob = __import__("glob").glob
            with mock.patch("maxwell2.transport.glob.glob",
                            lambda pattern: real_glob(os.path.join(root, "class", "hidraw*"))):
                devices = list_maxwell_devices()
                everything = list_maxwell_devices(include_all=True)

        # Numeric order (hidraw2 before hidraw10), the consumer-only interface left out
        self.assertEqual([d["path"] for d in devices], ["/dev/hidraw2", "/dev/hidraw10"])
        self.assertEqual([d["has_control"] for d in devices], [None, True])
        self.assertEqual(devices[1]["interface"], "1-2:1.0")
        self.assertEqual(devices[1]["model"], "Maxwell 1 (PlayStation Dongle)")
        self.assertEqual([d["path"] for d in everything], ["/dev/hidraw2", "/dev/hidraw9", "/dev/hidraw10"])


def race(head_type, cmd, payload):
    return struct.pack("<BBHH", 0x05, head_type, len(payload) + 2, cmd) + payload


def as_reports(packet):
    out = []
    for i in range(0, len(packet), 59):
        chunk = packet[i:i + 59]
        out.append(bytes([0x07, len(chunk), 0x00]) + chunk + b"\x00" * (59 - len(chunk)))
    return out


FIRMWARE_REPLY = [race(0x5B, 0x1C07, b"\x00"), race(0x5D, 0x1C07, b"\x00\x00\x0av1.0.1.74")]
PRESET_REPLY = [race(0x5B, 0x0901, b"\x00\x00\x00\x04")]
BATTERY_REPLY = [race(0x5B, 0x0CD6, b"\x00"), race(0x5D, 0x0CD6, b"\x00\x00\x3c")]


class ScriptedTransport:
    """Answers each (command, route) with the packets in `script`; nothing for anything else."""

    def __init__(self, script, open_error=None):
        self.script, self.open_error = script, open_error
        self.inbox = []
        self.sent = []

    def __call__(self, hidraw_path=None, dry_run=False):
        return self

    def open(self):
        if self.open_error:
            raise self.open_error

    def close(self):
        pass

    def drain(self):
        self.inbox = []

    def write_race_packet(self, packet, is_dongle=None, reconnect=True):
        cmd = struct.unpack_from("<H", packet, 4)[0]
        self.sent.append((cmd, is_dongle, reconnect))
        for p in self.script.get((cmd, is_dongle), []):
            self.inbox.extend(as_reports(p))

    def poll_input_report(self):
        return self.inbox.pop(0) if self.inbox else None


DONGLE = {"path": "/dev/hidraw7", "pid": 0x4B19, "is_dongle": True, "model": "Maxwell 1 (PlayStation Dongle)",
          "interface": "1-2:1.0", "has_control": True}
USB = {"path": "/dev/hidraw8", "pid": 0x4B1A, "is_dongle": False, "model": "Maxwell 1 (PlayStation USB)",
       "interface": "1-3:1.0", "has_control": True}


class TestProbe(unittest.TestCase):

    def probe(self, dev, script, open_error=None):
        t = ScriptedTransport(script, open_error)
        with mock.patch.object(diagnose, "Transport", t), mock.patch.object(diagnose, "QUERY_TIMEOUT_SEC", 0.05), \
                mock.patch.object(diagnose, "node_holders", return_value=[]):
            return diagnose.probe_node(dev), t

    def test_healthy_dongle(self):
        script = {(0x1C07, False): FIRMWARE_REPLY, (0x0901, True): PRESET_REPLY, (0x0CD6, True): BATTERY_REPLY}
        r, t = self.probe(DONGLE, script)
        self.assertEqual(r["verdict"], diagnose.VERDICT_OK)
        # Dongle itself on route 0x00, headset relayed on 0x80, and never a reconnect
        self.assertEqual(t.sent, [(0x1C07, False, False), (0x0901, True, False), (0x0CD6, True, False)])

    def test_headset_silent(self):
        r, _ = self.probe(DONGLE, {(0x1C07, False): FIRMWARE_REPLY})
        self.assertEqual(r["verdict"], diagnose.VERDICT_HEADSET_SILENT)
        self.assertIn("Switch the headset on", " ".join(diagnose.advice_for(r, None)))

    def test_dongle_rejecting_the_firmware_query_still_counts_as_alive(self):
        # An error ACK proves the dongle answers, even if it doesn't support the query
        r, _ = self.probe(DONGLE, {(0x1C07, False): [race(0x5B, 0x1C07, b"\x01")]})
        self.assertEqual(r["verdict"], diagnose.VERDICT_HEADSET_SILENT)

    def test_stuck_dongle(self):
        r, _ = self.probe(DONGLE, {})
        self.assertEqual(r["verdict"], diagnose.VERDICT_NO_REPLIES)
        advice = " ".join(diagnose.advice_for(r, None))
        self.assertIn("Unplug the dongle", advice)
        self.assertIn("plug it", advice)

    def test_usb_node_silent(self):
        r, _ = self.probe(USB, {})
        self.assertEqual(r["verdict"], diagnose.VERDICT_NO_REPLIES)
        self.assertIn("USB-C cable", " ".join(diagnose.advice_for(r, None)))
        r, t = self.probe(USB, {(0x1C07, False): FIRMWARE_REPLY, (0x0901, False): PRESET_REPLY})
        self.assertEqual(r["verdict"], diagnose.VERDICT_OK)
        self.assertTrue(all(route is False for _, route, _ in t.sent))  # the headset itself, route 0x00

    def test_rejected_preset_read_is_not_an_answer(self):
        rejected = [race(0x5B, 0x0901, b"\x00\x00\x01")]
        r, _ = self.probe(DONGLE, {(0x1C07, False): FIRMWARE_REPLY, (0x0901, True): rejected})
        self.assertEqual(r["verdict"], diagnose.VERDICT_HEADSET_SILENT)

    def test_unrelated_traffic_only(self):
        noise = [race(0x5D, 0x2CB1, b"\x00")]
        r, _ = self.probe(USB, {(0x1C07, False): noise})
        self.assertEqual(r["verdict"], diagnose.VERDICT_UNEXPECTED)

    def test_open_failures(self):
        r, _ = self.probe(DONGLE, {}, open_error=DevicePermissionError("denied"))
        self.assertEqual(r["verdict"], diagnose.VERDICT_PERMISSION)
        self.assertIn("setup-udev", " ".join(diagnose.advice_for(r, None)))
        in_box = " ".join(diagnose.advice_for(r, "distrobox"))
        self.assertIn("on the host", in_box)
        self.assertIn("distrobox", in_box)
        r, _ = self.probe(DONGLE, {}, open_error=DeviceBusyError("in use by another audezeforlinux process"))
        self.assertEqual(r["verdict"], diagnose.VERDICT_BUSY)

    def test_other_programs_holding_the_node_are_named(self):
        r = {"verdict": diagnose.VERDICT_NO_REPLIES, "model": "Maxwell 1 (Xbox Dongle)", "holders": ["headsetcontrol (4242)"]}
        self.assertIn("headsetcontrol (4242)", " ".join(diagnose.advice_for(r, None)))

    def test_diagnose_needs_every_node_healthy(self):
        results = {"/dev/hidraw7": diagnose.VERDICT_OK, "/dev/hidraw8": diagnose.VERDICT_HEADSET_SILENT}

        def fake_probe(dev):
            return {"path": dev["path"], "model": dev["model"], "verdict": results[dev["path"]], "holders": []}
        nodes = [dict(DONGLE), dict(USB, path="/dev/hidraw8")]
        with mock.patch.object(diagnose, "list_maxwell_devices", return_value=nodes), \
                mock.patch.object(diagnose, "probe_node", fake_probe):
            self.assertFalse(diagnose.diagnose()["ok"])
            results["/dev/hidraw8"] = diagnose.VERDICT_OK
            self.assertTrue(diagnose.diagnose()["ok"])

    def test_container_detection(self):
        def env(files=(), **environ):
            exists = lambda p: p in files
            return mock.patch.multiple(os.path, exists=exists), mock.patch.dict(os.environ, environ, clear=True)
        cases = [
            (("/run/.containerenv",), {"DISTROBOX_ENTER_PATH": "/usr/bin/distrobox-enter"}, "distrobox"),
            (("/run/.toolboxenv", "/run/.containerenv"), {}, "toolbox"),
            (("/.flatpak-info",), {}, "flatpak"),
            ((), {}, None),
        ]
        for files, environ, expected in cases:
            with self.subTest(expected=expected):
                a, b = env(files, **environ)
                with a, b:
                    self.assertEqual(diagnose.container_environment(), expected)


class TestHonestStatus(unittest.TestCase):

    def test_unreadable_fields_are_reported_and_shown_unknown(self):
        from maxwell2.client import MaxwellClient
        import maxwell_ctl

        class Silent:
            dry_run = False
            product_id = 0x4B19
            is_dongle = True

        client = MaxwellClient(Silent())
        fail = mock.Mock(side_effect=TimeoutError("no reply"))
        with mock.patch.multiple(MaxwellClient, get_preset=fail, get_power=fail, get_mic_mute=fail,
                                 get_sidetone=fail, get_chat_mix=fail, get_noise_reduction=fail,
                                 get_firmware=mock.Mock(return_value={"firmware": "Unknown", "dongle": "Unknown",
                                                                      "headset": "Unknown", "match": True})):
            status = client.get_status()
        self.assertFalse(status["device_responding"])
        self.assertEqual(set(status["unreadable"]), {"preset", "battery", "firmware", "mic_muted", "sidetone",
                                                     "chat_mix", "noise_reduction"})
        self.assertEqual(status["model"], "Maxwell 1 (PlayStation Dongle)")
        self.assertIsNone(status["mic_muted"])
        text = maxwell_ctl.format_plain_status(status)
        self.assertIn("MAXWELL 1 (PLAYSTATION DONGLE)", text)
        self.assertNotIn("Immersive", text)        # the placeholder preset is never shown as a reading
        self.assertNotIn("v1.0.5.7.4", text)       # nor a made-up firmware version
        self.assertNotIn("Game 50%", text)
        self.assertGreaterEqual(text.count("Unknown (no reply)"), 6)


if __name__ == "__main__":
    unittest.main()
