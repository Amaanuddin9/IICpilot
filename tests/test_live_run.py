"""Live ORFS runs in the chat: stage condensing, raw log routing, run_status
frames, and the cache-only switch. Offline: the adapter is stubbed."""
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from adaptive_iicpilot.types import FlowConfig, Metrics, RunResult

ENV = {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}

# Verbatim lines from a real nangate45/gcd make run, one per stage marker,
# plus lines that name step files but must NOT count as a transition.
REAL_LINES = [
    "/home/u/OpenROAD-flow-scripts/flow/scripts/synth.sh /home/u/OpenROAD-flow-scripts/"
    "flow/scripts/synth_canonicalize.tcl ./logs/nangate45/gcd/base/1_1_yosys_canonicalize.log",
    "1_1_yosys_canonicalize    .rtlil          0             46 da967a0ae9319764d26c",
    "Running synth_odb.tcl, stage 1_synth",
    "/home/u/OpenROAD-flow-scripts/flow/scripts/flow.sh 2_1_floorplan floorplan",
    "Running floorplan.tcl, stage 2_1_floorplan",
    "write_db ./results/nangate45/gcd/base/2_1_floorplan.odb",
    "/home/u/OpenROAD-flow-scripts/flow/scripts/flow.sh 3_1_place_gp_skip_io global_place_skip_io",
    "read_sdc ./results/nangate45/gcd/base/2_1_floorplan.sdc",
    "/home/u/OpenROAD-flow-scripts/flow/scripts/flow.sh 4_1_cts cts",
    "/home/u/OpenROAD-flow-scripts/flow/scripts/flow.sh 5_1_grt global_route",
    "Running detail_route.tcl, stage 5_2_route",
    "/home/u/OpenROAD-flow-scripts/flow/scripts/flow.sh 6_1_merge merge",
    "6_report                                  3            244                  N/A",
]


def _result(ok=True):
    metrics = Metrics(stage="finish", wns_ns=-0.1, tns_ns=-1.0, congestion=0.7,
                      cell_count=1000, area_um2=900.0, power_mw=6.0)
    return RunResult(config=FlowConfig(design="gcd"), reached_stage="finish",
                     metrics=metrics, ok=ok, failure=None if ok else "boom")


class TestLiveRunTracker(unittest.TestCase):
    def _track(self, lines):
        from adaptive_iicpilot.chat import _LiveRunTracker

        emitted, statuses, raw = [], [], []
        tracker = _LiveRunTracker(emitted.append, statuses.append, raw.append)
        tracker.start()
        for line in lines:
            tracker(line)
        return tracker, emitted, statuses, raw

    def test_one_line_per_stage_in_order(self):
        _, emitted, _, _ = self._track(REAL_LINES)
        stages = [line.split()[1] for line in emitted]
        self.assertEqual(stages, ["synth", "floorplan", "place", "cts", "route", "finish"])
        for line in emitted:
            self.assertRegex(line, r"^\[EDA\] \w+ started \(\d\d:\d\d\)$")

    def test_every_raw_line_goes_to_raw_log(self):
        _, _, _, raw = self._track(REAL_LINES)
        self.assertEqual(raw, REAL_LINES)

    def test_earlier_stage_mention_does_not_step_back(self):
        _, emitted, _, _ = self._track([REAL_LINES[3], REAL_LINES[8], REAL_LINES[0]])
        self.assertEqual([line.split()[1] for line in emitted], ["floorplan", "cts"])

    def test_status_frames_and_summary(self):
        tracker, emitted, statuses, _ = self._track(REAL_LINES[:5])
        tracker.finish(_result(ok=True))
        self.assertEqual(statuses[0], {"state": "running", "stage": "starting",
                                       "elapsed_s": statuses[0]["elapsed_s"]})
        self.assertEqual([s["stage"] for s in statuses[1:-1]], ["synth", "floorplan"])
        self.assertEqual(statuses[-1]["state"], "done")
        self.assertRegex(emitted[-1], r"live run finished in \d\d:\d\d: reached finish")

    def test_failed_and_aborted_summaries(self):
        tracker, emitted, _, _ = self._track([])
        tracker.finish(_result(ok=False))
        self.assertIn("FAILED", emitted[-1])
        tracker.finish(None)
        self.assertIn("aborted", emitted[-1])


