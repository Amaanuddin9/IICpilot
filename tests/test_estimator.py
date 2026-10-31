"""The calibrated estimator, and the guards around unusable model replies.

Motivated by a chat run of "ibex at 20% util": the paper-calibrated mock was
used for a design with a full cached ladder, and the model's reply was garbled.
"""
import unittest

from adaptive_iicpilot.chat import (
    ESTIMATED_FROM_REAL,
    ESTIMATED_MOCK,
    deterministic_summary,
    is_garbled,
)
from adaptive_iicpilot.estimator import CalibratedEstimator, MeasuredPoint

# The real measured ibex ladder (auto density), as cached on this machine.
LADDER = {
    50.0: (57254.9, 22.148, 0.9703, -0.0155),
    60.0: (47790.3, 21.860, 0.9326, -0.0282),
    70.0: (41022.5, 21.730, 0.9538, 0.0059),
    80.0: (35944.4, 21.643, 0.9737, -0.0072),
    85.0: (33850.5, 21.580, 1.0179, -0.0468),
    90.0: (31990.9, 21.870, 1.0413, -0.0732),
}
MEASURED_AT_75 = 38314.1


def _points(utils=None):
    utils = utils or sorted(LADDER)
    points = []
    for util in utils:
        die, power, congestion, wns = LADDER[util]
        points.append(
            MeasuredPoint(
                util=util,
                die_area_um2=die,
                core_area_um2=die * 0.98,
                instance_area_um2=29000.0,
                power_mw=power,
                congestion=congestion,
                wns_ns=wns,
                tns_ns=-1.0,
                cell_count=22894,
            )
        )
    return points


class TestDieAreaAccuracy(unittest.TestCase):
    def test_held_out_75_percent_within_5_percent(self):
        """The acceptance bar: fit the ladder WITHOUT 75%, predict 75%, and
        compare against the separately measured 38314.1."""
        estimator = CalibratedEstimator("ibex", _points())
        predicted = estimator.estimate_die_area(75.0)

        error = abs(predicted - MEASURED_AT_75) / MEASURED_AT_75
        self.assertLess(error, 0.05, f"predicted {predicted:.1f} vs {MEASURED_AT_75}")

    def test_reproduces_each_measured_point(self):
        estimator = CalibratedEstimator("ibex", _points())
        for util, (die, *_rest) in LADDER.items():
            predicted = estimator.estimate_die_area(util)
            self.assertLess(abs(predicted - die) / die, 0.01, f"util {util}")

    def test_needs_two_points(self):
        self.assertFalse(CalibratedEstimator("ibex", _points([50.0])).usable)
        self.assertTrue(CalibratedEstimator("ibex", _points([50.0, 90.0])).usable)

    def test_failed_runs_are_excluded(self):
        """A failed flow is cached with zero die area and must not skew the fit."""
        points = _points() + [
            MeasuredPoint(
                util=95.0, die_area_um2=0.0, core_area_um2=0.0, instance_area_um2=0.0,
                power_mw=0.0, congestion=0.0, wns_ns=0.0, tns_ns=0.0, cell_count=0,
            )
        ]
        estimator = CalibratedEstimator("ibex", points)

        self.assertEqual(estimator.estimated_point_count, len(LADDER))
        self.assertEqual(estimator.util_range, (50.0, 90.0))


