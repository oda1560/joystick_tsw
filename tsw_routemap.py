"""
A printable map of the speed limits along a timetabled service, read from Train Sim World's route files, to
learn a route from (like a real driver) instead of relying on the game's speed limit hints.

Each piece of track (a "ribbon") takes its limit from its track rule (e.g. 25 mph), and the route sets other
limits on stretches of it, separately for each direction of travel. A service's path comes from its timetable
data track, which places it on the track every so often; the pieces in between are found through the junctions.
Checked on Airedale-Wharfedale 2S00: the timetable's AI driver never runs faster than these limits.

    python tsw_routemap.py                    choose a route and a service
    python tsw_routemap.py airedale           list a route's services
    python tsw_routemap.py airedale 2S00      write the map of one (route_maps/...html) and open it to print

The limits are those for passenger trains. A board marks where the front of the train meets the limit.
"""

import collections
import datetime
import functools
import heapq
import html
import math
import os
import re
import sys

import tsw_paks
import tsw_stops
from tsw_units import MILE, MPH

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_maps")
DEFAULT_LIMIT = 90 * MPH    # a limit the files leave out is the game's default: the AI runs 90 mph on those
MATCH_SLACK = 5.0           # metres (+3 %) a path between two data track rows may differ from the rows' distance
ROUTE_DEFINITION = re.compile(r"^/[^/]+/RouteDefinition/", re.I)


# ---------------------------------------------------------------- the route's track
class Ribbon:
    __slots__ = ("start_node", "end_node", "length", "limit", "limits", "signals", "platforms", "position")

    def __init__(self, start_node, end_node, length, limit, position):
        self.start_node, self.end_node = start_node, end_node
        self.length = length                        # metres
        self.limit = limit                          # m/s, from the track rule
        self.limits = {True: [], False: []}         # by direction (True: start -> end): [(from, to, m/s)]
        self.signals = {True: [], False: []}        # signals facing that direction: [(at, name)]
        self.platforms = []                         # [(from, to, name)]
        self.position = position                    # (x, y) of its start in metres, x east and y south


class TrackNetwork:
    """The track of a route: its ribbons, which ones meet where, and which way a train can go at a junction."""

    def __init__(self, files, tiles):
        self.ribbons = {}
        self.at_node = collections.defaultdict(list)
        self.allowed = {}                           # junction node -> {(ribbon, ribbon)} a train can pass between
        rules = {}
        for path in tiles:
            self._read_tile(files, path, rules)
        for guid, r in self.ribbons.items():
            self.at_node[r.start_node].append(guid)
            self.at_node[r.end_node].append(guid)
        self.node_position = {r.start_node: r.position for r in self.ribbons.values()}

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
        start = p.get("CachedStartPosition") or {}
        r = Ribbon(p.get("StartNodeGuid"), p.get("EndNodeGuid"),
                   (pkg.properties(curve).get("Length", 0.0) if curve else 0.0) / 100,
                   self._rule_limit(files, p.get("TrackRule"), rules),
                   (start.get("X", 0.0) / 100, start.get("Y", 0.0) / 100))
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
                if str(q.get("MarkerType", "")).endswith("Platform"):
                    r.platforms.append((q.get("Start", 0.0), q.get("End", 1.0),
                                        str(q.get("DisplayName") or q.get("MarkerName") or "")))
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

    def position(self, guid, at):
        r = self.ribbons[guid]
        a, b = self.node_position.get(r.start_node), self.node_position.get(r.end_node)
        if a is None or b is None:
            return None
        return a[0] + (b[0] - a[0]) * at, a[1] + (b[1] - a[1]) * at


class Profile:
    """What a service runs past, in metres from the start of its path."""

    def __init__(self, network, pieces, gaps, stops):
        self.limits, self.signals, self.points, self.gaps = [], [], [], gaps
        platforms = []
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
            for s, e, name in r.platforms:
                if e >= lo and s <= hi:
                    m1, m2 = sorted((metres(max(s, lo)), metres(min(e, hi))))
                    platforms.append((m1, m2, name))
            for at in (a, b):
                xy = network.position(guid, at)
                if xy:
                    self.points.append((metres(at), xy[0], xy[1]))
        self.start = pieces[0][0] if pieces else 0.0
        self.end = max([self.start] + [d + abs(b - a) * network.ribbons[g].length for d, g, a, b in pieces[-1:]])
        self.signals.sort()
        self.gaps = [(a, b) for a, b in gaps if b > self.start and a < self.end]
        self.stations = self._stations(platforms, stops)

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


def service_rows(files, timetable, service):
    """The data track rows of a service, or None."""
    name = timetable.rsplit("/", 1)[-1]
    folder = timetable.rsplit("/", 1)[0] + "/DataTracks/" + name + "_"
    for track in sorted(f[:-len(".uasset")] for f in files.where
                        if f.startswith(folder) and f.endswith("DataTrack.uasset")):
        if not tsw_stops.named_in(files.where[track + ".uasset"].read(track + ".uasset"), service):
            continue
        data = dict(tsw_stops._load(files, track).get("ServiceDataTracks") or [])
        if service in data:
            return data[service].get("TrackData") or []
    return None


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

    def speed(self, v):
        return round(v / MPH) if self.imperial else round(v * 3.6)

    def colour(self, v):
        return next(c for top, c, _ in self.bands if self.speed(v) <= top)


def esc(text):
    return html.escape(str(text), quote=True)


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
* { box-sizing: border-box; }
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
.swatch { width: 6mm; height: 1.4mm; display: inline-block; }
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

    legend = "".join(f'<span><i class="swatch" style="background:{c}"></i>{esc(label)}</span>'
                     for _, c, label in units.bands)
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
        f'<div class="legend">{legend}<span>bold: stops</span></div>'
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
    os.makedirs(OUT_DIR, exist_ok=True)
    file_name = f"{name} {service.name} {info['origin']} - {info['destination']}"
    file_name = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|�\x00-\x1f]+', " ", file_name)).strip()
    path = os.path.join(OUT_DIR, file_name + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(info, prof, units))
    say(f"{len(prof.limits)} limits, {len(prof.signals)} signals, {len(prof.stations)} stations"
        + (f", {len(prof.gaps)} stretch(es) not followed" if prof.gaps else ""))
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
        if len(args) > 1:
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
            names = {s.name.upper() for _, s in services} | {s.number.upper() for _, s in services}
            pick = choose("Journey number or service (Enter to stop): ", len(listed), names)
            if isinstance(pick, str):
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
