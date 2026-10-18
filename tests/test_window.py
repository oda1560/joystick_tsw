"""
Tests for the bridge window (tsw_joystick_ui.py) without opening it: when its gauges are drawn again, and when
another window covers it.

    python -m unittest discover tests
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_joystick_ui as ui  # noqa: E402


class Redraw(unittest.TestCase):
    """The bridge window's gauges are drawn again only when they'd change, and not while it can't be seen."""

    def setUp(self):
        self.app = SimpleNamespace(root=SimpleNamespace(wm_frame=lambda: "0x10"), drawn=None, drawn_at=0.0,
                                   _draw=mock.Mock(), state=1)
        self.app._draw_state = lambda: self.app.state
        self.hidden, self.clock = False, 100.0

    def tick(self):
        with mock.patch.object(ui, "window_hidden", lambda hwnd: self.hidden), \
                mock.patch.object(ui.time, "monotonic", lambda: self.clock):
            ui.App._redraw(self.app)
        return self.app._draw.call_count

    def test_only_when_something_changed(self):
        self.assertEqual(self.tick(), 1)
        self.clock += 0.1
        self.assertEqual(self.tick(), 1)
        self.app.state = 2
        self.assertEqual(self.tick(), 2)

    def test_at_least_once_a_second(self):
        self.tick()
        self.clock += ui.REDRAW_SECONDS
        self.assertEqual(self.tick(), 2)

    def test_not_while_hidden_and_at_once_when_seen_again(self):
        self.tick()
        self.hidden = True
        self.app.state = 2
        self.assertEqual(self.tick(), 1)
        self.hidden = False
        self.app.state = 1                      # back as it was drawn, but it may have missed changes since
        self.clock += 0.1
        self.assertEqual(self.tick(), 2)


class WindowHidden(unittest.TestCase):
    """Which windows count as covering the bridge's: another program's, wholly over it, that can't be see-through."""
    MINE = (100, 100, 600, 500)
    GAME = (0, 0, 2560, 1440)

    def hidden(self, *above, iconic=False):
        """`above`: (rect, ex style, process, cloaked) of the windows over it, nearest first."""
        windows = {1: (self.MINE, 0, os.getpid(), 0)}
        windows.update({i + 2: w for i, w in enumerate(above)})

        def rect(h, p):
            r = p._obj
            r.left, r.top, r.right, r.bottom = windows[h][0]

        def pid(h, p):
            p._obj.value = windows[h][2]

        def cloaked(h, attribute, p, size):
            p._obj.value = windows[h][3]
        user32 = SimpleNamespace(IsIconic=lambda h: iconic, IsWindowVisible=lambda h: True,
                                 GetWindow=lambda h, cmd: h + 1 if h + 1 in windows else None,
                                 GetWindowLongW=lambda h, index: windows[h][1], GetWindowRect=rect,
                                 GetWindowThreadProcessId=pid)
        fake = SimpleNamespace(user32=user32, dwmapi=SimpleNamespace(DwmGetWindowAttribute=cloaked))
        with mock.patch.object(ui.ctypes, "windll", fake):
            return ui.window_hidden(1)

    def test_under_the_game(self):
        self.assertTrue(self.hidden((self.GAME, 0x40008, 4321, 0)))

    def test_minimised(self):
        self.assertTrue(self.hidden(iconic=True))

    def test_nothing_over_it(self):
        self.assertFalse(self.hidden())

    def test_partly_covered(self):
        self.assertFalse(self.hidden(((300, 0, 2560, 1440), 0, 4321, 0)))

    def test_see_through_or_cloaked_or_its_own(self):
        self.assertFalse(self.hidden((self.GAME, 0x00080020, 777, 0)))        # a driver's overlay
        self.assertFalse(self.hidden((self.GAME, 0, 888, 2)))                 # cloaked
        self.assertFalse(self.hidden((self.GAME, 0, os.getpid(), 0)))         # the bridge's own
        self.assertTrue(self.hidden((self.GAME, 0x00080020, 777, 0), (self.GAME, 0x40008, 4321, 0)))


if __name__ == "__main__":
    unittest.main()
