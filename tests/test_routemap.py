"""
Tests for the whole-route map in tsw_routemap.py, on small made-up track: where a piece of track runs, which way
each limit is drawn, the limits no train can use left out, and where the first page puts a board to slow down.

    python -m unittest discover tests
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_routemap as rm  # noqa: E402
from tsw_units import MPH  # noqa: E402


def network(*ribbons):
    """A TrackNetwork of ribbons given as (guid, start node, end node, start (x, y), direction (x, y), length,
    radius or None, limit in mph)."""
    net = rm.TrackNetwork(None, [])
    for guid, n1, n2, start, tangent, length, radius, limit in ribbons:
        r = rm.Ribbon(n1, n2, length, limit * MPH, start)
        r.tangent, r.radius = tangent, radius
        net.ribbons[guid] = r
        net.at_node[n1].append(guid)
        net.at_node[n2].append(guid)
    net.node_position = {r.start_node: r.position for r in net.ribbons.values()}
    net._shape()
    return net


def mph(limits):
    return [(round(x), round(v / MPH) if v else None) for x, v in limits]


def route(net, *pieces):
    """The Sections of journeys over `net`, each given as its path's pieces."""
    coverage = rm.Coverage(net)
    paths = [coverage.samples(p) for p in pieces]
    ends = [s for p in paths for s in (p[0], p[-1])]
    return rm.route_sections(net, paths, coverage, ends)


class Shape(unittest.TestCase):
    def test_a_curve_ends_where_the_next_piece_starts(self):
        # a quarter circle of 100 m radius from (0, 0) heading east, the next piece starting at (100, 100)
        net = network(("a", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 157.08, None, 50),
                      ("b", "n2", "n3", (100.0, 100.0), (0.0, 1.0), 50.0, None, 50))
        x, y = net.position("a", 1.0)
        self.assertAlmostEqual(x, 100.0, places=3)
        self.assertAlmostEqual(y, 100.0, places=3)
        x, y = net.position("a", 0.5)
        self.assertAlmostEqual(x, 100 * math.sin(math.pi / 4), places=2)
        self.assertAlmostEqual(y, 100 - 100 * math.cos(math.pi / 4), places=2)
        hx, hy = net.heading("a", 1.0)
        self.assertAlmostEqual(hx, 0.0, places=3)
        self.assertAlmostEqual(hy, 1.0, places=3)
        self.assertAlmostEqual(net.ribbons["a"].arc, 50 * math.pi, places=2)

    def test_an_end_no_piece_starts_at_follows_the_radius(self):
        # the files' radius has the opposite sign to the way it bends here
        net = network(("a", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 50 * math.pi, -100.0, 50))
        x, y = net.position("a", 1.0)
        self.assertAlmostEqual(x, 100.0, places=3)
        self.assertAlmostEqual(y, 100.0, places=3)


class UsableLimits(unittest.TestCase):
    def test_a_short_rise_is_left_out(self):
        limits = [(0, 50 * MPH), (100, 90 * MPH), (108, 50 * MPH), (500, 70 * MPH)]
        self.assertEqual(mph(rm.usable_limits(limits, True, 1000)), [(0, 50), (500, 70)])

    def test_a_short_drop_stays(self):
        limits = [(0, 30 * MPH), (100, 20 * MPH), (121, 50 * MPH)]
        self.assertEqual(mph(rm.usable_limits(limits, True, 1000)), [(0, 30), (100, 20), (121, 50)])

    def test_rises_are_judged_the_way_trains_run(self):
        # running down x the 8 m of 90 comes after the 50 beyond it, so it can't be used; running up x it
        # comes after 20, the same
        limits = [(0, 20 * MPH), (100, 90 * MPH), (108, 50 * MPH)]
        self.assertEqual(mph(rm.usable_limits(limits, False, 1000)), [(0, 20), (100, 50)])
        self.assertEqual(mph(rm.usable_limits(limits, True, 1000)), [(0, 20), (108, 50)])


