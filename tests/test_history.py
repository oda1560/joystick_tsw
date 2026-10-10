"""
Tests for tsw_history.py: keeping finished runs, telling them apart, and the best run before.

    python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tsw_history  # noqa: E402

BASE = {"service": "1M56", "route": "TFW Cardiff City", "train": "RVM_TFW_Class150_DMS_C"}


def at_end(points, first, stops):
    """tsw_stops.StopTracker.stop at the last stop, with the points read from the save."""
    return dict(BASE, points={"points": points, "stops": stops, "first": first})


class History(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "service_history.json")
        self.history = tsw_history.History(self.path)

    def test_a_run(self):
        run = self.history.finish(at_end(900, 68000.0, [(-30, 1.0), (70, 3.0), (10, 0.5)]),
                                  {"rides": 3, "smooth": 2, "passengers": 40})
        self.assertEqual((run["points"], run["stops"], run["on_time"], run["worst_late"], run["accuracy"]),
                         (900, 3, 2, 70, 1.5))
        self.assertEqual((run["passengers"], run["rides"], run["smooth"]), (40, 3, 2))
        self.assertEqual(run["route"], "TFW Cardiff City")

    def test_the_same_run_read_again_is_kept_once(self):
        stops = [(0, 1.0)]
        self.history.finish(at_end(900, 68000.0, stops))
        run = self.history.finish(at_end(1150, 68000.0, stops))       # the points came in after
        self.assertEqual(len(self.history.runs), 1)
        self.assertEqual(run["points"], 1150)

    def test_best_before_and_count(self):
        self.history.finish(at_end(1150, 1.0, [(0, 1.0)]))
        self.history.finish(at_end(800, 2.0, [(0, 1.0)]))
        run = self.history.finish(at_end(1300, 3.0, [(0, 1.0)]))
        self.assertEqual(self.history.best_before(run)["points"], 1150)
        self.assertEqual(self.history.count("1M56"), 3)
        self.assertIsNone(self.history.best_before(self.history.finish(dict(at_end(1, 4.0, [(0, 1.0)]),
                                                                             service="2P27"))))

    def test_kept_on_disk(self):
        self.history.finish(at_end(900, 1.0, [(0, 1.0)]))
        self.assertEqual(len(tsw_history.History(self.path).runs), 1)

    def test_comfort_kept_when_read_again_without_it(self):
        self.history.finish(at_end(900, 1.0, [(0, 1.0)]), {"rides": 4, "smooth": 3, "passengers": 58})
        again = tsw_history.History(self.path).finish(at_end(900, 1.0, [(0, 1.0)]), None)
        self.assertEqual(again["smooth"], 3)

    def test_no_stops_no_run(self):
        self.assertIsNone(self.history.finish(dict(BASE, points=None)))
        self.assertIsNone(self.history.finish(at_end(900, None, [])))

    def test_a_broken_file_is_kept_aside(self):
        with open(self.path, "w") as f:
            f.write("{broken")
        history = tsw_history.History(self.path)
        self.assertEqual(history.runs, [])
        self.assertTrue(os.path.exists(self.path + ".bad"))


if __name__ == "__main__":
    unittest.main()
