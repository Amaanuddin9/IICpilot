"""Persistent memory: MemoryPort conformance, round-trip, and real persistence."""
import inspect
import tempfile
import unittest
from pathlib import Path

from adaptive_iicpilot.sqlite_memory import SQLiteMemory
from adaptive_iicpilot.types import FlowConfig, MemoryPort, Metrics, RunResult


def result(design="ibex", util=50.0, die=57254.9, congestion=0.97, ok=True):
    config = FlowConfig(design=design, core_utilization=util, placement_density=0.0)
    metrics = Metrics(
        stage="finish", wns_ns=-0.015, tns_ns=-1.371, congestion=congestion,
        cell_count=31761, area_um2=29426.8, power_mw=22.15,
        die_area_um2=die, core_area_um2=die * 0.98, congestion_limit=1.0,
    )
    return RunResult(config=config, reached_stage="finish", metrics=metrics, ok=ok)


class TestProtocolAndRoundTrip(unittest.TestCase):
    def setUp(self):
        self.memory = SQLiteMemory(":memory:")

    def test_satisfies_memory_port(self):
        """Structural check rather than isinstance: MemoryPort is not
        runtime_checkable, and comparing signatures is the stronger assertion
        anyway since it catches a drifting parameter list."""
        for name in ("seed_for", "record_run"):
            self.assertTrue(callable(getattr(self.memory, name)), name)
            self.assertEqual(
                inspect.signature(getattr(SQLiteMemory, name)),
                inspect.signature(getattr(MemoryPort, name)),
                f"{name} signature differs from MemoryPort",
            )

    def test_empty_db_is_cold(self):
        self.assertTrue(self.memory.seed_for("ibex", "nangate45", "route").is_cold)

    def test_record_and_seed_round_trip(self):
        self.memory.record_run(result(util=50.0, die=57254.9))
        seed = self.memory.seed_for("ibex", "nangate45", "route")

        self.assertFalse(seed.is_cold)
        self.assertEqual(seed.best_config.core_utilization, 50.0)

    def test_best_is_smallest_clean_die(self):
        self.memory.record_run(result(util=50.0, die=57254.9, congestion=0.97))
        self.memory.record_run(result(util=80.0, die=35944.4, congestion=0.9737))
        # Smaller die but congested: must not win.
        self.memory.record_run(result(util=90.0, die=31990.9, congestion=1.0413))

        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.best_config.core_utilization, 80.0)

    def test_congested_runs_are_stored_as_the_wall(self):
        self.memory.record_run(result(util=80.0, die=35944.4, congestion=0.9737))
        self.memory.record_run(result(util=85.0, die=33850.5, congestion=1.0179))
        self.memory.record_run(result(util=90.0, die=31990.9, congestion=1.0413))

        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.congestion_wall_util, 85.0)

    def test_mock_rows_without_die_area_fall_back_to_score(self):
        config = FlowConfig(design="picorv32", core_utilization=45.0)
        metrics = Metrics(
            stage="route", wns_ns=0.4, tns_ns=0.0, congestion=0.2,
            cell_count=12000, area_um2=27000.0, power_mw=21.8,
        )
        self.memory.record_run(
            RunResult(config=config, reached_stage="route", metrics=metrics)
        )
        seed = self.memory.seed_for("picorv32", "nangate45", "route")

        self.assertIsNotNone(seed.best_config)
        self.assertEqual(seed.best_config.core_utilization, 45.0)

    def test_correction_becomes_a_heuristic(self):
        self.memory.record_correction("ibex", "nangate45", "core_utilization", 85, 75)
        seed = self.memory.seed_for("ibex", "nangate45", "route")

        self.assertEqual(len(seed.heuristics), 1)
        self.assertIn("core_utilization", seed.heuristics[0])
        self.assertIn("75", seed.heuristics[0])
        self.assertIn("85", seed.heuristics[0])
        self.assertEqual(len(self.memory.corrections("ibex")), 1)

    def test_reset_returns_to_cold(self):
        self.memory.record_run(result())
        self.memory.record_correction("ibex", "nangate45", "core_utilization", 85, 75)
        self.memory.reset()

        self.assertTrue(self.memory.seed_for("ibex", "nangate45", "route").is_cold)

    def test_reset_one_design_only(self):
        self.memory.record_run(result(design="ibex"))
        self.memory.record_run(result(design="gcd"))
        self.memory.reset("ibex")

        self.assertTrue(self.memory.seed_for("ibex", "nangate45", "route").is_cold)
        self.assertFalse(self.memory.seed_for("gcd", "nangate45", "route").is_cold)


