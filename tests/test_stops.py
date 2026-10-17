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


class FirstStop(unittest.TestCase):
    def at(self, instruction, key):
        """The stop the tracker shows standing at the LoadUnload `instruction` of A -> B -> C (Shipley-like: two
        steps at A, the second untimed)."""
        tracker = tsw_stops.StopTracker()
        tracker.service = "2P27"
        tracker.points = types.SimpleNamespace(read=lambda names, clock: {})
        tracker._doors_open = lambda: False
        instructions = [("LoadUnload", True, None, []), ("LoadUnload", True, None, []), ("GoTo", True, "B", []),
                        ("LoadUnload", True, None, []), ("GoTo", True, "C", []), ("LoadUnload", True, None, [])]
        tracker.path = tsw_stops.ServicePath("2P27", [(i, None) for i in range(len(instructions))], [], {},
                                             instructions, {"origin": "A", "end": "C"})
        tracker._at_stop((instruction, None), key, 0.0, 36000.0, {})
        return tracker.stop

    def test_the_service_s_first_stop(self):
        self.assertTrue(self.at(0, 0)["first"])
        self.assertTrue(self.at(1, 1)["first"])               # the second step there
        self.assertFalse(self.at(3, 3)["first"])
        self.assertFalse(self.at(5, 5)["first"])


class Readout(unittest.TestCase):
    def test_it_says_the_service_and_where_along_it_the_stop_is(self):
        """For the route map's ring on the stop (tsw_livemap.stop_places)."""
        tracker = tsw_stops.StopTracker()
        tracker.path = types.SimpleNamespace(name="2H26")
        tracker._say("", tsw_stops.Target("stop", 2, None, 22069.4, "Bingley Platform 2"), 812.0)
        s = tracker.state
        self.assertEqual((s["label"], s["metres"], s["service"], s["at"]),
                         ("Stop at Bingley · Platform 2", 812.0, "2H26", 22069.4))
        tracker._say("No more stops")
        self.assertEqual((tracker.state["service"], tracker.state["at"]), (None, None))


class SharedReading(unittest.TestCase):
    """Following the train takes the signal, clock, speed and step from the comfort watch's reading."""

    def follow(self, shared):
        instructions = [("LoadUnload", True, "", ()), ("GoTo", True, "Shipley Platform 2", ()),
                        ("LoadUnload", True, "", ())]
        path = tsw_stops.ServicePath("2P27", [(0, None), (1, None), (2, None)],
                                     [tsw_stops.Target("stop", 1, None, 3300.0, "Shipley Platform 2")], {},
                                     instructions, {"times": [(None, None)] * 3})
        tracker = tsw_stops.StopTracker(reading=lambda name: shared.get(name))
        asked = []
        tracker.api = types.SimpleNamespace(key="key", get=lambda p: asked.append(p) or {},
                                            get_value=lambda p: asked.append(p))
        tracker.paths, tracker.path, tracker.steps, tracker.offset = [path], path, path.steps, 0
        tracker._follow()
        return tracker, asked

    def test_nothing_asked_of_the_game(self):
        tracker, asked = self.follow({"aid": {"signalSeen": False}, "clock": {"LocalTime": 36000 * 10**7},
                                      "speed": {"Speed": 20.0}, "objective": {"Current": 1}})
        self.assertEqual(asked, [])
        self.assertEqual((tracker.step_key, tracker.clock), ((1, None), 36000.0))

    def test_asked_without_a_reading(self):
        _, asked = self.follow({})
        self.assertEqual(asked, ["DriverAid.Data", "TimeOfDay.Data", "CurrentDrivableActor.Function.HUD_GetSpeed",
                                 "Objectives.Current"])


class RouteName(unittest.TestCase):
    def test_from_the_timetable_id(self):
        self.assertEqual(tsw_stops.route_name("/MedwayValley/Map/MVL:PersistentLevel.SandboxRouteTimetable_1.T"),
                         "Medway Valley")
        self.assertEqual(tsw_stops.route_name("/TFW_CardiffCity/Maps/x:PersistentLevel.y"), "TFW Cardiff City")
        self.assertEqual(tsw_stops.route_name(None), "")


if __name__ == "__main__":
    unittest.main()
