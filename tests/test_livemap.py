"""
Tests for the route map over the game (tsw_livemap.py and its window, MapOverlay in tsw_joystick_ui.py): where the
game's latitude and longitude are in a route's world, which map is shown, how the window follows you and which
pieces of the map are drawn first.

    python -m unittest discover tests
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_joystick_ui as ui  # noqa: E402
import tsw_livemap as lm  # noqa: E402

BRITAIN = (51.523373, -0.681496)        # the central point Airedale-Wharfedale and many British routes share
# read from the game on Airedale-Wharfedale 2H26: (latitude, longitude, the tile the game said), each within a metre
# of a tile's edge; the last two 1.2 m apart, either side of one
READ = [(53.938099556204236, -2.0225559504840613, (-97, -267)),
        (53.91087613789782, -2.00527707106186, (-95, -263)),
        (53.9470682997199, -2.020800866768216, (-97, -267)),
        (53.91099699617354, -2.005442352954831, (-96, -264)),
        (53.91098557951146, -2.0054267212154535, (-96, -263))]


class WhereYouAre(unittest.TestCase):
    def test_the_tile_the_game_says(self):
        for lat, lon, tile in READ:
            self.assertEqual(lm.tile(*lm.world(lat, lon, BRITAIN)), tile)

    def test_measured_from_the_central_point_x_east_y_south(self):
        self.assertEqual(lm.world(*BRITAIN, BRITAIN), (0.0, 0.0))
        x, y = lm.world(BRITAIN[0] + 0.01, BRITAIN[1], BRITAIN)
        self.assertAlmostEqual(y, -1113, delta=2)           # north is up: y falls
        self.assertAlmostEqual(x, 0, delta=40)              # (UTM's north leans 1.8° from true north here)
        x, y = lm.world(BRITAIN[0], BRITAIN[1] + 0.01, BRITAIN)
        self.assertAlmostEqual(x, 694, delta=2)             # east: x grows, shorter degrees up here
        self.assertAlmostEqual(y, -22, delta=2)

    def test_the_zone_of_the_central_point(self):
        self.assertEqual(lm.zone_meridian(-0.681496), -3)   # Britain's shared point, though Airedale is in it too
        self.assertEqual(lm.zone_meridian(12.930972), 15)   # Dresden-Riesa
        self.assertEqual(lm.zone_meridian(-117.554817), -117)

    def test_the_level_and_the_name_a_map_is_kept_under(self):
        tid = ("/AiredaleWharfedaleLines/Map/AiredaleWharfedaleLines.AiredaleWharfedaleLines:PersistentLevel."
               "SandboxRouteTimetable_2147476370.AiredaleWharfedale_TT")
        self.assertEqual(lm.level_of(tid), "/AiredaleWharfedaleLines/Map/AiredaleWharfedaleLines")
        self.assertEqual(lm.map_key(tid, "2H26", 8), "AiredaleWharfedaleLines AiredaleWharfedale_TT 2H26 z8")
        self.assertEqual(lm.map_key(tid, None, 12.0),
                         "AiredaleWharfedaleLines AiredaleWharfedale_TT whole route z12")
        self.assertEqual(lm.map_key(tid, "1A/2", 8), "AiredaleWharfedaleLines AiredaleWharfedale_TT 1A 2 z8")


def a_map(**more):
    """A map's meta: 8 m a pixel, its top left at (0, 0) in the world, 10 pieces wide and 4 high, its line along
    y = 1000 m (125 px) from x = 0 to 60 km."""
    meta = {"version": lm.MAP_VERSION, "key": "R TT 2H26 z8", "title": "2H26  Leeds → Skipton", "origin": BRITAIN,
            "left": 0.0, "top": 0.0, "zoom": 8, "size": [10 * lm.CHUNK, 4 * lm.CHUNK], "unit": "mi",
            "unit_m": 1609.344, "line": [(x * 100.0, 1000.0) for x in range(601)], "scenery": True, "stamp": 5}
    meta.update(more)
    return meta


class FindingTheMap(unittest.TestCase):
    TARGET = ("/R/Map/R.R:PersistentLevel.T_1.TT", "2H26")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(lm, "CACHE", self.dir.name)
        self.patch.start()
        self.watch = lm.MapWatch()
        self.watch.target, self.watch.wanted = self.TARGET, True
        self.started = []
        self.watch._start = lambda target, key: self.started.append(key)

    def tearDown(self):
        self.watch.running = False
        self.patch.stop()
        self.dir.cleanup()

    def keep(self, scenery, zoom=8, pieces=()):
        key = lm.map_key(*self.TARGET, zoom)
        meta = a_map(key=key, scenery=scenery, zoom=zoom)
        with open(lm.meta_file(key), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        for cell in pieces:
            open(lm.chunk_file(meta, cell), "wb").close()
        return key

    def test_a_kept_map_is_shown_without_making_it(self):
        self.keep(True, pieces=[(0, 0), (3, 1)])
        self.watch._find_map()
        self.assertEqual((self.watch.map["title"], self.watch.done), ("2H26  Leeds → Skipton", {(0, 0), (3, 1)}))
        self.assertEqual(self.started, [])

    def test_a_map_kept_without_its_scenery_is_shown_while_its_made(self):
        key = self.keep(False)
        self.watch._find_map()
        self.assertIsNotNone(self.watch.map)
        self.assertEqual(self.started, [key])

    def test_another_zoom_is_made_and_the_last_one_shown_till_then(self):
        self.keep(True, zoom=12)
        self.watch.zoom = 12
        self.watch._find_map()
        self.watch.zoom = 8
        self.watch._find_map()
        self.assertEqual(self.watch.map["zoom"], 12)
        self.assertEqual(self.started, [lm.map_key(*self.TARGET, 8)])

    def test_nothing_is_made_until_the_map_is_asked_for(self):
        self.watch.wanted = False
        self.watch._find_map()
        self.assertEqual(self.started, [])

    def test_a_map_that_couldnt_be_made_isnt_tried_again(self):
        self.watch.failed[lm.map_key(*self.TARGET, 8)] = "No timetable"
        self.watch._find_map()
        self.assertEqual((self.started, self.watch.status), ([], "No timetable"))

    def test_no_timetable_no_map(self):
        self.keep(True)
        self.watch._find_map()
        self.watch.target = None
        self.watch._find_map()
        self.assertIsNone(self.watch.map)
        self.assertIn("Drive a service", self.watch.status)


class Following(unittest.TestCase):
    """Where the window's middle goes, and which pieces of the map are drawn first."""

    def setUp(self):
        self.watch = lm.MapWatch()
        self.watch.view = (700, 476)
        self.watch._set_map(a_map())

    def tearDown(self):
        self.watch.running = False

    def test_the_middle_is_ahead_of_you(self):
        self.watch._place((8000.0, 1000.0))
        self.assertEqual(self.watch.centre, (1000.0, 125.0))      # not moved yet: on you
        self.watch._place((8800.0, 1000.0))                         # going east
        self.assertEqual(self.watch.heading, (1.0, 0.0))
        self.assertAlmostEqual(self.watch.centre[0], 1100 + 700 * lm.MapWatch.AHEAD)

    def test_the_pieces_in_view_first_then_ahead_then_along_the_line(self):
        self.watch._place((30000.0, 1000.0))
        self.watch._place((31000.0, 1000.0))                         # 3875 px across: piece 5
        todo = self.watch.wanted_pieces()
        self.assertEqual(set(todo[:2]), {(5, 0), (4, 0)})            # the window, at the top of the map
        self.assertIn((6, 0), todo[2:4])                             # then ahead (east)
        self.assertEqual(todo[-1], (0, 0))                           # the far end of the line last
        self.assertNotIn((5, 3), todo)                               # nowhere near the line
        self.watch.done.add((5, 0))
        self.assertNotIn((5, 0), self.watch.wanted_pieces())

    def test_far_from_the_line_its_not_this_map(self):
        self.watch._place((30000.0, 9000.0))
        self.assertTrue(self.watch.off_line)
        self.watch._place((30000.0, 1200.0))
        self.assertFalse(self.watch.off_line)


