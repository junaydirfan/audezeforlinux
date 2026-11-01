import unittest

from maxwell2.interactive import CursesTUI


class FakeScreen:
    def __init__(self, events):
        self.events = events

    def erase(self):
        self.events.append("erase")

    def refresh(self):
        self.events.append("refresh")


class InteractiveStartupTests(unittest.TestCase):
    def test_dashboard_renders_loading_state_before_hardware_reads(self):
        events = []
        tui = CursesTUI(FakeScreen(events), client=None)
        tui.init_curses_terminal = lambda: events.append("init")

        def render():
            events.append(("render", tui.message))

        def load_state():
            events.append("load")
            tui.running = False

        tui.render = render
        tui.load_initial_hardware_state = load_state

        tui.run()

        self.assertLess(events.index("refresh"), events.index("load"))
        self.assertIn(("render", "Reading headset status..."), events)


if __name__ == "__main__":
    unittest.main()