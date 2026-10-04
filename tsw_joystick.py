"""
Train Sim World 7 - joystick throttle/brake bridge.

Joystick Y axis:
    push forward  -> throttle (power)
    centre        -> neutral (throttle 0, brake released)
    pull back     -> train brake (a Release / Hold / Apply valve, Class 66: a third of the travel each,
                     held in Release or Apply for as long as the stick is there)

Base slider (Z axis) -> reverser: + end Forward, middle Neutral, - end Reverse
    (only moved when the train is stopped, never to Off)

Button 1 (trigger)   -> AWS acknowledge (held for as long as you hold the trigger)
Button 2             -> alerter / DSD / SIFA acknowledge (held for as long as you hold the button)
Buttons 7 / 8        -> open the left / right doors (never while the train is moving)
Buttons 9 / 10       -> close the left / right doors

Twist (Z rotation)   -> look left / right; centre the twist and the view returns to straight ahead
    (emulated mouse movement, only while Train Sim World is the active window)

Talks to TSW's External Interface API (launch the game with -HTTPAPI).
The stick only sends values when you move it, so the keyboard keeps working.
Each train's handles are looked up in the game's own files (found through Steam) for where Off, full power,
full brake and the notches really are; if they can't be read, those places are estimated instead.
On locos with a cab at each end (Class 66, 47, BR 101...) the cab in use is the one worked; change ends and
the bridge follows within a couple of seconds.

Usage:
    python tsw_joystick.py              run the bridge
    python tsw_joystick.py --list       print the current train's controls (debug)
    python tsw_joystick.py --axes       show live joystick axis values (debug)
"""

import atexit
import ctypes
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")  # read stick while the game has focus
import pygame

import tsw_handles
from tsw_handles import is_emergency

# ---------------------------------------------------------------- settings
API_URL = "http://127.0.0.1:31270"
KEY_FILE = os.path.join(
    os.path.expanduser("~"), "OneDrive", "Documents", "My Games",
    "TrainSimWorld7", "Saved", "Config", "CommAPIKey.txt",
)
JOYSTICK_NAME_HINT = "extreme"   # prefers a device whose name contains this; else first joystick
Y_AXIS = 1                       # axis index for the stick's forward/back
INVERT_Y = False                 # set True if forward gives brake instead of throttle
DEADZONE = 0.08                  # around centre, treated as neutral
SEND_THRESHOLD = 0.01            # minimum change before sending to the game
STICK_BAND = 0.015               # stick reversals smaller than this are ignored (sensor flicker)
STICK_SMOOTHING = 0.05           # seconds of smoothing on the stick
POLL_HZ = 30
TRAIN_CHECK_SECONDS = 2.0        # how often to check whether you changed train

USE_REVERSER = True
REVERSER_AXIS = 3                # Extreme 3D Pro base slider: + end Forward, middle Neutral, - end Reverse
REVERSER_INVERT = False
REVERSER_MAX_SPEED = 0.3         # m/s; the reverser is never moved faster than this (~1 km/h)

AWS_BUTTON = 0                   # joystick button for AWS acknowledge (0 = trigger, labelled "1")
ALERTER_BUTTON = 1               # joystick button for alerter / DSD / SIFA acknowledge (labelled "2")
DOOR_OPEN_LEFT_BUTTON = 6        # base buttons labelled 7 / 8: open the left / right doors
DOOR_OPEN_RIGHT_BUTTON = 7
DOOR_CLOSE_LEFT_BUTTON = 8       # base buttons labelled 9 / 10: close the left / right doors
DOOR_CLOSE_RIGHT_BUTTON = 9
DOOR_MAX_SPEED = 0.3             # m/s; doors are never opened faster than this (~1 km/h)

USE_GAME_FILES = True            # look each train's controls up in the game's files (exact places, no guessing)

USE_LOOK = True
LOOK_AXIS = 2                    # stick twist (Z rotation): look left / right
LOOK_INVERT = False
LOOK_DEADZONE = 0.20             # twist inside this is "centred" and the view returns to straight ahead
LOOK_MAX_ANGLE = 90.0            # degrees left / right at full twist
LOOK_SMOOTHING = 0.25            # seconds for the view to ease most of the way to where the twist points

# Primary detection: the game's own input identifier on each control (same across trains).
THROTTLE_IDS = ["throttle", "mastercontroller", "combinedthrottle"]   # substring match
BRAKE_IDS = ["automaticbrake", "trainbrake"]                           # substring match

# Fallback detection by control name, case-insensitive, first match wins.
COMBINED_NAMES = ["throttlebrake", "throttleandbrake", "mastercontroller", "combinedthrottlebrake",
                  "powerbrake", "power_brake", "tbc", "controller(lever)"]
THROTTLE_NAMES = ["throttle", "powerhandle", "power"]
BRAKE_NAMES = ["trainbrake", "train_brake", "automaticbrake", "brakehandle", "stepbrake", "brake"]
NEUTRAL_LABELS = ["off", "neutral", "coast", "coasting", "idle", "n", "0"]

AWS_IDS = ["awsreset", "awsacknowledge"]      # compared with "_" removed, lower case
AWS_NAME_SKIP = ["isolat", "cover", "cutout", "fault", "sunflower", "mcb", "service", "test"]

# Vigilance: the game's "Alerter" input covers the US alerter, UK DSD / vigilance and German SIFA
ALERTER_IDS = ["alerter", "alerterreset", "vigilance", "vigilancereset", "dsd", "sifa", "sifareset",
               "deadman"]                     # compared with "_" removed, lower case
ALERTER_NAMES = ["alerter", "vigilance", "dsd", "sifa", "deadman"]
ALERTER_NAME_SKIP = ["isolat", "cover", "cutout", "cut-out", "fault", "mcb", "_cb", "service", "test", "device",
                     "light", "lamp"]   # LIRR M7 "Alerter_Cut-out" switch

# Door buttons are found by name (Class 350's have no input identifier). Words in the name starting with
# these are other doors' buttons (guard's panel, the buttons on each door, the cab door...) or not buttons.
DOOR_SKIP = ["guard", "ext", "int", "inner", "local", "gangway", "nose", "vestibule", "isolat", "cover",
             "light", "lamp", "indicat", "fault", "test", "emerg", "egress", "toilet", "luggage", "lock"]

PRESS_MESSAGES = {"aws": "AWS acknowledged", "alerter": "Alerter acknowledged",
                  "door_open_left": "Opening the left doors", "door_open_right": "Opening the right doors",
                  "door_close_left": "Closing the left doors", "door_close_right": "Closing the right doors"}

REVERSER_IDS = ["reverser"]
REVERSER_LABELS = {"forward": ["forward", "fwd", "fw", "f", "ahead"],
                   "neutral": ["neutral", "n", "mid", "centre", "center"],
                   "reverse": ["reverse", "rev", "r", "backward", "back"]}
# used for Neutral on reversers that have none, in order of preference: positions with no traction, and the
# engine kept running first (on BR diesels Off stops the engine, Engine Only is their neutral)
NEUTRAL_STAND_INS = ["engine only", "on", "0", "off"]
EXCLUDE = ["dynamic", "independent", "loco", "emergency", "park", "handbrake", "release", "snow",
           "bail", "reverser", "horn", "light", "wiper", "door", "sander", "pantograph",
           # circuit breakers, isolation switches, covers etc. are not the driving levers
           "mcb", "isolat", "cutout", "cover", "button", "switch", "cock", "hose", "lock"]
# game identifiers of brakes that are never the train brake
OTHER_BRAKE_IDS = ["dynamic", "independent", "loco", "emergency", "park", "handbrake"]


# ---------------------------------------------------------------- API
class TSWApi:
    def __init__(self):
        self.key = None

    def load_key(self):
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                self.key = f.read().strip()
        except OSError:
            self.key = None
        return self.key

    def _req(self, method, path, params=None):
        url = API_URL + "/" + urllib.parse.quote(path, safe="/().")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, headers={"DTGCommKey": self.key or ""})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=1.0) as r:
                    return json.loads(r.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, ConnectionError):
                if attempt:                # the game occasionally drops a connection: retry once
                    raise
                time.sleep(0.05)

    def get(self, path):
        return self._req("GET", "get/" + path)

    def set(self, path, value):
        return self._req("PATCH", "set/" + path, {"Value": f"{value:.4f}"})

    def list(self, path=""):
        return self._req("GET", "list/" + path)

    def get_value(self, path):
        data = self.get(path)
        if data.get("Result") != "Success":
            return None
        values = data.get("Values") or {}
        for v in values.values():
            return v
        return None


def node_names(listing):
    names = []
    for n in listing.get("Nodes", []) or []:
        name = n.get("NodeName") or n.get("Name")
        if name:
            names.append(name)
    return names


def pick_all(names, wanted, exclude):
    """Names matching any wanted word (in order of preference) and none of the excluded words."""
    lowered = [(n, n.lower()) for n in names]
    found = []
    for w in wanted:
        for orig, low in lowered:
            if w in low and not any(x in low for x in exclude) and orig not in found:
                found.append(orig)
    return found


def pick(names, wanted, exclude):
    found = pick_all(names, wanted, exclude)
    return found[0] if found else None


def is_brake_label(label):
    return "brake" in label or is_emergency(label) or label.strip().startswith("b")


# words in the names of places on a handle (lower case)
ENGINE_STOP_WORDS = ("stop", "shutdown", "shut down")
RUNNING_WORDS = [("running", "driving", "drive", "run"), ("release",), ("charge",)]   # preferred first
PAST_SERVICE_WORDS = ("shutdown", "shut down", "cut", "isol", "suppress", "handle off", "neutral")


def is_idle_label(label):
    """A place with no power and no braking: Off, Coasting, Idle, Neutral, Closed, Off And Release..."""
    s = label.strip()
    return (s in NEUTRAL_LABELS or s.startswith(("off", "coast", "idl", "neutral", "closed"))) and \
        not any(w in s for w in ("brake", "power", "%"))


def is_braking_label(label):
    """A named place where the train brakes (not emergency), e.g. "B2", "Max Brake", "Full Service", "Apply"."""
    return (is_brake_label(label) or "service" in label or "appl" in label) and not is_emergency(label)


CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lever_calibration.json")
_calibration = None


def load_calibration():
    """Off positions found on irregular levers, per train, so they're only searched for once."""
    global _calibration
    if _calibration is None:
        try:
            with open(CALIBRATION_FILE, encoding="utf-8") as f:
                _calibration = json.load(f)
        except (OSError, ValueError):
            _calibration = {}
    return _calibration


def save_calibration(key, value):
    load_calibration()[key] = round(value, 4)
    try:
        with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
            json.dump(_calibration, f, indent=2)
    except OSError:
        pass


_game_files = None   # Future of (tsw_paks.GameFiles, {train class: package}), read in the background


def load_game_files():
    """Start reading the game's file index in the background (a few seconds, once a run), so each train's
    controls can be looked up there when you get in a cab. Returns the Future, or None when not used."""
    global _game_files
    if _game_files is None and USE_GAME_FILES:
        _game_files = ThreadPoolExecutor(max_workers=1).submit(_open_game_files)
    return _game_files


def _open_game_files():
    import tsw_paks
    files = tsw_paks.GameFiles()
    return files, files.vehicle_classes()


def use_game_files(files):
    """Use game files that have already been read (the offline check reads them itself)."""
    global _game_files
    _game_files = Future()
    _game_files.set_result((files, files.vehicle_classes()))


