"""
Tests for tsw_joystick.py where the game doesn't answer: a read that fails now and then must not leave the bridge
without a control (a Class 331 was driven with "no reverser found" after one did).

    python -m unittest discover tests
"""

import os
import sys
import unittest

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


class FakeControls:
    def __init__(self, reverser, speed=0.0):
        self.reverser, self._speed = reverser, speed

    def speed(self):
        return self._speed


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


class PowerBrakeHandle(unittest.TestCase):
    def detect(self, api):
        controls = core.TrainControls(api)
        controls.detect()
        return controls

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


if __name__ == "__main__":
    unittest.main()
