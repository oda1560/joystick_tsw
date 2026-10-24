"""
Tests for tsw_joystick.py where the game doesn't answer: a read that fails now and then must not leave the bridge
without a control (a Class 331 was driven with "no reverser found" after one did).

    python -m unittest discover tests
"""

import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_joystick as core  # noqa: E402


class Flaky:
    """An API whose first `fails` reads raise, as when the game drops a connection."""
    key = "key"

    def __init__(self, fails, value=1.0):
        self.fails, self.value = fails, value

    def get_value(self, path):
        if self.fails > 0:
            self.fails -= 1
            raise ConnectionAbortedError("An established connection was aborted")
        return self.value

    def list(self, path):
        return {}


class LateAnswer(unittest.TestCase):
    """The game stalls for a moment as it saves on arriving at a stop: a request it answers late is asked again
    (one that timed out made the bridge drop the train, and the brake stayed where it was)."""

    def answers(self, *outcomes):
        """The game's answers in turn: an error raised, or the answer's body."""
        calls = []

        def urlopen(request, timeout):
            calls.append(request.full_url)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return io.BytesIO(outcome)

        api = core.TSWApi()
        api.key = "key"
        return api, calls, mock.patch.object(core.urllib.request, "urlopen", urlopen)

    def test_a_late_answer_is_asked_for_again(self):
        api, calls, game = self.answers(TimeoutError("timed out"),
                                        b'{"Result": "Success", "Values": {"InputValue": 0.5}}')
        with game:
            self.assertEqual(api.get_value("CurrentDrivableActor/Throttle.InputValue"), 0.5)
        self.assertEqual(len(calls), 2)

    def test_late_twice_is_no_answer(self):
        api, calls, game = self.answers(TimeoutError("timed out"), TimeoutError("timed out"))
        with game, self.assertRaises(TimeoutError) as raised:
            api.set("CurrentDrivableActor/Throttle.InputValue", 0.5)
        self.assertTrue(core.no_answer(raised.exception))

    def test_an_error_the_game_answers_with_isnt_asked_again(self):
        api, calls, game = self.answers(urllib.error.HTTPError("url", 403, "Forbidden", None, None))
        with game, self.assertRaises(urllib.error.HTTPError) as raised:
            api.get_value("CurrentDrivableActor.ObjectClass")
        self.assertEqual(len(calls), 1)
        self.assertFalse(core.no_answer(raised.exception))

    def test_what_is_no_answer(self):
        for error in (TimeoutError("timed out"), ConnectionAbortedError(10053, "aborted"),
                      urllib.error.URLError("timed out")):
            self.assertTrue(core.no_answer(error), error)
        for error in (urllib.error.HTTPError("url", 404, "Not Found", None, None), ValueError("bad"),
                      IOError("the game listed no controls for the train")):
            self.assertFalse(core.no_answer(error), error)


class Hand:
    """An API where the next `fails` sends aren't answered, keeping what's sent: (what, value)."""
    key = "key"

    def __init__(self, fails):
        self.fails, self.sent = fails, []

    def get_value(self, path):
        return None

    def set(self, path, value):
        if self.fails:
            self.fails -= 1
            raise TimeoutError("timed out")
        self.sent.append((path.rsplit(".", 1)[-1], value))


class HeldInPlace(unittest.TestCase):
    """A control is held in place as a hand would (the game's Interacting flag) once the game has the hold: one it
    didn't answer is sent again with the next press, not taken as done."""

    def test_a_button(self):
        api = Hand(fails=1)
        button = core.PushButton(api, "AWS_Reset")
        with self.assertRaises(TimeoutError):
            button.set(True)
        self.assertFalse(button.held)
        button.set(True)
        self.assertEqual(api.sent, [("Interacting", 1.0), ("InputValue", 1.0)])
        self.assertTrue(button.held)

    def test_a_release_hold_apply_valve(self):
        api = Hand(fails=1)
        valve = core.Lever.__new__(core.Lever)
        valve.api, valve.name, valve.path = api, "TrainBrake", "CurrentDrivableActor/TrainBrake.InputValue"
        valve.positions, valve.held, valve._last_sent = [0.0, 0.5, 1.0], False, (None, 0.0)
        valve.safe_lo, valve.safe_hi, valve.notches = 0.0, 1.0, None
        with self.assertRaises(TimeoutError):
            valve.set_value(1.0)                   # into Apply: held there
        self.assertFalse(valve.held)
        valve.set_value(1.0)
        self.assertEqual(api.sent, [("Interacting", 1.0), ("InputValue", 1.0)])


