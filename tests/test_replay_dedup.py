"""Cache replays must not inflate the measurement count.

Five identical util-80 rows appeared in a real database because every cache
replay went through EDAAgent and was recorded as a fresh run. Anyone reading
"runs: 26" would have believed 26 flows had been measured.
"""
import unittest

from adaptive_iicpilot.sqlite_memory import SQLiteMemory
from adaptive_iicpilot.types import FlowConfig, Metrics, RunResult


def _result(util=80.0, density=0.0, die=35944.4, congestion=0.9737, ok=True, power=21.64):
    config = FlowConfig(design="ibex", core_utilization=util, placement_density=density)
    metrics = Metrics(
        stage="finish", wns_ns=-0.0072, tns_ns=-0.2538, congestion=congestion,
        cell_count=22894, area_um2=29287.9, power_mw=power,
        die_area_um2=die if ok else 0.0, core_area_um2=die * 0.98,
        congestion_limit=1.0,
    )
    return RunResult(config=config, reached_stage="finish", metrics=metrics, ok=ok)


class TestReplayFolding(unittest.TestCase):
    def setUp(self):
        self.memory = SQLiteMemory(":memory:")
        self.addCleanup(self.memory.close)

    def test_identical_replays_do_not_add_rows(self):
        for _ in range(5):
            self.memory.record_run(_result())

        info = self.memory.describe()
        self.assertEqual(info["total_unique_configs"], 1)
        self.assertEqual(info["total_replays"], 5)

    def test_show_counts_are_not_inflated(self):
        for _ in range(5):
            self.memory.record_run(_result())
        self.memory.record_run(_result(util=85.0, die=33850.5, congestion=1.0179))

        entry = self.memory.describe()["designs"]["ibex::nangate45"]
        self.assertEqual(entry["unique_configs"], 2)
        self.assertEqual(entry["replay_count"], 6)

    def test_a_different_config_is_a_new_measurement(self):
        self.memory.record_run(_result(util=80.0))
        self.memory.record_run(_result(util=85.0, die=33850.5, congestion=1.0179))

        self.assertEqual(self.memory.describe()["total_unique_configs"], 2)

    def test_same_config_with_different_metrics_is_a_new_measurement(self):
        """A genuine re-measurement that disagrees must not be folded away."""
        self.memory.record_run(_result(util=80.0, die=35944.4))
        self.memory.record_run(_result(util=80.0, die=35000.0))

        self.assertEqual(self.memory.describe()["total_unique_configs"], 2)

    def test_same_config_at_a_different_density_is_separate(self):
        self.memory.record_run(_result(util=80.0, density=0.0))
        self.memory.record_run(_result(util=80.0, density=0.9))

        self.assertEqual(self.memory.describe()["total_unique_configs"], 2)

    def test_replays_do_not_change_the_best_config_or_wall(self):
        for _ in range(5):
            self.memory.record_run(_result(util=80.0))
        self.memory.record_run(_result(util=85.0, die=33850.5, congestion=1.0179))

        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.best_config.core_utilization, 80.0)
        self.assertEqual(seed.congestion_wall_util, 85.0)


class TestDeduplicateExisting(unittest.TestCase):
    """Repairing a database written before replays were tracked."""

    def test_collapses_pre_existing_duplicates(self):
        memory = SQLiteMemory(":memory:")
        self.addCleanup(memory.close)
        # Simulate the old behaviour: rows inserted directly, one per replay.
        for _ in range(5):
            memory._conn.execute(
                "INSERT INTO runs (ts, first_seen, design, tech, util, density, aspect,"
                " clock_ns, backend, ok, congested, wns_ns, tns_ns, die_area_um2,"
                " congestion, power_mw, score, replays)"
                " VALUES (1,1,'ibex','nangate45',80.0,0.0,1.0,2.5,'orfs',1,0,-0.007,"
                "-0.25,35944.4,0.9737,21.64,2.78e-05,1)"
            )
        memory._conn.commit()
        self.assertEqual(memory.describe()["total_unique_configs"], 5)

        result = memory.deduplicate_runs()

        self.assertEqual(result["rows_removed"], 4)
        info = memory.describe()
        self.assertEqual(info["total_unique_configs"], 1)
        self.assertEqual(info["total_replays"], 5)

    def test_dedupe_is_idempotent(self):
        memory = SQLiteMemory(":memory:")
        self.addCleanup(memory.close)
        for _ in range(3):
            memory.record_run(_result())
        first = memory.deduplicate_runs()
        second = memory.deduplicate_runs()

        self.assertEqual(first["rows_removed"], 0)  # already folded on insert
        self.assertEqual(second["rows_removed"], 0)
        self.assertEqual(memory.describe()["total_replays"], 3)


if __name__ == "__main__":
    unittest.main()
