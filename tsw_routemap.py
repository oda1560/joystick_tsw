"""
A printable map of the speed limits along a timetabled service, read from Train Sim World's route files, to
learn a route from (like a real driver) instead of relying on the game's speed limit hints.

Each piece of track (a "ribbon") takes its limit from its track rule (e.g. 25 mph), and the route sets other
limits on stretches of it, separately for each direction of travel. A service's path comes from its timetable
data track, which places it on the track every so often; the pieces in between are found through the junctions.
Checked on Airedale-Wharfedale 2S00: the timetable's AI driver never runs faster than these limits.

A map of the whole route instead puts every line its passenger journeys use on a few pages, the limits both ways
on each: the longest stretch of a journey not yet drawn becomes a section, the track alongside it (the other line,
or the same track the other way) going with it, and so on until what's left is short. Its first page maps them
with a board at each place to slow down and every station named, crowded places drawn again larger where the page
is empty, so it can be printed on its own; and landmarks from the route's scenery: tunnels, viaducts, roads over the
line and level crossings. Under it, faintly, the real roads, towns, rivers, canals and lakes from OpenStreetMap
(tsw_streets.py).

    python tsw_routemap.py                    choose a route and a service
    python tsw_routemap.py airedale           list a route's services
    python tsw_routemap.py airedale 2S00      write the map of one and open it to print
    python tsw_routemap.py airedale all       write the map of the whole route

Maps are saved in the folder chosen in the window (tsw_routemap_ui.py), at first route_maps next to this script.

The limits are those for passenger trains. A board marks where the front of the train meets the limit.
"""

import bisect
import collections
import datetime
import functools
import heapq
import html
import itertools
import json
import math
import os
import re
import sys

import tsw_paks
import tsw_stops
import tsw_streets
from tsw_units import MILE, MPH

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "route_maps")                 # where maps go unless another folder is chosen
SETTINGS_FILE = os.path.join(HERE, "routemap_settings.json")
DEFAULT_LIMIT = 90 * MPH    # a limit the files leave out is the game's default: the AI runs 90 mph on those
MATCH_SLACK = 5.0           # metres (+3 %) a path between two data track rows may differ from the rows' distance
ROUTE_DEFINITION = re.compile(r"^/[^/]+/RouteDefinition/", re.I)


# ---------------------------------------------------------------- the route's track
class Ribbon:
    __slots__ = ("start_node", "end_node", "length", "limit", "limits", "signals", "platforms", "position",
                 "tangent", "radius", "spiral", "curvature", "arc")

    def __init__(self, start_node, end_node, length, limit, position):
        self.start_node, self.end_node = start_node, end_node
        self.length = length                        # metres
        self.limit = limit                          # m/s, from the track rule
        self.limits = {True: [], False: []}         # by direction (True: start -> end): [(from, to, m/s)]
        self.signals = {True: [], False: []}        # signals facing that direction: [(at, name)]
        self.platforms = []                         # [(from, to, name)]
        self.position = position                    # (x, y) of its start in metres, x east and y south
        self.tangent = (1.0, 0.0)                   # its direction at the start, a unit vector
        self.radius = None                          # metres, a circular arc's (signed: 1 / radius = -curvature)
        self.spiral = False                         # a transition curve, whose radius changes along it
        self.curvature = 0.0                        # 1/m of the arc from its start to its end (+: towards
        self.arc = length                           #  (-tangent y, tangent x)), and its length in metres


class TrackNetwork:
    """The track of a route: its ribbons, which ones meet where, and which way a train can go at a junction."""

    def __init__(self, files, tiles):
        self.ribbons = {}
        self.at_node = collections.defaultdict(list)
        self.allowed = {}                           # junction node -> {(ribbon, ribbon)} a train can pass between
        self._around = {}                           # ribbon -> ribbons up to two away (leads_on)
        self.markers = []                           # [(type, name, ribbon, from, to)] other than platforms
        rules = {}
        for path in tiles:
            self._read_tile(files, path, rules)
        for guid, r in self.ribbons.items():
            self.at_node[r.start_node].append(guid)
            self.at_node[r.end_node].append(guid)
        self.node_position = {r.start_node: r.position for r in self.ribbons.values()}
        self._shape()

    def _shape(self):
        """Each ribbon as a circular arc from its start (place and direction) to the node at its end. Fitted like
        that, the arcs' lengths are within 1 % of the files' own, transition curves included. A node no ribbon
        starts at is placed from the arcs that end there, and failing that by running straight on."""
        def arc_to(r, end):
            cx, cy = end[0] - r.position[0], end[1] - r.position[1]
            c2 = cx * cx + cy * cy
            if c2 < 1e-6:
                return
            tx, ty = r.tangent
            cross, dot = tx * cy - ty * cx, tx * cx + ty * cy
            r.curvature = 2 * cross / c2
            r.arc = abs(2 * math.atan2(cross, dot) / r.curvature) if abs(r.curvature) > 1e-9 else math.sqrt(c2)

        later = []
        for r in self.ribbons.values():
            end = self.node_position.get(r.end_node)
            if end is not None:
                arc_to(r, end)
            elif r.spiral:
                later.append(r)
            else:
                r.curvature = -1 / r.radius if r.radius else 0.0
                self.node_position[r.end_node] = self._point(r, 1.0)
        for r in later:
            end = self.node_position.get(r.end_node)
            if end is not None:
                arc_to(r, end)
            else:
                self.node_position[r.end_node] = self._point(r, 1.0)

    @staticmethod
    def _point(r, at):
        s, k = at * r.arc, r.curvature
        (tx, ty), (x, y) = r.tangent, r.position
        if abs(k * s) < 1e-6:
            along, aside = s, k * s * s / 2
        else:
            along, aside = math.sin(k * s) / k, (1 - math.cos(k * s)) / k
        return x + tx * along - ty * aside, y + ty * along + tx * aside

    def leads_on(self, g1, g2):
        """Whether ribbon g2 is g1, or joins it directly or through one other (a sample STEP metres on may be past
        a short one)."""
        if g1 == g2:
            return True
        if g1 not in self._around:
            def next_to(g):
                r = self.ribbons[g]
                return set(self.at_node.get(r.start_node, ())) | set(self.at_node.get(r.end_node, ()))
            one = next_to(g1)
            self._around[g1] = one.union(*(next_to(g) for g in one))
        return g2 in self._around[g1]

    def position(self, guid, at):
        """(x, y) in metres of a point on a ribbon, `at` 0-1 along it."""
        return self._point(self.ribbons[guid], at)

    def heading(self, guid, at):
        """Which way a ribbon runs at a point (start -> end), as a unit vector."""
        r = self.ribbons[guid]
        a = r.curvature * at * r.arc
        (tx, ty), c, s = r.tangent, math.cos(a), math.sin(a)
        return tx * c - ty * s, ty * c + tx * s

    def _read_tile(self, files, path, rules):
        pak = files.where[path + ".umap"]
        head = pak.read(path + ".umap")
        if b"NetworkRibbon" not in head:
            return
        pkg = tsw_paks.Package("/" + path, head, pak.read(path + ".uexp"))
        children = collections.defaultdict(dict)
        for i, e in enumerate(pkg.exports, 1):
            children[e["outer"]][e["name"]] = i
        for i in range(1, len(pkg.exports) + 1):
            kind = pkg.class_name(i)
            if kind == "NetworkRibbon":
                self._read_ribbon(files, pkg, pkg.properties(i), children[i], rules)
            elif kind.startswith("Network") and kind.endswith("Junction"):
                self._read_junction(kind, pkg.properties(i))

    def _read_ribbon(self, files, pkg, p, kids, rules):
        curve = kids.get(str(p.get("Curve")))
        shape = pkg.properties(curve) if curve else {}
        start = p.get("CachedStartPosition") or {}
        r = Ribbon(p.get("StartNodeGuid"), p.get("EndNodeGuid"), shape.get("Length", 0.0) / 100,
                   self._rule_limit(files, p.get("TrackRule"), rules),
                   (start.get("X", 0.0) / 100, start.get("Y", 0.0) / 100))
        tangent = shape.get("StartTangent2D") or (1.0, 0.0)
        size = math.hypot(tangent[0], tangent[1]) or 1.0
        r.tangent = (tangent[0] / size, tangent[1] / size)
        r.radius = shape["Radius"] / 100 if shape.get("Radius") else None
        r.spiral = curve is not None and pkg.class_name(curve) == "NetworkCurveClothoidSpiral"
        for key, forward in (("NetworkProperties", True), ("NetworkPropertiesReverseOrder", False)):
            for name in p.get(key) or []:
                j = kids.get(str(name))
                if not j:
                    continue
                q = pkg.properties(j)
                if pkg.class_name(j) == "TrackSpeedLimitProperty":
                    r.limits[forward].append((q.get("Start", 0.0), q.get("End", 1.0),
                                              q.get("PrimarySpeedLimit") or DEFAULT_LIMIT))
                elif "SignalID" in q:
                    r.signals[forward].append((q.get("Location", 0.0),
                                               str(q.get("SignalDisplayID") or q.get("SignalID"))))
        for j in kids.values():
            if pkg.class_name(j) == "TrackMarkerProperty":
                q = pkg.properties(j)
                name = str(q.get("DisplayName") or q.get("MarkerName") or "")
                if str(q.get("MarkerType", "")).endswith("Platform"):
                    r.platforms.append((q.get("Start", 0.0), q.get("End", 1.0), name))
                else:
                    self.markers.append((str(q.get("MarkerType", "")).split("::")[-1], name, p.get("RibbonGuid"),
                                         q.get("Start", 0.0), q.get("End", 1.0)))
        self.ribbons[p.get("RibbonGuid")] = r

    @staticmethod
    def _rule_limit(files, rule, rules):
        """The passenger limit a track rule asset gives its track."""
        if rule is None or rule.package is None:
            return DEFAULT_LIMIT
        if rule.package not in rules:
            limit = DEFAULT_LIMIT
            pkg = files.package(rule.package)
            for i in range(1, len(pkg.exports) + 1) if pkg else ():
                if pkg.class_name(i) == "TrackSpeedLimitProperty":
                    limit = pkg.properties(i).get("PrimarySpeedLimit") or DEFAULT_LIMIT
            rules[rule.package] = limit
        return rules[rule.package]

    def _read_junction(self, kind, p):
        def ribbon(key):
            return (p.get(key) or {}).get("RibbonGuid")
        if kind == "NetworkTurnoutJunction":
            pairs = [(ribbon("IngoingRibbonConnection"), ribbon(k))
                     for k in ("OutgoingRibbonConnection", "TurnoutRibbonConnection")]
        elif kind == "Network3WayJunction":
            pairs = [(ribbon("IngoingRibbonConnection"), ribbon(k))
                     for k in ("OutgoingRibbonConnection", "TurnoutRibbon1Connection", "TurnoutRibbon2Connection")]
        elif kind == "NetworkCrossingJunction":
            pairs = [(ribbon("IngoingRibbonConnection1"), ribbon("OutgoingRibbonConnection1")),
                     (ribbon("IngoingRibbonConnection2"), ribbon("OutgoingRibbonConnection2"))]
        else:
            return                                  # slips, portals, turntables: any way (the length check stays)
        moves = self.allowed.setdefault(p.get("NodeGuid"), set())
        for a, b in pairs:
            moves.update(((a, b), (b, a)))

    def _next(self, guid, forward):
        """(ribbon, forward) a train can go on to, leaving a ribbon at its end (forward) or start."""
        node = self.ribbons[guid].end_node if forward else self.ribbons[guid].start_node
        for other in self.at_node.get(node, ()):
            if other == guid or (node in self.allowed and (guid, other) not in self.allowed[node]):
                continue
            o = self.ribbons[other]
            if o.start_node == node:
                yield other, True
            if o.end_node == node:
                yield other, False

    def _route(self, g1, l1, forward, g2, l2, want):
        """The way from ribbon g1 at l1 (0-1 along it) to g2 at l2 whose length is nearest `want` metres, as
        ([(ribbon, from, to)], length, arriving forward), or (None, None, None)."""
        best = None
        slack = MATCH_SLACK + 0.03 * want
        for f in (True, False) if forward is None else (forward,):
            r1 = self.ribbons[g1]
            if g1 == g2 and (l2 >= l1 if f else l2 <= l1):
                length = abs(l2 - l1) * r1.length
                if best is None or abs(length - want) < best[0]:
                    best = (abs(length - want), length, ((g1, l1, l2),), f)
            heap = [((1 - l1 if f else l1) * r1.length, 0, g1, f, ((g1, l1, 1.0 if f else 0.0),))]
            seen, pushed = {}, 0
            while heap:
                d, _, g, gf, steps = heapq.heappop(heap)
                if d > want + slack:
                    break
                for n, nf in self._next(g, gf):
                    rn = self.ribbons[n]
                    if n == g2:
                        length = d + (l2 if nf else 1 - l2) * rn.length
                        if best is None or abs(length - want) < best[0]:
                            best = (abs(length - want), length, steps + ((n, 0.0 if nf else 1.0, l2),), nf)
                    nd = d + rn.length
                    if seen.get((n, nf), math.inf) <= nd:
                        continue
                    seen[(n, nf)] = nd
                    pushed += 1
                    heapq.heappush(heap, (nd, pushed, n, nf, steps + ((n, 0.0 if nf else 1.0, 1.0 if nf else 0.0),)))
        if best is None or best[0] > slack:
            return None, None, None
        return best[2], best[1], best[3]

    def follow(self, rows):
        """The pieces of track a service runs over, from its data track rows: [(metres from the start of the
        service, ribbon, from, to)] (from > to: running end -> start), and the stretches [(from, to)] in metres
        that couldn't be followed."""
        points = []             # [metres arriving, metres leaving, ribbon, at, reverses there]
        for row in rows:
            loc = row.get("Location") or {}
            if loc.get("RibbonReference") not in self.ribbons:
                continue
            metres = row.get("Distance", 0.0) / 100
            point = [metres, metres, loc["RibbonReference"], loc.get("RibbonLocation", 0.0),
                     str(row.get("DataType", "")).endswith("ReversePoint")]
            if points and points[-1][2:4] == point[2:4]:     # the count jumps on where the train reverses
                points[-1][1] = metres
                points[-1][4] = points[-1][4] or point[4]
            else:
                points.append(point)
        pieces, gaps, forward = [], [], None
        for (_, d1, g1, l1, reverses), (d2, _, g2, l2, _) in zip(points, points[1:]):
            steps, _, arriving = self._route(g1, l1, None if reverses else forward, g2, l2, d2 - d1)
            if steps is None and forward is not None and not reverses:
                steps, _, arriving = self._route(g1, l1, None, g2, l2, d2 - d1)
            if steps is None:
                gaps.append((d1, d2))
                forward = None
                continue
            forward = arriving
            d = d1
            for g, a, b in steps:
                if a != b:
                    pieces.append((d, g, a, b))
                    d += abs(b - a) * self.ribbons[g].length
        return pieces, gaps



class Profile:
    """What a service runs past, in metres from the start of its path."""

    def __init__(self, network, pieces, gaps, stops):
        self.limits, self.signals, self.points, self.gaps = [], [], [], gaps
        for d, guid, a, b in pieces:
            r = network.ribbons[guid]
            forward = b > a
            lo, hi = min(a, b), max(a, b)

            def metres(at):
                return d + abs(at - a) * r.length
            overrides = r.limits[forward]
            cuts = sorted({lo, hi} | {x for s, e, _ in overrides for x in (s, e) if lo < x < hi})
            stretches = []
            for x, y in zip(cuts, cuts[1:]):
                mid = (x + y) / 2
                limit = next((v for s, e, v in overrides if s <= mid <= e), r.limit)
                stretches.append((metres(x if forward else y), limit))
            for start, limit in stretches if forward else reversed(stretches):
                if not self.limits or self.limits[-1][1] != limit:
                    self.limits.append((start, limit))
            for at, name in r.signals[forward]:
                if lo <= at <= hi:
                    self.signals.append((metres(at), name))
            steps =max(1, math.ceil(abs(b - a) * r.length / 50))      # every 50 m or so, to draw the curves
            for k in range(steps + 1):
                at = a + (b - a) * k / steps
                xy = network.position(guid, at)
                self.points.append((metres(at), xy[0], xy[1]))
        self.start = pieces[0][0] if pieces else 0.0
        self.end = max([self.start] + [d + abs(b - a) * network.ribbons[g].length for d, g, a, b in pieces[-1:]])
        self.signals.sort()
        self.gaps = [(a, b) for a, b in gaps if b > self.start and a < self.end]
        self.stations = self._stations(path_platforms(network, pieces), stops)

    def _stations(self, platforms, stops):
        """[(from, to, station, stopping)]: platforms along the path (joined up per station), and the timetable's
        stops where no platform is marked."""
        out = []
        for m1, m2, name in sorted(platforms):
            station = tsw_stops.split_platform(name)[0]
            if out and out[-1][2] == station and m1 - out[-1][1] < 50:
                out[-1][1] = max(out[-1][1], m2)
            else:
                out.append([m1, m2, station, False])
        for m, name in stops:
            near = [s for s in out if s[0] - 60 <= m <= s[1] + 60]
            if near:
                near[0][3] = True
            else:
                out.append([m - 10, m, tsw_stops.split_platform(name)[0], True])
        for s in out:                       # where the service starts (no instruction of its own) and ends
            s[3] = s[3] or any(s[0] - 60 <= m <= s[1] + 60 for m in (self.start, self.end))
        return sorted(tuple(s) for s in out if self.start - 1 <= s[1] and s[0] <= self.end + 1)

    def limit_at(self, m):
        v = self.limits[0][1] if self.limits else DEFAULT_LIMIT
        for start, limit in self.limits:
            if start > m:
                break
            v = limit
        return v


def path_platforms(network, pieces):
    """[(from, to, name)] in metres along a path (TrackNetwork.follow): the platforms it runs past."""
    out = []
    for d, guid, a, b in pieces:
        r = network.ribbons[guid]
        lo, hi = min(a, b), max(a, b)
        for s, e, name in r.platforms:
            if e >= lo and s <= hi:
                out.append(tuple(sorted((d + abs(max(s, lo) - a) * r.length, d + abs(min(e, hi) - a) * r.length)))
                           + (name,))
    return out


