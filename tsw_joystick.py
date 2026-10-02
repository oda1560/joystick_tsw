"""
Train Sim World 7 - joystick throttle/brake bridge.

Joystick Y axis:
    push forward  -> throttle (power)
    centre        -> neutral (throttle 0, brake released)
    pull back     -> train brake

Base slider (Z axis) -> reverser: + end Forward, middle Neutral, - end Reverse
    (only moved when the train is stopped, never to Off)

Button 1 (trigger)   -> AWS acknowledge (held for as long as you hold the trigger)

Talks to TSW's External Interface API (launch the game with -HTTPAPI).
The stick only sends values when you move it, so the keyboard keeps working.

Usage:
    python tsw_joystick.py              run the bridge
    python tsw_joystick.py --list       print the current train's controls (debug)
    python tsw_joystick.py --axes       show live joystick axis values (debug)
"""

import json
import os
import sys
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
POLL_HZ = 30
TRAIN_CHECK_SECONDS = 2.0        # how often to check whether you changed train

USE_REVERSER = True
REVERSER_AXIS = 3                # Extreme 3D Pro base slider: + end Forward, middle Neutral, - end Reverse
REVERSER_INVERT = False
REVERSER_MAX_SPEED = 0.3         # m/s; the reverser is never moved faster than this (~1 km/h)

AWS_BUTTON = 0                   # joystick button for AWS acknowledge (0 = trigger, labelled "1")

# Primary detection: the game's own input identifier on each control (same across trains).
THROTTLE_IDS = ["throttle", "mastercontroller", "combinedthrottle"]   # substring match
BRAKE_IDS = ["automaticbrake", "trainbrake"]                           # substring match

# Fallback detection by control name, case-insensitive, first match wins.
COMBINED_NAMES = ["throttlebrake", "throttleandbrake", "mastercontroller", "combinedthrottlebrake",
                  "powerbrake", "power_brake", "tbc", "controller(lever)"]
THROTTLE_NAMES = ["throttle", "powerhandle", "power"]
BRAKE_NAMES = ["trainbrake", "train_brake", "automaticbrake", "brakehandle", "stepbrake", "brake"]
NEUTRAL_LABELS = ["off", "neutral", "coast", "idle", "n", "0"]

AWS_IDS = ["awsreset", "awsacknowledge"]      # compared with "_" removed, lower case
AWS_NAME_SKIP = ["isolat", "cover", "cutout", "fault", "sunflower", "mcb", "service", "test"]

REVERSER_IDS = ["reverser"]
REVERSER_LABELS = {"forward": ["forward", "fwd", "fw", "f", "ahead"],
                   "neutral": ["neutral", "n", "mid", "centre", "center"],
                   "reverse": ["reverse", "rev", "r", "backward", "back"]}
EXCLUDE = ["dynamic", "independent", "loco", "emergency", "park", "handbrake", "release",
           "bail", "reverser", "horn", "light", "wiper", "door", "sander", "pantograph",
           # circuit breakers, isolation switches, covers etc. are not the driving levers
           "mcb", "isolat", "cutout", "cover", "button", "switch", "cock", "hose", "lock"]


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


def pick(names, wanted, exclude):
    lowered = [(n, n.lower()) for n in names]
    for w in wanted:
        for orig, low in lowered:
            if w in low and not any(x in low for x in exclude):
                return orig
    return None


def is_emergency(label):
    return "emerg" in label or label.strip() in ("eb", "e")


def is_brake_label(label):
    return "brake" in label or is_emergency(label) or label.strip().startswith("b")