class TestInRangeEstimates(unittest.TestCase):
    def setUp(self):
        self.estimator = CalibratedEstimator("ibex", _points())

    def test_interpolates_congestion_and_wns(self):
        estimate = self.estimator.estimate(75.0)

        # Between the 70% (0.9538) and 80% (0.9737) measurements.
        self.assertIsNotNone(estimate["congestion"])
        self.assertGreater(estimate["congestion"], 0.9538)
        self.assertLess(estimate["congestion"], 0.9737)
        self.assertIsNotNone(estimate["wns_ns"])

    def test_verdict_is_clean_and_marked_estimated(self):
        estimate = self.estimator.estimate(75.0)
        self.assertTrue(estimate["verdict"].startswith("CLEAN"))
        self.assertIn("estimated", estimate["verdict"])

    def test_congested_region_is_reported_congested(self):
        estimate = self.estimator.estimate(87.0)
        self.assertTrue(estimate["congested"])
        self.assertTrue(estimate["verdict"].startswith("CONGESTED"))

    def test_power_is_interpolated_not_invented(self):
        estimate = self.estimator.estimate(75.0)
        self.assertGreater(estimate["power_mw"], 21.0)
        self.assertLess(estimate["power_mw"], 22.5)


class TestOutsideMeasuredRange(unittest.TestCase):
    """The "ibex at 20%" case: die area may be extrapolated, routability may not."""

    def setUp(self):
        self.estimate = CalibratedEstimator("ibex", _points()).estimate(20.0)

    def test_die_area_is_still_extrapolated(self):
        self.assertGreater(self.estimate["die_area_um2"], 57254.9)

    def test_congestion_and_wns_are_unknown_not_zero(self):
        self.assertIsNone(self.estimate["congestion"])
        self.assertIsNone(self.estimate["wns_ns"])
        self.assertIsNone(self.estimate["congested"])

    def test_verdict_refuses_to_judge_routability(self):
        verdict = self.estimate["verdict"]
        self.assertTrue(verdict.startswith("UNKNOWN"), verdict)
        self.assertNotIn("CLEAN", verdict)
        self.assertIn("outside measured range", verdict)

    def test_notes_name_the_measured_range(self):
        self.assertIn("50-90%", self.estimate["congestion_note"])
        self.assertIn("outside the measured range", self.estimate["range_note"])


class TestGarbledReplies(unittest.TestCase):
    RECORD = {
        "design": "ibex",
        "util": 20.0,
        "source": ESTIMATED_FROM_REAL,
        "die_area_um2": 142527.2,
        "power_mw": 22.148,
        "congestion": None,
        "verdict": "UNKNOWN - congestion is unknown (outside measured range 50-90% utilization)",
    }

    def test_empty_reply_is_garbled(self):
        self.assertTrue(is_garbled(""))
        self.assertTrue(is_garbled("   \n "))

    def test_reply_naming_a_tool_is_garbled(self):
        self.assertTrue(is_garbled("I will now call run_flow with util 20."))

    def test_reply_telling_the_user_to_call_a_tool_is_garbled(self):
        self.assertTrue(is_garbled("You should use the tool to get this result."))

    def test_reply_leaking_json_is_garbled(self):
        self.assertTrue(is_garbled('{"design": "ibex", "util": 20}'))

    def test_a_normal_reply_is_not_garbled(self):
        self.assertFalse(
            is_garbled("ibex at 20% has an estimated die area of 142527 um2; "
                       "congestion is outside the measured range.")
        )

    def test_deterministic_summary_states_the_real_facts(self):
        summary = deterministic_summary(self.RECORD)

        self.assertIn("ibex at 20%", summary)
        self.assertIn("estimated from real data", summary)
        self.assertIn("142527.2", summary)
        self.assertIn("22.15", summary)
        # Unknown congestion must read n/a, never 0.
        self.assertIn("congestion n/a", summary)
        self.assertIn("UNKNOWN", summary)

    def test_summary_marks_mock_source_plainly(self):
        record = {**self.RECORD, "source": ESTIMATED_MOCK, "congestion": 0.035,
                  "verdict": "CLEAN - congestion 0.0350 is BELOW the 1.0 limit"}
        summary = deterministic_summary(record)

        self.assertIn("mock model", summary)
        self.assertIn("congestion 0.0350", summary)

    def test_summary_survives_a_missing_record(self):
        self.assertIsInstance(deterministic_summary(None), str)


if __name__ == "__main__":
    unittest.main()
