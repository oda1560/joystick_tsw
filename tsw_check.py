"""
Checks the joystick bridge against every train installed, without loading them in the game.

Each rail vehicle's cab controls are read from Train Sim World's .pak files (tsw_paks.py). A model of those
controls stands in for the game's API and the bridge's own code (tsw_joystick.py) is run against it, just as
in the cab: it finds the controls, then the stick is pushed to full power, centred, pulled to full brake and
centred again, and the reverser slider is tried. What each one did is reported, with anything doubtful
flagged.

The model comes from each control's settings in the game files: its notches, its input -> output table and
its gated notches (ones a handle has to be moved into on its own, e.g. Class 331 Coasting). It can't know
about interlocks a train applies itself (e.g. no power with the reverser in Neutral), so it assumes the cab is
set up and ready to drive.

Usage:
    python tsw_check.py              check every train; writes train_check_report.txt
    python tsw_check.py 331 802      only trains whose name contains one of these
    python tsw_check.py --live       compare the model with the game for the train you're in (read-only)
"""

import math
import os
import re
import sys
import time as real_time
from collections import defaultdict

import tsw_joystick as tj
from tsw_paks import GameFiles

REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_check_report.txt")

# a vehicle is checked if one of its controls has one of these game input identifiers
DRIVING_IDS = {"throttle", "automaticbrake", "reverser"}
VHID_BASES = {"IrregularLeverComponent", "SimpleLeverComponent", "PushButtonComponent", "LeverBaseComponent",
              "CouplerLockComponent", "VirtualHIDComponent"}
LEVER_KEYS = ("ValueMap", "Notches", "NumberOfNotches", "InputIdentifier", "DisplayInfo")
ERROR = {"Result": "Error", "Message": "Node failed to return valid data."}

STICK_MOVE_SECONDS = 0.8         # how long the simulated hand takes to move the stick to a new place
STICK_HOLD_SECONDS = 0.7         # and how long it then holds it there
SHORT_OF_FULL = 0.97             # full power / full brake reaching less than this of the handle's is flagged


def enum(value, default):
    return str(value).split("::")[-1] if value else default


IDLE_WORDS = ("off", "coast", "idl", "neutral", "closed")      # no power, no brake
ENGINE_STOP_WORDS = ("stop", "shutdown", "shut down")
RELEASE_WORDS = ("release", "running", "run", "off", "charge", "driv")
# beyond full service: not where a stick at full brake should put a brake handle
PAST_SERVICE_WORDS = ("off", "shutdown", "shut down", "cut", "neutral", "isol", "suppress", "handle")


def is_neutral(label):
    s = label.strip().lower()
    return (s in tj.NEUTRAL_LABELS or any(w in s for w in IDLE_WORDS)) and \
        not any(w in s for w in ("brake", "power", "%"))


