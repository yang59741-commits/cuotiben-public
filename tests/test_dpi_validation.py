import unittest

from cuoti_app.services.dpi_validation import DPI_PERCENTAGES, validate_matrix


class DpiValidationTests(unittest.TestCase):
    def test_all_required_scales_and_cross_monitor_transitions_are_covered(self):
        result = validate_matrix()
        self.assertTrue(result["passed"])
        self.assertEqual([item["dpi_percent"] for item in result["scenarios"]], list(DPI_PERCENTAGES))
        self.assertEqual(len(result["cross_monitor_transitions"]), 20)
        for scenario in result["scenarios"]:
            self.assertTrue(all(control["visual_rect"] == control["hit_rect"] for control in scenario["controls"]))


if __name__ == "__main__":
    unittest.main()