def train_handles(train, wait=30.0):
    """{component name: tsw_handles.Control} for a train, from the game files; {} when they can't be read
    (game not found, files still loading after `wait` seconds, or the train isn't in them)."""
    future = load_game_files()
    if future is None or not train:
        return {}
    try:
        files, classes = future.result(timeout=wait)
        package = classes.get(train)
        if package is None:
            return {}
        comps = files.components(train, package)
        sides = tsw_handles.cab_sides(comps)
        return {name: tsw_handles.Control(c, sides.get(name)) for name, c in comps.items()}
    except Exception:
        return {}


def driving_cabs(handles):
    """The ends of a train that have a cab with driving controls in it, from train_handles(): {"Front"}, or
    {"Front", "Back"} on a train with a cab at each end. Empty when the game files weren't read."""
    return {m.side for m in handles.values()
            if m.side and any(k in m.ident.lower() for k in THROTTLE_IDS + BRAKE_IDS + REVERSER_IDS)}


class Lever:
    """One controllable lever, its safe input range and (for combined levers) neutral point."""

    def __init__(self, api, name, ident="", train=None, role="throttle", model=None):
        """role: "throttle" (incl. a combined power/brake handle), "brake" (a separate train brake) or
        "reverser". model: the handle's settings from the game files (tsw_handles.Control), if read."""
        self.api = api
        self.name = name
        self.ident = ident or ""
        self.path = f"CurrentDrivableActor/{name}.InputValue"
        self.lo, self.hi = self._range()
        self.note = ""
        brake = role == "brake"
        self.zones = self._zones(brake)
        self.safe_lo, self.safe_hi = self._exclude_emergency()
        self.combined = ("brake" in name.lower() or "brake" in self.ident.lower()
                         or any(is_brake_label(z[2]) for z in self.zones)) \
            and not any(b in self.ident.lower() for b in BRAKE_IDS)
        self.neutral = self.safe_lo
        self.power_end = self.safe_hi
        self.brake_end = None
        self.neutral_verified = False     # Off position confirmed with the game (combined levers)
        self.brake_end_verified = False   # full brake checked with the game (combined levers, train brakes)
        self._calibration_key = f"{train}|{name}" if train else None
        if self.combined:
            self._find_neutral()
            learned = load_calibration().get(self._calibration_key) if self._calibration_key else None
            if isinstance(learned, (int, float)) and self.safe_lo <= learned <= self.safe_hi:
                self.neutral, self.neutral_verified = float(learned), True
                self.note = re.sub(r"neutral [-\d.e]+ from [^,]*",
                                   f"neutral {self.neutral:.3g} (learned earlier)", self.note)
        elif brake:
            self.brake_end = self.safe_hi     # a separate train brake: released at its low end
        if self.brake_end is not None:
            end = (load_calibration().get(f"{self._calibration_key}|brake_end")
                   if self._calibration_key else None)
            if isinstance(end, (int, float)) and self.lo <= end <= self.hi:
                self._set_brake_end(float(end))
        self.notches = self._notch_positions()
        self._notches_confirmed = set()   # notch positions the game has shown give their output
        self._uneven = False              # notched, but not evenly spaced as worked out (seen in the game)
        self._notch = None                # index of the notch last sent
        self._last_sent = (None, 0.0)     # (value, time)
        self.model = None                 # the handle's settings from the game files, when read
        self.exact_places = False         # its named places located from them (self.zones)
        self.from_files = False           # its places (Off, full power / brake) taken from them
        self._exact_notches = False       # notch positions read from the game files
        self.positions = None             # a Release / Hold / Apply valve's three positions (see _valve)
        self._position = None             # index of the one last sent
        self.held = False                 # held in place in the game, as a hand would (see _set_valve)
        if model is not None and model.lever and model.table_known and not model.custom:
            self._from_game_files(model, role)

    def _from_game_files(self, model, role):
        """Use the handle's settings from the game files instead of estimating: every named place where it
        really is on the handle, and Off, full power and full brake at places the handle stays at by itself
        (in a notch), so a notch never pulls it somewhere else. Nothing then needs checking with the game.
        Places the files can't settle (no names) keep their estimates."""
        self.model = model
        if role == "brake" and self._valve(model):
            return
        resting = model.resting()
        # each named place where it really is: one zone per stretch of the handle (Class 142 has Off at
        # both ends), in the order the game lists the names
        stretches = []
        for x in resting:
            label = model.name_at(x)
            if label and stretches and stretches[-1][2] == label.lower():
                stretches[-1][1] = x
            elif label:
                stretches.append([x, x, label.lower()])
        if not stretches:
            return
        order = {n.lower(): i for i, n in enumerate(model.places())}
        self.zones = [(a, b, label, "Input") for a, b, label in sorted(stretches, key=lambda z: order[z[2]])]
        self.exact_places = True
        if role == "reverser":
            return
        # full power / brake can also be where the stick holds the handle without a notch pulling it away
        # (Bnrdzf: Run Up, at the end of the travel past the last notch)
        holdable = sorted(set(resting) | set(model.reachable(model.lo, model.hi)))
        name = lambda x: (model.name_at(x) or "").lower()
        emergency = [x for x in resting if is_emergency(name(x))]
        if role == "brake":
            # released: the train's running position (Running, Driving) rather than one that overcharges the
            # brake pipe (German valves: Quick Release), furthest from emergency
            usable = [x for x in resting if not is_emergency(name(x))
                      and not any(w in name(x) for w in PAST_SERVICE_WORDS + ENGINE_STOP_WORDS)]
            for words in RUNNING_WORDS:
                release = [x for x in usable if any(w in name(x) for w in words)]
                if release:
                    break
            else:
                return
            far = lambda x: min((abs(x - e) for e in emergency), default=abs(x - self.lo))
            start = max(release, key=far)
            way = (1.0 if sum(emergency) / len(emergency) > start else -1.0) if emergency else 1.0
            end = self._strongest(model, start, way, holdable)
            if end is None:
                return
            self.neutral, self.brake_end = start, end
            self.note = f"from game files: released {start:.3g}, full brake {end:.3g}"
        elif self.combined:
            idle = [x for x in resting if is_idle_label(name(x))]
            braking = [x for x in resting if is_braking_label(name(x))] or emergency
            if not idle or not braking:
                return
            start = min(idle, key=lambda x: abs(model.output(x)))
            way = -1.0 if sum(braking) / len(braking) < start else 1.0
            end = self._strongest(model, start, way, holdable)
            power = self._strongest(model, start, -way, holdable)
            if end is None or power is None:
                return
            self.neutral, self.brake_end, self.power_end = start, end, power
            self.note = f"from game files: Off {start:.3g}, full power {power:.3g}, full brake {end:.3g}"
        else:
            # a throttle on its own: idle where it gives no power (Class 40: On, not Off; SD40: Idle, not Stop)
            usable = [x for x in resting if not is_emergency(name(x))
                      and not any(w in name(x) for w in ENGINE_STOP_WORDS)]
            if not usable:
                return
            start = min(usable, key=lambda x: (abs(model.output(x)) > 1e-6, not is_idle_label(name(x)),
                                               model.output(x)))
            power = max((x for x in holdable if not is_emergency(name(x))), key=model.output)
            if model.output(power) <= model.output(start):
                return
            self.neutral, self.power_end = start, power
            self.note = f"from game files: idle {start:.3g}, full power {power:.3g}"
        ends = [v for v in (self.neutral, self.power_end if role != "brake" else None, self.brake_end)
                if v is not None]
        self.safe_lo, self.safe_hi = min(ends), max(ends)
        self.neutral_verified = self.brake_end_verified = self.from_files = True
        detents = sorted({a for a, _, _ in model.notches})
        if len(detents) >= 2 and all(b - a < 1e-9 for a, b, _ in model.notches):   # no smooth stretch
            ends = {v for v in (self.neutral, self.power_end, self.brake_end) if v is not None}
            self.notches = sorted({x for x in detents if self.safe_lo - 1e-9 <= x <= self.safe_hi + 1e-9}
                                  | ends) or None
            self._exact_notches = True
        else:
            self.notches = None

    def _valve(self, model):
        """A brake valve with just Release, Hold and Apply on it (Class 66): the brakes go on for as long as
        the handle is in Apply, come off in Release and stay as they are in Hold. The stick picks one of the
        three, a third of its travel back each: the handle goes right into Release or Apply, and into Hold's
        notch, rather than sliding through them. Returns False for any other brake (an Apply with an amount
        is a graduated brake: BR 442, Talent 2)."""
        stretches = []                             # [label, positions], along the handle
        for x in model.reachable(model.lo, model.hi):
            label = (model.name_at(x) or "").lower()
            if stretches and stretches[-1][0] == label:
                stretches[-1][1].append(x)
            else:
                stretches.append([label, [x]])
        if len(stretches) != 3:
            return False
        if "appl" in stretches[0][0]:
            stretches.reverse()                    # the handle runs Apply .. Release
        (release, rx), (hold, hx), (apply, ax) = stretches
        if not ("release" in release and any(w in hold.split() for w in ("hold", "lap"))
                and "appl" in apply and "{" not in apply and not is_emergency(apply)):
            return False
        notch = [x for x in model.resting() if x in hx]
        held = min(notch or hx, key=lambda x: abs(x - (hx[0] + hx[-1]) / 2))
        self.positions = [max(rx, key=lambda x: abs(x - held)), held, max(ax, key=lambda x: abs(x - held))]
        self.zones = [(min(xs), max(xs), label, "Input") for label, xs in stretches]
        self.exact_places = True
        self.neutral, self.brake_end = self.positions[0], self.positions[-1]
        self.safe_lo, self.safe_hi = min(self.positions), max(self.positions)
        self.notches, self._exact_notches = sorted(self.positions), True
        self.neutral_verified = self.brake_end_verified = self.from_files = True
        self.note = "from game files: Release {:.3g} / Hold {:.3g} / Apply {:.3g}, a third each".format(
            *self.positions)
        return True

    def valve_position(self, frac):
        """Where a Release / Hold / Apply valve goes for frac (0..1) of the stick's travel back: a third each,
        with a margin so a stick resting on a boundary can't flip between two."""
        n, k = len(self.positions), self._position
        if k is None:
            k = min(n - 1, int(frac * n))
        while k + 1 < n and frac > (k + 1) / n + self.VALVE_HYSTERESIS:
            k += 1
        while k > 0 and frac < k / n - self.VALVE_HYSTERESIS:
            k -= 1
        self._position = k
        return self.positions[k]

    VALVE_HYSTERESIS = 0.03   # of the stick's travel back, past a boundary before moving to the next position

    def _set_valve(self, value):
        """The game springs a Release / Hold / Apply valve back to Hold as soon as it's let go (Class 66: in
        about 0.15 s; its keys only apply for as long as they're held). So in Release or Apply the handle is
        held there as a hand would hold it (the game's Interacting flag; seen in the game to keep it in
        place), and let go again in Hold."""
        hold = value != self.positions[1]
        if hold and not self.held:
            self._interact(True)
        self.api.set(self.path, value)
        self._last_sent = (value, time.time())
        if not hold and self.held:
            self._interact(False)

    def _interact(self, on):
        self.held = on
        self.api.set(f"CurrentDrivableActor/{self.name}.Interacting", 1.0 if on else 0.0)

    def let_go(self):
        """Stop holding the handle, so it goes where the game takes it (a valve springs back to Hold). Done
        whenever the bridge stops driving: paused, joystick gone, another train or cab, closed."""
        if not self.held:
            return
        self._last_sent = (None, 0.0)              # held again on the next send
        try:
            self._interact(False)
        except Exception:
            self.held = False

    @staticmethod
    def _strongest(model, start, way, places):
        """From start, the way given, the place with the most power or braking before emergency or a place
        past full service (Suppression, Handle Off, Shutdown...); None if there's none. A notch is preferred
        to a place the stick has to hold the handle at, unless that's stronger (Bnrdzf: Run Up)."""
        base, first = model.output(start), (model.name_at(start) or "").lower()
        resting = set(model.resting())
        best = None
        for x in sorted((x for x in places if (x - start) * way > 1e-9), key=lambda x: abs(x - start)):
            name = (model.name_at(x) or "").lower()
            if name != first and (is_emergency(name) or any(w in name for w in PAST_SERVICE_WORDS)):
                break
            gain = abs(model.output(x) - base) - (abs(model.output(best) - base) if best is not None else -1)
            if gain > 1e-9 or (abs(gain) <= 1e-9 and x in resting and best not in resting):
                best = x
        return best

    NOTCH_HYSTERESIS = 0.2    # past the halfway point by this fraction of a notch before moving to it

    def _notch_positions(self):
        """Input values of the lever's notches, when they can be worked out reliably: the lever maps
        input to output linearly and has exactly one notch per whole output number (e.g. Class 375
        power handle: Emergency, B3..B1, Off, P1..P4 = outputs -4..4, 9 notches). None otherwise
        (e.g. levers with a smooth brake section) - those get sent smooth values instead."""
        count = self._get("Function.GetNotchCount").get("ReturnValue")
        if not isinstance(count, int) or count < 2 or self._out_range is None:
            return None
        out_lo, out_hi = self._out_range
        outputs = list(range(math.ceil(out_lo - 1e-6), math.floor(out_hi + 1e-6) + 1))
        if len(outputs) != count:
            return None
        positions = sorted(self._input_at(o) for o in outputs)
        positions = [p for p in positions if self.safe_lo - 1e-6 <= p <= self.safe_hi + 1e-6]
        return positions if len(positions) >= 2 else None

    def _input_at(self, out):
        """Where on the handle an output value would be if the handle were linear (which way round it runs
        taken into account)."""
        out_lo, out_hi = self._out_range
        f = (out - out_lo) / (out_hi - out_lo)
        return self.lo + ((1.0 - f) if self.out_backwards else f) * (self.hi - self.lo)

    def _output_at(self, x):
        """The output at input x if the handle were linear: the inverse of _input_at."""
        out_lo, out_hi = self._out_range
        f = (x - self.lo) / (self.hi - self.lo)
        return out_lo + ((1.0 - f) if self.out_backwards else f) * (out_hi - out_lo)

    def _snap(self, value):
        """Nearest notch, with hysteresis so a stick resting near a boundary can't flip between two."""
        p = self.notches
        k = self._notch
        if k is None:
            k = min(range(len(p)), key=lambda i: abs(p[i] - value))
        else:
            while k + 1 < len(p) and value > (p[k] + p[k + 1]) / 2 + self.NOTCH_HYSTERESIS * (p[k + 1] - p[k]):
                k += 1
            while k > 0 and value < (p[k - 1] + p[k]) / 2 - self.NOTCH_HYSTERESIS * (p[k] - p[k - 1]):
                k -= 1
        self._notch = k
        return p[k]

    def _get(self, endpoint):
        try:
            return self.api.get(f"CurrentDrivableActor/{self.name}.{endpoint}").get("Values") or {}
        except Exception:
            return {}

    def _zones(self, brake=False):
        """Named notches/zones as (start, end, label, source) in this lever's input units."""
        named = self._get("Property.DisplayInfo").get("namedValues") or []
        out_lo = self._get("Function.GetMinimumOutputValue").get("ReturnValue")
        out_hi = self._get("Function.GetMaximumOutputValue").get("ReturnValue")
        custom = self._get("Property.bCustomOutputValueMapping").get("Value")
        out_ok = (isinstance(out_lo, (int, float)) and isinstance(out_hi, (int, float))
                  and out_hi > out_lo and not custom)
        self._out_range = (float(out_lo), float(out_hi)) if out_ok else None
        span = self.hi - self.lo
        # on some train brakes the output falls as the input rises (Isle of Wight: Release at input 0, output 4;
        # Emergency at input 1, output 0): seen from Emergency having a lower output than Release. (Not from
        # where the handle is: the OBB 1020's brake starts at Shutdown, its highest output.)
        self.out_backwards = False
        if out_ok and brake:
            centres = lambda words: [(float(nv["valueRange"]["min"]) + float(nv["valueRange"]["max"])) / 2
                                     for nv in named if nv.get("valueSource") == "Output"
                                     and "min" in (nv.get("valueRange") or {}) and "max" in nv["valueRange"]
                                     and any(w in str(nv.get("displayName", "")).lower() for w in words)]
            release, emergency = centres(("release", "running")), centres(("emerg",))
            self.out_backwards = bool(release and emergency) and max(emergency) < min(release)

        def convert(v, source):
            if source == "Input":
                return v
            if source == "InputNormalised":
                return self.lo + v * span
            if source == "OutputNormalised":
                return self.lo + ((1.0 - v) if self.out_backwards else v) * span
            if source == "Output" and out_ok:
                f = (v - out_lo) / (out_hi - out_lo)                    # linear estimate
                return self.lo + ((1.0 - f) if self.out_backwards else f) * span
            return None

        zones = []
        self._neutral_out_zone = None     # Off notch as the game reports it, in output units
        self.out_named = {}               # label -> (min, max) for notches the game names by output value
        for nv in named:
            r = nv.get("valueRange") or {}
            source = nv.get("valueSource", "")
            if "min" not in r or "max" not in r:
                continue
            a, b = convert(float(r["min"]), source), convert(float(r["max"]), source)
            label = str(nv.get("displayName", "")).lower()
            if source == "Output":
                self.out_named.setdefault(label, (min(float(r["min"]), float(r["max"])),
                                                  max(float(r["min"]), float(r["max"]))))
            if source == "Output" and label.strip() in NEUTRAL_LABELS:
                self._neutral_out_zone = (min(float(r["min"]), float(r["max"])),
                                          max(float(r["min"]), float(r["max"])))
            if a is None or b is None:
                zones.append((None, None, label, source))
            else:
                zones.append((min(a, b), max(a, b), label, "Input" if source.startswith("Input") else "Output"))
        return zones

    def _exclude_emergency(self):
        """Shrink the usable range so the joystick can never reach an emergency notch."""
        emerg = [z for z in self.zones if is_emergency(z[2])]
        if not emerg:
            if not self.zones and "brake" in (self.name + self.ident).lower():
                # unknown layout: stay well clear of the end where emergency usually is
                self.note = "no notch info, capped at 85%"
                return self.lo, self.lo + (self.hi - self.lo) * 0.85
            return self.lo, self.hi

        safe_lo, safe_hi = self.lo, self.hi
        mid = (self.lo + self.hi) / 2
        for a, b, _, source in emerg:
            if a is None:
                self.note = "emergency position unknown, trimmed 20% both ends"
                span = self.hi - self.lo
                return self.lo + 0.2 * span, self.hi - 0.2 * span
            if b - a < 1e-6:
                # named as a single point (Isle of Wight brake, Class 165): go no further than the nearest
                # other named place - halfway can still be in emergency where the estimate is off (Class 165:
                # Full Service at -0.75, estimated -0.82)
                gaps = [abs((za + zb) / 2 - a) for za, zb, zl, _ in self.zones
                        if za is not None and not is_emergency(zl) and abs((za + zb) / 2 - a) > 1e-6]
                margin = min(gaps) if gaps else 0.1 * (self.hi - self.lo)
            else:
                # stay half an emergency-notch width away from where the emergency zone starts. Where the
                # game gives the zone on the handle itself, count only the part the handle can reach (German
                # brake valves name Emergency 0.9..1.5 on a 0..1 handle); a zone given by output value is only
                # an estimate on the handle, so all of it counts (M3a: the gap before its emergency notch)
                if source == "Input":
                    a, b = max(a, self.lo), min(b, self.hi)
                    if a >= b:
                        continue
                margin = 0.5 * (b - a)
            if (a + b) / 2 >= mid:
                safe_hi = min(safe_hi, a - margin)
            else:
                safe_lo = max(safe_lo, b + margin)
        self.note = "emergency excluded"
        return safe_lo, safe_hi

    def _find_neutral(self):
        """Locate the Off/neutral point and which end is brake on a combined power/brake lever."""
        clamp = lambda v: min(self.safe_hi, max(self.safe_lo, v))
        neutral = how = None
        for source_wanted in ("Input", "Output"):
            for a, b, label, source in self.zones:
                if a is not None and source == source_wanted and label.strip() in NEUTRAL_LABELS:
                    neutral, how = (a + b) / 2, f"'{label}' notch"
                    break
            if neutral is not None:
                break
            if source_wanted == "Input":
                # an unnamed gap between the brake zones and the power zones is the Off notch
                spans = sorted((max(a, self.lo), min(b, self.hi)) for a, b, l, s in self.zones
                               if a is not None and s == "Input" and not is_emergency(l))
                gaps = [(e1, s2) for (_, e1), (s2, _) in zip(spans, spans[1:]) if s2 > e1 + 1e-6]
                if len(gaps) == 1:
                    neutral, how = sum(gaps[0]) / 2, "gap between brake and power"
                    break
        if neutral is None:
            neutral, how = (self.safe_lo + self.safe_hi) / 2, "middle (guessed)"
        self.neutral = clamp(neutral)

        brake_low = True
        emerg = [z for z in self.zones if is_emergency(z[2]) and z[0] is not None]
        brakes = [z for z in self.zones if is_brake_label(z[2]) and z[0] is not None]
        if emerg:
            brake_low = (emerg[0][0] + emerg[0][1]) / 2 < self.neutral
        elif brakes:
            brake_low = (brakes[0][0] + brakes[0][1]) / 2 < self.neutral
        self.brake_end = self.safe_lo if brake_low else self.safe_hi
        self.power_end = self.safe_hi if brake_low else self.safe_lo
        self.note = (self.note + ", " if self.note else "") + f"neutral {self.neutral:.3g} from {how}"

    def _range(self):
        lo = hi = None
        for fn_lo, fn_hi in (("Function.GetMinimumInputValue", "Function.GetMaximumInputValue"),
                             ("GetMinimumInputValue", "GetMaximumInputValue")):
            try:
                lo = self.api.get_value(f"CurrentDrivableActor/{self.name}.{fn_lo}")
                hi = self.api.get_value(f"CurrentDrivableActor/{self.name}.{fn_hi}")
            except Exception:
                lo = hi = None
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > lo:
                return float(lo), float(hi)
        return 0.0, 1.0

    def works(self):
        try:
            return self.api.get_value(self.path) is not None
        except Exception:
            return False

    def value_between(self, start, end, frac):
        """Value frac (0..1) of the way from start to end, never outside the safe range."""
        frac = min(1.0, max(0.0, frac))
        return min(self.safe_hi, max(self.safe_lo, start + (end - start) * frac))

    def settle_neutral(self):
        """Put a combined power/brake lever on Off, checking with the game where Off really is.
        Irregular levers (e.g. Class 802) aren't evenly spaced, so the estimate from the notch list can
        land in a brake notch. The game is asked which notch the handle is in and the handle is nudged
        toward Off - from the brake side toward less braking, never toward emergency. Done once per
        train; where the estimate is already right this is a single check. Returns a log message."""
        zone = self._neutral_out_zone
        if self.neutral_verified or zone is None:
            self.neutral_verified = True
            self.set_value(self.neutral)
            return None
        target = 0.0 if zone[0] <= 0.0 <= zone[1] else (zone[0] + zone[1]) / 2
        if self._out_range:
            slope = (self._out_range[1] - self._out_range[0]) / (self.hi - self.lo)
        else:
            slope = max(1e-3, (zone[1] - zone[0]) / 0.1)
        if self.out_backwards:
            slope = -slope
        x, prev = self.neutral, None
        for _ in range(25):
            self.api.set(self.path, x)
            time.sleep(0.05)
            out = self._get("Function.GetCurrentOutputValue").get("ReturnValue")
            if not isinstance(out, (int, float)):
                self.neutral_verified = True          # the game can't tell us: keep the estimate
                return None
            inside = zone[0] <= out <= zone[1]
            if inside and (abs(out - target) < 0.05 or (prev and prev[1] == out)):
                moved = abs(x - self.neutral) > 1e-4
                self.neutral, self.neutral_verified = x, True
                self._last_sent = (x, time.time())
                self.note = re.sub(r"neutral [-\d.e]+ from [^,]*", f"neutral {x:.3g} (checked with game)",
                                   self.note)
                if self._calibration_key:
                    save_calibration(self._calibration_key, x)
                return f"Off position found at {x:.3f} on {self.name}" if moved else None
            if prev and abs(x - prev[0]) > 1e-6 and (out - prev[1]) / (x - prev[0]) * slope > 0:
                slope = (out - prev[1]) / (x - prev[0])
            prev = (x, out)
            step = (target - out) / slope * (1.0 if inside else 0.8)   # approach without overshooting
            step = max(-0.1, min(0.1, step))
            if abs(step) < 0.003:
                step = math.copysign(0.003, target - out)
            x = min(self.safe_hi, max(self.safe_lo, x + step))
        self.neutral_verified = True                  # give up searching; keep the estimate
        self.set_value(self.neutral)
        return f"Couldn't confirm the Off position on {self.name}; using the estimate"

    def _brake_limit(self):
        """The furthest full brake may ever go: 5% of the brake side's travel short of the end of the handle,
        where emergency usually is (Class 331's emergency is right at the end, with normal braking up to it)."""
        end = self.lo if self.brake_end <= self.neutral else self.hi
        return end + 0.05 * (self.neutral - end)

    def _set_brake_end(self, end):
        """Move full brake to end, if that is further than now (a place checked with the game)."""
        self.brake_end_verified = True
        if self.brake_end is None or self.brake_end == end:
            return
        limit = self._brake_limit()
        end = max(end, limit) if self.brake_end <= self.neutral else min(end, limit)
        if self.brake_end == self.safe_lo and end < self.safe_lo:
            self.safe_lo = self.brake_end = end
        elif self.brake_end == self.safe_hi and end > self.safe_hi:
            self.safe_hi = self.brake_end = end

    def extend_brake(self):
        """On a smooth brake range the emergency margin, worked out from an estimate of where emergency
        starts, can stop well short of full braking (Class 331: 86%). With the handle at full brake, check
        with the game: read where the handle is, confirm the brake range responds smoothly, then move toward
        the end of the named brake range in small steps, checking each one, and stop 3% short of it, or where
        braking stops increasing (Class 802: at Max Brake, before the emergency notch) - never into
        emergency. Done once per train and remembered. Also for a separate train brake (BR 143: its
        emergency margin stops at step 7 of 7, short of Full Service). Returns a log message, or None."""
        self.brake_end_verified = True
        brake = emerg = None
        for label, (a, b) in self.out_named.items():
            if is_emergency(label):
                emerg = emerg or (a, b)
            elif is_braking_label(label):
                # all the brake names together (Class 802: Min Brake, {BrakePercent} Brake, Max Brake)
                brake = (min(a, brake[0]), max(b, brake[1])) if brake else (a, b)
        if self.notches or brake is None or emerg is None or self.brake_end is None:
            return None                    # notched handles already stop on the last brake notch
        b_lo, b_hi = brake
        edge, inward = (b_lo, 1.0) if sum(emerg) < sum(brake) else (b_hi, -1.0)
        target = edge + inward * 0.03 * (b_hi - b_lo)
        # where a brake name of its own leads up to emergency (Class 802: Max Brake, right next to the emergency
        # notch), full brake is getting into it - going further could land the handle in emergency
        last = [(a, b) for label, (a, b) in self.out_named.items()
                if is_braking_label(label) and b - a <= 0.5 * (b_hi - b_lo)
                and (abs(a - emerg[1]) < 1e-3 or abs(b - emerg[0]) < 1e-3)]
        if not last and (not self.combined or self._uneven):
            # braking runs straight up to emergency, on a train brake (BR 442) or an unevenly notched handle
            # (Class 170 in Regional Railways: emergency is the notch next to B3): no safe place to stop
            return None
        if last:
            a, b = last[0]
            target = (b if inward > 0 else a) - inward * 0.03 * (b_hi - b_lo)

        def arrived(out):
            if last:
                return last[0][0] <= out <= last[0][1]
            return abs(target - out) <= 0.01 * (b_hi - b_lo)

        def reading(x):
            self.api.set(self.path, x)
            time.sleep(0.05)
            out = self._get("Function.GetCurrentOutputValue").get("ReturnValue")
            return float(out) if isinstance(out, (int, float)) and b_lo <= out <= b_hi else None

        def place():
            v = self._get("InputValue").get("InputValue")
            return float(v) if isinstance(v, (int, float)) else None

        # three readings stepping back toward Off (less braking, so always safe); the handle has to answer
        # smoothly, about in a straight line, otherwise it's left as it is. (The Class 802 gives its braking in
        # whole percent, so readings close together can't be expected to line up exactly.)
        start = self.brake_end
        step = (self.neutral - start) * 0.1
        xs = [start, start + step, start + 2 * step]
        outs = [reading(x) for x in xs]
        slopes = [(o2 - o1) / (x2 - x1) for x1, x2, o1, o2 in zip(xs, xs[1:], outs, outs[1:])
                  if None not in (o1, o2)]
        if (len(slopes) < 2 or not slopes[0] or slopes[0] * slopes[1] <= 0
                or max(abs(s) for s in slopes) > 1.5 * min(abs(s) for s in slopes)):
            self.api.set(self.path, start)
            return None
        slope = sum(slopes) / 2
        limit = self._brake_limit()
        toward = -1.0 if start <= self.neutral else 1.0        # the way to more braking
        # steps of at least 1% of the brake side's travel: smaller ones may not change a whole-percent output
        min_step = 0.01 * abs(self.neutral - (self.lo if toward < 0 else self.hi))
        x, out, at = start, reading(start), place()
        for _ in range(16):
            if out is None or arrived(out):
                break
            # cover 40% of what's left, with the slope measured over the last step, so a handle that
            # gets steeper toward the end still can't be pushed past the target in one go
            nx = x + (target - out) / slope * 0.4
            if abs(nx - x) < min_step:
                nx = x + (math.copysign(min_step, nx - x) if nx != x else toward * min_step)
            nx = max(limit, nx) if start <= self.neutral else min(limit, nx)
            if abs(nx - x) < 1e-4:
                break                      # at the limit: the named brake range ends beyond the handle
            nout = reading(nx)
            if nout is None:               # outside the brake range: stay at the last good place
                break
            nat = place()
            if abs(nout - out) < 1e-6 and None not in (at, nat) and abs(nat - at) > 1e-4:
                break                      # it moved but braked no more: the strongest is where it was
            # (a handle pulled back into the notch it was in hasn't moved: keep going, BR 143 notches)
            if abs(nx - x) > 1e-6 and nout != out and (nout - out) / (nx - x) * slope > 0:
                slope = (nout - out) / (nx - x)
            x, out, at = nx, nout, nat
        further = (x - start) * (start - self.neutral) > 1e-6     # more braking than before
        best = x if further else start
        self.api.set(self.path, best)
        self._last_sent = (best, time.time())
        if not further:
            return None
        self._set_brake_end(best)
        if self._calibration_key:
            save_calibration(f"{self._calibration_key}|brake_end", best)
        return f"Full brake on {self.name} extended from {start:.3f} to {best:.3f} (checked with game)"

    def set_value(self, value):
        """Send the lever toward value. Returns a log message, or None."""
        value = min(self.safe_hi, max(self.safe_lo, value))
        if self.notches:
            # only ever send exact notch positions: an in-between value makes the game draw the handle
            # there and then snap it back to the notch, which looks like the handle jumping about
            value = self._snap(value)
            last_value, last_time = self._last_sent
            if value == last_value and time.time() - last_time < 0.5:
                return None                # already there (re-sent now and then in case keys moved it)
        if self.positions:
            self._set_valve(value)
            return None
        previous = self._last_sent[0]
        self.api.set(self.path, value)
        self._last_sent = (value, time.time())
        if self.model is not None and previous is not None and self.model.gated_between(previous, value):
            # a gated notch on the way stops the handle short (M3a: P1 on the way to P4): send it again
            for _ in range(3):
                time.sleep(0.02)
                x = self._get("InputValue").get("InputValue")
                if not isinstance(x, (int, float)) or abs(x - value) < 1e-3:
                    break
                self.api.set(self.path, value)
        if (self.notches and not self._exact_notches and len(self._notches_confirmed) < 3
                and round(value, 4) not in self._notches_confirmed):
            return self._check_notch(value)
        return None

    def _check_notch(self, value):
        """The notch positions are worked out assuming the notches are evenly spaced, which only shows in the
        game: the first few times a notch is sent, check that the game gives its whole-number output there.
        Where it doesn't (RhB ABe 8/12, M7, BR 442: some notches evenly spaced, others not, or a smooth
        stretch), stop using notch positions and send smooth values. A handle stopped short by a gated notch,
        or pulled into another one, says nothing either way. Returns a log message, or None."""
        time.sleep(0.05)
        x = self._get("InputValue").get("InputValue")
        out = self._get("Function.GetCurrentOutputValue").get("ReturnValue")
        if not isinstance(x, (int, float)) or not isinstance(out, (int, float)) or abs(x - value) > 1e-3:
            return None
        expected = round(self._output_at(value))
        if abs(out - expected) < 0.01:
            self._notches_confirmed.add(round(value, 4))
            return None
        self.notches, self._notch, self._uneven = None, None, True
        return f"{self.name}: its notches aren't evenly spaced, so it's moved smoothly instead"

    def __repr__(self):
        text = f"{self.name} [{self.safe_lo:.3g}..{self.safe_hi:.3g}]"
        note = ", ".join(n for n in (self.note, "runs backwards" if self.out_backwards else "") if n)
        return f"{text} ({note})" if note else text


