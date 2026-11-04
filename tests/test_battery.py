import errno
import time
import unittest
from unittest import mock

from maxwell2.client import MaxwellClient, battery_state, describe_battery
from maxwell2.interactive import CursesTUI
from maxwell2.protocol import (
    ProtocolError,
    build_battery_command,
    parse_battery_notification,
)
from maxwell2.transport import Transport

DONGLE = {"path": "/dev/hidraw1", "pid": 0x4B28, "is_dongle": True, "model": "dongle"}
HEADSET = {"path": "/dev/hidraw2", "pid": 0x4B2E, "is_dongle": False, "model": "headset"}


def input_report(packet: bytes) -> bytes:
    """Wraps a RACE packet in one 62-byte input report, as the device sends it."""
    report = bytes([0x07, len(packet), 0x00]) + packet
    return report + b"\x00" * (62 - len(report))


class FakeHidTransport(Transport):
    """A Transport whose device is a queue of input reports, filled when a packet is written."""

    def __init__(self, replies=None, stale=()):
        super().__init__(hidraw_path="/dev/hidraw2")
        self.fd = 99
        self.replies = list(replies or [])
        self.queue = [input_report(p) for p in stale]
        self.sent = []

    def _write_reports(self, race_packet, is_dongle):
        self.sent.append(race_packet)
        if self.replies:
            self.queue.extend(input_report(p) for p in self.replies.pop(0))
        return []

    def poll_input_report(self):
        return self.queue.pop(0) if self.queue else None

    def drain(self, max_rounds=64):
        self.input_stream.clear()
        self.pending_notifications.clear()
        while self.queue and max_rounds:
            self.queue.pop(0)
            max_rounds -= 1


BATTERY_ACK = bytes.fromhex("05 5B 03 00 D6 0C 00")
BATTERY_ERROR_ACK = bytes.fromhex("05 5B 03 00 D6 0C 02")


def battery_notification(level: int, status: int = 0) -> bytes:
    return bytes.fromhex("05 5D 05 00 D6 0C") + bytes([status, 0x00, level])


class BatteryParsingTests(unittest.TestCase):
    def test_level_is_the_third_payload_byte(self):
        self.assertEqual(parse_battery_notification(battery_notification(70)), 70)

    def test_error_status_and_impossible_levels_are_refused(self):
        with self.assertRaises(ProtocolError):
            parse_battery_notification(battery_notification(70, status=2))
        with self.assertRaises(ProtocolError):
            parse_battery_notification(battery_notification(0xFF))


class BatteryQueryTests(unittest.TestCase):
    def test_reads_level_after_ack(self):
        transport = FakeHidTransport(replies=[[BATTERY_ACK, battery_notification(71)]])
        self.assertEqual(MaxwellClient(transport).get_battery(), 71)

    def test_stale_notification_left_in_the_queue_is_not_reported(self):
        # Seen on the dongle: replies to earlier requests, including an old battery level, queued up
        stale = [battery_notification(11)] * 10
        transport = FakeHidTransport(replies=[[BATTERY_ACK, battery_notification(70)]], stale=stale)
        self.assertEqual(MaxwellClient(transport).get_battery(), 70)

    def test_error_ack_returns_without_waiting_for_the_timeout(self):
        transport = FakeHidTransport(replies=[[BATTERY_ERROR_ACK]])
        start = time.time()
        self.assertIsNone(transport.query_notification(build_battery_command(), timeout_sec=5.0))
        self.assertLess(time.time() - start, 1.0)

    def test_retries_once_when_the_first_query_gets_no_answer(self):
        transport = FakeHidTransport(replies=[[BATTERY_ERROR_ACK], [BATTERY_ACK, battery_notification(55)]])
        self.assertEqual(MaxwellClient(transport).get_battery(), 55)
        self.assertEqual(len(transport.sent), 2)


class ChargingStateTests(unittest.TestCase):
    def test_state_follows_usb_power_and_level(self):
        self.assertEqual(battery_state(70, usb_powered=True), "charging")
        self.assertEqual(battery_state(100, usb_powered=True), "charged")
        self.assertEqual(battery_state(None, usb_powered=True), "charging")
        self.assertIsNone(battery_state(70, usb_powered=False))

    def test_usb_c_headset_node_means_it_is_charging(self):
        transport = FakeHidTransport(replies=[[BATTERY_ACK, battery_notification(70)]])
        with mock.patch("maxwell2.client.list_maxwell_devices", return_value=[DONGLE, HEADSET]):
            power = MaxwellClient(transport).get_power()
        self.assertEqual(power, {"battery_percent": 70, "usb_powered": True, "battery_state": "charging"})

    def test_dongle_only_does_not_claim_a_charging_state(self):
        transport = FakeHidTransport(replies=[[BATTERY_ACK, battery_notification(70)]])
        with mock.patch("maxwell2.client.list_maxwell_devices", return_value=[DONGLE]):
            power = MaxwellClient(transport).get_power()
        self.assertFalse(power["usb_powered"])
        self.assertIsNone(power["battery_state"])

    def test_description(self):
        self.assertEqual(describe_battery({"battery_percent": 70, "battery_state": "charging"}),
                         "70% (charging over USB-C)")
        self.assertEqual(describe_battery({"battery_percent": 100, "battery_state": "charged"}),
                         "100% (fully charged, on USB-C)")
        self.assertEqual(describe_battery({"battery_percent": 40, "battery_state": None}), "40%")
        self.assertIn("Unknown", describe_battery({"battery_percent": None}))

    def test_failed_battery_read_is_unknown_not_a_placeholder(self):
        class BrokenBatteryClient(MaxwellClient):
            def get_battery(self, **kwargs):
                raise OSError("device gone")

        status = BrokenBatteryClient(Transport(dry_run=True)).get_status()
        self.assertIsNone(status["battery_percent"])
        self.assertIsNone(status["battery_state"])


