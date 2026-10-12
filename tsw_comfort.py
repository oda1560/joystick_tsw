"""
How the ride feels to the passengers: in the last seconds of a hard stop, a warning to ease the brake off before
the train stops; how each stop went; a warning while braking or speeding up gets firm or harsh; and a report of the
ride from one station to the next.

What the game gives (seen on a Class 150, Cardiff valleys 1M56):
- CurrentDrivableActor.Function.HUD_GetAcceleration: the change of speed in m/s², in the game's own time. Working
  it out from the speed by the wall clock doesn't do: the game moves on at most 50 ms a frame, so below 20 fps it
  runs slower than the wall clock (by up to 40% on that drive). TimeOfDay's clock is the game's time.
- PassengerCargoModel.Function.GetPassengerCount on each passenger car (Passenger_CargoModel on DB N-Wagen coaches).
- HUD_GetTrainBrakeHandle {"HandlePosition": 0..1, "IsActive"} (steps of a third on the 150; on the Class 331's
  power / brake handle the brake side, smoothly) and HUD_GetPowerHandle {"Power", "IsNegative"} (notches on the
  150; on the 331 percent of the effort, negative while braking), for when the brake is eased. Both read
  IsActive false until the cab is in use.
- CurrentDrivableActor/Simulation/<node>.BrakeHoldActive: the train's own holding brake is on (HOLD_NODES).
All of these but the passengers come in one request a game tick, by an API subscription.

What's judged is how hard the train speeds up and slows down, not the slope: on a hill passengers lean a little all
the time, which the driver can't help (the Bernina line's 7% would add 0.69 m/s² holding speed downhill). The limits
come from real trains: a modern train's full service brake is 0.9 m/s² and its emergency brake about 1.2, and
standing passengers start having to step at about 1.0; speeding up, a Class 331 does 1.3 on full power and up to
about 1.5 is found acceptable.

Stopping on full service, that train braked harder and harder as it slowed, 1.8 m/s² just before it stopped,
then nothing within a second: the jolt. Easing the brake off in the last few seconds avoids it. How long before
the stop the warning comes depends on how quickly the train's brake eases off: that's measured each time you step
the brake down while braking (no power, nothing else moved), kept per train (brake_ease.json), and 3 s until then.
Some trains brake by themselves in the last moments of a stop: a Class 350's holding brake comes on at 0.62 m/s
with no power, and braked it at 0.9-1.0 m/s² with the handle off (seen live, 2W09), so every stop read as a jolt
however well it was driven. In the game files the 331, 333, 350 and 380 have one, and the 710 and DB 423 something
like it. From when it comes on, braking harder than just before isn't counted, unless the handle goes on harder.
All this on passenger trains (a car has a passenger model), empty ones too: a 331 on 2V04 had nobody aboard, and
showed nothing when it was only for trains with passengers. Freight and light engines are left out: in the game files
no locomotive or wagon has a passenger model. It used to take a car with nodes named PassengerDoor_* as well, which
left out a third of the passenger vehicles (the Class 350's are Door_PassengerDoor_*) and showed nothing on them.

Each ride from one station to the next gets a mark out of 100: it loses a point a second braking or speeding up
over the firm limit, 10 for a harsh moment and 2 a second it goes on past the first, and for each stop with a jolt
30 an m/s² over the jolt limit (1.4 m/s² as it stopped: 21). The service's comfort is the rides' marks weighted by
the passengers aboard: how the ride felt to the average passenger. The weights are a starting point, not taken from
anywhere.

    watch = ComfortWatch(); watch.start()
    watch.coach              # None, or what the comfort panel says:
                             #   {"phase": "ease", "seconds": to the stop (wall clock), "braking": m/s²}
                             #   {"phase": "stopped", "jolt": bool, "braking": m/s² as it stopped, "cost": points,
                             #    "mark": this ride's, "score": the service's}
                             #   {"phase": "firm" / "harsh", "kind": "brake" / "power", "felt": m/s², "passengers"}
                             #   {"phase": "lost", "level": "firm" / "harsh", "kind", "cost", "mark", "score",
                             #    "passengers"}: for a moment after a firm or harsh stretch that cost points
    watch.report(service)    # the ride since the last station, None off passenger trains, at a service's first
                             #   stop (no ride to it) and if it was on another service: {"stop": None / "smooth" /
                             #   "jolt" (the stop you're at), "braking": as it stopped, "harsh": [["brake" / "power",
                             #   worst m/s², seconds]], "firm": {"brake" / "power": seconds}, "way": braking at each
                             #   stop with a jolt on the way (signals), "passengers": most aboard, "mark": out of 100,
                             #   "lost": what cost it (losses()), "score": the service's comfort with this ride,
                             #   "smooth": rides this service without a jolt or a harsh moment, "rides": of them}
    watch.service(service, station)   # this service so far, the ride to `station` too if it's over: {"rides",
                             #   "smooth", "passengers": most aboard, "comfort": score, "marks": [ride (_ride)]}
    watch.new_leg(service, station, first)   # on leaving a station: the ride to it is done; leaving the service's
                             #   first stop (first=True) starts the service afresh, whatever came before it
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

POLL_SECONDS = 1 / 15          # a poll this often at most (each waits for the game's next tick, 30-65 ms);
                               # each is a connection of its own, so not every tick
SUBSCRIPTION = 31              # the API subscription number used here
RESUBSCRIBE_SECONDS = 5.0      # with no train to read, how often to subscribe again
COUNT_SECONDS = 5.0            # how often the passengers are counted while stopped (they don't change on the move)
MAX_CARS = 16
CARGO_MODELS = ("PassengerCargoModel", "Passenger_CargoModel")   # a passenger car's node for its passengers
STOPPED = 0.1                 # m/s: slower than this the train has stopped
MOVING = 2.0                   # m/s: it has to have gone faster than this since the last stop for a stop to count
LAST = 1.0                     # m/s: the braking slower than this is what passengers feel as the train stops
JOLT = 0.7                     # m/s²: braking harder than this as it stops is a jolt
EASE_ON = 0.6                  # m/s²: the warning comes braking harder than this, with less than the lead to go
EASE_OFF = 0.45                # ... and goes once eased off below this, or with a second more than the lead to go
REACTION = 1.0                 # s (wall clock) to see the warning and move the handle
DEFAULT_EASE = 3.0             # s (game) a brake takes to ease off, until measured on the train
LEAD_MIN, LEAD_MAX = 2.0, 6.0  # s (game): the lead is reaction + ease, kept within these
RESULT_SECONDS = 6.0           # how long how the stop went stays up
# m/s² braking / speeding up, (over this, until below this): firm is the warning's amber; harsh its red, and a
# harsh moment of the ride once it lasts HARSH_SECONDS
LIMITS = {"brake": {"firm": (0.9, 0.8), "harsh": (1.2, 1.0)},
          "power": {"firm": (1.3, 1.2), "harsh": (1.5, 1.3)}}
HARSH_SECONDS = 1.0
HOLD_SECONDS = 1.0             # the firm / harsh warning stays this long after
FIRM_COST = 1.0                # a ride's mark loses this a second (game) over the firm limit,
HARSH_COST, HARSH_EXTRA = 10.0, 2.0   # this a harsh moment and this a second it lasts past HARSH_SECONDS,
STOP_COST, STOP_MOST = 30.0, 30.0     # and this an m/s² braking over JOLT as it stops, at most STOP_MOST a stop
LOSS_SECONDS = 4.0             # what a firm / harsh stretch cost stays up this long after it
MAX_STEP = 0.5                 # s (game): a longer gap between readings counts as this long
RATIO_SECONDS = 3.0            # game time against the wall clock over this long
BRAKE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brake_ease.json")

HELD_HARDER = 0.05             # the brake handle this much further on after the train's own holding brake came on:
                               # what it brakes counts again
# the simulation nodes that apply a train's own holding brake: these four names (LevellingValve, the fifth of the
# kind, is air suspension) are all the game files have, and a train has only some of them
HOLD_NODES = ("DirectBrakeControlPowered", "DirectBrakeControlUnpowered", "DirectBrakeControl", "EP_BrakeControl")

A = "CurrentDrivableActor."
PATHS = {"speed": A + "Function.HUD_GetSpeed", "acc": A + "Function.HUD_GetAcceleration",
         "brake": A + "Function.HUD_GetTrainBrakeHandle", "power": A + "Function.HUD_GetPowerHandle",
         "train": A + "ObjectClass", "clock": "TimeOfDay.Data",
         **{"hold " + node: f"CurrentDrivableActor/Simulation/{node}.BrakeHoldActive" for node in HOLD_NODES}}


def _leg(service=None):
    """A ride from a station on `service` (None: the bridge started on the way, so it isn't known)."""
    return {"stop": None, "braking": None, "harsh": [], "firm": {"brake": 0.0, "power": 0.0}, "way": [],
            "passengers": 0, "judged": False, "service": service}


def _smooth(leg):
    return leg["stop"] != "jolt" and not leg["harsh"] and not leg["way"]


def _stop_cost(braking):
    return min(max(braking - JOLT, 0.0) * STOP_COST, STOP_MOST)


def losses(leg):
    """What a ride lost of its 100, costliest first, each [points, what, ...]: [p, "stop", braking] stopping at the
    station with a jolt, [p, "way", braking] a jolt at a stop on the way, [p, "harsh", kind, worst m/s², seconds],
    [p, "firm", kind, seconds]."""
    lost = []
    if leg["stop"] == "jolt":
        lost.append([_stop_cost(leg["braking"]), "stop", leg["braking"]])
    lost += [[_stop_cost(braking), "way", braking] for braking in leg["way"]]
    lost += [[HARSH_COST + HARSH_EXTRA * max(seconds - HARSH_SECONDS, 0.0), "harsh", kind, worst, seconds]
             for kind, worst, seconds in leg["harsh"]]
    lost += [[FIRM_COST * seconds, "firm", kind, seconds] for kind, seconds in leg["firm"].items() if seconds > 0]
    return sorted(lost, key=lambda loss: -loss[0])


def mark(leg):
    """A ride's comfort out of 100."""
    return max(0.0, 100.0 - sum(loss[0] for loss in losses(leg)))


def score(rides):
    """A service's comfort from its rides' marks, each weighted by the passengers aboard: how the ride felt to the
    average passenger. With nobody aboard all the way the rides count alike. None with no rides."""
    if not rides:
        return None
    aboard = sum(r["passengers"] for r in rides)
    if aboard:
        return sum(r["mark"] * r["passengers"] for r in rides) / aboard
    return sum(r["mark"] for r in rides) / len(rides)


def _ride(leg, station=None):
    """A ride as it's kept: {"station", "mark", "passengers", "smooth", "lost": losses()}."""
    lost = [[round(x, 2) if isinstance(x, float) else x for x in loss] for loss in losses(leg)]
    return {"station": station, "mark": round(mark(leg), 1), "passengers": leg["passengers"], "smooth": _smooth(leg),
            "lost": lost}


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
        self.hold = False             # the train's own holding brake on at the reading before
        self.held = None              # since it came on as the train stopped: {"cap": the most braking counted,
                                      #   "demand": brake handle then}
        self.harsh_since = None       # when braking or speeding up went over harsh
        self.harsh_kind = None        # ... which of them
        self.harsh_time = 0.0         # ... and for how long since (game)
        self.harsh = None             # the harsh moment going on, once counted: [kind, worst, seconds]
        self.worst = 0.0
        self.last_time = None         # (game clock?, seconds) at the reading before
        self.episode = None           # a firm / harsh stretch going on: {"cost", "kind", "level"}
        self.loss = None              # what the last one cost, for a moment after
        self.loss_until = 0.0
        self.learner = BrakeLearner()
        self.leg = _leg()
        self.service = None           # the service the rides below are of
        self.done = []                # rides done on it (_ride)
        self.most = 0                 # most passengers aboard on it

    @property
    def panel(self):
        """What the comfort panel says: the stop warning or how the stop went, else firm / harsh, else what that
        cost."""
        return self.coach or self.alert or self.loss

    def lead(self):
        """Seconds (game) before the stop the warning comes: time to react, and for the brake to ease off."""
        ease = self.ease if self.ease is not None else DEFAULT_EASE
        return min(max(REACTION * self.ratio + ease, LEAD_MIN), LEAD_MAX)

    def feed(self, now, speed, acc, passengers, clock=None, brake=None, power=False, hold=None):
        """`now`: wall clock seconds; `speed`: m/s, signed; `acc`: the change of speed in m/s² (the game's sign);
        `passengers`: aboard (0 on an empty passenger train), None if not a passenger train or not known; `clock`:
        game seconds; `brake`: the brake handle, None if not known; `power`: under power; `hold`: the train's own
        holding brake is on, None if it hasn't one. What happened, for the log: [("stop", jolt, braking),
        ("harsh", kind, worst) once a harsh moment is over, ("ease", seconds) the brake was timed easing off,
        ("hold", speed, braking) the train's holding brake came on as it stopped, braking this much just before]."""
        self.accs.append(acc)
        acc = sorted(self.accs)[len(self.accs) // 2]     # the middle of the last three: a slow frame can read
        v = abs(speed)                                   # one far out (-1.09 between -0.76 and -0.70)
        along = acc if speed >= 0 else -acc       # speeding up: positive
        braking = -along
        kind = "brake" if along < 0 else "power"
        judged = passengers is not None          # a passenger train
        said = []
        self._time(now, clock)
        game = clock if clock is not None else now
        step = self._step(now, clock)
        eased = self.learner.feed(game, braking, brake, power, v)
        if eased:
            said.append(("ease", eased))
        braking = self._held(v, braking, brake, hold, said)
        along = -braking
        if self.coach and self.coach["phase"] == "stopped" and now > self.coach_until:
            self.coach = None
        if self.loss and now > self.loss_until:
            self.loss = None
        if v > MOVING:
            self.moved = True
            if judged:
                self.leg["judged"] = True
                self.leg["passengers"] = max(self.leg["passengers"], passengers)
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
            if judged and self.moved and easing:
                self.coach = {"phase": "ease", "seconds": seconds / self.ratio, "braking": braking}
            elif self.coach and self.coach["phase"] == "ease":
                self.coach = None
            self.braking = braking
        elif self.moved:                            # it has just stopped
            hardest = self.approach if self.approach is not None else max(self.braking, 0.0)
            jolt = hardest > JOLT
            if judged:
                if self.leg["stop"] == "jolt":     # at a stop on the way (a signal, say)
                    self.leg["way"].append(self.leg["braking"])
                self.leg["stop"], self.leg["braking"] = ("jolt" if jolt else "smooth"), hardest
                self.coach = {"phase": "stopped", "jolt": jolt, "braking": hardest,
                              "cost": _stop_cost(hardest) if jolt else 0.0, "mark": mark(self.leg),
                              "score": self.live_score()}
                self.coach_until = now + RESULT_SECONDS
                said.append(("stop", jolt, hardest))
            else:
                self.coach = None
            self.moved, self.approach = False, None

        spent = self._spent()
        level = self._alert(now, v, along, kind, passengers)
        harsh, harsh_off = LIMITS[kind]["harsh"]
        if judged and v >= STOPPED and abs(along) > (harsh_off if self.harsh_since is not None else harsh):
            if self.harsh_since is None:
                self.harsh_since, self.harsh_kind, self.harsh_time, self.worst = now, kind, 0.0, 0.0
            self.worst = max(self.worst, abs(along))
            self.harsh_time += step
            if self.harsh is None and now - self.harsh_since >= HARSH_SECONDS:
                self.harsh = [kind, self.worst, self.harsh_time]
                self.leg["harsh"].append(self.harsh)
            elif self.harsh is not None:
                self.harsh[1:] = [self.worst, self.harsh_time]
        else:
            if self.harsh is not None:
                said.append(("harsh", self.harsh[0], self.harsh[1]))
            elif self.harsh_since is not None:       # over harsh too briefly to be a harsh moment: firm
                self.leg["firm"][self.harsh_kind] += self.harsh_time
            self.harsh_since = self.harsh = None
            if judged and level and v >= STOPPED:
                self.leg["firm"][kind] += step
        self._episode(now, v, kind, level, passengers, self._spent() - spent)
        return said

    def _held(self, v, braking, demand, hold, said):
        """The braking that's down to the driver. Some trains put their brakes on by themselves in the last moments
        of a stop and hold them there: a Class 350's holding brake comes on at 0.62 m/s with no power and fills the
        brake cylinders to 1.5 bar whatever the handle says (seen live, 2W09), braking 0.9-1.0 m/s² with the
        handle off. From when it comes on, no more braking counts than just before, unless the brake handle is put
        on harder."""
        if hold and not self.hold and self.moved and v >= STOPPED:
            cap = max(min(self.braking, braking), 0.0)
            self.held = {"cap": cap, "demand": demand}
            said.append(("hold", v, cap))
        elif not hold:
            self.held = None
        self.hold = bool(hold)
        held = self.held
        if held is not None:
            if demand is not None and held["demand"] is not None and demand > held["demand"] + HELD_HARDER:
                self.held = None                    # braking harder: that's the driver's
            else:
                braking = min(braking, held["cap"])
        return braking

    def lost(self):
        """Out of the cab, or into another train: how the last one was moving is forgotten. Getting into an 807
        standing still after leaving a 331 on the move read as the 331 stopping with a jolt."""
        self.accs.clear()
        self.hold, self.held = False, None
        self.coach = self.alert = self.approach = self.harsh_since = self.harsh = None
        self.episode = self.loss = self.last_time = None
        self.moved, self.braking = False, 0.0
        self.learner = BrakeLearner()

    def _step(self, now, clock):
        """Game seconds since the reading before (by the wall clock when the game's isn't read), at most MAX_STEP."""
        t = (clock is not None, clock if clock is not None else now)
        last, self.last_time = self.last_time, t
        if last is None or last[0] != t[0]:
            return 0.0
        return min(max(t[1] - last[1], 0.0), MAX_STEP)

    def _spent(self):
        """Points the ride has lost to firm and harsh driving so far."""
        return sum(loss[0] for loss in losses(self.leg) if loss[1] in ("firm", "harsh"))

    def live_score(self):
        """The service's comfort now, with the ride going on."""
        return score(self.done + ([_ride(self.leg)] if self.leg["judged"] else []))

    def _episode(self, now, v, kind, level, passengers, cost):
        """What a stretch of firm / harsh driving cost: shown for a moment once its warning has gone, unless the
        train has stopped (how the stop went says it then)."""
        if level:
            self.episode = self.episode or {"cost": 0.0, "kind": kind, "level": "firm"}
            if self.harsh is not None:
                self.episode.update(kind=self.harsh[0], level="harsh")
        if level or self.coach:
            self.loss = None
        if self.episode is None:
            return
        self.episode["cost"] += cost
        if self.alert is None:
            episode, self.episode = self.episode, None
            if passengers is not None and v >= STOPPED and self.coach is None and episode["cost"] >= 0.5:
                self.loss = dict(episode, phase="lost", mark=mark(self.leg), score=self.live_score(),
                                 passengers=passengers)
                self.loss_until = now + LOSS_SECONDS

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

    def _alert(self, now, v, along, kind, passengers):
        """Firm (amber) or harsh (red), braking or speeding up, held for a moment after. The level now, or None."""
        size = abs(along)
        was = self.alert["phase"] if self.alert and self.alert["kind"] == kind else None
        (firm, firm_off), (harsh, harsh_off) = LIMITS[kind]["firm"], LIMITS[kind]["harsh"]
        level = None
        if passengers is not None and v >= STOPPED:
            if size > harsh or (was == "harsh" and size > harsh_off):
                level = "harsh"
            elif size > firm or (was is not None and size > firm_off):
                level = "firm"
        if level:
            self.alert = {"phase": level, "kind": kind, "felt": size, "passengers": passengers}
            self.alert_until = now + HOLD_SECONDS
        elif self.alert and (now > self.alert_until or passengers is None or v < STOPPED):
            self.alert = None
        return level

    def _ours(self, service):
        """The ride going on is one on `service`. A ride on another doesn't carry into it: a Class 484 stopping at
        a signal on 2U02 with a jolt, then 2W18_1 started on a 350, showed that jolt at 2W18_1's first stop and
        kept it as the ride there (seen live)."""
        return self.leg["service"] in (None, service)

    def report(self, service, first=False):
        """The ride to the stop you're at on `service`; `first`: that's the service's first stop, there's no ride
        to it."""
        leg = self.leg
        lost = losses(leg)
        if first or not leg["judged"] or not self._ours(service) or (leg["stop"] is None and not lost):
            return None
        done = self.done if service == self.service else []
        ended = done + ([_ride(leg)] if leg["stop"] is not None else [])
        return dict(leg, harsh=[list(h) for h in leg["harsh"]], firm=dict(leg["firm"]), way=list(leg["way"]),
                    mark=mark(leg), lost=lost, score=score(done + [_ride(leg)]),
                    smooth=sum(r["smooth"] for r in ended), rides=len(ended))

    def service_so_far(self, service, station=None):
        """{"rides", "smooth", "passengers": most aboard, "comfort": score, "marks": the rides} on a service, with the
        ride now (to `station`) if it has ended at a stop."""
        leg = self.leg
        same = service == self.service
        ours = self._ours(service)
        done = list(self.done) if same else []
        if ours and leg["judged"] and leg["stop"] is not None:
            done.append(_ride(leg, station))
        aboard = leg["passengers"] if ours else 0
        return {"rides": len(done), "smooth": sum(r["smooth"] for r in done),
                "passengers": max(self.most, aboard) if same else aboard, "comfort": score(done), "marks": done}

    def new_leg(self, service, station=None, first=False):
        """Leaving `station`: the ride to it is done. That ride as kept, or None if it wasn't judged. `first`: the
        service's first stop: the service starts here (again, if it was started over), and how the train went
        before isn't a ride of it."""
        leg, ride = self.leg, None
        ours = not first and self._ours(service) and leg["judged"] and leg["stop"] is not None
        if first or service != self.service:
            self.service, self.done, self.most = service, [], leg["passengers"] if ours else 0
        if ours:
            ride = _ride(leg, station)
            self.done.append(ride)
        self.leg = _leg(service)
        self.harsh = None                 # a harsh moment going on carries on into the next ride uncounted ...
        self.harsh_time = 0.0             # ... and what's left of it is firm there
        return ride


class Feed:
    """Readings by an API subscription: all of them in one request, answered at the game's next tick. read()
    gives {name: values}, None where the game has nothing (not in a cab). Each request has a connection of its
    own: the game aborts connections kept open every second or so (seen live), and here that made the
    subscription start over each time, so the readings hardly ever came."""

    def __init__(self, paths, number=SUBSCRIPTION, address=None):
        self.paths, self.number = dict(paths), number
        url = urllib.parse.urlparse(address or core.API_URL)
        self.host, self.port = url.hostname, url.port
        self.key = None
        self.subscribed = False

    def _req(self, method, path):
        url = "/" + urllib.parse.quote(path, safe="/().") + "?" + urllib.parse.urlencode({"Subscription": self.number})
        for attempt in range(2):
            conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
            try:
                conn.request(method, url, headers={"DTGCommKey": self.key or "", "Connection": "close"})
                body = conn.getresponse().read()
                break
            except (OSError, http.client.HTTPException):
                if attempt:                         # the game dropped the connection, say: try once more
                    raise
            finally:
                conn.close()
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def close(self):
        """Subscribe again before the next read (the game may have been started again)."""
        self.subscribed = False

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
        if self.subscribed:
            try:
                self._req("DELETE", "subscription")
            except (OSError, http.client.HTTPException):
                pass
        self.subscribed = False


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

    def report(self, service, first=False):
        with self.lock:
            return self.judge.report(service, first)

    def service(self, service, station=None):
        with self.lock:
            return self.judge.service_so_far(service, station)

    def new_leg(self, service, station=None, first=False):
        with self.lock:
            ride = self.judge.new_leg(service, station, first)
            done = len(self.judge.done)
            total = score(self.judge.done)
        if ride:
            why = ", ".join(f"{loss[1]} -{loss[0]:.0f}" for loss in ride["lost"] if loss[0] >= 0.5)
            self.log(f"Comfort: the ride to {station or 'the stop'} marked {ride['mark']:.0f}"
                     f"{f' ({why})' if why else ''}, {ride['passengers']} passengers; "
                     f"{service} so far {total:.0f} over {done} ride{'s' * (done != 1)}")

    def run(self):
        while self.running:
            started = time.monotonic()
            try:
                self.step()
            except Exception:             # game not running, or a dropped connection
                self._gone()
                self.feed.close()
                time.sleep(1)
            time.sleep(max(0.0, POLL_SECONDS - (time.monotonic() - started)))
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
            with self.lock:
                self.judge.lost()
            if now - self.subscribed_at > RESUBSCRIBE_SECONDS:
                self.feed.subscribed = False        # in case it was tied to a train that's gone
            time.sleep(0.5)
            return
        train = _first(got["train"])
        if train is not None and train != self.train:    # (None: that entry missing from this answer)
            self.train = train
            with self.lock:
                self.judge.lost()
                self.judge.ease = (self.eases.get(train) or {}).get("ease")
        clock = (got["clock"] or {}).get("LocalTime")
        brake, power = got["brake"] or {}, got["power"] or {}
        demand = brake.get("HandlePosition") if brake.get("IsActive") else None
        if demand is None and power.get("IsActive"):
            # no train brake reading: the power handle on its brake side (Power in percent there). Where there is
            # one it's the handle; on the Class 331 Power follows the braking effort, a moment behind the handle
            demand = abs(power.get("Power") or 0.0) / 100 if power.get("IsNegative") else 0.0
        power_on = bool(power.get("IsActive")) and not power.get("IsNegative") and (power.get("Power") or 0) > 0
        holds = [_first(got["hold " + node]) for node in HOLD_NODES]
        holds = [h for h in holds if h is not None]
        hold = any(holds) if holds else None       # None: the train has no holding brake of its own
        if self.cars is None or (abs(speed) < STOPPED and now - self.counted > COUNT_SECONDS):
            self._count()
        with self.lock:
            said = self.judge.feed(now, speed, acc, self.passengers, clock / 1e7 if clock else None, demand, power_on,
                                   hold)
        for event in said:
            self._say(event)

    def _say(self, event):
        if event[0] == "stop":
            self.log(f"Comfort: {'jolt on stopping' if event[1] else 'smooth stop'}, braking {event[2]:.1f} m/s² as "
                     f"it stopped, {self.passengers} passengers")
        elif event[0] == "harsh":
            self.log(f"Comfort: harsh {'braking' if event[1] == 'brake' else 'acceleration'}, {event[2]:.1f} m/s²")
        elif event[0] == "hold":
            self.log(f"Comfort: the train's own holding brake came on at {event[1]:.2f} m/s; braking over "
                     f"{event[2]:.2f} m/s² from there isn't counted")
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
        """The passengers aboard: the passenger cars are looked for when the train changes."""
        api = self.api
        self.counted = time.monotonic()
        n = int(api.get_value("CurrentFormation.FormationLength") or 0)
        if n == 0:                                  # no answer: count next time round
            self.cars = self.passengers = None
            return
        if self.cars is None or n != self.formation:
            cars, answered = [], True
            for i in range(min(n, MAX_CARS)):
                names = core.node_names(api.list(f"CurrentFormation/{i}"))
                answered = answered and bool(names)
                model = next((m for m in CARGO_MODELS if m in names), None)
                if model:
                    cars.append(f"CurrentFormation/{i}/{model}")
            # a car that didn't answer is looked at again at the next count, not left out for good
            self.cars, self.formation = cars, n if answered else None
        counts = [api.get_value(car + ".Function.GetPassengerCount") for car in self.cars]
        counts = [c for c in counts if isinstance(c, (int, float))]
        self.passengers = int(sum(counts)) if counts else None
