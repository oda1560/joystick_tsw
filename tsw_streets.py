"""
Roads and towns from OpenStreetMap under the first page of a whole-route map (tsw_routemap.py), faintly, to tell
where you are between stations: motorways and main roads with their numbers, built-up areas, rivers, canals and
lakes, other railways, and the names of towns and villages. Names and road numbers go only where the page has room:
clear of the line, the stations' names, the boards, the insets and the key. The insets stay as they are.

Where things go: the game lays out a route's world in UTM (WGS84, scale 0.9996, the zone of the route's central
point) measured from the route's central latitude and longitude (TS2WorldSettings in its level), x east and y south,
so a real latitude and longitude go straight onto the map. Checked on Airedale & Wharfedale, where the route's track
lies on OpenStreetMap's railways; a route whose track doesn't (half of it more than OFF_RAILS away) gets no roads,
as the map would be wrong.

The data comes from the Overpass service the first time a route's map is made (about half a minute), and is kept
in street_maps next to this script. With no internet the map is made without it. On a smaller page scale (a long
route) B roads and villages are left out.

Map data © OpenStreetMap contributors, under the Open Database License: the page says so under the map.
"""

import gzip
import json
import math
import os
import re
import urllib.parse
import urllib.request

import tsw_paks

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "street_maps")
VERSION = 1
# Overpass servers, tried in turn: OpenStreetMap France's has the whole world and answered a route in 6 s on
# 2026-10-17 while the main one was full (its queue) and the Kumi Systems mirror gave errors
OVERPASS = ("https://overpass.openstreetmap.fr/api/interpreter", "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
AGENT = "tsw-joystick route maps (personal use)"
FETCH_SECONDS = 300             # how long to wait for the Overpass service
SECONDARY_UPTO = 50.0           # metres a map unit: B roads only on pages at least this close
VILLAGES_UPTO = 60.0            # ... and villages' names
SMALLEST = 24.0                 # map units round: a built-up area or lake smaller than this is left out
STRAIGHTER = 0.3                # map units: lines kept to within this of the real ones, with fewer points
OFF_RAILS = 100.0               # metres: half the route's track further than this from OpenStreetMap's railways: no
                                # roads (the route's world isn't laid out as it should be)
REF_APART = 110.0               # map units between two of one road's numbers
SAME_NAME = 3000.0              # metres: a town's name this close to a station of that name is left out
# (casing width, fill width, casing colour, fill colour, drawing order) in map units
ROADS = {"motorway": (1.8, 1.28, "#7d7a74", "#f3dfa2", 4), "trunk": (1.6, 1.15, "#8a8780", "#fff", 3),
         "primary": (1.47, 1.02, "#8a8780", "#fff", 2), "secondary": (1.22, 0.83, "#a29f98", "#fff", 1)}
LINK = 0.7                      # slip roads this much narrower
RIVERS = {"river": 1.44, "canal": 1.17}
PLACES = {"city": ("os-city", 10.5, 1.5), "town": ("os-town", 8.5, 1.2), "village": ("os-village", 5.6, 0.4)}
REF_SIZE = 4.65                 # map units: road numbers' letters
CSS = """
.os-built { fill: #f1efea; }
.os-ind { fill: #ece9e3; }
.os-water { fill: #cfe3f3; }
.os-river { fill: none; stroke: #9cc7e6; stroke-linecap: round; stroke-linejoin: round; }
.os-case, .os-fill { fill: none; stroke-linecap: round; stroke-linejoin: round; }
.os-rail { fill: none; stroke: #a8a49c; stroke-width: 0.7; }
.os-raildash { fill: none; stroke: #fff; stroke-width: 0.4; stroke-dasharray: 1.6 1.6; }
.os-ref { fill: #fff; stroke: #8a8780; stroke-width: 0.45; }
.os-reftext { font-size: 4.65px; font-weight: 600; fill: #55524c; }
.os-city { font-size: 10.5px; font-weight: 600; letter-spacing: 1.5px; fill: #9d988e; }
.os-town { font-size: 8.5px; font-weight: 600; letter-spacing: 1.2px; fill: #9d988e; }
.os-village { font-size: 5.6px; font-weight: 600; letter-spacing: 0.4px; fill: #a39e94; }
.streets ~ .maplabel, .streets ~ .linespeed, .streets ~ .landmarkname {
  paint-order: stroke; stroke: #fff; stroke-width: 3px; stroke-linejoin: round; }
"""

# WGS84 and UTM's scale on its meridian
A, F = 6378137.0, 1 / 298.257223563
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
K0 = 0.9996


class StreetError(Exception):
    """Roads and towns that can't be had, and why."""


# ---------------------------------------------------------------- where things are
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


class World:
    """A route's world: latitude and longitude to its metres (x east, y south) and back. `origin`: the route's
    central (latitude, longitude)."""

    def __init__(self, origin):
        self.origin = origin
        self.lon0 = zone_meridian(origin[1])
        self.e0, self.n0 = utm(origin[0], origin[1], self.lon0)

    def at(self, lat, lon):
        e, n = utm(lat, lon, self.lon0)
        return e - self.e0, self.n0 - n

    def back(self, x, y):
        """The (latitude, longitude) of a place in the world (Newton's method, to a centimetre)."""
        lat, lon = self.origin
        for _ in range(30):
            wx, wy = self.at(lat, lon)
            dx, dy = x - wx, y - wy
            if abs(dx) < 0.01 and abs(dy) < 0.01:
                break
            h = 1e-5
            ax, ay = self.at(lat + h, lon)
            bx, by = self.at(lat, lon + h)
            j11, j21, j12, j22 = (ax - wx) / h, (ay - wy) / h, (bx - wx) / h, (by - wy) / h
            det = j11 * j22 - j12 * j21
            lat += (j22 * dx - j12 * dy) / det
            lon += (j11 * dy - j21 * dx) / det
        return lat, lon


def geo_origin(files, level):
    """A route's central (latitude, longitude), which its world is measured from: TS2WorldSettings in its level."""
    path = files.file_of(level)
    if not path or path + ".umap" not in files.where or path + ".uexp" not in files.where:
        return None
    pkg = tsw_paks.Package(level, files.where[path + ".umap"].read(path + ".umap"),
                           files.where[path + ".uexp"].read(path + ".uexp"))
    for i in range(1, len(pkg.exports) + 1):
        if pkg.class_name(i) == "TS2WorldSettings":
            p = pkg.properties(i)
            if p.get("CentralLatitude") is not None and p.get("CentralLongitude") is not None:
                return float(p["CentralLatitude"]), float(p["CentralLongitude"])
    return None


# ---------------------------------------------------------------- the data
def query(bbox, metres_a_unit):
    """The Overpass query for what's drawn on a page at `metres_a_unit` over (south, west, north, east)."""
    roads = "motorway|trunk|primary" + ("|secondary" if metres_a_unit <= SECONDARY_UPTO else "")
    roads += "|" + "|".join(r + "_link" for r in roads.split("|"))
    places = "city|town" + ("|village" if metres_a_unit <= VILLAGES_UPTO else "")
    round_m = round(SMALLEST * metres_a_unit)
    b = "({:.4f},{:.4f},{:.4f},{:.4f})".format(*bbox)
    return (f'[out:json][timeout:{FETCH_SECONDS - 30}];('
            f'way["highway"~"^({roads})$"]{b};'
            f'way["landuse"~"^(residential|commercial|retail|industrial)$"]{b}(if: length() > {round_m});'
            f'way["natural"="water"]{b}(if: length() > {round_m});'
            f'way["waterway"~"^(river|canal)$"]{b};'
            f'way["railway"="rail"][!"service"]{b};'
            f'node["place"~"^({places})$"]["name"]{b};'
            f');out geom qt;')


def kind_of(tags):
    """What an OpenStreetMap thing is drawn as, or None."""
    if "place" in tags:
        return "place"
    if "highway" in tags:
        return "road"
    if tags.get("landuse") in ("residential", "commercial", "retail"):
        return "built"
    if tags.get("landuse") == "industrial":
        return "industrial"
    if tags.get("natural") == "water":
        return "water"
    if tags.get("waterway") in RIVERS:
        return "river"
    if tags.get("railway") == "rail":
        return "rail"
    return None


KEEP = ("highway", "ref", "name", "place", "population", "waterway")


def compact(elements):
    """Overpass's answer cut down to what's drawn: [{"kind", "tags", "at": [[lat, lon], ...]}]."""
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        kind = kind_of(tags)
        if kind is None:
            continue
        if el.get("type") == "node":
            at = [[round(el["lat"], 6), round(el["lon"], 6)]]
        else:
            at = [[round(g["lat"], 6), round(g["lon"], 6)] for g in el.get("geometry") or () if g]
        if at:
            out.append({"kind": kind, "tags": {k: tags[k] for k in KEEP if k in tags}, "at": at})
    return out


def fetch(text):
    """The elements an Overpass server finds: each server in turn, till one answers."""
    body = urllib.parse.urlencode({"data": text}).encode()
    why = None
    for url in OVERPASS:
        try:
            request = urllib.request.Request(url, data=body, headers={"User-Agent": AGENT,
                                                                       "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(request, timeout=FETCH_SECONDS) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            answer = json.loads(data)
            if "runtime error" in str(answer.get("remark", "")):        # stopped part way: not all of it
                raise StreetError(answer["remark"])
            return answer["elements"]
        except (OSError, ValueError, KeyError, StreetError) as e:      # URLError and timeouts are OSErrors
            why = e
    raise StreetError(f"OpenStreetMap couldn't be reached ({why})")


def cache_file(name):
    return os.path.join(CACHE, re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name).strip() + ".json")


def elements(name, text, say=print, ask=fetch):
    """What `text` (a query) finds, kept for the route `name`: asked again only when the query changes."""
    path = cache_file(name)
    try:
        with open(path, encoding="utf-8") as f:
            kept = json.load(f)
        if kept.get("version") == VERSION and kept.get("query") == text:
            return kept["elements"]
    except (OSError, ValueError, AttributeError):
        pass
    say("Getting the roads and towns from OpenStreetMap (the first time for this route, a minute or so)...")
    found = compact(ask(text))
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(path + ".new", "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "query": text, "elements": found}, f)
        os.replace(path + ".new", path)
    except OSError:
        pass                            # drawn this time, asked for again next time
    return found


# ---------------------------------------------------------------- drawing
def straighter(pts, within=STRAIGHTER):
    """A line with fewer points, none of it further than `within` from the original (Douglas-Peucker)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    todo = [(0, len(pts) - 1)]
    while todo:
        i, j = todo.pop()
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        far, k = 0.0, None
        for m in range(i + 1, j):
            px, py = pts[m]
            d = abs(dx * (ay - py) - dy * (ax - px)) / length if length else math.hypot(px - ax, py - ay)
            if d > far:
                far, k = d, m
        if k is not None and far > within:
            keep[k] = True
            todo += [(i, k), (k, j)]
    return [p for p, kept in zip(pts, keep) if kept]


def path(pts, closed=False):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + (" Z" if closed else "")


def length(pts):
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def point_along(pts, d):
    """The point `d` along a line."""
    for a, b in zip(pts, pts[1:]):
        s = math.dist(a, b)
        if d <= s and s:
            t = d / s
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        d -= s
    return pts[-1]


def text_width(text, size, spacing=0.0):
    """Roughly, as tsw_routemap.text_width, with the letters spaced out."""
    return len(text) * (size * 0.56 + spacing)


def plain(name):
    return re.sub(r"[^a-z]", "", name.lower())


def off_rails(sections, rails):
    """How far (metres) the middle one of the route's track points is from OpenStreetMap's railways (world)."""
    cell = 250.0
    grid = {}
    for line in rails:
        for a, b in zip(line, line[1:]):        # points every 25 m along each piece
            n = max(1, int(math.dist(a, b) // 25))
            for k in range(n + 1):
                p = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
                grid.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(p)
    pts = [(x, y) for s in sections for _, x, y in s.points]
    sample = pts[::max(1, len(pts) // 400)]
    if not sample or not grid:
        return math.inf
    far = []
    for x, y in sample:
        i, j = int(x // cell), int(y // cell)
        near = [q for di in (-1, 0, 1) for dj in (-1, 0, 1) for q in grid.get((i + di, j + dj), ())]
        far.append(min((math.dist((x, y), q) for q in near), default=math.inf))
    far.sort()
    return far[len(far) // 2]


class Streets:
    """The roads and towns under one route's whole-route map: `svg` draws them for the first page's map. `origin`:
    the route's central (latitude, longitude); `name`: what they're kept under (the route's)."""

    def __init__(self, origin, name, say=print, ask=fetch):
        self.world = World(origin)
        self.name, self.say, self.ask = name, say, ask
        self.drawn = False              # they're on the map: the page credits OpenStreetMap

    def svg(self, view, sections, stations=()):
        """The roads and towns on a tsw_routemap.MapView of the whole page (its `rect`), drawn last so their names
        go where nothing else is, but to go under everything else. `stations`: [(name, (x, y))] in the world."""
        r = view.rect
        corners = [self.world.back(*view.back(x, y)) for x in (r[0], r[2]) for y in (r[1], r[3])]
        bbox = (min(c[0] for c in corners) - 0.005, min(c[1] for c in corners) - 0.005,
                max(c[0] for c in corners) + 0.005, max(c[1] for c in corners) + 0.005)
        per_unit = 1 / view.scale
        try:
            found = elements(self.name, query(bbox, per_unit), self.say, self.ask)
        except StreetError as e:
            self.say(f"No roads and towns on the map: {e}")
            return ""
        world = [(e, [self.world.at(*p) for p in e["at"]]) for e in found]
        off = off_rails(sections, [pts for e, pts in world if e["kind"] == "rail"])
        if off > OFF_RAILS:
            self.say("No roads and towns on the map: the route's track isn't where OpenStreetMap's railways are "
                     + ("(none here)" if off == math.inf else f"(half of it over {off:.0f} m away)"))
            return ""
        under = self._shapes(view, world, per_unit)
        labels = self._labels(view, world, sections, stations, per_unit)
        self.drawn = True
        return f'<g class="streets">{under}{labels}</g>'

    def _shapes(self, view, world, per_unit):
        x0, y0, x1, y1 = view.rect
        layers = {k: [] for k in ("built", "water", "river", "rail")}
        cases, fills = [[] for _ in range(5)], [[] for _ in range(5)]
        for e, pts in world:
            kind, tags = e["kind"], e["tags"]
            if kind == "place":
                continue
            q = [view.at(*p) for p in pts]
            if not any(x0 - 20 < x < x1 + 20 and y0 - 20 < y < y1 + 20 for x, y in q):
                continue
            q = straighter(q)
            if kind in ("built", "industrial", "water"):
                if len(q) < 3 or length(q) < SMALLEST:
                    continue
                cls = {"built": "os-built", "industrial": "os-ind", "water": "os-water"}[kind]
                layers["water" if kind == "water" else "built"].append(f'<path d="{path(q, True)}" class="{cls}"/>')
            elif kind == "river":
                layers["river"].append(f'<path d="{path(q)}" class="os-river" '
                                       f'stroke-width="{RIVERS[tags.get("waterway")]}"/>')
            elif kind == "rail":
                layers["rail"].append(f'<path d="{path(q)}" class="os-rail"/><path d="{path(q)}" class="os-raildash"/>')
            elif kind == "road":
                road = tags.get("highway", "").replace("_link", "")
                if road not in ROADS or (road == "secondary" and per_unit > SECONDARY_UPTO):
                    continue
                cw, fw, cc, fc, order = ROADS[road]
                if tags.get("highway", "").endswith("_link"):
                    cw, fw = cw * LINK, fw * LINK
                cases[order].append(f'<path d="{path(q)}" class="os-case" stroke="{cc}" stroke-width="{cw:.2f}"/>')
                fills[order].append(f'<path d="{path(q)}" class="os-fill" stroke="{fc}" stroke-width="{fw:.2f}"/>')
        return "".join("".join(layers[k]) for k in ("built", "water", "river", "rail")) + \
            "".join("".join(c) for c in cases) + "".join("".join(f) for f in fills)

    def _labels(self, view, world, sections, stations, per_unit):
        """Road numbers and town names, each only where it's clear of everything on the page (view.taken), the
        line and each other."""
        line = type(view.taken)()               # the line's points as small boxes (a tsw_routemap.Boxes)
        for s in sections:
            for _, x, y in s.points[::3]:
                px, py = view.at(x, y)
                line.add((px - 2, py - 2, px + 2, py + 2))

        def free(box):
            return view.free(box) and not line.hits(box)
        out = []
        # main roads' numbers (not B roads'): the busier roads first, each number along its longest pieces,
        # REF_APART apart
        refs = {}
        for e, pts in world:
            road = e["tags"].get("highway", "")
            if e["kind"] == "road" and e["tags"].get("ref") and road in ROADS and road != "secondary":
                ref = e["tags"]["ref"].split(";")[0].strip()
                refs.setdefault((ROADS[road][4], ref), []).append([view.at(*p) for p in pts])
        for (_, ref), ways in sorted(refs.items(), key=lambda kv: -kv[0][0]):
            done = []
            w, h = text_width(ref, REF_SIZE) + 3.4, REF_SIZE * 1.45
            for q in sorted(ways, key=length, reverse=True):
                total = length(q)
                if total < w * 2:
                    break
                for t in (0.5, 0.3, 0.7):
                    x, y = point_along(q, total * t)
                    box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
                    if any(math.dist((x, y), d) < REF_APART for d in done) or not free(box):
                        continue
                    done.append((x, y))
                    view.take(box, solid=False)
                    out.append(f'<rect x="{box[0]:.1f}" y="{box[1]:.1f}" width="{w:.1f}" height="{h:.1f}" rx="1.2" '
                               f'class="os-ref"/><text x="{x:.1f}" y="{y + REF_SIZE * 0.36:.1f}" '
                               f'text-anchor="middle" class="os-reftext">{esc(ref)}</text>')
                    break
        # towns and villages: cities first, then the biggest
        rank = {"city": 0, "town": 1, "village": 2}
        places = [(e["tags"], pts[0]) for e, pts in world
                  if e["kind"] == "place" and e["tags"].get("place") in PLACES
                  and (e["tags"]["place"] != "village" or per_unit <= VILLAGES_UPTO)]

        def population(tags):
            try:
                return -int(re.sub(r"\D", "", str(tags.get("population", ""))) or 0)
            except ValueError:
                return 0
        for tags, p in sorted(places, key=lambda tp: (rank[tp[0]["place"]], population(tp[0]))):
            name = tags.get("name", "")
            if not name or any(plain(name)[:6] == plain(s)[:6] and math.dist(p, at) < SAME_NAME
                               for s, at in stations):
                continue                        # the station's name says it
            cls, size, spacing = PLACES[tags["place"]]
            text = name.upper() if tags["place"] != "village" else name
            x, y = view.at(*p)
            w = text_width(text, size, spacing)
            for dx, dy in ((0, 0), (0, -size), (0, size), (-w / 3, 0), (w / 3, 0)):
                box = (x + dx - w / 2, y + dy - size * 0.8, x + dx + w / 2, y + dy + size * 0.25)
                if free(box):
                    view.take(box, solid=False)
                    out.append(f'<text x="{x + dx:.1f}" y="{y + dy:.1f}" text-anchor="middle" '
                               f'class="{cls}">{esc(text)}</text>')
                    break
        return "".join(out)


def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
