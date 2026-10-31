"""Floorplan <-> DSE integration: one ConfigKey, the screened pipeline, memory
seeded on the full key, deterministic intent routing, renders in the UI.

Hermetic: fakes stand in for ORFS and the measurement index, so these tests
pin the contracts rather than whatever happens to be measured today.
"""
import base64
import os
import tempfile
import unittest
from unittest import mock

from adaptive_iicpilot.configkey import (
    ConfigKey, from_floorplan_knobs, from_flow_config, from_row, macro_strategy_of,
)
from adaptive_iicpilot.measurements import FULL, SCREEN, Point
from adaptive_iicpilot.pipeline import ScreenStats, clock_for_objective, propose, screened_search
from adaptive_iicpilot.sqlite_memory import SQLiteMemory
from adaptive_iicpilot.types import FlowConfig, MemorySeed, Metrics, RunResult


def key(util, aspect=1.0, clock=2.2, **kw):
    return ConfigKey(design="ibex", tech="nangate45", util=util, density=0.0, aspect=aspect,
                     margin=kw.get("margin"), pin_strategy=kw.get("pins", "default"),
                     macro_strategy=kw.get("macros", "stock"), clock_ns=clock)


class TestConfigKey(unittest.TestCase):
    def test_the_same_run_from_both_producers_is_one_key(self):
        dse = from_flow_config(FlowConfig(design="ibex", core_utilization=85.0, placement_density=0.0,
                                          aspect_ratio=0.5, clock_period_ns=0.0), 2.2)
        fp = from_floorplan_knobs({"design": "ibex", "core_utilization": 85.0, "aspect_ratio": 0.5,
                                   "io_strategy": "default", "macro_mode": "none"}, 50.0, 2.2)
        self.assertEqual(dse, fp)

    def test_stock_values_resolve(self):
        # aspect None == 1.0, util None == the design's stock util
        fp = from_floorplan_knobs({"design": "ibex", "core_utilization": None, "aspect_ratio": None}, 50.0, 2.2)
        self.assertEqual(fp, key(50.0))

    def test_old_rows_without_floorplan_columns_mean_stock(self):
        row = {"design": "ibex", "tech": "nangate45", "util": 80.0, "density": 0.0, "aspect": 1.0,
               "clock_ns": 2.2}
        self.assertEqual(from_row(row), key(80.0))
        self.assertTrue(from_row(row).is_stock_floorplan())

    def test_floorplan_knobs_survive_a_round_trip(self):
        k = key(70.0, aspect=0.75, margin=5.0, pins="bus_sides", macros="rtlmp halo=10x10")
        self.assertEqual(from_flow_config(k.to_flow_config(), None), k)
        stock = key(80.0)
        self.assertIsNone(stock.to_flow_config().floorplan, "stock knobs must keep old cache keys")

    def test_macro_strategy_names(self):
        self.assertEqual(macro_strategy_of("none"), "stock")
        self.assertEqual(macro_strategy_of("auto"), "stock")  # RTL-MP at the platform halo
        self.assertEqual(macro_strategy_of("auto", macro_halo=(10.0, 10.0)), "rtlmp halo=10x10")
        self.assertEqual(macro_strategy_of("manual", "periphery"), "manual:periphery")

    def test_floats_do_not_split_a_key(self):
        self.assertEqual(key(85), key(85.000001))


def _record(memory, util, aspect, congestion, die, ok=True, clock=2.2, pins="default"):
    floorplan = {"io_strategy": pins} if pins != "default" else None
    config = FlowConfig(design="ibex", core_utilization=util, placement_density=0.0,
                        aspect_ratio=aspect, clock_period_ns=clock, floorplan=floorplan)
    metrics = Metrics(stage="finish", wns_ns=-0.01, tns_ns=-0.1, congestion=congestion, cell_count=1,
                      area_um2=1.0, power_mw=21.0, die_area_um2=die if ok else 0.0, congestion_limit=1.0)
    memory.record_run(RunResult(config=config, reached_stage="finish", metrics=metrics, ok=ok))