class LeverWorks(unittest.TestCase):
    def lever(self, api):
        lever = core.Lever.__new__(core.Lever)
        lever.path, lever.api = "CurrentDrivableActor/Reverser.InputValue", api
        return lever

    def test_a_dropped_read_or_two_is_tried_again(self):
        for fails in (0, 1, 2):
            self.assertTrue(self.lever(Flaky(fails)).works(), fails)

    def test_no_answer_at_all(self):
        self.assertFalse(self.lever(Flaky(3)).works())
        self.assertFalse(self.lever(Flaky(0, value=None)).works())


class FakeReverser:
    ok = True

    def __init__(self, fails):
        self.fails, self.moves = fails, []

    def set(self, zone):
        if self.fails:
            self.fails -= 1
            raise ConnectionAbortedError("aborted")
        self.moves.append(zone)

    def position(self):
        return self.moves[-1] if self.moves else "forward"


class Refusing(FakeReverser):
    """A reverser that stays in Forward (master key out, say), and doesn't answer the first `silent` reads of where
    it is."""

    def __init__(self, silent):
        super().__init__(fails=0)
        self.silent = silent

    def position(self):
        if self.silent:
            self.silent -= 1
            raise TimeoutError("timed out")
        return "forward"


class FakeControls:
    def __init__(self, reverser, speed=0.0):
        self.reverser, self._speed = reverser, speed

    def speed(self):
        return self._speed


