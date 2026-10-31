"""Utilization and placement-density validation.

A row of `util=0.7` really reached memory once — a placement density that landed
in the utilization slot and ran as 0.7% utilization instead of being refused.
"""
import os
import unittest
from unittest import mock

from adaptive_iicpilot import chat
from adaptive_iicpilot.chat import UTIL_MAX, UTIL_MIN, Session, validate_knobs

ENV = {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}


def _session(emitted=None):
    with mock.patch.dict(os.environ, ENV):
        return Session(emit=(emitted.append if emitted is not None else (lambda line: None)))


class TestUtilRange(unittest.TestCase):
    def test_accepts_the_inclusive_bounds(self):
        self.assertIsNone(validate_knobs(UTIL_MIN, None))
        self.assertIsNone(validate_knobs(UTIL_MAX, None))
        self.assertIsNone(validate_knobs(80.0, None))

    def test_rejects_below_the_minimum(self):
        message = validate_knobs(19.0, None)
        self.assertIsNotNone(message)
        self.assertIn("outside the supported range", message)
        self.assertIn("20-95", message)

    def test_rejects_above_the_maximum(self):
        message = validate_knobs(96.0, None)
        self.assertIsNotNone(message)
        self.assertIn("outside the supported range", message)

    def test_fraction_gets_the_did_you_mean_hint(self):
        message = validate_knobs(0.7, None)
        self.assertIn("did you mean 70%?", message)
        self.assertIn("looks like a fraction", message)

    def test_fraction_hint_scales_the_suggestion(self):
        self.assertIn("did you mean 80%?", validate_knobs(0.8, None))
        self.assertIn("did you mean 45%?", validate_knobs(0.45, None))

    def test_zero_is_treated_as_a_fraction_not_a_range_error(self):
        """0 is the shape of a mis-slotted value, so the hint is more useful
        than a bare range complaint."""
        self.assertIn("did you mean", validate_knobs(0.0, None))


class TestDensityRange(unittest.TestCase):
    def test_none_means_auto_and_is_accepted(self):
        self.assertIsNone(validate_knobs(80.0, None))

    def test_zero_means_auto_and_skips_the_floor(self):
        self.assertIsNone(validate_knobs(80.0, 0.0))

    def test_rejects_above_one(self):
        message = validate_knobs(80.0, 1.5)
        self.assertIn("between 0 and 1", message)

    def test_rejects_negative(self):
        message = validate_knobs(80.0, -0.1)
        self.assertIn("between 0 and 1", message)

    def test_accepts_one(self):
        self.assertIsNone(validate_knobs(80.0, 1.0))

    def test_rejects_below_the_utilization_floor(self):
        message = validate_knobs(80.0, 0.5)
        self.assertIn("at least the utilization", message)
        self.assertIn("0.82", message)

    def test_accepts_exactly_the_floor(self):
        self.assertIsNone(validate_knobs(80.0, 0.82))

    def test_floor_tracks_the_utilization(self):
        self.assertIn("0.52", validate_knobs(50.0, 0.4))
        self.assertIn("0.92", validate_knobs(90.0, 0.8))


class TestRunFlowRejects(unittest.TestCase):
    def test_fraction_util_is_refused_without_running(self):
        emitted = []
        session = _session(emitted)
        record = session.run_flow(design="ibex", util=0.7)

        self.assertTrue(record["rejected"])
        self.assertIn("did you mean 70%?", record["error"])
        self.assertFalse([line for line in emitted if line.startswith("[EDA]")])
        self.assertTrue([line for line in emitted if "REJECTED" in line])

    def test_out_of_range_util_is_refused(self):
        record = _session().run_flow(design="ibex", util=120)
        self.assertTrue(record["rejected"])
        self.assertIn("outside the supported range", record["error"])

    def test_density_above_one_is_refused(self):
        record = _session().run_flow(design="ibex", util=80, density=2)
        self.assertTrue(record["rejected"])
        self.assertIn("between 0 and 1", record["error"])

    def test_density_below_floor_is_refused(self):
        record = _session().run_flow(design="ibex", util=80, density=0.5)
        self.assertTrue(record["rejected"])
        self.assertIn("0.82", record["error"])

    def test_a_rejection_leaves_no_summarisable_record(self):
        """Nothing ran, so the deterministic summary must not describe a result."""
        session = _session()
        session.run_flow(design="ibex", util=0.7)
        self.assertIsNone(session.last_record)
        self.assertIn("REJECTED", session.last_verdict["verdict"])

    def test_valid_request_still_runs(self):
        with mock.patch.object(chat, "_live_runs_allowed", return_value=False):
            record = _session().run_flow(design="ibex", util=80)
        self.assertNotIn("rejected", record)
        self.assertIn("verdict", record)


class TestOptimizeGuard(unittest.TestCase):
    def test_optimize_refuses_an_invalid_base_utilization(self):
        """Defensive guard on the ORFS branch: if the shared ladder ever held a
        bad first rung, optimize must refuse rather than search from it. Both
        patches are needed — an uncached ladder would divert to the mock branch,
        whose base is FlowConfig's default 45% and therefore always valid."""
        session = _session()
        with mock.patch.object(chat, "ORFS_UTIL_LADDER", [0.5, 60.0]), mock.patch.object(
            type(session), "_ladder_cached", return_value=True
        ):
            result = session.optimize("ibex")

        self.assertTrue(result["rejected"])
        self.assertIn("did you mean 50%?", result["error"])

    def test_optimize_runs_with_the_real_ladder(self):
        result = _session().optimize("ibex")
        self.assertNotIn("rejected", result)
        self.assertEqual(result["objective"], "die_area")


if __name__ == "__main__":
    unittest.main()
