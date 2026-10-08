"""
How far along the track the next stop of the service you're driving is.

The game's API has no stop positions, but its timetable files do. For every service a "data track" gives the
place of each timetable instruction as a distance along the service's path, and names the signal that protects
each stretch of that path. The API names the next signal and how far away it is, which places the train:

    train = (where that signal's first stretch starts) - (distance to the signal)

Tried on Medway Valley 2T09: it ran on smoothly from one signal to the next and read 7.8 m to go when the game
took the stop. About 4 in 10 route timetables name no signals. There the place is only known from the last
stop on, by adding up the speed over the game's own clock (it runs slower than the wall clock).

    tracker = StopTracker(); tracker.start()
    tracker.state      # {"text": ..., "name": ..., "detail": ..., "metres": ..., "estimated": ...}
"""

import functools
import re
import threading
import time

import tsw_joystick as core
import tsw_paks

POLL_SECONDS = 0.5             # how often the train is placed on its path
SHOWN_POLL_SECONDS = 0.2       # ... while the readout is on screen
SERVICE_CHECK_SECONDS = 3.0    # how often to check whether you started another service
PASSED_METRES = 20.0           # without objectives, a stop counts as passed this far beyond it
NO_GUID = "0" * 32
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


def format_distance(metres):
    if metres is None:
        return "-"
    if metres < -1:
        return f"{-metres:.0f} m past"
    if metres < 10:
        return f"{max(metres, 0.0):.1f} m"
    if metres < 1000:
        return f"{metres:.0f} m"
    return f"{metres / 1000:.2f} km"


class Stop:
    def __init__(self, instruction, metres, destination):
        self.instruction = instruction    # index of its timetable instruction
        self.metres = metres              # from the start of the service's path
        self.name, self.detail = split_platform(destination)

    def __repr__(self):
        return f"Stop({self.instruction}, {self.metres:.0f} m, {self.name} {self.detail})"


class ServicePath:
    """A service's stops and signals along its path, from the timetable files. Distances in metres from the
    start of the path."""

    def __init__(self, name, instructions, stops, signals):
        self.name = name
        self.instructions = instructions  # how many timetable instructions the service has
        self.stops = stops                # [Stop] in path order
        self.signals = signals            # {API GUID: [metres where a stretch it protects starts]}

    def place(self, signal, to_signal, near=None):
        """Where the train is, from its next signal and the distance to it (metres), or None if that signal
        isn't on the path. A signal passed twice (after reversing) is taken where it fits `near` best."""
        starts = self.signals.get(signal)
        if not starts:
            return None
        start = starts[0] if near is None else min(starts, key=lambda s: abs(s - to_signal - near))
        return start - to_signal

    def next_stop(self, instruction=None, position=None):
        """The next stop: the first one at or after the instruction the game is on, or failing that the first
        one not yet passed."""
        if instruction is not None:
            return next((s for s in self.stops if s.instruction >= instruction), None)
        if position is not None:
            return next((s for s in self.stops if s.metres > position - PASSED_METRES), None)
        return None


# ---------------------------------------------------------------- reading the timetable files
def _load(files, path):
    pak = files.where[path + ".uasset"]
    package = tsw_paks.Package("/" + path, pak.read(path + ".uasset"), pak.read(path + ".uexp"))
    return package.properties(1)


@functools.lru_cache(maxsize=4)        # reading a route's timetable takes several seconds
def timetable_services(files, path):
    """{service name: (service number, player drivable, instruction count, [(instruction, destination)])} for
    the stopping instructions of each service in a timetable file."""
    out = {}
    for s in _load(files, path).get("Services") or []:
        instructions = s.get("Instructions") or []
        stops = []
        for i, ins in enumerate(instructions):
            dest = (ins.get("Destination") or {}).get("Name")
            if ins.get("bIsStopping") and dest and dest != "None":
                stops.append((i, dest))
        out[s.get("Name")] = (s.get("ServiceNumber"), bool(s.get("bIsPlayerDrivable")), len(instructions), stops)
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
            if not any(n.encode("latin-1", "replace") in pak.read(track + ".uasset") for n in names):
                continue
            data = dict(_load(files, track).get("ServiceDataTracks") or [])
            for name in names:
                if name in data:
                    _, _, count, stops = services[name]
                    paths.append(_build(name, count, stops, data[name].get("TrackData") or []))
        if paths:
            break
    return paths


