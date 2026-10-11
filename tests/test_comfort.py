"""
Tests for tsw_comfort.py: a drive logged in the game (Class 150, Cardiff valleys 1M56, full service to a stand at
the end: data/class150_1M56.csv, with time, speed, acceleration, gradient, power notch and brake handle), made-up
stops and brakes, and the subscription feed against a stand-in for the game's API.

    python -m unittest discover tests
"""

import csv
import json
import math
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_comfort  # noqa: E402

DRIVE = os.path.join(HERE, "data", "class150_1M56.csv")


def replay(passengers=15):
    """The logged drive through a Judge: (judge, [(t, panel phase, speed, panel)] as the panel changed, said)."""
    judge, panels, said = tsw_comfort.Judge(), [], []
    with open(DRIVE) as f:
        for r in csv.DictReader(f):
            t, v = float(r["t"]), float(r["speed"])
            said += [(t, e) for e in judge.feed(t, v, float(r["acc"]), passengers, None, float(r["brake"]),
                                                float(r["power"]) > 0)]
            p = judge.panel
            phase = p and p["phase"]
            if not panels or panels[-1][1] != phase:
                panels.append((t, phase, v, p))
    return judge, panels, said


class LoggedDrive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.judge, cls.panels, cls.said = replay()

    def shown(self, phase):
        return [(t, v, p) for t, ph, v, p in self.panels if ph == phase]

    def test_panels_come_in_order(self):
        self.assertEqual([ph for _, ph, _, _ in self.panels if ph], ["firm", "harsh", "ease", "stopped"])

    def test_one_far_out_reading_shows_nothing(self):
        # one slow frame read -1.09 m/s² between -0.76 and -0.70 at 12.7 s, braking at 0.8
        self.assertFalse(any(ph and t < 100 for t, ph, _, _ in self.panels))

    def test_warning_comes_about_4_s_before_the_stop(self):
        t, v, p = self.shown("ease")[0]
        self.assertTrue(5.0 < v < 6.0, v)
        self.assertAlmostEqual(p["seconds"], 4.0, delta=0.2)

    def test_stopping_on_full_service_is_a_jolt(self):
        t, v, p = self.shown("stopped")[0]
        self.assertTrue(p["jolt"])
        self.assertAlmostEqual(p["braking"], 1.83, delta=0.05)

    def test_harsh_braking_red_from_1_2(self):
        t, v, p = self.shown("harsh")[0]
        self.assertEqual(p["kind"], "brake")
        self.assertGreaterEqual(p["felt"], 1.2)

    def test_ride_report(self):
        r = self.judge.report("1M56")
        self.assertEqual(r["stop"], "jolt")
        self.assertEqual([k for k, *_ in r["harsh"]], ["brake"])
        self.assertEqual((r["passengers"], r["smooth"], r["rides"], r["way"]), (15, 0, 1, []))

    def test_full_service_to_a_stand_marks_low(self):
        # stopping at 1.83 m/s² (the most a stop costs), 9 s harsh before it, 15 s firm before that
        r = self.judge.report("1M56")
        self.assertEqual([loss[1] for loss in r["lost"]], ["stop", "harsh", "firm"])
        self.assertEqual(r["lost"][0][0], tsw_comfort.STOP_MOST)
        self.assertAlmostEqual(r["lost"][1][4], 9.25, delta=0.5)
        self.assertAlmostEqual(r["mark"], 28, delta=2)
        self.assertAlmostEqual(r["score"], r["mark"], delta=0.05)
        stopped = self.shown("stopped")[0][2]
        self.assertEqual((stopped["cost"], stopped["mark"]), (tsw_comfort.STOP_MOST, r["mark"]))

    def test_said_for_the_log(self):
        kinds = [e[0] for _, e in self.said]
        self.assertEqual(kinds, ["harsh", "stop"])

    def test_an_empty_passenger_train_is_judged_too(self):
        # a 331 on 2V04 had nobody aboard and showed nothing when it was only for trains with passengers
        judge, panels, said = replay(passengers=0)
        self.assertEqual([ph for _, ph, _, _ in panels if ph], ["firm", "harsh", "ease", "stopped"])
        self.assertEqual(next(p for _, ph, _, p in panels if ph == "harsh")["passengers"], 0)
        r = judge.report("1M56")
        self.assertEqual((r["stop"], r["passengers"], r["rides"]), ("jolt", 0, 1))

    def test_not_a_passenger_train_nothing_shown(self):
        judge, panels, said = replay(passengers=None)
        self.assertEqual([ph for _, ph, _, _ in panels if ph], [])
        self.assertIsNone(judge.report("1M56"))