class TestMemoryOnTheFullKey(unittest.TestCase):
    def setUp(self):
        self.memory = SQLiteMemory(":memory:")
        self.addCleanup(self.memory.close)

    def test_warm_seed_is_the_best_measured_config_with_its_floorplan(self):
        _record(self.memory, 80.0, 1.0, 0.974, 35944.4)
        _record(self.memory, 85.0, 1.0, 1.018, 33850.5)   # congested at AR 1
        _record(self.memory, 85.0, 0.5, 0.982, 33895.8)   # clean at AR 0.5
        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual((seed.best_config.core_utilization, seed.best_config.aspect_ratio), (85.0, 0.5))
        self.assertEqual(seed.best_die_area_um2, 33895.8)

    def test_walls_are_per_floorplan(self):
        _record(self.memory, 85.0, 1.0, 1.018, 33850.5)
        _record(self.memory, 85.0, 0.5, 0.982, 33895.8)
        _record(self.memory, 85.0, 0.5, 1.024, 33895.8, pins="bus_sides")
        seed = self.memory.seed_for("ibex", "nangate45", "route")
        self.assertEqual(seed.congestion_walls[key(85.0).wall_key], 85.0)
        self.assertNotIn(key(85.0, 0.5).wall_key, seed.congestion_walls)
        self.assertEqual(seed.congestion_walls[key(85.0, 0.5, pins="bus_sides").wall_key], 85.0)
        # the stock-floorplan view older callers read
        self.assertEqual(seed.congestion_wall_by_density[(0.0, 2.2)], 85.0)
        # the seed's own wall follows ITS floorplan (AR 0.5: none known)
        self.assertIsNone(seed.congestion_wall_util)

    def test_aspect_is_part_of_the_identity_on_ingest(self):
        _record(self.memory, 85.0, 1.0, 1.018, 33850.5)
        self.assertTrue(self.memory.has_key(key(85.0), ok=True))
        self.assertFalse(self.memory.has_key(key(85.0, 0.5), ok=True))

    def test_a_replay_does_not_add_a_row_but_another_floorplan_does(self):
        _record(self.memory, 85.0, 0.5, 0.982, 33895.8)
        _record(self.memory, 85.0, 0.5, 0.982, 33895.8)
        _record(self.memory, 85.0, 0.5, 0.982, 33895.8, pins="bus_sides")
        rows = self.memory._conn.execute("SELECT replays FROM runs ORDER BY id").fetchall()
        self.assertEqual([r["replays"] for r in rows], [2, 1])


def _point(k, kind, congestion, die, ok=True, wns=-0.01, power=21.0, cp=2.2, elapsed=480.0):
    m = {"congestion": congestion, "die_area_um2": die, "wns_ns": wns if kind == FULL else None,
         "power_mw": power, "cp_delay_ns": cp if kind == FULL else None}
    return Point(key=k, kind=kind, source="floorplan", ok=ok, metrics=m, elapsed_s=elapsed)


class FakeIndex:
    def __init__(self, screens, fulls, median=900.0):
        self.screens, self.fulls, self.median = screens, fulls, median

    def points(self, design, kind=FULL):
        return list(self.screens if kind == SCREEN else self.fulls)

    def median_full_seconds(self, design):
        return self.median


class FakeAdapter:
    def __init__(self, fulls):
        self.fulls = {p.key: p for p in fulls}

    def stock_clock_ns(self, design):
        return 2.2

    def is_cached(self, config):
        return from_flow_config(config, 2.2) in self.fulls


class FakeEDA:
    def __init__(self, adapter):
        self.adapter, self.ran = adapter, []

    def run_flow(self, config, stage):
        p = self.adapter.fulls[from_flow_config(config, 2.2)]
        self.ran.append(p.key)
        m = Metrics(stage="finish", wns_ns=p.metrics["wns_ns"], tns_ns=0.0, congestion=p.congestion,
                    cell_count=1, area_um2=1.0, power_mw=21.0, die_area_um2=p.metrics["die_area_um2"],
                    timing_threshold_ns=-0.11, congestion_limit=1.0)
        return RunResult(config=config, reached_stage="finish", metrics=m, ok=p.ok)


