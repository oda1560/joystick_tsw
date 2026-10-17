"""
Tests for the roads and towns under the whole-route map (tsw_streets.py), with made-up OpenStreetMap data: where a
latitude and longitude land in a route's world, what's asked for and kept, and what's drawn and where its names go.

    python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_routemap as rm  # noqa: E402
import tsw_streets as st  # noqa: E402
from test_routemap import network, route  # noqa: E402

ORIGIN = (53.8, -1.8)


class Where(unittest.TestCase):
    def test_utm_as_published(self):
        # London (51.5074, -0.1278): UTM zone 30, 699316 E 5710164 N (without the 500 km false easting here)
        e, n = st.utm(51.5074, -0.1278, st.zone_meridian(-0.1278))
        self.assertAlmostEqual(e + 500000, 699316, delta=1)
        self.assertAlmostEqual(n, 5710164, delta=1)

    def test_the_world_is_measured_from_the_routes_centre_x_east_y_south(self):
        world = st.World(ORIGIN)
        self.assertEqual(world.at(*ORIGIN), (0.0, 0.0))
        x, y = world.at(ORIGIN[0] + 0.01, ORIGIN[1] + 0.01)
        self.assertGreater(x, 0)            # east
        self.assertLess(y, 0)               # north is up the page
        lat, lon = world.back(x, y)
        self.assertAlmostEqual(lat, ORIGIN[0] + 0.01, places=7)
        self.assertAlmostEqual(lon, ORIGIN[1] + 0.01, places=7)


class Asking(unittest.TestCase):
    def test_b_roads_and_villages_only_on_a_close_page(self):
        close = st.query((53.7, -2.1, 54.0, -1.5), 35)
        far = st.query((53.7, -2.1, 54.0, -1.5), 90)
        self.assertIn("secondary", close)
        self.assertIn("village", close)
        self.assertNotIn("secondary", far)
        self.assertNotIn("village", far)
        self.assertIn("length() > 840", close)      # built-up areas smaller than SMALLEST units round left out

    def test_kept_and_asked_again_only_when_the_query_changes(self):
        asked = []

        def ask(text):
            asked.append(text)
            return [{"type": "node", "lat": 53.8, "lon": -1.8, "tags": {"place": "town", "name": "Shipley",
                                                                        "wikidata": "Q1"}},
                    {"type": "way", "tags": {"amenity": "bench"}, "geometry": [{"lat": 53.8, "lon": -1.8}]}]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(st, "CACHE", folder):
            first = st.elements("Route", "query 1", say=lambda t: None, ask=ask)
            again = st.elements("Route", "query 1", say=lambda t: None, ask=ask)
            st.elements("Route", "query 2", say=lambda t: None, ask=ask)
        self.assertEqual(asked, ["query 1", "query 2"])
        self.assertEqual(first, again)
        self.assertEqual(first, [{"kind": "place", "tags": {"place": "town", "name": "Shipley"},
                                  "at": [[53.8, -1.8]]}])           # the bench and the other tags left out

    def test_fewer_points_on_a_line_kept_where_it_bends(self):
        pts = [(0, 0), (1, 0.01), (2, 0), (3, 0.02), (4, 0), (4, 5)]
        self.assertEqual(st.straighter(pts), [(0, 0), (4, 0), (4, 5)])


class Drawing(unittest.TestCase):
    def setUp(self):
        """10 km of line east from the route's centre, a station at its middle; OpenStreetMap's railway on it, the
        A650 3 km north, the town of Bingley between, and a village named like the station."""
        net = network(("s", "n1", "n2", (0.0, 0.0), (1.0, 0.0), 10000.0, None, 90))
        net.ribbons["s"].platforms.append((0.5, 0.505, "Middle Platform 1"))
        self.sections = route(net, [(0.0, "s", 0.0, 1.0)], [(0.0, "s", 1.0, 0.0)])
        self.stations = rm.route_stations(self.sections)
        self.world = st.World(ORIGIN)
        self.messages = []
        self.osm = [self.way({"railway": "rail"}, [(-500, 2), (10500, 2)]),
                    self.way({"highway": "primary", "ref": "A650"}, [(-500, -3000), (10500, -3000)]),
                    self.way({"landuse": "residential"}, [(4000, -2500), (6000, -2500), (6000, -1000),
                                                          (4000, -1000), (4000, -2500)]),
                    self.node({"place": "town", "name": "Bingley"}, (5000, -1600)),
                    self.node({"place": "village", "name": "Middleham"}, (5200, 600))]

    def way(self, tags, pts):
        return {"type": "way", "tags": tags,
                "geometry": [dict(zip(("lat", "lon"), self.world.back(x, y))) for x, y in pts]}

    def node(self, tags, p):
        lat, lon = self.world.back(*p)
        return {"type": "node", "tags": tags, "lat": lat, "lon": lon}

    def streets(self, osm=None, ask=None):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(st, "CACHE", folder):
            s = st.Streets(ORIGIN, "Test", self.messages.append, ask or (lambda text: osm or self.osm))
            view = rm.MapView(0.08, 50, 300, -500, -3500, (0, 0, rm.MAP_W, rm.MAP_H))   # 12.5 m a map unit
            for name, at in self.stations:
                view.take((view.at(*at)[0] - 5, view.at(*at)[1] - 5, view.at(*at)[0] + 30, view.at(*at)[1] + 5))
            return s, s.svg(view, self.sections, self.stations), view

    def test_roads_towns_and_numbers_drawn(self):
        s, svg, _ = self.streets()
        self.assertTrue(s.drawn)
        self.assertTrue(svg.startswith('<g class="streets">'))
        for what in ('class="os-rail"', 'class="os-built"', 'class="os-case"', ">A650<", ">BINGLEY<"):
            self.assertIn(what, svg)
        self.assertNotIn("Middleham", svg)        # the station's name says it

    def test_a_name_goes_only_where_theres_room(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(st, "CACHE", folder):
            s = st.Streets(ORIGIN, "Test", self.messages.append, lambda text: self.osm)
            view = rm.MapView(0.08, 50, 300, -500, -3500, (0, 0, rm.MAP_W, rm.MAP_H))
            x, y = view.at(5000, -1600)
            view.take((x - 80, y - 30, x + 80, y + 30))           # the key, say, over the town
            svg = s.svg(view, self.sections, self.stations)
        self.assertNotIn("BINGLEY", svg)
        self.assertIn(">A650<", svg)

    def test_none_where_the_track_isnt_on_openstreetmaps_railways(self):
        shifted = [self.way({"railway": "rail"}, [(-500, 900), (10500, 900)])] + self.osm[1:]
        s, svg, _ = self.streets(shifted)
        self.assertEqual(svg, "")
        self.assertFalse(s.drawn)
        self.assertIn("isn't where OpenStreetMap's railways are", self.messages[-1])

    def test_none_without_the_internet(self):
        def ask(text):
            raise st.StreetError("OpenStreetMap couldn't be reached (timed out)")
        s, svg, _ = self.streets(ask=ask)
        self.assertEqual((svg, s.drawn), ("", False))
        self.assertIn("couldn't be reached", self.messages[-1])

    def test_under_the_first_page_with_the_credit(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(st, "CACHE", folder):
            s = st.Streets(ORIGIN, "Test", self.messages.append, lambda text: self.osm)
            units = rm.Units(True)
            page = rm.render_route({"route": "Test", "journeys": 2, "timetable": "Test"}, self.sections, units,
                                   streets=s)
        svg = page[page.index('<svg viewBox="0 0 1000'):]
        self.assertTrue(svg[svg.index("</defs>") + 7:].startswith('<g class="streets">'))
        self.assertEqual(page.count("© OpenStreetMap contributors"), 1)
        self.assertIn(".os-built", page)

    def test_no_credit_when_none_were_drawn(self):
        def ask(text):
            raise st.StreetError("no internet")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(st, "CACHE", folder):
            s = st.Streets(ORIGIN, "Test", self.messages.append, ask)
            page = rm.render_route({"route": "Test", "journeys": 2, "timetable": "Test"}, self.sections,
                                   rm.Units(True), streets=s)
        self.assertNotIn("OpenStreetMap", page)
        self.assertNotIn('class="streets"', page)


if __name__ == "__main__":
    unittest.main()
