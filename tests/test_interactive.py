import unittest
import threading
import curses
from unittest import mock

from maxwell2.client import MaxwellClient
from maxwell2.interactive import CursesTUI
from maxwell2.transport import DONGLE_PIDS, KNOWN_PIDS, Transport


class FakeScreen:
    def __init__(self, events, keys=None, before_input=None):
        self.events = events
        self.keys = iter(keys or [ord("q")])
        self.before_input = before_input

    def erase(self):
        self.events.append("erase")

    def refresh(self):
        self.events.append("refresh")

    def getch(self):
        if self.before_input:
            self.before_input.wait(timeout=2)
        return next(self.keys)


class InteractiveStartupTests(unittest.TestCase):
    def test_dashboard_navigates_and_quits_while_hardware_state_loads(self):
        events = []
        load_started = threading.Event()
        finish_load = threading.Event()
        screen = FakeScreen(events, [curses.KEY_DOWN, ord("q")], load_started)
        tui = CursesTUI(screen, client=None)
        tui.init_curses_terminal = lambda: events.append("init")

        def render():
            events.append(("render", tui.message))

        def load_state():
            load_started.set()
            finish_load.wait(timeout=2)

        tui.render = render
        tui.load_initial_hardware_state = load_state

        try:
            tui.run()
            self.assertTrue(load_started.is_set())
            self.assertFalse(tui.running)
            self.assertEqual(tui.selected_row, 1)
            self.assertLess(events.index(("render", "Reading headset status...")), events.index("refresh"))
        finally:
            finish_load.set()

    def test_dashboard_handles_keyboard_interrupt_during_input(self):
        class InterruptingScreen(FakeScreen):
            def getch(self):
                raise KeyboardInterrupt

        tui = CursesTUI(InterruptingScreen([]), client=None)
        tui.init_curses_terminal = lambda: None
        tui.render = lambda: None
        tui.load_initial_hardware_state = lambda: None

        tui.run()

        self.assertFalse(tui.running)


class HardwareStateTests(unittest.TestCase):
    def test_hardware_actions_wait_for_background_load(self):
        class NoCallsClient:
            def __getattr__(self, name):
                raise AssertionError(f"transport used during load: {name}")

        tui = CursesTUI(None, NoCallsClient())
        tui.loading = True

        tui.activate_preset(3)
        tui.toggle_sidetone()
        tui.refresh_telemetry()

        self.assertIn("Still reading", tui.message)

    def test_silent_headset_is_reported_and_preset_cursor_follows_click(self):
        class SilentClient:
            def set_preset(self, target):
                raise TimeoutError("no reply")

        tui = CursesTUI(None, SilentClient())

        tui.activate_preset(3)

        self.assertEqual(tui.selected_row, 2)
        self.assertIn("No response", tui.message)
        self.assertFalse(tui.has_live_status())

    def test_unacknowledged_writes_are_not_reported_as_done(self):
        class FastClient:
            def set_sidetone_fast(self, enabled=None, level=None):
                pass

        tui = CursesTUI(None, FastClient())
        tui.status["device_responding"] = False

        tui.toggle_sidetone()

        self.assertIn("unconfirmed", tui.message)


class SilentDeviceStatusTests(unittest.TestCase):
    def test_get_status_flags_defaults_when_device_is_silent(self):
        class SilentTransport:
            dry_run = False
            is_dongle = True

            def exchange(self, packet, timeout_sec=1.5):
                raise TimeoutError("no reply")

            def query_notification(self, packet, timeout_sec=2.0):
                return None

        status = MaxwellClient(SilentTransport()).get_status()

        self.assertFalse(status["device_responding"])

    def test_get_status_is_live_in_dry_run(self):
        self.assertTrue(MaxwellClient(Transport(dry_run=True)).get_status()["device_responding"])


class DeviceSelectionTests(unittest.TestCase):
    DONGLE = {"path": "/dev/hidraw1", "pid": 0x4B28, "is_dongle": True, "model": "dongle"}
    HEADSET = {"path": "/dev/hidraw2", "pid": 0x4B2E, "is_dongle": False, "model": "headset"}

    def test_xbox_usb_headset_is_a_known_direct_device(self):
        self.assertIn(0x4B2E, KNOWN_PIDS)
        self.assertNotIn(0x4B2E, DONGLE_PIDS)

    def test_direct_usb_headset_is_preferred_over_dongle(self):
        with mock.patch("maxwell2.transport.list_maxwell_devices", return_value=[self.DONGLE, self.HEADSET]):
            self.assertEqual(Transport.find_device(), ("/dev/hidraw2", 0x4B2E, False))

    def test_explicit_device_path_uses_that_devices_route(self):
        transport = Transport(hidraw_path="/dev/hidraw2")
        with mock.patch("maxwell2.transport.list_maxwell_devices", return_value=[self.DONGLE, self.HEADSET]), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.open", return_value=99), \
             mock.patch.object(Transport, "drain"):
            transport.open()
        self.assertFalse(transport.is_dongle)


if __name__ == "__main__":
    unittest.main()