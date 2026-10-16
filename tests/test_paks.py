"""
Tests for tsw_paks.py finding every train in the game files, whatever its blueprint is called (the Freightliner
Class 70 is CL70_FL_RVM_C; the RVM_AABS_Class70 in its pack is an old one).

    python -m unittest discover tests
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tsw_paks  # noqa: E402
import tsw_check  # noqa: E402
import tsw_joystick as core  # noqa: E402
import tsw_joystick_ui as ui  # noqa: E402

ROOT = "TS2Prototype/Plugins/DLC/AABS_Class70/Content/"


class Definition:
    """A vehicle definition package (RVD_) naming the blueprint the game builds the vehicle from."""

    def __init__(self, model):
        self.exports = [{"name": "RVD"}]
        self.model = model

    def class_name(self, index):
        return "RailVehicleDefinition"

    def properties(self, index):
        return {"ModelActorAsset": [self.model, ""]}


def game_files(files, definitions):
    f = tsw_paks.GameFiles.__new__(tsw_paks.GameFiles)
    f.where = {name: None for name in files}
    f.roots = {"Game": "TS2Prototype/Content/", "AABS_Class70": ROOT}
    f._packages = dict(definitions)
    return f


class VehicleClasses(unittest.TestCase):
    def test_blueprint_not_named_rvm_found_from_its_definition(self):
        f = game_files([ROOT + "Simulation/CL70_FL_RVM.uasset", ROOT + "OLD/Simulation/RVM_AABS_Class70.uasset",
                        ROOT + "Data/RVD/RVD_AABS_Class70.uasset"],
                       {"/AABS_Class70/Data/RVD/RVD_AABS_Class70":
                        Definition("/AABS_Class70/Simulation/CL70_FL_RVM.CL70_FL_RVM_C")})
        self.assertEqual(f.vehicle_classes(), {
            "RVM_AABS_Class70_C": ["/AABS_Class70/OLD/Simulation/RVM_AABS_Class70"],
            "CL70_FL_RVM_C": ["/AABS_Class70/Simulation/CL70_FL_RVM"]})

    def test_rvm_blueprint_named_by_its_definition_listed_once(self):
        f = game_files([ROOT + "Simulation/RVM_X.uasset", ROOT + "Data/RVD_X.uasset"],
                       {"/AABS_Class70/Data/RVD_X": Definition("/AABS_Class70/Simulation/RVM_X.RVM_X_C")})
        self.assertEqual(f.vehicle_classes(), {"RVM_X_C": ["/AABS_Class70/Simulation/RVM_X"]})

    def test_definition_of_a_blueprint_not_installed_adds_nothing(self):
        f = game_files([ROOT + "Data/RVD_Y.uasset"],
                       {"/AABS_Class70/Data/RVD_Y": Definition("/Other/Simulation/Y_RVM.Y_RVM_C")})
        self.assertEqual(f.vehicle_classes(), {})

    def test_controls_the_blueprints_react_to(self):
        f = tsw_paks.GameFiles.__new__(tsw_paks.GameFiles)
        blueprint = SimpleNamespace(names=[
            "BndEvt__VigilancePedal_F_K2Node_ComponentBoundEvent_35_VirtualHIDChangedSignature__DelegateSignature",
            "BndEvt__(Push Button) AWS_K2Node_ComponentBoundEvent_2_VirtualHIDChangingSignature__DelegateSignature",
            "BndEvt__BP_DVD_Service_K2Node_ComponentBoundEvent_0_OnPenaltyBrakeStateChanged__DelegateSignature",
            "AlerterReset", "UpdateVigilancePedal"])
        f.class_chain = lambda cls, package: [(blueprint, 1)]
        self.assertEqual(f.bound_controls("CL70_FL_RVM_C", "/AABS_Class70/Simulation/CL70_FL_RVM"),
                         {"VigilancePedal_F", "(Push Button) AWS"})

    def test_names_shown(self):
        self.assertEqual(tsw_check.short("CL70_FL_RVM_C"), "CL70_FL")
        self.assertEqual(tsw_check.short("RVM_AABS_Class70_C"), "AABS_Class70")
        self.assertEqual(ui.pretty_train("CL70_FL_RVM_C"), "CL70 FL")
        self.assertEqual(ui.pretty_train("RVM_LNWR_Class350-1_DMS2_C"), "LNWR Class350-1 DMS2")


class Alerter(unittest.TestCase):
    """Which vigilance button the bridge works, where the files say what the train's blueprints react to."""

    @staticmethod
    def handle(bound, env):
        return SimpleNamespace(bound=bound, env=env)

    def test_class70_key_only_button_the_blueprints_ignore_goes_last(self):
        handles = {"AlerterReset": self.handle(False, "None"),
                   "VigilancePedal_F": self.handle(True, "Driver_F (Environment)")}
        names = list(handles)
        self.assertTrue(core.ignored_alerter(handles, names, "AlerterReset"))
        self.assertFalse(core.ignored_alerter(handles, names, "VigilancePedal_F"))

    def test_cab_button_the_blueprints_ignore_still_counts(self):
        # MBTA HSP46: ResetAlerter is on the desk; the game's own alerter reads it without a blueprint
        handles = {"ResetAlerter": self.handle(False, "Engineer (InteractionEnvironment)"),
                   "Acknowledge_R": self.handle(True, "Engineer (InteractionEnvironment)")}
        self.assertFalse(core.ignored_alerter(handles, list(handles), "ResetAlerter"))

    def test_nothing_bound_or_no_files_changes_nothing(self):
        handles = {"AlerterReset": self.handle(False, "None"), "Pedal": self.handle(False, "Cab")}
        self.assertFalse(core.ignored_alerter(handles, list(handles), "AlerterReset"))
        self.assertFalse(core.ignored_alerter({}, ["AlerterReset", "Pedal"], "AlerterReset"))