def stop(decel_at, passengers=20, v=12.0, dt=0.07):
    """A made-up stop braking at decel_at(speed): (panels shown in order, the judge)."""
    judge, t, shown = tsw_comfort.Judge(), 0.0, []
    while t < 60:
        d = decel_at(v) if v > 0.02 else 0.0
        v = max(0.0, v - d * dt) if v > 0.02 else 0.0
        judge.feed(t, v, -d, passengers)
        p = judge.panel
        key = p and (p["phase"], p.get("jolt"))
        if key and (not shown or shown[-1] != key):
            shown.append(key)
        t += dt
    return shown, judge


class MadeUpStops(unittest.TestCase):
    def test_easing_off_clears_the_warning_and_is_smooth(self):
        shown, judge = stop(lambda v: 1.0 if v > 3 else 0.3 + 0.7 * (v - 1) / 2 if v > 1 else 0.3)
        self.assertEqual(shown, [("firm", None), ("ease", None), ("stopped", False)])
        self.assertEqual(judge.report("X")["stop"], "smooth")

    def test_holding_the_brake_to_the_stop_is_a_jolt(self):
        shown, judge = stop(lambda v: 0.8)
        self.assertEqual(shown, [("ease", None), ("stopped", True)])

    def test_light_braking_no_warning(self):
        shown, judge = stop(lambda v: 0.5)
        self.assertEqual(shown, [("stopped", False)])

    def test_tally_over_a_service(self):
        judge = tsw_comfort.Judge()
        for decel in (0.5, 0.8, 0.5):                 # smooth, jolt, smooth
            v, t = 10.0, judge.coach_until + 10
            while v > 0:
                v = max(0.0, v - decel * 0.07)
                judge.feed(t, v, -decel if v else 0.0, 30)
                t += 0.07
            judge.new_leg("2P27")
        done = judge.service_so_far("2P27")
        self.assertEqual((done["rides"], done["smooth"], done["passengers"]), (3, 2, 30))
        self.assertEqual([r["mark"] for r in done["marks"]], [100, 97, 100])    # 0.8 as it stopped: 3 off
        self.assertAlmostEqual(done["comfort"], 99)

    def test_another_train_standing_is_not_a_stop(self):
        judge = tsw_comfort.Judge()
        for i in range(20):                           # a 331 braking at 20 m/s ...
            judge.feed(i * 0.07, 20.0 - i * 0.07, -1.0, 11)
        judge.lost()                                  # ... left for an 807 standing still
        said = [e for i in range(20) for e in judge.feed(10 + i * 0.07, 0.0, 0.0, 390)]
        self.assertEqual(said, [])
        self.assertIsNone(judge.panel)


