"""
How far along the track the next stop (or go-via) of the service you're driving is.

The game's API has no stop positions, but its timetable files do. For every service a "data track" gives the
place of each timetable instruction and go-via point as a distance along the service's path, and names the
signal that protects each stretch of that path. The API names the next signal and how far away it is, which
places the train:

    train = (where that signal's first stretch starts) - (distance to the signal)

Tried on Medway Valley 2T09: it ran on smoothly from one signal to the next and read 7.8 m to go when the game
took the stop. About 4 in 10 route timetables name no signals. There the place is only known from the last
stop on, by adding up the speed over the game's own clock (it runs slower than the wall clock).

It also says what to do while you're stopped at a stop, from the step of the timetable the game is on and the
stop's scheduled departure: the game keeps a stop's step going until its departure time and the doors are shut,
then goes on to the next (seen on Airedale-Wharfedale 2P27 at Frizinghall: doors shut 8 s before 08:36:30, the
step done at 08:36:29). Before a service starts, the game can have a wait step of its own; that counts as being
at the first stop.

    tracker = StopTracker(); tracker.start()
    tracker.state      # {"text": message, "label": "Stop at ...", "metres": to go, "estimated": ...,
                       #  "then": the stop after a go-via, "then_metres": to go}
    tracker.stop       # while stopped at a stop, else None: {"phase": "wait" / "close" (the doors) / "depart" /
                       #  "signal" (wait for it) / "end" (of the service), "station": name, "now", "departs" (game
                       #  seconds after midnight; departs None if not timed), "left": seconds to departure,
                       #  "doors": open, "points": tsw_score.Points.read(), "service": its name, "schedule": this
                       #  stop and the rest (ServicePath.stops), "route": the route's name, "train": its class};
                       #  for a while after moving off, phase "departed" and "moved": when the train moved off
"""

import functools
import re
import threading
import time

import tsw_joystick as core
import tsw_paks
import tsw_score

POLL_SECONDS = 0.5             # how often the train is placed on its path
SHOWN_POLL_SECONDS = 0.2       # ... while the readout is on screen
SERVICE_CHECK_SECONDS = 3.0    # how often to check whether you started another service
PASSED_METRES = 20.0           # without objectives, a stop counts as passed this far beyond it (a go-via at once)
NO_GUID = "0" * 32
STOPPED = 0.3                  # m/s: slower than this the train is stopped
DEPARTED = 1.0                 # m/s: faster than this, it has left the stop
KEEP_SECONDS = 30.0            # what to do at a stop stays up this long after moving off (as "departed")
CLOSE_DOORS_SECONDS = 30.0     # the doors are to be shut this long before the departure time
DOOR_SECONDS = 1.0             # how often the doors are looked at while at a stop
MAX_CARS = 16
DANGER = {"Stop", "Danger"}    # signal aspects to wait at
DANGER_METRES = 400.0          # ... when the signal is this close
PLATFORM = re.compile(r"^(.*?)[\s,-]+((?:platform|plat|pl|gleis|track|bahnsteig|voie|binario)\.?\s*\S+)$", re.I)


def api_guid(raw):
    """A GUID read from the files (16 bytes as hex) in the form the API prints it: the files keep each of its
    four 32-bit parts little-endian, the API prints them as numbers."""
    b = bytes.fromhex(raw)
    return "".join(b[i:i + 4][::-1].hex() for i in range(0, 16, 4)).upper()


def split_platform(destination):
    """'Cuxton Platform 1' -> ('Cuxton', 'Platform 1')."""
    m = PLATFORM.match(destination.strip())
    return (m.group(1), m.group(2)) if m and m.group(1) else (destination.strip(), "")


def route_name(timetable_id):
    """'Medway Valley' from the game's timetable ID ('/MedwayValley/Map/...:PersistentLevel...')."""
    first = (timetable_id or "").strip("/").split("/", 1)[0].split(":", 1)[0]
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", first.replace("_", " "))


def map_point(name):
    """A timetable map point's name spelt out: 'BarkingRiverside' -> 'Barking Riverside'."""
    name = name.replace("_", " ")
    return name if " " in name else re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", name)


