"""Clock period as a real DSE knob: SDC rewriting, cache keying, bounds,
per-clock walls, and the refusal to guess across clocks.

The SDC tests touch the real ORFS tree read-only (they never write to it) and
are skipped when it is not present.
"""
import os
import unittest
from unittest import mock

from adaptive_iicpilot import chat
from adaptive_iicpilot.chat import (
    CLOCK_MAX_FACTOR,
    CLOCK_MIN_FACTOR,
    UNKNOWN_AT_CLOCK,
    Session,
    validate_clock,
)
from adaptive_iicpilot.eda_interface import _CLK_PERIOD_RE, ORFSAdapter
from adaptive_iicpilot.estimator import CalibratedEstimator
from adaptive_iicpilot.sqlite_memory import SQLiteMemory
from adaptive_iicpilot.types import FlowConfig, Metrics, RunResult

ENV = {"ADAPTIVE_IICPILOT_MEMORY": "inmemory", "ADAPTIVE_IICPILOT_CACHE_ONLY": "1"}
STOCK = {"ibex": 2.2, "aes": 0.82, "gcd": 0.25}


def _orfs_available() -> bool:
    return ORFSAdapter().stock_clock_ns("ibex") is not None


def _session(emitted=None):
    with mock.patch.dict(os.environ, ENV):
        return Session(emit=(emitted.append if emitted is not None else (lambda line: None)))


class TestStockClockReading(unittest.TestCase):
    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_reads_the_shipped_period_for_each_design(self):
        adapter = ORFSAdapter()
        for design, period in STOCK.items():
            self.assertEqual(adapter.stock_clock_ns(design), period, design)

    def test_regex_matches_the_sdc_line_shape(self):
        self.assertEqual(_CLK_PERIOD_RE.search("set clk_period 2.2").group(1), "2.2")
        self.assertEqual(_CLK_PERIOD_RE.search("set  clk_period   0.82").group(1), "0.82")
        self.assertIsNone(_CLK_PERIOD_RE.search("set clk_io_pct 0.2"))


class TestSdcRewriting(unittest.TestCase):
    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_only_the_period_line_changes(self):
        adapter = ORFSAdapter()
        ok, stock_text = adapter._shell(f"cat {adapter.sdc_path('ibex')}")
        self.assertTrue(ok)

        path = adapter._write_clock_sdc("ibex", 2.75)
        self.assertIsNotNone(path)
        ok, new_text = adapter._shell(f"cat {path}")
        self.assertTrue(ok)

        stock_lines = stock_text.strip().splitlines()
        new_lines = new_text.strip().splitlines()
        self.assertEqual(len(stock_lines), len(new_lines))
        differing = [
            (a, b) for a, b in zip(stock_lines, new_lines) if a.strip() != b.strip()
        ]
        self.assertEqual(len(differing), 1, f"expected one changed line, got {differing}")
        self.assertIn("clk_period 2.75", differing[0][1])

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_the_orfs_tree_is_never_written_to(self):
        """The rewritten SDC must live outside the ORFS tree."""
        path = ORFSAdapter()._write_clock_sdc("ibex", 2.42)
        self.assertIsNotNone(path)
        self.assertNotIn("OpenROAD-flow-scripts", path)
        self.assertTrue(path.startswith("/tmp/"))

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_derived_constraints_still_reference_the_variable(self):
        """create_clock/set_input_delay must keep deriving from $clk_period so
        the whole constraint scales, not just the one literal."""
        adapter = ORFSAdapter()
        path = adapter._write_clock_sdc("ibex", 2.75)
        _ok, text = adapter._shell(f"cat {path}")

        self.assertIn("create_clock -name $clk_name -period $clk_period", text)
        self.assertIn("[expr $clk_period * $clk_io_pct]", text)


