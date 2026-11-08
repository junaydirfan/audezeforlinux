"""
Tests for stuck-dongle recovery: telling a stuck dongle from a silent headset, resetting the dongle
over USB (once per stuck episode), the fast path for a headset that can't answer, the event log,
and how the dashboard, CLI and doctor report it.
"""

import errno
import os
import tempfile
import unittest
from unittest import mock

import maxwell_ctl
from maxwell2 import client as client_mod
from maxwell2 import events, system
from maxwell2.client import (
    LINK_DONGLE_SILENT,
    LINK_HEADSET_SILENT,
    LINK_OK,
    LINK_USB_SILENT,
    MaxwellClient,
)
from maxwell2.interactive import CursesTUI
from maxwell2.transport import (
    USBDEVFS_RESET,
    DeviceBusyError,
    DevicePermissionError,
    Transport,
    usb_device_node,
)


class FakeLinkTransport:
    """A dongle that can be stuck (answers nothing) and a headset that can be off."""

    dry_run = False
    is_dongle = True
    node_is_dongle = True
    product_id = 0x4B28

    def __init__(self, stuck=False, headset_on=True, reset_fixes=True, reset_error=None):
        self.stuck, self.headset_on = stuck, headset_on
        self.reset_fixes, self.reset_error = reset_fixes, reset_error
        self.resets = 0
        self.reads = 0

    @property
    def headset_answers(self):
        return not self.stuck and self.headset_on

    def node_answers(self, packet, timeout_sec=1.5):
        return not self.stuck

    def reset_usb_device(self):
        if self.reset_error:
            raise self.reset_error
        self.resets += 1
        if self.reset_fixes:
            self.stuck = False


class FakeClient(MaxwellClient):
    """Status reads that answer only while the fake headset can."""

    def _read(self, value):
        self.transport.reads += 1
        if not self.transport.headset_answers:
            raise TimeoutError("no reply")
        return value

    def get_preset(self):
        return self._read({"index": 1, "name": "Audeze", "is_custom": False, "slot": None})

    def get_battery(self, attempts=2, timeout_sec=2.0):
        self.transport.reads += 1
        return 80 if self.transport.headset_answers else None

    def is_usb_powered(self):
        return False

    def get_firmware(self):
        return self._read({"firmware": "v1.0", "dongle": "v1.0", "headset": "v1.0", "match": True})

    def get_mic_mute(self):
        return self._read(False)

    def get_sidetone(self):
        return self._read({"enabled": True, "level": 3, "max_level": 31})

    def get_chat_mix(self):
        return self._read({"raw": 10, "game_pct": 50, "chat_pct": 50})

    def get_noise_reduction(self):
        return self._read({"code": 0, "name": "Off"})


