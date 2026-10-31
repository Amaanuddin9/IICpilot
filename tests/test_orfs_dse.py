"""DSE behavior in the real-flow regime.

Pure proposal logic — no ORFS flow is invoked, so these run offline. The
numbers are the measured ibex ground truth: util 50 -> congestion 0.970 with
a 57255um2 die, util 90 -> congestion 1.041 with a 31991um2 die.
"""
import os
import unittest
from unittest import mock

from adaptive_iicpilot import DSEAgent, EDAAgent, FlowConfig, MockORFSAdapter
from adaptive_iicpilot.types import MemorySeed, Metrics

ORFS_ENV = {"ADAPTIVE_IICPILOT_BACKEND": "orfs"}


def real_metrics(congestion: float, die_area_um2: float) -> Metrics:
    return Metrics(
        stage="finish",
        wns_ns=-0.0155,
        tns_ns=-1.371,
        congestion=congestion,
        cell_count=31761,
        area_um2=29426.8,
        power_mw=22.1484,
        die_area_um2=die_area_um2,
        core_area_um2=die_area_um2 * 0.98,
        congestion_limit=1.0,
    )


class TestORFSProposals(unittest.TestCase):
    def setUp(self):
        self.agent = DSEAgent(EDAAgent(MockORFSAdapter()), target_stage="route")

    def test_clean_run_climbs_the_ladder(self):
        config = FlowConfig(design="ibex", core_utilization=50.0)
        with mock.patch.dict(os.environ, ORFS_ENV):
            proposal = self.agent.propose(config, real_metrics(0.97027, 57254.9), None)

        self.assertEqual(proposal.triggered_by, "die-area-shrink")
        self.assertEqual(proposal.changes, {"core_utilization": 60.0})

    def test_congested_run_backs_utilization_off(self):
        config = FlowConfig(design="ibex", core_utilization=90.0)
        with mock.patch.dict(os.environ, ORFS_ENV):
            proposal = self.agent.propose(config, real_metrics(1.0413, 31990.9), None)

        self.assertEqual(proposal.triggered_by, "congestion-recovery")
        self.assertEqual(proposal.changes, {"core_utilization": 85.0})

    def test_converges_at_top_of_ladder(self):
        config = FlowConfig(design="ibex", core_utilization=90.0)
        with mock.patch.dict(os.environ, ORFS_ENV):
            self.assertIsNone(self.agent.propose(config, real_metrics(0.99, 31990.9), None))

    def test_clock_is_never_swept(self):
        """Clock period lives in constraint.sdc and is not a real-backend knob."""
        with mock.patch.dict(os.environ, ORFS_ENV):
            for util, congestion in ((50.0, 0.97027), (90.0, 1.0413), (80.0, 0.99)):
                config = FlowConfig(design="ibex", core_utilization=util)
                proposal = self.agent.propose(config, real_metrics(congestion, 40000.0), None)
                if proposal is not None:
                    self.assertNotIn("clock_period_ns", proposal.changes)

    def test_negative_wns_alone_is_not_chased(self):
        """Real ibex sits at a small negative WNS at every rung; that must not
        by itself produce a timing proposal the way the mock regime does."""
        config = FlowConfig(design="ibex", core_utilization=90.0)
        metrics = real_metrics(0.99, 31990.9)
        with mock.patch.dict(os.environ, ORFS_ENV):
            self.assertIsNone(self.agent.propose(config, metrics, None))

class TestCorrectionAsStartingHint(unittest.TestCase):
    """A stored HITL correction must actually steer the search, not just sit in
    the heuristics list as prose (which is what it did before)."""

    def setUp(self):
        self.agent = DSEAgent(EDAAgent(MockORFSAdapter()), target_stage="route")
        self.base = FlowConfig(design="ibex", core_utilization=50.0, placement_density=0.0)

    def test_correction_opens_the_search_at_the_engineers_value(self):
        seed = MemorySeed(corrections={"core_utilization": 75.0})
        with mock.patch.dict(os.environ, ORFS_ENV):
            config, seeded = self.agent.starting_config(self.base, seed)

        self.assertTrue(seeded)
        self.assertEqual(config.core_utilization, 75.0)

    def test_measured_best_config_outranks_a_correction(self):
        """Once real runs exist, measurement wins — a stale hand-edit must not
        drag the search back off the measured optimum."""
        seed = MemorySeed(
            best_config=FlowConfig(design="ibex", core_utilization=80.0, placement_density=0.0),
            best_score=2.78e-05,
            corrections={"core_utilization": 75.0},
        )
        with mock.patch.dict(os.environ, ORFS_ENV):
            config, _ = self.agent.starting_config(self.base, seed)

        self.assertEqual(config.core_utilization, 80.0)

    def test_clock_correction_is_ignored(self):
        """Clock lives in constraint.sdc and is not driven in the real regime."""
        seed = MemorySeed(corrections={"clock_period_ns": 9.9})
        with mock.patch.dict(os.environ, ORFS_ENV):
            config, _ = self.agent.starting_config(self.base, seed)

        self.assertEqual(config.clock_period_ns, self.base.clock_period_ns)

    def test_mock_regime_ignores_corrections(self):
        seed = MemorySeed(corrections={"core_utilization": 75.0})
        env = dict(os.environ)
        env.pop("ADAPTIVE_IICPILOT_BACKEND", None)
        with mock.patch.dict(os.environ, env, clear=True):
            config, _ = self.agent.starting_config(self.base, seed)

        self.assertEqual(config.core_utilization, 50.0)


class TestMockRegime(unittest.TestCase):
    def setUp(self):
        self.agent = DSEAgent(EDAAgent(MockORFSAdapter()), target_stage="route")

    def test_mock_regime_is_untouched(self):
        """Same agent, no backend env: the original rule set still applies."""
        config = FlowConfig(design="picorv32", core_utilization=72.0, placement_density=0.95)
        mock_metrics = Metrics(
            stage="route", wns_ns=0.4, tns_ns=0.0, congestion=0.45,
            cell_count=12000, area_um2=27000.0, power_mw=21.8,
        )
        env = dict(os.environ)
        env.pop("ADAPTIVE_IICPILOT_BACKEND", None)
        with mock.patch.dict(os.environ, env, clear=True):
            proposal = self.agent.propose(config, mock_metrics, None)

        self.assertEqual(proposal.triggered_by, "congestion-density")
        self.assertIn("placement_density", proposal.changes)


if __name__ == "__main__":
    unittest.main()
