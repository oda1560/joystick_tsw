"""
Tests for the bridge window's worker (tsw_joystick_ui.Bridge) when the game stalls for a moment, as it does as it
saves on arriving at a stop: the train is kept, and a send or a cab button press the game didn't answer is made
again. An error the game answers with still drops the train, as before.

    python -m unittest discover tests
"""

import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import tsw_joystick_ui as ui  # noqa: E402

LATE = TimeoutError("timed out")


class Train:
    """A train's controls (tsw_joystick.TrainControls) where the next `fails` sends and presses raise `error`."""
    missing = ()

    def __init__(self, fails=0, error=LATE, reverser=None):
        self.fails, self.error, self.reverser = fails, error, reverser
        self.applied, self.pressed, self.let_go_count = [], [], 0

    def _send(self):
        if self.fails:
            self.fails -= 1
            raise self.error

    def apply(self, y):
        self._send()
        self.applied.append(y)

    def press(self, name, down):
        self._send()
        self.pressed.append((name, down))
        return f"{name} {'pressed' if down else 'let go'}"

    def speed(self):
        return 0.0

    def cab_changed(self):
        return False

    def let_go(self):
        self.let_go_count += 1


class SilentReverser:
    ok = True

    def position(self):
        raise LATE


class Stall(unittest.TestCase):
    def bridge(self, train):
        self.log = []
        with mock.patch.object(ui.core, "load_game_files"):   # no reading the game files here
            bridge = ui.Bridge(self.log.append)
        bridge.api.key = "key"
        bridge.controls, bridge.train = train, "RVM_Test_C"
        bridge.last_check = float("inf")       # these tests are about one train: no looking for another
        bridge.y = 0.0
        bridge.step()                          # where the stick is to begin with: nothing sent
        return bridge

    def test_a_send_the_game_didnt_answer_is_made_again(self):
        train = Train(fails=1)
        bridge = self.bridge(train)
        bridge.y = -0.5                        # the brake on, as the game stalls
        bridge.step()
        self.assertIs(bridge.controls, train)  # the same train, not looked at again
        self.assertEqual(train.applied, [])
        bridge.step()                          # sent again, the stick not moved since
        self.assertEqual(train.applied, [-0.5])
        bridge.step()
        self.assertEqual(train.applied, [-0.5])   # and then only when the stick moves
        self.assertEqual(train.let_go_count, 0)
        self.assertIn("Send failed (timed out) - sending again", self.log)
        self.assertIn("Sent again", self.log)

    def test_said_once_however_long_the_game_stalls(self):
        bridge = self.bridge(Train(fails=3))
        bridge.y = -0.5
        for _ in range(4):
            bridge.step()
        self.assertEqual(sum(line.startswith("Send failed") for line in self.log), 1)

    def test_sent_again_with_the_stick_back_where_it_was(self):
        train = Train(fails=1)
        bridge = self.bridge(train)
        bridge.y = -0.5
        bridge.step()
        bridge.y = 0.0                         # the send may have got to the game: where the stick is now is sent
        bridge.step()
        self.assertEqual(train.applied, [0.0])

    def test_after_a_pause_the_stick_has_to_move_first(self):
        train = Train(fails=1)
        bridge = self.bridge(train)
        bridge.y = -0.5
        bridge.step()
        bridge.enabled = False                 # paused while the send was to be made again
        bridge.step()
        bridge.enabled = True
        bridge.step()
        bridge.step()
        self.assertEqual(train.applied, [])

    def test_an_error_the_game_answers_with_drops_the_train(self):
        error = urllib.error.HTTPError("http://127.0.0.1:31270/set", 404, "Not Found", None, None)
        bridge = self.bridge(Train(fails=1, error=error))
        bridge.y = -0.5
        bridge.step()
        self.assertIsNone(bridge.controls)
        self.assertIn("Send failed: HTTP Error 404: Not Found", self.log)

    def test_a_press_the_game_didnt_answer_is_made_again_before_the_next(self):
        train = Train(fails=1)
        bridge = self.bridge(train)
        bridge.button_queue.put(("aws", True))
        bridge.button_queue.put(("aws", False))
        bridge.step()                          # not answered: the press, and the release after it, wait
        self.assertEqual(train.pressed, [])
        bridge.step()
        self.assertEqual(train.pressed, [("aws", True), ("aws", False)])
        self.assertIs(bridge.controls, train)
        self.assertIn("aws pressed", self.log)

    def test_a_press_the_game_never_answers_is_given_up(self):
        train = Train(fails=1)
        bridge = self.bridge(train)
        bridge.button_queue.put(("door_open_left", True))
        bridge.button_queue.put(("aws", True))
        with mock.patch.object(ui, "BUTTON_AGAIN_SECONDS", 0.0):
            bridge.step()
        self.assertIn("The game didn't answer: the open left doors button wasn't pressed", self.log)
        self.assertEqual(train.pressed, [("aws", True)])    # the next press goes on

    def test_a_reverser_read_the_game_didnt_answer_keeps_the_train(self):
        train = Train(reverser=SilentReverser())
        bridge = self.bridge(train)
        self.assertIs(bridge.controls, train)


if __name__ == "__main__":
    unittest.main()
