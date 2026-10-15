"""
The route map (tsw_routemap.py) over the game, with where you are on it, live: a small see-through window that
follows the train, shown and hidden from the bridge window (tsw_joystick_ui.py) with a joystick button or a key.

Where you are: the game gives the train's latitude and longitude (DriverAid.PlayerInfo.geoLocation), and lays its
world out in UTM (WGS84, scale 0.9996, the zone of the route's central point) measured from the route's central
latitude and longitude (TS2WorldSettings in the route's level), x east and y south, in 1 km tiles centred on whole
kilometres (PlayerInfo.currentTile). Checked on Airedale-Wharfedale 2H26, Leeds to Skipton: of 5469 readings over
13 km every tile came out right, some 0.3 m from a tile's edge, and the median was 1.6 m from the track; offline,
real stations' places fall 1-50 m from their platforms on routes in Britain, Germany, the Netherlands and the US.
Many British routes share one central point (51.523373, -0.681496), so their worlds are one. The route maps'
track is in the same metres, so the map places you as it places the track.

The map is the one of the service you're driving: its line both ways, drawn like the first page of a whole-route
map but larger (a few metres a pixel, ZOOMS), so it needs no insets, with names, boards to slow down and landmarks.
With no service (or none in the files), the whole route from the timetable the game is running. The drawing is made
by this script run on its own, so a crash can't take the bridge with it, and kept in live_maps next to this script;
Edge (or Chrome), with no window, turns it into pieces CHUNK pixels square as the window needs them, the ones near
the train first, then the rest along the line. A service's map takes about 15 s; the scenery (tunnels, rivers...)
half a minute more on a route the first time, so the map comes without it first, and the scenery is kept.

    python tsw_livemap.py make --timetable ID [--service NAME] [--zoom METRES-A-PIXEL] [--unit-px PIXELS]
"""

import json
import math
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import traceback

import tsw_joystick as core
import tsw_paks
import tsw_routemap as rm
import tsw_stops

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "live_maps")
MAP_VERSION = 2                 # a map made by an older version is made again
SCENERY_VERSION = 1
ZOOMS = (3, 5, 8, 12, 20, 30)   # metres a pixel the map can be drawn at
ZOOM = 8
UNIT_PX = 1.3                   # pixels a map unit (tsw_routemap.MapView): its names are 11 units, 14 px
PAD = 110.0                     # map units round the line: room for names and boards
CHUNK = 768                     # pixels: the map is turned into pieces this size
NEAR_LINE = 500                 # pixels: pieces this close to the line are drawn before they're needed
BROWSERS = (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")
NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW
LOW_PRIORITY = 0x00004000       # BELOW_NORMAL_PRIORITY_CLASS: the game comes first
LINE_EVERY = 10                 # section points (10 m apart) kept: the line, to tell whether you're on it
OFF_LINE = 1500.0               # metres: further than this from the map's line, the map isn't where you are
PAGE_CSS = "body { margin: 0; background: #fff; overflow: hidden; }"

# WGS84 and UTM's scale on its meridian
A, F = 6378137.0, 1 / 298.257223563
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
K0 = 0.9996


# ---------------------------------------------------------------- where you are
def utm(lat, lon, lon0):
    """(east, north) in metres of a point by UTM's transverse Mercator (WGS84, scale 0.9996) about the meridian
    `lon0`, from where it meets the equator (Snyder's series: well under a metre off within 5° of the meridian)."""
    p, dl = math.radians(lat), math.radians(lon - lon0)
    s, c = math.sin(p), math.cos(p)
    n = A / math.sqrt(1 - E2 * s * s)
    t, cc, a = (s / c) ** 2, EP2 * c * c, dl * c
    m = A * ((1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256) * p
             - (3 * E2 / 8 + 3 * E2 ** 2 / 32 + 45 * E2 ** 3 / 1024) * math.sin(2 * p)
             + (15 * E2 ** 2 / 256 + 45 * E2 ** 3 / 1024) * math.sin(4 * p)
             - 35 * E2 ** 3 / 3072 * math.sin(6 * p))
    east = K0 * n * (a + (1 - t + cc) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * cc - 58 * EP2) * a ** 5 / 120)
    north = K0 * (m + n * s / c * (a * a / 2 + (5 - t + 9 * cc + 4 * cc * cc) * a ** 4 / 24
                                   + (61 - 58 * t + t * t + 600 * cc - 330 * EP2) * a ** 6 / 720))
    return east, north


