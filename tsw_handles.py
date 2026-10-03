"""
How a cab control behaves, worked out from its settings in Train Sim World's game files (see tsw_paks.py):
where its notches are, what output the game gives for each handle position, what the game calls each place on
the handle, and how the handle moves when it's sent somewhere.

Most of this is read straight from the files. A few details only show in the game and were worked out there
(Class 331, 802): how strongly a notch pulls the handle in, which way a half-way output step rounds, and how a
gated notch stops a handle that's sent past it.
"""

import math
import re

VHID_BASES = {"IrregularLeverComponent", "SimpleLeverComponent", "PushButtonComponent", "LeverBaseComponent",
              "CouplerLockComponent", "VirtualHIDComponent"}
LEVER_KEYS = ("ValueMap", "Notches", "NumberOfNotches", "InputIdentifier", "DisplayInfo")


def enum(value, default):
    return str(value).split("::")[-1] if value else default


def is_emergency(label):
    label = label.lower()
    return "emerg" in label or label.strip() in ("eb", "e")


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
        # an IrregularLever with no table in the files uses the game's built-in one, which isn't known
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

    def gated_between(self, a, b):
        """True if a gated notch lies between a and b (not counting one that a is in)."""
        lo, hi = min(a, b), max(a, b)
        return any(gated and lo < n_hi + 1e-6 and n_lo - 1e-6 < hi and not n_lo - 1e-6 <= a <= n_hi + 1e-6
                   for n_lo, n_hi, gated in self.notches)

    def resting(self):
        """Where the handle stays when left: in its notches (all through one that's a range), or anywhere on
        a handle without notches. Sending it exactly to one of these needs no notch to pull it in."""
        if not self.notches:
            return [self.lo + (self.hi - self.lo) * i / 200 for i in range(201)]
        xs = set()
        for a, b, _ in self.notches:
            xs.update([a] if b - a < 1e-9 else [a + (b - a) * i / 40 for i in range(41)])
        return sorted(xs)

    def reachable(self, a, b, steps=400):
        """Inputs the handle can rest at between a and b (both included), notches pulling it in."""
        xs = {self.snap(a + (b - a) * i / steps) for i in range(steps + 1)}
        for lo, hi, _ in self.notches:
            xs.update(x for x in (lo, hi) if min(a, b) - 1e-9 <= x <= max(a, b) + 1e-9)
        return sorted(xs)

    # -------- the game's names for places on the handle
    def _named_at(self, x):
        """The game's named place covering the handle at x (the first that does, as the game shows it), and
        the values it's matched by, or (None, values)."""
        out = self.output(x)
        values = {"Input": x, "InputNormalised": (x - self.lo) / (self.hi - self.lo),
                  "Output": out, "OutputNormalised": self.normalised_output(x)}
        for nv in (self.props.get("DisplayInfo") or {}).get("NamedValues") or []:
            r = nv.get("ValueRange") or {}
            v = values.get(enum(nv.get("ValueSource"), "Output"))
            if v is not None and r.get("Min", 0.0) - 1e-6 <= v <= r.get("Max", 0.0) + 1e-6:
                return nv, values
        return None, values

    def name_at(self, x):
        """The game's name for the place on the handle at x, unformatted ("{BrakePos} Brake"), or None."""
        nv, _ = self._named_at(x)
        return str(nv.get("DisplayName") or "") if nv else None

    def places(self):
        """{name: [inputs]}: where on the handle each named place really is, among the places the handle
        stays (see resting), in the order the game lists them."""
        found = {}
        for x in self.resting():
            name = self.name_at(x)
            if name:
                found.setdefault(name, []).append(x)
        order = [str(nv.get("DisplayName") or "")
                 for nv in (self.props.get("DisplayInfo") or {}).get("NamedValues") or []]
        return {n: found[n] for n in dict.fromkeys(order) if n in found}

    def zone(self, x):
        """(label, is emergency) for the handle at x, from the game's named places, or (None, False)."""
        nv, values = self._named_at(x)
        if nv is None:
            return None, False
        label = str(nv.get("DisplayName") or "")
        return self._format(label, values) if nv.get("bDisplayNameIsFormatString") else label, \
            is_emergency(label)

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