def reverser_label(label, prefix=True):
    """Map a notch name to 'forward' / 'neutral' / 'reverse', or None."""
    s = label.strip().lower()
    for pos, words in REVERSER_LABELS.items():
        if s in words:
            return pos
    if prefix:
        for pos, words in REVERSER_LABELS.items():
            if any(len(w) >= 3 and s.startswith(w) for w in words):
                return pos
    return None


def reverser_zone(s, previous=None, hysteresis=0.06):
    """Slider value s (-1..+1, +1 = forward end) -> 'forward' / 'neutral' / 'reverse'.
    Hysteresis stops the choice flickering when the slider sits on a boundary."""
    edge = 1.0 / 3.0
    if previous == "forward" and s > edge - hysteresis:
        return "forward"
    if previous == "reverse" and s < -edge + hysteresis:
        return "reverse"
    if previous == "neutral" and -edge - hysteresis < s < edge + hysteresis:
        return "neutral"
    return "forward" if s > edge else "reverse" if s < -edge else "neutral"


class Reverser:
    """The train's reverser, driven to its Forward / Neutral / Reverse notches only.

    Where the game names the notches by output value, their place on the handle is only an estimate, and
    on some trains the output runs the other way to the input (Class 350: Reverse at input 0, Off at 1).
    So the game is asked which way round the handle is, every move is checked with the game, and a notch
    found somewhere other than estimated is remembered per train."""

    def __init__(self, api, name, ident="", train=None, model=None):
        self.lever = Lever(api, name, ident, role="reverser", model=model)
        self.api = api
        self.name = name
        self.path = self.lever.path
        self.notches = {}                 # 'forward' / 'neutral' / 'reverse' -> input value
        self.out_ranges = {}              # the same notches in output units, where the game names them so
        self.backwards = False            # True when the output falls as the input rises
        self.learned = set()              # notches confirmed with the game somewhere other than estimated
        self.neutral_label = None         # what the train calls the notch used as Neutral, if not Neutral
        lo, hi = self.lever.lo, self.lever.hi
        for prefix in (False, True):      # exact names first, then e.g. "Forward 1"
            for a, b, label, source in self.lever.zones:
                pos = reverser_label(label, prefix)
                if a is not None and pos and pos not in self.notches:
                    self.notches[pos] = min(hi, max(lo, (a + b) / 2))
                    if source == "Output" and label in self.lever.out_named:
                        self.out_ranges[pos] = self.lever.out_named[label]
        if "neutral" not in self.notches and "forward" in self.notches and "reverse" in self.notches:
            self._stand_in_neutral()
        self._trim_overlaps()
        self.ok = all(p in self.notches for p in REVERSER_LABELS)
        self._calibration_key = f"{train}|{name}" if train else None
        if self.lever.exact_places:
            self.learned = set(self.notches)   # places read from the game files: nothing to learn
        elif self._calibration_key:
            for pos in self.out_ranges:
                learned = load_calibration().get(f"{self._calibration_key}|{pos}")
                if isinstance(learned, (int, float)) and lo <= learned <= hi:
                    self.notches[pos] = float(learned)
                    self.learned.add(pos)
        if self.out_ranges and self.lever._out_range:
            x = self._input()
            self._learn_direction((x, None), (x, self._output()))

    def _stand_in_neutral(self):
        """For a reverser with no Neutral, the notch used instead: one with no traction (NEUTRAL_STAND_INS),
        preferably between Reverse and Forward (BR diesels: Off, Reverse, Engine Only, Forward; Class 08:
        Reverse, Off, Forward), else anywhere (Class 101: Off, Forward, Reverse). With none, the unnamed notch
        between Reverse and Forward (BR 363)."""
        f, r = self.notches["forward"], self.notches["reverse"]
        lo, hi, middle = min(f, r), max(f, r), (f + r) / 2
        best = None
        for a, b, label, source in self.lever.zones:
            name = label.strip().lower()
            if a is None or name not in NEUTRAL_STAND_INS:
                continue
            if any(a < (za + zb) / 2 < b and zb - za < b - a
                   for za, zb, zl, _ in self.lever.zones if za is not None and zl != label):
                continue                   # a name for the rest of the handle (Class 52: Engine Only)
            x = min(self.lever.hi, max(self.lever.lo, (a + b) / 2))
            rank = (not lo < x < hi, NEUTRAL_STAND_INS.index(name), abs(x - middle))
            if best is None or rank < best[0]:
                best = (rank, x, label, source)
        if best:
            _, x, label, source = best
            self.notches["neutral"] = x
            self.neutral_label = label.strip().lower()
            if source == "Output" and label in self.lever.out_named:
                self.out_ranges["neutral"] = self.lever.out_named[label]
            return
        model = self.lever.model
        if model is not None:            # the game files show where that unnamed notch is
            unnamed = [x for x in model.resting() if lo < x < hi and model.name_at(x) is None]
            if unnamed:
                self.notches["neutral"] = min(unnamed, key=lambda x: abs(x - middle))
        fo, ro = self.out_ranges.get("forward"), self.out_ranges.get("reverse")
        if fo and ro and (fo[1] < ro[0] or ro[1] < fo[0]):
            gap = (fo[1], ro[0]) if fo[1] < ro[0] else (ro[1], fo[0])
            if not any(gap[0] < (a + b) / 2 < gap[1] for a, b in self.lever.out_named.values()):
                self.notches.setdefault("neutral", middle)
                self.out_ranges["neutral"] = gap

    def _trim_overlaps(self):
        """Keep each notch's output range off the other, narrower, named notches: the Class 323 names Reverse
        from -1.5 to 0.5, over Neutral (-0.5 to 0.5), so Neutral was taken for Reverse. (A wider name is one
        for the rest of the handle, e.g. Class 52 Engine Only.)"""
        for pos, (a, b) in list(self.out_ranges.items()):
            centre, trimmed = (a + b) / 2, (a, b)
            for oa, ob in self.lever.out_named.values():
                other = (oa + ob) / 2
                if (oa, ob) != (a, b) and ob - oa < b - a and trimmed[0] < other < trimmed[1]:
                    if other > centre:
                        trimmed = (trimmed[0], min(trimmed[1], oa))
                    else:
                        trimmed = (max(trimmed[0], ob), trimmed[1])
            if trimmed != (a, b):
                self.out_ranges[pos] = trimmed
                if self.lever._out_range:
                    self.notches[pos] = self._estimate(pos)

    def _name(self, label):
        """'forward' / 'neutral' / 'reverse' for a notch name, or the name itself."""
        s = label.strip()
        return reverser_label(s) or ("neutral" if s.lower() == self.neutral_label else s)

    def _input(self):
        v = self.api.get_value(self.path)
        return float(v) if isinstance(v, (int, float)) else None

    def _output(self):
        v = self.lever._get("Function.GetCurrentOutputValue").get("ReturnValue")
        return float(v) if isinstance(v, (int, float)) else None

    def _at(self, position, out):
        a, b = self.out_ranges[position]
        return out is not None and a <= out <= b

    def _estimate(self, position):
        o_lo, o_hi = self.lever._out_range
        frac = (sum(self.out_ranges[position]) / 2 - o_lo) / (o_hi - o_lo)
        if self.backwards:
            frac = 1.0 - frac
        lo, hi = self.lever.lo, self.lever.hi
        return min(hi, max(lo, lo + frac * (hi - lo)))

    def _learn_direction(self, before, after):
        """Work out which way round the output runs, from two (input, output) readings. With no output in
        the first reading, the handle's one position is compared with both possible ways round. Returns
        True if the estimates changed."""
        (x1, o1), (x2, o2) = before, after
        if x2 is None or o2 is None or self.lever._out_range is None:
            return False
        if o1 is None:
            o_lo, o_hi = self.lever._out_range
            lo, hi = self.lever.lo, self.lever.hi
            frac = (x2 - lo) / (hi - lo)
            rising, falling = o_lo + frac * (o_hi - o_lo), o_hi - frac * (o_hi - o_lo)
            if abs(o2 - rising) < abs(o2 - falling) - 0.25:
                backwards = False
            elif abs(o2 - falling) < abs(o2 - rising) - 0.25:
                backwards = True
            else:
                return False               # e.g. a 3-notch handle sitting in the middle: can't tell
        elif x1 is None or x1 == x2 or o1 == o2:
            return False
        else:
            backwards = (o2 - o1) * (x2 - x1) < 0
        if backwards == self.backwards:
            return False
        self.backwards = backwards
        for pos in self.out_ranges:
            if pos not in self.learned:
                self.notches[pos] = self._estimate(pos)
        return True

    def _move(self, x, position):
        """Send the handle to input x; returns the game's output once it reports the notch (or after a
        short wait). A gated notch on the way stops the handle short (HST: from Engine Off, Forward stops at
        Neutral), so it is sent again while that gets it further."""
        last = None
        for _ in range(3):
            self.api.set(self.path, x)
            out = None
            for wait in (0.05, 0.1, 0.15):
                time.sleep(wait)
                out = self._output()
                if out is None or self._at(position, out):
                    return out
            now = self._input()
            if now is None or abs(now - x) < 1e-3 or now == last:
                return out                 # it's where it was sent (or settled in a notch nearby)
            last = now
        return out

    def _remember(self, position):
        x = self._input()
        if x is None:
            return
        self.notches[position] = x
        self.learned.add(position)
        if self._calibration_key:
            save_calibration(f"{self._calibration_key}|{position}", x)

    def set(self, position):
        rng = self.out_ranges.get(position)
        if rng is None or self.lever._out_range is None:
            self.lever.set_value(self.notches[position])   # notch given as an input value: exact
            x = self.lever._last_sent[0]
            for _ in range(2):                             # stopped short at a gated notch: send again
                time.sleep(0.1)
                if self.position() == position:
                    return
                self.api.set(self.path, x)
            return
        before = (self._input(), self._output())
        x = self.notches[position]
        out = self._move(x, position)
        if out is None or self._at(position, out):
            return
        changed = self._learn_direction(before, (x, out))  # it went the other way round
        if position in self.learned:                       # remembered place no longer right (game update)
            self.learned.discard(position)
            self.notches[position] = self._estimate(position)
            changed = True
        if changed and self.notches[position] != x:        # try the new estimate
            x = self.notches[position]
            out = self._move(x, position)
            if out is None:
                return
            if self._at(position, out):
                self._remember(position)
                return
        # walk the handle toward the notch a step at a time, as a driver would, until the game reports it
        lo, hi = self.lever.lo, self.lever.hi
        count = self.lever._get("Function.GetNotchCount").get("ReturnValue")
        count = count if isinstance(count, int) and count >= 2 else 4
        notch = (hi - lo) / (count - 1)
        target, moved_since_change, turned = sum(rng) / 2, 0.0, None
        for _ in range(4 * count):
            way = turned or (1.0 if (target > out) != self.backwards else -1.0)
            nx = min(hi, max(lo, x + way * notch / 3))
            if nx == x:
                if turned:
                    return                 # end of the handle's travel both ways
                # end of travel: the notch is the other way (Class 142: a second Off past Forward)
                turned, moved_since_change = -way, 0.0
                continue
            nout = self._move(nx, position)
            if nout is None:
                return
            if nout != out:
                self._learn_direction((x, out), (nx, nout))
                moved_since_change, turned = 0.0, None
            else:
                moved_since_change += abs(nx - x)
                if moved_since_change > 1.5 * notch:
                    return                 # the handle isn't responding (e.g. master key out)
            x, out = nx, nout
            if self._at(position, out):
                self._remember(position)
                return

    def position(self):
        """Current notch: 'forward' / 'neutral' / 'reverse', another notch name (e.g. 'off'), or None."""
        model = self.lever.model
        if self.lever.exact_places:
            x = self._input()              # the game files say what's where on the handle
            if x is None:
                return None
            label = model.name_at(x)
            if label:
                return self._name(label)
            n = self.notches.get("neutral")
            return "neutral" if n is not None and abs(x - n) < 1e-3 else None
        if self.out_ranges and self.lever._out_range:
            out = self._output()           # as the game reports it
            if out is not None:
                for pos, (a, b) in self.out_ranges.items():
                    if a <= out <= b:
                        return pos
                for label, (a, b) in self.lever.out_named.items():
                    if a <= out <= b:
                        return self._name(label)
        v = self.lever.api.get_value(self.lever.path)
        zones = [z for z in self.lever.zones if z[0] is not None]
        if not isinstance(v, (int, float)) or not zones:
            return None
        lo, hi = self.lever.lo, self.lever.hi
        nearest = min(zones, key=lambda z: abs((max(z[0], lo) + min(z[1], hi)) / 2 - v))
        return self._name(nearest[2])

    def __repr__(self):
        if not self.ok:
            return f"{self.name} (Forward/Neutral/Reverse notches not recognised - not used)"
        text = ", ".join(f"{p} {v:.3g}" for p, v in self.notches.items())
        if self.neutral_label:
            text += f", Neutral is its '{self.neutral_label.title()}'"
        elif "neutral" in self.notches and "neutral" not in [reverser_label(z[2]) for z in self.lever.zones]:
            text += ", Neutral is its unnamed middle notch"
        return f"{self.name} ({text}{', runs backwards' if self.backwards else ''})"


