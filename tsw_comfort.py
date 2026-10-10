"""
How the ride feels to the passengers: in the last seconds of a hard stop, a warning to ease the brake off before
the train stops; how each stop went; and a report of the ride from one station to the next.

What the game gives (seen on a Class 150, Cardiff valleys 1M56):
- CurrentDrivableActor.Function.HUD_GetAcceleration: the change of speed in m/s², in the game's own time. Working
  it out from the speed by the wall clock doesn't do: the game moves on at most 50 ms a frame, so below 20 fps it
  runs slower than the wall clock (by up to 40% on that drive).
- DriverAid.Data gradient: percent, negative downhill in the direction of travel. Along the car, passengers feel
  the change of speed plus g x the gradient: coasting downhill they feel nothing.
- PassengerCargoModel.Function.GetPassengerCount on each passenger car (a car with passenger doors).

Stopping on full service, that train braked harder and harder as it slowed, 1.8 m/s² just before it stopped,
then nothing within a second: the jolt. Easing the brake off in the last few seconds avoids it, but an air brake
takes a second or two to ease, so the warning comes about 4 s before the stop. Everything here only counts with
passengers aboard.

    watch = ComfortWatch(); watch.start()
    watch.coach              # None, or {"phase": "ease", "seconds": to the stop, "braking": m/s²}
                             #       or {"phase": "stopped", "jolt": bool, "braking": m/s² as it stopped}
    watch.report(service)    # the ride since the last station, None with nobody aboard: {"stop": None / "smooth" /
                             #   "jolt" (the stop you're at), "braking": as it stopped, "harsh": [["brake" / "power",
                             #   worst m/s²]], "jolts": at stops on the way (signals), "passengers": aboard,
                             #   "smooth": rides this service without a jolt or a harsh moment, "rides": of them}
    watch.new_leg(service)   # on leaving a station: that ride is done
"""

import math
import threading
import time

import tsw_joystick as core

G = 9.81
POLL_SECONDS = 0.05            # between polls; each read waits for the game's next tick (~65 ms)
GRADIENT_SECONDS = 0.5         # how often the gradient is read
COUNT_SECONDS = 5.0            # how often the passengers are counted while stopped (they don't change on the move)
MAX_CARS = 16
STOPPED = 0.1                  # m/s: slower than this the train has stopped
MOVING = 2.0                   # m/s: it has to have gone faster than this since the last stop for a stop to count
LAST = 1.0                     # m/s: the braking slower than this is what passengers feel as the train stops
JOLT = 0.7                     # m/s²: braking harder than this as it stops is a jolt
EASE_ON = 0.6                  # m/s²: the warning comes braking harder than this ...
LEAD = 4.0                     # ... with this many seconds to go to the stop at that rate
EASE_OFF, LEAD_OFF = 0.45, 5.0  # ... and goes once eased off below this, or with more seconds than this to go
RESULT_SECONDS = 6.0           # how long how the stop went stays up
HARSH = 1.2                    # m/s² felt, braking or speeding up, for HARSH_SECONDS: a harsh moment
HARSH_OFF = 1.0                # ... over until below this
HARSH_SECONDS = 1.0


def _leg():
    return {"stop": None, "braking": None, "harsh": [], "jolts": 0, "passengers": 0}


def _smooth(leg):
    return leg["stop"] != "jolt" and not leg["harsh"] and not leg["jolts"]