class ReconnectTests(unittest.TestCase):
    def _transport(self, fail_errno):
        transport = Transport(hidraw_path="/dev/hidraw10")
        transport.explicit_path = False
        transport.fd = 5
        writes = []

        def fake_write(fd, data):
            if fd == 5:
                raise OSError(fail_errno, "gone")
            writes.append((fd, data))
            return len(data)

        def fake_open():
            transport.hidraw_path, transport.fd, transport.is_dongle = "/dev/hidraw1", 7, False

        return transport, writes, fake_write, fake_open

    def test_vanished_node_is_reopened_and_the_packet_resent(self):
        transport, writes, fake_write, fake_open = self._transport(errno.ENODEV)
        with mock.patch("os.write", side_effect=fake_write), mock.patch("os.close"), \
             mock.patch("maxwell2.transport.list_maxwell_devices", return_value=[HEADSET]), \
             mock.patch.object(Transport, "open", side_effect=fake_open):
            transport.write_race_packet(build_battery_command())
        self.assertEqual(transport.hidraw_path, "/dev/hidraw1")
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], 7)
        self.assertEqual(writes[0][1][2], 0x00)  # route byte re-read after reconnecting: now USB-C

    def test_no_reconnect_when_disabled_or_for_other_errors(self):
        for fail_errno, reconnect in ((errno.ENODEV, False), (errno.EINVAL, True)):
            transport, writes, fake_write, fake_open = self._transport(fail_errno)
            with mock.patch("os.write", side_effect=fake_write), \
                 mock.patch.object(Transport, "open", side_effect=AssertionError("reopened")):
                with self.assertRaises(OSError):
                    transport.write_race_packet(build_battery_command(), reconnect=reconnect)
            self.assertEqual(writes, [])

    def test_explicit_path_is_dropped_once_another_device_holds_it(self):
        transport = Transport(hidraw_path="/dev/hidraw10")
        opened = []
        with mock.patch("maxwell2.transport.list_maxwell_devices", return_value=[DONGLE]), \
             mock.patch.object(Transport, "open", side_effect=lambda: opened.append(transport.hidraw_path)):
            transport.reconnect()
        self.assertEqual(opened, [None])  # auto-detect, rather than open whatever is now hidraw10


class DrainTests(unittest.TestCase):
    def test_drains_a_long_backlog(self):
        transport = Transport(hidraw_path="/dev/hidraw2")
        transport.fd = 5
        backlog = [b"\x07\x05"] * 20

        def fake_ioctl(fd, request, buf):
            if backlog:
                backlog.pop()
                buf[1] = 5

        with mock.patch("fcntl.ioctl", side_effect=fake_ioctl), \
             mock.patch("select.select", return_value=([], [], [])), mock.patch("time.sleep"):
            transport.drain()
        self.assertEqual(backlog, [])


class DashboardPowerPollTests(unittest.TestCase):
    def test_poll_updates_battery_and_charging(self):
        class PowerClient:
            def get_power(self, attempts, timeout_sec):
                return {"battery_percent": 72, "usb_powered": True, "battery_state": "charging"}

        tui = CursesTUI(None, PowerClient())
        tui.status["device_responding"] = True
        tui.poll_power()
        self.assertEqual(tui.status["battery_percent"], 72)
        self.assertEqual(tui.status["battery_state"], "charging")

    def test_headset_coming_back_reloads_full_status(self):
        class ReturningClient:
            def get_power(self, attempts, timeout_sec):
                return {"battery_percent": 72, "usb_powered": False, "battery_state": None}

            def get_status(self):
                return {"device_responding": True, "mic_muted": True, "battery_percent": 72}

        tui = CursesTUI(None, ReturningClient())
        tui.status["device_responding"] = False
        tui.poll_power()
        self.assertTrue(tui.status["device_responding"])
        self.assertTrue(tui.status["mic_muted"])
        self.assertFalse(tui.loading)

    def test_poll_is_rate_limited_and_skipped_while_loading(self):
        class CountingClient:
            calls = 0

            def get_power(self, attempts, timeout_sec):
                CountingClient.calls += 1
                return {"battery_percent": 50, "usb_powered": False, "battery_state": None}

        tui = CursesTUI(None, CountingClient())
        tui.maybe_poll_power()  # interval not yet elapsed
        tui.last_power_poll = 0
        tui.loading = True
        tui.maybe_poll_power()  # status load owns the screen
        tui.loading = False
        tui.maybe_poll_power()
        tui.power_poll_thread.join(timeout=2)
        self.assertEqual(CountingClient.calls, 1)


if __name__ == "__main__":
    unittest.main()
