"""SignoffAgent: KLayout DRC / router DRC / antenna parsing, memory keyed by
ConfigKey, the chat routes, and optimize(signoff=True)."""
import os
import unittest
from pathlib import Path
from unittest import mock

from adaptive_iicpilot.configkey import ConfigKey
from adaptive_iicpilot.signoff import (DECK, LVS_STATUS, SignoffAgent, SignoffMemory, parse_lyrdb,
                                       parse_route_log, rule_category, verdict_line)

REPO = Path(__file__).resolve().parents[1]
IBEX_LYRDB = REPO / "signoff" / "runs" / "ibex" / "fp_full_6a78245a18ff" / "6_drc.lyrdb"

LYRDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <top-cell>top</top-cell>
 <categories>
  <category><name>METAL2.3</name></category>
  <category><name>METAL2.1</name></category>
  <category><name>CONTACT.3</name></category>
  <category><name>METAL4_ANTENNA</name></category>
 </categories>
 <items>
  <item><category>'METAL2.3'</category><multiplicity>1</multiplicity></item>
  <item><category>'METAL2.3'</category><multiplicity>2</multiplicity></item>
  <item><category>'METAL2.1'</category></item>
  <item><category>'METAL4_ANTENNA'</category><multiplicity>1</multiplicity></item>
 </items>
</report-database>
"""

ROUTE_LOG = """
[INFO DRT-0199]   Number of violations = 5390.
[INFO DRT-0199]   Number of violations = 85.
[INFO DRT-0199]   Number of violations = 3.
[INFO ANT-0002] Found 4 net violations.
[INFO ANT-0001] Found 7 pin violations.
[INFO ANT-0002] Found 1 net violations.
[INFO ANT-0001] Found 2 pin violations.
"""

KEY = ConfigKey(design="ibex", tech="nangate45", util=85.0, density=0.0, aspect=0.5, margin=None,
                pin_strategy="default", macro_strategy="stock", clock_ns=2.2)


def clean_result(**over) -> dict:
    result = {"design": "ibex", "label": KEY.label(), "variant": "fp_full_6a78245a18ff", "gds": "x.gds",
              "drc": {"total": 0, "rules_checked": 129, "by_category": {"METAL1": 0}, "violating_rules": {},
                      "deck": "signoff/decks/FreePDK45_full.lydrc", "lyrdb": "l"},
              "router": {"router_drc": 0, "antenna_net": 0, "antenna_pin": 0},
              "lvs": LVS_STATUS, "clean": True, "elapsed_s": 158.0}
    result.update(over)
    return result


class TestParsers(unittest.TestCase):
    def test_lyrdb_counts_per_rule_and_category_with_multiplicity(self):
        out = parse_lyrdb(LYRDB)
        self.assertEqual(out["total"], 5)
        self.assertEqual(out["rules_checked"], 4)
        self.assertEqual(out["violating_rules"], {"METAL2.1": 1, "METAL2.3": 3, "METAL4_ANTENNA": 1})
        self.assertEqual(out["by_category"], {"ANTENNA": 1, "CONTACT": 0, "METAL2": 4})

    def test_rule_categories(self):
        self.assertEqual(rule_category("METAL2.3"), "METAL2")
        self.assertEqual(rule_category("CONTACT.3"), "CONTACT")
        self.assertEqual(rule_category("METAL7_ANTENNA"), "ANTENNA")

    def test_route_log_takes_the_final_counts(self):
        self.assertEqual(parse_route_log(ROUTE_LOG),
                         {"router_drc": 3, "router_iterations": 3, "antenna_net": 1, "antenna_pin": 2})

    def test_route_log_without_the_lines_is_unknown_not_zero(self):
        self.assertEqual(parse_route_log("nothing here"),
                         {"router_drc": None, "router_iterations": 0, "antenna_net": None, "antenna_pin": None})

    def test_the_known_ibex_result_is_clean(self):
        """ibex util85/AR0.5 (fp_full_6a78245a18ff): the committed report of
        the real KLayout run has 0 violations over the deck's 129 rules."""
        out = parse_lyrdb(IBEX_LYRDB.read_text(encoding="utf-8"))
        self.assertEqual((out["total"], out["rules_checked"], out["top_cell"]), (0, 129, "ibex_core"))
        self.assertIn("ANTENNA", out["by_category"])


