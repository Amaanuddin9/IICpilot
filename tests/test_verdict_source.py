"""Provenance and verdict must be computed in Python on every path, and the
UI must not depend on the model relaying them correctly.

Motivated by a real failure: the model narrated a congestion of 0.42 as "above
the 1.0 limit, congested" and did not say the numbers were estimated.
"""
import os
import unittest
from unittest import mock

from adaptive_iicpilot import chat
from adaptive_iicpilot.chat import (
    CACHED_ORFS,
    ESTIMATED_FROM_REAL,
    ESTIMATED_MOCK,
    Session,
    contradiction_note,
)

ENV = {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}


def _session(emitted=None):
    with mock.patch.dict(os.environ, ENV):
        return Session(emit=(emitted.append if emitted is not None else (lambda line: None)))


class TestSourceAndVerdictOnEveryPath(unittest.TestCase):
    def test_mock_fallback_has_both_source_and_verdict(self):
        """The mock is only reached for a design with NO real cached runs
        (picorv32 has none); it must still be labelled and given a verdict."""
        session = _session()
        with mock.patch.object(chat, "_live_runs_allowed", return_value=False):
            record = session.run_flow(design="picorv32", util=65.0)

        self.assertEqual(record["source"], ESTIMATED_MOCK)
        self.assertIn("verdict", record)
        self.assertRegex(record["verdict"], r"^(CLEAN|CONGESTED|FAILED)")

    def test_design_with_real_data_prefers_the_calibrated_estimator(self):
        """gcd has real cached points, so an uncached util must be estimated
        from them rather than from the paper-calibrated mock. (55 is the gcd
        stock util and is measured now; 57 lies between measured 55 and 60.)"""
        session = _session()
        with mock.patch.object(chat, "_live_runs_allowed", return_value=False):
            record = session.run_flow(design="gcd", util=57.0)

        self.assertEqual(record["source"], ESTIMATED_FROM_REAL)
        self.assertIn("verdict", record)

    def test_cached_orfs_path_is_labelled_cached(self):
        session = _session()
        record = session.run_flow(design="ibex", util=80.0)

        self.assertEqual(record["source"], CACHED_ORFS)
        self.assertTrue(record["verdict"].startswith("CLEAN"), record["verdict"])

    def test_verdict_matches_the_congestion_number(self):
        """A CLEAN verdict must never be attached to congestion above 1.0."""
        session = _session()
        with mock.patch.object(chat, "_live_runs_allowed", return_value=False):
            record = session.run_flow(design="picorv32", util=65.0)

        congestion = record["congestion"]
        self.assertIsNotNone(congestion)
        if congestion > 1.0:
            self.assertTrue(record["verdict"].startswith("CONGESTED"))
        else:
            self.assertTrue(record["verdict"].startswith(("CLEAN", "FAILED")))

    def test_verdict_line_is_emitted_deterministically(self):
        emitted = []
        session = _session(emitted)
        session.run_flow(design="ibex", util=80.0)

        verdict_lines = [line for line in emitted if line.startswith("[Verdict]")]
        self.assertEqual(len(verdict_lines), 1)
        self.assertIn("source: ", verdict_lines[0])
        self.assertIn(CACHED_ORFS, verdict_lines[0])


class TestDensityValidation(unittest.TestCase):
    def test_density_below_utilization_is_rejected_before_running(self):
        emitted = []
        session = _session(emitted)
        record = session.run_flow(design="ibex", util=80.0, density=0.5)

        self.assertTrue(record["rejected"])
        self.assertIn("0.82", record["error"])
        self.assertIn("at least the utilization", record["error"])
        # Nothing ran, so there is no verdict to report.
        self.assertNotIn("verdict", record)
        self.assertFalse([line for line in emitted if line.startswith("[EDA]")])

    def test_density_at_the_floor_is_accepted(self):
        session = _session()
        with mock.patch.object(chat, "_live_runs_allowed", return_value=False):
            record = session.run_flow(design="ibex", util=80.0, density=0.82)

        self.assertNotIn("rejected", record)
        self.assertIn("verdict", record)

    def test_auto_density_is_not_validated(self):
        session = _session()
        record = session.run_flow(design="ibex", util=80.0)
        self.assertNotIn("rejected", record)

    def test_explicit_zero_density_means_auto_not_a_violation(self):
        """The model routinely sends density: 0, which is the "let ORFS choose"
        sentinel — rejecting it would refuse a perfectly valid request."""
        session = _session()
        record = session.run_flow(design="ibex", util=80.0, density=0)
        self.assertNotIn("rejected", record)
        self.assertIn("verdict", record)


class TestContradictionNote(unittest.TestCase):
    CLEAN = {"verdict": "CLEAN - congestion 0.42 is BELOW the 1.0 limit", "source": CACHED_ORFS}
    CONGESTED = {"verdict": "CONGESTED - congestion 1.04 is ABOVE the 1.0 limit", "source": CACHED_ORFS}
    MOCK = {"verdict": "CLEAN - congestion 0.42 is BELOW the 1.0 limit", "source": ESTIMATED_MOCK}

    def test_flags_the_exact_observed_failure(self):
        reply = "Congestion is 0.42, which is above the 1.0 limit, so it is congested."
        note = contradiction_note(reply, self.CLEAN)

        self.assertIsNotNone(note)
        self.assertIn("CLEAN", note)
        self.assertIn(CACHED_ORFS, note)

    def test_accepts_a_faithful_clean_reply(self):
        reply = "The run is clean: congestion 0.42 is below the 1.0 limit."
        self.assertIsNone(contradiction_note(reply, self.CLEAN))

    def test_not_congested_is_not_a_contradiction(self):
        reply = "It is not congested; congestion 0.42 sits below the limit."
        self.assertIsNone(contradiction_note(reply, self.CLEAN))

    def test_flags_clean_prose_against_a_congested_verdict(self):
        reply = "This configuration is clean and routable."
        note = contradiction_note(reply, self.CONGESTED)
        self.assertIsNotNone(note)
        self.assertIn("CONGESTED", note)

    def test_flags_missing_estimated_caveat(self):
        reply = "The run is clean: congestion 0.42 is below the 1.0 limit."
        note = contradiction_note(reply, self.MOCK)
        self.assertIsNotNone(note)
        self.assertIn(ESTIMATED_MOCK, note)

    def test_accepts_reply_that_says_estimated(self):
        reply = "These are estimated numbers: clean, congestion 0.42 below the limit."
        self.assertIsNone(contradiction_note(reply, self.MOCK))

    def test_no_verdict_means_no_note(self):
        self.assertIsNone(contradiction_note("anything at all", None))

    def test_flags_outcome_claims_after_a_rejection(self):
        """Observed live: the model hit the density rejection and then said
        "The design is CONGESTED". Nothing ran, so that must be corrected."""
        rejected = {
            "verdict": "REJECTED - placement density 0.5 is below the utilization",
            "source": "NOT_RUN",
        }
        note = contradiction_note("In the design ibex the tool failed due to "
                                  "congestion. The design is CONGESTED.", rejected)
        self.assertIsNotNone(note)
        self.assertIn("REJECTED", note)

    def test_faithful_rejection_reply_needs_no_note(self):
        rejected = {
            "verdict": "REJECTED - placement density 0.5 is below the utilization",
            "source": "NOT_RUN",
        }
        reply = "I did not run it: the density you gave is below the utilization. Try >= 0.82."
        self.assertIsNone(contradiction_note(reply, rejected))


if __name__ == "__main__":
    unittest.main()