class TestScreenedPipeline(unittest.TestCase):
    def _run(self, screens, fulls, seed=None, allow=False):
        adapter = FakeAdapter(fulls)
        eda = FakeEDA(adapter)
        lines = []
        best, history, stats = screened_search(
            FlowConfig(design="ibex", core_utilization=50.0, placement_density=0.0), seed, eda, adapter,
            lines.append, lambda p, c: p, allow_screen_runs=allow, allow_full_runs=allow,
            index=FakeIndex(screens, fulls))
        return best, history, stats, eda, lines

    def test_full_flows_run_only_on_survivors_smallest_die_first(self):
        screens = [_point(key(85, 0.5), SCREEN, 0.982, 33895.8), _point(key(85), SCREEN, 1.018, 33850.5),
                   _point(key(80), SCREEN, 0.974, 35944.4), _point(key(90, 0.5), SCREEN, 1.039, 32033.7)]
        fulls = [_point(key(85, 0.5), FULL, 0.982, 33895.8), _point(key(80), FULL, 0.974, 35944.4)]
        best, _h, stats, eda, _l = self._run(screens, fulls)
        self.assertEqual(eda.ran, [key(85, 0.5)], "stops at the first clean survivor")
        self.assertEqual(best.metrics.die_area_um2, 33895.8)
        self.assertEqual({p["label"] for p in stats.pruned}, {key(85).label(False), key(90, 0.5).label(False)})
        self.assertEqual(stats.full_runs_replayed, 1)

    def test_a_survivor_that_congests_in_the_full_flow_falls_through(self):
        screens = [_point(key(85, 0.5), SCREEN, 0.99, 33895.8), _point(key(80), SCREEN, 0.974, 35944.4)]
        fulls = [_point(key(85, 0.5), FULL, 1.01, 33895.8), _point(key(80), FULL, 0.974, 35944.4)]
        best, _h, _s, eda, _l = self._run(screens, fulls)
        self.assertEqual(eda.ran, [key(85, 0.5), key(80)])
        self.assertEqual(best.metrics.die_area_um2, 35944.4)

    def test_unmeasured_candidates_are_not_run_when_live_runs_are_off(self):
        best, _h, stats, _e, _l = self._run([], [])
        self.assertIsNone(best)
        self.assertEqual(len(stats.screens_not_run), len(stats.candidates))
        self.assertEqual(stats.full_runs_avoided, 0, "an unscreened candidate saved nothing")

    def test_time_saved_is_an_estimate_from_measured_times(self):
        screens = [_point(key(85, 0.5), SCREEN, 0.98, 33895.8, elapsed=500.0),
                   _point(key(90, 0.5), SCREEN, 1.04, 32033.7, elapsed=400.0)]
        fulls = [_point(key(85, 0.5), FULL, 0.98, 33895.8)]
        _b, _h, stats, _e, _l = self._run(screens, fulls)
        self.assertEqual(stats.full_runs_avoided, 1)
        self.assertEqual(stats.time_saved_s, 1 * 900.0 - 900.0)
        self.assertTrue(stats.as_dict()["time_saved_is_estimate"])

    def test_warm_proposal_starts_from_memory_and_respects_its_wall(self):
        seed = MemorySeed(best_config=key(85, 0.5).to_flow_config(),
                          congestion_walls={key(85, 0.5).wall_key: 90.0})
        stats = ScreenStats()
        cands = propose(FlowConfig(design="ibex", core_utilization=50.0, placement_density=0.0),
                        seed, 2.2, lambda line: None, stats)
        self.assertEqual(cands, [key(85, 0.5)])
        self.assertEqual(stats.mode, "warm")
        self.assertTrue(stats.skipped_by_memory)

    def test_cold_proposal_is_the_util_by_aspect_grid(self):
        stats = ScreenStats()
        cands = propose(FlowConfig(design="ibex", core_utilization=50.0, placement_density=0.0),
                        None, 2.2, lambda line: None, stats)
        self.assertEqual(len(cands), 12)
        self.assertIn(key(85, 0.5), cands)