class TestDeck(unittest.TestCase):
    def test_the_repo_deck_has_the_contact3_rewrite(self):
        text = DECK.read_text(encoding="utf-8")
        self.assertIn('cont.not(active).not(poly).not(metal1).output("CONTACT.3"', text)
        self.assertIn("source($in_gds)", text)
        self.assertIn("$report_file", text)
        self.assertEqual(DECK.relative_to(REPO).as_posix(), "signoff/decks/FreePDK45_full.lydrc")


class FakeAdapter:
    orfs_root = "~/OpenROAD-flow-scripts"

    def __init__(self, files=None, write_lyrdb=None):
        self.files = files or {}
        self.write_lyrdb = write_lyrdb
        self.commands = []

    def _design_nickname(self, design):
        return design

    def _shell(self, command, timeout=None, on_line=None):
        self.commands.append(command)
        if command.startswith("test -s "):
            return command.split()[2] in self.files, ""
        if command.startswith("cat "):
            path = command.split(" ", 1)[1]
            return (path in self.files), self.files.get(path, "")
        if command.startswith("klayout "):
            report = command.split("report_file=", 1)[1].split()[0]
            if self.write_lyrdb is not None:
                Path(report.replace("/mnt/c/", "C:/") if os.name == "nt" else report).write_text(
                    self.write_lyrdb, encoding="utf-8")
            return True, ""
        return False, ""


def point(variant="fp_full_6a78245a18ff", origin=".orfs_cache/ibex-431e5e2960cb73a9.json", ok=True):
    return mock.MagicMock(key=KEY, variant=variant, origin=origin, ok=ok)