class Canvas:
    """Enough of a Tk canvas to see where things are drawn."""

    def __init__(self):
        self.places, self.states, self.texts, self.items = {}, {}, {}, 100

    def coords(self, item, *xy):
        self.places[item] = xy

    def itemconfigure(self, item, state=None, text=None):
        if state is not None:
            self.states[item] = state
        if text is not None:
            self.texts[item] = text

    def itemcget(self, item, option):
        return self.texts.get(item, "")

    def create_image(self, x, y, anchor=None, image=None):
        self.items += 1
        return self.items

    def tag_lower(self, item):
        pass

    def delete(self, item):
        self.places.pop(item, None)


class TheWindow(unittest.TestCase):
    def setUp(self):
        self.overlay = SimpleNamespace(canvas=Canvas(), k=1.0, map=a_map(), view=None, size=(700, 476), pieces={},
                                       arrow=1, under=2, dot=3, note=99, EASE=ui.MapOverlay.EASE)
        self.watch = SimpleNamespace(position=(8000.0, 1000.0), heading=(1.0, 0.0), off_line=False,
                                     draw_failed=None, centre=(1100.0, 125.0), done=set())

    def show(self):
        o, w = self.overlay, self.watch
        ui.MapOverlay._follow(o, w.centre)
        with mock.patch.object(ui.tk, "PhotoImage", lambda file: file):
            missing = ui.MapOverlay._pieces(o, w.done)
        ui.MapOverlay._mark(o, w, missing)
        return o.canvas

    def test_the_view_jumps_to_you_then_follows_smoothly(self):
        self.show()
        self.assertEqual(self.overlay.view, (750.0, -113.0))
        self.watch.centre = (1200.0, 125.0)
        self.show()
        self.assertAlmostEqual(self.overlay.view[0], 750 + 100 * ui.MapOverlay.EASE)

    def test_the_dot_where_you_are_and_the_arrow_ahead(self):
        c = self.show()
        x0, y0, x1, y1 = c.places[3]
        self.assertEqual(((x0 + x1) / 2, (y0 + y1) / 2), (1000 - 750, 125 + 113))
        self.assertGreater(c.places[1][0], (x0 + x1) / 2)               # the arrow's tip east of it
        self.assertEqual([c.states[i] for i in (1, 2, 3)], ["normal"] * 3)

    def test_the_pieces_in_view_one_at_a_time_and_a_note_till_drawn(self):
        c = self.show()
        self.assertEqual(c.texts[99], "Drawing this part of the map...")
        self.watch.done = {(0, 0), (1, 0)}
        self.show()
        self.show()
        self.assertEqual(sorted(self.overlay.pieces), [(0, 0), (1, 0)])
        image, item, stamp = self.overlay.pieces[(1, 0)]
        self.assertEqual(c.places[item], (768 - round(self.overlay.view[0]), -round(self.overlay.view[1])))
        self.assertEqual(c.texts[99], "")

    def test_not_on_the_line_no_dot(self):
        self.watch.off_line = True
        c = self.show()
        self.assertEqual(c.states[3], "hidden")
        self.assertIn("not on this map", c.texts[99])