class Target:
    """A place the service stops at or goes via, from the timetable."""

    def __init__(self, kind, instruction, via, metres, destination):
        self.kind = kind                  # "stop", or "via": a go-via point or a destination passed without stopping
        self.instruction = instruction    # index of its timetable instruction
        self.via = via                    # which go-via of that instruction it is; None: the instruction itself
        self.metres = metres              # from the start of the service's path
        self.named = bool(destination) and destination != "None"
        self.name, self.detail = split_platform(destination) if self.named else ("", "")

    @property
    def key(self):
        return self.instruction, self.via

    def label(self):
        words = "Stop at" if self.kind == "stop" else "Go via"
        if not self.named:
            return words + " marker"
        return f"{words} {self.name}" + (f" · {self.detail}" if self.detail else "")

    def __repr__(self):
        return f"Target({self.key}, {self.metres:.0f} m, {self.label()})"


class ServicePath:
    """A service's stops, go-vias and signals along its path, from the timetable files. Distances in metres from
    the start of the path."""

    def __init__(self, name, steps, targets, signals, instructions=(), schedule=None):
        self.name = name
        self.steps = steps                # [(instruction, go-via or None)]: each instruction after its go-vias
        self.targets = targets            # [Target] in that order
        self.signals = signals            # {API GUID: [metres where a stretch it protects starts]}
        self.instructions = instructions  # [(type, stopping, destination, go-vias)] from the timetable
        self.times = (schedule or {}).get("times") or [(None, None)] * len(instructions)
        self.origin = (schedule or {}).get("origin") or ""
        self.end = (schedule or {}).get("end") or ""
        self.order = {key: k for k, key in enumerate(steps)}
        self.first = {}                   # instruction -> order of its first step
        for k, (i, _) in enumerate(steps):
            self.first.setdefault(i, k)

    def station(self, instruction):
        """The name of the stop a LoadUnload instruction is at: the place the service went to before it, or
        where it starts."""
        return self.stop_place(instruction)[0]

    def stop_place(self, instruction):
        """(station, platform) of the stop a LoadUnload instruction is at: where the GoTo before it went, or
        where the service starts. A GoTo with no destination is a stop at a marker (Airedale 2H33 waits at one
        outside Leeds), or at the end of the service, where it ends (Southeastern 1F05: St Pancras)."""
        for kind, _, dest, _ in reversed(self.instructions[:instruction]):
            if kind == "GoTo":
                if dest and dest != "None":
                    return split_platform(dest)
                last = not any(k == "GoTo" for k, *_ in self.instructions[instruction:])
                return split_platform(map_point(self.end)) if last and self.end else ("Stop marker", "")
        return split_platform(map_point(self.origin))

    def stops(self, start=0):
        """The stops from the one the LoadUnload instruction `start` is at on: [(station, platform, arrival,
        departure)], the times in seconds after midnight, None where the timetable has none (GOBLIN gives only
        arrivals). Steps with no GoTo between them are one stop (Airedale 2P27 has two at Shipley, where it
        reverses, the second without times): its arrival is from the first, its departure from the last."""
        while start > 0 and self.instructions[start - 1][0] != "GoTo":
            start -= 1
        out, at_stop = [], False
        for i in range(start, len(self.instructions)):
            kind, stopping = self.instructions[i][:2]
            if kind == "GoTo":
                at_stop = False
            elif kind == "LoadUnload" and stopping:
                arrives, departs = self.times[i]
                if at_stop:
                    station, platform, first, last = out[-1]
                    out[-1] = (station, platform, first if first is not None else arrives,
                               departs if departs is not None else last)
                else:
                    out.append((*self.stop_place(i), arrives, departs))
                    at_stop = True
        return out

    def place(self, signal, to_signal, near=None):
        """Where the train is, from its next signal and the distance to it (metres), or None if that signal
        isn't on the path. A signal passed twice (after reversing) is taken where it fits `near` best."""
        starts = self.signals.get(signal)
        if not starts:
            return None
        start = starts[0] if near is None else min(starts, key=lambda s: abs(s - to_signal - near))
        return start - to_signal

    def next_target(self, order=None, position=None, wanted=lambda t: True):
        """The next place to stop at or go via: the first one at or after the step the game is on (leaving out
        go-vias already passed), or without that, the first one not yet passed."""
        for t in self.targets:
            if not wanted(t):
                continue
            if order is not None:
                if self.order[t.key] < order or (t.kind == "via" and position is not None and t.metres < position):
                    continue
                return t
            if position is not None and t.metres > position - (PASSED_METRES if t.kind == "stop" else 0.0):
                return t
        return None


