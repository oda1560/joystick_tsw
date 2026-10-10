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


if __name__ == "__main__":
    unittest.main()
