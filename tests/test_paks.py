"""
Tests for tsw_paks.py finding every train in the game files, whatever its blueprint is called (the Freightliner
Class 70 is CL70_FL_RVM_C; the RVM_AABS_Class70 in its pack is an old one).

    python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tsw_paks  # noqa: E402
import tsw_check  # noqa: E402
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

    def test_names_shown(self):
        self.assertEqual(tsw_check.short("CL70_FL_RVM_C"), "CL70_FL")
        self.assertEqual(tsw_check.short("RVM_AABS_Class70_C"), "AABS_Class70")
        self.assertEqual(ui.pretty_train("CL70_FL_RVM_C"), "CL70 FL")
        self.assertEqual(ui.pretty_train("RVM_LNWR_Class350-1_DMS2_C"), "LNWR Class350-1 DMS2")


if __name__ == "__main__":
    unittest.main()
