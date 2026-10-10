"""
Tests for what the panels over the game say (tsw_joystick_ui.py), without opening any window: the ride lines on
the stop panel and the end-of-service summary.

    python -m unittest discover tests
"""

import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