class Sections(unittest.TestCase):
    def sections(self, net, *pieces):
        return route(net, *pieces)

    def test_double_track_is_one_section_with_both_lines(self):
        # eastbound line at y = 0: 40 mph, 60 on its second half; westbound line 4 m away: 30 mph
        net = network(("east", "e1", "e2", (0.0, 0.0), (1.0, 0.0), 2000.0, None, 40),
                      ("west", "w1", "w2", (2000.0, 4.0), (-1.0, 0.0), 2000.0, None, 30))
        net.ribbons["east"].limits[True].append((0.5, 1.0, 60 * MPH))
        sections = self.sections(net, [(0.0, "east", 0.0, 1.0)], [(0.0, "west", 0.0, 1.0)])
        self.assertEqual(len(sections), 1)
        s = sections[0]
        self.assertAlmostEqual(s.length, 1990, delta=1)
        self.assertEqual(mph(s.limits[True]), [(0, 40), (995, 60)])
        self.assertEqual(mph(s.limits[False]), [(0, 30)])

    def test_single_track_shows_the_limits_each_way(self):
        # 50 mph, but 20 over its first half when running back towards its start
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 2000.0, None, 50))
        net.ribbons["s"].limits[False].append((0.0, 0.5, 20 * MPH))
        sections = self.sections(net, [(0.0, "s", 0.0, 1.0)], [(0.0, "s", 1.0, 0.0)])
        self.assertEqual(len(sections), 1)
        s = sections[0]
        self.assertEqual(mph(s.limits[True]), [(0, 50)])
        self.assertEqual(mph(s.limits[False]), [(0, 20), (995, 50)])

    def test_turned_round_the_two_ways_swap(self):
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 2000.0, None, 50))
        net.ribbons["s"].limits[False].append((0.0, 0.5, 20 * MPH))
        s = self.sections(net, [(0.0, "s", 0.0, 1.0)], [(0.0, "s", 1.0, 0.0)])[0]
        s.flip()
        self.assertEqual(mph(s.limits[True]), [(0, 50), (995, 20)])
        self.assertEqual(mph(s.limits[False]), [(0, 50)])

    def test_a_branch_starts_where_it_leaves_the_line(self):
        # a 2 km line east, and a 1 km branch leaving its end to the north-east: the branch is section 2,
        # joining section 1 at its end
        net = network(("main", "m1", "m2", (0.0, 0.0), (1.0, 0.0), 2000.0, None, 70),
                      ("branch", "m2", "b2", (2000.0, 0.0), (math.sqrt(0.5), -math.sqrt(0.5)), 1000.0, None, 40))
        sections = self.sections(net, [(0.0, "main", 0.0, 1.0)],
                                 [(0.0, "main", 0.5, 1.0), (1000.0, "branch", 0.0, 1.0)])
        self.assertEqual(len(sections), 2)
        main, branch = sections
        self.assertEqual(branch.joins[0][0], 1)
        self.assertAlmostEqual(branch.joins[0][1], main.length, delta=15)
        self.assertEqual(mph(branch.limits[True]), [(0, 40)])
        self.assertIn(2, [n for _, n in main.links])



class SlowSpots(unittest.TestCase):
    def spots(self, *limits):
        """The slow spots of 3 km of single track at 90 mph, east from (0, 0), run both ways, with `limits` given
        as (start, end as fractions, mph, which way: True east, False west, None both)."""
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 3000.0, None, 90))
        for start, end, limit, way in limits:
            for forward in (True, False):
                if way in (None, forward):
                    net.ribbons["s"].limits[forward].append((start, end, limit * MPH))
        sections = route(net, [(0.0, "s", 0.0, 1.0)], [(0.0, "s", 1.0, 0.0)])
        return rm.slow_spots(sections, rm.Units(True))

    def test_a_low_limit_both_ways_is_a_board_for_both(self):
        spots = self.spots((0.4, 0.5, 40, None))
        self.assertEqual([round(p.limit / MPH) for p in spots], [40])
        self.assertAlmostEqual(spots[0].x2 - spots[0].x1, 300, delta=15)
        self.assertIsNone(spots[0].heading)

    def test_a_limit_low_one_way_points_that_way(self):
        spots = self.spots((0.4, 0.5, 30, True))
        self.assertEqual([round(p.limit / MPH) for p in spots], [30])
        hx, hy = spots[0].heading
        self.assertAlmostEqual(hx, 1.0, places=2)
        self.assertAlmostEqual(hy, 0.0, places=2)

    def test_a_big_drop_is_a_slow_spot_and_a_small_one_isnt(self):
        spots = self.spots((0.2, 0.4, 75, None), (0.6, 0.8, 60, None))
        self.assertEqual([round(p.limit / MPH) for p in spots], [60])

    def test_the_same_limit_close_by_is_one_board(self):
        # 40, 150 m of 50, 40: one board for the 40s; the 50 between isn't slow (over 45, higher than either side)
        spots = self.spots((0.40, 0.45, 40, None), (0.45, 0.50, 50, None), (0.50, 0.55, 40, None))
        self.assertEqual([round(p.limit / MPH) for p in spots], [40])



