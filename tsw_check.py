"""
Checks the joystick bridge against every train installed, without loading them in the game.

Each rail vehicle's cab controls are read from Train Sim World's .pak files (tsw_paks.py). A model of those
controls stands in for the game's API and the bridge's own code (tsw_joystick.py) is run against it, just as
in the cab: it finds the controls, then the stick is pushed to full power, centred, pulled to full brake and
centred again, and the reverser slider is tried. What each one did is reported, with anything doubtful
flagged. Locos with a cab at each end are checked from each cab.

The model comes from each control's settings in the game files: its notches, its input -> output table and
its gated notches (ones a handle has to be moved into on its own, e.g. Class 331 Coasting). It can't know
about interlocks a train applies itself (e.g. no power with the reverser in Neutral), so it assumes the cab is
set up and ready to drive.

Usage:
    python tsw_check.py              check every train; writes train_check_report.txt
    python tsw_check.py 331 802      only trains whose name contains one of these
    python tsw_check.py --no-files   as the bridge works when it can't read the game files (estimating)
    python tsw_check.py --live       compare the model with the game for the train you're in (read-only)
"""

import os
import re
import sys
import time as real_time
from collections import defaultdict

import tsw_handles
import tsw_joystick as tj
from tsw_handles import enum
from tsw_paks import GameFiles

REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_check_report.txt")

# a vehicle is checked if one of its controls has one of these game input identifiers
DRIVING_IDS = {"throttle", "automaticbrake", "reverser"}
ERROR = {"Result": "Error", "Message": "Node failed to return valid data."}

STICK_MOVE_SECONDS = 0.8         # how long the simulated hand takes to move the stick to a new place
STICK_HOLD_SECONDS = 0.7         # and how long it then holds it there
SHORT_OF_FULL = 0.97             # full power / full brake reaching less than this of the handle's is flagged


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
class Control(tsw_handles.Control):
    """A cab control as the game would behave (see tsw_handles), answering the game's API."""

    interacting = False                  # held in place, as by a hand (the game's Interacting flag)

    def __init__(self, comp, side=None):
        super().__init__(comp, side)
        if self.base == "PushButtonComponent":
            self.value = self.rest()

    def rest(self):
        """Where a button rests: the game holds some down (DSD pedals; Class 142 at its maximum, the Class 350
        one counting backwards, at 1)."""
        return 1.0 if self.reversed_button else self.hi if self.props.get("bDefaultToPressed") else self.lo

    def rests_down(self):
        return self.reversed_button or bool(self.props.get("bDefaultToPressed"))

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
            "Function.GetDefaultInputValue": lambda: {"ReturnValue": self.rest() if not self.lever
                                                      else float(self.props.get("DefaultInputValue", 0.0))},
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

    def __init__(self, train, components, cab=None):
        """cab: the cab in use on a train with one at each end, "Front" / "Back" (None = neither)."""
        super().__init__()
        self.key = "model"
        self.train = train
        self.cab = cab
        sides = tsw_handles.cab_sides(components)
        self.controls = {name: Control(c, sides.get(name)) for name, c in components.items()}
        self.moves = []                  # (control, input) after every move, in order
        self.sent = []                   # (control, input sent) for every move, before any notch pulls it in

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
            if endpoint == "Function.GetActiveCabSide":
                return {"Result": "Success", "Values": {"Front": self.cab == "Front", "Back": self.cab == "Back"}}
            return dict(ERROR)
        c = self.controls.get(node[len("CurrentDrivableActor/"):]) if node.startswith("CurrentDrivableActor/") \
            else None
        if c is None:
            return dict(ERROR)
        if kind == "set" and endpoint == "Interacting" and c.vhid:
            c.interacting = float(params["Value"]) >= 0.5
            return {"Result": "Success"}
        if kind == "set":
            if endpoint != "InputValue" or not c.vhid:
                return dict(ERROR)
            self.sent.append((c, float(params["Value"])))
            c.move(self.sent[-1][1])
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


class Result:
    def __init__(self, train, pack, cab=None):
        self.train, self.pack, self.cab = train, pack, cab
        self.lines = []                  # what each control did
        self.problems = []               # the bridge does something wrong on this train
        self.doubts = []                 # couldn't be checked properly, or the model may be off

    @property
    def verdict(self):
        return "PROBLEM" if self.problems else "CHECK" if self.doubts else "OK"

    @property
    def name(self):
        return short(self.train) + (f" ({self.cab.lower()} cab)" if self.cab else "")