# ---------------------------------------------------------------- model of a train's controls
class Control:
    """One cab control as the game would behave, worked out from its settings in the game files."""

    def __init__(self, comp):
        p = comp.props
        self.name, self.cls, self.base, self.props = comp.name, comp.cls, comp.base, p
        self.vhid = comp.base in VHID_BASES or any(k in p for k in LEVER_KEYS)
        ident = p.get("InputIdentifier")
        self.ident = str(ident.get("Identifier") or "None") if isinstance(ident, dict) else "None"
        env = p.get("InteractionEnvironmentComponent")
        self.env = str(env.get("ComponentName") or "None") if isinstance(env, dict) else "None"
        self.enabled = p.get("bInputEnabled", True) is not False
        self.custom = bool(p.get("bCustomOutputValueMapping"))
        self.interlocked = "BlockedMinInputValue" in p or "BlockedMaxInputValue" in p
        self.lever = self.base not in ("PushButtonComponent", "CouplerLockComponent")
        self.notches = []                # (lowest input, highest input, gated), lowest first
        self.notch_count = 0
        if "ValueMap" in p or self.base == "IrregularLeverComponent":
            points = [(float(m.get("InputValue", 0.0)), float(m.get("OutputValue", 0.0)),
                       float(m.get("LinearOutputValueInterval", 0.0)))
                      for m in p.get("ValueMap") or [] if isinstance(m, dict)]
            self.points = sorted(points, key=lambda m: m[0]) or [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0)]
            # a notch set beyond the end of the handle's travel (some BR 363s: -3 and 3 on a 0..1 handle)
            # is taken to be at the end
            lo, hi = self.points[0][0], self.points[-1][0]
            clamp = lambda v: min(hi, max(lo, float(v))) if hi > lo else float(v)
            self.notches = sorted((clamp(n.get("MinimumInputValue", 0.0)), clamp(n.get("MaximumInputValue", 0.0)),
                                   bool(n.get("bBlocker"))) for n in p.get("Notches") or [] if isinstance(n, dict))
            self.notch_count = len(self.notches)
        elif self.base == "SimpleLeverComponent":
            lo, hi = float(p.get("MinimumInputValue", 0.0)), float(p.get("MaximumInputValue", 1.0))
            step = float(p.get("LinearOutputValueInterval", 0.0))
            self.points = [(lo, float(p.get("MinimumOutputValue", 0.0)), step),
                           (hi, float(p.get("MaximumOutputValue", 1.0)), step)]
            self.notch_count = int(p.get("NumberOfNotches", 0) or 0)
            if self.notch_count >= 2 and hi > lo:
                n = self.notch_count
                self.notches = [(lo + i * (hi - lo) / (n - 1),) * 2 + (False,) for i in range(n)]
        else:
            self.points = [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0)]
        self.lo, self.hi = self.points[0][0], self.points[-1][0]
        if self.hi <= self.lo:
            self.hi = self.lo + 1.0
            self.points = [(self.lo, self.points[0][1], 0.0), (self.hi, self.points[-1][1], 0.0)]
        outs = [pt[1] for pt in self.points]
        self.out_lo, self.out_hi = min(outs), max(outs)
        # how far a notch pulls the handle in from, as a share of the gap to the next notch (or the end of the
        # handle): 0.1 x the lever's snap setting (the game's default is 5), at most half. Inferred from the
        # game: the Class 331 reverser (10) lands in its nearest notch from anywhere, the 331 power handle (5)
        # from halfway, while the Class 802 power handle (0.5) stayed between notches 0.1 into a 0.25 gap
        self.snap_pull = min(0.5, 0.1 * float(p.get("NotchSnapSensitivity", 5.0)))
        self.table_known = "ValueMap" in p or self.base != "IrregularLeverComponent"
        # the DSD pedal button counts backwards in the game (rests at 1, pressed at 0); the train sets that up
        # when it starts, so it isn't in the files
        self.reversed_button = self.cls == "ReversiblePushButton_C"
        self.value = self.snap(float(p.get("DefaultInputValue", 0.0)))

    # -------- how the game turns the handle's position into its output
    def output(self, x):
        pts = self.points
        x = min(self.hi, max(self.lo, x))
        for px, py, _ in pts:
            if abs(px - x) < 1e-6:
                return py                # exactly at a point of the table (e.g. Class 802 Off at 0)
        for (x0, y0, step), (x1, y1, _) in zip(pts, pts[1:]):
            if x <= x1:
                v = y1 if x1 == x0 else y0 + (x - x0) / (x1 - x0) * (y1 - y0)
                if step > 0:
                    # the section gives its output in steps of this size, counted from its start
                    # (seen in the files; the rounding direction is assumed)
                    q = (v - y0) / step
                    v = y0 + math.copysign(math.floor(abs(q) + 0.5), q) * step
                    v = min(max(y0, y1), max(min(y0, y1), v))
                return v
        return pts[-1][1]

    def normalised_output(self, x):
        return (self.output(x) - self.out_lo) / (self.out_hi - self.out_lo) if self.out_hi > self.out_lo else 0.0

    # -------- how the handle moves
    def snap(self, x, way=0):
        """Where the handle settles when sent to x (moving that way: +1 / -1): where it is if that's inside a
        notch, else pulled into the nearest notch when close enough to it (see snap_pull). Exactly
        halfway between two, it goes on to the one ahead (seen on the Class 331)."""
        x = min(self.hi, max(self.lo, x))
        if not self.notches:
            return x
        below = [b for a, b, _ in self.notches if b <= x + 1e-6]
        above = [a for a, b, _ in self.notches if a >= x - 1e-6]
        for a, b, _ in self.notches:
            if a - 1e-6 <= x <= b + 1e-6:
                return x
        lo, hi = (max(below) if below else None), (min(above) if above else None)
        nearest = min((e for e in (lo, hi) if e is not None), key=lambda e: (round(abs(e - x), 9), -way * e))
        gap = (hi if hi is not None else self.hi) - (lo if lo is not None else self.lo)
        return nearest if abs(nearest - x) <= self.snap_pull * gap + 1e-9 else x

    def notch_index(self, x):
        for i, (a, b, _) in enumerate(self.notches):
            if a - 1e-6 <= x <= b + 1e-6:
                return i
        return min(range(len(self.notches)), key=lambda i: min(abs(self.notches[i][0] - x),
                                                               abs(self.notches[i][1] - x)))

    def move(self, target):
        """Send the handle toward target. As in the game (seen on the Class 331), a gated notch stops it: the
        handle goes into one that is the first notch it meets and stops there, and stops at the end of the
        notch before one further on."""
        p = self.value
        x = self.snap(target, 1 if target > p else -1)
        if abs(x - p) < 1e-9:
            return
        way = 1 if x > p else -1
        met = [n for n in (self.notches if way > 0 else reversed(self.notches))
               if not n[0] - 1e-6 <= p <= n[1] + 1e-6
               and (p < n[0] <= x + 1e-6 if way > 0 else x - 1e-6 <= n[1] < p)]
        for k, (a, b, gated) in enumerate(met):
            if gated:
                if k == 0:
                    self.value = min(b, max(a, x))
                else:
                    pa, pb, _ = met[k - 1]
                    self.value = pb if way > 0 else pa
                return
        self.value = x

    # -------- the game's names for places on the handle
    def zone(self, x):
        """(label, is emergency) for the handle at x, from the game's named places, or (None, False)."""
        out = self.output(x)
        values = {"Input": x, "InputNormalised": (x - self.lo) / (self.hi - self.lo),
                  "Output": out, "OutputNormalised": self.normalised_output(x)}
        for nv in (self.props.get("DisplayInfo") or {}).get("NamedValues") or []:
            r = nv.get("ValueRange") or {}
            v = values.get(enum(nv.get("ValueSource"), "Output"))
            if v is not None and r.get("Min", 0.0) - 1e-6 <= v <= r.get("Max", 0.0) + 1e-6:
                label = str(nv.get("DisplayName") or "")
                return self._format(label, values) if nv.get("bDisplayNameIsFormatString") else label, \
                    tj.is_emergency(label.lower())
        return None, False

    def _format(self, label, values):
        def convert(m):
            vc = next((c for c in self.props.get("ValueConverters") or []
                       if isinstance(c, dict) and str(c.get("ID")) == m.group(1)), None)
            if vc is None:
                return "?"
            v = values.get(enum(vc.get("ValueSource"), "Output"), values["Output"])
            fr, to = vc.get("RemapFromRange"), vc.get("RemapToRange")
            if isinstance(fr, dict) and isinstance(to, dict) and fr.get("Max", 0.0) != fr.get("Min", 0.0):
                a, b = fr.get("Min", 0.0), fr.get("Max", 0.0)
                if vc.get("bUseOutOfRangeValue") and not min(a, b) <= v <= max(a, b):
                    v = vc.get("OutOfRangeValue", 0.0)
                else:
                    f = min(1.0, max(0.0, (v - a) / (b - a)))
                    v = to.get("Min", 0.0) + f * (to.get("Max", 0.0) - to.get("Min", 0.0))
            if enum(vc.get("Mode"), "None") == "Percentage":
                return f"{v * 100:.0f}%"
            return f"{v:.2f}".rstrip("0").rstrip(".")
        return re.sub(r"\{(\w+)\}", convert, label).strip()

    def describe(self, x=None):
        x = self.value if x is None else x
        label, _ = self.zone(x)
        out = f"output {self.output(x):.3g}"
        return f"{label} ({out})" if label else out

    # -------- the game's API
    def answer(self, endpoint):
        if endpoint == "ObjectClass":
            return {"ObjectClass": self.cls}
        if not self.vhid:
            return None
        lo, hi = (1.0, 0.0) if self.reversed_button else (self.lo, self.hi)
        di = self.props.get("DisplayInfo") or {}
        answers = {
            "InputValue": lambda: {"InputValue": self.value},
            "Property.InputIdentifier": lambda: {"identifier": self.ident},
            "Property.bInputEnabled": lambda: {"Value": self.enabled},
            "Property.InteractionEnvironmentComponent": lambda: {"componentName": self.env},
            "Property.bCustomOutputValueMapping": lambda: {"Value": self.custom},
            "Property.DisplayInfo": lambda: {
                "mode": enum(di.get("Mode"), "None"),
                "valueSource": enum(di.get("ValueSource"), "OutputNormalised"),
                "namedValues": [{"valueSource": enum(nv.get("ValueSource"), "Output"),
                                 "valueRange": {"min": (nv.get("ValueRange") or {}).get("Min", 0.0),
                                                "max": (nv.get("ValueRange") or {}).get("Max", 0.0)},
                                 "displayName": str(nv.get("DisplayName") or ""),
                                 "bDisplayNameIsFormatString": bool(nv.get("bDisplayNameIsFormatString"))}
                                for nv in di.get("NamedValues") or [] if isinstance(nv, dict)]},
            "Function.GetMinimumInputValue": lambda: {"ReturnValue": lo},
            "Function.GetMaximumInputValue": lambda: {"ReturnValue": hi},
            "Function.GetMinimumOutputValue": lambda: {"ReturnValue": self.out_lo},
            "Function.GetMaximumOutputValue": lambda: {"ReturnValue": self.out_hi},
            "Function.GetNotchCount": lambda: {"ReturnValue": self.notch_count},
            "Function.GetCurrentOutputValue": lambda: {"ReturnValue": self.output(self.value)},
            "Function.GetCurrentNotchIndex": lambda: {"ReturnValue": self.notch_index(self.value)
                                                      if self.notches else 0},
        }
        fn = answers.get(endpoint)
        return fn() if fn else None