# ---------------------------------------------------------------- timetables
def timetables(files):
    """[(route definition package, timetable file path)] for every timetable that has data tracks."""
    tracks = collections.defaultdict(set)
    for f in files.where:
        if f.endswith("DataTrack.uasset") and "/DataTracks/" in f:
            folder, name = f.rsplit("/DataTracks/", 1)
            tracks[folder].add(name)
    out = []
    for f in files.where:
        if not f.endswith(".uasset"):
            continue
        folder, name = f.rsplit("/", 1)
        name = name[:-len(".uasset")]
        if folder not in tracks or not any(t.startswith(name + "_") for t in tracks[folder]):
            continue
        try:
            pkg = tsw_paks.Package("/" + f[:-len(".uasset")], files.where[f].read(f), b"")
        except (ValueError, KeyError, IndexError):
            continue
        index = pkg.export(name)
        if not index or pkg.class_name(index) != "RouteTimetableDefinition":
            continue
        route = next((i["name"] for i in pkg.imports if i["class"] == "Package" and ROUTE_DEFINITION.match(i["name"])),
                     None)
        if route:
            out.append((route, f[:-len(".uasset")]))
    out.sort(key=lambda rt: ("/Scenarios/" in rt[1], rt[1]))        # the route's own timetables first
    return out


def route_details(files, definition):
    """(display name, map package) of a route from its RouteDefinition package."""
    pkg = files.package(definition)
    props = pkg.properties(pkg.export(definition.rsplit("/", 1)[-1]) or 1) if pkg else {}
    level = (props.get("Level") or [""])[0].split(".")[0]
    return str(props.get("DisplayName") or definition.split("/")[1]).strip(), level


def track_tiles(files, level):
    """The map files of a route that hold its track (the TT_ tiles, or any of its maps on routes laid out
    differently)."""
    path = files.file_of(level)
    if not path:
        return []
    folder = path.rsplit("/", 1)[0] + "/"
    maps = [f[:-len(".umap")] for f in files.where if f.startswith(folder) and f.endswith(".umap")]
    tiles = [m for m in maps if "/Tiles/TT_" in m]
    return sorted(tiles or maps)


@functools.lru_cache(maxsize=16)
def data_tracks(files, timetable):
    """The data track files of a timetable (looking through the game's files takes over a second)."""
    name = timetable.rsplit("/", 1)[-1]
    folder = timetable.rsplit("/", 1)[0] + "/DataTracks/" + name + "_"
    return sorted(f[:-len(".uasset")] for f in files.where if f.startswith(folder) and f.endswith("DataTrack.uasset"))


# what's read of a data track: the rest is over half of it, and a file can hold the rows of a hundred services
ROW_FIELDS = {"ServiceDataTracks": {"TrackData": {"Location": None, "Distance": None, "DataType": None,
                                                  "InstructionIndex": None}}}


def services_rows(files, timetable, services):
    """{service: its data track rows} for the services of a timetable found in its data tracks, each file read
    once."""
    out, wanted = {}, set(services)
    for track in data_tracks(files, timetable):
        if not wanted:
            break
        header = files.where[track + ".uasset"].read(track + ".uasset")
        if not any(tsw_stops.named_in(header, s) for s in wanted):
            continue
        data = dict(tsw_stops._load(files, track, ROW_FIELDS).get("ServiceDataTracks") or [])
        for s in list(wanted):
            if s in data:
                out[s] = data[s].get("TrackData") or []
                wanted.discard(s)
    return out


def service_rows(files, timetable, service):
    """The data track rows of a service, or None."""
    return services_rows(files, timetable, [service]).get(service)


@functools.lru_cache(maxsize=8)
def service_table(files, timetable):
    """{service name: Service} for the services in a timetable file."""
    out = {}
    for s in tsw_stops._load(files, timetable, {"Services": tsw_stops.SERVICE_FIELDS}).get("Services") or []:
        instructions = tuple(
            (str(ins.get("InstructionType", "")).split("::")[-1], bool(ins.get("bIsStopping")),
             (ins.get("Destination") or {}).get("Name") or "", ())
            for ins in s.get("Instructions") or [])
        out[s.get("Name")] = Service(s.get("Name"), str(s.get("ServiceNumber") or ""),
                                     bool(s.get("bIsPlayerDrivable")), str(s.get("MapPointA") or ""),
                                     str(s.get("MapPointB") or ""), instructions)
    return out


Service = collections.namedtuple("Service", "name number drivable origin destination instructions")


def journey(service):
    """The named places a service stops at after leaving, in order."""
    return tuple(tsw_stops.split_platform(dest)[0] for kind, stopping, dest, _ in service.instructions
                 if kind == "GoTo" and stopping and dest and dest != "None")


def stops_along(rows, instructions):
    """[(metres, destination)] of the timetable's stops, from the data track's action points."""
    out = []
    for row in rows:
        i = row.get("InstructionIndex", -1)
        if str(row.get("DataType", "")).endswith("ActionPoint") and 0 <= i < len(instructions):
            kind, stopping, dest, _ = instructions[i]
            if kind == "GoTo" and stopping and dest and dest != "None":
                out.append((row.get("Distance", 0.0) / 100, dest))
    return out


# ---------------------------------------------------------------- drawing
BANDS_MPH = ((25, "#D2502A", "25 or less"), (45, "#E3A11B", "30-45"), (65, "#2F9E77", "50-65"),
             (999, "#5F5E5A", "70 and over"))
BANDS_KMH = ((40, "#D2502A", "40 or less"), (70, "#E3A11B", "50-70"), (100, "#2F9E77", "80-100"),
             (999, "#5F5E5A", "110 and over"))


def imperial_limits(limits):
    """Whether a route's limits are round numbers in mph rather than km/h."""
    values = [v for _, v in limits if abs(v - DEFAULT_LIMIT) > 0.01]
    mph = sum(abs(v / MPH - 5 * round(v / MPH / 5)) < 0.05 for v in values)
    kmh = sum(abs(v * 3.6 - 5 * round(v * 3.6 / 5)) < 0.05 for v in values)
    return mph >= kmh


class Units:
    def __init__(self, imperial):
        self.imperial = imperial
        self.unit, self.length = ("mi", MILE) if imperial else ("km", 1000.0)
        self.speed_unit = "mph" if imperial else "km/h"
        self.row = 3 if imperial else 5                     # miles / km per strip
        self.minor = 0.25 if imperial else 0.5
        self.grid = 25 if imperial else 40
        self.bands = BANDS_MPH if imperial else BANDS_KMH
        self.slow, self.drop = (45, 20) if imperial else (70, 30)      # where to slow down: see slow_stretches

    def speed(self, v):
        return round(v / MPH) if self.imperial else round(v * 3.6)

    def colour(self, v):
        return next(c for top, c, _ in self.bands if self.speed(v) <= top)


def esc(text):
    return html.escape(str(text), quote=True)


def legend(units, extra, more=""):
    """The colour key, the swatches drawn as lines: a browser leaves background colours out of a print. `more`:
    other swatches after it."""
    swatches = "".join(f'<span><svg class="swatch" viewBox="0 0 24 6" preserveAspectRatio="none"><line x1="0" x2="24" '
                       f'y1="3" y2="3" stroke="{c}" stroke-width="6"/></svg>{esc(label)}</span>'
                       for _, c, label in units.bands)
    return f'<div class="legend">{swatches}<span>{esc(extra)}</span>{more}</div>'


def text_width(text, size):
    return len(str(text)) * size * 0.56


class Strip:
    """One row of the line diagram: speed profile, limit boards, platforms, signals and distances along a stretch
    of the path."""
    X0, X1 = 46.0, 992.0
    LANE, BOARD_H = 18.0, 14.0

    def __init__(self, prof, units, start, a, b, vmax):
        self.p, self.u, self.start, self.a, self.b, self.vmax = prof, units, start, a, b, vmax
        self.scale = (self.X1 - self.X0) / (units.row * units.length)
        self.boards = self._boards()
        lanes = max([lane for _, _, _, lane, _ in self.boards] + [0]) + 1
        self.track_y = 54 + lanes * self.LANE + 10
        self.height = self.track_y + 62

    def x(self, m):
        return self.X0 + (m - self.a) * self.scale

    def _boards(self):
        """[(x, left, text, lane, carried over)]: a board where each new limit starts, in lanes so they don't
        overlap; the first one is the limit carried over from the row before."""
        out, lane_end = [], []
        changes = [(self.a, self.p.limit_at(self.a), self.a > self.start + 1)] + \
                  [(m, v, False) for m, v in self.p.limits if self.a < m < self.b]
        for m, v, carried in changes:
            text = str(self.u.speed(v))
            w = text_width(text, 10.5) + 9
            x = self.x(m)
            left = max(self.X0 - 4, x - w / 2) if m > self.a else self.X0 - 4
            lane = next((k for k, end in enumerate(lane_end) if end + 3 <= left), len(lane_end))
            if lane == len(lane_end):
                lane_end.append(0)
            lane_end[lane] = left + w
            out.append((x, left, text, lane, carried))
        return out

    def svg(self):
        u, p, ty = self.u, self.p, self.track_y
        o = [f'<svg viewBox="0 0 1000 {self.height:.0f}" class="strip">']
        top, bottom = 6.0, 48.0

        def y(v):
            return bottom - min(u.speed(v), self.vmax) / self.vmax * (bottom - top)
        for g in range(u.grid, self.vmax + 1, u.grid):
            gy = bottom - g / self.vmax * (bottom - top)
            o.append(f'<line x1="{self.X0}" x2="{self.X1}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>'
                     f'<text x="{self.X0 - 5}" y="{gy + 3:.1f}" class="axis" text-anchor="end">{g}</text>')
        o.append(f'<line x1="{self.X0}" x2="{self.X1}" y1="{bottom}" y2="{bottom}" class="base"/>')
        # profile: the limit at each point as a stepped line
        stretches = [(self.a, p.limit_at(self.a))] + [(m, v) for m, v in p.limits if self.a < m < self.b]
        for (m, v), nxt in zip(stretches, stretches[1:] + [(min(self.b, p.end), None)]):
            x1, x2 = self.x(m), self.x(nxt[0])
            o.append(f'<rect x="{x1:.1f}" y="{y(v):.1f}" width="{max(x2 - x1, 0):.1f}" '
                     f'height="{bottom - y(v):.1f}" class="fill"/>'
                     f'<line x1="{x1:.1f}" x2="{x2:.1f}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
                     f'stroke="{u.colour(v)}" class="step"/>')
            if nxt[1] is not None:
                o.append(f'<line x1="{x2:.1f}" x2="{x2:.1f}" y1="{y(v):.1f}" y2="{y(nxt[1]):.1f}" class="riser"/>')
        # boards, each on a post down to the track
        for x, left, text, lane, carried in self.boards:
            bt = 54 + lane * self.LANE
            w = text_width(text, 10.5) + 9
            if not carried:
                x = max(x, left + 1)
                o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{bt + self.BOARD_H:.1f}" y2="{ty:.1f}" class="post"/>')
                if abs(left + w / 2 - x) > 1:
                    o.append(f'<line x1="{left + w / 2:.1f}" x2="{x:.1f}" y1="{bt + self.BOARD_H:.1f}" '
                             f'y2="{bt + self.BOARD_H + 3:.1f}" class="post"/>')
            o.append(f'<rect x="{left:.1f}" y="{bt:.1f}" width="{w:.1f}" height="{self.BOARD_H}" rx="1.5" '
                     f'class="{"board carried" if carried else "board"}"/>'
                     f'<text x="{left + w / 2:.1f}" y="{bt + 10.8:.1f}" text-anchor="middle" '
                     f'class="{"boardtext carried" if carried else "boardtext"}">{text}</text>')
        # track, gaps the service couldn't be followed over, platforms
        end_x = self.x(min(self.b, p.end))
        o.append(f'<line x1="{self.X0}" x2="{end_x:.1f}" y1="{ty}" y2="{ty}" class="track"/>')
        for g1, g2 in p.gaps:
            if g2 > self.a and g1 < self.b:
                o.append(f'<line x1="{self.x(max(g1, self.a)):.1f}" x2="{self.x(min(g2, self.b)):.1f}" '
                         f'y1="{ty}" y2="{ty}" class="gap"/>')
        for m1, m2, name, stopping in p.stations:
            if m2 < self.a or m1 > self.b:
                continue
            x1, x2 = self.x(max(m1, self.a)), self.x(min(m2, self.b))
            o.append(f'<rect x="{x1:.1f}" y="{ty - 8}" width="{max(x2 - x1, 2):.1f}" height="5" class="platform"/>')
            if self.a <= (m1 + m2) / 2 <= self.b:
                half = text_width(name, 10.5) / 2
                cx = min(max((x1 + x2) / 2, half + 2), 1000 - half - 2)
                o.append(f'<text x="{cx:.1f}" y="{ty + 40}" text-anchor="middle" '
                         f'class="{"station stop" if stopping else "station"}">{esc(name)}</text>')
        # signals facing this way
        last_x, row = -99.0, 0
        for m, name in p.signals:
            if not self.a <= m <= self.b:
                continue
            x = self.x(m)
            row = 1 - row if x - last_x < text_width(name, 8) + 4 else 0
            last_x = x
            o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ty + 2}" y2="{ty + 10}" class="signalpost"/>'
                     f'<circle cx="{x:.1f}" cy="{ty + 13}" r="2.8" class="signal"/>'
                     f'<text x="{x:.1f}" y="{ty + 23 + row * 8}" text-anchor="middle" class="signalid">'
                     f'{esc(name)}</text>')
        # distances from the start
        ay = ty + 47
        o.append(f'<line x1="{self.X0}" x2="{self.X1}" y1="{ay}" y2="{ay}" class="base"/>')
        k = 0
        while True:
            m = self.a + k * u.minor * u.length
            if m > self.b + 1:
                break
            major = abs((k * u.minor) % 1) < 1e-6
            x = self.x(m)
            o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ay}" y2="{ay + (6 if major else 3)}" class="base"/>')
            if major:
                o.append(f'<text x="{x:.1f}" y="{ay + 14}" text-anchor="middle" class="axis">'
                         f'{(m - self.start) / u.length:.0f} {u.unit}</text>')
            k += 1
        o.append('</svg>')
        return "".join(o)


def overview_map(prof, units, width=520.0, height=470.0):
    pts = prof.points
    if len(pts) < 2:
        return ""
    xs, ys = [p[1] for p in pts], [p[2] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    pad = 70.0
    scale = min((width - 2 * pad) / max(x1 - x0, 1), (height - 2 * pad) / max(y1 - y0, 1))
    ox = (width - (x1 - x0) * scale) / 2
    oy = (height - (y1 - y0) * scale) / 2

    def at(x, y):
        return ox + (x - x0) * scale, oy + (y - y0) * scale
    o = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" class="overview">']
    run, colour = [], None
    for m, x, y in pts:
        c = units.colour(prof.limit_at(m))
        q = at(x, y)
        if c != colour and run:
            o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in run + [q])}" stroke="{colour}" '
                     'class="line"/>')
            run = []
        colour = c
        if not run or abs(q[0] - run[-1][0]) + abs(q[1] - run[-1][1]) > 1.5:
            run.append(q)
    if len(run) > 1:
        o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in run)}" stroke="{colour}" '
                 'class="line"/>')
    # stations, labels placed where they don't overlap
    taken = [(a - 2, b - 2, a + 2, b + 2) for _, x, y in pts[::3] for a, b in [at(x, y)]]   # the line itself

    def free(box):
        return box[0] > 4 and box[2] < width - 4 and box[1] > 4 and box[3] < height - 30 and \
            all(box[2] < t[0] or box[0] > t[2] or box[3] < t[1] or box[1] > t[3] for t in taken)
    places = []
    for m1, m2, name, stopping in prof.stations:
        mid = (m1 + m2) / 2
        p = min(pts, key=lambda q: abs(q[0] - mid))
        sx, sy = at(p[1], p[2])
        places.append((sx, sy, name, stopping))
        taken.append((sx - 5, sy - 5, sx + 5, sy + 5))
    for sx, sy, name, stopping in places:
        w, h = text_width(name, 11), 11
        tries = [(9, 4, "start"), (-9, 4, "end"), (0, -9, "middle"), (0, 17, "middle"), (9, -6, "start"),
                 (-9, -6, "end"), (9, 14, "start"), (-9, 14, "end"), (14, -14, "start"), (-14, -14, "end"),
                 (14, 22, "start"), (-14, 22, "end")]
        for dx, dy, anchor in tries + tries[:1]:
            left = sx + dx - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
            box = (left, sy + dy - h + 2, left + w, sy + dy + 2)
            if free(box):
                break
        taken.append(box)
        o.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{4 if stopping else 3}" '
                 f'class="{"dot stop" if stopping else "dot"}"/>'
                 f'<text x="{sx + dx:.1f}" y="{sy + dy:.1f}" text-anchor="{anchor}" '
                 f'class="{"maplabel stop" if stopping else "maplabel"}">{esc(name)}</text>')
    # scale bar and north
    span = (width - 2 * pad) / scale / units.length
    bar = next(s for s in (0.25, 0.5, 1, 2, 5, 10, 20, 50, 100) if s >= span / 6)
    bw = bar * units.length * scale
    o.append(f'<line x1="20" x2="{20 + bw:.1f}" y1="{height - 18}" y2="{height - 18}" class="scalebar"/>'
             f'<text x="{20 + bw / 2:.1f}" y="{height - 24}" text-anchor="middle" class="axis">'
             f'{bar:g} {units.unit}</text>'
             f'<path d="M{width - 22} {height - 40} l7 18 h-14 z" class="north"/>'
             f'<text x="{width - 22}" y="{height - 8}" text-anchor="middle" class="axis">N</text></svg>')
    return "".join(o)


def where(prof, units, m):
    for m1, m2, name, _ in prof.stations:
        if m1 - 60 <= m <= m2 + 60:
            return f"at {name}"
    before = [s for s in prof.stations if s[1] < m]
    after = [s for s in prof.stations if s[0] > m]
    near = 0.4 * units.length
    if after and after[0][0] - m < near:
        return f"approaching {after[0][2]}"
    if before and m - before[-1][1] < near:
        return f"leaving {before[-1][2]}"
    if before and after:
        return f"{before[-1][2]} - {after[0][2]}"
    return ""