class Stations(unittest.TestCase):
    def test_names_are_tidied(self):
        self.assertEqual(rm.station_name("Coryton Station Platform 1"), "Coryton")
        self.assertEqual(rm.station_name("Westbourne Park Westbound"), "Westbourne Park")
        self.assertEqual(rm.station_name("Smethwick Rolfe Street P2"), "Smethwick Rolfe Street")
        self.assertEqual(rm.station_name("Station Road"), "Station Road")

    def test_one_station_spelt_two_ways_is_one(self):
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 3000.0, None, 90))
        net.ribbons["s"].platforms.append((0.5, 0.52, "Schlüchtern Gleis 1"))
        sections = route(net, [(0.0, "s", 0.0, 1.0)])
        stations = rm.route_stations(sections, {"Schluechtern": [(1540.0, 5.0)], "Elsewhere": [(2900.0, 0.0)]})
        self.assertEqual([n for n, _ in stations], ["Elsewhere", "Schlüchtern"])

    def test_the_numbered_platforms_of_one_station_are_one(self):
        named = [("Stafford 1", (0, 0)), ("Stafford 3", (30, 0)), ("Heathrow Terminal 4", (0, 100)),
                 ("Heathrow Terminal 5", (5000, 100))]
        out = rm.numbered_twins(named, lambda p, q: math.dist(p, q) < 400)
        self.assertEqual([n for n, _ in out], ["Stafford", "Stafford", "Heathrow Terminal 4", "Heathrow Terminal 5"])

    def test_a_station_off_the_drawn_track_is_named_too(self):
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 3000.0, None, 90))
        net.ribbons["s"].platforms.append((0.1, 0.12, "Alpha Platform 1"))
        sections = route(net, [(0.0, "s", 0.0, 1.0)])
        stations = rm.route_stations(sections, {"Beta": [(1500.0, 40.0), (1500.0, 44.0)]})
        self.assertEqual([n for n, _ in stations], ["Alpha", "Beta"])
        self.assertEqual(stations[1][1], (1500.0, 42.0))


class FirstPage(unittest.TestCase):
    def page(self, crowded):
        """The first page's map of 40 km of line east, with 3 stations along it and (`crowded`) 9 more in its
        first 1.5 km."""
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 40000.0, None, 90))
        names = [f"Far {c}" for c in "ABC"]
        net.ribbons["s"].platforms += [(0.3 + 0.25 * k, 0.302 + 0.25 * k, name) for k, name in enumerate(names)]
        if crowded:
            near = [f"Near {c}" for c in "ABCDEFGHI"]
            net.ribbons["s"].platforms += [(0.004 + 0.004 * k, 0.005 + 0.004 * k, n) for k, n in enumerate(near)]
            names += near
        sections = route(net, [(0.0, "s", 0.0, 1.0)], [(0.0, "s", 1.0, 0.0)])
        units = rm.Units(True)
        svg, _, _ = rm.route_overview(sections, units, 60, rm.slow_spots(sections, units),
                                      rm.route_stations(sections))
        return svg, names

    def test_a_crowded_place_is_drawn_again_larger(self):
        svg, names = self.page(True)
        self.assertEqual(svg.count('class="insetbox"'), 1)
        self.assertIn('class="frame"', svg)
        for name in names:
            self.assertIn(f">{name}<", svg)

    def test_an_uncrowded_line_has_no_inset(self):
        svg, names = self.page(False)
        self.assertNotIn('class="insetbox"', svg)
        for name in names:
            self.assertIn(f">{name}<", svg)


if __name__ == "__main__":
    unittest.main()
