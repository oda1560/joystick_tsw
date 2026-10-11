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

RIDE = {"stop": "jolt", "braking": 1.91, "harsh": [["brake", 1.92]], "jolts": 0, "passengers": 15, "smooth": 2,
        "rides": 3}


class RideLines(unittest.TestCase):
    def test_a_jolt(self):
        (line, colour), (tally, _) = ui.DwellOverlay.ride_lines(RIDE)
        self.assertEqual(line, "Jolt on stopping, 1.9 m/s²  ·  1 harsh brake")
        self.assertEqual(colour, ui.BAD)
        self.assertEqual(tally, "15 passengers  ·  smooth rides 2 of 3 this service")

    def test_smooth(self):
        (line, colour), _ = ui.DwellOverlay.ride_lines(dict(RIDE, stop="smooth", harsh=[]))
        self.assertEqual((line, colour), ("Smooth stop", ui.GOOD))

    def test_jolts_on_the_way_and_one_passenger(self):
        (line, _), (tally, _) = ui.DwellOverlay.ride_lines(dict(RIDE, stop="smooth", harsh=[], jolts=1,
                                                                passengers=1, rides=0))
        self.assertEqual(line, "Smooth stop  ·  jolt at a stop on the way")
        self.assertEqual(tally, "1 passenger")

    def test_an_empty_train(self):
        _, (tally, _) = ui.DwellOverlay.ride_lines(dict(RIDE, passengers=0))
        self.assertEqual(tally, "no passengers aboard  ·  smooth rides 2 of 3 this service")

    def test_nothing_to_say(self):
        self.assertEqual(ui.DwellOverlay.ride_lines(None), (("", ui.FG), ("", ui.MUTED)))


RUN = {"id": "1M56@61000.0", "finished": "2026-10-10T19:42:00", "service": "1M56", "route": "TFW Cardiff City",
       "train": "RVM_TFW_Class150_DMS_C", "points": 1320, "stops": 4, "on_time": 3, "worst_late": 130.0,
       "accuracy": 1.1, "passengers": 58, "rides": 4, "smooth": 3}


class Summary(unittest.TestCase):
    def test_against_the_best_before(self):
        best = dict(RUN, id="1M56@60000.0", finished="2026-10-09T18:00:00", train="RVM_TFW_Class142_DMS_C",
                    points=1150, accuracy=1.8, smooth=2, passengers=48)
        title, rows, footer, times = ui.SummaryOverlay.summary_for(RUN, best, 2)
        self.assertEqual(title, "SERVICE COMPLETE  ·  1M56  ·  TFW Cardiff City")
        cells = {r[0][0]: r[1:] for r in rows[1:]}
        self.assertEqual(cells["Points"], (("1,320", ui.GOOD, True), ("1,150", ui.FG, False)))
        self.assertEqual(cells["On time"][0], ("3 of 4 stops", ui.FG, True))       # no better
        self.assertEqual(cells["Stop accuracy"][0][1], ui.GOOD)                     # nearer the marker
        self.assertEqual(footer.split("\n"), ["Latest 2:10 behind time at a stop",
                                              "Best before on 2026-10-09 with the TFW Class142 DMS  ·  2 runs of "
                                              "1M56 kept"])
        self.assertEqual(times, (False, True, True))

    def test_first_run(self):
        title, rows, footer, times = ui.SummaryOverlay.summary_for(dict(RUN, worst_late=10.0), None, 1)
        self.assertEqual(rows[0], (("", ui.MUTED, True), ("THIS RUN", ui.MUTED, True)))
        self.assertEqual(footer, "First run of 1M56 kept")

    def test_no_passengers_row_for_an_empty_train(self):
        title, rows, footer, times = ui.SummaryOverlay.summary_for(dict(RUN, passengers=0), None, 1)
        self.assertNotIn("Passengers", [r[0][0] for r in rows])


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