def zone_meridian(lon):
    """The middle meridian of the UTM zone a longitude is in."""
    return (math.floor((lon + 180) / 6) + 1) * 6 - 183


def world(lat, lon, origin):
    """Where a latitude and longitude are in a route's world, in metres (x east, y south) like its track: `origin`
    is the route's central (latitude, longitude)."""
    lon0 = zone_meridian(origin[1])
    e0, n0 = utm(origin[0], origin[1], lon0)
    e, n = utm(lat, lon, lon0)
    return e - e0, n0 - n


def tile(x, y):
    """The game's tile (PlayerInfo.currentTile) of a place in the world."""
    return round(x / 1000), round(y / 1000)


def geo_origin(files, level):
    """A route's central (latitude, longitude), which its world is measured from: TS2WorldSettings in its level."""
    path = files.file_of(level)
    if not path or path + ".umap" not in files.where:
        return None
    pak = files.where[path + ".umap"]
    pkg = tsw_paks.Package(level, pak.read(path + ".umap"), files.where[path + ".uexp"].read(path + ".uexp"))
    for i in range(1, len(pkg.exports) + 1):
        if pkg.class_name(i) == "TS2WorldSettings":
            p = pkg.properties(i)
            if p.get("CentralLatitude") is not None and p.get("CentralLongitude") is not None:
                return float(p["CentralLatitude"]), float(p["CentralLongitude"])
    return None


def level_of(timetable_id):
    """The route's level from the game's name of the timetable it's running:
    '/AiredaleWharfedaleLines/Map/AiredaleWharfedaleLines.AiredaleWharfedaleLines:PersistentLevel...'."""
    return (timetable_id or "").split(":", 1)[0].split(".", 1)[0]


def to_px(meta, x, y):
    """A place in the world on a map, in its pixels from its top left."""
    return (x - meta["left"]) / meta["zoom"], (y - meta["top"]) / meta["zoom"]


# ---------------------------------------------------------------- the maps kept
def map_key(timetable_id, service, zoom):
    """The name a map is kept under: the route's level, the timetable, the service (or 'whole route'), the zoom."""
    level = level_of(timetable_id).rsplit("/", 1)[-1]
    timetable = (timetable_id or "").rsplit(".", 1)[-1]
    text = f"{level} {timetable} {service or 'whole route'} z{zoom:g}"
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text)).strip()


def meta_file(key):
    return os.path.join(CACHE, key + ".json")


def read_meta(key):
    """What's kept of a map: {"title", "origin", "left", "top" (the world at its top left), "zoom", "size" (pixels),
    "line", "scenery": made with it, "stamp": which drawing, ...}, or None if it isn't kept, or was made by an older
    version."""
    try:
        with open(meta_file(key), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return dict(meta, key=key) if meta.get("version") == MAP_VERSION else None


def chunk_file(meta, cell):
    """The file of a piece of a map: (column, row) of CHUNK pixels."""
    return os.path.join(CACHE, f"{meta['key']} {meta['stamp']} {cell[0]} {cell[1]}.png")


def chunks_kept(meta):
    """The pieces of a map drawn already."""
    pattern = re.compile(rf"^{re.escape(meta['key'])} {meta['stamp']} (\d+) (\d+)\.png$")
    try:
        names = os.listdir(CACHE)
    except OSError:
        return set()
    return {(int(m.group(1)), int(m.group(2))) for m in map(pattern.match, names) if m}


def write(path, text):
    """Writes a file whole, so the window never reads half of one."""
    with open(path + ".new", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".new", path)


def browser():
    """Edge, or failing that Chrome: what turns the map into pictures."""
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for name in BROWSERS:
            path = root and os.path.join(root, name)
            if path and os.path.isfile(path):
                return path
    return None


def draw_chunk(meta, cell):
    """Turns a piece of a kept map into a picture (Edge with no window, about a second)."""
    exe = browser()
    if not exe:
        raise rm.MapError("Edge or Chrome is needed to draw the map, and neither was found")
    path = chunk_file(meta, cell)
    page = pathlib.Path(os.path.join(CACHE, meta["key"] + ".html")).as_uri()
    if os.path.exists(path + ".new.png"):
        os.remove(path + ".new.png")
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                    "--no-default-browser-check", "--disable-extensions",
                    f"--user-data-dir={os.path.join(CACHE, 'browser')}", "--force-device-scale-factor=1",
                    f"--window-size={CHUNK},{CHUNK}", f"--screenshot={path}.new.png",
                    f"{page}#{cell[0] * CHUNK},{cell[1] * CHUNK}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
                   creationflags=NO_WINDOW | LOW_PRIORITY)
    if not os.path.isfile(path + ".new.png"):
        raise rm.MapError("The map couldn't be turned into a picture")
    os.replace(path + ".new.png", path)
    return path


