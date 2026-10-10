"""
How the ride feels to the passengers: in the last seconds of a hard stop, a warning to ease the brake off before
the train stops; how each stop went; a warning while braking or speeding up gets firm or harsh; and a report of the
ride from one station to the next.

What the game gives (seen on a Class 150, Cardiff valleys 1M56):
- CurrentDrivableActor.Function.HUD_GetAcceleration: the change of speed in m/s², in the game's own time. Working
  it out from the speed by the wall clock doesn't do: the game moves on at most 50 ms a frame, so below 20 fps it
  runs slower than the wall clock (by up to 40% on that drive). TimeOfDay's clock is the game's time.
- DriverAid.Data gradient: percent, negative downhill in the direction of travel. Along the car, passengers feel
  the change of speed plus g x the gradient: coasting downhill they feel nothing.
- PassengerCargoModel.Function.GetPassengerCount on each passenger car (a car with passenger doors).
- HUD_GetTrainBrakeHandle {"HandlePosition": 0..1} (steps of a third on the 150) and HUD_GetPowerHandle
  {"Power": notch, "IsNegative"}, for when the brake is eased.
All of these but the passengers come in one request a game tick, by an API subscription.

Stopping on full service, that train braked harder and harder as it slowed, 1.8 m/s² just before it stopped,
then nothing within a second: the jolt. Easing the brake off in the last few seconds avoids it. How long before
the stop the warning comes depends on how quickly the train's brake eases off: that's measured each time you step
the brake down while braking (no power, nothing else moved), kept per train (brake_ease.json), and 3 s until then.
Everything here only counts with passengers aboard.

    watch = ComfortWatch(); watch.start()
    watch.coach              # None, or what the comfort panel says:
                             #   {"phase": "ease", "seconds": to the stop (wall clock), "braking": m/s²}
                             #   {"phase": "stopped", "jolt": bool, "braking": m/s² as it stopped}
                             #   {"phase": "firm" / "harsh", "kind": "brake" / "power", "felt": m/s², "passengers"}
    watch.report(service)    # the ride since the last station, None with nobody aboard: {"stop": None / "smooth" /
                             #   "jolt" (the stop you're at), "braking": as it stopped, "harsh": [["brake" / "power",
                             #   worst m/s²]], "jolts": at stops on the way (signals), "passengers": aboard,
                             #   "smooth": rides this service without a jolt or a harsh moment, "rides": of them}
    watch.service(service)   # this service so far: {"rides", "smooth", "passengers": most aboard}
    watch.new_leg(service)   # on leaving a station: that ride is done
"""

import collections
import http.client
import json
import math
import os
import threading
import time
import urllib.parse

import tsw_joystick as core

G = 9.81
POLL_SECONDS = 0.02            # between polls; each waits for the game's next tick (~65 ms)
SUBSCRIPTION = 31              # the API subscription number used here
RESUBSCRIBE_SECONDS = 5.0      # with no train to read, how often to subscribe again
COUNT_SECONDS = 5.0            # how often the passengers are counted while stopped (they don't change on the move)
MAX_CARS = 16
STOPPED = 0.1                  # m/s: slower than this the train has stopped
MOVING = 2.0                   # m/s: it has to have gone faster than this since the last stop for a stop to count
LAST = 1.0                     # m/s: the braking slower than this is what passengers feel as the train stops
JOLT = 0.7                     # m/s²: braking harder than this as it stops is a jolt
EASE_ON = 0.6                  # m/s²: the warning comes braking harder than this, with less than the lead to go
EASE_OFF = 0.45                # ... and goes once eased off below this, or with a second more than the lead to go
REACTION = 1.0                 # s (wall clock) to see the warning and move the handle
DEFAULT_EASE = 3.0             # s (game) a brake takes to ease off, until measured on the train
LEAD_MIN, LEAD_MAX = 2.0, 6.0  # s (game): the lead is reaction + ease, kept within these
RESULT_SECONDS = 6.0           # how long how the stop went stays up
HARSH = 1.2                    # m/s² felt, braking or speeding up, for HARSH_SECONDS: a harsh moment
HARSH_OFF = 1.0                # ... over until below this
HARSH_SECONDS = 1.0
FIRM, FIRM_OFF = 0.9, 0.8      # m/s² felt: firm, the warning's amber
HOLD_SECONDS = 1.0             # the firm / harsh warning stays this long after
RATIO_SECONDS = 3.0            # game time against the wall clock over this long
BRAKE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brake_ease.json")