class ModelApi(tj.TSWApi):
    """Stands in for the game's API, answering from the model of one train's controls."""

    def __init__(self, train, components):
        super().__init__()
        self.key = "model"
        self.train = train
        self.controls = {name: Control(c) for name, c in components.items()}
        self.moves = []                  # (control, input) after every move, in order

    def _req(self, method, path, params=None):
        kind, _, rest = path.partition("/")
        if kind == "list":
            if rest == "CurrentDrivableActor":
                return {"Result": "Success", "Nodes": [{"Name": n} for n in self.controls]}
            return dict(ERROR)
        node, _, endpoint = rest.partition(".")
        if node == "CurrentDrivableActor":
            if endpoint == "ObjectClass":
                return {"Result": "Success", "Values": {"ObjectClass": self.train}}
            if endpoint == "Function.HUD_GetSpeed":
                return {"Result": "Success", "Values": {"ReturnValue": 0.0}}
            return dict(ERROR)
        c = self.controls.get(node[len("CurrentDrivableActor/"):]) if node.startswith("CurrentDrivableActor/") \
            else None
        if c is None:
            return dict(ERROR)
        if kind == "set":
            if endpoint != "InputValue" or not c.vhid:
                return dict(ERROR)
            c.move(float(params["Value"]))
            self.moves.append((c, c.value))
            return {"Result": "Success"}
        values = c.answer(endpoint)
        return {"Result": "Success", "Values": values} if values is not None else dict(ERROR)