CSS = """
@page { size: A4 landscape; margin: 0; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; background: #d9d9d6; font-family: "Segoe UI", Arial, sans-serif; color: #111; }
.page { width: 297mm; height: 210mm; padding: 9mm 10mm; margin: 6mm auto; background: #fff; position: relative;
        overflow: hidden; break-after: page; display: flex; flex-direction: column; }
.page:last-child { break-after: auto; }
@media print { body { background: none; } .page { margin: 0; } }
.head { display: flex; justify-content: space-between; align-items: baseline; font-size: 9pt; color: #444;
        border-bottom: 0.3mm solid #111; padding-bottom: 1.5mm; margin-bottom: 3mm; }
.head b { color: #111; font-size: 11pt; }
.foot { position: absolute; bottom: 4mm; left: 10mm; right: 10mm; font-size: 7pt; color: #777;
        display: flex; justify-content: space-between; }
h1 { font-size: 20pt; font-weight: 600; margin: 0 0 1mm; }
.sub { font-size: 10pt; color: #444; margin-bottom: 3mm; }
.cols { display: flex; gap: 8mm; flex: 1; min-height: 0; }
.left { flex: 0 0 148mm; display: flex; flex-direction: column; }
.right { flex: 1; min-width: 0; }
.overview { width: 148mm; height: 134mm; border: 0.2mm solid #bbb; }
.note { font-size: 8pt; color: #333; line-height: 1.45; margin-top: 2mm; }
.legend { display: flex; flex-wrap: wrap; gap: 2mm 5mm; font-size: 8pt; color: #333; margin-top: 2mm;
          align-items: center; }
.legend span { display: inline-flex; align-items: center; gap: 1.5mm; }
.swatch { width: 6mm; height: 1.6mm; display: inline-block; }
table.card { border-collapse: collapse; width: 100%; font-size: 8.6pt; }
table.card th { text-align: left; font-weight: 600; border-bottom: 0.3mm solid #111; padding: 0.8mm 2mm; }
table.card td { padding: 0.55mm 2mm; border-bottom: 0.1mm solid #ddd; }
table.card td.num { text-align: right; font-variant-numeric: tabular-nums; width: 15mm; }
table.card td.lim { text-align: center; width: 14mm; }
table.card td.lim b { display: inline-block; min-width: 9mm; border: 0.3mm solid #111; border-radius: 0.6mm;
                      padding: 0 1mm; }
table.card tr.stop td { color: #555; font-style: italic; }
.cardcols { display: flex; gap: 6mm; }
.cardcols > div { flex: 1; min-width: 0; }
.strips { display: flex; flex-direction: column; gap: 2mm; }
.strip { width: 100%; display: block; }
.strip text, .overview text { font-family: "Segoe UI", Arial, sans-serif; }
.grid { stroke: #e2e2e2; stroke-width: 0.6; }
.base { stroke: #888; stroke-width: 0.7; }
.axis { font-size: 8px; fill: #555; }
.fill { fill: #f1f1ef; }
.step { stroke-width: 2.2; stroke-linecap: square; }
.riser { stroke: #999; stroke-width: 0.8; }
.post { stroke: #555; stroke-width: 0.8; }
.board { fill: #fff; stroke: #111; stroke-width: 1.2; }
.board.carried { stroke: #999; stroke-dasharray: 2 1.5; }
.boardtext { font-size: 10.5px; font-weight: 700; fill: #111; }
.boardtext.carried { fill: #888; font-weight: 600; }
.track { stroke: #111; stroke-width: 2.6; }
.gap { stroke: #fff; stroke-width: 1.4; stroke-dasharray: 3 3; }
.platform { fill: #8d8d8a; }
.station { font-size: 10.5px; fill: #333; }
.station.stop { font-weight: 700; fill: #111; }
.signalpost { stroke: #111; stroke-width: 0.9; }
.signal { fill: #111; }
.signalid { font-size: 8px; fill: #444; }
.line { fill: none; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; }
.dot { fill: #fff; stroke: #111; stroke-width: 1.4; }
.dot.stop { fill: #111; }
.maplabel { font-size: 11px; fill: #333; }
.maplabel.stop { font-weight: 700; fill: #111; }
.scalebar { stroke: #111; stroke-width: 2.5; }
.north { fill: #111; }
"""


def render(info, prof, units):
    """The whole printable document."""
    title = f'{esc(info["service"])} &nbsp;{esc(info["origin"])} → {esc(info["destination"])}'
    head_left = f'<b>{esc(info["service"])}</b> &nbsp;{esc(info["origin"])} → {esc(info["destination"])}'
    route_line = f'{esc(info["route"])} · passenger speed limits in {units.speed_unit}'
    made = datetime.date.today().isoformat()
    length = (prof.end - prof.start) / units.length

    # route card: every change of limit, and the stops
    card = []
    events = [(m, 0, v) for m, v in prof.limits] + [(m1, 1, s) for m1, m2, s, stopping in prof.stations if stopping]
    for m, kind, value in sorted(events, key=lambda e: (e[0], e[1])):
        d = f"{(m - prof.start) / units.length:.2f}"
        if kind == 0:
            card.append(f'<tr><td class="num">{d}</td><td class="lim"><b>{units.speed(value)}</b></td>'
                        f'<td>{esc(where(prof, units, m))}</td></tr>')
        else:
            card.append(f'<tr class="stop"><td class="num">{d}</td><td class="lim"></td>'
                        f'<td>stop: {esc(value)}</td></tr>')
    table_head = (f'<table class="card"><tr><th style="text-align:right">{units.unit}</th>'
                  f'<th style="text-align:center">{units.speed_unit}</th><th>where</th></tr>')
    first_rows, per_column = 36, 42
    pages = []

    gaps = ""
    if prof.gaps:
        gaps = (" Short stretches the files couldn't follow the service over (the track is dashed there): " +
                ", ".join(f"{(a - prof.start) / units.length:.2f}-{(b - prof.start) / units.length:.2f}"
                          for a, b in prof.gaps) + f" {units.unit}.")
    also = ""
    if info.get("also"):
        also = f'<div class="note">Same stops: {esc(", ".join(info["also"]))}.</div>'
    pages.append(
        f'<h1>{title}</h1><div class="sub">{route_line} · {length:.1f} {units.unit} · '
        f'{sum(1 for s in prof.stations if s[3])} stops · {esc(info["timetable"])}</div>'
        f'<div class="cols"><div class="left">{overview_map(prof, units)}'
        f'{legend(units, "bold: stops")}'
        f'<div class="note">Limits for passenger trains, from the route files. A board marks where the front of '
        f'the train meets the new limit; where the limit rises, keep to the lower one until the whole train is past. '
        f'Distances are from the start of the service. Signals shown are the ones facing this way.{gaps}</div>'
        f'{also}</div>'
        f'<div class="right">{table_head}{"".join(card[:first_rows])}</table></div></div>')
    rest = card[first_rows:]
    while rest:
        cols = [rest[i:i + per_column] for i in range(0, min(len(rest), 3 * per_column), per_column)]
        rest = rest[3 * per_column:]
        pages.append('<div class="cardcols">' + "".join(f'<div>{table_head}{"".join(c)}</table></div>'
                                                        for c in cols) + '</div>')

    # line diagram, a few strips a page
    vmax = units.grid * math.ceil(max(units.speed(v) for _, v in prof.limits) / units.grid)
    step = units.row * units.length
    strips = [Strip(prof, units, prof.start, prof.start + k * step, prof.start + (k + 1) * step, vmax)
              for k in range(max(1, math.ceil((prof.end - prof.start) / step - 1e-6)))]
    room, page, used = 650.0, [], 0.0           # room under a page's header, in strip units (1000 = 277 mm)
    for s in strips:
        if page and used + s.height > room:
            pages.append('<div class="strips">' + "".join(page) + '</div>')
            page, used = [], 0.0
        page.append(s.svg())
        used += s.height + 7.5
    if page:
        pages.append('<div class="strips">' + "".join(page) + '</div>')

    out = [f'<!doctype html><html><head><meta charset="utf-8"><title>{esc(info["service"])} '
           f'{esc(info["origin"])} - {esc(info["destination"])} speed limits</title><style>{CSS}</style></head><body>']
    for n, body in enumerate(pages, 1):
        out.append(f'<section class="page"><div class="head"><span>{head_left}</span>'
                   f'<span>{route_line} · page {n} of {len(pages)}</span></div>{body}'
                   f'<div class="foot"><span>Made from the Train Sim World 7 files on {made} (tsw_routemap.py)'
                   f'</span><span>{esc(info["service"])} · {n}/{len(pages)}</span></div></section>')
    out.append('</body></html>')
    return "".join(out)


# ---------------------------------------------------------------- the whole route
STEP = 10.0             # metres between the points the track is looked at
ABREAST = 10.0          # metres across: track this close alongside (either way) is drawn as one line of route
WIDER = 30.0            # metres across: how far to look for track running the other way where there's none closer
SHORTEST = 400.0        # metres: stretches left over shorter than this aren't drawn (crossovers, platform roads)
SHORT_RISE = 40.0       # metres: a higher limit shorter than this (than a two-car train) can't be used
SAME_SPOT = 400.0       # metres: where to slow down for the same limit this close (on any sections), one board
LONG_RUN = 2400.0       # metres: a stretch this long has its limit written beside the line on the map

Sample = collections.namedtuple("Sample", "m x y hx hy key")    # a point of a path: metres, place, heading, bit