class PushButton:
    """A momentary cab button, held down in the game for as long as the joystick button is held."""

    def __init__(self, api, name):
        self.api = api
        self.name = name
        self.node = f"CurrentDrivableActor/{name}"
        self.path = f"{self.node}.InputValue"
        self.held = False                 # held in the game as a hand would hold it (its Interacting flag)
        try:
            lo = api.get_value(f"{self.node}.Function.GetMinimumInputValue")
            hi = api.get_value(f"{self.node}.Function.GetMaximumInputValue")
            rest = api.get_value(f"{self.node}.Function.GetDefaultInputValue")
        except Exception:
            lo = hi = rest = None
        # False for something found by name that isn't a cab control (LIRR M7 "M7_AlerterV2": the alerter)
        self.is_control = isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi != lo
        if self.is_control:
            # the minimum is where the button rests; a pedal the game holds down by default rests at the
            # other end and is let up when pressed: Class 142 DSD rests at its maximum, Class 350 DSD counts
            # backwards, resting at 1 and pressed at 0
            if hi > lo and rest == hi:
                self.released, self.pressed = float(hi), float(lo)
            else:
                self.released, self.pressed = float(lo), float(hi)
        else:
            self.released, self.pressed = 0.0, 1.0

    def set(self, down):
        """Pressed, the button is held in place as a hand would hold it (the game's Interacting flag). Sent
        without that, the game shows the value but never counts the button as pushed, so it didn't go all the
        way in (seen on the Class 142 door, DSD and AWS buttons)."""
        if down and not self.held:
            self._interact(True)
        self.api.set(self.path, self.pressed if down else self.released)
        if not down and self.held:
            self._interact(False)

    def _interact(self, on):
        self.held = on
        self.api.set(f"{self.node}.Interacting", 1.0 if on else 0.0)

    def let_go(self):
        """Back to where the button rests and stop holding it, if it's held (see TrainControls.let_go)."""
        if not self.held:
            return
        try:
            self.set(False)
        except Exception:
            self.held = False

    def __repr__(self):
        return self.name


