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


class NextStation(unittest.TestCase):
    """The next stop for the panel between stops (StopTracker.next_stop): go-vias left out, its name and arrival
    as the timetable's stop step has them."""

    def setUp(self):
        instructions = [("LoadUnload", True, "", ()),                              # 0 Bradford Forster Square
                        ("GoTo", True, "Frizinghall Platform 1", ()),
                        ("LoadUnload", True, "", ()),                              # 2
                        ("GoTo", True, "Shipley Platform 2", ("Junction",)),
                        ("LoadUnload", True, "", ()),                              # 4
                        ("GoTo", True, "", ()),                                    # the end, no name
                        ("LoadUnload", True, "", ())]                              # 6, not timed
        times = [(None, 30600.0), (None, None), (30930.0, 30990.0), (None, None), (31260.0, None), (None, None),
                 (None, None)]
        steps = [(0, None), (1, None), (2, None), (3, 0), (3, None), (4, None), (5, None), (6, None)]
        targets = [tsw_stops.Target("stop", 1, None, 1000.0, "Frizinghall Platform 1"),
                   tsw_stops.Target("via", 3, 0, 2500.0, "Junction"),
                   tsw_stops.Target("stop", 3, None, 3300.0, "Shipley Platform 2"),
                   tsw_stops.Target("stop", 5, None, 6000.0, "")]
        self.path = tsw_stops.ServicePath("2P27", steps, targets, {}, instructions,
                                          {"origin": "BradfordForsterSquare", "end": "LeedsCity", "times": times})
        self.tracker = tsw_stops.StopTracker()
        self.tracker.path, self.tracker.position, self.tracker.fixed = self.path, 2000.0, True

    def next(self, step, clock=31092.0):
        order = self.path.order[step] if step is not None else None
        return self.tracker._next_stop(order, clock)

    def test_the_go_via_on_the_way_is_left_out(self):
        self.assertEqual(self.next((3, 0)), {"station": "Shipley", "platform": "Platform 2", "arrives": 31260.0,
                                             "now": 31092.0, "metres": 1300.0, "estimated": False,
                                             "service": "2P27"})

    def test_from_the_stop_before(self):
        self.assertEqual(self.next((2, None))["station"], "Shipley")

    def test_counted_from_the_last_stop(self):
        self.tracker.fixed = False
        self.assertTrue(self.next((3, None))["estimated"])

    def test_not_placed_yet(self):
        self.tracker.position = None
        self.assertIsNone(self.next((3, None))["metres"])

    def test_the_end_of_the_service(self):
        nxt = self.next((5, None), clock=86400.0 + 31500.0)
        self.assertEqual((nxt["station"], nxt["platform"], nxt["arrives"], nxt["now"]),
                         ("Leeds City", "", None, 31500.0))

    def test_none_after_the_last_stop_or_without_the_clock(self):
        self.assertIsNone(self.tracker._next_stop(len(self.path.steps), 31092.0))
        self.assertIsNone(self.next((3, None), clock=None))

    def test_without_objectives_by_where_the_train_is(self):
        self.tracker.position = 3330.0                       # past Shipley
        self.assertEqual(self.next(None)["station"], "Leeds City")


class RouteName(unittest.TestCase):
    def test_from_the_timetable_id(self):
        self.assertEqual(tsw_stops.route_name("/MedwayValley/Map/MVL:PersistentLevel.SandboxRouteTimetable_1.T"),
                         "Medway Valley")
        self.assertEqual(tsw_stops.route_name("/TFW_CardiffCity/Maps/x:PersistentLevel.y"), "TFW Cardiff City")
        self.assertEqual(tsw_stops.route_name(None), "")


if __name__ == "__main__":
    unittest.main()