class ShowingIt(unittest.TestCase):
    def test_button_or_key_shows_and_hides_it(self):
        app = SimpleNamespace(map_shown=False, map_watch=SimpleNamespace(shown=False, wanted=False, last_check=5.0))
        ui.App._toggle_map(app)
        self.assertEqual((app.map_shown, app.map_watch.shown, app.map_watch.wanted), (True, True, True))
        ui.App._toggle_map(app)
        self.assertEqual((app.map_shown, app.map_watch.shown, app.map_watch.wanted), (False, False, True))

    def test_the_window_builds_with_its_settings(self):
        """The bridge window's settings, built with stand-ins for Tk (no window opens)."""
        app = ui.App.__new__(ui.App)
        app.root = mock.MagicMock()
        app.root.winfo_screenwidth.return_value, app.root.winfo_screenheight.return_value = 2560, 1440
        app.s, app.k, app.hs_train, app._hs_quiet = dict(ui.DEFAULTS), 1.0, None, False
        app.follow, app.bridge = SimpleNamespace(seconds=0.0), SimpleNamespace(train=None)
        app.map_watch = SimpleNamespace(view=None, zoom=8, last_check=5.0)
        app.map_overlay = mock.Mock()
        app.log = mock.Mock()
        with mock.patch.object(ui, "tk"), mock.patch.object(ui, "ttk"), mock.patch.object(ui, "save_settings"):
            app._build()
            app.on_map_size(0.3)
            app.on_map_size_done()
            app.on_map_zoom(4.2)
            app.on_map_zoom_done()
            app.map_corner_combo.get.return_value = "Top left"
            app.on_map_corner()
            app.on_map_opacity(0.6)
            app.map_key_combo.get.return_value = "F6"            # the next stop's: that one gives it up
            app.on_map_key()
        self.assertEqual((app.s["map_key"], app.s["stop_key"]), ("F6", None))
        self.assertEqual(app.map_watch.view, (768, 520))
        app.map_overlay.resize.assert_called_with((768, 520))
        self.assertEqual((app.map_watch.zoom, app.map_watch.last_check), (20, 0.0))
        self.assertEqual((app.s["map_opacity"], app.map_overlay.corner), (0.6, "Top left"))

    def test_its_key_is_f7_unless_the_next_stop_has_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            for saved, kept in (({}, "F7"), ({"map_key": "F10"}, "F10"), ({"map_key": "F6"}, None),
                                ({"map_key": "Q"}, None)):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(saved, f)
                with mock.patch.object(ui, "SETTINGS_FILE", path):
                    s = ui.load_settings()
                self.assertEqual((s["map_key"], s["map_button"]), (kept, 11))

    def test_a_zoom_or_corner_it_doesnt_have_goes_back_to_its_own(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"map_zoom": 7, "map_corner": "Somewhere", "map_window": 2}, f)
            with mock.patch.object(ui, "SETTINGS_FILE", path):
                s = ui.load_settings()
        self.assertEqual((s["map_zoom"], s["map_corner"], s["map_window"]), (lm.ZOOM, "Bottom left", 0.6))


if __name__ == "__main__":
    unittest.main()