class ButtonGroup:
    """Several cab buttons worked together by one joystick button."""

    def __init__(self, buttons):
        self.buttons = buttons
        self.name = " + ".join(b.name for b in buttons)

    def set(self, down):
        for b in self.buttons:
            b.set(down)

    def let_go(self):
        for b in self.buttons:
            b.let_go()

    def __repr__(self):
        return self.name


class ReverserSync:
    """Moves the reverser when the slider enters a different zone; never while the train is moving."""

    def __init__(self, log):
        self.log = log
        self.reset()

    def reset(self):
        self.last = None
        self.verify = None

    def update(self, controls, zone):
        rev = controls.reverser
        if rev is None or not rev.ok or zone is None:
            self.reset()
            return
        now = time.time()
        if self.verify and now >= self.verify[0]:
            expected, self.verify = self.verify[1], None
            actual = rev.position()
            if actual != expected:
                self.log(f"Reverser is at {actual or '?'}, not {expected} - the train may need "
                         f"the master key in or the brake applied first")
        if self.last is None:
            self.last = zone          # don't move the reverser until the slider actually moves
            return
        if zone == self.last:
            return
        self.last = zone
        speed = controls.speed()
        if speed is not None and abs(speed) > REVERSER_MAX_SPEED:
            self.log(f"Reverser NOT moved to {zone}: train is moving ({abs(speed) * 3.6:.0f} km/h)")
            return
        rev.set(zone)
        self.log(f"Reverser -> {zone}")
        self.verify = (now + 0.7, zone)


