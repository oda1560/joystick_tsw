"""
Tests for what the panels over the game say (tsw_joystick_ui.py), without opening any window: the ride lines on
the stop panel, the end-of-service summary, and the button and key that hold up the next stop.

    python -m unittest discover tests
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_joystick_ui as ui  # noqa: E402

RIDE = {"stop": "jolt", "braking": 1.4, "passengers": 62, "mark": 79.0, "score": 87.2, "smooth": 1, "rides": 2,
        "lost": [[21.0, "stop", 1.4]]}


class RideLines(unittest.TestCase):
    def test_a_jolt(self):
        (line, colour), (tally, colour2) = ui.DwellOverlay.ride_lines(RIDE)
        self.assertEqual(line, "This ride 79  ·  jolt on stopping, 1.4 m/s²")
        self.assertEqual(colour, ui.WARN)
        self.assertEqual((tally, colour2), ("Comfort 87 this service  ·  62 passengers", ui.FG))

    def test_smooth(self):
        (line, colour), _ = ui.DwellOverlay.ride_lines(dict(RIDE, stop="smooth", mark=100.0, lost=[]))
        self.assertEqual((line, colour), ("This ride 100  ·  smooth stop", ui.GOOD))

    def test_the_two_costliest_and_one_passenger(self):
        lost = [[12.0, "harsh", "brake", 1.33, 2.0], [3.0, "way", 0.8], [2.4, "firm", "power", 2.4]]
        (line, colour), (tally, _) = ui.DwellOverlay.ride_lines(dict(RIDE, stop="smooth", mark=82.6, lost=lost,
                                                                     passengers=1, score=None))
        self.assertEqual(line, "This ride 83  ·  harsh braking 1.3 m/s², 2 s  ·  jolt at a stop on the way, 0.8 m/s²")
        self.assertEqual(tally, "1 passenger")

    def test_less_than_half_a_point_not_said(self):
        (line, _), _ = ui.DwellOverlay.ride_lines(dict(RIDE, stop="smooth", mark=99.7, lost=[[0.3, "firm", "brake",
                                                                                              0.3]]))
        self.assertEqual(line, "This ride 100  ·  smooth stop")

    def test_an_empty_train(self):
        _, (tally, _) = ui.DwellOverlay.ride_lines(dict(RIDE, passengers=0))
        self.assertEqual(tally, "Comfort 87 this service  ·  no passengers aboard")

    def test_colours(self):
        self.assertEqual([ui.mark_colour(m) for m in (100, 89.5, 89.4, 75, 74.4)],
                         [ui.GOOD, ui.GOOD, ui.WARN, ui.WARN, ui.BAD])

    def test_nothing_to_say(self):
        self.assertEqual(ui.DwellOverlay.ride_lines(None), (("", ui.FG), ("", ui.MUTED)))


class ComfortPopup(unittest.TestCase):
    """What the comfort panel says (tsw_comfort.ComfortWatch.coach)."""

    def said(self, coach):
        overlay = ui.ComfortOverlay.__new__(ui.ComfortOverlay)
        overlay.set = mock.Mock()
        with mock.patch.object(ui.Overlay, "show"):
            overlay.show(coach)
        return overlay.set.call_args[0]

    def test_what_a_harsh_moment_cost(self):
        lines = self.said({"phase": "lost", "level": "harsh", "kind": "brake", "cost": 12.1, "mark": 87.9,
                           "score": 92.85, "passengers": 150})
        self.assertEqual(lines, (("HARSH BRAKING  −12", ui.BAD), ("COMFORT 93", ui.FG),
                                 ("this ride 88  ·  150 passengers aboard", ui.FG)))

    def test_what_firm_acceleration_cost(self):
        lines = self.said({"phase": "lost", "level": "firm", "kind": "power", "cost": 3.2, "mark": 96.8,
                           "score": 96.8, "passengers": 0})
        self.assertEqual(lines[0], ("FIRM ACCELERATION  −3", ui.WARN))
        self.assertEqual(lines[2][0], "this ride 97  ·  no passengers aboard")

    def test_a_jolt_on_stopping(self):
        lines = self.said({"phase": "stopped", "jolt": True, "braking": 1.4, "cost": 21.0, "mark": 79.0,
                           "score": 87.2})
        self.assertEqual(lines, (("COMFORT 87", ui.FG), ("JOLT ON STOPPING", ui.BAD),
                                 ("braking 1.4 m/s² as it stopped  ·  −21", ui.MUTED)))

    def test_a_smooth_stop(self):
        lines = self.said({"phase": "stopped", "jolt": False, "braking": 0.5, "cost": 0.0, "mark": 100.0,
                           "score": 95.0})
        self.assertEqual(lines[2], ("braking 0.5 m/s² as it stopped", ui.MUTED))


def ride(station, mark, lost=()):
    return {"station": station, "mark": mark, "passengers": 30, "smooth": not lost, "lost": [list(x) for x in lost]}


MARKS = [ride("Pontypridd", 100.0), ride("Trefforest", 79.0, [[21.0, "stop", 1.4]]),
         ride("Taffs Well", 96.0, [[4.0, "firm", "brake", 4.0]]), ride("Radyr", 100.0)]
RUN = {"id": "1M56@61000.0", "finished": "2026-10-10T19:42:00", "service": "1M56", "route": "TFW Cardiff City",
       "train": "RVM_TFW_Class150_DMS_C", "points": 1320, "stops": 4, "on_time": 3, "worst_late": 130.0,
       "accuracy": 1.1, "passengers": 58, "rides": 4, "smooth": 3, "comfort": 94.6, "marks": MARKS}


class Summary(unittest.TestCase):
    def test_against_the_best_before(self):
        best = dict(RUN, id="1M56@60000.0", finished="2026-10-09T18:00:00", train="RVM_TFW_Class142_DMS_C",
                    points=1150, accuracy=1.8, smooth=2, passengers=48, comfort=88.6)
        title, rows, footer, times, rides = ui.SummaryOverlay.summary_for(RUN, best, 2)
        self.assertEqual(title, "SERVICE COMPLETE  ·  1M56  ·  TFW Cardiff City")
        cells = {r[0][0]: r[1:] for r in rows[1:]}
        self.assertEqual(cells["Points"], (("1,320", ui.GOOD, True), ("1,150", ui.FG, False)))
        self.assertEqual(cells["Comfort"], (("95", ui.GOOD, True), ("89", ui.FG, False)))
        self.assertEqual(cells["On time"][0], ("3 of 4 stops", ui.FG, True))       # no better
        self.assertEqual(cells["Stop accuracy"][0][1], ui.GOOD)                     # nearer the marker
        self.assertEqual(footer.split("\n"), ["Latest 2:10 behind time at a stop",
                                              "Comfort: each ride's mark, weighted by the passengers aboard",
                                              "Best before on 2026-10-09 with the TFW Class142 DMS  ·  2 runs of "
                                              "1M56 kept"])
        self.assertEqual(times, (False, True, True))

    def test_first_run(self):
        title, rows, footer, times, rides = ui.SummaryOverlay.summary_for(dict(RUN, worst_late=10.0), None, 1)
        self.assertEqual(rows[0], (("", ui.MUTED, True), ("THIS RUN", ui.MUTED, True)))
        self.assertEqual(footer.split("\n")[-1], "First run of 1M56 kept")

    def test_a_run_from_before_the_comfort_marks(self):
        old = {k: v for k, v in RUN.items() if k not in ("comfort", "marks")}
        title, rows, footer, times, rides = ui.SummaryOverlay.summary_for(RUN, old, 2)
        cells = {r[0][0]: r[1:] for r in rows[1:]}
        self.assertEqual(cells["Comfort"], (("95", ui.FG, True), ("-", ui.FG, False)))
        _, rows, footer, _, rides = ui.SummaryOverlay.summary_for(old, None, 1)
        self.assertNotIn("Comfort", [r[0][0] for r in rows])
        self.assertIsNone(rides)

    def test_no_passengers_row_for_an_empty_train(self):
        title, rows, footer, times, rides = ui.SummaryOverlay.summary_for(dict(RUN, passengers=0), None, 1)
        self.assertNotIn("Passengers", [r[0][0] for r in rows])
        self.assertIn("Comfort: the rides' marks, nobody aboard", footer)

    def test_each_ride(self):
        rows, note = ui.SummaryOverlay.rides_for(MARKS)
        self.assertEqual(rows, (("Pontypridd", "100", ui.GOOD, ""),
                                ("Trefforest", "79", ui.WARN, "jolt on stopping, 1.4 m/s²"),
                                ("Taffs Well", "96", ui.GOOD, "4 s firm braking"),
                                ("Radyr", "100", ui.GOOD, "")))
        self.assertEqual(note, "")

    def test_a_long_service_lists_the_rides_that_lost_points(self):
        marks = [ride(f"Stop {i}", 100.0) for i in range(30)]
        marks[3], marks[20] = ride("Stop 3", 60.0, [[40.0, "stop", 1.9]]), ride("Stop 20", 90.0)
        rows, note = ui.SummaryOverlay.rides_for(marks)
        self.assertEqual([r[:2] for r in rows], [("Stop 3", "60"), ("Stop 20", "90")])
        self.assertEqual(note, "+ 28 more rides at 100")
        marks = [ride(f"Stop {i}", 50.0 + i) for i in range(30)]                    # the worst ten, in order
        rows, note = ui.SummaryOverlay.rides_for(marks)
        self.assertEqual([r[0] for r in rows], [f"Stop {i}" for i in range(10)])
        self.assertEqual(note, "+ 20 more rides")


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

    def test_see_through_or_cloaked_or_its_own_panels(self):
        self.assertFalse(self.hidden((self.GAME, 0x00080020, 777, 0)))        # a driver's overlay
        self.assertFalse(self.hidden((self.GAME, 0, 888, 2)))                 # cloaked
        self.assertFalse(self.hidden((self.GAME, 0, os.getpid(), 0)))         # the bridge's own
        self.assertTrue(self.hidden((self.GAME, 0x00080020, 777, 0), (self.GAME, 0x40008, 4321, 0)))


class NextStopHeld(unittest.TestCase):
    """The next stop panel shows while its joystick button or its key is held."""

    def setUp(self):
        self.app = SimpleNamespace(stop_held=set(), s={"stop_key": "F6"}, fg_pid=None, fg_ok=False,
                                   tracker=SimpleNamespace(shown=False, state=None), speed_watch=None,
                                   overlay=mock.Mock())

    def hold(self, source, down):
        ui.App._show_stop(self.app, source, down)

    def key_held(self, pressed, foreground):
        with mock.patch.object(ui.ctypes.windll.user32, "GetAsyncKeyState",
                               lambda vk: 0x8000 if vk == pressed else 0), \
                mock.patch.object(ui.autostart, "foreground_pid", lambda: foreground), \
                mock.patch.object(ui.autostart, "is_game", lambda pid: pid == 4321):
            return ui.App._stop_key_held(self.app)

    def test_button_and_key_together(self):
        self.hold("button", True)
        self.hold("key", True)
        self.hold("button", False)              # the key still holds it up
        self.assertTrue(self.app.tracker.shown)
        self.hold("key", False)
        self.assertFalse(self.app.tracker.shown)
        self.assertEqual((self.app.overlay.show.call_count, self.app.overlay.hide.call_count), (1, 1))

    def test_key_counts_in_the_game_and_this_window_only(self):
        self.assertTrue(self.key_held(0x75, 4321))           # F6 in the game
        self.assertFalse(self.key_held(0x75, 999))           # typing somewhere else
        self.assertTrue(self.key_held(0x75, os.getpid()))    # the bridge window
        self.assertFalse(self.key_held(0x76, 4321))          # F7 is the game's
        self.app.s["stop_key"] = None
        self.assertFalse(self.key_held(0x75, 4321))

    def test_only_keys_the_game_leaves_free(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            for saved, kept in (("F7", None), ("Num *", "Num *"), (None, None)):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"stop_key": saved}, f)
                with mock.patch.object(ui, "SETTINGS_FILE", path):
                    self.assertEqual(ui.load_settings()["stop_key"], kept)


if __name__ == "__main__":
    unittest.main()