class TestSignoffAgent(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = SignoffMemory(":memory:")
        flow = "~/OpenROAD-flow-scripts/flow"
        self.gds = f"{flow}/results/nangate45/ibex/fp_full_6a78245a18ff/6_final.gds"
        self.log = f"{flow}/logs/nangate45/ibex/fp_full_6a78245a18ff/5_2_route.log"

    def agent(self, adapter):
        return SignoffAgent(adapter=adapter, memory=self.memory, emit=lambda line: None,
                            runs_dir=Path(self.tmp.name))

    def test_measures_parses_stores_and_never_writes_the_orfs_tree(self):
        adapter = FakeAdapter(files={self.gds: "", self.log: ROUTE_LOG}, write_lyrdb=LYRDB)
        result = self.agent(adapter).signoff(KEY, point=point())
        self.assertEqual(result["source"], "MEASURED")
        self.assertEqual(result["drc"]["total"], 5)
        self.assertEqual(result["router"]["router_drc"], 3)
        self.assertFalse(result["clean"])
        self.assertTrue(result["verdict"].startswith("DRC NOT CLEAN - ibex util 85, AR 0.5, 2.2 ns"))
        self.assertIn("worst: METAL2.3 3", result["verdict"])
        klayout = next(c for c in adapter.commands if c.startswith("klayout "))
        self.assertIn(f"in_gds={self.gds}", klayout)
        report = klayout.split("report_file=", 1)[1].split()[0]
        self.assertNotIn("OpenROAD-flow-scripts", report)
        # Stored under the run's ConfigKey and replayed from memory.
        again = self.agent(FakeAdapter()).signoff(KEY)
        self.assertEqual(again["source"], "MEMORY")
        self.assertEqual(again["drc"]["violating_rules"], result["drc"]["violating_rules"])
        self.assertEqual(again["verdict"], result["verdict"])

    def test_the_variant_falls_back_to_the_clock_sweep_run(self):
        sweep_gds = "~/OpenROAD-flow-scripts/flow/results/nangate45/ibex/clk_431e5e2960cb73a9/6_final.gds"
        agent = self.agent(FakeAdapter(files={sweep_gds: ""}))
        self.assertEqual(agent.variant_of(point(variant=None)), "clk_431e5e2960cb73a9")

    def test_a_legacy_run_without_its_gds_is_not_run(self):
        result = self.agent(FakeAdapter()).signoff(KEY, point=point(variant=None))
        self.assertEqual(result["source"], "NOT_RUN")
        self.assertIn("6_final.gds no longer exists", result["verdict"])

    def test_unmeasured_and_memory_only_are_not_run_never_estimated(self):
        agent = self.agent(FakeAdapter(files={self.gds: ""}))
        self.assertEqual(agent.signoff(KEY, point=point(ok=False))["source"], "NOT_RUN")
        result = agent.signoff(KEY, point=point(), run=False)
        self.assertEqual(result["source"], "NOT_RUN")
        self.assertIn("has not been DRC-checked yet", result["verdict"])
        self.assertFalse(any(c.startswith("klayout") for c in agent.adapter.commands))

    def test_verdict_line_for_the_known_clean_result(self):
        self.assertEqual(
            verdict_line(clean_result()),
            "DRC CLEAN - ibex util 85, AR 0.5, 2.2 ns (fp_full_6a78245a18ff): KLayout FreePDK45 DRC "
            "0 violation(s) over 129 rules; router DRT-0199 0; antenna 0 net / 0 pin (ANT-0002/ANT-0001); "
            "LVS not available (ORFS ships no Nangate45 LVS deck)")


class TestChatSignoff(unittest.TestCase):
    def session(self):
        from adaptive_iicpilot import chat

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
            return chat.Session(emit=lambda line: None)

    def test_routes(self):
        from adaptive_iicpilot.chat import route_intent

        self.assertEqual(route_intent("is ibex DRC clean?")[:2], ("signoff", {"design": "ibex", "run": False}))
        self.assertEqual(route_intent("run DRC on the best ibex")[:2], ("signoff", {"design": "ibex", "run": True}))
        self.assertEqual(route_intent("is the best ibex layout DRC clean?")[0], "signoff")
        self.assertEqual(route_intent("optimize ibex and run signoff")[:2],
                         ("optimize", {"design": "ibex", "objective": "die_area", "signoff": True}))
        self.assertEqual(route_intent("check DRC for ibex at util 85 AR 0.5")[1],
                         {"design": "ibex", "run": True, "util": 85.0, "aspect": 0.5})

    def test_best_config_is_signed_off_with_one_verdict_line(self):
        s = self.session()
        lines = []
        s.emit = lines.append
        row = {"util": 85.0, "aspect_ratio": 0.5, "clock_ns": 2.2, "core_margin": None}
        stored = {**clean_result(), "source": "MEMORY"}
        stored["verdict"] = verdict_line(stored)
        with mock.patch.object(s, "_select_by_objective", return_value=(row, None)), \
                mock.patch.object(SignoffAgent, "signoff", return_value=stored) as signoff:
            result = s.signoff(design="ibex")
        self.assertEqual(signoff.call_args.args[0], KEY)
        self.assertFalse(signoff.call_args.kwargs["run"])
        self.assertTrue(result["verdict"].startswith("DRC CLEAN"))
        self.assertEqual(sum(line.startswith("[Verdict]") for line in lines), 1)

    def test_the_signoff_reply_is_the_verdict_line_without_narration(self):
        from adaptive_iicpilot import chat

        s = self.session()
        client = mock.MagicMock()
        replies = []
        verdict = verdict_line(clean_result())
        with mock.patch.object(s, "dispatch", return_value={"verdict": verdict, "source": "MEMORY"}):
            chat.run_turn(client, s, "is the best ibex layout DRC clean?", lambda: None, replies.append,
                          lambda r: None)
        self.assertEqual(replies, [verdict])
        client.chat.completions.create.assert_not_called()

    def test_optimize_signs_off_only_the_final_best(self):
        from adaptive_iicpilot import chat

        s = self.session()
        stored = {**clean_result(), "source": "MEMORY"}
        stored["verdict"] = verdict_line(stored)
        row = {"util": 85.0, "aspect_ratio": 0.5, "clock_ns": 2.2, "core_margin": None, "floorplan": "util 85, AR 0.5",
               "die_area_um2": 33895.8, "power_mw": 21.7, "fmax_mhz": 450.8}
        report = mock.MagicMock(pipeline=None, seeded=True, history=[], floorplan=None)
        with mock.patch("adaptive_iicpilot.web.compat.Orchestrator") as orch, \
                mock.patch("adaptive_iicpilot.measurements.Measurements.points", return_value=[object()]), \
                mock.patch.object(s, "tradeoff", return_value={"measured_points": []}), \
                mock.patch.object(s, "_select_by_objective", return_value=(row, {"verdict": "CLEAN - x"})), \
                mock.patch.object(s, "_show_renders", return_value=[]), \
                mock.patch.object(SignoffAgent, "signoff", return_value=stored) as signoff:
            orch.return_value.run.return_value = report
            result = s.optimize(design="ibex", signoff=True)
        self.assertEqual(signoff.call_count, 1)
        self.assertEqual(signoff.call_args.args[0], KEY)
        self.assertIn("signoff: DRC CLEAN", result["verdict"])


if __name__ == "__main__":
    unittest.main()