class Coverage:
    """The track a route's journeys run over, in bits of up to STEP metres per direction of travel, and how
    many journeys use each; found by place."""
    CELL = 25.0

    def __init__(self, network):
        self.network = network
        self.bits = {}                      # (ribbon, forward, k) -> [x, y, heading x, heading y, at, journeys]
        self.cells = collections.defaultdict(list)

    def key(self, guid, forward, at):
        count = max(1, math.ceil(self.network.ribbons[guid].length / STEP))
        return guid, forward, min(int(at * count), count - 1)

    def samples(self, pieces, start=-math.inf, end=math.inf):
        """A path (TrackNetwork.follow) from `start` to `end` metres as Samples every STEP metres or less, counted
        in as one journey."""
        out, network = [], self.network
        for d, guid, a, b in pieces:
            length = abs(b - a) * network.ribbons[guid].length
            n = max(1, math.ceil(length / STEP))
            sign = 1 if b > a else -1
            for k in range(n):
                f = (k + 0.5) / n
                if not start <= d + length * f <= end:
                    continue
                at = a + (b - a) * f
                x, y = network.position(guid, at)
                hx, hy = network.heading(guid, at)
                out.append(Sample(d + length * f, x, y, sign * hx, sign * hy, self.key(guid, b > a, at)))
        for key in {s.key for s in out}:
            bit = self.bits.get(key)
            if bit is None:
                guid, forward, k = key
                at = (k + 0.5) / max(1, math.ceil(network.ribbons[guid].length / STEP))
                x, y = network.position(guid, at)
                hx, hy = network.heading(guid, at)
                sign = 1 if forward else -1
                bit = self.bits[key] = [x, y, sign * hx, sign * hy, at, 0]
                self.cells[(int(x // self.CELL), int(y // self.CELL))].append(key)
            bit[5] += 1
        return out

    def abreast(self, s, reach=ABREAST):
        """[(bit, same way, metres across, metres ahead)]: the bits alongside a sample (up to `reach` metres
        across), running its way or the other."""
        out = []
        cx, cy = int(s.x // self.CELL), int(s.y // self.CELL)
        cells = range(-math.ceil(reach / self.CELL), math.ceil(reach / self.CELL) + 1)
        for i in cells:
            for j in cells:
                for key in self.cells.get((cx + i, cy + j), ()):
                    x, y, hx, hy, _, _ = self.bits[key]
                    dx, dy = x - s.x, y - s.y
                    ahead, across = dx * s.hx + dy * s.hy, s.hx * dy - s.hy * dx
                    if abs(ahead) > 0.6 * STEP or abs(across) > reach:
                        continue
                    dot = hx * s.hx + hy * s.hy
                    if abs(dot) > 0.85:
                        out.append((key, dot > 0, across, ahead))
        return out


def breaks(path, i):
    """Whether a path doesn't run on smoothly from sample i - 1 to i: it reverses, or it wasn't followed."""
    a, b = path[i - 1], path[i]
    return a.hx * b.hx + a.hy * b.hy < 0 or math.hypot(b.x - a.x, b.y - a.y) > 3 * STEP or b.m - a.m > 3 * STEP


def station_name(platform):
    """A platform's station as the map names it: 'Coryton Station Platform 1' -> 'Coryton', 'Westbourne Park
    Westbound' -> 'Westbourne Park'."""
    name = tsw_stops.split_platform(platform)[0]
    name = re.sub(r"\s+(P\d+[a-z]?|Pl|Plat|Platform|Gleis|Track)\.?$", "", name, flags=re.I)
    name = re.sub(r"\s+(Eastbound|Westbound|Northbound|Southbound)$", "", name)
    return re.sub(r"\s+Station$", "", name)


def plain(name):
    """A name to compare spellings by: 'Schlüchtern' and 'Schluechtern' are the same."""
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")):
        name = name.replace(a, b)
    return name.casefold()


def numbered_twins(named, near):
    """[(name, where)] with names like 'Stafford 1' and 'Stafford 3' (the platforms of one station, named apart)
    as 'Stafford' where `near(where, where)` says they're close."""
    def stem(name):
        m = re.match(r"^(.*\D)\s+\d+$", name)
        return m.group(1) if m else None
    out = []
    for name, at in named:
        base = stem(name)
        if base and any(other != name and base in (other, stem(other)) and near(at, there) for other, there in named):
            name = base
        out.append((name, at))
    return out


class Section:
    """A stretch of the route drawn as one line, with the limits both ways: forward (True) is the way the
    journey it was found on runs, from x = 0 to its length. Distances x are in metres from its start."""

    def __init__(self, number, network, coverage, path, i0, i1, drawn):
        self.number = number
        self.length = path[i1].m - path[i0].m
        self.links = []                                 # [(x, other section, x on that one)] where they meet
        self.joins = [None, None]                       # (section, x on it) this one starts / ends on
        if i0 > 0 and not breaks(path, i0) and path[i0 - 1].key in drawn:
            self.joins[0] = drawn[path[i0 - 1].key]
        if i1 + 1 < len(path) and not breaks(path, i1 + 1) and path[i1 + 1].key in drawn:
            self.joins[1] = drawn[path[i1 + 1].key]
        found = {True: [], False: []}                   # per sample: (x, [(bit, metres ahead)] busiest first)
        self.points = []
        for i in range(i0, i1 + 1):
            s, x = path[i], path[i].m - path[i0].m
            near = coverage.abreast(s)
            for way in (True, False):
                here = [n for n in near if n[1] == way]
                if way and not any(n[0] == s.key for n in here):
                    here.append((s.key, True, 0.0, 0.0))
                if not here:                            # the lines spread apart here and there
                    here = [n for n in coverage.abreast(s, WIDER) if n[1] == way]
                    near += here
                here.sort(key=lambda n: (-coverage.bits[n[0]][5], abs(n[2])))
                found[way].append((x, [(n[0], n[3]) for n in here]))
            for key, _, _, _ in near:
                drawn.setdefault(key, (number, x))
            drawn.setdefault(s.key, (number, x))
            if (i - i0) % 5 == 0 or i == i1:
                self.points.append((x, s.x, s.y))
        self.limits, self.signals, self.platforms = {}, {}, {}
        for way in (True, False):
            self._read(network, coverage, way, self._follow(network, found[way]))
        both = numbered_twins([(n, (a, b)) for a, b, n in self.platforms[True] + self.platforms[False]],
                              lambda p, q: abs(p[0] + p[1] - q[0] - q[1]) / 2 < 400)
        self.stations = self._join(sorted((a, b, n) for n, (a, b) in both), 400)

    @staticmethod
    def _follow(network, found):
        """[(x, bit, metres ahead)]: one track along the samples, as a train could run: from each the busiest
        track that leads on from the one before, and only where none does the busiest there is (unless the track
        before comes back within a few samples: the ones between are left out). (x, None, 0) where there's none."""
        out, last = [], None
        i = 0
        while i < len(found):
            x, here = found[i]
            if not here:
                out.append((x, None, 0.0))
                last, i = None, i + 1
                continue
            pick = here[0]
            if last is not None:
                on = [n for n in here if network.leads_on(last, n[0][0])]
                if on:
                    pick = on[0]
                else:
                    back = next((j for j in range(i + 1, min(i + 4, len(found)))
                                 if any(network.leads_on(last, n[0][0]) for n in found[j][1])), None)
                    if back is not None:
                        i = back
                        continue
            out.append((x, pick[0], pick[1]))
            last, i = pick[0][0], i + 1
        return out

    def _read(self, network, coverage, way, chosen):
        """The limits, signals and platforms one way, from the track chosen alongside each sample."""
        runs = []                                       # [[ribbon, forward, [(x, at abreast)]]] or [None, ...]
        for x, key, ahead in chosen:
            if key is None:
                guid, forward, at = None, None, None
            else:
                guid, forward, _ = key
                r = network.ribbons[guid]
                rate = (1 if forward else -1) * (1 if way else -1) / max(r.length, 0.01)   # at per metre of x
                at = coverage.bits[key][4] - ahead * rate
            if not runs or runs[-1][0] != guid or runs[-1][1] != forward:
                runs.append([guid, forward, []])
            runs[-1][2].append((x, at))
        limits, signals, platforms = [], [], []
        for k, (guid, forward, points) in enumerate(runs):
            x0 = 0.0 if k == 0 else (runs[k - 1][2][-1][0] + points[0][0]) / 2
            x1 = self.length if k == len(runs) - 1 else (points[-1][0] + runs[k + 1][2][0][0]) / 2
            if guid is None:
                limits.append((x0, None))
                continue
            r = network.ribbons[guid]
            (xa, ata), (xb, atb) = points[0], points[-1]
            rate = (atb - ata) / (xb - xa) if xb - xa > 1 else \
                (1 if forward else -1) * (1 if way else -1) / max(r.length, 0.01)

            def at_x(x):
                return ata + (x - xa) * rate

            def x_at(at):
                return xa + (at - ata) / rate if rate else xa
            lo, hi = sorted((at_x(x0), at_x(x1)))
            cuts = sorted({lo, hi} | {c for s, e, _ in r.limits[forward] for c in (s, e) if lo < c < hi})
            pieces = []
            for a, b in zip(cuts, cuts[1:]):
                mid = min(max((a + b) / 2, 0.0), 1.0)       # the stretch may reach a few metres past the ribbon
                v = next((v for s, e, v in r.limits[forward] if s <= mid <= e), r.limit)
                pieces.append((min(x_at(a), x_at(b)), v))
            for x, v in sorted(pieces):
                limits.append((max(x, x0), v))
            for at, name in r.signals[forward]:
                if lo <= at <= hi:
                    signals.append((x_at(at), name))
            for s, e, name in r.platforms:
                if e >= lo and s <= hi:
                    a, b = sorted((x_at(max(s, lo)), x_at(min(e, hi))))
                    platforms.append((a, b, station_name(name)))
        self.limits[way] = usable_limits(limits, way, self.length)
        self.signals[way] = sorted(set(signals))
        self.platforms[way] = self._join(sorted(platforms))

    @staticmethod
    def _join(platforms, apart=50):
        """Platforms of one station side by side or end to end (or up to `apart` metres apart, like staggered
        ones) as one: [(from, to, station)]."""
        out = []
        for a, b, name in platforms:
            if out and out[-1][2] == name and a - out[-1][1] < apart:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b, name])
        return [tuple(p) for p in out]

    def flip(self):
        """Turns it round: distances from the other end, the two ways swapped."""
        length = self.length

        def back(limits):
            ends = [x for x, _ in limits[1:]] + [length]
            return [(length - end, v) for (_, v), end in reversed(list(zip(limits, ends)))]
        self.limits = {True: back(self.limits[False]), False: back(self.limits[True])}
        self.signals = {True: sorted((length - x, n) for x, n in self.signals[False]),
                        False: sorted((length - x, n) for x, n in self.signals[True])}
        self.platforms = {True: sorted((length - b, length - a, n) for a, b, n in self.platforms[False]),
                          False: sorted((length - b, length - a, n) for a, b, n in self.platforms[True])}
        self.stations = sorted((length - b, length - a, n) for a, b, n in self.stations)
        self.points = [(length - x, px, py) for x, px, py in reversed(self.points)]
        self.joins.reverse()

    def limit_at(self, way, x):
        v = self.limits[way][0][1] if self.limits[way] else None
        for start, limit in self.limits[way]:
            if start > x:
                break
            v = limit
        return v

    def stretches(self, way):
        """[(from, to, limit)] one way."""
        limits = self.limits[way]
        return [(x, nx, v) for (x, v), (nx, _) in zip(limits, limits[1:] + [(self.length, None)]) if nx > x]

    def lower_limits(self):
        """[(from, to, limit)]: the lower limit of the two ways."""
        cuts = sorted({0.0} | {x for way in (True, False) for x, _ in self.limits[way] if 0 < x < self.length})
        out = []
        for x, nx in zip(cuts, cuts[1:] + [self.length]):
            vs = [v for v in (self.limit_at(True, x), self.limit_at(False, x)) if v is not None]
            v = min(vs) if vs else None
            if out and out[-1][2] == v:
                out[-1] = (out[-1][0], nx, v)
            else:
                out.append((x, nx, v))
        return out

    def place(self, x):
        """Where (in the route's coordinates) the line is x metres along."""
        pts = self.points
        k = min(max(bisect.bisect_left(pts, (x,)), 1), len(pts) - 1)
        (xa, pa, qa), (xb, pb, qb) = pts[k - 1], pts[k]
        f = min(max((x - xa) / (xb - xa), 0.0), 1.0) if xb > xa else 0.0
        return pa + (pb - pa) * f, qa + (qb - qa) * f

    def end_names(self):
        """(first, last): the stations at its ends, or None where it starts or ends away from one."""
        first = self.stations[0][2] if self.stations and self.stations[0][0] < 300 else None
        last = self.stations[-1][2] if self.stations and self.stations[-1][1] > self.length - 300 else None
        return first, last


def usable_limits(limits, way, length):
    """[(x, limit)] of a stretch `length` long without the rises too short for any train to use (its back is still
    on the lower limit before), and with no limit given twice in a row. `way`: whether trains run up x."""
    stretches = [[x, nx, v] for (x, v), (nx, _) in zip(limits, limits[1:] + [(length, None)]) if nx > x]
    kept = []
    for s in stretches if way else reversed(stretches):
        if kept and s[1] - s[0] < SHORT_RISE and None not in (s[2], kept[-1][2]) and s[2] > kept[-1][2]:
            kept[-1][0], kept[-1][1] = min(kept[-1][0], s[0]), max(kept[-1][1], s[1])
        else:
            kept.append(s)
    out = []
    for x, _, v in sorted(kept):
        if not out or out[-1][1] != v:
            out.append((x, v))
    return out


def slow_stretches(stretches, units):
    """The stretches [(from, to, limit)] to slow down for: those at units.slow or less, or units.drop or more
    below the stretch before or after."""
    out = []
    for k, (x1, x2, v) in enumerate(stretches):
        if v is None:
            continue
        near = [units.speed(n[2]) for n in stretches[max(k - 1, 0):k] + stretches[k + 1:k + 2] if n[2] is not None]
        if units.speed(v) <= units.slow or any(units.speed(v) <= n - units.drop for n in near):
            out.append((x1, x2, v))
    return out


Spot = collections.namedtuple("Spot", "section x1 x2 limit heading")    # heading: (x, y) the one way it's for


def slow_spots(sections, units):
    """Where to slow down: [Spot], the slow stretches of the lower limit of the two ways, those with the same limit
    within SAME_SPOT metres of each other (on any of the sections) as one, the longest. A spot slow only one way has
    that way's heading."""
    groups = []                                     # [spot, the places of its stretches' ends and middles]
    for s in sections:
        slow = {way: slow_stretches(s.stretches(way), units) for way in (True, False)}
        for x1, x2, v in slow_stretches(s.lower_limits(), units):
            mid = (x1 + x2) / 2
            ways = [way for way in (True, False) if any(a < x2 and b > x1 for a, b, _ in slow[way])]
            heading = None
            if len(ways) == 1:
                (ax, ay), (bx, by) = s.place(mid - 15), s.place(mid + 15)
                d = (1 if ways[0] else -1) / max(math.hypot(bx - ax, by - ay), 0.01)
                heading = ((bx - ax) * d, (by - ay) * d)
            spot, places = Spot(s, x1, x2, v, heading), [s.place(x) for x in (x1, mid, x2)]
            group = next((g for g in groups if units.speed(g[0].limit) == units.speed(v) and
                          any(math.dist(a, b) < SAME_SPOT for a in places for b in g[1])), None)
            if group is None:
                groups.append([spot, places])
                continue
            p = group[0]
            same_way = p.heading and heading and p.heading[0] * heading[0] + p.heading[1] * heading[1] > 0.5
            keep = spot if x2 - x1 > p.x2 - p.x1 else p
            group[0] = keep._replace(heading=keep.heading if same_way else None)
            group[1] += places
    return [g[0] for g in groups]


def route_sections(network, paths, coverage, ends=()):
    """The route's track as Sections: the longest stretch of a journey's path not drawn yet, again and again, the
    track alongside it (other lines, either way) drawn with it, until what's left is short. Then each is turned to
    start where it leaves another, or else at the end more journeys start or end at (`ends`: Samples)."""
    drawn = {}                                          # bit -> (section number, x on it)
    sections = []
    while True:
        best = None
        for path in paths:
            i0 = None
            for i in range(len(path) + 1):
                free = i < len(path) and path[i].key not in drawn and not (i0 is not None and breaks(path, i))
                if free and i0 is None:
                    i0 = i
                elif not free and i0 is not None:
                    length = path[i - 1].m - path[i0].m
                    if best is None or length > best[0]:
                        best = (length, path, i0, i - 1)
                    i0 = i if i < len(path) and path[i].key not in drawn else None
        if best is None or best[0] < SHORTEST:
            break
        _, path, i0, i1 = best
        sections.append(Section(len(sections) + 1, network, coverage, path, i0, i1, drawn))

    def busy(point):
        return sum(1 for e in ends if math.hypot(e.x - point[1], e.y - point[2]) < 600)
    turn = set()
    for s in sections:
        first, last = s.joins
        if first and last:
            if last[0] < first[0]:
                turn.add(s.number)
        elif last or (not first and busy(s.points[-1]) > busy(s.points[0])):
            turn.add(s.number)
    for s in sections:              # where on the others they meet, measured as those will be
        s.joins = [None if j is None else (j[0], sections[j[0] - 1].length - j[1] if j[0] in turn else j[1])
                   for j in s.joins]
    for s in sections:
        if s.number in turn:
            s.flip()
    for s in sections:
        for end, join in enumerate(s.joins):
            if join:
                other, x = join
                sections[other - 1].links.append((x, s.number))
                s.links.append((0.0 if end == 0 else s.length, other))
    return sections


def section_title(section, sections):
    """[first, last]: what's at its ends, like ['Leeds', 'Skipton'] or ['near Shipley', 'Ilkley']: the station at
    that end, else the station nearest where it leaves the section it meets there, else the nearest station on it."""
    names = []
    for end, name in enumerate(section.end_names()):
        join = section.joins[end]
        if not name and join:
            other, x = sections[join[0] - 1], join[1]
            near = min(other.stations, key=lambda s: abs((s[0] + s[1]) / 2 - x), default=None)
            name = "near " + near[2] if near and abs((near[0] + near[1]) / 2 - x) < 3000 else f"section {join[0]}"
        if not name:
            name = "near " + section.stations[-end][2] if section.stations else "-"
        names.append(name)
    return names


class RouteStrip:
    """One row of a section's line diagram: the limits travelling left to right above the track, and right to
    left below it, with their boards and signals; platforms, station names, where other sections meet it, and
    distances along it."""
    X0, X1 = 46.0, 992.0
    LANE, BOARD_H, CHART = 15.0, 13.0, 17.0

    def __init__(self, section, names, units, a, b, vmax):
        self.s, self.names, self.u, self.a, self.b, self.vmax = section, names, units, a, b, vmax
        self.scale = (self.X1 - self.X0) / (units.row * units.length)
        self.boards = {way: self._boards(way) for way in (True, False)}
        lanes = {way: max([lane for _, _, _, lane, _ in self.boards[way]] + [0]) + 1 for way in (True, False)}
        y = 14.0
        self.chart_up = y                               # top of the chart above
        y += self.CHART + 4
        self.boards_up = y
        y += lanes[True] * self.LANE + 1
        self.signals_up = y                             # signal names, then posts down to the track
        y += 19
        self.track_y = y
        self.names_y = y + 17
        self.signals_down = y + 21                      # posts, then signal names
        y += 40
        self.boards_down = y
        y += lanes[False] * self.LANE + 2
        self.chart_down = y                             # top (the base line) of the chart below
        y += self.CHART + 6
        self.axis_y = y
        self.height = y + 16

    def x(self, m):
        return self.X0 + (m - self.a) * self.scale

    def _boards(self, way):
        """[(x, left, text, lane, carried over)]. Above the track a board stands where a limit starts going left
        to right, the first one carried over from the strip before; below, where one starts going right to left,
        the last one carried over from the strip after."""
        limits = self.s.limits[way]
        if way:
            changes = [(self.a, self.s.limit_at(True, self.a), self.a > 1)] + \
                      [(x, v, False) for x, v in limits if self.a < x < self.b]
        else:
            end = min(self.b, self.s.length)
            changes = [(x, before, False) for (_, before), (x, _) in zip(limits, limits[1:]) if self.a < x < end]
            changes.append((end, self.s.limit_at(False, end - 0.5), end < self.s.length - 1))
        out, lane_end = [], []
        for m, v, carried in sorted(changes, key=lambda c: c[0]):
            if v is None:
                continue
            text = str(self.u.speed(v))
            w = text_width(text, 10) + 8
            x = self.x(m)
            if carried:
                left = self.X0 - 4 if way else min(x, self.X1) - w + 4
            else:
                left = max(self.X0 - 4, min(x - w / 2, self.X1 + 4 - w))
            lane = next((k for k, end in enumerate(lane_end) if end + 3 <= left), len(lane_end))
            if lane == len(lane_end):
                lane_end.append(-1e9)
            lane_end[lane] = left + w
            out.append((x, left, text, lane, carried))
        return out

    def _chart(self, way, top, o):
        """The limit along the strip as a stepped line: above the track rising away from it, below falling."""
        u, base = self.u, (top + self.CHART if way else top)
        span = self.CHART

        def y(v):
            h = min(u.speed(v), self.vmax) / self.vmax * span
            return base - h if way else base + h
        for g in range(u.grid, self.vmax + 1, u.grid):
            gy = base - g / self.vmax * span if way else base + g / self.vmax * span
            o.append(f'<line x1="{self.X0}" x2="{self.X1}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>')
            if g == self.vmax:
                o.append(f'<text x="{self.X0 - 5}" y="{gy + 3:.1f}" class="axis" text-anchor="end">{g}</text>')
        o.append(f'<line x1="{self.X0}" x2="{self.X1}" y1="{base}" y2="{base}" class="base"/>'
                 f'<text x="{self.X0 - 5}" y="{base + (-1 if way else 7):.1f}" class="arrow" text-anchor="end">'
                 f'{"▶" if way else "◀"}</text>')
        end = min(self.b, self.s.length)
        limits = self.s.limits[way]
        stretches = [(self.a, self.s.limit_at(way, self.a))] + [(x, v) for x, v in limits if self.a < x < end]
        for (m, v), nxt in zip(stretches, stretches[1:] + [(end, None)]):
            x1, x2 = self.x(m), self.x(nxt[0])
            if x2 - x1 < 0.05:
                continue
            if v is None:
                o.append(f'<rect x="{x1:.1f}" y="{base - span if way else base:.1f}" width="{x2 - x1:.1f}" '
                         f'height="{span}" class="none"/>')
                if x2 - x1 > 130:
                    o.append(f'<text x="{(x1 + x2) / 2:.1f}" y="{base - span / 2 + 3 if way else base + span / 2 + 3:.1f}"'
                             f' text-anchor="middle" class="nonetext">no journey runs this way</text>')
                continue
            yv = y(v)
            o.append(f'<rect x="{x1:.1f}" y="{min(yv, base):.1f}" width="{x2 - x1:.1f}" height="{abs(base - yv):.1f}" '
                     f'class="fill"/><line x1="{x1:.1f}" x2="{x2:.1f}" y1="{yv:.1f}" y2="{yv:.1f}" '
                     f'stroke="{u.colour(v)}" class="step"/>')
            if nxt[1] is not None:
                o.append(f'<line x1="{x2:.1f}" x2="{x2:.1f}" y1="{yv:.1f}" y2="{y(nxt[1]):.1f}" class="riser"/>')

    def svg(self):
        u, s, ty = self.u, self.s, self.track_y
        o = [f'<svg viewBox="0 0 1000 {self.height:.0f}" class="strip">']
        first, last = self.names
        o.append(f'<text x="0" y="10" class="caption"><tspan class="secnum">{s.number}</tspan>  '
                 f'{esc(first)} – {esc(last)}</text>'
                 f'<text x="1000" y="10" text-anchor="end" class="caption2">▶ to {esc(last)}  ·  ◀ to {esc(first)}'
                 f'  ·  {self.a / u.length:.0f}–{min(self.b, s.length) / u.length:.1f} {u.unit}</text>')
        self._chart(True, self.chart_up, o)
        self._chart(False, self.chart_down, o)
        # boards on posts to the track
        for way, top in ((True, self.boards_up), (False, self.boards_down)):
            for x, left, text, lane, carried in self.boards[way]:
                bt = top + lane * self.LANE
                w = text_width(text, 10) + 8
                if not carried:
                    x = min(max(x, left + 1), left + w - 1)
                    post_end = (self.signals_up + 18) if way else (self.signals_down - 2)
                    y1, y2 = (bt + self.BOARD_H, post_end) if way else (bt, post_end)
                    o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y1:.1f}" y2="{y2:.1f}" class="post"/>')
                o.append(f'<rect x="{left:.1f}" y="{bt:.1f}" width="{w:.1f}" height="{self.BOARD_H}" rx="1.5" '
                         f'class="{"board carried" if carried else "board"}"/>'
                         f'<text x="{left + w / 2:.1f}" y="{bt + 10:.1f}" text-anchor="middle" '
                         f'class="{"boardtext carried" if carried else "boardtext"}">{text}</text>')
        # track, platforms either side, station names
        end_x = self.x(min(self.b, s.length))
        o.append(f'<line x1="{self.X0}" x2="{end_x:.1f}" y1="{ty}" y2="{ty}" class="track"/>')
        for way, py in ((True, ty - 8), (False, ty + 3)):
            for m1, m2, _ in s.platforms[way]:
                if m2 >= self.a and m1 <= self.b:
                    x1, x2 = self.x(max(m1, self.a)), self.x(min(m2, self.b))
                    o.append(f'<rect x="{x1:.1f}" y="{py}" width="{max(x2 - x1, 2):.1f}" height="5" class="platform"/>')
        for m1, m2, name in s.stations:
            if self.a <= (m1 + m2) / 2 <= self.b:
                half = text_width(name, 10) / 2
                cx = min(max(self.x((m1 + m2) / 2), half + 2), 1000 - half - 2)
                o.append(f'<text x="{cx:.1f}" y="{self.names_y}" text-anchor="middle" class="station">'
                         f'{esc(name)}</text>')
        # signals facing each way: above the track left to right, below right to left
        for way in (True, False):
            last_x, row = -99.0, 0
            for m, name in s.signals[way]:
                if not self.a <= m <= self.b:
                    continue
                x = self.x(m)
                row = 1 - row if x - last_x < text_width(name, 7.5) + 4 else 0
                last_x = x
                if way:
                    o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{self.signals_up + 10}" y2="{ty - 2}" '
                             f'class="signalpost"/><circle cx="{x:.1f}" cy="{self.signals_up + 10}" r="2.4" '
                             f'class="signal"/><text x="{x:.1f}" y="{self.signals_up + 6 - row * 7.5}" '
                             f'text-anchor="middle" class="signalid">{esc(name)}</text>')
                else:
                    sy = self.signals_down
                    o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{sy}" y2="{sy + 6}" class="signalpost"/>'
                             f'<circle cx="{x:.1f}" cy="{sy + 6}" r="2.4" class="signal"/>'
                             f'<text x="{x:.1f}" y="{sy + 15 + row * 7.5}" text-anchor="middle" class="signalid">'
                             f'{esc(name)}</text>')
        # where other sections meet this one
        for m, other in s.links:
            if self.a - 1 <= m <= self.b + 1:
                x = min(max(self.x(m), self.X0), self.X1)
                o.append(f'<circle cx="{x:.1f}" cy="{ty}" r="6.5" class="link"/>'
                         f'<text x="{x:.1f}" y="{ty + 3.3}" text-anchor="middle" class="linktext">{other}</text>')
        # distances from the start of the section
        ay = self.axis_y
        o.append(f'<line x1="{self.X0}" x2="{self.X1}" y1="{ay}" y2="{ay}" class="base"/>')
        k = 0
        while True:
            m = self.a + k * u.minor * u.length
            if m > self.b + 1:
                break
            major = abs((k * u.minor) % 1) < 1e-6
            x = self.x(m)
            o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ay}" y2="{ay + (6 if major else 3)}" class="base"/>')
            if major:
                o.append(f'<text x="{x:.1f}" y="{ay + 14}" text-anchor="middle" class="axis">'
                         f'{m / u.length:.0f} {u.unit}</text>')
            k += 1
        o.append('</svg>')
        return "".join(o)