# ---------------------------------------------------------------- reading the timetable files
def _load(files, path, keep=None):
    """Settings of the asset a timetable or data track file holds: the export named like the file (not always
    the first one: Goblin, Bakerloo, Mittenwald... put a service data class first). `keep`: only these
    (tsw_paks.Package.properties)."""
    pak = files.where[path + ".uasset"]
    package = tsw_paks.Package("/" + path, pak.read(path + ".uasset"), pak.read(path + ".uexp"))
    return package.properties(package.export(path.rsplit("/", 1)[-1]) or 1, keep)


# what's read of a timetable's services: the rest is most of a big timetable (80 MB on Frankfurt-Fulda)
SERVICE_FIELDS = {"Name": None, "ServiceNumber": None, "bIsPlayerDrivable": None, "MapPointA": None,
                  "MapPointB": None, "Instructions": {"InstructionType": None, "bIsStopping": None,
                                                      "Destination": {"Name": None}, "GoVias": {"Name": None},
                                                      "ArrivalTime": None, "CompletionTime": None,
                                                      "bHasScheduledArrivalTime": None,
                                                      "bHasScheduledCompletionTime": None}}
DAY = 86400.0


def _time_of_day(ticks, scheduled):
    """Seconds after midnight of a timetable time (FTimespan ticks), or None if it isn't a scheduled one."""
    return (ticks / 1e7) % DAY if scheduled and ticks is not None else None


@functools.lru_cache(maxsize=4)        # reading a route's timetable takes seconds
def timetable_services(files, path):
    """{service name: (service number, player drivable, instructions, schedule)} for each service in a timetable
    file, its instructions as (type "GoTo" / "LoadUnload" / ..., stopping, destination, (go-via names)), and its
    schedule as {"origin": where it starts, "end": where it ends (map points), "times": [(arrival, departure) per
    instruction, in seconds after midnight, None where not scheduled]}: a stop's times are on its LoadUnload."""
    out = {}
    for s in _load(files, path, {"Services": SERVICE_FIELDS}).get("Services") or []:
        instructions, times = [], []
        for ins in s.get("Instructions") or []:
            kind = str(ins.get("InstructionType", "")).split("::")[-1]
            dest = (ins.get("Destination") or {}).get("Name") or ""
            vias = tuple(v.get("Name") or "" for v in ins.get("GoVias") or [])
            instructions.append((kind, bool(ins.get("bIsStopping")), dest, vias))
            times.append((_time_of_day(ins.get("ArrivalTime"), ins.get("bHasScheduledArrivalTime")),
                          _time_of_day(ins.get("CompletionTime"), ins.get("bHasScheduledCompletionTime"))))
        schedule = {"origin": str(s.get("MapPointA") or ""), "end": str(s.get("MapPointB") or ""),
                    "times": tuple(times)}
        out[s.get("Name")] = (s.get("ServiceNumber"), bool(s.get("bIsPlayerDrivable")), tuple(instructions), schedule)
    return out


def timetable_files(files, timetable_id):
    """Paths (without extension) of the timetable the game is running and of its data tracks. The game names
    the timetable like '/MedwayValley/Map/...:PersistentLevel.SandboxRouteTimetable_2147475787.MVL_Timetable'."""
    name = (timetable_id or "").rsplit(".", 1)[-1]
    if not name:
        return []
    out = []
    for f in files.where:
        if f.endswith("/" + name + ".uasset"):
            base = f[:-len(".uasset")]
            folder = base[:-len(name)] + "DataTracks/" + name + "_"
            tracks = [g[:-len(".uasset")] for g in files.where
                      if g.startswith(folder) and g.endswith("DataTrack.uasset")]
            out.append((base, tracks))
    return out


def named_in(header, service):
    """Whether a file's header may name a service: it keeps a name like '245_18' as '245' and a number."""
    return re.sub(r"_(0|[1-9][0-9]*)$", "", service).encode("latin-1", "replace") in header