class StateDirTestCase(unittest.TestCase):
    """Keeps the event log in a temporary directory, never the real ~/.local/state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        relink = mock.patch.object(client_mod, "RELINK_INTERVAL_SEC", 0)
        relink.start()
        self.addCleanup(relink.stop)


class LinkCheckTests(StateDirTestCase):

    def test_tells_the_cases_apart(self):
        self.assertEqual(FakeClient(FakeLinkTransport()).check_link(), LINK_OK)
        self.assertEqual(FakeClient(FakeLinkTransport(headset_on=False)).check_link(), LINK_HEADSET_SILENT)
        self.assertEqual(FakeClient(FakeLinkTransport(stuck=True)).check_link(), LINK_DONGLE_SILENT)
        usb = FakeLinkTransport(stuck=True)
        usb.node_is_dongle = False
        self.assertEqual(FakeClient(usb).check_link(), LINK_USB_SILENT)

    def test_dry_run_is_ok(self):
        self.assertEqual(MaxwellClient(Transport(dry_run=True)).check_link(), LINK_OK)


class StatusRecoveryTests(StateDirTestCase):

    def test_stuck_dongle_is_reset_and_status_read(self):
        t = FakeLinkTransport(stuck=True)
        status = FakeClient(t).get_status()
        self.assertEqual(t.resets, 1)
        self.assertTrue(status["device_responding"])
        self.assertEqual(status["preset"]["name"], "Audeze")
        self.assertEqual(status["unreadable"], [])
        self.assertEqual(status["link"], LINK_OK)
        self.assertEqual(status["recovery"]["action"], "reset")
        self.assertTrue(status["recovery"]["recovered"])
        logged = events.recent_events()
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["event"], "dongle_silent")
        self.assertTrue(logged[0]["recovered"])

    def test_reset_that_does_not_help_is_not_repeated_until_the_dongle_worked_again(self):
        t = FakeLinkTransport(stuck=True, reset_fixes=False)
        c = FakeClient(t)
        first = c.get_status()
        self.assertFalse(first["device_responding"])
        self.assertEqual(first["link"], LINK_DONGLE_SILENT)
        self.assertEqual(first["recovery"]["action"], "reset")
        self.assertFalse(first["recovery"]["recovered"])
        self.assertEqual(set(first["unreadable"]), set(client_mod.STATUS_FIELDS))

        second = c.get_status()  # e.g. [r] in the dashboard: no second audio dropout
        self.assertEqual(second["recovery"]["action"], "already_tried")
        self.assertEqual(t.resets, 1)

        t.stuck = False  # replugged by hand
        self.assertTrue(c.get_status()["device_responding"])
        t.stuck = True   # stuck again days later: reset again
        c.get_status()
        self.assertEqual(t.resets, 2)

    def test_headset_off_is_not_a_reason_to_reset_and_skips_the_other_reads(self):
        t = FakeLinkTransport(headset_on=False)
        status = FakeClient(t).get_status()
        self.assertEqual(t.resets, 0)
        self.assertFalse(status["device_responding"])
        self.assertEqual(status["link"], LINK_HEADSET_SILENT)
        self.assertEqual(status["recovery"]["action"], "none")
        # The preset read and the link check's battery query; not one timeout per field
        self.assertEqual(t.reads, 2)
        self.assertIsNone(status["battery_percent"])
        self.assertIsNone(status["mic_muted"])

    def test_no_permission_is_reported(self):
        t = FakeLinkTransport(stuck=True, reset_error=DevicePermissionError("run setup-udev"))
        status = FakeClient(t).get_status()
        self.assertFalse(status["device_responding"])
        self.assertEqual(status["recovery"]["action"], "no_permission")
        self.assertIn("setup-udev", status["recovery"]["detail"])
        self.assertEqual(events.recent_events()[-1]["action"], "no_permission")

    def test_recover_false_only_checks(self):
        t = FakeLinkTransport(stuck=True)
        status = FakeClient(t).get_status(recover=False)
        self.assertEqual(t.resets, 0)
        self.assertEqual(status["link"], LINK_DONGLE_SILENT)
        self.assertIsNone(status["recovery"])

    def test_silent_status_placeholders_are_independent_copies(self):
        a = FakeClient(FakeLinkTransport(headset_on=False)).get_status()
        a["sidetone"]["level"] = 30
        b = FakeClient(FakeLinkTransport(headset_on=False)).get_status()
        self.assertEqual(b["sidetone"]["level"], 5)

    def test_forced_reset_also_covers_a_silent_headset(self):
        t = FakeLinkTransport(headset_on=False)
        res = FakeClient(t).recover_stuck_dongle(force=True)
        self.assertEqual(t.resets, 1)
        self.assertEqual(res["action"], "reset")
        self.assertEqual(res["link_after"], LINK_HEADSET_SILENT)
        self.assertFalse(res["recovered"])

    def test_forced_reset_skips_a_healthy_dongle(self):
        t = FakeLinkTransport()
        res = FakeClient(t).recover_stuck_dongle(force=True)
        self.assertEqual((res["action"], res["recovered"], t.resets), ("none", True, 0))


class TransportResetTests(unittest.TestCase):

    def test_usb_device_node_walks_up_to_the_usb_device(self):
        with tempfile.TemporaryDirectory() as root:
            usb_dev = os.path.join(root, "devices", "usb1", "1-12", "1-12.2", "1-12.2.1")
            hid = os.path.join(usb_dev, "1-12.2.1:1.5", "0003:3329:4B28.0014")
            os.makedirs(hid)
            with open(os.path.join(usb_dev, "busnum"), "w") as f:
                f.write("1\n")
            with open(os.path.join(usb_dev, "devnum"), "w") as f:
                f.write("21\n")
            cls = os.path.join(root, "class", "hidraw", "hidraw10")
            os.makedirs(cls)
            os.symlink(hid, os.path.join(cls, "device"))
            self.assertEqual(usb_device_node("/dev/hidraw10", sys_root=root), "/dev/bus/usb/001/021")
            self.assertIsNone(usb_device_node("/dev/hidraw99", sys_root=root))

    def make_transport(self):
        t = Transport(hidraw_path="/dev/hidraw10")
        t.fd = 5
        t.node_is_dongle = True
        return t

    def test_reset_issues_usbdevfs_reset_and_reopens(self):
        t = self.make_transport()
        dongle = [{"path": "/dev/hidraw10", "is_dongle": True}]
        with mock.patch("maxwell2.transport.usb_device_node", return_value="/dev/bus/usb/001/021"), \
                mock.patch("maxwell2.transport.os.open", return_value=42) as opened, \
                mock.patch("maxwell2.transport.os.close"), \
                mock.patch("maxwell2.transport.fcntl.ioctl") as ioctl, \
                mock.patch("maxwell2.transport.time.sleep"), \
                mock.patch("maxwell2.transport.list_maxwell_devices", return_value=dongle), \
                mock.patch.object(Transport, "reconnect") as reconnect:
            t.reset_usb_device()
        opened.assert_called_once_with("/dev/bus/usb/001/021", os.O_WRONLY)
        ioctl.assert_called_once_with(42, USBDEVFS_RESET, 0)
        reconnect.assert_called_once_with(prefer_dongle=True)

    def test_reset_that_re_enumerates_is_fine(self):
        t = self.make_transport()
        with mock.patch("maxwell2.transport.usb_device_node", return_value="/dev/bus/usb/001/021"), \
                mock.patch("maxwell2.transport.os.open", return_value=42), \
                mock.patch("maxwell2.transport.os.close"), \
                mock.patch("maxwell2.transport.fcntl.ioctl", side_effect=OSError(errno.ENODEV, "gone")), \
                mock.patch("maxwell2.transport.time.sleep"), \
                mock.patch("maxwell2.transport.list_maxwell_devices",
                           return_value=[{"path": "/dev/hidraw11", "is_dongle": True}]), \
                mock.patch.object(Transport, "reconnect") as reconnect:
            t.reset_usb_device()
        reconnect.assert_called_once()

    def test_reset_without_permission(self):
        t = self.make_transport()
        with mock.patch("maxwell2.transport.usb_device_node", return_value="/dev/bus/usb/001/021"), \
                mock.patch("maxwell2.transport.os.open", side_effect=PermissionError()):
            with self.assertRaises(DevicePermissionError) as ctx:
                t.reset_usb_device()
        self.assertIn("setup-udev", str(ctx.exception))

    def test_never_during_a_firmware_update(self):
        t = self.make_transport()
        t.exclusive = True
        with mock.patch("maxwell2.transport.os.open") as opened:
            with self.assertRaises(DeviceBusyError):
                t.reset_usb_device()
        opened.assert_not_called()


class NodeAnswersTests(unittest.TestCase):

    def make(self, reports):
        class Fake(Transport):
            def __init__(self):
                super().__init__(hidraw_path="/dev/hidraw10")
                self.fd = 99
                self.queue = []
                self.sent = []

            def drain(self, max_rounds=64):
                pass

            def _write_reports(self, race_packet, is_dongle):
                self.sent.append(is_dongle)
                self.queue.extend(reports)
                return []

            def poll_input_report(self):
                return self.queue.pop(0) if self.queue else None

        return Fake()

    def test_any_packet_counts_and_route_is_the_node_itself(self):
        packet = bytes.fromhex("05 5D 05 00 D6 0C 00 00 50")  # an unrelated battery notification
        report = bytes([0x07, len(packet), 0x00]) + packet + b"\x00" * (59 - len(packet))
        t = self.make([report])
        self.assertTrue(t.node_answers(b"\x05\x5a\x02\x00\x07\x1c", timeout_sec=0.2))
        self.assertEqual(t.sent, [False])

    def test_silence(self):
        self.assertFalse(self.make([]).node_answers(b"\x05\x5a\x02\x00\x07\x1c", timeout_sec=0.05))


class EventLogTests(StateDirTestCase):

    def test_record_and_read_back(self):
        events.record_event("dongle_silent", action="reset", recovered=True)
        events.record_event("headset_silent", action="none")
        got = events.recent_events()
        self.assertEqual([e["event"] for e in got], ["dongle_silent", "headset_silent"])
        self.assertTrue(events.log_path().startswith(self.tmp.name))

    def test_log_is_trimmed(self):
        with mock.patch.object(events, "MAX_LOG_BYTES", 500):
            for i in range(100):
                events.record_event("dongle_silent", n=i)
        self.assertLess(os.path.getsize(events.log_path()), 1000)
        self.assertEqual(events.recent_events(1)[0]["n"], 99)

    def test_unwritable_log_never_raises(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/proc/no-such-dir"}):
            events.record_event("dongle_silent")
            self.assertEqual(events.recent_events(), [])


class DashboardRecoveryTests(unittest.TestCase):

    def test_poll_resets_a_dongle_that_got_stuck_while_open(self):
        class StuckClient:
            def get_power(self, attempts, timeout_sec):
                return {"battery_percent": None, "usb_powered": False, "battery_state": None}

            def recover_stuck_dongle(self):
                return {"link": LINK_DONGLE_SILENT, "action": "reset", "link_after": LINK_OK,
                        "recovered": True, "detail": ""}

            def get_status(self):
                return {"device_responding": True, "battery_percent": 64, "link": LINK_OK}

        tui = CursesTUI(None, StuckClient())
        tui.status["device_responding"] = False
        tui.poll_power()
        self.assertTrue(tui.status["device_responding"])
        self.assertEqual(tui.status["battery_percent"], 64)
        self.assertIn("reset", tui.message)

    def test_poll_with_headset_off_changes_nothing(self):
        class OffClient:
            def get_power(self, attempts, timeout_sec):
                return {"battery_percent": None, "usb_powered": False, "battery_state": None}

            def recover_stuck_dongle(self):
                return {"link": LINK_HEADSET_SILENT, "action": "none", "recovered": False, "detail": ""}

            def get_status(self):
                raise AssertionError("no reload while the headset is off")

        tui = CursesTUI(None, OffClient())
        tui.status["device_responding"] = False
        tui.message = "before"
        tui.poll_power()
        self.assertEqual(tui.message, "before")
        self.assertEqual(tui.status["link"], LINK_HEADSET_SILENT)

    def test_messages_name_the_silent_part(self):
        tui = CursesTUI(None, None)
        tui.status.update(device_responding=False, link=LINK_DONGLE_SILENT,
                          recovery={"action": "no_permission", "recovered": False})
        self.assertIn("setup-udev", tui.no_response_message())
        tui.status["recovery"] = {"action": "reset", "recovered": False}
        self.assertIn("didn't help", tui.no_response_message())
        tui.status.update(link=LINK_HEADSET_SILENT, recovery={"action": "none", "recovered": False})
        self.assertIn("reset-dongle", tui.no_response_message())
        tui.status.update(device_responding=True, link=LINK_OK, recovery={"action": "reset", "recovered": True})
        self.assertIn("was reset", tui.loaded_message())
        tui.status["recovery"] = None
        self.assertIsNone(tui.loaded_message())


class CliAndDoctorTests(unittest.TestCase):

    def test_silent_advice(self):
        self.assertIn("setup-udev", maxwell_ctl.silent_advice(
            {"link": LINK_DONGLE_SILENT, "recovery": {"action": "no_permission"}}))
        self.assertIn("didn't help", maxwell_ctl.silent_advice(
            {"link": LINK_DONGLE_SILENT, "recovery": {"action": "reset"}}))
        self.assertIn("switched on", maxwell_ctl.silent_advice({"link": LINK_HEADSET_SILENT, "recovery": None}))
        self.assertIn("USB-C", maxwell_ctl.silent_advice({"link": LINK_USB_SILENT}))

    def test_reset_result_text(self):
        self.assertIn("no reset needed", maxwell_ctl.format_reset_result(
            {"link": LINK_OK, "action": "none", "recovered": True}))
        self.assertIn("answers again", maxwell_ctl.format_reset_result(
            {"link": LINK_DONGLE_SILENT, "action": "reset", "link_after": LINK_OK, "recovered": True}))
        self.assertIn("still doesn't answer", maxwell_ctl.format_reset_result(
            {"link": LINK_DONGLE_SILENT, "action": "reset", "link_after": LINK_DONGLE_SILENT, "recovered": False}))

    def test_rule_content_lets_the_session_user_reset_dongles_only(self):
        reset_lines = [l for l in system.UDEV_RULE_CONTENT.splitlines() if 'DEVTYPE}=="usb_device"' in l]
        self.assertEqual(len(reset_lines), 6)
        self.assertTrue(all('TAG+="uaccess"' in l and "MODE" not in l for l in reset_lines))
        repo_rule = os.path.join(os.path.dirname(__file__), "..", "udev", "70-audeze-maxwell.rules")
        self.assertTrue(system.udev_rule_allows_reset(repo_rule))
        with tempfile.NamedTemporaryFile("w", suffix=".rules") as old:
            old.write('KERNEL=="hidraw*", ATTRS{idVendor}=="3329", TAG+="uaccess"\n')
            old.flush()
            self.assertFalse(system.udev_rule_allows_reset(old.name))

    def test_describe_event(self):
        self.assertIn("reset, fixed", system.describe_event(
            {"time": "2026-11-08T08:30:00-0500", "event": "dongle_silent", "action": "reset", "recovered": True}))
        self.assertIn("udev rule out of date", system.describe_event(
            {"time": "2026-11-08T08:30:00-0500", "event": "dongle_silent", "action": "no_permission"}))


if __name__ == "__main__":
    unittest.main()
