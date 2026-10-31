import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from adaptive_iicpilot import FlowConfig, JSONMemory, Metrics, Proposal, RunResult


def _result(design="gcd", tech="nangate45", ok=True, wns_ns=0.1, area_um2=100.0, power_mw=10.0):
    config = FlowConfig(design=design, tech=tech)
    metrics = Metrics(
        stage="route",
        wns_ns=wns_ns,
        tns_ns=0.0,
        congestion=0.1,
        cell_count=1000,
        area_um2=area_um2,
        power_mw=power_mw,
    )
    return RunResult(config=config, reached_stage="route", metrics=metrics, ok=ok)


class TestJSONMemory(unittest.TestCase):
    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "memory.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_cold_seed_when_unseen(self):
        memory = JSONMemory(path=self.path)
        seed = memory.seed_for("gcd", "nangate45", "route")
        self.assertTrue(seed.is_cold)

    def test_persistence_roundtrip(self):
        writer = JSONMemory(path=self.path)
        best = _result(area_um2=50.0)
        proposal = Proposal(
            changes={"placement_density": 0.5},
            rationale="reduce congestion",
            triggered_by="congestion-density",
        )
        writer.record_session(best, [proposal])

        reader = JSONMemory(path=self.path)
        seed = reader.seed_for("gcd", "nangate45", "route")
        self.assertFalse(seed.is_cold)
        self.assertEqual(seed.best_config.design, "gcd")
        self.assertIn("reduce congestion", seed.heuristics)

    def test_best_config_updates_only_on_strict_improvement(self):
        memory = JSONMemory(path=self.path)
        better = _result(area_um2=50.0, power_mw=5.0)
        worse = _result(area_um2=500.0, power_mw=50.0)

        memory.record_session(better, [])
        first_score = memory.seed_for("gcd", "nangate45", "route").best_score

        memory.record_session(worse, [])
        second_score = memory.seed_for("gcd", "nangate45", "route").best_score

        self.assertEqual(first_score, second_score)

    def test_heuristic_dedup_by_trigger(self):
        memory = JSONMemory(path=self.path)
        best = _result()
        p1 = Proposal(changes={}, rationale="first fix", triggered_by="congestion-density")
        p2 = Proposal(changes={}, rationale="second fix", triggered_by="congestion-density")

        memory.record_session(best, [p1])
        memory.record_session(best, [p2])

        seed = memory.seed_for("gcd", "nangate45", "route")
        self.assertEqual(len(seed.heuristics), 1)
        self.assertIn("second fix", seed.heuristics)

    def test_heuristic_cap_eviction(self):
        memory = JSONMemory(path=self.path)
        best = _result()
        proposals = [
            Proposal(changes={}, rationale=f"fix {i}", triggered_by=f"trigger-{i}")
            for i in range(JSONMemory.HEURISTIC_CAP + 5)
        ]
        memory.record_session(best, proposals)

        seed = memory.seed_for("gcd", "nangate45", "route")
        self.assertEqual(len(seed.heuristics), JSONMemory.HEURISTIC_CAP)

    def test_seeded_from_memory_proposals_are_not_relearned(self):
        memory = JSONMemory(path=self.path)
        best = _result()
        proposal = Proposal(
            changes={}, rationale="from memory", triggered_by="congestion-utilization(memory)",
            seeded_from_memory=True,
        )
        memory.record_session(best, [proposal])

        seed = memory.seed_for("gcd", "nangate45", "route")
        self.assertEqual(seed.heuristics, [])

    def test_record_run_updates_counts(self):
        memory = JSONMemory(path=self.path)
        memory.record_run(_result(ok=True))
        memory.record_run(_result(ok=False))

        data = memory._load()
        entry = data["designs"]["gcd::nangate45"]
        self.assertEqual(entry["run_count"], 2)
        self.assertEqual(entry["failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