# ---------------------------------------------------------------- look left / right
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", _MOUSEINPUT)]


def mouse_move(dx):
    """Relative horizontal mouse movement, as if the mouse had been moved by dx counts."""
    inp = _INPUT(type=0, mi=_MOUSEINPUT(dx=int(dx), dwFlags=0x0001))    # INPUT_MOUSE, MOUSEEVENTF_MOVE
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def game_has_focus():
    """True when the foreground window belongs to Train Sim World."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = ctypes.c_ulong(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return "trainsimworld" in os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def shape_axis(raw, deadzone):
    """Apply a centre dead zone and rescale so the output still reaches +-1."""
    if abs(raw) < deadzone:
        return 0.0
    return (1.0 if raw > 0 else -1.0) * (abs(raw) - deadzone) / (1.0 - deadzone)


def wrap_angle(a):
    return (a + 180.0) % 360.0 - 180.0


class MousePump(threading.Thread):
    """Sends queued mouse movement in small, evenly spaced pieces (~250 per second), like a real mouse
    moving slowly, instead of one jump per update - so the view turns smoothly on every game frame."""

    INTERVAL = 0.004     # seconds between pieces
    SPREAD = 0.066       # each update is spread over two update intervals, so consecutive updates
                         # overlap and the motion never pauses between them

    def __init__(self):
        super().__init__(daemon=True)
        self.running = True
        self._lock = threading.Lock()
        self._pending = 0.0              # counts still to send
        self._rate = 0.0                 # counts per second
        self._carry = 0.0                # fraction of a count not sent yet

    def add(self, counts):
        with self._lock:
            self._pending += counts
            self._rate = abs(self._pending) / self.SPREAD

    def clear(self):
        with self._lock:
            self._pending = self._rate = self._carry = 0.0

    def tick(self, dt):
        with self._lock:
            if not self._pending:
                return
            step = min(abs(self._pending), self._rate * dt)
            step = step if self._pending > 0 else -step
            self._pending -= step
            if abs(self._pending) < 1e-9:
                self._pending = 0.0
            self._carry += step
            n = int(self._carry) if self._pending else int(round(self._carry))
            self._carry -= n
            if not self._pending:
                self._carry = 0.0
        if n:
            mouse_move(n)

    def run(self):
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)    # 1 ms sleep resolution instead of ~16 ms
        except Exception:
            pass
        last = time.perf_counter()
        while self.running:
            time.sleep(self.INTERVAL)
            now = time.perf_counter()
            self.tick(min(0.05, now - last))
            last = now


class LookController(threading.Thread):
    """Turns the cab view to the angle the twist asks for, using emulated mouse movement and the
    view direction the game reports (0 = straight ahead), so mouse sensitivity doesn't matter.
    When the twist goes back to centre the view returns to straight ahead, then the mouse is left alone.

    Moves are sent from a prediction of where the view is heading (instant response); sensitivity and
    direction are only learned once the view has stopped moving, so game input lag can't fool them."""

    MAX_STEP = 600       # mouse counts per step
    TOLERANCE = 0.5      # degrees
    HOLD_BAND = 1.0      # degrees: twist wobble smaller than this doesn't move the view
    MAX_SPEED = 360.0    # degrees per second

    def __init__(self, log):
        super().__init__(daemon=True)
        self.api = TSWApi()
        self.log = log
        self.pump = MousePump()
        self.twist = None                 # -1..+1 after dead zone, from the joystick thread; None = off
        self.max_angle = LOOK_MAX_ANGLE
        self.smoothing = LOOK_SMOOTHING
        self.enabled = True
        self.running = True
        self.counts_per_degree = 6.0      # mouse counts per degree of view, learned
        self.learned = False
        self.direction = 1                # -1 if the game's mouse look is inverted
        self.steering = False             # True while we own the view (twisted, or returning)
        self.yaw = None                   # last view yaw read from the game, for display
        self.state = "idle"
        self._next_check = 0.0
        self._blocked = False
        self._stuck_target = None
        self._reset_tracking()

    def _reset_tracking(self):
        self._predicted = None            # where the view will end up once sent moves are applied
        self._settled_yaw = None          # view yaw at the last moment it was standing still
        self._sent = 0                    # counts sent since then
        self._prev_yaw = None
        self._still = 0                   # consecutive reads without the view moving
        self._quiet = 0                   # steps since the last mouse move
        self._held = None                 # twist target with small wobble filtered out
        self._smooth = None               # eased target the view actually follows
        self._smooth_done = True
        self._last_time = None

    def run(self):
        self.pump.start()
        while self.running:
            time.sleep(1.0 / 30)
            try:
                self.step()
            except Exception:
                self.state = "no game"
                self.pump.clear()
                self._reset_tracking()
                time.sleep(0.5)
        self.pump.running = False

    def _read_yaw(self):
        v = self.api.get("Player.Function.GetControlRotation").get("Values") or {}
        yaw = (v.get("ReturnValue") or {}).get("yaw")
        return float(yaw) if isinstance(yaw, (int, float)) else None

    def _settled(self, yaw, target):
        """The view has stopped: learn from the moves since the last stop, then trust the real angle."""
        if self._sent and self._settled_yaw is not None:
            moved = yaw - self._settled_yaw
            expected = self._sent / self.counts_per_degree
            if abs(moved) > 2.0:
                if moved * self._sent > 0:
                    estimate = min(200.0, max(0.2, abs(self._sent / moved)))
                    if not self.learned:
                        self.counts_per_degree, self.learned = estimate, True
                    elif abs(moved) >= 0.6 * abs(expected):
                        # only refine from moves that fully landed; a move cut short by the
                        # edge of the view would make the mouse look less sensitive than it is
                        self.counts_per_degree = 0.7 * self.counts_per_degree + 0.3 * estimate
                else:
                    self.direction = -self.direction
                    self.log("Look: mouse turns the view the other way - direction flipped")
            elif abs(self._sent) > 3 * self.counts_per_degree:
                self._stuck_target = target      # pushed but nothing moved: view is at its limit
        self._sent = 0
        self._settled_yaw = yaw
        self._predicted = yaw

    def _ease(self, target, yaw, now):
        """Filter the twist target: ignore small wobble, then glide toward it at a limited speed."""
        if target == 0.0 or self._held is None:
            self._held = target
        elif target > self._held + self.HOLD_BAND:     # trail the twist, ignoring wobble inside the band
            self._held = target - self.HOLD_BAND
        elif target < self._held - self.HOLD_BAND:
            self._held = target + self.HOLD_BAND
        if self._smooth is None:
            self._smooth = yaw                 # start gliding from wherever the view is now
        dt = min(0.1, now - self._last_time) if self._last_time else 1.0 / 30
        self._last_time = now
        change = self._held - self._smooth
        if self.smoothing > 0:
            change *= 1.0 - math.exp(-dt / self.smoothing)
        limit = self.MAX_SPEED * dt
        self._smooth += max(-limit, min(limit, change))
        if abs(self._held - self._smooth) < 0.2:
            self._smooth = self._held
        self._smooth_done = self._smooth == self._held
        return self._smooth

    def step(self):
        twist = self.twist
        if twist is None or not self.enabled:
            self.steering, self.state = False, "off"
            self.pump.clear()
            self._reset_tracking()
            return
        if twist:
            target = twist * self.max_angle
            self.steering = True
        elif self.steering:
            target = 0.0                   # twist centred: bring the view back to straight ahead
        else:
            self.state = "idle"            # centred and already back: the mouse is free
            self._reset_tracking()
            now = time.time()
            if now >= self._next_check and (self.api.key or self.api.load_key()):
                self._next_check = now + 0.5
                yaw = self._read_yaw()     # display only
                self.yaw = wrap_angle(yaw) if yaw is not None else None
            return

        if not self.api.key and not self.api.load_key():
            self.state = "no game"
            return
        if not game_has_focus():
            self.state = "game not focused"
            self.pump.clear()
            self._reset_tracking()
            return
        now = time.time()
        if now >= self._next_check:        # don't fight a mouse cursor (menus, interacting with a control)
            self._next_check = now + 0.25
            cursor = self.api.get_value("Player.Function.IsControllingCursor")
            ignored = self.api.get_value("Player.Function.IsLookInputIgnored")
            self._blocked = bool(cursor) or bool(ignored)
        if self._blocked:
            self.state = "cursor active"
            self.pump.clear()
            self._reset_tracking()
            return

        yaw = self._read_yaw()
        if yaw is None:
            self.state = "no game"
            return
        yaw = wrap_angle(yaw)              # -180..180; the cab view can't turn all the way round,
        self.yaw = yaw                     # so angles are never wrapped "the short way" below
        if self._prev_yaw is not None and abs(wrap_angle(yaw - self._prev_yaw)) < 0.05:
            self._still += 1
        else:
            self._still = 0
        self._prev_yaw = yaw
        self._quiet += 1
        settled = self._quiet >= 3 and self._still >= 2
        if settled or self._predicted is None:
            self._settled(yaw, target)
        elif self._still >= 5 and abs(self._sent) > 3 * self.counts_per_degree:
            # being pushed but not moving for ~0.17 s (longer than game input lag): the view is at
            # its edge. Re-sync with the real
            # angle now, so moves the game discarded don't make the view overshoot later.
            self._stuck_target = target
            self._sent, self._settled_yaw = 0, yaw
            self._predicted = self._smooth = yaw
            self.pump.clear()

        if self._stuck_target is not None:
            if abs(target - self._stuck_target) < 1.0:
                self._smooth = yaw         # glide away from where the view really is, not past the limit
                self.state = "at view limit"
                return
            self._stuck_target = None

        target = self._ease(target, yaw, now)
        error = target - self._predicted
        if abs(error) < self.TOLERANCE:
            if settled and abs(target - yaw) < self.TOLERANCE and not twist and self._smooth_done:
                self.steering = False      # back at centre: hand the view back to the mouse
            self.state = "following" if twist else ("idle" if not self.steering else "returning to centre")
            return
        if not self.learned:
            if self._sent:
                return                     # first move sent: wait until the view settles to measure it
            error = max(-20.0, min(20.0, error))   # small first move until sensitivity is measured
        dx = max(-self.MAX_STEP, min(self.MAX_STEP, error * self.counts_per_degree))
        dx = int(round(dx)) or (1 if error > 0 else -1)
        if not self._sent:
            self._still = 0                # a new push: only stillness from here on means "at the edge"
        self.pump.add(dx * self.direction)
        self._predicted += dx / self.counts_per_degree
        self._sent += dx
        self._quiet = 0
        self.state = "following" if twist else "returning to centre"