ROUTE_CSS = """
.caption { font-size: 10.5px; font-weight: 600; fill: #111; }
.secnum { font-weight: 800; }
.caption2 { font-size: 9px; fill: #555; }
.arrow { font-size: 8px; fill: #555; }
.none { fill: #f4f4f2; }
.nonetext { font-size: 8.5px; fill: #888; font-style: italic; }
.link { fill: #fff; stroke: #111; stroke-width: 1.3; }
.linktext { font-size: 8.5px; font-weight: 700; fill: #111; }
table.index td.num, table.index th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.index td.sec { font-weight: 700; width: 9mm; }
.maplink { fill: #fff; stroke: #111; stroke-width: 1.3; }
.maplinktext { font-size: 10.5px; font-weight: 700; fill: #111; }
.mapfull { position: relative; width: 277mm; height: 179mm; }
.overview.full { width: 277mm; height: 179mm; border: 0.2mm solid #bbb; }
.overview.full .maplabel { font-size: 11px; }
.key { position: absolute; width: 92mm; background: #fff; border: 0.2mm solid #bbb; padding: 2.5mm 3mm; }
.key.tr { top: 0; right: 0; } .key.tl { top: 0; left: 0; } .key.br { bottom: 0; right: 0; }
.key.bl { bottom: 0; left: 0; }
.keytitle { font-size: 15pt; font-weight: 600; line-height: 1.15; }
.keysub { font-size: 8pt; color: #444; margin: 0.8mm 0 1mm; }
.key .legend { margin-top: 1mm; gap: 1.2mm 3.5mm; }
.keyscale { display: flex; align-items: center; gap: 2mm; font-size: 8pt; color: #333; margin: 1.8mm 0 1.5mm; }
.keyscale .north { margin-left: auto; font-weight: 600; }
.key table.card { font-size: 8pt; }
.key .note { font-size: 7.3pt; line-height: 1.35; }
.leader { stroke: #111; stroke-width: 0.9; }
.leaderdot, .maparrowhead { fill: #111; }
.maparrow { stroke: #111; stroke-width: 1.6; }
.mapboard { fill: #fff; stroke: #111; stroke-width: 1.4; }
.mapboard.low { stroke: #D2502A; stroke-width: 2; }
.mapboardtext { font-size: 11px; font-weight: 800; fill: #111; }
.linespeed { font-size: 10px; font-weight: 600; font-style: italic; fill: #5F5E5A; }
.nameleader { stroke: #777; stroke-width: 0.7; }
.tunnelcase { fill: none; stroke: #111; stroke-width: 7.5; stroke-linejoin: round; }
.tunneldash { fill: none; stroke: #fff; stroke-width: 1.6; stroke-dasharray: 3 2.5; stroke-linejoin: round; }
.viaduct { fill: none; stroke: #111; stroke-width: 1.2; stroke-linejoin: round; }
.roadover { stroke: #333; stroke-width: 1.6; }
.crossing { fill: none; stroke: #111; stroke-width: 1.8; }
.landmarkname { font-size: 9px; font-style: italic; fill: #333; }
.place { fill: #fff; stroke: #666; stroke-width: 1; }
.placename { font-size: 8.5px; fill: #555; }
.swatch.tall { height: 2.4mm; }
.frame { fill: none; stroke: #111; stroke-width: 1; stroke-dasharray: 4 2.5; }
.insetbox { fill: #fff; stroke: #333; stroke-width: 1.1; }
.insettag { fill: #111; }
.insettagtext { font-size: 11px; font-weight: 800; fill: #fff; }
.insettagtext.small { font-size: 9.5px; }
.insettitle { font-size: 10px; font-weight: 600; fill: #111; }
.insetrule { stroke: #bbb; stroke-width: 0.6; }
.insetscale { font-size: 8px; fill: #555; }
.key td.shortsec { color: #555; font-size: 7.6pt; }
"""


MAP_MM = (277.0, 179.0)     # the whole-route map: all of the first page under its heading
KEY_MM = 92.0               # how wide the key is, in the map's emptiest corner
MAP_W = 1000.0              # the map's own units across it
MAP_H = MAP_W * MAP_MM[1] / MAP_MM[0]
CROWD = (8, 50.0)           # this many names and boards within this many map units (14 mm) is crowded: an inset
CLOSE = (3, 30.0)           # this many within this many (8 mm) are close together: an inset if there's room
INSETS = 10                 # insets on a page at most
INSET_MOST = 180.0          # map units: a crowded stretch longer than this goes in more than one inset
INSET_ZOOMS = (2, 2.5, 3, 3.5, 4, 5, 6)
INSET_LAST = 1.5            # the zoom of an inset with no room at the smallest of those
INSET_TITLE = 16.0          # map units: an inset's title bar
INSET_NARROWEST = 130.0     # map units: an inset's width at least, for its scale bar
BOARD_REACH = (16, 24, 34, 46, 60, 76, 95)      # map units: how far from its stretch a board is tried


# ---------------------------------------------------------------- landmarks
# Scenery the route's makers built, by what its blueprint is called: (kind, matching, not matching). The names
# differ from route to route (BP_Crossing_Gate_01, BP_LevelCrossingOld_01, BP_SignalActivatedLevelCrossingLink).
LANDMARK_KINDS = (
    ("crossing", r"LevelCrossing|Crossing_?Gate", r"Sign|Floor|Filler|Pedestrian"),
    ("tunnel", r"Tunnel", r"Wall|Pillar|Block|Light|Seam|Poly|ViewBlocker|Retaining|Hanger|Wire"),
    ("viaduct", r"Viaduct|BridgeRail|Train_?Bridge|RailBridge|BridgeArched_\dT", r"Support|Pillar|Cover|Siding"),
    ("over", r"OcclusionUnderBridge|Road_?Bridge|BridgeRoad|Overbridge(?!Support)", r"Support|Pillar|Wall|Rail"),
)
LANDMARK_WORDS = (b"Tunnel", b"Viaduct", b"Bridge", b"Crossing")
LANDMARK_DENSE = 9.0        # map units (2.5 mm): a road bridge closer than this to another or a crossing is left out
PORTALS_APART = (80.0, 2500.0)  # metres: two tunnel ends this far apart, nothing between, are one tunnel


def landmark_kind(cls):
    for kind, yes, no in LANDMARK_KINDS:
        if re.search(yes, cls, re.I) and not re.search(no, cls, re.I):
            return kind
    return None


LANDMARK_SWATCHES = (
    ("tunnels", "tunnel", '<line x1="0" x2="24" y1="5" y2="5" stroke="#111" stroke-width="7.5"/><line x1="0" x2="24" '
                          'y1="5" y2="5" stroke="#fff" stroke-width="1.6" stroke-dasharray="3 2.5"/>'),
    ("viaducts", "viaduct, rail bridge", '<line x1="0" x2="24" y1="1.5" y2="1.5" stroke="#111" stroke-width="1.2"/>'
                                         '<line x1="0" x2="24" y1="8.5" y2="8.5" stroke="#111" stroke-width="1.2"/>'
                                         '<line x1="0" x2="24" y1="5" y2="5" stroke="#5F5E5A" stroke-width="3"/>'),
    ("over", "road over", '<line x1="0" x2="24" y1="5" y2="5" stroke="#5F5E5A" stroke-width="3"/><line x1="12" '
                          'x2="12" y1="0" y2="10" stroke="#333" stroke-width="1.6"/>'),
    ("crossings", "level crossing", '<line x1="0" x2="24" y1="5" y2="5" stroke="#5F5E5A" stroke-width="3"/><path '
                                    'd="M8.4,1.4 L15.6,8.6 M8.4,8.6 L15.6,1.4" stroke="#111" stroke-width="1.8"/>'),
)


def landmark_legend(landmarks):
    """The key's swatches for the landmarks a route has."""
    lm = landmarks or {}
    out = []
    for kind, label, svg in LANDMARK_SWATCHES:
        if lm.get(kind):
            out.append(f'<span><svg class="swatch tall" viewBox="0 0 24 10">{svg}</svg>{esc(label)}</span>')
    return "".join(out)


def tile_origin(path):
    """Where a 1 km map tile 'ST_x-77_y-258' is, in metres: what's in it is placed from there."""
    m = re.search(r"_x(-?\d+)_y(-?\d+)$", path)
    return int(m.group(1)) * 1000.0, int(m.group(2)) * 1000.0


@functools.lru_cache(maxsize=4)
def scenery(files, level):
    """The route's landmarks among its scenery: [(kind, blueprint, (x, y), [points of its spline] or None, half its
    length along the line or 0)], in metres like the track. Reading every scenery tile takes a while."""
    path = files.file_of(level)
    if not path:
        return ()
    folder = path.rsplit("/", 1)[0] + "/"
    out = []
    for f in sorted(files.where):
        if not (f.startswith(folder) and re.search(r"/(ST|TS)_x-?\d+_y-?\d+\.umap$", f)):
            continue
        tile = f[:-len(".umap")]
        try:
            head = files.where[f].read(f)
            if not any(word in head for word in LANDMARK_WORDS):
                continue
            pkg = tsw_paks.Package("/" + tile, head, files.where[tile + ".uexp"].read(tile + ".uexp"))
        except ValueError:                  # a damaged tile: its landmarks are left off
            continue
        out += tile_landmarks(pkg, tile)
    return tuple(out)


def tile_landmarks(pkg, tile):
    """The landmarks among a map tile's blueprints, as scenery() gives them."""
    out = []
    kids = collections.defaultdict(list)
    for i, e in enumerate(pkg.exports, 1):
        kids[e["outer"]].append(i)
    ox, oy = tile_origin(tile)
    for i in range(1, len(pkg.exports) + 1):
        cls = pkg.class_name(i).split(".")[-1]
        kind = None if cls.startswith("Default__") or cls.endswith("Component") else landmark_kind(cls)
        if not kind:
            continue
        root = getattr(pkg.properties(i).get("RootComponent"), "name", None)
        rp = next((pkg.properties(j) for j in kids[i] if pkg.exports[j - 1]["name"] == root), None)
        if not rp or not rp.get("RelativeLocation"):
            continue
        lx, ly = rp["RelativeLocation"][:2]
        yaw = math.radians((rp.get("RelativeRotation") or [0, 0, 0])[1])
        sx, sy = (rp.get("RelativeScale3D") or [1, 1, 1])[:2]
        at = (ox + lx / 100, oy + ly / 100)

        def world(px, py):
            px, py = px * sx, py * sy
            return (at[0] + (px * math.cos(yaw) - py * math.sin(yaw)) / 100,
                    at[1] + (px * math.sin(yaw) + py * math.cos(yaw)) / 100)
        spline, half = None, 0.0
        for j in kids[i]:
            c = pkg.class_name(j)
            if c.endswith("SplineComponent"):
                points = ((pkg.properties(j).get("SplineCurves") or {}).get("Position") or {}).get("Points") or []
                if len(points) >= 2:
                    spline = tuple(world(*p["OutVal"][:2]) for p in points)
            elif c == "BoxComponent":
                extent = pkg.properties(j).get("BoxExtent")
                if extent:
                    half = max(abs(extent[0] * sx), abs(extent[1] * sy)) / 100
        out.append((kind, cls, at, spline, half))
    return out