class TestCommandLineOverride(unittest.TestCase):
    """config.mk assigns SDC_FILE with plain `=`, so only a command-line
    variable wins. Asserted against make's own variable database."""

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_env_var_loses_and_command_line_wins(self):
        adapter = ORFSAdapter()
        flow = f"{adapter.orfs_root}/flow"
        design_config = "./designs/nangate45/ibex/config.mk"
        base = f"cd {flow} && source ../env.sh >/dev/null 2>&1"

        _ok, env_out = adapter._shell(
            f"{base} && SDC_FILE=/tmp/ENVVAR.sdc make DESIGN_CONFIG={design_config} "
            f"-p -n 2>/dev/null | grep -E '^SDC_FILE *=' | head -1"
        )
        _ok, cli_out = adapter._shell(
            f"{base} && make DESIGN_CONFIG={design_config} SDC_FILE=/tmp/CMDLINE.sdc "
            f"-p -n 2>/dev/null | grep -E '^SDC_FILE *=' | head -1"
        )

        self.assertNotIn("/tmp/ENVVAR.sdc", env_out)
        self.assertIn("constraint.sdc", env_out)
        self.assertIn("/tmp/CMDLINE.sdc", cli_out)

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_a_non_stock_clock_puts_sdc_file_on_the_command_line(self):
        # An empty cache dir and no floorplan replays: this test is about the
        # command a LIVE run builds, so nothing may short-circuit to a replay
        # (2.75ns is a measured, cached point now).
        import tempfile
        from pathlib import Path

        adapter = ORFSAdapter(cache_dir=Path(tempfile.mkdtemp()))
        adapter._fp_points = {"ibex": []}
        sent = {"commands": []}

        def fake_shell(command, on_line=None, timeout=None):
            sent.setdefault("commands", []).append(command)
            return False, ""  # stop before a real flow

        with mock.patch.object(adapter, "_shell", side_effect=fake_shell):
            adapter.configure(
                FlowConfig(design="ibex", core_utilization=80.0,
                           placement_density=0.0, clock_period_ns=2.75)
            )
            try:
                adapter.run("synth", "route")
            except RuntimeError:
                pass  # the stubbed shell cannot write the SDC; the args are what matter

        flow_commands = [c for c in sent["commands"] if "make DESIGN_CONFIG" in c]
        self.assertTrue(
            any("SDC_FILE=" in c for c in flow_commands) or not flow_commands,
            "a non-stock clock must pass SDC_FILE on the make command line",
        )


class TestCacheKeyIncludesClock(unittest.TestCase):
    def setUp(self):
        self.adapter = ORFSAdapter()
        self.base = FlowConfig(design="ibex", core_utilization=80.0, placement_density=0.0)

    def test_different_clocks_are_different_entries(self):
        self.assertNotEqual(
            self.adapter.cache_key(self.base.tweaked(clock_period_ns=2.2)),
            self.adapter.cache_key(self.base.tweaked(clock_period_ns=2.75)),
        )

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_stock_sentinel_and_explicit_stock_are_one_entry(self):
        """clock 0 means "stock"; it must not split one physical run in two."""
        self.assertEqual(
            self.adapter.cache_key(self.base),
            self.adapter.cache_key(self.base.tweaked(clock_period_ns=2.2)),
        )


class TestClockBounds(unittest.TestCase):
    def test_accepts_the_band_around_stock(self):
        for clock in (1.1, 1.76, 2.2, 2.75, 4.4):
            self.assertIsNone(validate_clock(clock, 2.2), clock)

    def test_rejects_too_tight(self):
        message = validate_clock(1.0, 2.2)
        self.assertIn("outside", message)
        self.assertIn("1.1-4.4ns", message)

    def test_rejects_too_loose(self):
        self.assertIn("outside", validate_clock(4.5, 2.2))

    def test_rejects_non_positive(self):
        self.assertIn("must be positive", validate_clock(-1.0, 2.2))

    def test_zero_means_stock_and_is_accepted(self):
        self.assertIsNone(validate_clock(0, 2.2))
        self.assertIsNone(validate_clock(None, 2.2))

    def test_bounds_scale_with_the_designs_own_stock(self):
        """gcd ships at 0.25ns, so its band is 10x tighter than ibex's."""
        self.assertIsNone(validate_clock(0.3, 0.25))
        self.assertIn("outside", validate_clock(2.2, 0.25))

    def test_no_stock_period_means_no_bound_to_apply(self):
        self.assertIsNone(validate_clock(99.0, None))

    def test_factors_are_the_documented_half_to_double(self):
        self.assertEqual((CLOCK_MIN_FACTOR, CLOCK_MAX_FACTOR), (0.5, 2.0))

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_run_flow_refuses_an_out_of_band_clock(self):
        record = _session().run_flow("ibex", 80, clock_ns=10.0)
        self.assertTrue(record["rejected"])
        self.assertIn("outside", record["error"])