class Judge:
    """Goes by the readings one at a time (so it can be tried on a logged drive)."""

    def __init__(self):
        self.coach = None
        self.coach_until = 0.0
        self.moved = False            # faster than MOVING since the last stop
        self.approach = None          # hardest braking slower than LAST on the way into a stop
        self.braking = 0.0            # braking at the reading before
        self.harsh_since = None       # when the felt force went over HARSH
        self.harsh = None             # the harsh moment going on, once counted: [kind, worst]
        self.worst = 0.0
        self.leg = _leg()
        self.service = None           # the service the rides below are of
        self.smooth = self.rides = 0  # rides done on it

    def feed(self, now, speed, acc, gradient, passengers):
        """`now`: seconds; `speed`: m/s, signed; `acc`: the change of speed in m/s² (the game's sign); `gradient`:
        percent; `passengers`: aboard, None if not known. Says what happened, for the log: None, ("stop", jolt,
        braking), or ("harsh", kind, worst) once a harsh moment is over."""
        v = abs(speed)
        along = acc if speed >= 0 else -acc       # speeding up: positive
        braking = -along
        felt = along + G * gradient / 100
        aboard = bool(passengers)
        said = None
        if self.coach and self.coach["phase"] == "stopped" and now > self.coach_until:
            self.coach = None
        if v > MOVING:
            self.moved = True
            if aboard:
                self.leg["passengers"] = passengers

        if v >= STOPPED:
            if v < LAST:
                self.approach = max(self.approach or 0.0, braking)
            else:
                self.approach = None                # faster again
            seconds = v / braking if braking > 0.05 else math.inf
            if self.coach and self.coach["phase"] == "ease":
                easing = braking > EASE_OFF and seconds < LEAD_OFF
            else:
                easing = braking > EASE_ON and seconds < LEAD
            if aboard and self.moved and easing:
                self.coach = {"phase": "ease", "seconds": seconds, "braking": braking}
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
                said = ("stop", jolt, hardest)
            else:
                self.coach = None
            self.moved, self.approach = False, None

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
            if self.harsh is not None and said is None:
                said = ("harsh", self.harsh[0], self.harsh[1])
            self.harsh_since = self.harsh = None
        return said

    def report(self, service):
        leg = self.leg
        if not leg["passengers"] or (leg["stop"] is None and not leg["harsh"] and not leg["jolts"]):
            return None
        smooth, rides = (self.smooth, self.rides) if service == self.service else (0, 0)
        if leg["stop"] is not None:
            smooth, rides = smooth + _smooth(leg), rides + 1
        return dict(leg, harsh=[list(h) for h in leg["harsh"]], smooth=smooth, rides=rides)

    def new_leg(self, service):
        leg = self.leg
        if service != self.service:
            self.service, self.smooth, self.rides = service, 0, 0
        if leg["passengers"] and leg["stop"] is not None:
            self.smooth += _smooth(leg)
            self.rides += 1
        self.leg = _leg()
        self.harsh = None                 # a harsh moment going on carries on into the next ride uncounted


class ComfortWatch(threading.Thread):
    """Reads the train's motion and passengers on a thread of its own and judges the ride."""

    def __init__(self, log=None):
        super().__init__(daemon=True)
        self.api = core.TSWApi()
        self.log = log or (lambda msg: None)
        self.running = True
        self.judge = Judge()
        self.lock = threading.Lock()
        self.gradient = 0.0
        self.gradient_read = 0.0
        self.cars = None              # API paths of the passenger cars' PassengerCargoModel
        self.formation = None         # number of vehicles when they were looked at
        self.passengers = None
        self.counted = 0.0

    @property
    def coach(self):
        return self.judge.coach

    def report(self, service):
        with self.lock:
            return self.judge.report(service)

    def new_leg(self, service):
        with self.lock:
            self.judge.new_leg(service)

    def run(self):
        while self.running:
            try:
                self.step()
            except Exception:             # game not running, or a dropped connection
                self._gone()
                time.sleep(1)
            time.sleep(POLL_SECONDS)

    def _gone(self):
        self.judge.coach = None
        self.cars = self.formation = self.passengers = None

    def step(self):
        api = self.api
        if not api.key and not api.load_key():
            self._gone()
            time.sleep(2)
            return
        speed = api.get_value("CurrentDrivableActor.Function.HUD_GetSpeed")
        acc = None if speed is None else api.get_value("CurrentDrivableActor.Function.HUD_GetAcceleration")
        if speed is None or acc is None:  # not in a cab
            self._gone()
            time.sleep(1)
            return
        now = time.monotonic()
        if now - self.gradient_read > GRADIENT_SECONDS:
            self.gradient_read = now
            self.gradient = (api.get("DriverAid.Data").get("Values") or {}).get("gradient") or 0.0
        if self.cars is None or (abs(speed) < STOPPED and now - self.counted > COUNT_SECONDS):
            self._count()
        with self.lock:
            said = self.judge.feed(now, speed, acc, self.gradient, self.passengers)
        if said and said[0] == "stop":
            self.log(f"Comfort: {'jolt on stopping' if said[1] else 'smooth stop'}, braking {said[2]:.1f} m/s² as it "
                     f"stopped, {self.passengers} passengers")
        elif said:
            self.log(f"Comfort: harsh {'braking' if said[1] == 'brake' else 'acceleration'}, {said[2]:.1f} m/s²")

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
