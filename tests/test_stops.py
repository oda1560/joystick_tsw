"""
Tests for tsw_stops.py where the game doesn't answer: a read that failed once must not spoil the rest of the
service (the doors of a car left out, the timetable never read, the objectives never matched).

    python -m unittest discover tests
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_stops  # noqa: E402


class FakeApi:
    """Cars listing `cars[i]` (None: no answer), doors open as in `open_doors`."""
    key = "key"

    def __init__(self, cars=(), open_doors=(), service="2P30"):
        self.cars, self.open_doors, self.service = list(cars), set(open_doors), service

    def load_key(self):
        return self.key

    def get(self, path):
        if path == "DriverAid.PlayerInfo":
            return {"Result": "Success", "Values": {"currentServiceName": self.service}}
        return {}

    def get_value(self, path):
        if path == "CurrentFormation.FormationLength":
            return len(self.cars)
        if path.endswith(".Function.GetCurrentOutputValue"):
            return 1.0 if path.split(".Function")[0] in self.open_doors else 0.0
        return None

    def list(self, path):
        if path == "Objectives":
            raise ConnectionAbortedError("aborted")
        names = self.cars[int(path.rsplit("/", 1)[1])]
        return {"Nodes": [{"NodeName": n} for n in names or []]}


class Doors(unittest.TestCase):
    def test_a_car_that_didnt_answer_is_looked_at_again(self):
        tracker = tsw_stops.StopTracker()
        tracker.api = FakeApi([["PassengerDoor_FL", "Horn"], None], open_doors={"CurrentFormation/1/PassengerDoor_BR"})
        self.assertFalse(tracker._doors_open())
        self.assertIsNone(tracker.door_nodes)                    # not kept with car 1 left out
        tracker.api.cars[1] = ["PassengerDoor_BR"]
        tracker.doors_read = 0.0
        self.assertTrue(tracker._doors_open())
        self.assertEqual(tracker.door_nodes, ["CurrentFormation/0/PassengerDoor_FL",
                                              "CurrentFormation/1/PassengerDoor_BR"])


class Timetable(unittest.TestCase):
    def test_read_again_when_reading_it_failed(self):
        tracker = tsw_stops.StopTracker()
        tracker.api = FakeApi()

        def fails(service):
            raise IOError("timed out")
        tracker._load = fails
        with self.assertRaises(IOError):
            tracker.step()
        self.assertIsNone(tracker.service)                       # so the next step reads it again

    def test_objectives_matched_again_when_the_game_didnt_answer(self):
        tracker = tsw_stops.StopTracker()
        tracker.api = FakeApi()
        stop = types.SimpleNamespace(key=(0, None), kind="stop", via=None, named=True)
        tracker.path = types.SimpleNamespace(steps=[(0, None)], targets=[stop], name="2P30", signals={})
        for attempt in range(tsw_stops.MATCH_TRIES):
            tracker._find_objectives()
            self.assertIsNotNone(tracker.match_again, attempt)
        tracker._find_objectives()
        self.assertIsNone(tracker.match_again)                   # and gives up after a few


class RouteName(unittest.TestCase):
    def test_from_the_timetable_id(self):
        self.assertEqual(tsw_stops.route_name("/MedwayValley/Map/MVL:PersistentLevel.SandboxRouteTimetable_1.T"),
                         "Medway Valley")
        self.assertEqual(tsw_stops.route_name("/TFW_CardiffCity/Maps/x:PersistentLevel.y"), "TFW Cardiff City")
        self.assertEqual(tsw_stops.route_name(None), "")


if __name__ == "__main__":
    unittest.main()