def brake_drive(steps, lag=0.4, tau=1.0, wall_dt=0.08, game_dt=0.05, power_at=None):
    """A brake whose force follows the handle after `lag` with a first-order lag `tau`, in slow motion (game time
    0.05 s a 0.08 s frame): the brake-ease timings the Judge says, and its game/wall ratio."""
    judge = tsw_comfort.Judge()
    t, wall, v, demand, force, history, timed = 0.0, 0.0, 25.0, 1.0, 1.2, [], []
    while t < 25.0 and v > 0.5:
        for at, d in steps:
            if abs(t - at) < game_dt / 2:
                demand = d
        history.append((t, demand))
        target = 1.2 * next((d for tt, d in reversed(history) if t - tt >= lag), history[0][1])
        force += (target - force) * (1 - math.exp(-game_dt / tau))
        v = max(0.0, v - force * game_dt)
        power = power_at is not None and power_at <= t < power_at + 3
        timed += [e[1] for e in judge.feed(wall, v, -force, 30, 6e4 + t, demand, power) if e[0] == "ease"]
        t, wall = t + game_dt, wall + wall_dt
    return timed, judge.ratio


def start(acc, seconds=10.0, dt=0.07):
    """Speeding up from a stand at `acc` m/s²: (panels shown in order, the judge)."""
    judge, t, v, shown = tsw_comfort.Judge(), 0.0, 0.0, []
    while t < seconds:
        v += acc * dt
        judge.feed(t, v, acc, 20)
        p = judge.panel
        key = p and (p["phase"], p["kind"])
        if key and (not shown or shown[-1] != key):
            shown.append(key)
        t += dt
    return shown, judge


class Limits(unittest.TestCase):
    def test_a_class_331_on_full_power_is_fine(self):
        # the real Class 331 does 1.3 m/s² on full power
        self.assertEqual(start(1.25)[0], [])

    def test_speeding_up_firm_over_1_3(self):
        self.assertEqual(start(1.4)[0], [("firm", "power")])

    def test_speeding_up_harsh_over_1_5(self):
        shown, judge = start(1.6)
        self.assertEqual(shown, [("harsh", "power")])
        self.assertEqual([k for k, *_ in judge.report("X")["harsh"]], ["power"])

    def test_braking_as_hard_is_firm(self):
        self.assertEqual(stop(lambda v: 1.0)[0][0], ("firm", None))
        self.assertEqual(start(1.0)[0], [])


def drive(segments, v=25.0, passengers=40, dt=0.07, rate=None):
    """Moving at `v` m/s, then (seconds, acceleration) one after another; `rate`: game seconds a wall second (the
    game's clock read), None: no clock. (judge, [(t, phase, panel)] each time the panel changed)."""
    judge, t, shown = tsw_comfort.Judge(), 0.0, [(0.0, None, None)]
    for seconds, acc in segments:
        end = t + seconds
        while t < end - 1e-9:
            v = max(0.0, v + acc * dt * (rate or 1.0))
            judge.feed(t, v, acc, passengers, 6e4 + t * rate if rate else None)
            p = judge.panel
            if shown[-1][1] != (p and p["phase"]):
                shown.append((t, p and p["phase"], p))
            t += dt
    return judge, shown


def stop_at(judge, decel, t, passengers=30, v=10.0):
    """A made-up stop braking at `decel` from `v`: the time after."""
    while v > 0:
        v = max(0.0, v - decel * 0.07)
        judge.feed(t, v, -decel if v else 0.0, passengers)
        t += 0.07
    return t + 10.0