# ---------------------------------------------------------------- making a map
def follow_map(sections, units, places, landmarks, scale):
    """The line drawn at `scale` map units a metre, all of it, with PAD round it: every station named, a board at
    each place to slow down, the limit along long stretches and the landmarks, as on the first page of a whole-route
    map, where it's small enough to need insets. Returns (svg contents, width, height in map units, the world (x, y)
    at its top left)."""
    pts = [(x, y) for s in sections for _, x, y in s.points]
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    w = (max(p[0] for p in pts) - x0) * scale + 2 * PAD
    h = (max(p[1] for p in pts) - y0) * scale + 2 * PAD
    view = rm.MapView(scale, PAD, PAD, x0, y0, (0, 0, w, h), landmarks=landmarks)
    view.lines(sections, units)
    view.names(view.dots(rm.route_stations(sections, places)))
    view.boards(rm.slow_spots(sections, units), units)
    view.line_speeds(sections, units)
    view.landmark_names()
    return "".join(view.o), w, h, (x0 - PAD / scale, y0 - PAD / scale)


def scenery_of(files, level, say):
    """(scenery, sea, water) of a route (tsw_routemap.route_scenery), kept in live_maps/scenery."""
    import pickle
    path = os.path.join(CACHE, "scenery", level.rsplit("/", 1)[-1] + ".pickle")
    try:
        with open(path, "rb") as f:
            kept = pickle.load(f)
        if kept.get("version") == SCENERY_VERSION:
            return kept["objects"], kept["sea"], kept["water"]
    except Exception:
        pass
    say("Finding the tunnels, bridges and water of the route (half a minute, the first time)...")
    objects, sea, water = rm.route_scenery(files, level)
    objects = tuple(tuple(str(v) if isinstance(v, str) else v for v in o) for o in objects)  # names, not Refs
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".new", "wb") as f:
        pickle.dump({"version": SCENERY_VERSION, "objects": objects, "sea": sea, "water": water}, f)
    os.replace(path + ".new", path)
    return objects, sea, water


def has_scenery(level):
    return os.path.isfile(os.path.join(CACHE, "scenery", level.rsplit("/", 1)[-1] + ".pickle"))


def route_of(files, timetable):
    """The name of the route a timetable file is for."""
    pak = files.where[timetable + ".uasset"]
    pkg = tsw_paks.Package("/" + timetable, pak.read(timetable + ".uasset"), b"")
    definition = next((i["name"] for i in pkg.imports
                       if i["class"] == "Package" and rm.ROUTE_DEFINITION.match(i["name"])), None)
    return rm.route_details(files, definition)[0] if definition else None