def check_train(train, package, files, cab=None):
    """cab: on a train with a cab at each end, the one it's driven from ("Front" / "Back")."""
    result = Result(train, files.pack_of(package), cab)
    clock = bridge_patched()
    api = ModelApi(train, files.components(train, package), cab)
    messages = []
    controls = tj.TrainControls(api, log=messages.append)
    controls.train_id = train
    controls.detect()
    model = api.controls
    if cab:
        check_cab(result, controls, model, cab)
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
    snapshots, held = {}, {}
    # a handle that starts beyond emergency (1972 Stock: Shutdown) has to pass through it, as a driver would
    beyond = {c.name for c in (tm, bm) if c and any(w in (c.zone(c.value)[0] or "").lower()
                                                    for w in ("shutdown", "shut down"))}
    for name, y in (("full power", 1.0), ("centre", 0.0), ("full brake", -1.0), ("centre again", 0.0),
                    ("half power", 0.5), ("half brake", -0.5), ("centre at the end", 0.0)):
        start = len(api.moves)
        driver.move_to(y)
        snapshots[name] = {c.name: c.value for c in (tm, bm) if c}
        held[name] = bm.interacting if bm else False
        for c, x in api.moves[start:]:
            if c in (tm, bm) and c.zone(x)[1]:
                text = f"{c.name} went into emergency ({c.describe(x)}) while moving the stick to {name}"
                if name == "full power" and c.name in beyond:
                    result.doubts.append(f"{text}, on its way from where the train starts it "
                                         f"({c.zone(c.value)[0]} is past Emergency)")
                else:
                    result.problems.append(text)
                break

    def on(c, name):
        return snapshots[name].get(c.name, c.value)

    if t:
        kind = "power+brake lever" if t.brake_end is not None and not b else "throttle"
        result.lines.append(f"stick: {kind} {t.name}" + (f", brake {b.name}" if b else "")
                            + (" (Release / Hold / Apply, a third of the travel back each)" if b and b.positions
                               else ""))
    elif b:
        result.lines.append(f"stick: brake {b.name} (no throttle)")
    for name in snapshots:
        parts = [f"{c.name} {c.describe(on(c, name))}" for c in (tm, bm) if c]
        result.lines.append(f"  {name:<18} {'; '.join(parts)}")

    if tm:
        power_end = t.power_end
        far = tm.hi if power_end >= t.neutral else tm.lo
        xs = tm.reachable(t.neutral, far)
        best = max(xs, key=lambda x: abs(tm.output(x) - tm.output(t.neutral)))
        got = on(tm, "full power")
        span = tm.output(best) - tm.output(t.neutral)
        if span and (tm.output(got) - tm.output(t.neutral)) / span < SHORT_OF_FULL:
            result.problems.append(f"full power on {t.name} only reaches {tm.describe(got)}; the handle goes "
                                   f"to {tm.describe(best)}")
        combined = t.brake_end is not None and not b
        lowest = [x for x in tm.reachable(tm.lo, tm.hi)
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

    if b and b.positions:
        # the game springs a Release / Hold / Apply valve back to Hold unless it's held (Class 66)
        for name, want in (("full brake", True), ("half brake", False), ("centre", True)):
            if held[name] != want:
                result.problems.append(f"stick at {name}: {b.name} is {'not ' if want else ''}held in "
                                       f"{bm.describe(on(bm, name))}")
        controls.let_go()
        if bm.interacting:
            result.problems.append(f"{b.name} is still held after the bridge lets go")
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


def check_cab(result, controls, model, cab):
    """Everything the bridge works has to be in the cab the train is driven from: the other cab's handles do
    nothing."""
    used = [controls.throttle, controls.brake, controls.reverser, controls.aws, controls.alerter] + \
           [getattr(controls, f"door_{a}_{s}") for a in ("open", "close") for s in ("left", "right")]
    names = [b.name for x in used if x for b in getattr(x, "buttons", [x])]
    away = [n for n in names if model[n].side not in (None, cab)]
    if away:
        result.problems.append(f"driving from the {cab.lower()} cab, the bridge works the other cab's "
                               f"{', '.join(away)}")


def check_brake(result, m, release, end, got):
    """Full stick should give the strongest braking the handle has short of emergency: looking from the
    released position toward the end of the handle, up to the first emergency place, or one past full
    service (e.g. Handle Off, Suppression, Shutdown)."""
    xs = m.reachable(release, end)
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
    got, between = [], {}
    m, sent = model[rev.name], controls.api.sent
    for position in ("forward", "neutral", "reverse", "neutral", "forward"):
        start = len(sent)
        rev.set(position)
        got.append((position, rev.position()))
        # left between notches, the game may not pull it into one: the Class 150 reverser stayed at 0.875,
        # between its two Forward notches. (The end of the handle's travel holds it too, and values are
        # sent to 4 decimals, so 2/3 goes as 0.6667.)
        last = [x for c, x in sent[start:] if c is m][-1:]
        rests = [(a, b) for a, b, _ in m.notches] + [(m.lo, m.lo), (m.hi, m.hi)]
        if last and m.notches and not any(a - 1e-4 <= last[0] <= b + 1e-4 for a, b in rests):
            between.setdefault(position, last[0])
    wrong = [f"{want} gave {have}" for want, have in got if want != have]
    wrong += [f"{want} sent to {x:.3g}, between notches" for want, x in between.items()]
    result.lines.append(f"slider: reverser {rev.name}: " + ("Forward / Neutral / Reverse all right"
                                                            if not wrong else "; ".join(wrong)))
    if wrong:
        result.problems.append(f"reverser {rev.name}: " + "; ".join(wrong))


def check_buttons(result, controls, model):
    found = []
    for name, label in (("aws", "AWS"), ("alerter", "alerter")):
        button = getattr(controls, name)
        found.append(f"{label} {button}" if button else f"no {label}")
    doors = [f"{a} {s}" for a in ("open", "close") for s in ("left", "right")
             if getattr(controls, f"door_{a}_{s}")]
    found.append(f"doors {', '.join(doors)}" if doors else "no door buttons")
    result.lines.append("buttons: " + "; ".join(found))

    ids = {c.ident.lower().replace("_", "") for c in model.values() if c.enabled}
    if controls.aws is None and ids & set(tj.AWS_IDS):
        result.problems.append("the train has an AWS acknowledge button but the bridge didn't find it")
    if controls.alerter is None and ids & set(tj.ALERTER_IDS):
        result.problems.append("the train has an alerter / DSD / SIFA but the bridge didn't find it")
    a = controls.alerter
    if a and model[a.name].rests_down():
        # a DSD pedal the game holds down, where the train has a button to acknowledge with (Class 142, 47)
        resets = [c.name for c in model.values() if c.enabled and c.base == "PushButtonComponent"
                  and not c.rests_down() and c.ident.lower().replace("_", "") in tj.ALERTER_IDS
                  and (controls.cab is None or c.side in (None, controls.cab))]
        if resets:
            result.problems.append(f"alerter: the bridge works {a.name}, which the game holds down, rather "
                                   f"than the button {resets[0]}")

    # the game springs a button straight back unless it's held, as by a hand
    for name, button in controls.buttons().items():
        parts = [model[b.name] for b in getattr(button, "buttons", [button])]
        if not all(m.vhid for m in parts):
            result.problems.append(f"{name}: {button} isn't a control in the cab ({parts[0].cls}), so the "
                                   f"joystick button does nothing")
            continue
        controls.press(name, True)
        if any(not m.interacting or abs(m.value - m.rest()) < 0.5 for m in parts):
            result.problems.append(f"{name}: {button} isn't held down while the joystick button is")
        controls.press(name, False)
        if any(m.interacting or abs(m.value - m.rest()) > 1e-6 for m in parts):
            result.problems.append(f"{name}: {button} isn't back where it rests after the joystick button")
        controls.press(name, True)
        controls.let_go()
        if any(m.interacting or abs(m.value - m.rest()) > 1e-6 for m in parts):
            result.problems.append(f"{name}: {button} is still pressed after the bridge lets go")


# ---------------------------------------------------------------- report
CAUSES = [   # (what the problem is, pattern in the problem text), most specific first
    ("Works the other cab's controls (train with a cab at each end)", r"the other cab's"),
    ("Reverser slider does nothing: no position the bridge reads as Forward / Neutral / Reverse",
     r"slider does nothing"),
    ("Reverser not found by the bridge", r"reverser .* not found"),
    ("Reverser lands in the wrong position", r"reverser .* gave"),
    ("Reverser left between notches", r"reverser .* between notches"),
    ("Handle goes into emergency", r"went into emergency"),
    ("Stick centred stops the engine", r"stops the engine"),
    ("Stick centred doesn't give Off (power or brake applied)", r"gives power|not Off|stick at .*: .* is at"),
    ("Stick centred leaves the train brake applied", r"not released"),
    ("Full stick back gives less than full service braking", r"full brake only reaches"),
    ("Full stick forward gives less than full power", r"full power only reaches"),
    ("Stick does nothing / can't brake", r"does nothing: no|can't brake"),
    ("AWS / alerter button not found", r"didn't find"),
    ("Alerter worked through a DSD pedal the game holds down, not the train's reset button",
     r"which the game holds down"),
    ("Cab button not held while pressed, or left held", r"held down while|where it rests|still pressed after"),
    ("Joystick button works something that isn't a cab control", r"isn't a control in the cab"),
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


def cabs(files, train, package):
    """The cabs to check a train from: on one with a cab at each end both, "Front" and "Back"; else [None]."""
    comps = files.components(train, package)
    sides = tsw_handles.cab_sides(comps)
    found = tj.driving_cabs({n: tsw_handles.Control(c, sides.get(n)) for n, c in comps.items()})
    return ["Front", "Back"] if len(found) > 1 else [None]


def short(train):
    return re.sub(r"^RVM_|_C$", "", train)


def write_report(results, files, seconds):
    by_pack = defaultdict(list)
    for r in results:
        by_pack[r.pack].append(r)
    counts = {v: sum(r.verdict == v for r in results) for v in ("OK", "CHECK", "PROBLEM")}
    vehicles, two_cabs = len({r.train for r in results}), len({r.train for r in results if r.cab})
    out = ["Joystick bridge check against the installed trains",
           f"{real_time.strftime('%Y-%m-%d %H:%M')}, {vehicles} drivable vehicles in {len(by_pack)} packs, "
           f"{seconds:.0f} s",
           f"OK {counts['OK']}   CHECK {counts['CHECK']}   PROBLEM {counts['PROBLEM']}"
           + (f"   ({two_cabs} vehicles with a cab at each end are checked from each cab)" if two_cabs else ""),
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
            for key in sorted(groups, key=lambda k: (order[k[0]], groups[k][0].name)):
                verdict, problems, doubts, lines = key
                names = ", ".join(r.name for r in groups[key])
                out.append(f"\n[{verdict}] {names}")
                out += [f"  ! {p}" for p in problems] + [f"  ? {d}" for d in doubts]
                out += [f"  {line}" for line in lines]
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def check_all(filters, use_files=True):
    start = real_time.time()
    print("Reading the game files...", flush=True)
    files = GameFiles()
    if use_files:
        tj.use_game_files(files)         # the bridge looks each train up in them, as in the cab
    else:
        tj.USE_GAME_FILES = False
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
            ends = cabs(files, train, package)
        except Exception as e:
            r = Result(train, files.pack_of(package))
            r.doubts.append(f"couldn't be checked: {type(e).__name__}: {e}")
            results.append(r)
            continue
        for cab in ends:
            try:
                results.append(check_train(train, package, files, cab))
            except Exception as e:
                r = Result(train, files.pack_of(package), cab)
                r.doubts.append(f"couldn't be checked: {type(e).__name__}: {e}")
                results.append(r)
    print("\r" + " " * 80 + "\r", end="")
    write_report(results, files, real_time.time() - start)
    counts = {v: sum(r.verdict == v for r in results) for v in ("OK", "CHECK", "PROBLEM")}
    print(f"{len({r.train for r in results})} drivable vehicles, {len(results)} cabs: OK {counts['OK']}, "
          f"CHECK {counts['CHECK']}, PROBLEM {counts['PROBLEM']}")
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
    tj.use_game_files(files)
    trains = files.vehicle_classes()
    if train not in trains:
        print("This train isn't in the game files (or isn't a rail vehicle blueprint).")
        return
    tj._calibration = {}
    tj.save_calibration = lambda key, value: None
    cab = tj.TrainControls(live).active_cab()
    if cab:
        print("Cab in use:", cab.lower())
    model = ModelApi(train, files.components(train, trains[train]), cab)

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
        check_all([a for a in sys.argv[1:] if not a.startswith("-")], use_files="--no-files" not in sys.argv)