class TestClockOnTheBestFloorplan(unittest.TestCase):
    def setUp(self):
        fp = key(85, 0.5)
        self.fulls = [
            _point(fp, FULL, 0.98, 33895.8, wns=-0.018, power=21.70, cp=2.2183),
            _point(fp.tweaked(clock_ns=2.8), FULL, 0.97, 33895.8, wns=0.19, power=16.9, cp=2.61),
            _point(fp.tweaked(clock_ns=1.98), FULL, 0.98, 33895.8, wns=-0.2, power=25.0, cp=2.18),  # VIOLATED
            _point(key(80, clock=2.8), FULL, 0.97, 35944.4, wns=0.19, power=16.0, cp=2.6),  # other floorplan
        ]
        self.index = FakeIndex([], self.fulls)

    def test_power_picks_the_lowest_power_clock_on_that_floorplan_only(self):
        chosen, _ = clock_for_objective(self.index, key(85, 0.5), "power", lambda c: -0.11)
        self.assertEqual(chosen.key, key(85, 0.5).tweaked(clock_ns=2.8))

    def test_timing_picks_highest_fmax_never_a_violated_point(self):
        chosen, usable = clock_for_objective(self.index, key(85, 0.5), "timing", lambda c: -0.11)
        self.assertEqual(chosen.key.clock_ns, 2.2)
        self.assertNotIn(1.98, [p.key.clock_ns for p in usable])


class TestIntentRouting(unittest.TestCase):
    def test_acceptance_prompts(self):
        from adaptive_iicpilot.chat import route_intent

        cases = {
            "optimize ibex": ("optimize", "die_area"),
            "reduce ibex power": ("optimize", "power"),
            "make ibex as fast as possible": ("optimize", "timing"),
        }
        for text, (tool, objective) in cases.items():
            name, args, _ = route_intent(text)
            self.assertEqual((name, args["objective"], args["design"]), (tool, objective, "ibex"), text)
        name, args, _ = route_intent("show me the best floorplan for ibex")
        self.assertEqual((name, args["mode"]), ("floorplan", "best"))
        name, args, _ = route_intent("compare macro placements for tinyRocket")
        self.assertEqual((name, args["design"], args["strategies"]),
                         ("floorplan", "tinyRocket", ["macros_auto", "macros_manual"]))

    def test_layout_words_route_to_the_floorplan_tool(self):
        from adaptive_iicpilot.chat import route_intent

        for text in ("what aspect ratio should gcd use", "show the layout of ibex", "where do the pins go"):
            self.assertEqual(route_intent(text)[0], "floorplan", text)

    def test_open_questions_are_left_to_the_model(self):
        from adaptive_iicpilot.chat import route_intent

        for text in ("why did util 90 congest?", "what do you remember about ibex?", "hello"):
            self.assertIsNone(route_intent(text), text)

    def test_a_routed_turn_never_offers_tools_to_the_model(self):
        from adaptive_iicpilot import chat

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
            session = chat.Session(emit=lambda line: None)
        client = mock.MagicMock()
        client.chat.completions.create.return_value.choices = [
            mock.MagicMock(message=mock.MagicMock(content="Power is lower at a slower clock."))]
        replies = []
        with mock.patch.object(session, "dispatch", return_value={"verdict": "CLEAN | reg-to-reg fmax 1 MHz"}) as d:
            chat.run_turn(client, session, "reduce ibex power", lambda: None, replies.append, replies.append)
        d.assert_called_once_with("optimize", {"design": "ibex", "objective": "power"})
        self.assertNotIn("tools", client.chat.completions.create.call_args.kwargs)

    def test_model_down_falls_back_to_the_verdict(self):
        from adaptive_iicpilot import chat

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
            session = chat.Session(emit=lambda line: None)
        client = mock.MagicMock()
        client.chat.completions.create.side_effect = ConnectionError("down")
        replies = []
        with mock.patch.object(session, "dispatch", return_value={"verdict": "CLEAN | the verdict"}):
            chat.run_turn(client, session, "make ibex as fast as possible", lambda: None,
                          replies.append, replies.append)
        self.assertEqual(replies, ["CLEAN | the verdict"])