class Marks(unittest.TestCase):
    def test_a_firm_stretch_then_what_it_cost(self):
        judge, shown = drive([(5, -1.0), (8, -0.5)])
        self.assertEqual([ph for _, ph, _ in shown], [None, "firm", "lost", None])
        t, _, lost = shown[2]
        self.assertEqual((lost["level"], lost["kind"], lost["passengers"]), ("firm", "brake", 40))
        self.assertAlmostEqual(lost["cost"], 5.0, delta=0.2)
        self.assertAlmostEqual(lost["mark"], 95.0, delta=0.2)
        self.assertAlmostEqual(lost["score"], lost["mark"], delta=0.05)
        self.assertAlmostEqual(t, 5 + tsw_comfort.HOLD_SECONDS, delta=0.2)          # once the warning's gone
        self.assertAlmostEqual(shown[3][0] - t, tsw_comfort.LOSS_SECONDS, delta=0.1)

    def test_a_harsh_moment_then_what_it_cost(self):
        judge, shown = drive([(2, -1.3), (6, -0.5)])
        self.assertEqual([ph for _, ph, _ in shown], [None, "harsh", "lost", None])
        lost = shown[2][2]
        self.assertEqual(lost["level"], "harsh")
        self.assertAlmostEqual(lost["cost"], 12.0, delta=0.3)                       # 10, and 2 for the second second
        self.assertAlmostEqual(lost["mark"], 88.0, delta=0.3)
        r = judge.report("X")
        self.assertEqual([loss[1] for loss in r["lost"]], ["harsh"])

    def test_over_harsh_too_briefly_is_firm(self):
        judge, shown = drive([(0.8, -1.3), (6, -0.5)])
        self.assertEqual([ph for _, ph, _ in shown], [None, "harsh", "lost", None])
        self.assertEqual(shown[2][2]["level"], "firm")
        r = judge.report("X")
        self.assertEqual(r["harsh"], [])
        self.assertAlmostEqual(r["firm"]["brake"], 0.8, delta=0.1)

    def test_seconds_in_the_game_s_time(self):
        # in slow motion 5 s on the wall clock is 3 s in the game
        judge, shown = drive([(5, -1.0), (8, -0.5)], rate=0.6)
        self.assertAlmostEqual(next(p for _, ph, p in shown if ph == "lost")["cost"], 3.0, delta=0.2)

    def test_nothing_lost_nothing_shown(self):
        judge, shown = drive([(10, -0.8)])
        self.assertEqual([ph for _, ph, _ in shown], [None])
        self.assertIsNone(judge.report("X"))                # not stopped, nothing lost

    def test_a_stop_costs_by_how_hard_it_stopped(self):
        self.assertEqual(tsw_comfort._stop_cost(0.6), 0)
        self.assertAlmostEqual(tsw_comfort._stop_cost(1.4), 21)
        self.assertEqual(tsw_comfort._stop_cost(1.9), tsw_comfort.STOP_MOST)

    def test_a_jolt_at_a_stop_on_the_way(self):
        judge = tsw_comfort.Judge()
        stop_at(judge, 0.5, stop_at(judge, 0.8, 0.0))        # a signal with a jolt, then the station smoothly
        r = judge.report("X")
        self.assertEqual((r["stop"], r["way"]), ("smooth", [0.8]))
        self.assertEqual([loss[1] for loss in r["lost"]], ["way"])
        self.assertAlmostEqual(r["mark"], 97)
        self.assertEqual(r["smooth"], 0)

    def test_score_weighted_by_passengers(self):
        rides = [{"mark": 100, "passengers": 40}, {"mark": 79, "passengers": 62}]
        self.assertAlmostEqual(tsw_comfort.score(rides), (4000 + 79 * 62) / 102)
        self.assertEqual(tsw_comfort.score(rides + [{"mark": 0, "passengers": 0}]), tsw_comfort.score(rides))
        self.assertEqual(tsw_comfort.score([dict(r, passengers=0) for r in rides]), 89.5)    # nobody aboard
        self.assertIsNone(tsw_comfort.score([]))

    def test_rides_named_by_the_station(self):
        judge = tsw_comfort.Judge()
        t = stop_at(judge, 0.8, 0.0, passengers=20)
        ride = judge.new_leg("2P27", "Frizinghall")
        self.assertEqual((ride["station"], ride["mark"], ride["passengers"]), ("Frizinghall", 97, 20))
        stop_at(judge, 0.5, t, passengers=60)
        done = judge.service_so_far("2P27", "Shipley")       # at the end, still there
        self.assertEqual([(r["station"], r["mark"]) for r in done["marks"]], [("Frizinghall", 97), ("Shipley", 100)])
        self.assertAlmostEqual(done["comfort"], (97 * 20 + 100 * 60) / 80)
        self.assertEqual(json.loads(json.dumps(done)), done)                     # kept in the history as it is