class _FakeLiveAdapter:
    """Replays REAL_LINES through on_line instead of invoking make."""

    def __init__(self, on_line=None, raises=False):
        self.on_line = on_line
        self.raises = raises
        self.cache_dir = None

    def is_cached(self, config):
        return False

    def configure(self, config):
        self.config = config

    def run(self, from_stage, to_stage):
        for line in REAL_LINES:
            self.on_line(line)
        if self.raises:
            raise RuntimeError("wsl died")
        return _result(ok=True)

    def collect(self, stage):
        return _result().metrics


class TestLiveRunFlow(unittest.TestCase):
    def _backend(self):
        from adaptive_iicpilot.chat_jsonl import JsonlBackend

        with mock.patch.dict(os.environ, ENV):
            return JsonlBackend()

    def _run_flow(self, raises=False):
        backend = self._backend()
        out, err = io.StringIO(), io.StringIO()
        fake = lambda on_line=None, **_: _FakeLiveAdapter(on_line, raises)  # noqa: E731
        with mock.patch("adaptive_iicpilot.chat.ORFSAdapter", side_effect=fake), \
                mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_CACHE_ONLY": ""}), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                backend.session.run_flow("gcd", 70)
            except RuntimeError:
                pass
        frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return frames, err.getvalue()

    def test_chat_gets_stages_not_raw_lines(self):
        frames, err = self._run_flow()
        eda = [f["text"] for f in frames if f.get("type") == "log" and f["agent"] == "EDA"]
        started = [t for t in eda if " started (" in t]
        self.assertEqual(len(started), 6)
        for raw in REAL_LINES:
            self.assertNotIn(raw, eda)
            self.assertIn(raw, err)
        self.assertTrue(any("live run finished" in t for t in eda))

    def test_run_status_opens_and_closes(self):
        frames, _ = self._run_flow()
        status = [f for f in frames if f["type"] == "run_status"]
        self.assertEqual(status[0]["state"], "running")
        self.assertEqual(status[-1]["state"], "done")
        self.assertEqual(len(status), 8)  # start + 6 stages + done

    def test_run_status_closes_even_if_run_raises(self):
        frames, _ = self._run_flow(raises=True)
        status = [f for f in frames if f["type"] == "run_status"]
        self.assertEqual(status[-1]["state"], "done")

    def test_failed_run_verdict_is_not_clean(self):
        backend = self._backend()
        failing = _FakeLiveAdapter(lambda line: None)
        failing.run = lambda *_: _result(ok=False)
        with mock.patch.object(type(backend.session), "_pick_adapter",
                               return_value=(failing, "REAL_ORFS", "stub", None)),                 redirect_stdout(io.StringIO()):
            record = backend.session.run_flow("gcd", 70)
        self.assertTrue(record["verdict"].startswith("FAILED"))
        # A failed flow must still carry its provenance.
        self.assertEqual(record["source"], "REAL_ORFS")

    def test_cache_only_falls_back_to_mock(self):
        from adaptive_iicpilot.chat import _live_runs_allowed

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_CACHE_ONLY": "1"}):
            self.assertFalse(_live_runs_allowed("gcd"))
        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_CACHE_ONLY": "0"}):
            self.assertTrue(_live_runs_allowed("gcd"))
            self.assertFalse(_live_runs_allowed("ibex"))


class TestFailedRunScore(unittest.TestCase):
    def test_flow_that_died_before_finish_scores_zero(self):
        # Real metadata from gcd util=70, which fails DPL-0038 at CTS.
        dead = Metrics(stage="cts", wns_ns=0.0, tns_ns=0.0, congestion=0.0,
                       cell_count=0, area_um2=0.0, power_mw=0.0, congestion_limit=1.0)
        self.assertEqual(dead.ppa_score(), 0.0)


if __name__ == "__main__":
    unittest.main()