class TestRendersReachTheUI(unittest.TestCase):
    def test_png_renders_are_sent_as_base64_frames_with_the_verdict(self):
        from adaptive_iicpilot import chat

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "placement.png")
            with open(path, "wb") as handle:
                handle.write(b"\x89PNG fake")
            frames = []
            with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
                session = chat.Session(emit=lambda line: None, on_image=frames.append)
            row = {"floorplan": "util 85, AR 0.5", "clock_ns": 2.2, "renders": {"placement": path}}
            shown = session._show_renders("ibex", row, "best floorplan", "CLEAN | v", views=("placement", "congestion"))
        self.assertEqual(shown, [path])
        self.assertEqual(base64.b64decode(frames[0]["png"]), b"\x89PNG fake")
        self.assertEqual(frames[0]["verdict"], "CLEAN | v")

    def test_floorplan_log_lines_keep_their_own_agent_tag(self):
        from adaptive_iicpilot.chat import _split_log_line

        self.assertEqual(_split_log_line("[Floorplan] screen util 85")[0], "Floorplan")


class TestFloorplanResultsCarryRegToRegFmax(unittest.TestCase):
    def test_metrics_from_uses_the_real_clock(self):
        from adaptive_iicpilot.floorplan.knobs import FloorplanKnobs
        from adaptive_iicpilot.floorplan.runner import FloorplanRunner

        from adaptive_iicpilot.eda_interface import ORFSAdapter

        runner = FloorplanRunner.__new__(FloorplanRunner)
        runner.orfs = ORFSAdapter()
        # Only the methods that would read the ORFS tree are stubbed.
        for name, value in (("timing_threshold_ns", -0.11), ("stock_clock_ns", 2.2),
                            ("real_clock_name", "core_clock")):
            patcher = mock.patch.object(runner.orfs, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        meta = {"finish__timing__fmax": 547968000.0,
                "finish__timing__fmax__clock__core_clock": 450782000.0,
                "finish__timing__fmax__clock__vclk_core_clock": 547968000.0}
        m = runner.metrics_from(meta, FloorplanKnobs(design="ibex", core_utilization=85.0, aspect_ratio=0.5), {})
        self.assertAlmostEqual(1000.0 / m["cp_delay_ns"], 450.8, places=1)
        self.assertEqual(m["fmax_clock"], "core_clock")
        self.assertEqual(m["orfs_fmax_headline_clock"], "vclk_core_clock")


    def _runner(self):
        from adaptive_iicpilot.eda_interface import ORFSAdapter
        from adaptive_iicpilot.floorplan.runner import FloorplanRunner

        runner = FloorplanRunner.__new__(FloorplanRunner)
        runner.orfs = ORFSAdapter()
        for name, value in (("timing_threshold_ns", -0.11), ("stock_clock_ns", 2.2),
                            ("real_clock_name", "core_clock")):
            patcher = mock.patch.object(runner.orfs, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return runner

    def test_a_floorplan_full_runs_fmax_equals_the_real_clock_key(self):
        """End to end: metadata -> FloorplanRunner.metrics_from -> the agent's
        result row. ibex util 85 / AR 0.5 at 2.75 ns (clk_c0603bbe50080e91):
        core_clock 384.2 MHz, headline (virtual I/O clock) 484.5 MHz."""
        from adaptive_iicpilot.floorplan.agent import FloorplanAgent
        from adaptive_iicpilot.floorplan.knobs import FloorplanKnobs

        meta = {"finish__timing__fmax": 484463000.0,
                "finish__timing__fmax__clock__core_clock": 384157000.0,
                "finish__timing__fmax__clock__vclk_core_clock": 484463000.0,
                "finish__timing__setup__ws": 0.146895}
        knobs = FloorplanKnobs(design="ibex", core_utilization=85.0, aspect_ratio=0.5)
        metrics = self._runner().metrics_from(meta, knobs, {})
        row = FloorplanAgent.row({"metrics": metrics, "stage": "FULL", "ok": True}, 1, "balanced", [])
        real_mhz = meta["finish__timing__fmax__clock__core_clock"] / 1e6
        self.assertAlmostEqual(row["fmax_mhz"], real_mhz, places=1)
        self.assertNotAlmostEqual(row["fmax_mhz"], meta["finish__timing__fmax"] / 1e6, places=0)

    def test_without_the_real_clock_key_fmax_is_unknown_not_the_headline(self):
        from adaptive_iicpilot.floorplan.agent import FloorplanAgent
        from adaptive_iicpilot.floorplan.knobs import FloorplanKnobs

        meta = {"finish__timing__fmax": 484463000.0,
                "finish__timing__fmax__clock__vclk_core_clock": 484463000.0}
        metrics = self._runner().metrics_from(meta, FloorplanKnobs(design="ibex"), {})
        row = FloorplanAgent.row({"metrics": metrics, "stage": "FULL", "ok": True}, 1, "balanced", [])
        self.assertIsNone(row["fmax_mhz"])
        self.assertIn("core_clock", metrics["cp_delay_note"])

    def test_stored_floorplan_full_runs_never_carry_the_virtual_clock(self):
        """The committed FULL results: fmax comes from a real clock, and where
        ORFS's headline is a different (virtual) clock it is not the fmax."""
        import glob
        import json

        root = os.path.join(os.path.dirname(__file__), "..", ".floorplan_runs")
        checked = 0
        for path in glob.glob(os.path.join(root, "*", "fp_full_*", "result.json")):
            with open(path, encoding="utf-8") as handle:
                m = json.load(handle)["metrics"]
            if not m.get("cp_delay_ns"):
                continue
            checked += 1
            self.assertFalse(m["fmax_clock"].startswith("vclk"), path)
            if m.get("orfs_fmax_headline_clock") not in (None, m["fmax_clock"]):
                self.assertNotAlmostEqual(1e9 / m["cp_delay_ns"], m["orfs_fmax_headline_hz"], delta=1e5)
        self.assertGreater(checked, 0)

    def test_the_util85_ar05_clock_runs_store_the_real_clock_fmax(self):
        """ibex util 85 / AR 0.5 at 2.75 and 2.8 ns: the stored fmax is the
        core_clock one (384.2 / 376.9 MHz), consistent with clock - WNS since
        the WNS path is reg2reg, never ORFS's virtual-clock headline (484.5 /
        479.6 MHz)."""
        import json

        cache = os.path.join(os.path.dirname(__file__), "..", ".orfs_cache")
        for name, clock, mhz in (("ibex-c0603bbe50080e91.json", 2.75, 384.2),
                                 ("ibex-a90782748e33e08b.json", 2.8, 376.9)):
            with open(os.path.join(cache, name), encoding="utf-8") as handle:
                entry = json.load(handle)
            m = entry["metrics"]
            self.assertEqual((entry["config"]["core_utilization"], entry["config"]["aspect_ratio"],
                              entry["config"]["clock_period_ns"]), (85.0, 0.5, clock))
            self.assertEqual(m["fmax_clock"], "core_clock")
            self.assertAlmostEqual(1000.0 / m["cp_delay_ns"], mhz, places=1)
            self.assertAlmostEqual(m["cp_delay_ns"], clock - m["wns_ns"], places=4)
            self.assertGreater(m["orfs_fmax_headline_hz"] / 1e6 - mhz, 90)


class TestClockSweepLog(unittest.TestCase):
    def test_landed_line_reports_reg2reg_fmax_and_labels_the_headline(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "clock_sweep", os.path.join(os.path.dirname(__file__), "..", "clock_sweep.py"))
        sweep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sweep)
        raw = {"wns": 0.146895, "tns": 0, "fmax_hz": 484463000.0, "power_w": 0.017275,
               "die": 33895.8, "congestion": 0.95706, "drc": 0}
        metrics = {"cp_delay_ns": 2.6031023774134012, "fmax_clock": "core_clock",
                   "orfs_fmax_headline_clock": "vclk_core_clock"}
        line = sweep.landed_line("ibex", 2.75, {"ok": True, "problems": []}, True, 876.0, raw, metrics)
        self.assertIn("reg2reg_fmax=384.2MHz (core_clock)", line)
        self.assertIn("orfs headline 484.5MHz from vclk_core_clock, not fmax", line)
        self.assertNotIn(" fmax=484.5", line)


class TestChatFixes(unittest.TestCase):
    def session(self, **kw):
        from adaptive_iicpilot import chat

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
            return chat.Session(emit=lambda line: None, **kw)

    def test_knobs_are_extracted_from_the_request(self):
        from adaptive_iicpilot.chat import extract_knobs

        self.assertEqual(extract_knobs("ibex at util 85 AR 0.5 at 2.75 ns"),
                         {"util": 85.0, "aspect": 0.5, "clock_ns": 2.75})
        self.assertEqual(extract_knobs("85% utilization, aspect ratio of 0.75, margin 2"),
                         {"util": 85.0, "aspect": 0.75, "margin": 2.0})
        self.assertEqual(extract_knobs("optimize ibex"), {})

    def test_a_named_config_routes_to_mode_config(self):
        from adaptive_iicpilot.chat import route_intent

        name, args, _ = route_intent("show me the ibex floorplan at util 85 AR 0.5 at 2.75 ns")
        self.assertEqual((name, args), ("floorplan", {"design": "ibex", "mode": "config", "util": 85.0,
                                                      "aspect": 0.5, "clock_ns": 2.75}))
        name, args, _ = route_intent("what was measured for ibex at util 80?")
        self.assertEqual((name, args["mode"], args["util"]), ("floorplan", "config", 80.0))
        # Without numbers the existing modes still apply.
        self.assertEqual(route_intent("compare aspect ratios for gcd")[1]["mode"], "compare")
        self.assertEqual(route_intent("show me the best floorplan for ibex")[1]["mode"], "best")

    def _rows(self):
        base = {"core_margin": None, "pin_strategy": "default", "macro_strategy": "stock", "ok": True,
                "congested": False, "timing_status": "MET", "timing_threshold_ns": -0.11, "renders": None}
        return [dict(base, util=85.0, aspect_ratio=0.5, floorplan="util 85, AR 0.5", clock_ns=c,
                     die_area_um2=33895.8, congestion=0.957, wns_ns=w, fmax_mhz=f, power_mw=p)
                for c, w, f, p in ((2.75, 0.1469, 384.2, 17.275), (2.2, -0.0184, 450.8, 21.7))]

    def test_config_mode_answers_from_the_full_run_of_that_key(self):
        s = self.session()
        with mock.patch.object(s, "tradeoff", return_value={"measured_points": self._rows()}):
            result = s.floorplan(design="ibex", util=85, aspect=0.5, clock_ns=2.75)
        self.assertEqual(result["mode"], "config")
        self.assertEqual(result["measured"]["clock_ns"], 2.75)
        self.assertIn("reg-to-reg fmax 384.2 MHz", result["verdict"])
        self.assertIn("also measured on this floorplan at 2.2 ns", result["verdict"])

    def test_config_mode_never_estimates_an_unmeasured_key(self):
        from adaptive_iicpilot.measurements import Measurements

        s = self.session()
        with mock.patch.object(s, "tradeoff", return_value={"measured_points": self._rows()}), \
                mock.patch.object(Measurements, "points", return_value=[]):
            result = s.floorplan(design="ibex", util=70, aspect=0.5)
        self.assertEqual(result["source"], "NOT_RUN")
        self.assertIn("NOT_RUN", result["verdict"])

    def test_a_turn_starts_with_no_verdict_from_the_last_one(self):
        from adaptive_iicpilot import chat

        s = self.session()
        s.last_verdict = {"verdict": "CONGESTED - old", "source": "x", "objective": None}
        s.last_record = {"verdict": "old"}
        client = mock.MagicMock()
        client.chat.completions.create.return_value.choices = [mock.MagicMock(message=mock.MagicMock(content="Hi."))]
        chat.run_turn(client, s, "hello", lambda: None, lambda r: None, lambda r: None)
        self.assertIsNone(s.last_verdict)
        self.assertIsNone(s.last_record)

    def test_routed_narration_sees_only_this_turn(self):
        from adaptive_iicpilot import chat

        s = self.session()
        s.messages += [{"role": "user", "content": "reduce ibex power"},
                       {"role": "assistant", "content": "OLD ANSWER 16.9 mW"}]
        client = mock.MagicMock()
        client.chat.completions.create.return_value.choices = [
            mock.MagicMock(message=mock.MagicMock(content="Fastest is 2.2 ns."))]
        with mock.patch.object(s, "dispatch", return_value={"verdict": "CLEAN | fmax 450.8 MHz"}):
            chat.run_turn(client, s, "make ibex as fast as possible", lambda: None, lambda r: None, lambda r: None)
        sent = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])
        self.assertNotIn("OLD ANSWER", json_dump(sent))
        self.assertEqual(sent[1]["content"], "make ibex as fast as possible")
        # The session history still has both turns for follow-up questions.
        self.assertIn("OLD ANSWER 16.9 mW", json_dump(s.messages))

    def test_repeated_sentences_are_removed_from_the_reply(self):
        from adaptive_iicpilot.chat import dedupe_reply

        text = ("The best floorplan is util 85, AR 0.5. It is routable.\n\n"
                "The best floorplan is util 85, AR 0.5. Power is 21.7 mW.")
        self.assertEqual(dedupe_reply(text),
                         "The best floorplan is util 85, AR 0.5. It is routable.\n\nPower is 21.7 mW.")

    def test_the_verdict_goes_on_one_picture_per_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            renders = {}
            for view in ("placement", "congestion"):
                renders[view] = os.path.join(tmp, f"{view}.png")
                with open(renders[view], "wb") as handle:
                    handle.write(b"\x89PNG")
            frames = []
            s = self.session(on_image=frames.append)
            row = {"floorplan": "util 85, AR 0.5", "clock_ns": 2.2, "renders": renders}
            s._show_renders("ibex", row, "best", "CLEAN | v")
        self.assertEqual([f["verdict"] for f in frames], ["CLEAN | v", None])

    def test_a_macro_comparison_runs_each_placement_style_in_full(self):
        from adaptive_iicpilot.floorplan.agent import FloorplanAgent

        s = self.session()
        empty = {"table": [], "best": None, "studies": [], "best_svg_path": None, "screen_vs_full": [],
                 "config_mk": {"command_line_required": {}}}
        with mock.patch.object(FloorplanAgent, "run", return_value=empty) as run:
            s.floorplan(design="tinyRocket", mode="compare", strategies=["macros_auto", "macros_manual"])
            self.assertEqual(run.call_args.kwargs["full"], "per_family")
            s.floorplan(design="ibex", mode="compare", strategies=["aspect"])
            self.assertEqual(run.call_args.kwargs["full"], "best")

    def test_fallback_is_short(self):
        from adaptive_iicpilot.chat import FALLBACK_MAX_CHARS, short_fallback

        long = "CLEAN - routable at util 85, AR 0.5 | " + " | ".join(f"segment {i} " + "x" * 60 for i in range(10))
        out = short_fallback(long)
        self.assertLessEqual(len(out), FALLBACK_MAX_CHARS + len(" ... (full verdict in the log)"))
        self.assertTrue(out.startswith("CLEAN - routable at util 85, AR 0.5"))
        self.assertTrue(out.endswith("(full verdict in the log)"))
        self.assertEqual(short_fallback("CLEAN | short"), "CLEAN | short")


class TestFloorplanTimingStatus(unittest.TestCase):
    def test_tinyrocket_runs_are_classified_against_the_rules_base_limit(self):
        from adaptive_iicpilot.floorplan.agent import timing_status

        def run(wns):
            return {"stage": "FULL", "metrics": {"wns_ns": wns, "timing_threshold_ns": -0.154}}

        self.assertEqual(timing_status(run(-0.123793)), "ACCEPTABLE")  # RTL-MP halo 10x10
        self.assertEqual(timing_status(run(-0.139247)), "ACCEPTABLE")  # manual periphery
        self.assertEqual(timing_status(run(-0.2)), "VIOLATED")
        self.assertEqual(timing_status(run(0.01)), "MET")
        self.assertIsNone(timing_status({"stage": "SCREEN", "metrics": {"wns_ns": -0.2}}))


def json_dump(value) -> str:
    import json

    return json.dumps(value, default=str)

if __name__ == "__main__":
    unittest.main()