class Clock:
    """Stands in for the time module, so waits take no real time."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ReverserSync(unittest.TestCase):
    def test_a_failed_move_is_tried_again(self):
        log = []
        sync, controls = core.ReverserSync(log.append), FakeControls(FakeReverser(fails=1))
        sync.update(controls, "forward")           # where the slider is to begin with: nothing moves
        sync.update(controls, "neutral")           # the move fails ...
        self.assertEqual(controls.reverser.moves, [])
        sync.update(controls, "neutral")           # ... and is made next time round
        self.assertEqual(controls.reverser.moves, ["neutral"])
        self.assertTrue(any("trying again" in line for line in log))

    def test_never_while_moving(self):
        sync, controls = core.ReverserSync(lambda msg: None), FakeControls(FakeReverser(fails=0), speed=10.0)
        sync.update(controls, "forward")
        sync.update(controls, "neutral")
        sync.update(controls, "neutral")
        self.assertEqual(controls.reverser.moves, [])

    def test_a_check_the_game_doesnt_answer_is_made_again(self):
        log, clock = [], Clock()
        controls = FakeControls(Refusing(silent=1))
        with mock.patch.object(core, "time", clock):
            sync = core.ReverserSync(log.append)
            sync.update(controls, "forward")
            sync.update(controls, "neutral")       # moved, and checked a moment later ...
            clock.sleep(1.0)
            sync.update(controls, "neutral")       # ... when the game doesn't answer: no error, no verdict
            refused = [line for line in log if line.startswith("Reverser is at")]
            self.assertEqual(refused, [])
            sync.update(controls, "neutral")       # checked again
        refused = [line for line in log if line.startswith("Reverser is at")]
        self.assertEqual(refused, ["Reverser is at forward, not neutral - the train may need the master key in "
                                   "or the brake applied first"])


class Detect(unittest.TestCase):
    def test_no_controls_listed_is_an_error_not_a_train_without_controls(self):
        controls = core.TrainControls(Flaky(0))
        with self.assertRaises(IOError):
            controls.detect()


def named(*places):
    return [{"valueSource": "Output", "valueRange": {"min": v, "max": v}, "displayName": n} for n, v in places]


class Class484(core.TSWApi):
    """The Isle of Wight Class 484's power/brake handle and its TrainBrake, as the game reported them
    (2026-10-11): the TrainBrake has no input identifier and braked nothing when moved."""
    LEVERS = {
        "Throttle (Irregular Lever)": dict(
            ident="Throttle", inputs=(-1, 1), outputs=(-4, 7), notches=12,
            places=named(("Emergency Brake", -4), ("Full Service", -3), ("Braking 2", -2), ("Braking 1", -1),
                         ("Off", 0), *((str(k), k) for k in range(1, 8)))),
        "TrainBrake (Irregular Lever)": dict(
            ident="None", inputs=(0, 1), outputs=(0, 4), notches=5,
            places=named(("Release", 4), ("Braking 1", 3), ("Braking 2", 2), ("Full Service", 1),
                         ("Emergency", 0))),
    }

    def __init__(self, train_brake_ident="None"):
        super().__init__()
        self.key = "key"
        self.levers = {n: dict(v) for n, v in self.LEVERS.items()}
        self.levers["TrainBrake (Irregular Lever)"]["ident"] = train_brake_ident

    def _req(self, method, path, params=None):
        if path == "list/CurrentDrivableActor":
            return {"Result": "Success", "Nodes": [{"Name": n} for n in self.levers]}
        node, _, endpoint = path[len("get/CurrentDrivableActor/"):].partition(".")
        lever = self.levers.get(node)
        if not path.startswith("get/CurrentDrivableActor/") or lever is None:
            return {"Result": "Error"}
        values = {
            "Property.InputIdentifier": {"identifier": lever["ident"]},
            "Property.DisplayInfo": {"namedValues": lever["places"]},
            "Property.bCustomOutputValueMapping": {"Value": False},
            "Property.bInputEnabled": {"Value": True},
            "ObjectClass": {"ObjectClass": "IrregularLeverComponent"},
            "InputValue": {"InputValue": 0.0},
            "Function.GetMinimumInputValue": {"ReturnValue": lever["inputs"][0]},
            "Function.GetMaximumInputValue": {"ReturnValue": lever["inputs"][1]},
            "Function.GetMinimumOutputValue": {"ReturnValue": lever["outputs"][0]},
            "Function.GetMaximumOutputValue": {"ReturnValue": lever["outputs"][1]},
            "Function.GetNotchCount": {"ReturnValue": lever["notches"]},
        }.get(endpoint)
        return {"Result": "Success", "Values": values} if values else {"Result": "Error"}


class Class807(Class484):
    """A power/brake handle (the 484's stands in) beside brake pipe hoses, which the game lists with the controls,
    as on the Class 807: BrakePipe_F has "brake" in its name but isn't a control (an AirHoseComponent). Taken for a
    train brake that didn't answer, it had the 807 looked at again five times on each drive (2026-10-11)."""

    def __init__(self):
        super().__init__()
        del self.levers["TrainBrake (Irregular Lever)"]

    def _req(self, method, path, params=None):
        if path == "list/CurrentDrivableActor":
            return {"Result": "Success",
                    "Nodes": [{"Name": n} for n in [*self.levers, "BrakePipe_F", "BrakePipe_B"]]}
        return super()._req(method, path, params)


class PowerBrakeHandle(unittest.TestCase):
    def detect(self, api):
        controls = core.TrainControls(api)
        controls.detect()
        return controls

    def test_a_brake_hose_is_neither_a_brake_nor_a_control_to_look_for_again(self):
        controls = self.detect(Class807())
        self.assertEqual(controls.throttle.name, "Throttle (Irregular Lever)")
        self.assertIsNone(controls.brake)
        self.assertEqual(controls.missing, [])

    def test_a_brake_the_game_gives_no_input_isnt_used_beside_a_power_brake_handle(self):
        controls = self.detect(Class484())
        self.assertEqual(controls.throttle.name, "Throttle (Irregular Lever)")
        self.assertIsNone(controls.brake)
        throttle = controls.throttle
        self.assertLess(controls.targets(-1.0)[throttle], throttle.neutral)     # stick back: the handle brakes
        self.assertIn("power+brake lever", controls.describe())

    def test_a_train_brake_the_game_drives_is_still_used(self):
        controls = self.detect(Class484(train_brake_ident="TrainBrake"))
        self.assertEqual(controls.brake.name, "TrainBrake (Irregular Lever)")


class KeyedHandle(Class807):
    """The 484's power/brake handle (Off at -0.27, full power at 1) with a keyboard speed, `rate` input a second
    (None: the game doesn't say), kept where it's sent, or where the keyboard moves it (`value`)."""

    def __init__(self, rate):
        super().__init__()
        self.rate, self.value, self.sent = rate, 0.0, []

    def _req(self, method, path, params=None):
        if path.endswith(".Property.DigitalSensitivity"):
            return {"Result": "Success", "Values": {"Value": self.rate}} if self.rate else {"Result": "Error"}
        if path.endswith(".InputValue"):
            return {"Result": "Success", "Values": {"InputValue": self.value}}
        return super()._req(method, path, params)

    def set(self, path, value):
        self.sent.append(value)
        self.value = value


class KeyboardSpeed(unittest.TestCase):
    """The stick moves a handle no faster than its keyboard key moves it in the game, so pushed all the way it
    doesn't make the handle jump there (Class 70 power handle: 0.25 a second, Idle to N8 in 3.1 s)."""

    def setUp(self):
        self.clock = Clock()
        self.clock.sleep = lambda seconds: None             # only the stick's ticks take time here
        patch = mock.patch.object(core, "time", self.clock)
        patch.start()
        self.addCleanup(patch.stop)

    def drive(self, rate, y, seconds):
        """The stick pushed to y and held there `seconds`, the bridge sending 30 times a second as in run()."""
        api = KeyedHandle(rate) if not isinstance(rate, KeyedHandle) else rate
        controls = core.TrainControls(api)
        controls.detect()
        api.value = controls.throttle.neutral
        controls.apply(y)
        for _ in range(round(seconds * 30)):
            self.clock.now += 1 / 30
            if controls.moving():
                controls.apply(y)
        return api, controls

    def test_the_stick_pushed_all_the_way_moves_the_handle_at_its_keyboard_speed(self):
        api, controls = self.drive(0.25, 1.0, 1.0)
        t = controls.throttle
        self.assertTrue(controls.moving())
        self.assertAlmostEqual(t.on_way(1.0), t.neutral + 0.25, places=6)
        self.assertLess(max(api.sent), 0.0)                 # a notch on from Off, not full power
        self.assertAlmostEqual(controls.keyboard_seconds()[0], (t.power_end - t.neutral) / 0.25)

    def test_it_gets_there_in_the_time_the_keyboard_takes_and_is_left_alone_then(self):
        api, controls = self.drive(0.25, 1.0, 5.2)          # 1.27 of the handle at 0.25 a second: 5.1 s
        self.assertFalse(controls.moving())
        self.assertEqual(api.sent[-1], controls.throttle.power_end)
        self.assertEqual(api.sent, sorted(api.sent))        # through the notches in turn, never back

    def test_a_move_starts_from_where_the_keyboard_left_the_handle(self):
        api, controls = self.drive(0.25, 1.0, 6.0)
        t = controls.throttle
        api.value = t.notches[-4]                           # the keyboard took it back three notches
        self.clock.now += 2.0
        controls.apply(0.0)                                 # the stick let go: back to Off from there
        self.assertEqual(api.sent[-1], t.notches[-4])
        self.clock.now += 0.1
        controls.apply(0.0)
        self.assertAlmostEqual(t.on_way(None), t.notches[-4] - 0.025, places=6)
        self.clock.now += 3.0                               # the game stalled: no jump after it
        controls.apply(0.0)
        self.assertAlmostEqual(t.on_way(None), t.notches[-4] - 0.05, places=6)

    def test_without_a_keyboard_speed_the_handle_goes_there_at_once(self):
        api, controls = self.drive(None, 1.0, 0.0)
        self.assertIsNone(controls.keyboard_seconds()[0])
        self.assertEqual(api.sent, [controls.throttle.power_end])
        self.assertFalse(controls.moving())

    def test_keyboard_speed_off_goes_there_at_once(self):
        with mock.patch.object(core, "KEYBOARD_SPEED", False):
            api, controls = self.drive(0.25, 1.0, 0.0)
        self.assertEqual(api.sent, [controls.throttle.power_end])
        self.assertFalse(controls.moving())


class DoorDesk:
    """A cab's door buttons: `off` are switched off, `silent` don't answer."""
    key = "key"
    NAMES = ["DoorRelease_L", "DoorRelease_R", "DoorsClose_L", "DoorsClose_R"]

    def __init__(self, off=(), silent=()):
        self.off, self.silent = set(off), set(silent)

    def _name(self, path):
        return path.split("/")[1].split(".")[0]

    def get_value(self, path):
        n = self._name(path)
        if n in self.silent:
            raise ConnectionAbortedError("aborted")
        if path.endswith(".ObjectClass"):
            return "PushButtonComponent"
        if path.endswith(".bInputEnabled"):
            return n not in self.off
        return None

    def get(self, path):
        if self._name(path) in self.silent:
            raise ConnectionAbortedError("aborted")
        return {"Values": {"componentName": "DriverEnvironment"}}


class KeyedDoorDesk(DoorDesk):
    """A Class 142's door buttons and the DoorKeySwitch they need (or another key by name); `sent` lists what was
    set, but not Interacting."""

    def __init__(self, key_on, key="DoorKeySwitch"):
        super().__init__()
        self.key_name, self.key_value, self.sent = key, 1.0 if key_on else 0.0, []

    def get_value(self, path):
        if path.startswith(f"CurrentDrivableActor/{self.key_name}."):
            return self.key_value
        return super().get_value(path)

    def set(self, path, value):
        if path.endswith(".InputValue"):
            self.sent.append((self._name(path), value))
            if path.startswith(f"CurrentDrivableActor/{self.key_name}."):
                self.key_value = value


class Doors(unittest.TestCase):
    def doors(self, api):
        controls = core.TrainControls(api)
        controls._detect_doors(DoorDesk.NAMES)
        names = {k: getattr(controls, f"door_{k}") for k in ("open_left", "open_right", "close_left", "close_right")}
        return {k: b.name if b else None for k, b in names.items()}, controls.missing

    def test_the_far_side_switched_off_at_a_platform_is_kept(self):
        doors, missing = self.doors(DoorDesk(off={"DoorRelease_R"}))
        self.assertEqual(doors["open_right"], "DoorRelease_R")

    def test_all_switched_off_none_used(self):
        doors, missing = self.doors(DoorDesk(off=set(DoorDesk.NAMES)))
        self.assertEqual(set(doors.values()), {None})

    def test_a_button_that_doesnt_answer_has_the_train_looked_at_again(self):
        doors, missing = self.doors(DoorDesk(silent={"DoorRelease_R"}))
        self.assertIsNone(doors["open_right"])
        self.assertEqual(missing, ["DoorRelease_R"])

    def keyed(self, key_on):
        api = KeyedDoorDesk(key_on)
        controls = core.TrainControls(api)
        controls._detect_doors(DoorDesk.NAMES + ["DoorKeySwitch"])
        return api, controls

    def test_class_142_door_key_turned_on_for_the_press_and_back_off(self):
        """The Class 142 sends Door Open down the train only while its DoorKeySwitch is On (game files)."""
        api, controls = self.keyed(key_on=False)
        controls.door_open_left.set(True)
        self.assertEqual(api.sent, [("DoorKeySwitch", 1.0), ("DoorRelease_L", 1.0)])
        controls.door_open_left.set(False)
        self.assertEqual(api.sent[2:], [("DoorRelease_L", 0.0), ("DoorKeySwitch", 0.0)])
        self.assertIn("(with DoorKeySwitch)", controls.describe())

    def test_a_door_key_already_on_is_left_alone(self):
        api, controls = self.keyed(key_on=True)
        controls.door_close_right.set(True)
        controls.door_close_right.set(False)
        self.assertEqual(api.sent, [("DoorsClose_R", 1.0), ("DoorsClose_R", 0.0)])

    def test_letting_go_turns_the_door_key_back_off(self):
        api, controls = self.keyed(key_on=False)
        controls.door_open_right.set(True)
        controls.let_go()
        self.assertEqual(api.sent[-1], ("DoorKeySwitch", 0.0))

    # the Class 323's door panel buttons, as the game lists them (2026-10-18)
    CLASS_323 = ["DoorControlPanel_Release_1_L", "DoorControlPanel_Release_2_L", "DoorControlPanel_Close_L",
                 "DoorControlPanel_Release_1_R", "DoorControlPanel_Release_2_R", "DoorControlPanel_Close_R",
                 "DoorPanelKeySwitch_L", "DoorPanelKeySwitch_R"]

    def panel_keyed(self, train):
        api = KeyedDoorDesk(key_on=False, key="DoorPanelKeySwitch_L")
        controls = core.TrainControls(api)
        controls.train_id = train
        controls._detect_doors(self.CLASS_323)
        return api, controls

    def test_class_323_door_panel_key_turned_on_for_either_side(self):
        """The Class 323's panel buttons do nothing unless DoorPanelKeySwitch_L is On, for both sides (game files)."""
        api, controls = self.panel_keyed("RVM_MGP_NT_Class323_DMS_A_C")
        controls.door_open_right.set(True)
        self.assertEqual(api.sent, [("DoorPanelKeySwitch_L", 1.0), ("DoorControlPanel_Release_1_R", 1.0),
                                    ("DoorControlPanel_Release_2_R", 1.0)])
        controls.door_open_right.set(False)
        self.assertEqual(api.sent[-1], ("DoorPanelKeySwitch_L", 0.0))
        self.assertIn("(with DoorPanelKeySwitch_L)", controls.describe())

    def test_a_door_panel_key_on_another_train_is_left_alone(self):
        """The LIRR M7 has a DoorPanelKeySwitch_L too; what it does there isn't known."""
        api, controls = self.panel_keyed("RVM_LIRR_M7_Cab_C")
        self.assertIsNone(controls.door_key)
        controls.door_close_left.set(True)
        self.assertEqual(api.sent, [("DoorControlPanel_Close_L", 1.0)])

    def test_class_80x_close_interlock_buttons(self):
        """'Interlock' starts like 'int' (an interior door's button), but it's the desk's door close."""
        controls = core.TrainControls(DoorDesk())
        controls._detect_doors(["DoorRelease_Left(PushButton)", "DoorCloseInterlock_L(PushButton)",
                                "PassengerDoorBL_Close_Interior", "BL_Int_DoorClose_PushButton"])
        self.assertEqual(controls.door_close_left.name, "DoorCloseInterlock_L(PushButton)")


class Class350Vigilance(core.TSWApi):
    """The Class 350's two vigilance controls with the "Alerter" identifier, as the game reported them
    (2026-10-17): VigilancePedal, which the train reacts to, counts backwards (resting at 1, pressed at 0), and
    PushButton_DSD_Pedal is switched off in the game files but was taken for switched on as a save loaded."""

    def __init__(self, dsd_on=True):
        super().__init__()
        self.key = "key"
        self.on = {"VigilancePedal": True, "PushButton_DSD_Pedal": dsd_on}
        self.sent = []                     # (control, what, value)

    def _req(self, method, path, params=None):
        if path == "list/CurrentDrivableActor":
            return {"Result": "Success", "Nodes": [{"Name": n} for n in self.on]}
        verb, _, rest = path.partition("/CurrentDrivableActor/")
        node, _, endpoint = rest.partition(".")
        if node not in self.on:
            return {"Result": "Error"}
        if verb == "set":
            self.sent.append((node, endpoint, float(params["Value"])))
            return {"Result": "Success"}
        lo, hi = (1, 0) if node == "VigilancePedal" else (0, 1)
        values = {
            "Property.InputIdentifier": {"identifier": "Alerter"},
            "Property.bInputEnabled": {"Value": self.on[node]},
            "ObjectClass": {"ObjectClass": "PushButtonComponent"},
            "Function.GetMinimumInputValue": {"ReturnValue": lo},
            "Function.GetMaximumInputValue": {"ReturnValue": hi},
            "Function.GetDefaultInputValue": {"ReturnValue": lo},
        }.get(endpoint)
        return {"Result": "Success", "Values": values} if values else {"Result": "Error"}


class Vigilance(unittest.TestCase):
    def detect(self, api):
        controls = core.TrainControls(api)
        controls.detect()
        return controls

    def test_a_button_the_game_switches_off_after_set_up_isnt_pressed(self):
        api = Class350Vigilance(dsd_on=True)
        controls = self.detect(api)
        self.assertEqual(controls.alerter.name, "PushButton_DSD_Pedal")   # a button that rests up comes first
        api.on["PushButton_DSD_Pedal"] = False
        message = controls.press("alerter", True)
        self.assertIn("VigilancePedal", message)
        controls.press("alerter", False)
        self.assertEqual(api.sent, [("VigilancePedal", "Interacting", 1.0), ("VigilancePedal", "InputValue", 0.0),
                                    ("VigilancePedal", "InputValue", 1.0), ("VigilancePedal", "Interacting", 0.0)])
        self.assertEqual(controls.alerter.name, "VigilancePedal")

    def test_the_button_in_use_stays_while_its_on(self):
        api = Class350Vigilance(dsd_on=False)
        controls = self.detect(api)
        self.assertEqual(controls.alerter.name, "VigilancePedal")
        api.on["PushButton_DSD_Pedal"] = True
        self.assertEqual(controls.press("alerter", True), "Alerter acknowledged")
        controls.press("alerter", False)
        self.assertEqual({node for node, _, _ in api.sent}, {"VigilancePedal"})


if __name__ == "__main__":
    unittest.main()