class LinkedLever(unittest.TestCase):
    """A cab handle that passes its moves to another lever (SlaveVHIDComponent): the Class 70's brake valve reads
    AutomaticBrake, which F_AutomaticBrake / B_AutomaticBrake pass their moves to. Set through the API, the game
    passed them on only some of the time, so the bridge sets that lever too, where it's just like the handle."""

    BRAKE = [{"InputValue": 0.0, "OutputValue": -1.0}, {"InputValue": 0.2, "OutputValue": 0.0},
             {"InputValue": 0.6, "OutputValue": 1.0}]

    def handles(self, master_map, slave_map):
        lever = "IrregularLeverComponent"
        comps = {"F_AutomaticBrake": tsw_paks.Component("F_AutomaticBrake", lever, lever, {
                     "ValueMap": master_map, "SlaveVHIDComponent": {"ComponentName": "AutomaticBrake"},
                     "InputIdentifier": {"Identifier": "AutomaticBrake"}}),
                 "AutomaticBrake": tsw_paks.Component("AutomaticBrake", lever, lever, {"ValueMap": slave_map})}
        files = SimpleNamespace(vehicle_classes=lambda: {"CL70_FL_RVM_C": ["/P"]},
                                matching_package=lambda cls, packages, names: "/P",
                                components=lambda cls, package: comps, bound_controls=lambda cls, package: set())
        saved = core._game_files
        try:
            core.use_game_files(files)
            return core.train_handles("CL70_FL_RVM_C")
        finally:
            core._game_files = saved

    def test_class70_brake_passes_its_moves_to_automaticbrake(self):
        handles = self.handles(self.BRAKE, self.BRAKE)
        self.assertEqual(handles["F_AutomaticBrake"].mirror, "AutomaticBrake")
        self.assertIsNone(handles["AutomaticBrake"].mirror)

    def test_lever_on_another_scale_is_left_to_the_game(self):
        # BR 101: TrainBrake passes its moves to a lever with a different table
        other = [{"InputValue": 0.0, "OutputValue": 0.0}, {"InputValue": 1.0, "OutputValue": 5.0}]
        self.assertIsNone(self.handles(self.BRAKE, other)["F_AutomaticBrake"].mirror)

    def test_both_levers_sent(self):
        sent = []
        lever = core.Lever.__new__(core.Lever)
        lever.api = SimpleNamespace(set=lambda path, value: sent.append((path, value)))
        lever.path = "CurrentDrivableActor/F_AutomaticBrake.InputValue"
        lever.mirror_path = "CurrentDrivableActor/AutomaticBrake.InputValue"
        lever._send(0.6)
        self.assertEqual(sent, [("CurrentDrivableActor/F_AutomaticBrake.InputValue", 0.6),
                                ("CurrentDrivableActor/AutomaticBrake.InputValue", 0.6)])


if __name__ == "__main__":
    unittest.main()