class RouteIndex:
    """Points every 10 m along the sections, found by place: where on the route (section, x) something is."""
    CELL = 50.0

    def __init__(self, sections):
        self.cells = collections.defaultdict(list)
        for s in sections:
            for k in range(int(s.length // 10) + 1):
                x = min(k * 10.0, s.length)
                p = s.place(x)
                self.cells[(int(p[0] // self.CELL), int(p[1] // self.CELL))].append((p, s, x))

    def nearest(self, p, within):
        """(section, x) of the route nearest p, if it's `within` metres."""
        best, reach = None, math.ceil(within / self.CELL)
        cx, cy = int(p[0] // self.CELL), int(p[1] // self.CELL)
        for i in range(cx - reach, cx + reach + 1):
            for j in range(cy - reach, cy + reach + 1):
                for q, s, x in self.cells.get((i, j), ()):
                    d = math.dist(p, q)
                    if d <= within and (best is None or d < best[0]):
                        best = (d, s, x)
        return best and best[1:]


def densify(points, step=10.0):
    out = []
    for a, b in zip(points, points[1:]):
        n = max(1, int(math.dist(a, b) // step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    return out + list(points[-1:])


def runs(found, gap):
    """[(section, x1, x2)] of the places [(section, x)] on one section closer than `gap` metres in a row."""
    by = collections.defaultdict(list)
    for s, x in found:
        by[s.number].append((x, s))
    out = []
    for xs in by.values():
        xs.sort(key=lambda t: t[0])
        start = last = xs[0]
        for x, s in xs[1:] + [(math.inf, None)]:
            if x - last[0] > gap:
                out.append((start[1], start[0], last[0]))
                start = (x, s)
            last = (x, s)
    return out


def route_landmarks(network, objects, sections):
    """What the first page shows besides the line: {'tunnels': [(section, x1, x2, name or None)], 'viaducts': ...,
    'over': [(section, x)] road bridges, 'crossings': [(section, x)] level crossings, 'places': [((x, y), name)]
    junctions and depots}, from the track's markers (named tunnels, viaducts, junctions, depots) and the `objects` of
    its scenery."""
    index = RouteIndex(sections)
    out = {"tunnels": [], "viaducts": [], "over": [], "crossings": [], "places": []}
    named = collections.defaultdict(list)
    places = collections.defaultdict(list)
    for kind, name, guid, a, b in network.markers:
        if guid not in network.ribbons:
            continue
        n = max(4, int(abs(b - a) * network.ribbons[guid].length // 20))     # every 20 m along it
        points = [network.position(guid, a + (b - a) * k / n) for k in range(n + 1)]
        what = "tunnels" if kind == "Tunnel" or re.search(r"tunnel", name, re.I) else \
            "viaducts" if re.search(r"viadu|bridge|brücke|bruecke", name, re.I) else None
        if what:
            base = re.sub(r"\s+(\d+|I+V?|Up|Down|Gleis \d+)$", "", name.strip())
            named[(what, base)] += [f for f in (index.nearest(p, 15) for p in points) if f]
            continue
        m = re.match(r"^(.*?\b(Junction|Jn|Depot|Yard|Works|BW|Bahnbetriebswerk))\b", name)
        if m:
            places[m.group(1)].append(points[len(points) // 2])
    for (what, base), found in named.items():
        out[what] += [(s, x1 - 5, x2 + 5, base) for s, x1, x2 in runs(found, 120)]
    out["places"] = [((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)), name)
                     for name, pts in places.items()]
    found = collections.defaultdict(list)
    for kind, cls, at, spline, half in objects:
        if kind in ("tunnel", "viaduct"):
            on = [f for f in (index.nearest(p, 12) for p in (densify(spline, 8) if spline else [at])) if f]
            if half and not spline and on:          # a box along the line
                s, x = on[0]
                on = [(s, max(x - half, 0)), (s, min(x + half, s.length))]
            found[kind] += on
        else:
            f = index.nearest(at, 25 if kind == "crossing" else 18)
            if f:
                found[kind].append(f)
    crossings = runs(found["crossing"], 60)
    # a tunnel's two ends, with no station or level crossing between them, are one tunnel
    ends = sorted(runs(found["tunnel"], 60), key=lambda r: (r[0].number, r[1]))
    tunnels, k = [], 0
    while k < len(ends):
        s, a1, a2 = ends[k]
        if k + 1 < len(ends):
            t, b1, b2 = ends[k + 1]
            between = [st for st in s.stations if st[0] < b1 and st[1] > a2] + \
                [c for c in crossings if c[0] is s and a2 < c[1] < b1]
            if t is s and PORTALS_APART[0] <= b1 - a2 <= PORTALS_APART[1] and not between:
                tunnels.append((s, a1, b2))
                k += 2
                continue
        tunnels.append((s, a1, a2))
        k += 1
    for what, found_runs in (("tunnels", tunnels), ("viaducts", runs(found["viaduct"], 30))):
        for s, x1, x2 in found_runs:
            if x2 - x1 < 15:
                x1, x2 = x1 - 12, x2 + 12
            out[what].append((s, max(x1, 0), min(x2, s.length), None))
        merged = []                                 # those overlapping or nearly meeting: one, keeping a name
        for s, x1, x2, name in sorted(out[what], key=lambda r: (r[0].number, r[1])):
            if merged and merged[-1][0] is s and x1 <= merged[-1][2] + 25:
                m = merged[-1]
                merged[-1] = (s, m[1], max(m[2], x2), m[3] or name)
            else:
                merged.append((s, x1, x2, name))
        out[what] = merged
    for s, x1, x2 in runs(found["over"], 15):
        if not any(t is s and a - 5 < x1 and x2 < b + 5 for t, a, b, _ in out["tunnels"]):
            out["over"].append((s, (x1 + x2) / 2))
    out["crossings"] = [(s, (x1 + x2) / 2) for s, x1, x2 in crossings]
    return out


def crossing(a, b, c, d):
    """Whether line a-b crosses line c-d."""
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def route_stations(sections, places=None):
    """[(name, (x, y))]: the stations on the sections and those the journeys run past on track that isn't drawn
    (`places`: {name: [(x, y)]}), each once, where its platforms are."""
    found = collections.defaultdict(list)
    for s in sections:
        for a, b, name in s.stations:
            found[name].append(s.place((a + b) / 2))
    drawn = set(found)
    for name, at in (places or {}).items():
        if name not in drawn:
            found[name] += at
    middles = [(name, (sum(p[0] for p in at) / len(at), sum(p[1] for p in at) / len(at)))
               for name, at in found.items()]
    named = numbered_twins(middles, lambda p, q: math.dist(p, q) < 400)
    kept, spelt = [], {}                    # spelt the way with the most letters like ü
    for name, p in sorted(named, key=lambda np: -sum(ord(c) > 127 for c in np[0])):
        twin = next((n for n, q in kept if plain(n) == plain(name) and math.dist(p, q) < 400), None)
        if twin:
            spelt[(name, p)] = twin
        else:
            kept.append((name, p))
    merged = collections.defaultdict(list)
    for name, p in named:
        merged[spelt.get((name, p), name)].append(p)
    return sorted((name, (sum(p[0] for p in at) / len(at), sum(p[1] for p in at) / len(at)))
                  for name, at in merged.items())


class Boxes:
    """Boxes (x0, y0, x1, y1) on the map, found by place."""
    CELL = 25.0

    def __init__(self):
        self.all, self.cells = [], collections.defaultdict(list)

    def _cells(self, box):
        for i in range(int(box[0] // self.CELL), int(box[2] // self.CELL) + 1):
            for j in range(int(box[1] // self.CELL), int(box[3] // self.CELL) + 1):
                yield i, j

    def add(self, box):
        self.all.append(box)
        for k in self._cells(box):
            self.cells[k].append(box)

    def remove(self, box):
        self.all.remove(box)
        for k in self._cells(box):
            self.cells[k].remove(box)

    def hits(self, box):
        return any(not (box[2] < t[0] or box[0] > t[2] or box[3] < t[1] or box[1] > t[3])
                   for k in self._cells(box) for t in self.cells.get(k, ()))


class Occupancy:
    """Which 5-unit cells of the map are used, to find empty rectangles quickly."""
    CELL = 5.0

    def __init__(self):
        self.nx, self.ny = int(MAP_W // self.CELL) + 1, int(MAP_H // self.CELL) + 1
        self.grid = [[0] * self.nx for _ in range(self.ny)]

    def add(self, box, margin=5.0):
        c = self.CELL
        i0, j0 = max(int((box[0] - margin) // c), 0), max(int((box[1] - margin) // c), 0)
        i1, j1 = min(int((box[2] + margin) // c), self.nx - 1), min(int((box[3] + margin) // c), self.ny - 1)
        for j in range(j0, j1 + 1):
            row = self.grid[j]
            for i in range(i0, i1 + 1):
                row[i] = 1

    def empty(self, w, h, near):
        """The top left (x, y) of the empty w x h rectangle nearest `near`, or None."""
        c = self.CELL
        s = [[0] * (self.nx + 1) for _ in range(self.ny + 1)]        # how many used cells above and left
        for j in range(self.ny):
            run = 0
            for i in range(self.nx):
                run += self.grid[j][i]
                s[j + 1][i + 1] = s[j][i + 1] + run
        cw, ch = math.ceil(w / c), math.ceil(h / c)
        best = None
        for j in range(1, self.ny - ch):
            for i in range(1, self.nx - cw):
                if s[j + ch][i + cw] - s[j][i + cw] - s[j + ch][i] + s[j][i] == 0:
                    d = math.hypot(i * c + w / 2 - near[0], j * c + h / 2 - near[1])
                    if best is None or d < best[0]:
                        best = (d, i * c, j * c)
        return best and best[1:]


NAME_SPOTS = [(8, 4, "start"), (-8, 4, "end"), (0, -8, "middle"), (0, 16, "middle"), (8, -6, "start"),
              (-8, -6, "end"), (8, 14, "start"), (-8, 14, "end"), (13, -13, "start"), (-13, -13, "end"),
              (13, 21, "start"), (-13, 21, "end"), (18, 4, "start"), (-18, 4, "end"), (0, -16, "middle"),
              (0, 25, "middle"), (20, -18, "start"), (-20, -18, "end"), (20, 26, "start"), (-20, 26, "end")]


class MapView:
    """The route drawn onto a rectangle of the first page's map at one scale, things placed where they don't
    overlap. World (x, y) -> map (x, y) by `at`. `strict`: a name needing a leader counts as finding no room."""

    def __init__(self, scale, ox, oy, x0, y0, rect, strict=False, landmarks=None):
        self.scale, self.ox, self.oy, self.x0, self.y0, self.rect = scale, ox, oy, x0, y0, rect
        self.strict = strict
        self.landmarks = landmarks or {}            # route_landmarks
        self.taken = Boxes()            # nothing else may go here
        self.solid = Boxes()            # names, boards, dots and circles: no leader runs through these
        self.leaders, self.drawn, self.o = [], [], []
        self.failed = []                # [map point] of what found no room, or only far away
        self.stuck = []                 # [map point] of what found no room at all: a name over others, a board
                                        # whose leader runs through names

    def at(self, x, y):
        return self.ox + (x - self.x0) * self.scale, self.oy + (y - self.y0) * self.scale

    def back(self, px, py):
        return self.x0 + (px - self.ox) / self.scale, self.y0 + (py - self.oy) / self.scale

    def inside(self, p, pad=0.0):
        r = self.rect
        return r[0] - pad <= p[0] <= r[2] + pad and r[1] - pad <= p[1] <= r[3] + pad

    def free(self, box):
        r = self.rect
        return box[0] > r[0] + 4 and box[2] < r[2] - 4 and box[1] > r[1] + 4 and box[3] < r[3] - 4 and \
            not self.taken.hits(box)

    def take(self, box, solid=True):
        self.taken.add(box)
        self.drawn.append(box)
        if solid:
            self.solid.add(box)

    def lines(self, sections, units, clip=None):
        """The line and its landmarks: tunnels as a dark casing with white dashes, viaducts and rail bridges as a line
        each side, road bridges as a bar across (left out where closer than LANDMARK_DENSE to another or to a level
        crossing) and every level crossing as a cross."""
        lm, o = self.landmarks, self.o
        tunnels = [self.stretch(s, x1, x2) for s, x1, x2, _ in lm.get("tunnels", ())]
        tunnels = [pts for pts in tunnels if pts]
        for points in tunnels:
            o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in points)}" class="tunnelcase"/>')
        self.route_lines(sections, units, clip)
        for points in tunnels:
            o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in points)}" class="tunneldash"/>')
        for s, x1, x2, _ in lm.get("viaducts", ()):
            points = self.stretch(s, x1, x2)
            for side in (1, -1) if points else ():
                beside = []
                for k, q in enumerate(points):
                    a, b = points[max(k - 1, 0)], points[min(k + 1, len(points) - 1)]
                    d = max(math.dist(a, b), 0.01)
                    beside.append((q[0] - side * 4.6 * (b[1] - a[1]) / d, q[1] + side * 4.6 * (b[0] - a[0]) / d))
                o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in beside)}" class="viaduct"/>')
        marks = [(self.at(*s.place(x)), s, x, kind) for kind in ("over", "crossings") for s, x in lm.get(kind, ())]
        marks = [m for m in marks if self.inside(m[0])]
        for q, s, x, kind in marks:
            if kind == "over" and any(r is not q and math.dist(q, r) < LANDMARK_DENSE for r, *_ in marks):
                continue                # every level crossing is drawn; a road bridge only where there's room
            if kind == "over":
                hx, hy = self.heading(s, x)
                o.append(f'<line x1="{q[0] - hy * 5.5:.1f}" y1="{q[1] + hx * 5.5:.1f}" x2="{q[0] + hy * 5.5:.1f}" '
                         f'y2="{q[1] - hx * 5.5:.1f}" class="roadover"/>')
            else:
                o.append(f'<path d="M{q[0] - 3.6:.1f},{q[1] - 3.6:.1f} L{q[0] + 3.6:.1f},{q[1] + 3.6:.1f} '
                         f'M{q[0] - 3.6:.1f},{q[1] + 3.6:.1f} L{q[0] + 3.6:.1f},{q[1] - 3.6:.1f}" class="crossing"/>')

    def stretch(self, s, x1, x2):
        """Map points every 5 m along a stretch of a section, or [] if it's under 5 map units or not here."""
        if (x2 - x1) * self.scale < 5:
            return []
        n = max(2, int((x2 - x1) // 5) + 1)
        points = [self.at(*s.place(x1 + (x2 - x1) * k / (n - 1))) for k in range(n)]
        return points if any(self.inside(q, 10) for q in points) else []

    def landmark_names(self):
        """The names of tunnels and viaducts, and of junctions and depots, where there's room beside them."""
        lm = self.landmarks
        for s, x1, x2, name in lm.get("tunnels", []) + lm.get("viaducts", []):
            ax, ay = self.at(*s.place((x1 + x2) / 2))
            if not name or not self.inside((ax, ay)) or (x2 - x1) * self.scale < 5:
                continue
            found = self.around(ax, ay, text_width(name, 9) + 2, 10.0, (10, 15, 21, 28),
                                self.heading(s, (x1 + x2) / 2), ring=8, leader=0)
            if found:
                box = found[0]
                self.take(box)
                self.o.append(f'<text x="{(box[0] + box[2]) / 2:.1f}" y="{box[3] - 2:.1f}" text-anchor="middle" '
                              f'class="landmarkname">{esc(name)}</text>')
        for p, name in lm.get("places", ()):
            q = self.at(*p)
            if not self.inside(q):
                continue
            found = self.around(q[0], q[1], text_width(name, 8.5) + 2, 10.0, (8, 13, 19), ring=8, leader=0)
            if found:
                box = found[0]
                self.take(box, solid=False)
                self.o.append(f'<rect x="{q[0] - 2:.1f}" y="{q[1] - 2:.1f}" width="4" height="4" class="place"/>'
                              f'<text x="{(box[0] + box[2]) / 2:.1f}" y="{box[3] - 2:.1f}" text-anchor="middle" '
                              f'class="placename">{esc(name)}</text>')

    def route_lines(self, sections, units, clip=None):
        """The sections coloured by the lower limit of the two ways, through their points and every place the
        limit changes (so a short stretch shows its colour); the line taken every 3 units."""
        if clip:
            self.o.append(f'<g clip-path="url(#{clip})">')
        for s in sections:
            cuts = {x for way in (True, False) for x, _ in s.limits[way] if 0 < x < s.length}
            marks = sorted([(m, x, y, False) for m, x, y in s.points] + [(x, *s.place(x), True) for x in cuts])
            run, colour = [], None

            def flush():
                if len(run) > 1 and any(self.inside(q, 30) for q in run):
                    self.o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in run)}" '
                                  f'stroke="{colour}" class="line"/>')
            for (ma, xa, ya, _), (mb, xb, yb, cut) in zip(marks, marks[1:]):
                vs = [v for v in (s.limit_at(True, (ma + mb) / 2), s.limit_at(False, (ma + mb) / 2)) if v is not None]
                c = units.colour(min(vs)) if vs else "#bbb"
                p, q = self.at(xa, ya), self.at(xb, yb)
                if c != colour:
                    flush()
                    run, colour = [p], c
                if cut or mb == marks[-1][0] or abs(q[0] - run[-1][0]) + abs(q[1] - run[-1][1]) > 1.5:
                    run.append(q)
                n = max(1, math.ceil(math.dist(p, q) / 3))
                for k in range(n):
                    x, y = p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n
                    if self.inside((x, y)):
                        self.taken.add((x - 2.5, y - 2.5, x + 2.5, y + 2.5))
                        self.drawn.append((x - 2.5, y - 2.5, x + 2.5, y + 2.5))
            flush()
        if clip:
            self.o.append('</g>')

    def heading(self, s, m):
        (ax, ay), (bx, by) = self.at(*s.place(m - 40)), self.at(*s.place(m + 40))
        d = max(math.hypot(bx - ax, by - ay), 0.01)
        return (bx - ax) / d, (by - ay) / d

    def through(self, a, b):
        """How many 3-unit steps of a leader from a to b run through names, boards and dots (not counting the
        first 12 units, by the dot it starts at)."""
        length = math.dist(a, b)
        return sum(1 for t in range(12, int(length), 3)
                   if self.solid.hits((a[0] + (b[0] - a[0]) * t / length, a[1] + (b[1] - a[1]) * t / length) * 2))

    def clear(self, a, b, cross=True):
        """Whether a leader from a to b runs clear of names, boards and dots, and (`cross`) of other leaders."""
        return not self.through(a, b) and (not cross or not any(crossing(a, b, c, d) for c, d in self.leaders))

    def around(self, ax, ay, w, h, reach, heading=(1.0, 0.0), ring=16, leader=2):
        """A free box w x h near (ax, ay), tried round it at each distance in `reach`, across the line (`heading`)
        first: (box, where its leader meets it), or None. `leader` 2: the leader runs clear of names and other
        leaders, 1: of names, 0: the one running through least."""
        hx, hy = heading
        angles = sorted((k * 2 * math.pi / ring for k in range(ring)),
                        key=lambda a: abs(math.cos(a) * hx + math.sin(a) * hy))
        least = None
        for r in reach:
            for a in angles:
                cx, cy = ax + r * math.cos(a), ay + r * math.sin(a)
                box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                if not self.free(box):
                    continue
                end = (min(max(ax, box[0]), box[2]), min(max(ay, box[1]), box[3]))
                if leader == 0:
                    through = self.through((ax, ay), end)
                    if least is None or through < least[0]:
                        least = (through, box, end)
                    if through == 0:
                        break
                    continue
                if not self.clear((ax, ay), end, leader > 1):
                    continue
                return box, end
            if least and least[0] == 0:
                break
        return least and least[1:]

    def section_numbers(self, sections, units):
        """[(map point, number)] for the sections a mile (km) or more long, each where it's clear along it."""
        out = []
        for s in sections:
            if s.length < units.length:
                continue
            for f in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88, 0.5):
                q = self.at(*s.place(s.length * f))
                box = (q[0] - 9, q[1] - 9, q[0] + 9, q[1] + 9)
                if self.inside(q) and not self.solid.hits(box):
                    break
            if self.inside(q):
                self.take(box)
                out.append((q, s.number))
        return out

    def draw_numbers(self, numbers):
        for (qx, qy), number in numbers:
            self.o.append(f'<circle cx="{qx:.1f}" cy="{qy:.1f}" r="8" class="maplink"/><text x="{qx:.1f}" '
                          f'y="{qy + 3.7:.1f}" text-anchor="middle" class="maplinktext">{number}</text>')

    def dots(self, stations):
        """Each station's dot: [(name, map point)] of those in the rectangle."""
        out = []
        for name, (x, y) in stations:
            q = self.at(x, y)
            if self.inside(q):
                self.take((q[0] - 5, q[1] - 5, q[0] + 5, q[1] + 5))
                self.o.append(f'<circle cx="{q[0]:.1f}" cy="{q[1]:.1f}" r="3.3" class="dot"/>')
                out.append((name, q))
        return out

    def names(self, placed):
        """The stations' names beside their dots, the one with the fewest places left first each time; where
        there's none, a little way off on a leader."""
        todo = list(placed)
        while todo:
            best = None
            for name, (sx, sy) in todo:
                w = text_width(name, 11)
                fits = [spot for spot in NAME_SPOTS if self.free(name_box(sx, sy, w, *spot))]
                if best is None or len(fits) < len(best[0]):
                    best = (fits, name, (sx, sy), w)
                    if not fits:
                        break
            fits, name, (sx, sy), w = best
            todo.remove((name, (sx, sy)))
            if fits:
                tx, ty, anchor = fits[0]
                box = name_box(sx, sy, w, tx, ty, anchor)
                self.o.append(f'<text x="{sx + tx:.1f}" y="{sy + ty:.1f}" text-anchor="{anchor}" '
                              f'class="maplabel">{esc(name)}</text>')
                self.take(box)
                continue
            found = self.around(sx, sy, w + 2, 12, (24, 34, 46, 60, 76, 95))
            if found is None or self.strict:
                self.failed.append((sx, sy))
            if found:
                box, end = found
                self.leaders.append(((sx, sy), end))
                self.o.append(f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
                              f'class="nameleader"/><text x="{(box[0] + box[2]) / 2:.1f}" y="{box[3] - 2:.1f}" '
                              f'text-anchor="middle" class="maplabel">{esc(name)}</text>')
            else:
                self.stuck.append((sx, sy))
                tx, ty, anchor = NAME_SPOTS[0]
                box = name_box(sx, sy, w, tx, ty, anchor)
                self.o.append(f'<text x="{sx + tx:.1f}" y="{sy + ty:.1f}" text-anchor="{anchor}" '
                              f'class="maplabel">{esc(name)}</text>')
            self.take(box)

    def boards(self, spots, units):
        """A board at each of the `spots` in the rectangle, the lowest placed first, on a leader to its stretch."""
        for spot in sorted(spots, key=lambda p: p.limit):
            s, mid = spot.section, (spot.x1 + spot.x2) / 2
            ax, ay = self.at(*s.place(mid))
            if not self.inside((ax, ay)):
                continue
            text = str(units.speed(spot.limit))
            w, h = text_width(text, 11) + (21 if spot.heading else 9), 14.0
            heading = self.heading(s, mid)
            found = next((f for leader in (2, 1) for f in [self.around(ax, ay, w, h, BOARD_REACH, heading, leader)]
                          if f), None)
            if found is None:
                found = self.around(ax, ay, w, h, BOARD_REACH, heading, leader=0)
                if found is None or self.through((ax, ay), found[1]):
                    self.stuck.append((ax, ay))
            if found is None or math.dist((ax, ay), found[1]) > 40:
                self.failed.append((ax, ay))
            if found is None:                   # nowhere free: across the line, over whatever is there
                cx, cy = ax - heading[1] * 18, ay + heading[0] * 18
                box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                found = box, (min(max(ax, box[0]), box[2]), min(max(ay, box[1]), box[3]))
            box, end = found
            self.take((box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2))
            self.leaders.append(((ax, ay), end))
            tx = box[0] + (w - 12 if spot.heading else w) / 2
            self.o.append(f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" class="leader"/>'
                          f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="2" class="leaderdot"/>'
                          f'<rect x="{box[0]:.1f}" y="{box[1]:.1f}" width="{w:.1f}" height="{h:.0f}" rx="2" '
                          f'class="{"mapboard low" if units.speed(spot.limit) <= units.slow else "mapboard"}"/>'
                          f'<text x="{tx:.1f}" y="{box[1] + 10.8:.1f}" text-anchor="middle" class="mapboardtext">'
                          f'{text}</text>')
            if spot.heading:                    # for going one way only: an arrow that way
                angle = math.degrees(math.atan2(spot.heading[1], spot.heading[0]))
                self.o.append(f'<g transform="translate({box[2] - 9:.1f},{box[1] + h / 2:.1f}) rotate({angle:.0f})">'
                              '<line x1="-5" y1="0" x2="1" y2="0" class="maparrow"/><path d="M5,0 L0,-3.4 L0,3.4 Z" '
                              'class="maparrowhead"/></g>')

    def line_speeds(self, sections, units, skip=()):
        """The limit beside the line along the long stretches that aren't slow (not in the boxes `skip`)."""
        for s in sections:
            limits = s.lower_limits()
            slow = slow_stretches(limits, units)
            for x1, x2, v in limits:
                if v is None or x2 - x1 < LONG_RUN or (x1, x2, v) in slow:
                    continue
                ax, ay = self.at(*s.place((x1 + x2) / 2))
                if not self.inside((ax, ay)) or any(within(b, (ax, ay)) for b in skip):
                    continue
                text = str(units.speed(v))
                found = self.around(ax, ay, text_width(text, 10), 10.0, (10, 14, 19), self.heading(s, (x1 + x2) / 2),
                                    ring=8, leader=0)
                if found:
                    box = found[0]
                    self.take(box, solid=False)
                    self.o.append(f'<text x="{(box[0] + box[2]) / 2:.1f}" y="{box[3] - 1.5:.1f}" '
                                  f'text-anchor="middle" class="linespeed">{text}</text>')


def name_box(sx, sy, w, tx, ty, anchor):
    left = sx + tx - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
    return (left, sy + ty - 9, left + w, sy + ty + 2)


def within(box, p):
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def overview_layout(sections, key_height):
    """Where the map of the sections goes on the first page: as large as it can be with a corner left empty for
    the key (`key_height` mm tall), then as near the middle as it goes clear of it. Returns (map units per metre,
    the map point and world point of the top left, the key's corner 'tr', 'tl', 'br' or 'bl', the key's box)."""
    per_mm = MAP_W / MAP_MM[0]
    pts = [(x, y) for s in sections for _, x, y in s.points]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    pad, near = 30.0, 12.0
    kw, kh = (KEY_MM + 3) * per_mm, (key_height + 3) * per_mm
    corners = {"tr": (MAP_W - kw, 0, MAP_W, kh), "tl": (0, 0, kw, kh),
               "br": (MAP_W - kw, MAP_H - kh, MAP_W, MAP_H), "bl": (0, MAP_H - kh, kw, MAP_H)}
    full = min((MAP_W - 2 * pad) / dx, (MAP_H - 2 * pad) / dy)

    def placed(scale, corner, centred=0.0):
        """Where the map goes at a scale: pushed away from the key's corner, or `centred` (0-1) of the way back
        to the middle of the page."""
        spare_x, spare_y = MAP_W - 2 * pad - dx * scale, MAP_H - 2 * pad - dy * scale
        fx = (0.0 if corner[1] == "r" else 1.0) * (1 - centred) + 0.5 * centred
        fy = (1.0 if corner[0] == "t" else 0.0) * (1 - centred) + 0.5 * centred
        return pad + spare_x * fx, pad + spare_y * fy

    def hits(scale, corner, centred=0.0):
        ox, oy = placed(scale, corner, centred)
        a, b, c, d = corners[corner]
        return sum(1 for x, y in pts
                   if a - near <= ox + (x - x0) * scale <= c + near and b - near <= oy + (y - y0) * scale <= d + near)
    layout = next(((full * (1 - 0.04 * k), corner) for k in range(19) for corner in corners
                   if hits(full * (1 - 0.04 * k), corner) == 0), None)
    if layout is None:                  # nowhere's empty: the corner the key hides least of
        layout = (full, min(corners, key=lambda corner: hits(full, corner)))
        centred = 0.0
    else:
        centred = next(t / 10 for t in range(10, -1, -1) if hits(*layout, t / 10) == 0)
    scale, corner = layout
    ox, oy = placed(scale, corner, centred)
    return scale, (ox, oy), (x0, y0), corner, corners[corner]


def place_boxes(points, things, radius, stuck=()):
    """[(how many things, box)]: boxes (map units) round the groups of `points` within `radius` of each other (two
    or more, or one of `stuck`), each taking in what's just outside it; those overlapping as one; one longer than
    INSET_MOST cut across into pieces, each down to the `things` (map points) in it."""
    groups = []
    for p in points:
        near = [g for g in groups if any(math.dist(p, q) < radius for q in g)]
        groups = [g for g in groups if g not in near] + [[p] + [q for g in near for q in g]]
    boxes = []
    for g in groups:
        if len(g) < 2 and g[0] not in stuck:
            continue
        xs, ys = [p[0] for p in g], [p[1] for p in g]
        box = [min(xs) - 14, min(ys) - 14, max(xs) + 14, max(ys) + 14]
        for q in things:                    # what's just outside comes in (once: no creeping along the line)
            if not within(box, q) and box[0] - 10 < q[0] < box[2] + 10 and box[1] - 10 < q[1] < box[3] + 10:
                box = [min(box[0], q[0] - 14), min(box[1], q[1] - 14), max(box[2], q[0] + 14), max(box[3], q[1] + 14)]
        boxes.append(box)
    merged = True
    while merged:                           # boxes that overlap are one
        merged = False
        for i, j in itertools.combinations(range(len(boxes)), 2):
            if overlap(boxes[i], boxes[j]):
                a, b = boxes[i], boxes[j]
                boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                del boxes[j]
                merged = True
                break
    pieces = []
    for b in boxes:                         # a long one cut across its length, each piece down to what's in it
        axis = 0 if b[2] - b[0] >= b[3] - b[1] else 1
        n = math.ceil((b[axis + 2] - b[axis]) / INSET_MOST)
        for k in range(n):
            lo = b[axis] + (b[axis + 2] - b[axis]) * k / n
            hi = b[axis] + (b[axis + 2] - b[axis]) * (k + 1) / n
            mine = [q for q in things if within(b, q) and lo <= q[axis] < hi + (k == n - 1)]
            if len(mine) < 2 and not any(within(b, q) and lo <= q[axis] < hi + (k == n - 1) for q in stuck):
                continue
            xs, ys = [q[0] for q in mine], [q[1] for q in mine]
            piece = [max(b[0], min(xs) - 14), max(b[1], min(ys) - 14), min(b[2], max(xs) + 14),
                     min(b[3], max(ys) + 14)]
            piece[axis], piece[axis + 2] = max(piece[axis], lo), min(piece[axis + 2], hi)
            spare = INSET_NARROWEST / INSET_ZOOMS[0] - (piece[2] - piece[0])
            if spare > 0:
                piece[0], piece[2] = piece[0] - spare / 2, piece[2] + spare / 2
            pieces.append([len(mine), piece])
    apart = False
    while not apart:                        # widened ones that now overlap
        apart = True
        for i, j in itertools.combinations(range(len(pieces)), 2):
            (na, a), (nb, b) = pieces[i], pieces[j]
            if a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]:
                continue
            m = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
            if max(m[2] - m[0], m[3] - m[1]) <= INSET_MOST:
                pieces[i] = [na + nb, m]
                del pieces[j]
            elif min(a[2], b[2]) - max(a[0], b[0]) < min(a[3], b[3]) - max(a[1], b[1]):
                first, second = (a, b) if a[0] < b[0] else (b, a)
                first[2] = second[0] = (first[2] + second[0]) / 2
            else:
                first, second = (a, b) if a[1] < b[1] else (b, a)
                first[3] = second[1] = (first[3] + second[1]) / 2
            apart = False
            break
    return [tuple(p) for p in pieces]


def overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def crowded(view, things, stuck=()):
    """Boxes (map units) round the crowded places of a trial drawing (`view`): where something found no room, or
    CROWD[0] names and boards (`things`: their map points) are within CROWD[1] map units, or something on the map
    found no room at all (`stuck`, a place of its own). The busiest first."""
    count, radius = CROWD
    points = view.failed + list(stuck) + \
        [p for p in things if sum(1 for q in things if math.dist(p, q) < radius) >= count]
    return [b for _, b in sorted(place_boxes(points, things, radius, stuck), key=lambda nb: -nb[0])]


def close_together(things, boxes):
    """Boxes (map units) round the places, outside the `boxes` already found, where CLOSE[0] names and boards
    (`things`) are within CLOSE[1] map units: shown larger too where the page has room. The closest first."""
    count, radius = CLOSE
    points = [p for p in things if not any(within(b, p) for b in boxes) and
              sum(1 for q in things if math.dist(p, q) < radius) >= count]
    found = [(n, b) for n, b in place_boxes(points, things, radius) if not any(overlap(b, o) for o in boxes)]
    return [b for n, b in sorted(found, key=lambda nb: -nb[0] / max((nb[1][2] - nb[1][0]) * (nb[1][3] - nb[1][1]), 1))]


def route_overview(sections, units, key_height, spots=(), stations=(), landmarks=None, streets=None):
    """The first page's map: the sections filling the page, coloured by the lower limit of the two ways, with the
    long sections' numbers, every one of the `stations` (route_stations) named, a board at each of the `spots`
    (slow_spots) and the limit along long stretches; as large as it can be with a corner left empty for the key
    (`key_height` mm tall). Crowded places are framed and drawn again larger, lettered, where the page is empty.
    `streets` (tsw_streets.Streets): roads and towns under it all. Returns (svg, the key's corner: 'tr', 'tl', 'br'
    or 'bl', map units per metre)."""
    if sum(len(s.points) for s in sections) < 2:
        return "", "tr", 1.0
    scale, (ox, oy), (x0, y0), corner, key_box = overview_layout(sections, key_height)
    page = (0, 0, MAP_W, MAP_H)

    def anchor(view, spot):
        return view.at(*spot.section.place((spot.x1 + spot.x2) / 2))
    # a trial drawing finds the crowded places
    trial = MapView(scale, ox, oy, x0, y0, page, strict=True, landmarks=landmarks)
    trial.lines(sections, units)
    trial.take(key_box)
    trial.section_numbers(sections, units)
    trial.names(trial.dots(stations))
    trial.boards(spots, units)
    things = [trial.at(*p) for _, p in stations] + [anchor(trial, p) for p in spots]
    stuck = []
    for _ in range(3):                      # what the map itself has no room for gets a box too
        boxes = crowded(trial, things, stuck)
        boxes += close_together(things, boxes)

        def boxed(p):
            return any(within(b, p) for b in boxes)
        main = MapView(scale, ox, oy, x0, y0, page, landmarks=landmarks)
        main.lines(sections, units)
        main.take(key_box)
        numbers = main.section_numbers(sections, units)
        placed = main.dots(stations)
        for b in boxes:                     # where the frames go: nothing else from outside them
            main.taken.add(tuple(b))
        main.names([(n, q) for n, q in placed if not boxed(q)])
        main.boards([p for p in spots if not boxed(anchor(main, p))], units)
        if not main.stuck:
            break
        stuck += main.stuck
    empty = Occupancy()
    for box in main.drawn + [tuple(b) for b in boxes]:
        empty.add(box)

    defs, insets, framed, shown = [], [], [], []
    for b in boxes:
        letter = chr(ord("A") + len(framed))
        made = inset(sections, units, spots, stations, main, b, empty, letter, landmarks) \
            if len(framed) < INSETS else None
        if made is None:                    # no room for it: its names and boards go on the map after all
            main.taken.remove(tuple(b))
            main.names([(n, q) for n, q in placed if within(b, q)])
            main.boards([p for p in spots if within(b, anchor(main, p))], units)
            continue
        frame, clip, svg, outline = made
        framed.append(frame)
        shown.append(b)
        defs.append(clip)
        insets.append(svg)
        main.take(outline, solid=False)
        empty.add(outline, 6)
        # its frame on the map, with its letter at a free corner
        f0, f1, f2, f3 = frame
        tags = [(f0 - 1, f1 - 14), (f2 - 12, f1 - 14), (f0 - 1, f3 + 1), (f2 - 12, f3 + 1), (f0 - 14, f1),
                (f2 + 1, f1), (f0 - 14, f3 - 13), (f2 + 1, f3 - 13), (f0 - 15, f1 - 15), (f2 + 2, f1 - 15),
                (f0 - 15, f3 + 2), (f2 + 2, f3 + 2), (f0 + 2, f1 + 2), (f2 - 15, f1 + 2), (f0 + 2, f3 - 15),
                (f2 - 15, f3 - 15)]
        tx, ty = next(((x, y) for x, y in tags if main.free((x, y, x + 13, y + 13))), tags[0])
        main.take((tx, ty, tx + 13, ty + 13))
        main.o.append(f'<rect x="{f0:.1f}" y="{f1:.1f}" width="{f2 - f0:.1f}" height="{f3 - f1:.1f}" rx="4" '
                      f'class="frame"/><rect x="{tx:.1f}" y="{ty:.1f}" width="13" height="13" class="insettag"/>'
                      f'<text x="{tx + 6.5:.1f}" y="{ty + 10:.1f}" text-anchor="middle" class="insettagtext small">'
                      f'{letter}</text>')
    main.line_speeds(sections, units, skip=shown)
    main.landmark_names()
    main.draw_numbers([n for n in numbers if not any(within(f, n[0]) for f in framed)])
    under = streets.svg(main, sections, stations) if streets else ""     # last: its names go where there's room
    return (f'<svg viewBox="0 0 {MAP_W:.0f} {MAP_H:.1f}" class="overview full"><defs>{"".join(defs)}</defs>'
            f'{under}{"".join(main.o)}{"".join(insets)}</svg>', corner, scale)


def inset(sections, units, spots, stations, main, box, empty, letter, landmarks=None):
    """The box (map units) of the main map drawn larger where the map is empty (`empty`): the smallest of
    INSET_ZOOMS everything fits at, cut down to what's in it. Returns (its frame on the main map, its clip path,
    its svg, its outline) or None if there's no room for it."""
    centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    world = main.back(box[0], box[1])
    inner = [(n, main.at(*p)) for n, p in stations if within(box, main.at(*p))]
    ends = [n for n, _ in sorted(max(itertools.combinations(inner, 2), key=lambda ab: math.dist(ab[0][1], ab[1][1])),
                                 key=lambda st: st[1][0])] if len(inner) > 1 else [n for n, _ in inner]

    def draw(view):
        view.lines(sections, units)
        view.names(view.dots(stations))
        view.boards(spots, units)
        view.line_speeds(sections, units)
        view.landmark_names()
        return view
    best = None
    for zoom in INSET_ZOOMS + (INSET_LAST,):
        if zoom == INSET_LAST and best:
            break
        w, h = (box[2] - box[0]) * zoom, (box[3] - box[1]) * zoom
        if w > 0.75 * MAP_W or h + INSET_TITLE > 0.97 * MAP_H:
            continue
        view = draw(MapView(main.scale * zoom, 0, 0, world[0], world[1], (0, 0, w, h), landmarks=landmarks))
        # cut down to what's drawn, keeping room for the title and the scale bar
        shown = [d for d in view.drawn if d[2] > 0 and d[0] < w and d[3] > 0 and d[1] < h]
        if not shown:
            continue
        c0, c1 = max(min(d[0] for d in shown) - 6, 0), max(min(d[1] for d in shown) - 6, 0)
        c2, c3 = min(max(d[2] for d in shown) + 6, w), min(max(d[3] for d in shown) + 6, h)
        narrowest = min(INSET_NARROWEST, w)
        if c2 - c0 < narrowest:
            c0 = max(c0 - (narrowest - (c2 - c0)) / 2, 0)
            c2 = min(c0 + narrowest, w)
            c0 = c2 - narrowest
        cw, ch = c2 - c0, c3 - c1
        outer = max(cw, min(text_width(" – ".join(ends), 10) + 30, 400))
        spot = empty.empty(outer, ch + INSET_TITLE + 12, centre)
        if spot is None:
            continue
        fails = (len(view.stuck), len(view.failed))
        if best is None or fails < best[0]:
            best = (fails, zoom, view, (c0, c1, c2, c3), spot, outer)
        if fails[0] == 0 and fails[1] <= 1:
            break
    if best is None:
        return None
    _, zoom, trial, (c0, c1, c2, c3), (ox, py), outer = best
    cw, ch = c2 - c0, c3 - c1
    px = ox + (outer - cw) / 2              # the drawing's left
    top = py + INSET_TITLE
    room = outer - 30                       # the title: both ends, or the first, or as much of it as fits
    title = next((tt for tt in (" – ".join(ends), ends[0] if ends else "") if text_width(tt, 10) <= room), None)
    if title is None:
        title = ends[0]
        while title and text_width(title + "…", 10) > room:
            title = title[:-1]
        title = title.rstrip() + "…"
    corner = trial.back(c0, c1)
    view = draw(MapView(main.scale * zoom, px, top, corner[0], corner[1], (px, top, px + cw, top + ch),
                        landmarks=landmarks))
    # the scale bar: in a free bottom corner, or under the drawing
    bar = max([x for x in (0.1, 0.25, 0.5, 1, 2, 5) if x * units.length * view.scale <= min(cw * 0.35, 90)]
              or [0.1])
    length = bar * units.length * view.scale
    label = f"{bar:g} {units.unit} · ×{zoom:g}"
    need = length + text_width(label, 8) + 10
    spots_for_bar = [(px + cw - need - 4, top + ch - 13), (px + 4, top + ch - 13)]
    sx, sy = next(((x, y) for x, y in spots_for_bar if view.free((x, y, x + need, y + 11))), (None, None))
    height = ch
    if sx is None:
        sx, sy, height = px + cw - need - 4, top + ch, ch + 12
    outline = (ox, py, ox + outer, top + height)
    frame0, frame1 = main.at(*corner), main.at(*view.back(px + cw, top + ch))
    svg = (f'<g class="inset"><rect x="{ox:.1f}" y="{py:.1f}" width="{outer:.1f}" height="{top + height - py:.1f}" '
           f'class="insetbox"/><rect x="{ox:.1f}" y="{py:.1f}" width="{INSET_TITLE:.0f}" '
           f'height="{INSET_TITLE:.0f}" class="insettag"/><text x="{ox + 8:.1f}" y="{py + 12:.1f}" '
           f'text-anchor="middle" class="insettagtext">{letter}</text><text x="{ox + 22:.1f}" y="{py + 12:.1f}" '
           f'class="insettitle">{esc(title)}</text><line x1="{ox + INSET_TITLE:.1f}" x2="{ox + outer:.1f}" '
           f'y1="{top:.1f}" y2="{top:.1f}" class="insetrule"/><g clip-path="url(#clip{letter})">{"".join(view.o)}</g>'
           f'<text x="{sx:.1f}" y="{sy + 8:.1f}" class="insetscale">{esc(label)}</text>'
           f'<rect x="{sx + need - length - 2:.1f}" y="{sy + 5:.1f}" width="{length:.1f}" height="2.5" '
           f'fill="#111"/></g>')
    clip = (f'<clipPath id="clip{letter}"><rect x="{px:.1f}" y="{top:.1f}" width="{cw:.1f}" height="{ch:.1f}"/>'
            f'</clipPath>')
    return (frame0[0], frame0[1], frame1[0], frame1[1]), clip, svg, outline



def map_page(info, sections, units, places, landmarks, on_page, streets=None):
    """The whole-route map's first page under its heading: the map filling it, and its key in the emptiest corner.
    `on_page`: {section number: [pages its strips are on]}, for the key's list of the sections; `streets`
    (tsw_streets.Streets): roads and towns under the map."""
    route_line = f'passenger speed limits in {units.speed_unit}, both ways'
    names = {s.number: section_title(s, sections) for s in sections}
    def pages_of(numbers):
        p = sorted({n for number in numbers for n in on_page[number]})
        return f"{p[0]}" if len(p) == 1 else f"{p[0]}-{p[-1]}"
    rows = []
    long_ones = [s for s in sections if s.length >= units.length]
    short = [s for s in sections if s.length < units.length]
    for s in long_ones:
        first, last = names[s.number]
        rows.append(f'<tr><td class="sec">{s.number}</td><td>{esc(first)} – {esc(last)}</td>'
                    f'<td class="num">{s.length / units.length:.1f}</td><td class="num">{pages_of([s.number])}</td>'
                    f'</tr>')
    if short:                               # numbered on their strips, not on the map
        rows.append(f'<tr><td class="sec"></td><td colspan="2" class="shortsec">{len(short)} short loops and '
                    f'crossovers: {", ".join(str(s.number) for s in short)}</td>'
                    f'<td class="num">{pages_of([s.number for s in short])}</td></tr>')
    total = sum(s.length for s in sections) / units.length
    sub = (f'{route_line} · {len(sections)} sections, {total:.1f} {units.unit} of line · from {info["journeys"]} '
           f'journeys in {info["timetable"]}')
    speed = units.speed_unit
    notes = (f"Boards: where to slow down, to {units.slow} {speed} or less (red edge) or to {units.drop} {speed} "
             "or more below the limit either side. Each gives the lower limit of the two ways on the coloured "
             "stretch it points to; one with an arrow is only for trains going that way. Grey numbers: the limit "
             "along long stretches. A lettered frame is drawn larger in the box with its letter. Pages "
             f"2-{max(n for p in on_page.values() for n in p)} have every limit both ways, with boards, signals "
             "and platforms.")
    titles = [f"{a} – {b}" for s in long_ones for a, b in [names[s.number]]]
    if short:
        titles.append(f"{len(short)} short loops and crossovers: " + ", ".join(str(s.number) for s in short))
    index = (f'<table class="card index"><tr><th></th><th>section</th><th class="num">{units.unit}</th>'
             f'<th class="num">page</th></tr>{"".join(rows)}</table><div class="note">{notes}</div>')
    extra = sum(1.4 + 3.5 * math.ceil(len(t) / 28) for t in titles) + 3.3 * math.ceil(len(notes) / 64)
    # the key's height in mm, roughly (the lines its text takes), for the map to leave room for it
    key_height = 9 + 3.5 * math.ceil(len(sub) / 58) + 10 + 7 + 5.5 + (9 if landmarks else 0) + extra + 9
    overview, corner, scale = route_overview(sections, units, key_height, slow_spots(sections, units),
                                             route_stations(sections, places), landmarks, streets)
    per_mm = MAP_W / MAP_MM[0]
    bar = max([b for b in (0.25, 0.5, 1, 2, 5, 10, 20, 50, 100) if b * units.length * scale / per_mm <= 32]
              or [0.25])
    bar_mm = bar * units.length * scale / per_mm
    return (f'<div class="mapfull">{overview}<div class="key {corner}">'
            f'<div class="keytitle">{esc(info["route"])}</div><div class="keysub">{esc(sub)}</div>'
            f'{legend(units, "line: the lower limit of the two ways", landmark_legend(landmarks))}'
            f'<div class="keyscale"><svg width="{bar_mm:.1f}mm" height="2.4mm" viewBox="0 0 {bar_mm:.1f} 2.4" '
            f'preserveAspectRatio="none"><rect x="0" y="0.6" width="{bar_mm:.1f}" height="1.2" fill="#111"/></svg>'
            f'<span>{bar:g} {units.unit}</span><span class="north">▲ N</span></div>{index}</div></div>')


def render_route(info, sections, units, places=None, landmarks=None, streets=None):
    """The printable document of a whole route. `places`: {station: [(x, y)]} the journeys run past; `landmarks`:
    route_landmarks; `streets` (tsw_streets.Streets): roads and towns under the first page's map."""
    route_line = f'passenger speed limits in {units.speed_unit}, both ways'
    made = datetime.date.today().isoformat()
    names = {s.number: section_title(s, sections) for s in sections}
    known = [v for s in sections for way in (True, False) for _, v in s.limits[way] if v is not None]
    vmax = units.grid * math.ceil(max([units.speed(v) for v in known] + [units.grid]) / units.grid)

    # the line diagram, strips flowing on from page to page
    step = units.row * units.length
    pages, page, used, on_page = [], [], 0.0, collections.defaultdict(list)
    room = 650.0
    for s in sections:
        for k in range(max(1, math.ceil(s.length / step - 1e-6))):
            strip = RouteStrip(s, names[s.number], units, k * step, (k + 1) * step, vmax)
            if page and used + strip.height > room:
                pages.append('<div class="strips">' + "".join(page) + '</div>')
                page, used = [], 0.0
            page.append(strip.svg())
            used += strip.height + 7.5
            if len(pages) + 2 not in on_page[s.number]:
                on_page[s.number].append(len(pages) + 2)
    if page:
        pages.append('<div class="strips">' + "".join(page) + '</div>')

    pages.insert(0, map_page(info, sections, units, places, landmarks, on_page, streets))
    credit = " · roads and towns © OpenStreetMap contributors" if streets and streets.drawn else ""
    head_left = f'<b>{esc(info["route"])}</b> &nbsp;whole route'
    out = [f'<!doctype html><html><head><meta charset="utf-8"><title>{esc(info["route"])} speed limits</title>'
           f'<style>{CSS}{ROUTE_CSS}{tsw_streets.CSS if credit else ""}</style></head><body>']
    strips_line = f'passenger speed limits in {units.speed_unit}: ▶ above the track, ◀ below'
    for n, body in enumerate(pages, 1):
        out.append(f'<section class="page"><div class="head"><span>{head_left}</span>'
                   f'<span>{route_line if n == 1 else strips_line} · page {n} of {len(pages)}</span></div>{body}'
                   f'<div class="foot"><span>Made from the Train Sim World 7 files on {made} (tsw_routemap.py)'
                   f'{credit if n == 1 else ""}</span><span>{esc(info["route"])} · {n}/{len(pages)}</span></div>'
                   f'</section>')
    out.append('</body></html>')
    return "".join(out)


# ---------------------------------------------------------------- choosing a service
class Library:
    """The routes and timetables installed."""

    def __init__(self, files):
        self.files = files
        self.routes = collections.OrderedDict()             # route definition -> [timetable paths]
        for route, timetable in timetables(files):
            self.routes.setdefault(route, []).append(timetable)
        self.names = {r: route_details(files, r)[0] for r in self.routes}
        self._network = (None, None)

    def network(self, route, say=print):
        """A route's track, kept for the route asked for last (reading it takes seconds)."""
        if self._network[0] != route:
            self._network = (None, None)
            say(f"Reading the track of {self.names[route]}...")
            level = route_details(self.files, route)[1]
            self._network = (route, TrackNetwork(self.files, track_tiles(self.files, level)))
        return self._network[1]

    def find_driving(self, timetable_id, service_name):
        """(route, timetable, Service) of the service the game says you're driving, or None. The game names the
        timetable like '/MedwayValley/Map/...:PersistentLevel.SandboxRouteTimetable_2147475787.MVL_Timetable', and
        a service by its name or (when split, 2T02 for 2T02-1 / 2T02-2) its number."""
        name = (timetable_id or "").rsplit(".", 1)[-1]
        for route, timetables in self.routes.items():
            for timetable in timetables:
                if name and timetable.rsplit("/", 1)[-1] == name:
                    table = service_table(self.files, timetable)
                    service = table.get(service_name) or next(
                        (s for s in table.values() if s.number == service_name and s.drivable), None)
                    if service:
                        return route, timetable, service
        return None

    def find_routes(self, text):
        key = re.sub(r"[^a-z0-9]", "", text.lower())
        return [r for r in self.routes
                if key in re.sub(r"[^a-z0-9]", "", (self.names[r] + r).lower())]

    def services(self, route):
        """[(timetable, Service)] of the services you can drive on a route."""
        out, seen = [], set()
        for timetable in self.routes[route]:
            for name, service in service_table(self.files, timetable).items():
                if service.drivable and name not in seen:
                    seen.add(name)
                    out.append((timetable, service))
        return out


def journeys(services):
    """Services grouped by where they start and the stops they make: [((origin, stops), [(timetable, Service)])]."""
    groups = collections.OrderedDict()
    for timetable, service in services:
        stops = journey(service)
        if stops:
            groups.setdefault((service.origin, stops), []).append((timetable, service))
    return sorted(groups.items(), key=lambda g: (g[0][0], g[0][1][-1], -len(g[0][1])))


def main_journeys(groups):
    """The journeys worth a map: those with two stops or more (not depot and other short moves)."""
    return [g for g in groups if len(g[0][1]) >= 2] or groups


def journey_label(group, most=5):
    """'Leeds -> Skipton, 10 stops  (2H68, 2H76 +5)'."""
    (origin, stops), members = group
    names = ", ".join(s.name for _, s in members[:most]) + (f" +{len(members) - most}" if len(members) > most else "")
    return f"{origin or '?'} -> {stops[-1]}, {len(stops)} stop{'s' if len(stops) > 1 else ''}  ({names})"


class MapError(Exception):
    """A map that can't be made, and why."""


def map_folder():
    """Where maps are saved: the folder chosen in the window, or route_maps next to this script."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            folder = json.load(f).get("folder")
    except (OSError, ValueError, AttributeError):
        folder = None
    return os.path.normpath(folder) if isinstance(folder, str) and folder.strip() else OUT_DIR


def set_map_folder(folder):
    """Saves maps in `folder` from now on (None: back to route_maps)."""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"folder": os.path.normpath(folder) if folder else None}, f, indent=2)


def make_map(library, route, timetable, service, also=(), say=print):
    """Writes the map of a service; returns its file. `say` is told how it's going."""
    files = library.files
    instructions = service.instructions
    rows = service_rows(files, timetable, service.name)
    if not rows:
        raise MapError(f"No data track for {service.name} in {timetable.rsplit('/', 1)[-1]}")
    name = library.names[route]
    network = library.network(route, say)
    pieces, gaps = network.follow(rows)
    if not pieces:
        raise MapError(f"Couldn't follow {service.name} over the track of {name}")
    prof = Profile(network, pieces, gaps, stops_along(rows, instructions))
    stops = journey(service)
    timetable_name = tsw_stops._load(files, timetable, {"TimetableName": None}).get("TimetableName")
    info = {"service": service.name, "origin": service.origin or (stops[0] if stops else ""),
            "destination": service.destination or (stops[-1] if stops else ""), "route": name,
            "timetable": timetable_name or timetable.rsplit("/", 1)[-1], "also": [s for s in also if s != service.name]}
    units = Units(imperial_limits(prof.limits))
    path = save_map(f"{name} {service.name} {info['origin']} - {info['destination']}", render(info, prof, units))
    say(f"{len(prof.limits)} limits, {len(prof.signals)} signals, {len(prof.stations)} stations"
        + (f", {len(prof.gaps)} stretch(es) not followed" if prof.gaps else ""))
    return path


def make_route_map(library, route, say=print):
    """Writes the map of a whole route, from the paths of all its journeys; returns its file."""
    files = library.files
    name = library.names[route]
    groups = main_journeys(journeys(library.services(route)))
    network = library.network(route, say)
    say(f"Following {len(groups)} journeys over the track...")
    sections, places, count, timetables_used = journey_sections(files, network, groups, name, say)
    timetable_names = sorted({tsw_stops._load(files, t, {"TimetableName": None}).get("TimetableName")
                              or t.rsplit("/", 1)[-1] for t in timetables_used})
    info = {"route": name, "journeys": count, "timetable": ", ".join(timetable_names)}
    units = route_units(sections)
    say("Finding landmarks...")
    level = route_details(files, route)[1]
    landmarks = route_landmarks(network, scenery(files, level), sections)
    origin = tsw_streets.geo_origin(files, level)
    if origin is None:
        say("No roads and towns on the map: the route's files don't say where in the world it is")
    streets = tsw_streets.Streets(origin, name, say) if origin else None
    path = save_map(f"{name} whole route", render_route(info, sections, units, places, landmarks, streets))
    say(f"{len(sections)} sections, {sum(s.length for s in sections) / units.length:.1f} {units.unit} of line, "
        f"from {count} journeys")
    return path


def journey_sections(files, network, groups, name, say=print):
    """The Sections of journeys (`groups`, as journeys() gives them) over a route's track: each passenger journey
    from its first stop at a platform to its last (depot moves, freight and light engines left out when there are
    passenger journeys). Returns (sections, {station: [(x, y)] where the journeys pass its platforms}, how many
    journeys were followed, the timetables they're in). `name`: the route's, for what can't be done."""
    by_timetable = collections.defaultdict(dict)
    for _, members in groups:
        timetable, service = members[0]
        by_timetable[timetable][service.name] = service
    followed = []                       # [(timetable, pieces, metres of the stops at platforms)]
    for timetable, services in by_timetable.items():
        for service_name, rows in services_rows(files, timetable, list(services)).items():
            pieces = network.follow(rows)[0]
            if not pieces:
                continue
            platforms = path_platforms(network, pieces)
            stops = [pieces[0][0]] + [m for m, _ in stops_along(rows, services[service_name].instructions)]
            followed.append((timetable, pieces, [m for m in stops
                                                 if any(a - 60 <= m <= b + 60 for a, b, _ in platforms)], platforms))
    # passenger journeys: from the first stop at a platform to the last (no depot moves, freight, light engines)
    passenger = [f for f in followed if len(f[2]) >= 2]
    coverage = Coverage(network)
    paths, ends, timetables_used = [], [], set()
    places = collections.defaultdict(list)          # station -> [(x, y)] where the journeys pass its platforms
    for timetable, pieces, stops, platforms in passenger or followed:
        path = coverage.samples(pieces, stops[0] - 300, stops[-1] + 50) if passenger else coverage.samples(pieces)
        if path:
            paths.append(path)
            ends += [path[0], path[-1]]
            timetables_used.add(timetable)
            for a, b, platform in platforms:
                k = bisect.bisect_left(path, (a + b) / 2, key=lambda s: s.m)
                if path[0].m <= (a + b) / 2 <= path[-1].m and "staff halt" not in platform.lower():
                    s = path[min(k, len(path) - 1)]
                    places[station_name(platform)].append((s.x, s.y))
    if not paths:
        raise MapError(f"Couldn't follow any journey over the track of {name}")
    say("Laying out the sections...")
    sections = route_sections(network, paths, coverage, ends)
    if not sections:
        raise MapError(f"The journeys of {name} are all too short to map")
    return sections, places, len(paths), timetables_used


def route_units(sections):
    """Miles and mph, or km and km/h, as the limits of the sections are round in."""
    return Units(imperial_limits([(0, v) for s in sections for way in (True, False)
                                  for _, v in s.limits[way] if v is not None]))


def save_map(file_name, document):
    """Writes a map in the maps folder; returns its file."""
    folder = map_folder()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise MapError(f"Can't save in {folder}: {e.strerror or e}")
    # no characters Windows won't have in a file name, nor the replacement character some names in the files hold
    file_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", file_name).replace(chr(0xFFFD), " ")
    file_name = re.sub(r"\s+", " ", file_name).strip()
    path = os.path.join(folder, file_name + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(document)
    return path


def choose(prompt, count, names=()):
    """A number from 1 to count (returned from 0), or one of `names` as typed; Enter stops."""
    while True:
        answer = input(prompt).strip()
        if not answer:
            raise SystemExit
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        if answer.upper() in names:
            return answer.upper()
        print(f"  pick 1 to {count}" + (" or type a service" if names else ""))


def find_service(services, wanted):
    wanted = wanted.upper()
    return next((s for s in services if s[1].name.upper() == wanted), None) or \
        next((s for s in services if s[1].number.upper() == wanted), None)


def main(args):
    sys.stdout.reconfigure(errors="replace")         # a console that can't show a name's letters
    show = "--no-open" not in args
    args = [a for a in args if not a.startswith("--")]
    print("Reading the game files...")
    library = Library(tsw_paks.GameFiles())
    routes = library.find_routes(args[0]) if args else list(library.routes)
    if not routes:
        raise SystemExit(f"No route matches {args[0]!r}")
    if len(routes) > 1:
        routes.sort(key=lambda r: library.names[r])
        for k, r in enumerate(routes, 1):
            print(f"  {k:3}  {library.names[r]}")
        route = routes[choose("Route number (Enter to stop): ", len(routes))]
    else:
        route = routes[0]
    print(f"{library.names[route]}: reading its timetables...")
    services = library.services(route)
    try:
        if len(args) > 1 and args[1].lower() == "all":
            path = make_route_map(library, route)
        elif len(args) > 1:
            found = find_service(services, args[1])
            if not found:
                raise MapError(f"No service {args[1]} you can drive on {library.names[route]}")
            path = make_map(library, route, *found)
        else:
            groups = journeys(services)
            listed = main_journeys(groups)
            for k, group in enumerate(listed, 1):
                print(f"  {k:3}  {journey_label(group)}")
            if len(listed) < len(groups):
                print(f"  ({len(groups) - len(listed)} shorter moves not listed: type one's service number)")
            print("  all  the whole route, both ways")
            names = {s.name.upper() for _, s in services} | {s.number.upper() for _, s in services} | {"ALL"}
            pick = choose("Journey number, service or all (Enter to stop): ", len(listed), names)
            if pick == "ALL":
                path = make_route_map(library, route)
            elif isinstance(pick, str):
                path = make_map(library, route, *find_service(services, pick))
            else:
                members = listed[pick][1]
                timetable, service = members[0]
                path = make_map(library, route, timetable, service, [s.name for _, s in members])
    except MapError as e:
        raise SystemExit(str(e))
    print(f"Written: {path}")
    if show:
        os.startfile(path)


if __name__ == "__main__":
    main(sys.argv[1:])