A = "CurrentDrivableActor."
PATHS = {"speed": A + "Function.HUD_GetSpeed", "acc": A + "Function.HUD_GetAcceleration",
         "brake": A + "Function.HUD_GetTrainBrakeHandle", "power": A + "Function.HUD_GetPowerHandle",
         "train": A + "ObjectClass", "aid": "DriverAid.Data", "clock": "TimeOfDay.Data"}


def _leg():
    return {"stop": None, "braking": None, "harsh": [], "jolts": 0, "passengers": 0}


def _smooth(leg):
    return leg["stop"] != "jolt" and not leg["harsh"] and not leg["jolts"]


class BrakeLearner:
    """How long the train's brake takes to ease off: from stepping the brake handle down while braking (no power,
    the handle left alone for a while before) until the braking has eased three quarters of the way to where it
    settles, in the game's time. A step made of several notches in quick succession counts as one."""
    MIN_BRAKING = 0.4          # m/s²
    MIN_SPEED = 3.0            # m/s: not in the last moments of a stop
    MIN_STEP = 0.05            # handle travel down that counts as stepping it down
    MOVED = 0.02               # ... and that counts as moving it at all
    MERGE = 0.5                # s: moves this soon after the first are part of the same step
    QUIET = 1.5                # s: the handle left alone this long before ...
    STEADY = 0.08              # ... and the braking changed less than this over SETTLE_SECONDS before
    SETTLE, SETTLE_SECONDS = 0.04, 0.6   # settled: braking changed less than this over this long
    MIN_DROP = 0.2             # m/s²: it has to ease off at least this much
    MAX_SECONDS = 10.0

    def __init__(self):
        self.last = None              # handle at the reading before
        self.moved = -math.inf        # when the handle last moved
        self.recent = collections.deque()   # (t, braking) over the last second
        self.watch = None             # a step being followed: {"t0", "trace": [(t, braking)], "peak"}

    def feed(self, t, braking, demand, power, speed):
        """`t`: game seconds; `demand`: brake handle (None: not known). The seconds measured, else None."""
        before, self.last = self.last, demand
        if t is None or demand is None or before is None:
            self.watch = None
            return None
        recent = self.recent
        if recent and t < recent[-1][0]:
            recent.clear()
        recent.append((t, braking))
        while t - recent[0][0] > 1.0:
            recent.popleft()
        then = next((d for tt, d in reversed(recent) if t - tt >= self.SETTLE_SECONDS), None)
        steady = then is not None and abs(braking - then) < self.STEADY
        moved = abs(demand - before) >= self.MOVED
        w = self.watch
        if w is not None:
            if power or speed < self.MIN_SPEED or t - w["t0"] > self.MAX_SECONDS or t < w["trace"][-1][0] \
                    or (moved and t - w["t0"] > self.MERGE):
                self.watch = w = None
            else:
                w["trace"].append((t, braking))
                w["peak"] = max(w["peak"], braking)
                eased = self._eased(w, t, braking)
                if eased is not None:
                    self.watch = None
                    return eased or None          # 0: it didn't ease off enough to tell
        if moved:
            quiet = t - self.moved >= self.QUIET
            self.moved = t
            if w is None and quiet and steady and demand <= before - self.MIN_STEP and braking >= self.MIN_BRAKING \
                    and not power and speed >= self.MIN_SPEED:
                self.watch = {"t0": t, "trace": [(t, braking)], "peak": braking}
        return None

    def _eased(self, w, t, braking):
        """Once the braking has settled: the seconds to three quarters eased, or 0 if it hardly eased."""
        if t - w["t0"] < 2 * self.SETTLE_SECONDS:
            return None
        then = next((d for tt, d in reversed(w["trace"]) if t - tt >= self.SETTLE_SECONDS), None)
        if then is None or abs(braking - then) >= self.SETTLE:
            return None
        drop = w["peak"] - braking
        if drop < self.MIN_DROP:
            return 0
        peak_at = next(tt for tt, d in w["trace"] if d == w["peak"])
        target = braking + 0.25 * drop
        return next(tt for tt, d in w["trace"] if tt >= peak_at and d <= target) - w["t0"]