# ---------------------------------------------------------------- running the bridge on the model
class Clock:
    """Stands in for the time module inside the bridge, so its waits take no real time."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    @staticmethod
    def strftime(*args):
        return real_time.strftime(*args)


class Stick:
    def __init__(self):
        self.axes = [0.0] * 4

    def get_axis(self, i):
        return self.axes[i]

    def get_numaxes(self):
        return len(self.axes)


class Driver:
    """Works the stick as run() does: read 30 times a second, smoothed, and only sent on when it moves."""

    def __init__(self, controls, clock):
        self.controls, self.clock = controls, clock
        self.stick, self.filter = Stick(), tj.AxisFilter()
        self.position, self.last_sent = 0.0, None

    def tick(self):
        self.clock.sleep(1.0 / tj.POLL_HZ)
        self.stick.axes[tj.Y_AXIS] = -self.position          # pushing the stick forward reads negative
        y = self.filter.update(tj.read_y(self.stick, tj.Y_AXIS, False, tj.DEADZONE), self.clock.now)
        if self.last_sent is not None and abs(y - self.last_sent) < tj.SEND_THRESHOLD:
            return
        if self.last_sent is None:
            self.last_sent = y
            return
        self.controls.apply(y)
        self.last_sent = y

    def move_to(self, position):
        start, steps = self.position, max(1, round(STICK_MOVE_SECONDS * tj.POLL_HZ))
        for i in range(1, steps + 1):
            self.position = start + (position - start) * i / steps
            self.tick()
        for _ in range(round(STICK_HOLD_SECONDS * tj.POLL_HZ)):
            self.tick()


def bridge_patched():
    """Point the bridge at simulated time and an empty, in-memory calibration (as on a first drive)."""
    clock = Clock()
    tj.time = clock
    tj._calibration = {}
    tj.save_calibration = lambda key, value: tj._calibration.__setitem__(key, round(value, 4))
    return clock


def reachable(control, a, b, steps=400):
    """Inputs the handle can rest at between a and b (both included)."""
    xs = {control.snap(a + (b - a) * i / steps) for i in range(steps + 1)}
    for lo, hi, _ in control.notches:
        xs.update(x for x in (lo, hi) if min(a, b) - 1e-9 <= x <= max(a, b) + 1e-9)
    return sorted(xs)


class Result:
    def __init__(self, train, pack):
        self.train, self.pack = train, pack
        self.lines = []                  # what each control did
        self.problems = []               # the bridge does something wrong on this train
        self.doubts = []                 # couldn't be checked properly, or the model may be off

    @property
    def verdict(self):
        return "PROBLEM" if self.problems else "CHECK" if self.doubts else "OK"


def check_train(train, package, files):
    result = Result(train, files.pack_of(package))
    clock = bridge_patched()
    api = ModelApi(train, files.components(train, package))
    messages = []
    controls = tj.TrainControls(api, log=messages.append)
    controls.train_id = train
    controls.detect()
    model = api.controls
    t, b = controls.throttle, controls.brake
    tm, bm = (model[t.name] if t else None), (model[b.name] if b else None)

    if not t and not b:
        result.problems.append("the stick does nothing: no throttle or brake found")
    elif not b and (not t or t.brake_end is None):
        result.problems.append("the stick can't brake: no train brake found")
    for lever, m in ((t, tm), (b, bm)):
        if lever is None:
            continue
        if any(w in lever.note for w in ("guessed", "unknown", "no notch info")):
            result.doubts.append(f"{lever.name}: {lever.note}")
        if m.custom:
            result.doubts.append(f"{lever.name} maps its output in the train's own code: the model may be off")
        if m.interlocked:
            result.doubts.append(f"{lever.name} is held back by another control in some states (interlock)")

    # the stick: as you'd use it after getting into the cab
    driver = Driver(controls, clock)
    driver.tick()
    snapshots = {}
    for name, y in (("full power", 1.0), ("centre", 0.0), ("full brake", -1.0), ("centre again", 0.0),
                    ("half power", 0.5), ("half brake", -0.5), ("centre at the end", 0.0)):
        start = len(api.moves)
        driver.move_to(y)
        snapshots[name] = {c.name: c.value for c in (tm, bm) if c}
        for c, x in api.moves[start:]:
            if c in (tm, bm) and c.zone(x)[1]:
                result.problems.append(f"{c.name} went into emergency ({c.describe(x)}) "
                                       f"while moving the stick to {name}")
                break

    def on(c, name):
        return snapshots[name].get(c.name, c.value)

    if t:
        kind = "power+brake lever" if t.brake_end is not None and not b else "throttle"
        result.lines.append(f"stick: {kind} {t.name}" + (f", brake {b.name}" if b else ""))
    elif b:
        result.lines.append(f"stick: brake {b.name} (no throttle)")
    for name in snapshots:
        parts = [f"{c.name} {c.describe(on(c, name))}" for c in (tm, bm) if c]
        result.lines.append(f"  {name:<18} {'; '.join(parts)}")

    if tm:
        power_end = t.power_end
        far = tm.hi if power_end >= t.neutral else tm.lo
        xs = reachable(tm, t.neutral, far)
        best = max(xs, key=lambda x: abs(tm.output(x) - tm.output(t.neutral)))
        got = on(tm, "full power")
        span = tm.output(best) - tm.output(t.neutral)
        if span and (tm.output(got) - tm.output(t.neutral)) / span < SHORT_OF_FULL:
            result.problems.append(f"full power only reaches {tm.describe(got)}; the handle goes to "
                                   f"{tm.describe(best)}")
        combined = t.brake_end is not None and not b
        lowest = [x for x in reachable(tm, tm.lo, tm.hi)
                  if not any(w in (tm.zone(x)[0] or "").lower() for w in ENGINE_STOP_WORDS)]
        idle_out = max(0.0, min(tm.output(x) for x in lowest)) if lowest else 0.0
        for name in ("centre", "centre again", "centre at the end"):
            x = on(tm, name)
            label, _ = tm.zone(x)
            low = (label or "").lower()
            if any(w in low for w in ENGINE_STOP_WORDS):
                result.problems.append(f"stick at {name}: {t.name} is at {tm.describe(x)} - "
                                       f"that stops the engine")
            elif combined and label is not None and not is_neutral(label):
                result.problems.append(f"stick at {name}: {t.name} is at {tm.describe(x)}, not Off")
            elif combined and label is None and abs(tm.output(x) - tm.output(t.neutral)) > 1e-6:
                result.problems.append(f"stick at {name}: {t.name} is at {tm.describe(x)}")
            elif not combined and not (label and (is_neutral(label) or "min" in low)) \
                    and tm.output(x) > idle_out + 1e-6:
                result.problems.append(f"stick at {name}: {t.name} gives power ({tm.describe(x)})")
            else:
                continue
            break
        if not b and t.brake_end is not None and "capped" not in t.note:
            check_brake(result, tm, t.neutral, tm.lo if t.brake_end < t.neutral else tm.hi,
                        on(tm, "full brake"))
    if bm and "capped" not in b.note:     # capped: no named places, kept short of the end on purpose
        check_brake(result, bm, b.safe_lo, bm.hi, on(bm, "full brake"))
        for name in ("centre", "centre again", "centre at the end"):
            x = on(bm, name)
            label, _ = bm.zone(x)
            released = any(w in label.lower() for w in RELEASE_WORDS) if label else \
                abs(bm.output(x) - bm.out_lo) < 1e-6
            if not released:
                result.problems.append(f"stick at {name}: {b.name} is at {bm.describe(x)}, not released")
                break

    check_reverser(result, controls, model)
    check_buttons(result, controls, model)
    for m in messages:
        result.lines.append(f"  bridge said: {m}")

    # a handle whose table the game files don't give (it's the game's built-in default) can't be modelled
    # reliably, so what it seemed to do is only worth a check in the game
    unsure = [c.name for c in model.values() if c.vhid and not c.table_known
              and c.name in (getattr(t, "name", None), getattr(b, "name", None),
                             getattr(controls.reverser, "name", None))]
    for name in unsure:
        moved = [p for p in result.problems if name in p]
        result.problems = [p for p in result.problems if name not in p]
        result.doubts += [f"{p} (the game files don't give {name}'s table, so the model may be off)"
                          for p in moved]
    return result


def check_brake(result, m, release, end, got):
    """Full stick should give the strongest braking the handle has short of emergency: looking from the
    released position toward the end of the handle, up to the first emergency place, or one past full
    service (e.g. Handle Off, Suppression, Shutdown)."""
    xs = reachable(m, release, end)
    if end < release:
        xs.reverse()
    base, best = m.output(release), None
    for x in xs:
        label, emergency = m.zone(x)
        if emergency or (best is not None and any(w in (label or "").lower() for w in PAST_SERVICE_WORDS)):
            break
        if best is None or abs(m.output(x) - base) > abs(m.output(best) - base):
            best = x
    span = m.output(best) - base if best is not None else 0.0
    same_place = m.zone(got)[0] is not None and m.zone(got)[0] == m.zone(best)[0]
    if span and not same_place and (m.output(got) - base) / span < SHORT_OF_FULL:
        result.problems.append(f"full brake only reaches {m.describe(got)}; strongest normal braking is "
                               f"{m.describe(best)}")


def check_reverser(result, controls, model):
    rev = controls.reverser
    has = [c for c in model.values() if c.ident.lower() == "reverser" and c.enabled]
    if rev is None:
        if has:
            result.problems.append(f"reverser {has[0].name} not found by the bridge")
        else:
            result.lines.append("slider: no reverser on this train")
        return
    if not rev.ok:
        names = ", ".join(l for _, _, l, _ in rev.lever.zones) or "none named"
        missing = [p for p in tj.REVERSER_LABELS if p not in rev.notches]
        result.problems.append(f"the slider does nothing: reverser {rev.name} has no position the bridge "
                               f"takes for {' / '.join(missing)} (its positions: {names})")
        return
    got = []
    for position in ("forward", "neutral", "reverse", "neutral", "forward"):
        rev.set(position)
        got.append((position, rev.position()))
    wrong = [f"{want} gave {have}" for want, have in got if want != have]
    result.lines.append(f"slider: reverser {rev.name}: " + ("Forward / Neutral / Reverse all right"
                                                            if not wrong else "; ".join(wrong)))
    if wrong:
        result.problems.append(f"reverser {rev.name}: " + "; ".join(wrong))


def check_buttons(result, controls, model):
    found = []
    for name, label in (("aws", "AWS"), ("alerter", "alerter")):
        button = getattr(controls, name)
        found.append(f"{label} {button}" if button else f"no {label}")
    doors = [f"{a} {s}" for a in ("open", "close") for s in ("left", "right") if getattr(controls, f"door_{a}_{s}")]
    found.append(f"doors {', '.join(doors)}" if doors else "no door buttons")
    result.lines.append("buttons: " + "; ".join(found))

    ids = {c.ident.lower().replace("_", "") for c in model.values() if c.enabled}
    if controls.aws is None and ids & set(tj.AWS_IDS):
        result.problems.append("the train has an AWS acknowledge button but the bridge didn't find it")
    if controls.alerter is None and ids & set(tj.ALERTER_IDS):
        result.problems.append("the train has an alerter / DSD / SIFA but the bridge didn't find it")


# ---------------------------------------------------------------- report
CAUSES = [   # (what the problem is, pattern in the problem text), most specific first
    ("Reverser slider does nothing: no position the bridge reads as Forward / Neutral / Reverse",
     r"slider does nothing"),
    ("Reverser not found by the bridge", r"reverser .* not found"),
    ("Reverser lands in the wrong position", r"reverser .* gave"),
    ("Handle goes into emergency", r"went into emergency"),
    ("Stick centred stops the engine", r"stops the engine"),
    ("Stick centred doesn't give Off (power or brake applied)", r"gives power|not Off|stick at .*: .* is at"),
    ("Stick centred leaves the train brake applied", r"not released"),
    ("Full stick back gives less than full service braking", r"full brake only reaches"),
    ("Full stick forward gives less than full power", r"full power only reaches"),
    ("Stick does nothing / can't brake", r"does nothing: no|can't brake"),
    ("AWS / alerter button not found", r"didn't find"),
]


def causes(result):
    """The causes of a vehicle's problems, each problem counted under the first cause it matches."""
    found = []
    for problem in result.problems:
        title = next((t for t, pattern in CAUSES if re.search(pattern, problem)), None)
        if title and title not in found:
            found.append(title)
    return found