class BrakeTiming(unittest.TestCase):
    def test_air_brake(self):
        timed, ratio = brake_drive([(5.0, 0.33)])
        self.assertEqual(len(timed), 1)
        self.assertAlmostEqual(timed[0], 0.4 + math.log(4), delta=0.2)    # delay + 3/4 of the way: 1.79 s
        self.assertAlmostEqual(ratio, 0.625, delta=0.02)

    def test_notches_in_quick_succession_are_one_step(self):
        timed, _ = brake_drive([(5.0, 0.67), (5.1, 0.33)])
        self.assertEqual(len(timed), 1)

    def test_quick_brake(self):
        timed, _ = brake_drive([(5.0, 0.33)], lag=0.1, tau=0.25)
        self.assertAlmostEqual(timed[0], 0.1 + 0.25 * math.log(4), delta=0.15)

    def test_power_cancels(self):
        self.assertEqual(brake_drive([(5.0, 0.33)], power_at=6.0)[0], [])

    def test_another_step_cancels(self):
        self.assertEqual(brake_drive([(5.0, 0.67), (6.0, 0.33)])[0], [])

    def test_a_small_step_tells_nothing(self):
        self.assertEqual(brake_drive([(5.0, 0.9)])[0], [])

    def test_lead(self):
        judge = tsw_comfort.Judge()
        self.assertEqual(judge.lead(), tsw_comfort.REACTION + tsw_comfort.DEFAULT_EASE)
        judge.ease, judge.ratio = 1.9, 0.62
        self.assertAlmostEqual(judge.lead(), 0.62 + 1.9)
        judge.ease = 0.3
        self.assertEqual(judge.lead(), tsw_comfort.LEAD_MIN)


class FakeGame(BaseHTTPRequestHandler):
    """The game's subscription API, enough of it: values in `values`, subscriptions in `subs`."""
    protocol_version = "HTTP/1.1"
    values, subs, connections = {}, {}, set()

    def log_message(self, *args):
        pass

    def _answer(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        url = urllib.parse.urlparse(self.path)
        FakeGame.connections.add(self.client_address)
        return urllib.parse.unquote(url.path), int(urllib.parse.parse_qs(url.query)["Subscription"][0])

    def do_DELETE(self):
        _, n = self._path()
        FakeGame.subs.pop(n, None)
        self._answer({"SubscriptionID": n})

    def do_POST(self):
        path, n = self._path()
        FakeGame.subs.setdefault(n, []).append(path[len("/subscription/"):])
        self._answer({"SubscriptionID": n, "CurrentlyValid": True})

    def do_GET(self):
        _, n = self._path()
        if n not in FakeGame.subs:
            return self._answer({"Result": "Error"})
        self._answer({"Entries": [{"Path": p, "NodeValid": p in FakeGame.values, "Values": FakeGame.values.get(p, {})}
                                  for p in FakeGame.subs[n]]})


class Feed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGame)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        A = "CurrentDrivableActor."
        FakeGame.subs.clear()
        FakeGame.connections.clear()
        FakeGame.values.clear()
        FakeGame.values.update({A + "Function.HUD_GetSpeed": {"Speed (ms)": 12.5},
                                A + "Function.HUD_GetAcceleration": {"Acceleration (ms2)": -0.8},
                                A + "ObjectClass": {"ObjectClass": "RVM_TFW_Class150_DMS_C"}})
        self.feed = tsw_comfort.Feed(tsw_comfort.PATHS, address=self.address)

    def test_reads_all_in_one_request(self):
        got = self.feed.read("key")
        self.assertEqual(got["speed"], {"Speed (ms)": 12.5})
        self.assertEqual(got["acc"], {"Acceleration (ms2)": -0.8})
        self.assertIsNone(got["clock"])                        # nothing there: None
        self.assertEqual(len(FakeGame.subs[tsw_comfort.SUBSCRIPTION]), len(tsw_comfort.PATHS))

    def test_a_connection_per_request(self):
        for _ in range(3):
            self.feed.read("key")
        self.assertGreater(len(FakeGame.connections), 3)       # the game aborts connections kept open

    def test_subscribes_again_once_gone_without_doubling(self):
        self.feed.read("key")
        FakeGame.subs.clear()                                   # the game started again
        with self.assertRaises(IOError):
            self.feed.read("key")
        self.feed.read("key")
        self.assertEqual(len(FakeGame.subs[tsw_comfort.SUBSCRIPTION]), len(tsw_comfort.PATHS))

    def test_unsubscribe(self):
        self.feed.read("key")
        self.feed.unsubscribe()
        self.assertEqual(FakeGame.subs, {})