def make(timetable_id, service, zoom=ZOOM, unit_px=UNIT_PX, say=print, made=print):
    """Makes the map of a service (or with no service, of the whole route) of the timetable the game names, at
    `zoom` metres a pixel, `unit_px` pixels a map unit, unless it's kept already; `made(meta file)` is told of each
    map made: the first time on a route there's one without the scenery first."""
    os.makedirs(CACHE, exist_ok=True)
    key = map_key(timetable_id, service, zoom)
    meta = read_meta(key)
    if meta and meta.get("scenery"):
        made(meta_file(key))
        return
    level = level_of(timetable_id)
    say("Reading the game files...")
    files = tsw_paks.GameFiles()
    found = tsw_stops.timetable_files(files, timetable_id)
    if not found:
        raise rm.MapError("The timetable the game is running isn't in the game files")
    timetable = found[0][0]
    origin = geo_origin(files, level)
    if origin is None:
        raise rm.MapError("The route's files don't say where it is on the earth")
    route = route_of(files, timetable) or level.rsplit("/", 1)[-1]
    table = rm.service_table(files, timetable)
    svc = None
    if service:
        svc = table.get(service) or next((s for s in table.values() if s.number == service and s.drivable), None)
    say("Reading the track...")
    network = rm.TrackNetwork(files, rm.track_tiles(files, level))
    keys, note = [key], ""
    try:
        if svc is None:
            raise rm.MapError(f"{service} isn't in the timetable" if service else "")
        say(f"Following {svc.name} over the track...")
        sections, places, _, _ = rm.journey_sections(files, network, [(None, [(timetable, svc)])], route, say)
        stops = rm.journey(svc)
        first = tsw_stops.split_platform(svc.origin)[0] if svc.origin else ""
        title = f"{svc.name}  {first or '?'} → {stops[-1] if stops else svc.destination}"
    except rm.MapError as e:
        if svc is not None or service:          # no map of the service: the whole route instead
            note = f"{e} - the whole route instead" if str(e) else ""
            say(note or "The whole route instead...")
            keys.append(map_key(timetable_id, None, zoom))
        groups = rm.main_journeys(rm.journeys([(timetable, s) for s in table.values() if s.drivable]))
        say(f"Following {len(groups)} journeys over the track...")
        sections, places, _, _ = rm.journey_sections(files, network, groups, route, say)
        title = route
    units = rm.route_units(sections)
    line = [(round(x, 1), round(y, 1)) for s in sections for _, x, y in s.points[::LINE_EVERY]]

    def save(landmarks):
        svg, w, h, (left, top) = follow_map(sections, units, places, landmarks, 1 / (zoom * unit_px))
        size = (math.ceil(w * unit_px), math.ceil(h * unit_px))
        html = (f'<!doctype html><html><head><meta charset="utf-8"><style>{rm.CSS}{rm.ROUTE_CSS}{PAGE_CSS}</style>'
                f'</head><body><svg id="map" class="overview" viewBox="0 0 {w:.1f} {h:.1f}" style="position: '
                f'absolute; left: 0; top: 0; width: {size[0]}px; height: {size[1]}px; border: none">{svg}</svg>'
                '<script>var at = location.hash.slice(1).split(","), map = document.getElementById("map");'
                'map.style.left = -at[0] + "px"; map.style.top = -at[1] + "px";</script></body></html>')
        stamp = int(time.time() * 1000)
        meta = {"version": MAP_VERSION, "title": title, "route": route, "service": svc and svc.name, "note": note,
                "origin": origin, "left": left, "top": top, "zoom": zoom, "size": size, "line": line,
                "unit": units.unit, "unit_m": units.length, "scenery": landmarks is not None, "stamp": stamp}
        for k in keys:
            for name in os.listdir(CACHE):          # the pieces of an older drawing
                if name.startswith(k + " ") and re.fullmatch(r"\d+ \d+ \d+\.png", name[len(k) + 1:]):
                    os.remove(os.path.join(CACHE, name))
            write(os.path.join(CACHE, k + ".html"), html)
            write(meta_file(k), json.dumps(meta))
            made(meta_file(k))

    if not has_scenery(level):
        say("Drawing the map...")
        save(None)                          # to show while the scenery is read
    objects, sea, water = scenery_of(files, level, say)
    say("Drawing the map...")
    save(rm.route_landmarks(network, objects, sections, sea, water))