def service_path(files, timetable_id, service):
    """ServicePaths for the service the game names (several when only its number matches: 2T02 -> 2T02-1 and
    2T02-2), or [] when its timetable files can't be found or read."""
    paths = []
    for timetable, tracks in timetable_files(files, timetable_id):
        services = timetable_services(files, timetable)
        names = ([service] if service in services else
                 [n for n, v in services.items() if v[0] == service and v[1]] or
                 [n for n, v in services.items() if v[0] == service])
        for track in tracks:
            pak = files.where[track + ".uasset"]
            if not any(named_in(pak.read(track + ".uasset"), n) for n in names):
                continue
            data = dict(_load(files, track).get("ServiceDataTracks") or [])
            for name in names:
                if name in data:
                    paths.append(_build(name, services[name][2], data[name].get("TrackData") or [],
                                        services[name][3]))
        if paths:
            break
    return paths


def _build(name, instructions, rows, schedule=None):
    places, signals, last = {}, {}, None
    for row in rows:
        kind = str(row.get("DataType", ""))
        metres = row.get("Distance", 0.0) / 100
        if kind.endswith("ActionPoint"):
            places.setdefault((row.get("InstructionIndex"), None), metres)
        elif kind.endswith("GoVia"):
            places.setdefault((row.get("InstructionIndex"), row.get("GoViaIndex")), metres)
        signal = (row.get("SignalRef") or {}).get("PropertyReference", NO_GUID)
        if signal != NO_GUID:
            if signal != last:            # a new run of stretches behind this signal: it stands here
                signals.setdefault(api_guid(signal), []).append(metres)
            last = signal
    steps, targets = [], []
    for i, (kind, stopping, dest, vias) in enumerate(instructions):
        for j, via in enumerate(vias):
            steps.append((i, j))
            if (i, j) in places:
                targets.append(Target("via", i, j, places[(i, j)], via))
        steps.append((i, None))
        if kind == "GoTo" and (i, None) in places:
            targets.append(Target("stop" if stopping else "via", i, None, places[(i, None)], dest))
    return ServicePath(name, steps, targets, signals, instructions, schedule or {})