class Judge:
    """Goes by the readings one at a time (so it can be tried on a logged drive)."""

    def __init__(self, ease=None):
        self.ease = ease              # s (game) the train's brake takes to ease off, None: not measured
        self.ratio = 1.0              # game seconds a wall clock second
        self.clocks = collections.deque()   # (wall, game) over the last RATIO_SECONDS
        self.accs = collections.deque(maxlen=3)
        self.coach = None             # the stop warning, or how the stop went
        self.coach_until = 0.0
        self.alert = None             # firm / harsh going on
        self.alert_until = 0.0
        self.moved = False            # faster than MOVING since the last stop
        self.approach = None          # hardest braking slower than LAST on the way into a stop
        self.braking = 0.0            # braking at the reading before
        self.harsh_since = None       # when the felt force went over HARSH
        self.harsh = None             # the harsh moment going on, once counted: [kind, worst]
        self.worst = 0.0
        self.learner = BrakeLearner()
        self.leg = _leg()
        self.service = None           # the service the rides below are of
        self.smooth = self.rides = 0  # rides done on it
        self.most = 0                 # most passengers aboard on it

    @property
    def panel(self):
        """What the comfort panel says: the stop warning or how the stop went, else firm / harsh."""
        return self.coach or self.alert

    def lead(self):
        """Seconds (game) before the stop the warning comes: time to react, and for the brake to ease off."""
        ease = self.ease if self.ease is not None else DEFAULT_EASE
        return min(max(REACTION * self.ratio + ease, LEAD_MIN), LEAD_MAX)

    def feed(self, now, speed, acc, gradient, passengers, clock=None, brake=None, power=False):
        """`now`: wall clock seconds; `speed`: m/s, signed; `acc`: the change of speed in m/s² (the game's sign);
        `gradient`: percent; `passengers`: aboard, None if not known; `clock`: game seconds; `brake`: the brake
        handle, None if not known; `power`: under power. What happened, for the log: [("stop", jolt, braking),
        ("harsh", kind, worst) once a harsh moment is over, ("ease", seconds) the brake was timed easing off]."""
        self.accs.append(acc)
        acc = sorted(self.accs)[len(self.accs) // 2]     # the middle of the last three: a slow frame can read
        v = abs(speed)                                   # one far out (-1.09 between -0.76 and -0.70)
        along = acc if speed >= 0 else -acc       # speeding up: positive
        braking = -along
        felt = along + G * gradient / 100
        aboard = bool(passengers)
        said = []
        self._time(now, clock)
        game = clock if clock is not None else now
        eased = self.learner.feed(game, braking, brake, power, v)
        if eased:
            said.append(("ease", eased))
        if self.coach and self.coach["phase"] == "stopped" and now > self.coach_until:
            self.coach = None
        if v > MOVING:
            self.moved = True
            if aboard:
                self.leg["passengers"] = passengers
                self.most = max(self.most, passengers)

        if v >= STOPPED:
            if v < LAST:
                self.approach = max(self.approach or 0.0, braking)
            else:
                self.approach = None                # faster again
            seconds = v / braking if braking > 0.05 else math.inf
            if self.coach and self.coach["phase"] == "ease":
                easing = braking > EASE_OFF and seconds < self.lead() + 1.0
            else:
                easing = braking > EASE_ON and seconds < self.lead()
            if aboard and self.moved and easing:
                self.coach = {"phase": "ease", "seconds": seconds / self.ratio, "braking": braking}
            elif self.coach and self.coach["phase"] == "ease":
                self.coach = None
            self.braking = braking
        elif self.moved:                            # it has just stopped
            hardest = self.approach if self.approach is not None else max(self.braking, 0.0)
            jolt = hardest > JOLT
            if aboard:
                self.coach = {"phase": "stopped", "jolt": jolt, "braking": hardest}
                self.coach_until = now + RESULT_SECONDS
                if self.leg["stop"] == "jolt":     # at a stop on the way (a signal, say)
                    self.leg["jolts"] += 1
                self.leg["stop"], self.leg["braking"] = ("jolt" if jolt else "smooth"), hardest
                said.append(("stop", jolt, hardest))
            else:
                self.coach = None
            self.moved, self.approach = False, None

        self._alert(now, v, felt, passengers if aboard else None)
        if aboard and v >= STOPPED and abs(felt) > (HARSH_OFF if self.harsh_since is not None else HARSH):
            if self.harsh_since is None:
                self.harsh_since, self.worst = now, 0.0
            self.worst = max(self.worst, abs(felt))
            if self.harsh is None and now - self.harsh_since >= HARSH_SECONDS:
                self.harsh = ["brake" if felt < 0 else "power", self.worst]
                self.leg["harsh"].append(self.harsh)
            elif self.harsh is not None:
                self.harsh[1] = self.worst
        else:
            if self.harsh is not None:
                said.append(("harsh", self.harsh[0], self.harsh[1]))
            self.harsh_since = self.harsh = None
        return said

    def _time(self, now, clock):
        """How fast the game's time goes against the wall clock (slower below 20 fps)."""
        if clock is None or (self.clocks and clock == self.clocks[-1][1]):
            return                                  # paused, or the same tick read again
        if self.clocks and (clock < self.clocks[-1][1] or now - self.clocks[-1][0] > 0.5):
            self.clocks.clear()                     # another game, or back from a pause
        self.clocks.append((now, clock))
        while now - self.clocks[0][0] > RATIO_SECONDS:
            self.clocks.popleft()
        wall, game = now - self.clocks[0][0], clock - self.clocks[0][1]
        if wall > 1.0 and game > 0:               # not while paused
            self.ratio = min(max(game / wall, 0.1), 1.0)

    def _alert(self, now, v, felt, passengers):
        """Firm (amber) or harsh (red), braking or speeding up, held for a moment after."""
        size, was = abs(felt), self.alert["phase"] if self.alert else None
        level = None
        if passengers and v >= STOPPED:
            if size > HARSH or (was == "harsh" and size > HARSH_OFF):
                level = "harsh"
            elif size > FIRM or (was is not None and size > FIRM_OFF):
                level = "firm"
        if level:
            self.alert = {"phase": level, "kind": "brake" if felt < 0 else "power", "felt": size,
                          "passengers": passengers}
            self.alert_until = now + HOLD_SECONDS
        elif self.alert and (now > self.alert_until or not passengers or v < STOPPED):
            self.alert = None

    def report(self, service):
        leg = self.leg
        if not leg["passengers"] or (leg["stop"] is None and not leg["harsh"] and not leg["jolts"]):
            return None
        smooth, rides = (self.smooth, self.rides) if service == self.service else (0, 0)
        if leg["stop"] is not None:
            smooth, rides = smooth + _smooth(leg), rides + 1
        return dict(leg, harsh=[list(h) for h in leg["harsh"]], smooth=smooth, rides=rides)

    def service_so_far(self, service):
        """{"rides", "smooth", "passengers": most aboard} on a service, with the ride now if it has ended at a stop."""
        leg = self.leg
        if service != self.service:
            done = {"rides": 0, "smooth": 0, "passengers": leg["passengers"]}
        else:
            done = {"rides": self.rides, "smooth": self.smooth, "passengers": max(self.most, leg["passengers"])}
        if leg["passengers"] and leg["stop"] is not None:
            done["rides"] += 1
            done["smooth"] += _smooth(leg)
        return done

    def new_leg(self, service):
        leg = self.leg
        if service != self.service:
            self.service, self.smooth, self.rides, self.most = service, 0, 0, leg["passengers"]
        if leg["passengers"] and leg["stop"] is not None:
            self.smooth += _smooth(leg)
            self.rides += 1
        self.leg = _leg()
        self.harsh = None                 # a harsh moment going on carries on into the next ride uncounted


class Feed:
    """Readings by an API subscription: all of them in one request, answered at the game's next tick, over one
    connection kept open. read() gives {name: values}, None where the game has nothing (not in a cab)."""

    def __init__(self, paths, number=SUBSCRIPTION, address=None):
        self.paths, self.number = dict(paths), number
        url = urllib.parse.urlparse(address or core.API_URL)
        self.host, self.port = url.hostname, url.port
        self.conn = None
        self.key = None
        self.subscribed = False

    def _req(self, method, path):
        url = "/" + urllib.parse.quote(path, safe="/().") + "?" + urllib.parse.urlencode({"Subscription": self.number})
        for attempt in range(2):
            if self.conn is None:
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
            try:
                self.conn.request(method, url, headers={"DTGCommKey": self.key or ""})
                r = self.conn.getresponse()
                body = r.read()
                break
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt:                         # the game closed the connection, say: try a new one once
                    raise
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.conn, self.subscribed = None, False

    def subscribe(self):
        self._req("DELETE", "subscription")         # one left over from before
        for path in self.paths.values():
            self._req("POST", "subscription/" + path)
        self.subscribed = True

    def read(self, key):
        self.key = key
        if not self.subscribed:
            self.subscribe()
        entries = self._req("GET", "subscription").get("Entries")
        if entries is None:                         # gone (the game started again, say)
            self.subscribed = False
            raise IOError("subscription gone")
        got = {e.get("Path"): e.get("Values") if e.get("NodeValid") else None for e in entries}
        return {name: got.get(path) for name, path in self.paths.items()}

    def unsubscribe(self):
        try:
            if self.conn is not None:
                self._req("DELETE", "subscription")
        except (OSError, http.client.HTTPException):
            pass
        self.close()


def _first(values):
    for v in (values or {}).values():
        return v
    return None


def load_eases():
    try:
        with open(BRAKE_FILE, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if isinstance(v, dict) and "ease" in v}
    except (OSError, ValueError, AttributeError):
        return {}


class ComfortWatch(threading.Thread):
    """Reads the train's motion and passengers on a thread of its own and judges the ride."""

    def __init__(self, log=None):
        super().__init__(daemon=True)
        self.api = core.TSWApi()
        self.feed = Feed(PATHS)
        self.log = log or (lambda msg: None)
        self.running = True
        self.judge = Judge()
        self.lock = threading.Lock()
        self.eases = load_eases()     # train -> {"ease": s, "n": measured}
        self.train = None
        self.cars = None              # API paths of the passenger cars' PassengerCargoModel
        self.formation = None         # number of vehicles when they were looked at
        self.passengers = None
        self.counted = 0.0
        self.subscribed_at = 0.0

    @property
    def coach(self):
        return self.judge.panel

    def report(self, service):
        with self.lock:
            return self.judge.report(service)

    def service(self, service):
        with self.lock:
            return self.judge.service_so_far(service)

    def new_leg(self, service):
        with self.lock:
            self.judge.new_leg(service)

    def run(self):
        while self.running:
            try:
                self.step()
            except Exception:             # game not running, or a dropped connection
                self._gone()
                self.feed.close()
                time.sleep(1)
            time.sleep(POLL_SECONDS)
        self.feed.unsubscribe()

    def _gone(self):
        self.judge.coach = self.judge.alert = None
        self.cars = self.formation = self.passengers = None

    def step(self):
        api = self.api
        if not api.key and not api.load_key():
            self._gone()
            time.sleep(2)
            return
        if not self.feed.subscribed:
            self.subscribed_at = time.monotonic()
        got = self.feed.read(api.key)
        speed, acc = _first(got["speed"]), _first(got["acc"])
        now = time.monotonic()
        if speed is None or acc is None:  # not in a cab
            self._gone()
            if now - self.subscribed_at > RESUBSCRIBE_SECONDS:
                self.feed.subscribed = False        # in case it was tied to a train that's gone
            time.sleep(0.5)
            return
        train = _first(got["train"])
        if train != self.train:
            self.train = train
            self.judge.ease = (self.eases.get(train) or {}).get("ease")
        clock = (got["clock"] or {}).get("LocalTime")
        gradient = (got["aid"] or {}).get("gradient") or 0.0
        brake, power = got["brake"] or {}, got["power"] or {}
        demand = brake.get("HandlePosition") if brake.get("IsActive") else None
        if power.get("IsNegative"):                 # a power / brake handle on the brake side
            demand = max(demand or 0.0, abs(power.get("Power") or 0.0))
        power_on = bool(power.get("IsActive")) and not power.get("IsNegative") and (power.get("Power") or 0) > 0
        if self.cars is None or (abs(speed) < STOPPED and now - self.counted > COUNT_SECONDS):
            self._count()
        with self.lock:
            said = self.judge.feed(now, speed, acc, gradient, self.passengers, clock / 1e7 if clock else None,
                                   demand, power_on)
        for event in said:
            self._say(event)

    def _say(self, event):
        if event[0] == "stop":
            self.log(f"Comfort: {'jolt on stopping' if event[1] else 'smooth stop'}, braking {event[2]:.1f} m/s² as "
                     f"it stopped, {self.passengers} passengers")
        elif event[0] == "harsh":
            self.log(f"Comfort: harsh {'braking' if event[1] == 'brake' else 'acceleration'}, {event[2]:.1f} m/s²")
        elif event[0] == "ease" and self.train:
            old = self.eases.get(self.train) or {"ease": event[1], "n": 0}
            n = old["n"] + 1
            ease = old["ease"] + (event[1] - old["ease"]) / min(n, 5)    # the last few count most
            self.eases[self.train] = {"ease": round(ease, 2), "n": n}
            with self.lock:
                self.judge.ease = ease
                lead = self.judge.lead() / self.judge.ratio
            try:
                with open(BRAKE_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.eases, f, indent=2)
            except OSError:
                pass
            self.log(f"Comfort: the brake eased off in {event[1]:.1f} s (on this train {ease:.1f} s, timed {n}x); "
                     f"the stop warning comes {lead:.1f} s before")

    def _count(self):
        """The passengers aboard: the cars with passenger doors are looked for when the train changes."""
        api = self.api
        self.counted = time.monotonic()
        n = int(api.get_value("CurrentFormation.FormationLength") or 0)
        if self.cars is None or n != self.formation:
            self.formation, self.cars = n, []
            for i in range(min(n, MAX_CARS)):
                names = core.node_names(api.list(f"CurrentFormation/{i}"))
                if "PassengerCargoModel" in names and any(name.startswith("PassengerDoor_") for name in names):
                    self.cars.append(f"CurrentFormation/{i}/PassengerCargoModel")
        counts = [api.get_value(car + ".Function.GetPassengerCount") for car in self.cars]
        counts = [c for c in counts if isinstance(c, (int, float))]
        self.passengers = int(sum(counts)) if counts else None