def is_driven(files, train, package):
    if re.search(r"_?Base_C$", train) or "/Base/" in package:
        return False                     # parent blueprints shared by a train's vehicles, never driven
    comps = files.components(train, package)
    ids = {str((c.props.get("InputIdentifier") or {}).get("Identifier", "")).lower()
           for c in comps.values() if isinstance(c.props.get("InputIdentifier"), dict)}
    if any(i.startswith("steam") for i in ids):
        return False                     # steam locomotives (blower, injectors...) aren't driven with the stick
    return bool(ids & DRIVING_IDS)


def short(train):
    return re.sub(r"^RVM_|_C$", "", train)


def write_report(results, files, seconds):
    by_pack = defaultdict(list)
    for r in results:
        by_pack[r.pack].append(r)
    counts = {v: sum(r.verdict == v for r in results) for v in ("OK", "CHECK", "PROBLEM")}
    out = ["Joystick bridge check against the installed trains",
           f"{real_time.strftime('%Y-%m-%d %H:%M')}, {len(results)} drivable vehicles in {len(by_pack)} packs, "
           f"{seconds:.0f} s",
           f"OK {counts['OK']}   CHECK {counts['CHECK']}   PROBLEM {counts['PROBLEM']}",
           "",
           "PROBLEM: the bridge would do something wrong on this train (per the model of its controls).",
           "CHECK:   it looks right, but something couldn't be checked properly - worth a try in the game.",
           "Vehicles with identical results are listed together.",
           ""]
    tally = defaultdict(list)
    for r in results:
        for title in causes(r):
            tally[title].append(r)
    if tally:
        out += ["Problems by cause (a vehicle can have several):"]
        for title, _ in CAUSES:
            if tally[title]:
                packs = sorted({r.pack for r in tally[title]}, key=str.lower)
                more = f" and {len(packs) - 8} more" if len(packs) > 8 else ""
                out += [f"  {len(tally[title]):4} vehicles  {title}",
                        f"                 in {', '.join(packs[:8])}{more}"]
        out.append("")
    order = {"PROBLEM": 0, "CHECK": 1, "OK": 2}
    for title, verdicts in (("NEEDS A LOOK", ("PROBLEM", "CHECK")), ("ALL OK", ("OK",))):
        out += ["=" * 100, title, "=" * 100]
        for pack in sorted(by_pack, key=str.lower):
            groups = defaultdict(list)
            for r in by_pack[pack]:
                if r.verdict in verdicts:
                    groups[(r.verdict, tuple(r.problems), tuple(r.doubts), tuple(r.lines))].append(r)
            if not groups:
                continue
            out.append(f"\n## {pack}")
            for key in sorted(groups, key=lambda k: (order[k[0]], short(groups[k][0].train))):
                verdict, problems, doubts, lines = key
                names = ", ".join(short(r.train) for r in groups[key])
                out.append(f"\n[{verdict}] {names}")
                out += [f"  ! {p}" for p in problems] + [f"  ? {d}" for d in doubts]
                out += [f"  {line}" for line in lines]
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def check_all(filters):
    start = real_time.time()
    print("Reading the game files...", flush=True)
    files = GameFiles()
    for name, err in files.errors:
        print(f"  couldn't read {name}: {err}")
    trains = files.vehicle_classes()
    if filters:
        trains = {k: v for k, v in trains.items() if any(f.lower() in k.lower() for f in filters)}
    results = []
    for i, (train, package) in enumerate(sorted(trains.items()), 1):
        print(f"\r  {i}/{len(trains)} {short(train)[:60]:<60}", end="", flush=True)
        try:
            if not is_driven(files, train, package):
                continue
            results.append(check_train(train, package, files))
        except Exception as e:
            r = Result(train, files.pack_of(package))
            r.doubts.append(f"couldn't be checked: {type(e).__name__}: {e}")
            results.append(r)
    print("\r" + " " * 80 + "\r", end="")
    write_report(results, files, real_time.time() - start)
    counts = {v: sum(r.verdict == v for r in results) for v in ("OK", "CHECK", "PROBLEM")}
    print(f"{len(results)} drivable vehicles: OK {counts['OK']}, CHECK {counts['CHECK']}, "
          f"PROBLEM {counts['PROBLEM']}")
    print(f"Report: {REPORT_FILE}")