class TrainControls:
    def __init__(self, api, log=None):
        self.api = api
        self.log = log
        self.train_id = None
        self.throttle = self.brake = self.reverser = self.aws = self.alerter = None
        self.door_open_left = self.door_open_right = self.door_close_left = self.door_close_right = None
        self.from_files = False           # the train's controls were found in the game files
        self.cab = None                   # on a train with a cab at each end: the one used, "Front" / "Back"
        self.cab_known = False            # and the game said it's in use (else the front one is assumed)

    def speed(self):
        """Train speed in m/s, or None if the game doesn't report it."""
        try:
            v = self.api.get_value("CurrentDrivableActor.Function.HUD_GetSpeed")
            return float(v) if isinstance(v, (int, float)) else None
        except Exception:
            return None

    def active_cab(self):
        """The end of the train whose cab is in use, "Front" or "Back", or None if the game doesn't say
        (no cab in use yet)."""
        try:
            v = self.api.get("CurrentDrivableActor.Function.GetActiveCabSide").get("Values") or {}
        except Exception:
            return None
        sides = [s for s in ("Front", "Back") if v.get(s) is True]
        return sides[0] if len(sides) == 1 else None

    def cab_changed(self):
        """True when a train with a cab at each end is now driven from the other one, so its controls have to
        be found again."""
        if self.cab is None:
            return False
        cab = self.active_cab()
        return cab is not None and cab != self.cab

    def _this_cab(self, names, handles):
        """On a train with a cab at each end (Class 66, 47, 86, BR 101, Vectron, Class 153...) only the cab in
        use drives it, so the other cab's controls are left out. Which cab each control is in comes from the
        game files."""
        self.cab, self.cab_known = None, False
        if len(driving_cabs(handles)) < 2:
            return names
        active = self.active_cab()
        self.cab, self.cab_known = active or "Front", active is not None
        return [n for n in names if getattr(handles.get(n), "side", None) in (None, self.cab)]

    def _identifiers(self, names):
        # some reversers are called switches (Class 380 DirectionSwitch, Class 86 / 87 MasterSwitch)
        skip = [x for x in EXCLUDE if x not in ("reverser", "switch")]

        def resets(n):
            # vigilance reset buttons go by many names (Class 142 / 47 "DVDReset_F (PushButton)", ACS-64
            # "Acknowledge_F"): without them the bridge used a DSD pedal the game holds down
            words = [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", n)]
            return (any(w.startswith(("reset", "ack")) for w in words)
                    and not any(x in n.lower() for x in ALERTER_NAME_SKIP))

        candidates = [n for n in names if not any(x in n.lower() for x in skip)
                      or ("aws" in n.lower() and not any(x in n.lower() for x in AWS_NAME_SKIP))
                      or (any(w in n.lower() for w in ALERTER_NAMES)
                          and not any(x in n.lower() for x in ALERTER_NAME_SKIP))
                      or resets(n)]

        def ident(n):
            try:
                v = self.api.get(f"CurrentDrivableActor/{n}.Property.InputIdentifier").get("Values") or {}
                return n, str(v.get("identifier") or "")
            except Exception:
                return n, ""

        with ThreadPoolExecutor(max_workers=16) as pool:
            return [(n, i) for n, i in pool.map(ident, candidates) if i and i != "None"]

    def _enabled(self, n):
        # some trains carry a spare copy of a control that the game has switched off
        try:
            return self.api.get_value(f"CurrentDrivableActor/{n}.Property.bInputEnabled") is not False
        except Exception:
            return True

    def _detect_doors(self, names):
        """Door open / close buttons for each side. Some trains carry two sets of desk buttons, only one of
        them wired to the driver's position (Class 350); the wired set is used."""
        found = []                                   # (action, side, name); side None = both sides
        for n in names:
            words = [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", n)]
            opens, closes = bool({"open", "release"} & set(words)), "close" in words
            if (not any(w.startswith("door") for w in words) or "cabdoor" in "".join(words)
                    or any(w.startswith(x) for w in words for x in DOOR_SKIP) or opens == closes):
                continue
            side = "left" if {"l", "left"} & set(words) else "right" if {"r", "right"} & set(words) else None
            if opens and side is None:
                continue                             # never open both sides at once
            found.append(("open" if opens else "close", side, n))

        def rank(n):
            # 0 = driver's position, 1 = somewhere else in the cab, 2 = not wired up; None = not a button
            path = f"CurrentDrivableActor/{n}"
            try:
                if "button" not in str(self.api.get_value(path + ".ObjectClass") or "").lower():
                    return None                      # e.g. the doors themselves
                env = self.api.get(path + ".Property.InteractionEnvironmentComponent").get("Values") or {}
            except Exception:
                return None
            if not self._enabled(n):
                return None
            env = str(env.get("componentName") or "None").lower()
            return 0 if "driver" in env else 2 if env == "none" else 1

        ranks = {n: rank(n) for _, _, n in found}
        for action in ("open", "close"):
            for side in ("left", "right"):
                group = [(ranks[n], n) for a, s, n in found if a == action and s in (side, None)
                         and ranks[n] is not None]
                best = [n for r, n in group if r == min(r for r, _ in group)]
                if best:
                    buttons = [PushButton(self.api, n) for n in best]
                    setattr(self, f"door_{action}_{side}",
                            buttons[0] if len(buttons) == 1 else ButtonGroup(buttons))

    def detect(self):
        handles = train_handles(self.train_id)
        self.from_files = bool(handles)
        names = self._this_cab(node_names(self.api.list("CurrentDrivableActor")), handles)
        self.throttle = self.brake = self.reverser = self.aws = self.alerter = None
        self.door_open_left = self.door_open_right = self.door_close_left = self.door_close_right = None
        ids = self._identifiers(names)

        a = (next((n for n, i in ids if i.lower().replace("_", "") in AWS_IDS), None)
             or next((n for n in names if "aws" in n.lower()
                      and any(w in n.lower() for w in ("reset", "ack"))
                      and not any(x in n.lower() for x in AWS_NAME_SKIP)), None))
        if a:
            self.aws = PushButton(self.api, a)

        alerters = ([n for n, i in ids if i.lower().replace("_", "") in ALERTER_IDS and self._enabled(n)]
                    or [n for n in names if any(w in n.lower() for w in ALERTER_NAMES)
                        and not any(x in n.lower() for x in ALERTER_NAME_SKIP) and self._enabled(n)])
        if alerters:
            # where a train has both, use a push button rather than a pedal the game holds down, and a cab
            # control before anything else found by name
            self.alerter = min((PushButton(self.api, n) for n in alerters),
                               key=lambda b: (not b.is_control, b.pressed < b.released))
        self._detect_doors(names)

        r = (next((n for n, i in ids if any(k in i.lower() for k in REVERSER_IDS)), None)
             or pick(names, ["reverser"], [x for x in EXCLUDE if x != "reverser"]))
        if r:
            rev = Reverser(self.api, r, dict(ids).get(r, ""), train=self.train_id, model=handles.get(r))
            self.reverser = rev if rev.lever.works() else None

        def is_lever(n):
            # some trains give push buttons a driving-lever identifier (Class 375 "BrakeHold" button
            # is "AutomaticBrake"); throttle and brake must be real levers
            try:
                cls = self.api.get_value(f"CurrentDrivableActor/{n}.ObjectClass") or ""
            except Exception:
                cls = ""
            return "button" not in str(cls).lower()

        t = next((n for n, i in ids if any(k in i.lower() for k in THROTTLE_IDS) and is_lever(n)), None)
        b = next((n for n, i in ids if any(k in i.lower() for k in BRAKE_IDS) and is_lever(n)), None)
        id_of = dict(ids)
        if not t:
            t = next((n for n in pick_all(names, COMBINED_NAMES, EXCLUDE)
                      + pick_all(names, THROTTLE_NAMES, EXCLUDE + ["brake"]) if is_lever(n)), None)
        if not b:
            # by name, but never a lever the game itself calls another kind of brake (Class 323
            # "RegenBrakes" is the regenerative brake on/off switch, identifier DynamicBrake) or a throttle
            # (CD 843: "ThrottleAndBrakeCab_2", the other cab's power/brake handle)
            not_brakes = OTHER_BRAKE_IDS + THROTTLE_IDS
            b = next((n for n in pick_all(names, BRAKE_NAMES, EXCLUDE) if n != t and is_lever(n)
                      and not any(x in id_of.get(n, "").lower() for x in not_brakes)), None)
        if t:
            lever = Lever(self.api, t, id_of.get(t, ""), train=self.train_id, model=handles.get(t))
            self.throttle = lever if lever.works() else None
        if b:
            lever = Lever(self.api, b, id_of.get(b, ""), train=self.train_id, role="brake",
                          model=handles.get(b))
            self.brake = lever if lever.works() else None
        return names, ids

    def buttons(self):
        """The cab buttons worked by joystick buttons, {"aws" / "alerter" / "door_open_left"...: button}."""
        names = ["aws", "alerter"] + [f"door_{a}_{s}" for a in ("open", "close") for s in ("left", "right")]
        return {n: getattr(self, n) for n in names if getattr(self, n)}

    def let_go(self):
        """Let go of any handle or button the bridge is holding in place (see Lever.let_go, PushButton)."""
        for control in [self.throttle, self.brake] + list(self.buttons().values()):
            if control:
                control.let_go()

    def press(self, name, down):
        """Press (down=True) or release a cab button worked by a joystick button: "aws", "alerter",
        "door_open_left" etc. Returns a message for the log, or None."""
        button = getattr(self, name)
        if button is None:
            return None
        if down and name.startswith("door_open"):
            speed = self.speed()
            if speed is not None and abs(speed) > DOOR_MAX_SPEED:
                return f"Doors NOT opened: train is moving ({abs(speed) * 3.6:.0f} km/h)"
        button.set(down)
        return PRESS_MESSAGES[name] if down else None

    def targets(self, y):
        """Lever values for stick position y: -1 full brake .. 0 neutral .. +1 full power."""
        out = {}
        t, b = self.throttle, self.brake
        if t:
            if y >= 0:
                out[t] = t.value_between(t.neutral, t.power_end, y)
            elif b or t.brake_end is None:
                out[t] = t.neutral                       # separate brake handle does the braking
            else:
                out[t] = t.value_between(t.neutral, t.brake_end, -y)   # combined lever brake side
        if b and b.positions:
            out[b] = b.valve_position(max(0.0, -y))     # Release / Hold / Apply, a third of the travel each
        elif b:
            # released .. full brake: from the game files these can run either way round on the handle
            start, end = (b.neutral, b.brake_end) if b.from_files else (b.safe_lo, b.safe_hi)
            out[b] = b.value_between(start, end, max(0.0, -y))
        return out

    def apply(self, y):
        for lever, value in self.targets(y).items():
            message = None
            if (lever is self.throttle and lever.combined and not lever.neutral_verified
                    and value == lever.neutral):
                message = lever.settle_neutral()     # first time at Off: check where Off really is
            else:
                message = lever.set_value(value)
                at_full_brake = (lever.brake_end is not None and value == lever.brake_end
                                 and not lever.brake_end_verified)
                if at_full_brake and (lever is self.brake or (lever.combined and lever.neutral_verified)):
                    extended = lever.extend_brake()  # first time at full brake: check how far it goes
                    message = f"{message}; {extended}" if message and extended else message or extended
            if message and self.log:
                self.log(message)

    def describe(self):
        parts = []
        if self.cab:
            parts.append(f"{self.cab.lower()} cab" + ("" if self.cab_known else " (no cab in use yet)"))
        if self.throttle:
            kind = "power+brake lever" if self.throttle.brake_end is not None and not self.brake else "throttle"
            parts.append(f"{kind} {self.throttle}")
        if self.brake:
            parts.append(f"brake {self.brake}")
        if not parts:
            parts.append("NO throttle/brake controls found (run with --list)")
        parts.append(f"reverser {self.reverser}" if self.reverser else "no reverser found")
        parts.append(f"AWS {self.aws}" if self.aws else "no AWS button found")
        parts.append(f"alerter {self.alerter}" if self.alerter else "no alerter / DSD / SIFA found")
        doors = [f"{action} {side} {b}" for action in ("open", "close") for side in ("left", "right")
                 for b in [getattr(self, f"door_{action}_{side}")] if b]
        parts.append("doors: " + ", ".join(doors) if doors else "no door buttons found")
        if not self.from_files and USE_GAME_FILES:
            parts.append("handle places estimated (the game files couldn't be read, or don't have this train)")
        return ", ".join(parts)


# ---------------------------------------------------------------- joystick
def open_joystick():
    pygame.joystick.quit()
    pygame.joystick.init()
    sticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    if not sticks:
        return None
    for s in sticks:
        if JOYSTICK_NAME_HINT in s.get_name().lower():
            return s
    return sticks[0]


class AxisFilter:
    """Calms a stick axis: smooths sensor flicker and ignores tiny reversals (like the slack in a real
    handle), so moving the stick slowly never makes a lever twitch back and forth. Centre (dead zone)
    and the ends of travel still come through exactly."""

    def __init__(self, band=STICK_BAND, smoothing=STICK_SMOOTHING):
        self.band = band
        self.smoothing = smoothing
        self.value = None
        self._smooth = 0.0
        self._time = 0.0

    def update(self, raw, now=None):
        now = time.time() if now is None else now
        if self.value is None or raw == 0.0:      # first reading, or stick in the dead zone: exact
            self.value = self._smooth = raw
            self._time = now
            return raw
        dt = min(0.1, max(0.0, now - self._time))
        self._time = now
        if self.smoothing > 0:
            self._smooth += (raw - self._smooth) * (1.0 - math.exp(-dt / self.smoothing))
        else:
            self._smooth = raw
        if self._smooth > self.value + self.band:
            self.value = self._smooth - self.band
        elif self._smooth < self.value - self.band:
            self.value = self._smooth + self.band
        if abs(self._smooth) > 1.0 - self.band:   # end of travel: full power / full brake exactly
            self.value = math.copysign(1.0, self._smooth)
        return self.value


def read_y(stick, axis=None, invert=None, deadzone=None):
    axis = Y_AXIS if axis is None else axis
    invert = INVERT_Y if invert is None else invert
    deadzone = DEADZONE if deadzone is None else deadzone
    raw = stick.get_axis(axis) if axis < stick.get_numaxes() else 0.0
    y = raw if invert else -raw          # joystick forward is negative; make it positive
    if abs(y) < deadzone:
        return 0.0
    sign = 1.0 if y > 0 else -1.0
    return sign * (abs(y) - deadzone) / (1.0 - deadzone)


# ---------------------------------------------------------------- modes
def show_axes():
    pygame.init()
    stick = open_joystick()
    if not stick:
        print("No joystick found.")
        return
    print(f"Joystick: {stick.get_name()}  ({stick.get_numaxes()} axes). Ctrl+C to stop.")
    while True:
        pygame.event.pump()
        vals = "  ".join(f"a{i}:{stick.get_axis(i):+.2f}" for i in range(stick.get_numaxes()))
        print("\r" + vals + f"   -> Y used: {read_y(stick):+.2f}   ", end="", flush=True)
        time.sleep(0.05)


def list_controls():
    api = TSWApi()
    if not api.load_key():
        print(f"API key not found at {KEY_FILE}. Launch TSW7 with -HTTPAPI first.")
        return
    controls = TrainControls(api)
    controls.train_id = api.get_value("CurrentDrivableActor.ObjectClass")
    print("Reading the game files...")
    print("Train:", controls.train_id)
    names, ids = controls.detect()
    print("Controls with a game input identifier:")
    for n, i in ids:
        print(f"   {n:45s} {i}")
    print(f"({len(names)} controls in total)")
    print("\nSelected:", controls.describe())
    print("\nStick position -> lever values that would be sent (nothing is sent now):")
    for y in (1.0, 0.5, 0.0, -0.5, -1.0):
        vals = ", ".join(f"{lv.name}={v:.3f}" for lv, v in controls.targets(y).items())
        print(f"   {y:+.1f}: {vals}")


def run():
    load_game_files()
    pygame.init()
    api = TSWApi()
    controls = TrainControls(api, log=lambda msg: print(time.strftime("%H:%M:%S"), msg, flush=True))
    stick = None
    last_sent = None
    last_train_check = 0.0
    status = None

    def say(msg):
        nonlocal status
        if msg != status:
            print(time.strftime("%H:%M:%S"), msg, flush=True)
            status = msg

    rev_sync = ReverserSync(lambda msg: print(time.strftime("%H:%M:%S"), msg, flush=True))
    rev_zone = None
    y_filter = AxisFilter()
    look = LookController(lambda msg: print(time.strftime("%H:%M:%S"), msg, flush=True))
    look.start()
    atexit.register(controls.let_go)          # never leave a handle held when the bridge stops
    cab_buttons = {AWS_BUTTON: "aws", ALERTER_BUTTON: "alerter",
                   DOOR_OPEN_LEFT_BUTTON: "door_open_left", DOOR_OPEN_RIGHT_BUTTON: "door_open_right",
                   DOOR_CLOSE_LEFT_BUTTON: "door_close_left", DOOR_CLOSE_RIGHT_BUTTON: "door_close_right"}

    print("TSW7 joystick bridge running. Ctrl+C to stop.")
    while True:
        time.sleep(1.0 / POLL_HZ)
        for event in pygame.event.get():
            if event.type == pygame.JOYDEVICEREMOVED:
                stick = None
                controls.let_go()
            elif (event.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP) and event.button in cab_buttons
                  and controls.train_id is not None):
                try:
                    message = controls.press(cab_buttons[event.button], event.type == pygame.JOYBUTTONDOWN)
                    if message:
                        controls.log(message)
                except Exception as e:
                    say(f"Button {event.button + 1} failed: {e}")

        if stick is None:
            stick = open_joystick()
            if stick is None:
                say("Waiting for joystick to be plugged in...")
                time.sleep(1)
                continue
            say(f"Joystick: {stick.get_name()}")

        if not api.key and not api.load_key():
            say("Waiting for TSW7 (start it with -HTTPAPI in Steam launch options)...")
            time.sleep(2)
            continue

        now = time.time()
        if now - last_train_check > TRAIN_CHECK_SECONDS:
            last_train_check = now
            try:
                train = api.get_value("CurrentDrivableActor.ObjectClass")
                if train != controls.train_id or controls.cab_changed():
                    controls.let_go()
                    controls.train_id = train
                    controls.detect()
                    last_sent = None
                    rev_sync.reset()
                    say(f"Train: {train} -> {controls.describe()}")
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    api.load_key()   # key may have been regenerated
                say(f"Game API error {e.code} (are you in a train cab?)")
                controls.let_go()
                controls.train_id = None
                continue
            except Exception:
                say("Waiting for TSW7 API on port 31270 (is the game running with -HTTPAPI?)...")
                controls.let_go()
                controls.train_id = None
                continue

        if USE_LOOK and LOOK_AXIS < stick.get_numaxes():
            raw = stick.get_axis(LOOK_AXIS)
            look.twist = shape_axis(-raw if LOOK_INVERT else raw, LOOK_DEADZONE)
        else:
            look.twist = None

        if controls.train_id is None:
            continue

        if USE_REVERSER and REVERSER_AXIS < stick.get_numaxes():
            raw = stick.get_axis(REVERSER_AXIS)
            rev_zone = reverser_zone(raw if REVERSER_INVERT else -raw, rev_zone)
            try:
                rev_sync.update(controls, rev_zone)
            except Exception as e:
                say(f"Reverser failed: {e}")
                controls.let_go()
                controls.train_id = None
                continue

        y = y_filter.update(read_y(stick))
        if last_sent is not None and abs(y - last_sent) < SEND_THRESHOLD:
            continue   # stick not moved: leave levers alone so keyboard still works
        if last_sent is None:
            last_sent = y   # don't snap levers on startup / train change until stick moves
            continue
        try:
            controls.apply(y)
            last_sent = y
        except Exception as e:
            say(f"Send failed: {e}")
            controls.let_go()
            controls.train_id = None


if __name__ == "__main__":
    try:
        if "--axes" in sys.argv:
            show_axes()
        elif "--list" in sys.argv:
            list_controls()
        else:
            run()
    except KeyboardInterrupt:
        print("\nStopped.")