# ---------------------------------------------------------------- following the train
class StopTracker(threading.Thread):
    """Keeps track of the next stop of the service you're driving, on a thread of its own. `shown` (set from
    the window) makes it update faster while the readout is on screen."""

    def __init__(self, log=None):
        super().__init__(daemon=True)
        self.api = core.TSWApi()
        self.log = log or (lambda msg: None)
        self.running = True
        self.shown = False
        self.points = tsw_score.Points()
        self._say("Starting...")
        self._reset(None)
        self.last_service_check = 0.0

    def _reset(self, service):
        self.service = service        # name the game gives the service you're driving
        self.route = ""               # route it's on
        self.train = None             # train (class) driving it
        self.paths = []               # ServicePath candidates for it
        self.path = None              # the one you're driving
        self.offset = None            # objective number of the service's first step
        self.steps = None             # the steps the game makes objectives for, None: not matched
        self.vias_counted = False     # the game makes go-vias objectives of their own
        self.position = None          # metres along the path
        self.fixed = False            # position from a signal (else counted on from the last stop)
        self.clock = None             # game seconds at the last poll
        self.step_key = None          # (instruction, go-via) the game is on
        self.dwell = None             # the LoadUnload instruction of the stop you're stopped at
        self.stop = None              # what to do there (see the module's notes)
        self.departed = None          # (self.stop as you moved off, monotonic time to stop showing it)
        self.door_nodes = None        # API paths of the passenger doors
        self.doors_open = False
        self.doors_read = 0.0

    def _say(self, text, target=None, metres=None, estimated=False, then=None, then_metres=None):
        self.state = {"text": text, "label": target.label() if target else "", "metres": metres,
                      "estimated": estimated, "then": then.label() if then else "", "then_metres": then_metres}

    def _wanted(self, target):
        """Go-vias without a name are only shown when the game makes objectives of them (they may only be there
        to route the service)."""
        return target.kind == "stop" or target.via is None or target.named or self.vias_counted

    def run(self):
        while self.running:
            try:
                self.step()
            except Exception as e:
                self._say(f"Not available: {e}")     # the game drops a connection now and then: carry on
                self.stop = None
                time.sleep(1)
            time.sleep(SHOWN_POLL_SECONDS if self.shown or self.departed else POLL_SECONDS)

    def step(self):
        api = self.api
        if not api.key and not api.load_key():
            self._say("No API key - start TSW7 with -HTTPAPI")
            return
        now = time.monotonic()
        if self.path is None or now - self.last_service_check > SERVICE_CHECK_SECONDS:
            self.last_service_check = now
            try:
                info = api.get("DriverAid.PlayerInfo").get("Values") or {}
            except Exception:
                self._say("Game not reachable")
                self._reset(None)
                return
            service = info.get("currentServiceName")
            if not service or service == "None":
                self._say("No timetabled service")
                self._reset(None)
                return
            if service != self.service:
                self._reset(service)
                self._load(service)
        if not self.paths:
            return
        self._follow()

    def _load(self, service):
        future = core.load_game_files()
        if future is None:
            self._say("Reading the game files is turned off")
            return
        self._say(f"Reading the timetable for {service}...")
        timetable = self.api.get_value("Timetable.VehicleID")
        self.route = route_name(timetable)
        self.train = self.api.get_value("CurrentDrivableActor.ObjectClass")
        files, _ = future.result(timeout=120)
        self.paths = service_path(files, timetable, service)
        if not self.paths:
            self._say(f"No timetable data for {service}")
            self.log(f"Next stop: no timetable data for {service}")
            return
        self.path = self.paths[0]
        self._find_objectives()

    def _find_objectives(self):
        """Match the game's objectives to the timetable, so Objectives.Current tells the step the game is on. The
        game makes one objective per timetable instruction after any of its own (on Medway Valley a 'wait'
        comes first). Go-vias may have objectives of their own: those objectives say bIsGoVia."""
        self.offset, self.steps, self.vias_counted = None, None, False
        path = self.path
        plain = [k for k in path.steps if k[1] is None]
        passing = {t.key for t in path.targets if t.kind == "via" and t.via is None}   # may count as go-vias
        try:
            count = len(core.node_names(self.api.list("Objectives")))
            for steps in ([path.steps, plain] if len(plain) < len(path.steps) else [plain]):
                offset = count - len(steps)
                if offset < 0 or "ServiceBP" not in str(self.api.get_value(f"Objectives/{offset}.ObjectClass")):
                    continue
                flags = [bool(self.api.get_value(f"Objectives/{offset + k}.Property.bIsGoVia"))
                         for k in range(len(steps))]
                if all(flag == (key[1] is not None) or key in passing for flag, key in zip(flags, steps)):
                    self.offset, self.steps, self.vias_counted = offset, steps, steps is path.steps
                    break
        except Exception:
            pass
        stops = sum(t.kind == "stop" for t in path.targets)
        vias = sum(t.kind == "via" and self._wanted(t) for t in path.targets)
        how = ("objectives not matched, going by position" if self.steps is None
               else "go-vias have objectives" if self.vias_counted
               else "objectives matched" if len(plain) == len(path.steps)
               else "go-vias have no objectives")
        self.log(f"Next stop: {path.name} read, {stops} stop{'s' * (stops != 1)}, {vias} go-via{'s' * (vias != 1)}"
                 f" ({how})"
                 + ("" if path.signals else "; counted from the last stop: no signal data"))

    def _follow(self):
        api = self.api
        aid = api.get("DriverAid.Data").get("Values") or {}
        signal = (aid.get("nextSignalProperty") or {}).get("propertyReference")
        to_signal = (aid.get("distanceToSignal") or 0.0) / 100
        clock = (api.get("TimeOfDay.Data").get("Values") or {}).get("LocalTime")
        clock = clock / 1e7 if clock else None
        speed = abs(api.get_value("CurrentDrivableActor.Function.HUD_GetSpeed") or 0.0)

        if len(self.paths) > 1 and signal not in self.path.signals:
            other = next((p for p in self.paths if signal in p.signals), None)
            if other is not None:         # you're on the other part of the service
                self.path, self.position, self.step_key = other, None, None
                self._find_objectives()
        path = self.path

        place = path.place(signal, to_signal, self.position) if aid.get("signalSeen") else None
        if place is not None:
            self.position, self.fixed = place, True
        elif self.position is not None and clock is not None and self.clock is not None:
            self.position += speed * max(0.0, clock - self.clock)
            self.fixed = False
        self.clock = clock

        order = key = None
        waiting = False                              # on the game's own wait before the service starts
        if self.steps is not None:
            current = api.get_value("Objectives.Current")
            if isinstance(current, (int, float)) and current >= self.offset:
                k = int(current) - self.offset
                if k >= len(self.steps):
                    order = len(path.steps)          # all done
                else:
                    key = self.steps[k]
                    order = path.order[key] if self.vias_counted else path.first[key[0]]
            elif isinstance(current, (int, float)) and current >= 0:
                waiting = True
        self._at_stop(key, order, speed, clock, aid, waiting)
        if key is not None and self.step_key is not None and path.order[key] > path.order[self.step_key]:
            done = next((t for t in path.targets if t.key == self.step_key), None)
            if done is not None and not self.fixed:
                self.position = done.metres      # no signal to go by: you're where that objective was met
        self.step_key = key

        target = path.next_target(order, self.position, self._wanted)
        if target is None:
            self._say("No more stops" if order is not None or self.position is not None
                      else "Place on the line not known yet")
            return
        then = None
        if target.kind == "via":                # say which stop comes after it
            then = path.next_target(path.order[target.key] + 1, None, lambda t: t.kind == "stop")
        if self.position is None:
            self._say("Distance known after the next stop", target, then=then)
        else:
            self._say("", target, target.metres - self.position, estimated=not self.fixed, then=then,
                      then_metres=then.metres - self.position if then else None)

    def _at_stop(self, key, order, speed, clock, aid, waiting=False):
        """Sets self.stop while you're stopped at a stop: from when the game is on its LoadUnload step until the
        train moves off, and as "departed" for KEEP_SECONDS after. `waiting`: the game is on a wait of its own
        before the service's first step (on Airedale 2P27, a wait for a time of day at Bradford Forster Square):
        you're at the first stop."""
        path = self.path
        if key is not None:
            instruction = key[0]
        elif order == len(path.steps):
            instruction = len(path.instructions)         # all done
        else:
            instruction = 0 if waiting else None
        if instruction is None or clock is None:
            self.dwell = self.stop = self.departed = None
            return
        now = clock % DAY
        if instruction < len(path.instructions) and path.instructions[instruction][0] == "LoadUnload" \
                and path.instructions[instruction][1] and speed < STOPPED:
            self.dwell = instruction
        if self.dwell is not None and (speed > DEPARTED or instruction < self.dwell):
            if speed > DEPARTED and self.stop is not None and self.stop["phase"] != "end":
                self.departed = (dict(self.stop, phase="departed", left=None, moved=now),
                                 time.monotonic() + KEEP_SECONDS)
            self.dwell = None
        if self.dwell is None:
            if self.departed is not None and time.monotonic() > self.departed[1]:
                self.departed = None
            self.stop = None if self.departed is None else dict(self.departed[0], now=now)
            return
        self.departed = None
        _, departs = path.times[self.dwell]
        left = None if departs is None else (departs - now + DAY / 2) % DAY - DAY / 2
        doors = self._doors_open()
        if not any(kind == "GoTo" for kind, *_ in path.instructions[self.dwell + 1:]):
            phase = "end"
        elif instruction > self.dwell:                      # the game has let the train go
            danger = aid.get("signalAspectClass") in DANGER and \
                (aid.get("distanceToSignal") or 0.0) / 100 < DANGER_METRES
            phase = "close" if doors else "signal" if danger else "depart"
        elif doors and left is not None and left <= CLOSE_DOORS_SECONDS:
            phase = "close"
        else:
            phase = "wait"
        self.stop = {"phase": phase, "station": path.station(self.dwell), "now": now, "departs": departs,
                     "left": left, "doors": doors, "points": self.points.read({self.service, path.name}, clock),
                     "service": self.service, "schedule": path.stops(self.dwell), "route": self.route,
                     "train": self.train}

    def _doors_open(self):
        """Whether any passenger door of the train is open (looked at once a second)."""
        if time.monotonic() - self.doors_read < DOOR_SECONDS:
            return self.doors_open
        self.doors_read = time.monotonic()
        api = self.api
        if self.door_nodes is None:
            self.door_nodes = []
            for i in range(MAX_CARS):
                names = core.node_names(api.list(f"CurrentFormation/{i}"))
                if not names:
                    break
                self.door_nodes += [f"CurrentFormation/{i}/{n}" for n in names if n.startswith("PassengerDoor_")]
        self.doors_open = any((api.get_value(n + ".Function.GetCurrentOutputValue") or 0.0) > 0.05
                              for n in self.door_nodes)
        return self.doors_open
