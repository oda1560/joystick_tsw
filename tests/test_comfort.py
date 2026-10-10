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
            said += [(t, e) for e in judge.feed(t, v, float(r["acc"]), float(r["gradient"]), passengers, None,
                                                float(r["brake"]), float(r["power"]) > 0)]
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
        self.assertEqual([k for k, _ in r["harsh"]], ["brake"])
        self.assertEqual((r["passengers"], r["smooth"], r["rides"], r["jolts"]), (15, 0, 1, 0))

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
        judge.feed(t, v, -d, 0.0, passengers)
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
                judge.feed(t, v, -decel if v else 0.0, 0.0, 30)
                t += 0.07
            judge.new_leg("2P27")
        self.assertEqual(judge.service_so_far("2P27"), {"rides": 3, "smooth": 2, "passengers": 30})


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
        timed += [e[1] for e in judge.feed(wall, v, -force, 0.0, 30, 6e4 + t, demand, power) if e[0] == "ease"]
        t, wall = t + game_dt, wall + wall_dt
    return timed, judge.ratio


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
                                A + "ObjectClass": {"ObjectClass": "RVM_TFW_Class150_DMS_C"},
                                "DriverAid.Data": {"gradient": -0.25}})
        self.feed = tsw_comfort.Feed(tsw_comfort.PATHS, address=self.address)

    def test_reads_all_in_one_request(self):
        got = self.feed.read("key")
        self.assertEqual(got["speed"], {"Speed (ms)": 12.5})
        self.assertEqual(got["aid"]["gradient"], -0.25)
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
        watch.api = FakeApi([["PassengerCargoModel", "PassengerDoor_FL"], ["PassengerCargoModel", "Engine"],
                             ["PassengerCargoModel", "PassengerDoor_BR"]], [4, 99, 6])
        watch._count()
        self.assertEqual(watch.passengers, 10)                  # not the loco's 99

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