class TestPersistenceAcrossConnections(unittest.TestCase):
    """The actual claim: learning outlives the object that recorded it."""

    def test_second_connection_sees_first_ones_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"

            writer = SQLiteMemory(db)
            writer.record_run(result(util=80.0, die=35944.4, congestion=0.9737))
            writer.record_run(result(util=85.0, die=33850.5, congestion=1.0179))
            writer.record_correction("ibex", "nangate45", "core_utilization", 85, 75)
            writer.close()

            reader = SQLiteMemory(db)
            seed = reader.seed_for("ibex", "nangate45", "route")

            self.assertFalse(seed.is_cold)
            self.assertEqual(seed.best_config.core_utilization, 80.0)
            self.assertEqual(seed.congestion_wall_util, 85.0)
            self.assertEqual(len(seed.heuristics), 1)
            reader.close()


if __name__ == "__main__":
    unittest.main()


class TestFailedRunsCountTowardTheWall(unittest.TestCase):
    """A failed run is a measurement. gcd at 70% failed with congestion 0.679 —
    comfortably "clean" — so a congestion-only wall would have missed it
    entirely and let the search walk straight back into it."""

    def setUp(self):
        self.memory = SQLiteMemory(":memory:")

    def _run(self, util, ok, congestion, die=1000.0):
        config = FlowConfig(design="gcd", core_utilization=util, placement_density=0.0)
        metrics = Metrics(
            stage="cts" if not ok else "finish", wns_ns=-0.16, tns_ns=-7.1,
            congestion=congestion, cell_count=986, area_um2=947.0, power_mw=6.7,
            die_area_um2=die if ok else 0.0, core_area_um2=die * 0.98,
            congestion_limit=1.0,
        )
        return RunResult(
            config=config, reached_stage=metrics.stage, metrics=metrics, ok=ok,
            failure=None if ok else "ORFS flow returned non-zero",
        )

    def test_uncongested_failure_still_sets_the_wall(self):
        self.memory.record_run(self._run(50.0, True, 0.6954, 1399.1))
        self.memory.record_run(self._run(60.0, True, 0.7150, 1177.9))
        self.memory.record_run(self._run(70.0, False, 0.6785))

        seed = self.memory.seed_for("gcd", "nangate45", "route")
        self.assertEqual(seed.congestion_wall_util, 70.0)

    def test_clean_runs_alone_leave_no_wall(self):
        self.memory.record_run(self._run(50.0, True, 0.6954, 1399.1))
        seed = self.memory.seed_for("gcd", "nangate45", "route")
        self.assertIsNone(seed.congestion_wall_util)

    def test_failed_run_is_never_the_best_config(self):
        self.memory.record_run(self._run(50.0, True, 0.6954, 1399.1))
        self.memory.record_run(self._run(70.0, False, 0.6785))

        seed = self.memory.seed_for("gcd", "nangate45", "route")
        self.assertEqual(seed.best_config.core_utilization, 50.0)


class TestCacheIngestion(unittest.TestCase):
    def test_ingest_is_idempotent(self):
        memory = SQLiteMemory(":memory:")
        first = memory.ingest_cached_runs()
        second = memory.ingest_cached_runs()

        self.assertGreater(first["added"], 0)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["skipped"], first["added"])

    def test_ingest_brings_in_failed_runs(self):
        memory = SQLiteMemory(":memory:")
        stats = memory.ingest_cached_runs()

        self.assertGreater(stats["failed_runs_added"], 0)
        info = memory.describe()
        self.assertGreater(info["total_failures"], 0)


class TestMockRunsDoNotDefineTheWall(unittest.TestCase):
    """A mock run at ibex 80% recorded congested=1 at congestion 0.42 (the mock
    scale congests above 0.30) and dragged the real wall from 85% down to 80% —
    below the utilization real silicon routes cleanly, and the actual optimum."""

    def setUp(self):
        self.memory = SQLiteMemory(":memory:")

    def _record(self, util, congestion, die, limit):
        config = FlowConfig(design="ibex", core_utilization=util, placement_density=0.0)
        metrics = Metrics(
            stage="finish", wns_ns=-0.01, tns_ns=-0.3, congestion=congestion,
            cell_count=22894, area_um2=29287.9, power_mw=21.64,
            die_area_um2=die, core_area_um2=die * 0.98, congestion_limit=limit,
        )
        self.memory.record_run(
            RunResult(config=config, reached_stage="finish", metrics=metrics, ok=True)
        )

    def test_real_wall_ignores_a_mock_congested_row(self):
        # Real: 80% clean, 85% congested.
        self._record(80.0, 0.9737, 35944.4, 1.0)
        self._record(85.0, 1.0179, 33850.5, 1.0)
        # Mock at the same utilization: no die area, congested on the 0.30 scale.
        self._record(80.0, 0.42, 0.0, 0.30)

        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.congestion_wall_util, 85.0)
        # And the real 80% point is still the best config, not walled off.
        self.assertEqual(seed.best_config.core_utilization, 80.0)

    def test_mock_only_design_has_no_real_wall(self):
        self._record(80.0, 0.42, 0.0, 0.30)
        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertIsNone(seed.congestion_wall_util)