def _build(name, instructions, stops, rows):
    actions, signals, last = {}, {}, None
    for row in rows:
        kind = str(row.get("DataType", ""))
        metres = row.get("Distance", 0.0) / 100
        if kind.endswith("ActionPoint"):
            actions.setdefault(row.get("InstructionIndex"), metres)
        signal = (row.get("SignalRef") or {}).get("PropertyReference", NO_GUID)
        if signal != NO_GUID:
            if signal != last:            # a new run of stretches behind this signal: it stands here
                signals.setdefault(api_guid(signal), []).append(metres)
            last = signal
    return ServicePath(name, instructions, [Stop(i, actions[i], d) for i, d in stops if i in actions], signals)


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
        self.state = {"text": "Starting...", "name": "", "detail": "", "metres": None, "estimated": False}
        self._reset(None)
        self.last_service_check = 0.0

    def _reset(self, service):
        self.service = service        # name the game gives the service you're driving
        self.paths = []               # ServicePath candidates for it
        self.path = None              # the one you're driving
        self.offset = None            # objective number of the service's first instruction
        self.position = None          # metres along the path
        self.fixed = False            # position from a signal (else counted on from the last stop)
        self.clock = None             # game seconds at the last poll
        self.instruction = None

    def _say(self, text, stop=None, metres=None, estimated=False):
        self.state = {"text": text, "name": stop.name if stop else "", "detail": stop.detail if stop else "",
                      "metres": metres, "estimated": estimated}

    def run(self):
        while self.running:
            try:
                self.step()
            except Exception as e:
                self._say(f"Not available: {e}")     # the game drops a connection now and then: carry on
                time.sleep(1)
            time.sleep(SHOWN_POLL_SECONDS if self.shown else POLL_SECONDS)

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
        files, _ = future.result(timeout=120)
        self.paths = service_path(files, self.api.get_value("Timetable.VehicleID"), service)
        if not self.paths:
            self._say(f"No timetable data for {service}")
            self.log(f"Next stop: no timetable data for {service}")
            return
        self.path = self.paths[0]
        self._find_objectives()
        self.log(f"Next stop: {service} read, {len(self.path.stops)} stops"
                 + ("" if any(p.signals for p in self.paths) else " (counted from the last stop: no signal data)"))

    def _find_objectives(self):
        """The game makes one objective per timetable instruction, after any of its own (on Medway Valley a
        'wait' comes first), so Objectives.Current gives the instruction it's on."""
        self.offset = None
        try:
            count = len(core.node_names(self.api.list("Objectives")))
            offset = count - self.path.instructions
            if offset >= 0 and "ServiceBP" in str(self.api.get_value(f"Objectives/{offset}.ObjectClass")):
                self.offset = offset
        except Exception:
            pass

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
                self.path, self.position, self.instruction = other, None, None
                self._find_objectives()
        path = self.path

        place = path.place(signal, to_signal, self.position) if aid.get("signalSeen") else None
        if place is not None:
            self.position, self.fixed = place, True
        elif self.position is not None and clock is not None and self.clock is not None:
            self.position += speed * max(0.0, clock - self.clock)
            self.fixed = False
        self.clock = clock

        instruction = None
        if self.offset is not None:
            current = api.get_value("Objectives.Current")
            if isinstance(current, (int, float)) and current >= self.offset:
                instruction = int(current) - self.offset
        if instruction is not None and self.instruction is not None and instruction > self.instruction:
            done = next((s for s in path.stops if s.instruction == self.instruction), None)
            if done is not None and not self.fixed:
                self.position = done.metres      # no signal to go by: you're at the stop just made
        self.instruction = instruction

        stop = path.next_stop(instruction, self.position)
        if stop is None:
            self._say("No more stops" if instruction is not None or self.position is not None
                      else "Place on the line not known yet")
        elif self.position is None:
            self._say("Distance known after the next stop", stop)
        else:
            metres = stop.metres - self.position
            self._say(format_distance(metres), stop, metres, estimated=not self.fixed)