class FakeApi:
    """TSWApi for counting passengers: car i lists `cars[i]` (None: doesn't answer)."""
    key = "key"

    def __init__(self, cars, counts):
        self.cars, self.counts = cars, counts

    def load_key(self):
        return self.key

    def get_value(self, path):
        if path == "CurrentFormation.FormationLength":
            return len(self.cars)
        if path.endswith("GetPassengerCount"):
            return self.counts[int(path.split("/")[1])]
        return None

    def list(self, path):
        names = self.cars[int(path.rsplit("/", 1)[1])]
        return {"Nodes": [{"NodeName": n} for n in names or []]}


class Passengers(unittest.TestCase):
    def test_counted_on_cars_with_passenger_doors(self):
        watch = tsw_comfort.ComfortWatch()
        watch.api = FakeApi([["PassengerCargoModel", "PassengerDoor_FL"], ["Engine"],
                             ["PassengerCargoModel", "PassengerDoor_BR"]], [4, 99, 6])
        watch._count()
        self.assertEqual(watch.passengers, 10)                  # not the loco's 99

    def test_whatever_the_doors_are_called(self):
        # a Class 350's coach (Door_PassengerDoor_FR, seen live) and a DB N-Wagen's (Passenger_CargoModel)
        watch = tsw_comfort.ComfortWatch()
        watch.api = FakeApi([["PassengerCargoModel", "Door_PassengerDoor_FR", "PassengerDoorManager"],
                             ["Passenger_CargoModel", "RailVehicleDoor_FL"]], [0, 6])
        watch._count()
        self.assertEqual(watch.passengers, 6)
        self.assertEqual(watch.cars, ["CurrentFormation/0/PassengerCargoModel",
                                      "CurrentFormation/1/Passenger_CargoModel"])

    def test_a_car_that_didnt_answer_is_looked_at_again(self):
        watch = tsw_comfort.ComfortWatch()
        watch.api = FakeApi([["PassengerCargoModel", "PassengerDoor_FL"], None], [4, 6])
        watch._count()
        self.assertEqual(watch.passengers, 4)
        watch.api.cars[1] = ["PassengerCargoModel", "PassengerDoor_BR"]
        watch._count()
        self.assertEqual(watch.passengers, 10)


class BrakeFile(unittest.TestCase):
    def test_timings_kept_per_train(self):
        old = tsw_comfort.BRAKE_FILE
        tsw_comfort.BRAKE_FILE = os.path.join(tempfile.mkdtemp(), "brake_ease.json")
        try:
            watch = tsw_comfort.ComfortWatch()
            watch.train = "RVM_TFW_Class150_DMS_C"
            for seconds in (1.7, 2.1, 1.9):
                watch._say(("ease", seconds))
            self.assertEqual(tsw_comfort.load_eases(), {"RVM_TFW_Class150_DMS_C": {"ease": 1.9, "n": 3}})
            self.assertAlmostEqual(watch.judge.ease, 1.9)
        finally:
            tsw_comfort.BRAKE_FILE = old


if __name__ == "__main__":
    unittest.main()
