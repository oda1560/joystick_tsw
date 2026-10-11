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
        if path.endswith(".ObjectClass"):
            return "PushButtonComponent" if "Button" in path or "Release" in path else "RailVehicleDoorComponent"
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

    def test_class_350_doors(self):
        # node names of a Class 350 coach, seen live (one door is spelt Passsenger)
        car = ["Door_PasssengerDoor_FL", "Door_PassengerDoor_FR", "Door_GangwayDoor_F", "Door_InternalCabDoor",
               "PushButton_PassengerDoor_Open_Ext_FL", "PassengerDoorManager", "CabToPassengerDoor"]
        tracker = tsw_stops.StopTracker()
        tracker.api = FakeApi([car], open_doors={"CurrentFormation/0/Door_PasssengerDoor_FL"})
        self.assertTrue(tracker._doors_open())
        self.assertEqual(tracker.door_nodes, ["CurrentFormation/0/Door_PasssengerDoor_FL",
                                              "CurrentFormation/0/Door_PassengerDoor_FR"])

    def test_a_button_named_after_a_door_is_not_one(self):
        tracker = tsw_stops.StopTracker()
        tracker.api = FakeApi([["PassengerDoor_FL", "PassengerDoor_Release_L (PushButton)"]],
                              open_doors={"CurrentFormation/0/PassengerDoor_Release_L (PushButton)"})
        self.assertFalse(tracker._doors_open())
        self.assertEqual(tracker.door_nodes, ["CurrentFormation/0/PassengerDoor_FL"])


class DoorNames(unittest.TestCase):
    def test_passenger_doors(self):
        for name in ("PassengerDoor_FL", "Door_PassengerDoor_FR", "Door_PasssengerDoor_FL", "Door_Passenger_BL",
                     "RailVehicleDoor_FL", "SingleDoor_FC", "DoubleDoors_L", "BerthingDoor_L1", "CoachDoor_R",
                     "PassengerDoor_FL (DoorControl)", "VehiclePassengerDoors", "Door_T1"):
            self.assertTrue(tsw_stops.passenger_door(name), name)

    def test_other_doors(self):
        for name in ("Door_GangwayDoor_F", "GangWayDoor_B", "Door_InternalCabDoor", "CabDoorInt", "IntDoor_F",
                     "DriverDoor", "FirstClassDoor", "VestibuleDoor_L", "CompartmentDoor_1", "PassengerDoorManager",
                     "PushButton_PassengerDoor_Open_Ext_FL", "CabToPassengerDoor", "PassengerDoorWindow_L_A",
                     "ButtonDoorIndicatorLogicController", "Horn"):
            self.assertFalse(tsw_stops.passenger_door(name), name)

    def test_door_classes(self):
        self.assertTrue(tsw_stops.door_class("RailVehicleDoorComponent"))
        self.assertTrue(tsw_stops.door_class("DoorV2Component"))
        self.assertFalse(tsw_stops.door_class("PushButtonComponent"))
        self.assertFalse(tsw_stops.door_class("BP_EMK_LNWR_Class350-2_PassengerDoorManager_C"))


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