# ---------------------------------------------------------------- comparing the model with the game
class ReadOnlyApi(tj.TSWApi):
    """The game's API, with reads recorded and anything that would move a control refused."""

    def __init__(self, key):
        super().__init__()
        self.key = key
        self.paths = []

    def _req(self, method, path, params=None):
        if method != "GET":
            raise RuntimeError("read-only")
        self.paths.append(path)
        return super()._req(method, path, params)


VOLATILE = ("InputValue", "Function.GetCurrentOutputValue", "Function.GetCurrentNotchIndex",
            "Function.HUD_GetSpeed")


def same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) < 1e-3
    if isinstance(a, dict) and isinstance(b, dict):      # the game may send more than the model has
        return all(k in a and same(a[k], b[k]) for k in b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def compare_live():
    live = tj.TSWApi()
    if not live.load_key():
        print(f"API key not found at {tj.KEY_FILE}. Launch TSW7 with -HTTPAPI first.")
        return
    train = live.get_value("CurrentDrivableActor.ObjectClass")
    print("Train:", train)
    files = GameFiles()
    trains = files.vehicle_classes()
    if train not in trains:
        print("This train isn't in the game files (or isn't a rail vehicle blueprint).")
        return
    tj._calibration = {}
    tj.save_calibration = lambda key, value: None
    model = ModelApi(train, files.components(train, trains[train]))

    live_names = tj.node_names(live.list("CurrentDrivableActor"))
    model_names = list(model.controls)
    lower = {n.lower() for n in live_names}
    missing = [n for n in model_names if n.lower() not in lower]
    extra = [n for n in live_names if n.lower() not in {m.lower() for m in model_names}]
    controls = {n.lower() for n, c in model.controls.items() if c.vhid}     # C++ components' order is unknown
    order = [n.lower() for n in live_names if n.lower() in controls] == \
            [n.lower() for n in model_names if n.lower() in controls and n.lower() in lower]
    print(f"\nComponents: {len(live_names)} in the game, {len(model_names)} in the model"
          + (f"; only in the model: {', '.join(missing)}" if missing else "")
          + (f"; only in the game: {', '.join(extra)}" if extra else "")
          + ("" if order else "; listed in a different order"))

    reader = ReadOnlyApi(live.key)
    found = {}
    for name, api in (("game", reader), ("model", model)):
        c = tj.TrainControls(api)
        c.train_id = train
        c.detect()
        found[name] = c.describe()
    print("\nWhat the bridge picks:")
    print("  game: ", found["game"])
    print("  model:", found["model"], "" if found["game"] == found["model"] else "   <-- DIFFERENT")

    print("\nEverything the bridge read, game vs model:")
    differ = 0
    for path in dict.fromkeys(reader.paths):
        if path.startswith("list/") or any(path.endswith(v) for v in VOLATILE):
            continue
        g = live._req("GET", path)
        m = model._req("GET", path)
        gv = g.get("Values") if g.get("Result") == "Success" else None
        mv = m.get("Values") if m.get("Result") == "Success" else None
        if gv is None and mv is not None and "identifier" in mv and mv["identifier"] == "None":
            continue                                  # no identifier either way
        if not same(gv, mv):
            differ += 1
            print(f"  {path[4:]}\n      game:  {gv}\n      model: {mv}")
    print(f"  {differ} difference(s)")

    print("\nWhere each lever is now, and what the game and the model make of it:")
    differ = 0
    for c in model.controls.values():
        if not c.vhid or not c.lever or c.base == "PushButtonComponent":
            continue
        path = f"CurrentDrivableActor/{c.name}"
        try:
            x = live.get_value(path + ".InputValue")
            out = live.get_value(path + ".Function.GetCurrentOutputValue")
        except Exception:
            continue
        if not isinstance(x, (int, float)) or not isinstance(out, (int, float)):
            continue
        mine = c.output(x)
        flag = "" if abs(mine - out) < 1e-3 else "   <-- DIFFERENT"
        differ += bool(flag)
        print(f"  {c.name:<40} input {x:+.4f}  game {out:+.4f}  model {mine:+.4f}{flag}")
    print(f"  {differ} difference(s)")


if __name__ == "__main__":
    if "--live" in sys.argv[1:]:
        compare_live()
    else:
        check_all([a for a in sys.argv[1:] if not a.startswith("-")])