class TestWallRules(unittest.TestCase):
    """The three rules the wall got wrong in practice."""

    def setUp(self):
        self.memory = SQLiteMemory(":memory:")

    def _record(self, util, density, congestion, die, limit=1.0, ok=True, clock=2.2):
        config = FlowConfig(design="ibex", core_utilization=util, placement_density=density,
                            clock_period_ns=clock)
        metrics = Metrics(
            stage="finish", wns_ns=-0.01, tns_ns=-0.3, congestion=congestion,
            cell_count=22894, area_um2=29287.9, power_mw=21.64,
            die_area_um2=die, core_area_um2=die * 0.98, congestion_limit=limit,
        )
        self.memory.record_run(
            RunResult(config=config, reached_stage="finish", metrics=metrics, ok=ok)
        )

    def _wall(self, density=0.0, clock=2.2):
        """Walls are keyed by (density, clock); clock None means the rows were
        recorded without one."""
        seed = self.memory.seed_for("ibex", "nangate45", "route")
        return seed.congestion_wall_by_density.get((density, clock))

    def test_c_clean_real_run_at_same_density_cannot_be_the_wall(self):
        """Rule (c). Even with a contradictory congested row at the same
        utilization and density, a clean real run there wins."""
        self._record(80.0, 0.0, 0.9737, 35944.4)          # real, clean
        self._record(80.0, 0.0, 1.0100, 35900.0)          # contradictory, congested
        self._record(85.0, 0.0, 1.0179, 33850.5)          # real, congested

        self.assertEqual(self._wall(0.0), 85.0)

    def test_a_mock_row_never_walls_a_utilization(self):
        self._record(80.0, 0.0, 0.9737, 35944.4)
        self._record(85.0, 0.0, 1.0179, 33850.5)
        # Mock scale: congests above 0.30, so 0.42 is "congested" there.
        self._record(80.0, 0.5, 0.4200, 0.0, limit=0.30)

        self.assertEqual(self._wall(0.0), 85.0)
        self.assertIsNone(self._wall(0.5), "a mock row must not create a wall")

    def test_b_wall_is_per_density(self):
        self._record(80.0, 0.0, 0.9737, 35944.4)          # clean at auto density
        self._record(80.0, 0.9, 1.0500, 35000.0)          # congested at density 0.9

        self.assertIsNone(self._wall(0.0))
        self.assertEqual(self._wall(0.9), 80.0)

    def test_seed_wall_follows_the_best_configs_density(self):
        self._record(80.0, 0.0, 0.9737, 35944.4)
        self._record(85.0, 0.0, 1.0179, 33850.5)

        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.best_config.placement_density, 0.0)
        self.assertEqual(seed.congestion_wall_util, 85.0)


class TestPurgeSynthetic(unittest.TestCase):
    def test_purge_removes_only_non_real_rows(self):
        memory = SQLiteMemory(":memory:")

        def record(design, limit, die):
            config = FlowConfig(design=design, core_utilization=80.0, placement_density=0.0)
            metrics = Metrics(
                stage="finish", wns_ns=-0.01, tns_ns=-0.3, congestion=0.42 if limit < 1 else 0.97,
                cell_count=1, area_um2=1.0, power_mw=1.0, die_area_um2=die,
                core_area_um2=die, congestion_limit=limit,
            )
            memory.record_run(
                RunResult(config=config, reached_stage="finish", metrics=metrics, ok=True)
            )

        record("ibex", 1.0, 35944.4)   # real
        record("ibex", 0.30, 0.0)      # mock

        result = memory.purge_synthetic_runs()

        self.assertEqual(result["count"], 1)
        self.assertIn("mock", result["purged"][0])
        self.assertEqual(memory.describe()["total_runs"], 1)
        memory.close()