# ---------------------------------------------------------------- for the bridge window
class MapWatch(threading.Thread):
    """Where you are, and the map of what you're driving, for the bridge window, on a thread of its own. Maps are
    made as services start once the map has been asked for (`wanted`), each by this script run on its own, and
    their pieces drawn on another thread, the ones the window shows first.

        watch.map        # the map's meta (read_meta), or None
        watch.status     # what's happening, while there's no map to show
        watch.position   # (x, y) metres in the map's world, None when not known
        watch.heading    # the way the train last moved, a unit vector (x east, y south), or None
        watch.centre     # where the window's middle should be, map pixels: a little ahead of you
        watch.done       # the map's pieces drawn: (column, row)
        watch.off_line   # you're far from the map's line: it can't be where you are
    """
    CHECK_SECONDS = 3.0         # how often to look at which service you're driving
    POLL_SECONDS = 0.25         # how often to read where you are while the map is shown
    MOVED = 8.0                 # metres moved before the heading is taken again
    AHEAD = 0.22                # the window's middle is this share of its width ahead of you

    def __init__(self, log=None):
        super().__init__(daemon=True)
        self.api = core.TSWApi()
        self.log = log or (lambda msg: None)
        self.running = True
        self.shown = False          # set by the window
        self.wanted = False         # ... once the map has been shown: make maps
        self.view = (700, 476)      # ... its size in pixels
        self.zoom = ZOOM            # ... metres a pixel
        self.unit_px = UNIT_PX      # ... pixels a map unit
        self.map = None
        self.status = "Looking for the game..."
        self.position = self.heading = self.centre = None
        self.off_line = False
        self.done = set()
        self.near_line = []         # the map's pieces near its line
        self.target = None          # (timetable ID, service) you're driving
        self.last_check = 0.0
        self.last_place = None      # where the heading was last taken from
        self.proc = None            # the script making a map, while it runs
        self.failed = {}            # map key -> why it couldn't be made
        self.draw_failed = None     # why the last piece couldn't be drawn
        threading.Thread(target=self._draw_pieces, daemon=True).start()

    def run(self):
        while self.running:
            try:
                self.step()
            except Exception:                      # the game isn't running, or dropped a connection
                self.status = "Waiting for the game..."
                self.position = None
                time.sleep(1)
            time.sleep(self.POLL_SECONDS if self.shown else 1.0)

    def stop(self):
        self.running = False
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def step(self):
        api = self.api
        if not api.key and not api.load_key():
            self.status = "No API key - start TSW7 with -HTTPAPI"
            return
        now = time.monotonic()
        if not self.shown and now - self.last_check < self.CHECK_SECONDS:
            return
        info = api.get("DriverAid.PlayerInfo").get("Values") or {}
        if now - self.last_check >= self.CHECK_SECONDS:
            self.last_check = now
            service = info.get("currentServiceName")
            timetable = api.get_value("Timetable.VehicleID")
            target = (timetable, service if service and service != "None" else None) if timetable else None
            if target != self.target:
                self.target, self.position, self.heading, self.last_place = target, None, None, None
                self._set_map(None)
            self._find_map()
        geo = info.get("geoLocation") or {}
        meta = self.map
        if meta is None or "latitude" not in geo or "longitude" not in geo:
            self.position = None
            return
        self._place(world(geo["latitude"], geo["longitude"], meta["origin"]), info.get("currentTile"))

    def _place(self, at, game_tile=None):
        """Where you are now, the way you're going, and where the window's middle should be."""
        meta, (x, y) = self.map, at
        self.position = at
        if self.last_place is None:
            self.last_place = at
        elif math.dist(self.last_place, at) >= self.MOVED:
            d = math.dist(self.last_place, at)
            self.heading = ((x - self.last_place[0]) / d, (y - self.last_place[1]) / d)
            self.last_place = at
        px, py = to_px(meta, x, y)
        ahead = self.view[0] * self.AHEAD
        hx, hy = self.heading or (0.0, 0.0)
        self.centre = (px + hx * ahead, py + hy * ahead)
        line = meta.get("line") or ()
        far = min((math.dist(p, at) for p in line), default=0.0)
        if far > OFF_LINE and not self.off_line:
            self.log(f"Route map: you're {far / 1000:.1f} km from the line of {meta['title']} (tile {tile(x, y)}, "
                     f"the game says {game_tile})")
        self.off_line = far > OFF_LINE

    def _set_map(self, meta):
        if meta is not None and self.map is not None and (meta["key"], meta["stamp"]) == \
                (self.map["key"], self.map["stamp"]):
            return
        self.done = chunks_kept(meta) if meta else set()
        self.near_line = []
        if meta is not None:
            w, h = meta["size"]
            cells = set()
            for x, y in meta["line"]:
                px, py = to_px(meta, x, y)
                for i in range(max(0, int((px - NEAR_LINE) // CHUNK)), int((px + NEAR_LINE) // CHUNK) + 1):
                    for j in range(max(0, int((py - NEAR_LINE) // CHUNK)), int((py + NEAR_LINE) // CHUNK) + 1):
                        if i * CHUNK < w and j * CHUNK < h:
                            cells.add((i, j))
            self.near_line = sorted(cells)
        if meta is None or self.map is None or meta["key"] != self.map["key"]:
            self.centre = None
        self.map = meta
        if meta is not None and self.position is not None:
            self._place(self.position)

    def _find_map(self):
        """The map for what you're driving at the zoom wanted: kept already, or made now if the map's wanted."""
        target = self.target
        if target is None:
            self._set_map(None)
            self.status = "Drive a service, or free roam, to see the map"
            return
        key = map_key(*target, self.zoom)
        meta = read_meta(key)
        if meta is not None:
            self._set_map(meta)
            if meta.get("scenery"):
                return
        elif self.map is not None and self.map["key"] != key and \
                self.map["key"].rsplit(" z", 1)[0] != key.rsplit(" z", 1)[0]:
            self._set_map(None)                 # another service's (another zoom's is shown till this one's made)
        if self.proc is not None or not self.wanted:
            return
        if key in self.failed:
            self.status = self.failed[key]
            return
        self._start(target, key)

    def _start(self, target, key):
        """Runs this script to make the map, reading what it says as it goes."""
        exe = sys.executable
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.basename(exe).lower() == "pythonw.exe" and os.path.isfile(console):
            exe = console                       # its output comes through a pipe; no window either way
        args = [exe, os.path.abspath(__file__), "make", "--timetable", target[0], "--zoom", f"{self.zoom:g}",
                "--unit-px", f"{self.unit_px:.3f}"]
        if target[1]:
            args += ["--service", target[1]]
        what = f"the map of {target[1]}" if target[1] else "the route map"
        self.status = f"Making {what}..."
        self.log(f"Route map: making {what}")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     encoding="utf-8", errors="replace", env=env,
                                     creationflags=NO_WINDOW | LOW_PRIORITY)
        threading.Thread(target=self._read, args=(self.proc, key, what), daemon=True).start()

    def _read(self, proc, key, what):
        started, failed = time.monotonic(), None
        for line in proc.stdout:
            kind, _, text = line.rstrip("\n").partition(" ")
            if kind == "SAY" and self.map is None:
                self.status = text
            elif kind == "MAP":
                self.last_check = 0.0           # show it at once
            elif kind == "FAIL":
                failed = text
        proc.wait()
        if failed or proc.returncode:
            failed = failed or f"Making the map stopped (code {proc.returncode})"
            self.failed[key] = failed
            self.status = failed
            self.log(f"Route map: {failed}")
        else:
            self.log(f"Route map: {what} made in {time.monotonic() - started:.0f} s")
        self.proc = None
        self.last_check = 0.0

    def wanted_pieces(self):
        """The map's pieces not drawn yet, those the window needs first: where it is, then ahead of you, then along
        the line, nearest first."""
        meta, centre = self.map, self.centre
        if meta is None:
            return []
        w, h = meta["size"]
        vw, vh = self.view
        hx, hy = self.heading or (0.0, 0.0)
        near = centre or to_px(meta, *meta["line"][0])
        out = []
        for cx, cy in ((centre, (centre[0] + hx * vw, centre[1] + hy * vw)) if centre else ()):
            box = (cx - vw / 2 - CHUNK / 4, cy - vh / 2 - CHUNK / 4, cx + vw / 2 + CHUNK / 4, cy + vh / 2 + CHUNK / 4)
            cells = [(i, j) for i in range(max(0, int(box[0] // CHUNK)), int(min(box[2], w - 1) // CHUNK) + 1)
                     for j in range(max(0, int(box[1] // CHUNK)), int(min(box[3], h - 1) // CHUNK) + 1)]
            out += sorted(cells, key=lambda c: math.dist(((c[0] + 0.5) * CHUNK, (c[1] + 0.5) * CHUNK), (cx, cy)))
        out += sorted(self.near_line, key=lambda c: math.dist(((c[0] + 0.5) * CHUNK, (c[1] + 0.5) * CHUNK), near))
        seen, done = set(), self.done
        return [c for c in out if c not in done and not (c in seen or seen.add(c))]

    def _draw_pieces(self):
        """Turns the map into pictures a piece at a time, once the map's been asked for."""
        tries = {}
        while self.running:
            meta = self.map
            todo = [c for c in self.wanted_pieces() if tries.get((meta and meta["stamp"], c), 0) < 2] \
                if self.wanted else []
            if not todo:
                time.sleep(0.3)
                continue
            cell = todo[0]
            try:
                draw_chunk(meta, cell)
                if self.map is meta:
                    self.done.add(cell)
                self.draw_failed = None
            except Exception as e:
                tries[(meta["stamp"], cell)] = tries.get((meta["stamp"], cell), 0) + 1
                if str(e) != self.draw_failed:
                    self.log(f"Route map: {e}")
                self.draw_failed = str(e)
                time.sleep(2)


def main(args):
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
    if not args or args[0] != "make":
        raise SystemExit(__doc__)
    opts = dict(zip(args[1::2], args[2::2]))
    try:
        make(opts["--timetable"], opts.get("--service"), float(opts.get("--zoom", ZOOM)),
             float(opts.get("--unit-px", UNIT_PX)), say=lambda text: print("SAY", text),
             made=lambda path: print("MAP", path))
    except rm.MapError as e:
        print("FAIL", e)
        raise SystemExit(1)
    except Exception as e:
        print("FAIL", f"Making the map failed: {e}")
        with open(os.path.join(HERE, "livemap_error.log"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
