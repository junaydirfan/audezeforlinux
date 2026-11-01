import unittest
import threading

from maxwell2.interactive import CursesTUI


class FakeScreen:
    def __init__(self, events):
        self.events = events

    def erase(self):
        self.events.append("erase")

    def refresh(self):
        self.events.append("refresh")

    def getch(self):
        return ord("q")


class InteractiveStartupTests(unittest.TestCase):
    def test_dashboard_can_quit_while_hardware_state_loads(self):
        events = []
        load_started = threading.Event()
        finish_load = threading.Event()
        tui = CursesTUI(FakeScreen(events), client=None)
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


if __name__ == "__main__":
    unittest.main()