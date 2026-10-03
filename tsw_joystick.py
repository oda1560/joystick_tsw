"""
Train Sim World 7 - joystick throttle/brake bridge.

Joystick Y axis:
    push forward  -> throttle (power)
    centre        -> neutral (throttle 0, brake released)
    pull back     -> train brake

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

Usage:
    python tsw_joystick.py              run the bridge
    python tsw_joystick.py --list       print the current train's controls (debug)
    python tsw_joystick.py --axes       show live joystick axis values (debug)
"""

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
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")  # read stick while the game has focus
import pygame

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
ALERTER_NAME_SKIP = ["isolat", "cover", "cutout", "fault", "mcb", "_cb", "service", "test", "device",
                     "light", "lamp"]

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
EXCLUDE = ["dynamic", "independent", "loco", "emergency", "park", "handbrake", "release",
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


def is_emergency(label):
    return "emerg" in label or label.strip() in ("eb", "e")


def is_brake_label(label):
    return "brake" in label or is_emergency(label) or label.strip().startswith("b")


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


class Lever:
    """One controllable lever, its safe input range and (for combined levers) neutral point."""

    def __init__(self, api, name, ident="", train=None):
        self.api = api
        self.name = name
        self.ident = ident or ""
        self.path = f"CurrentDrivableActor/{name}.InputValue"
        self.lo, self.hi = self._range()
        self.note = ""
        self.zones = self._zones()
        self.safe_lo, self.safe_hi = self._exclude_emergency()
        self.combined = ("brake" in name.lower() or "brake" in self.ident.lower()
                         or any(is_brake_label(z[2]) for z in self.zones)) \
            and not any(b in self.ident.lower() for b in BRAKE_IDS)
        self.neutral = self.safe_lo
        self.power_end = self.safe_hi
        self.brake_end = None
        self.neutral_verified = False     # Off position confirmed with the game (combined levers)
        self._calibration_key = f"{train}|{name}" if train else None
        if self.combined:
            self._find_neutral()
            learned = load_calibration().get(self._calibration_key) if self._calibration_key else None
            if isinstance(learned, (int, float)) and self.safe_lo <= learned <= self.safe_hi:
                self.neutral, self.neutral_verified = float(learned), True
                self.note = re.sub(r"neutral [-\d.e]+ from [^,]*",
                                   f"neutral {self.neutral:.3g} (learned earlier)", self.note)
        self.notches = self._notch_positions()
        self._notch = None                # index of the notch last sent
        self._last_sent = (None, 0.0)     # (value, time)

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
        span = self.hi - self.lo
        positions = [self.lo + (o - out_lo) / (out_hi - out_lo) * span for o in outputs]
        positions = [p for p in positions if self.safe_lo - 1e-6 <= p <= self.safe_hi + 1e-6]
        return positions if len(positions) >= 2 else None

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

    def _zones(self):
        """Named notches/zones as (start, end, label, source) in this lever's input units."""
        named = self._get("Property.DisplayInfo").get("namedValues") or []
        out_lo = self._get("Function.GetMinimumOutputValue").get("ReturnValue")
        out_hi = self._get("Function.GetMaximumOutputValue").get("ReturnValue")
        custom = self._get("Property.bCustomOutputValueMapping").get("Value")
        out_ok = (isinstance(out_lo, (int, float)) and isinstance(out_hi, (int, float))
                  and out_hi > out_lo and not custom)
        self._out_range = (float(out_lo), float(out_hi)) if out_ok else None
        span = self.hi - self.lo

        def convert(v, source):
            if source == "Input":
                return v
            if source in ("InputNormalised", "OutputNormalised"):
                return self.lo + v * span
            if source == "Output" and out_ok:
                return self.lo + (v - out_lo) / (out_hi - out_lo) * span   # linear estimate
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
        for a, b, _, _ in emerg:
            if a is None:
                self.note = "emergency position unknown, trimmed 20% both ends"
                span = self.hi - self.lo
                return self.lo + 0.2 * span, self.hi - 0.2 * span
            # stay half an emergency-notch width away from where the emergency zone starts
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
            if prev and abs(x - prev[0]) > 1e-6 and (out - prev[1]) / (x - prev[0]) > 0:
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

    def set_value(self, value):
        value = min(self.safe_hi, max(self.safe_lo, value))
        if self.notches:
            # only ever send exact notch positions: an in-between value makes the game draw the handle
            # there and then snap it back to the notch, which looks like the handle jumping about
            value = self._snap(value)
            last_value, last_time = self._last_sent
            if value == last_value and time.time() - last_time < 0.5:
                return                     # already there (re-sent now and then in case keys moved it)
        self.api.set(self.path, value)
        self._last_sent = (value, time.time())

    def __repr__(self):
        text = f"{self.name} [{self.safe_lo:.3g}..{self.safe_hi:.3g}]"
        return f"{text} ({self.note})" if self.note else text


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

    def __init__(self, api, name, ident="", train=None):
        self.lever = Lever(api, name, ident)
        self.api = api
        self.name = name
        self.path = self.lever.path
        self.notches = {}                 # 'forward' / 'neutral' / 'reverse' -> input value
        self.out_ranges = {}              # the same notches in output units, where the game names them so
        self.backwards = False            # True when the output falls as the input rises
        self.learned = set()              # notches confirmed with the game somewhere other than estimated
        lo, hi = self.lever.lo, self.lever.hi
        for prefix in (False, True):      # exact names first, then e.g. "Forward 1"
            for a, b, label, source in self.lever.zones:
                pos = reverser_label(label, prefix)
                if a is not None and pos and pos not in self.notches:
                    self.notches[pos] = min(hi, max(lo, (a + b) / 2))
                    if source == "Output" and label in self.lever.out_named:
                        self.out_ranges[pos] = self.lever.out_named[label]
        self.ok = all(p in self.notches for p in REVERSER_LABELS)
        self._calibration_key = f"{train}|{name}" if train else None
        if self._calibration_key:
            for pos in self.out_ranges:
                learned = load_calibration().get(f"{self._calibration_key}|{pos}")
                if isinstance(learned, (int, float)) and lo <= learned <= hi:
                    self.notches[pos] = float(learned)
                    self.learned.add(pos)
        if self.out_ranges and self.lever._out_range:
            x = self._input()
            self._learn_direction((x, None), (x, self._output()))

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
        short wait)."""
        self.api.set(self.path, x)
        out = None
        for wait in (0.05, 0.1, 0.15):
            time.sleep(wait)
            out = self._output()
            if out is None or self._at(position, out):
                break
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
        target, moved_since_change = sum(rng) / 2, 0.0
        for _ in range(3 * count):
            way = 1.0 if (target > out) != self.backwards else -1.0
            nx = min(hi, max(lo, x + way * notch / 3))
            if nx == x:
                return                     # end of the handle's travel
            nout = self._move(nx, position)
            if nout is None:
                return
            if nout != out:
                self._learn_direction((x, out), (nx, nout))
                moved_since_change = 0.0
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
        if self.out_ranges and self.lever._out_range:
            out = self._output()           # as the game reports it
            for label, (a, b) in self.lever.out_named.items():
                if out is not None and a <= out <= b:
                    return reverser_label(label) or label.strip()
        v = self.lever.api.get_value(self.lever.path)
        zones = [z for z in self.lever.zones if z[0] is not None]
        if not isinstance(v, (int, float)) or not zones:
            return None
        lo, hi = self.lever.lo, self.lever.hi
        nearest = min(zones, key=lambda z: abs((max(z[0], lo) + min(z[1], hi)) / 2 - v))
        return reverser_label(nearest[2]) or nearest[2].strip()

    def __repr__(self):
        if not self.ok:
            return f"{self.name} (Forward/Neutral/Reverse notches not recognised - not used)"
        text = ", ".join(f"{p} {v:.3g}" for p, v in self.notches.items())
        return f"{self.name} ({text}{', runs backwards' if self.backwards else ''})"


class PushButton:
    """A momentary cab button, held down in the game for as long as the joystick button is held."""

    def __init__(self, api, name):
        self.api = api
        self.name = name
        self.path = f"CurrentDrivableActor/{name}.InputValue"
        try:
            lo = api.get_value(f"CurrentDrivableActor/{name}.Function.GetMinimumInputValue")
            hi = api.get_value(f"CurrentDrivableActor/{name}.Function.GetMaximumInputValue")
        except Exception:
            lo = hi = None
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi != lo:
            # the minimum is where the button rests; a pedal the game holds down by default (Class 350
            # DSD) counts backwards, resting at 1 and pressed at 0
            self.released, self.pressed = float(lo), float(hi)
        else:
            self.released, self.pressed = 0.0, 1.0

    def set(self, down):
        self.api.set(self.path, self.pressed if down else self.released)

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

    def speed(self):
        """Train speed in m/s, or None if the game doesn't report it."""
        try:
            v = self.api.get_value("CurrentDrivableActor.Function.HUD_GetSpeed")
            return float(v) if isinstance(v, (int, float)) else None
        except Exception:
            return None

    def _identifiers(self, names):
        skip = [x for x in EXCLUDE if x != "reverser"]
        candidates = [n for n in names if not any(x in n.lower() for x in skip)
                      or ("aws" in n.lower() and not any(x in n.lower() for x in AWS_NAME_SKIP))
                      or (any(w in n.lower() for w in ALERTER_NAMES)
                          and not any(x in n.lower() for x in ALERTER_NAME_SKIP))]

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
        names = node_names(self.api.list("CurrentDrivableActor"))
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
            # where a train has both, use a push button rather than a pedal the game holds down
            self.alerter = min((PushButton(self.api, n) for n in alerters),
                               key=lambda b: b.pressed < b.released)
        self._detect_doors(names)

        r = (next((n for n, i in ids if any(k in i.lower() for k in REVERSER_IDS)), None)
             or pick(names, ["reverser"], [x for x in EXCLUDE if x != "reverser"]))
        if r:
            rev = Reverser(self.api, r, dict(ids).get(r, ""), train=self.train_id)
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
            # "RegenBrakes" is the regenerative brake on/off switch, identifier DynamicBrake)
            b = next((n for n in pick_all(names, BRAKE_NAMES, EXCLUDE) if n != t and is_lever(n)
                      and not any(x in id_of.get(n, "").lower() for x in OTHER_BRAKE_IDS)), None)
        if t:
            lever = Lever(self.api, t, id_of.get(t, ""), train=self.train_id)
            self.throttle = lever if lever.works() else None
        if b:
            lever = Lever(self.api, b, id_of.get(b, ""))
            self.brake = lever if lever.works() else None
        return names, ids

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
        if b:
            out[b] = b.value_between(b.safe_lo, b.safe_hi, max(0.0, -y))
        return out

    def apply(self, y):
        for lever, value in self.targets(y).items():
            if (lever is self.throttle and lever.combined and not lever.neutral_verified
                    and value == lever.neutral):
                message = lever.settle_neutral()     # first time at Off: check where Off really is
                if message and self.log:
                    self.log(message)
            else:
                lever.set_value(value)

    def describe(self):
        parts = []
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
    cab_buttons = {AWS_BUTTON: "aws", ALERTER_BUTTON: "alerter",
                   DOOR_OPEN_LEFT_BUTTON: "door_open_left", DOOR_OPEN_RIGHT_BUTTON: "door_open_right",
                   DOOR_CLOSE_LEFT_BUTTON: "door_close_left", DOOR_CLOSE_RIGHT_BUTTON: "door_close_right"}

    print("TSW7 joystick bridge running. Ctrl+C to stop.")
    while True:
        time.sleep(1.0 / POLL_HZ)
        for event in pygame.event.get():
            if event.type == pygame.JOYDEVICEREMOVED:
                stick = None
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
                if train != controls.train_id:
                    controls.train_id = train
                    controls.detect()
                    last_sent = None
                    rev_sync.reset()
                    say(f"Train: {train} -> {controls.describe()}")
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    api.load_key()   # key may have been regenerated
                say(f"Game API error {e.code} (are you in a train cab?)")
                controls.train_id = None
                continue
            except Exception:
                say("Waiting for TSW7 API on port 31270 (is the game running with -HTTPAPI?)...")
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