class Lever:
    """One controllable lever, its safe input range and (for combined levers) neutral point."""

    def __init__(self, api, name, ident=""):
        self.api = api
        self.name = name
        self.ident = ident or ""
        self.path = f"CurrentDrivableActor/{name}.InputValue"
        self.lo, self.hi = self._range()
        self.note = ""
        self.zones = self._zones()
        self.safe_lo, self.safe_hi = self._exclude_emergency()
        self.combined = ("brake" in name.lower() or "brake" in self.ident.lower()
                         or any(is_brake_label(z[2]) for z in self.zones if z[3] == "Input")) \
            and not any(b in self.ident.lower() for b in BRAKE_IDS)
        self.neutral = self.safe_lo
        self.power_end = self.safe_hi
        self.brake_end = None
        if self.combined:
            self._find_neutral()

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
        for nv in named:
            r = nv.get("valueRange") or {}
            source = nv.get("valueSource", "")
            if "min" not in r or "max" not in r:
                continue
            a, b = convert(float(r["min"]), source), convert(float(r["max"]), source)
            label = str(nv.get("displayName", "")).lower()
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

    def set_value(self, value):
        self.api.set(self.path, min(self.safe_hi, max(self.safe_lo, value)))

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
    """The train's reverser, driven to its Forward / Neutral / Reverse notches only."""

    def __init__(self, api, name, ident=""):
        self.lever = Lever(api, name, ident)
        self.name = name
        self.notches = {}                 # 'forward' / 'neutral' / 'reverse' -> input value
        lo, hi = self.lever.lo, self.lever.hi
        for prefix in (False, True):      # exact names first, then e.g. "Forward 1"
            for a, b, label, _ in self.lever.zones:
                pos = reverser_label(label, prefix)
                if a is not None and pos and pos not in self.notches:
                    self.notches[pos] = min(hi, max(lo, (a + b) / 2))
        self.ok = all(p in self.notches for p in REVERSER_LABELS)

    def set(self, position):
        self.lever.set_value(self.notches[position])

    def position(self):
        """Current notch: 'forward' / 'neutral' / 'reverse', another notch name (e.g. 'off'), or None."""
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
        return f"{self.name} (" + ", ".join(f"{p} {v:.3g}" for p, v in self.notches.items()) + ")"


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
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > lo:
            self.released, self.pressed = float(lo), float(hi)
        else:
            self.released, self.pressed = 0.0, 1.0

    def set(self, down):
        self.api.set(self.path, self.pressed if down else self.released)

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


class TrainControls:
    def __init__(self, api):
        self.api = api
        self.train_id = None
        self.throttle = self.brake = self.reverser = self.aws = None

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
                      or ("aws" in n.lower() and not any(x in n.lower() for x in AWS_NAME_SKIP))]

        def ident(n):
            try:
                v = self.api.get(f"CurrentDrivableActor/{n}.Property.InputIdentifier").get("Values") or {}
                return n, str(v.get("identifier") or "")
            except Exception:
                return n, ""

        with ThreadPoolExecutor(max_workers=16) as pool:
            return [(n, i) for n, i in pool.map(ident, candidates) if i and i != "None"]

    def detect(self):
        names = node_names(self.api.list("CurrentDrivableActor"))
        self.throttle = self.brake = self.reverser = self.aws = None
        ids = self._identifiers(names)

        a = (next((n for n, i in ids if i.lower().replace("_", "") in AWS_IDS), None)
             or next((n for n in names if "aws" in n.lower()
                      and any(w in n.lower() for w in ("reset", "ack"))
                      and not any(x in n.lower() for x in AWS_NAME_SKIP)), None))
        if a:
            self.aws = PushButton(self.api, a)

        r = (next((n for n, i in ids if any(k in i.lower() for k in REVERSER_IDS)), None)
             or pick(names, ["reverser"], [x for x in EXCLUDE if x != "reverser"]))
        if r:
            rev = Reverser(self.api, r, dict(ids).get(r, ""))
            self.reverser = rev if rev.lever.works() else None

        t = next((n for n, i in ids if any(k in i.lower() for k in THROTTLE_IDS)), None)
        b = next((n for n, i in ids if any(k in i.lower() for k in BRAKE_IDS)), None)
        id_of = dict(ids)
        if not t:
            t = pick(names, COMBINED_NAMES, EXCLUDE) or pick(names, THROTTLE_NAMES, EXCLUDE + ["brake"])
        if not b:
            b = pick(names, BRAKE_NAMES, EXCLUDE)
            if b == t:
                b = None
        if t:
            lever = Lever(self.api, t, id_of.get(t, ""))
            self.throttle = lever if lever.works() else None
        if b:
            lever = Lever(self.api, b, id_of.get(b, ""))
            self.brake = lever if lever.works() else None
        return names, ids

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
    print("Train:", api.get_value("CurrentDrivableActor.ObjectClass"))
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
    controls = TrainControls(api)
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

    print("TSW7 joystick bridge running. Ctrl+C to stop.")
    while True:
        time.sleep(1.0 / POLL_HZ)
        for event in pygame.event.get():
            if event.type == pygame.JOYDEVICEREMOVED:
                stick = None
            elif (event.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP) and event.button == AWS_BUTTON
                  and controls.aws and controls.train_id is not None):
                try:
                    controls.aws.set(event.type == pygame.JOYBUTTONDOWN)
                except Exception as e:
                    say(f"AWS button failed: {e}")

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

        y = read_y(stick)
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
