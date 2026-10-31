import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adaptive_iicpilot import EDAAgent, FlowConfig, MockORFSAdapter, ORFSAdapter
from adaptive_iicpilot.eda_interface import make_adapter


class TestEDAAgent(unittest.TestCase):
    def test_run_flow_reaches_place(self):
        agent = EDAAgent(MockORFSAdapter())
        config = FlowConfig(design="gcd")
        result = agent.run_flow(config, target_stage="place")

        self.assertEqual(result.reached_stage, "place")
        self.assertTrue(result.ok)
        self.assertGreater(result.metrics.area_um2, 0)
        self.assertGreater(result.metrics.power_mw, 0)


class TestORFSAdapter(unittest.TestCase):
    """The adapter is implemented as of Milestone C; these cover the parts
    that must hold without invoking a real 10-30 minute flow."""

    def test_collect_before_run_raises(self):
        adapter = ORFSAdapter()
        adapter.configure(FlowConfig(design="ibex"))
        with self.assertRaises(RuntimeError):
            adapter.collect("route")

    def test_cache_key_tracks_knobs_only(self):
        adapter = ORFSAdapter()
        base = FlowConfig(design="ibex", core_utilization=50.0)

        self.assertEqual(adapter.cache_key(base), adapter.cache_key(base.tweaked()))
        # Clock IS a knob now, so it must split the cache.
        self.assertNotEqual(
            adapter.cache_key(base), adapter.cache_key(base.tweaked(clock_period_ns=9.9))
        )
        self.assertNotEqual(
            adapter.cache_key(base), adapter.cache_key(base.tweaked(core_utilization=90.0))
        )

    def test_default_clock_keys_the_same_as_the_explicit_stock_clock(self):
        """clock 0 means "stock", so it must not create a second cache entry
        for a run that already exists under the stock period."""
        adapter = ORFSAdapter()
        base = FlowConfig(design="ibex", core_utilization=80.0, placement_density=0.0)
        stock = adapter.stock_clock_ns("ibex")

        self.assertEqual(stock, 2.2)
        self.assertEqual(
            adapter.cache_key(base), adapter.cache_key(base.tweaked(clock_period_ns=stock))
        )

    def test_cache_roundtrip_skips_the_flow(self):
        from adaptive_iicpilot.types import Metrics, RunResult

        with tempfile.TemporaryDirectory() as tmp:
            adapter = ORFSAdapter(cache_dir=Path(tmp))
            config = FlowConfig(design="ibex", core_utilization=50.0)
            metrics = Metrics(
                stage="finish", wns_ns=-0.0155, tns_ns=-1.371, congestion=0.97027,
                cell_count=31761, area_um2=29426.8, power_mw=22.1484,
                die_area_um2=60000.0, core_area_um2=48000.0, congestion_limit=1.0,
            )
            adapter._store_cached(
                config, RunResult(config=config, reached_stage="finish", metrics=metrics)
            )

            adapter.configure(config)
            result = adapter.run("synth", "route")

            self.assertEqual(result.metrics.die_area_um2, 60000.0)
            self.assertFalse(result.metrics.congested)


class TestBackendSelection(unittest.TestCase):
    def test_defaults_to_mock(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADAPTIVE_IICPILOT_BACKEND", None)
            self.assertIsInstance(make_adapter(), MockORFSAdapter)

    def test_orfs_selected_by_env(self):
        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_BACKEND": "orfs"}):
            self.assertIsInstance(make_adapter(), ORFSAdapter)


class TestCongestionGate(unittest.TestCase):
    """Real nangate45 congestion runs 0..1+; the mock's scale tops out near
    0.5. The gate travels with the metrics so both stay correct."""

    def _metrics(self, congestion: float, **kw):
        from adaptive_iicpilot.types import Metrics

        return Metrics(
            stage="finish", wns_ns=-0.02, tns_ns=-1.4, congestion=congestion,
            cell_count=1000, area_um2=29000.0, power_mw=22.0, **kw
        )

    def test_real_gate_at_one(self):
        # Measured ground truth: util 50 -> 0.970 clean, util 90/0.95 -> 1.041 congested.
        clean = self._metrics(0.97027, die_area_um2=60000.0, congestion_limit=1.0)
        congested = self._metrics(1.0413, die_area_um2=55000.0, congestion_limit=1.0)

        self.assertFalse(clean.congested)
        self.assertTrue(congested.congested)
        # A smaller die does not win if it is congested.
        self.assertGreater(clean.ppa_score(), congested.ppa_score())

    def test_mock_gate_unchanged(self):
        self.assertTrue(self._metrics(0.31).congested)
        self.assertFalse(self._metrics(0.29).congested)


if __name__ == "__main__":
    unittest.main()