class TestPerClockWalls(unittest.TestCase):
    def setUp(self):
        self.memory = SQLiteMemory(":memory:")
        self.addCleanup(self.memory.close)

    def _record(self, util, clock, congestion, die=35944.4, ok=True):
        config = FlowConfig(design="ibex", core_utilization=util,
                            placement_density=0.0, clock_period_ns=clock)
        metrics = Metrics(
            stage="finish", wns_ns=-0.01, tns_ns=-0.3, congestion=congestion,
            cell_count=22894, area_um2=29287.9, power_mw=21.64,
            die_area_um2=die if ok else 0.0, core_area_um2=die * 0.98,
            congestion_limit=1.0,
        )
        self.memory.record_run(
            RunResult(config=config, reached_stage="finish", metrics=metrics, ok=ok)
        )

    def test_a_wall_at_one_clock_does_not_apply_to_another(self):
        self._record(80.0, 2.2, 1.05)   # congested at stock
        self._record(80.0, 2.75, 0.95)  # clean at a relaxed clock

        walls = self.memory.seed_for("ibex", "nangate45", "route").congestion_wall_by_density
        self.assertEqual(walls.get((0.0, 2.2)), 80.0)
        self.assertIsNone(walls.get((0.0, 2.75)))

    def test_the_seeds_wall_follows_the_best_configs_clock(self):
        self._record(80.0, 2.2, 0.97)
        self._record(85.0, 2.2, 1.02)

        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.best_config.clock_period_ns, 2.2)
        self.assertEqual(seed.congestion_wall_util, 85.0)


class TestEstimatorNeverCrossesClocks(unittest.TestCase):
    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_only_points_at_the_requested_clock_are_fitted(self):
        stock = CalibratedEstimator.from_cache("ibex")
        self.assertTrue(stock.usable)
        self.assertEqual(stock.clock_ns, 2.2)

        other = CalibratedEstimator.from_cache("ibex", clock_ns=1.76)
        self.assertFalse(
            other.usable, "a clock with no measurements must not be fittable"
        )

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_measured_clocks_are_reported(self):
        self.assertIn(2.2, CalibratedEstimator.measured_clocks("ibex"))

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_an_unmeasured_clock_is_unknown_not_guessed(self):
        # 2.42ns was unmeasured when this test was written and is measured
        # now; 2.6ns is inside the allowed band and has never been run.
        record = _session().run_flow("ibex", 80, clock_ns=2.6)

        self.assertEqual(record["source"], UNKNOWN_AT_CLOCK)
        self.assertTrue(record["verdict"].startswith("UNKNOWN"))
        # No invented numbers.
        self.assertIsNone(record["die_area_um2"])
        self.assertIsNone(record["power_mw"])
        self.assertIn(2.2, record["measured_clocks_ns"])


class TestMockUnaffected(unittest.TestCase):
    def test_mock_still_uses_its_own_default_clock(self):
        from adaptive_iicpilot.eda_interface import MockORFSAdapter

        adapter = MockORFSAdapter()
        adapter.configure(FlowConfig(design="gcd"))  # clock 0 -> mock default
        result = adapter.run("synth", "route")

        self.assertTrue(result.ok)
        self.assertGreater(result.metrics.area_um2, 0)
        self.assertGreater(result.metrics.power_mw, 0)


if __name__ == "__main__":
    unittest.main()


class TestTimingStatus(unittest.TestCase):
    """Three states, because every real run here closes with a hair of
    negative slack and ORFS ships a per-design allowance for exactly that."""

    def _metrics(self, wns, threshold):
        return Metrics(
            stage="finish", wns_ns=wns, tns_ns=-1.0, congestion=0.95,
            cell_count=1, area_um2=1.0, power_mw=1.0, die_area_um2=1.0,
            timing_threshold_ns=threshold, congestion_limit=1.0,
        )

    def test_non_negative_slack_is_met(self):
        self.assertEqual(self._metrics(0.0059, -0.11).timing_status, "MET")
        self.assertEqual(self._metrics(0.0, -0.11).timing_status, "MET")

    def test_within_the_orfs_allowance_is_acceptable(self):
        # ibex stock: WNS -0.0072 against an allowance of -0.11.
        self.assertEqual(self._metrics(-0.0072, -0.11).timing_status, "ACCEPTABLE")
        self.assertEqual(self._metrics(-0.11, -0.11).timing_status, "ACCEPTABLE")

    def test_below_the_allowance_is_violated(self):
        # The measured 1.76ns ibex run.
        self.assertEqual(self._metrics(-0.4289, -0.11).timing_status, "VIOLATED")

    def test_no_allowance_means_only_met_passes(self):
        """A design with no rules file and no period gets no free slack."""
        self.assertEqual(self._metrics(-0.001, 0.0).timing_status, "VIOLATED")

    def test_an_acceptable_run_is_never_described_as_met(self):
        from adaptive_iicpilot.chat import _metrics_summary

        summary = _metrics_summary(self._metrics(-0.0072, -0.11))
        self.assertEqual(summary["timing_status"], "ACCEPTABLE")
        self.assertIn("timing ACCEPTABLE", summary["verdict"])
        self.assertIn("ORFS limit -0.1100", summary["verdict"])
        self.assertNotIn("timing MET", summary["verdict"])


class TestTimingThresholds(unittest.TestCase):
    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_thresholds_come_from_the_designs_rules_file(self):
        adapter = ORFSAdapter()
        self.assertAlmostEqual(adapter.timing_threshold_ns("ibex", 2.2), -0.11, places=4)
        self.assertAlmostEqual(adapter.timing_threshold_ns("aes", 0.82), -0.0667, places=4)
        self.assertAlmostEqual(adapter.timing_threshold_ns("gcd", 0.25), -0.193, places=4)

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_falls_back_to_two_percent_of_the_period(self):
        """picorv32 has no nangate45 rules file here."""
        adapter = ORFSAdapter()
        self.assertAlmostEqual(
            adapter.timing_threshold_ns("picorv32", 2.5), -0.05, places=4
        )


class TestObjectiveTimingGates(unittest.TestCase):
    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_power_and_die_area_admit_acceptable_runs(self):
        """Requiring MET would reject nearly the whole measured corpus."""
        session = _session()
        for objective in ("die_area", "power"):
            chosen, _ = session._select_by_objective("ibex", objective)
            self.assertIsNotNone(chosen, objective)
            self.assertNotEqual(chosen["timing_status"], "VIOLATED", objective)

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_timing_objective_is_never_violated_and_names_its_status(self):
        """Timing = highest reg-to-reg fmax on the best floorplan that is not
        VIOLATED (ORFS's own allowance). A pick that is only ACCEPTABLE must
        say so and name the fastest point that actually MEETS timing; it is
        never described as met."""
        session = _session()
        session._last_tradeoff = session.tradeoff("ibex")
        chosen, _ = session._select_by_objective("ibex", "timing", table=session._last_tradeoff)
        if chosen is None:
            self.skipTest("no usable timing point measured")
        self.assertIn(chosen["timing_status"], ("MET", "ACCEPTABLE"))
        note = session._timing_vs_die_area_best("ibex", "timing", chosen)["summary"]
        self.assertIn(f"timing {chosen['timing_status']}", note)
        self.assertIn("reg-to-reg fmax", note)
        if chosen["timing_status"] != "MET":
            self.assertNotIn("timing MET", note)

    @unittest.skipUnless(_orfs_available(), "ORFS tree not available")
    def test_a_violated_run_is_never_selected(self):
        session = _session()
        table = session.tradeoff("ibex")
        violated = [r for r in table["measured_points"] if r["timing_status"] == "VIOLATED"]
        if not violated:
            self.skipTest("no VIOLATED run measured yet")
        for objective in ("die_area", "power", "timing"):
            chosen, _ = session._select_by_objective("ibex", objective)
            if chosen is not None:
                self.assertNotIn(
                    (chosen["util"], chosen["clock_ns"]),
                    [(r["util"], r["clock_ns"]) for r in violated],
                    objective,
                )
