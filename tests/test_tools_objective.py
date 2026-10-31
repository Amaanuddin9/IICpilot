"""The objective parameter, the tradeoff table, and unknown-argument handling."""
import os
import unittest
from unittest import mock

from adaptive_iicpilot import chat
from adaptive_iicpilot.chat import OBJECTIVES, Session, contradiction_note

ENV = {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}


def _session(emitted=None):
    with mock.patch.dict(os.environ, ENV):
        return Session(emit=(emitted.append if emitted is not None else (lambda line: None)))


class TestTradeoffTable(unittest.TestCase):
    def test_returns_real_measured_points(self):
        table = _session().tradeoff("ibex")

        self.assertGreater(len(table["measured_points"]), 3)
        for row in table["measured_points"]:
            self.assertEqual(row["source"], "CACHED_ORFS")
            for key in ("util", "die_area_um2", "power_mw", "congestion", "wns_ns"):
                self.assertIn(key, row)

    def test_findings_answer_the_bigger_die_question_from_data(self):
        findings = " ".join(_session().tradeoff("ibex")["key_findings"])

        self.assertIn("Smallest routable die", findings)
        self.assertIn("Lowest power", findings)
        # The measured answer for ibex: power barely moves, so a bigger die does
        # not buy lower power.
        self.assertIn("does NOT buy lower power", findings)

    def test_runs_no_flow(self):
        emitted = []
        _session(emitted).tradeoff("ibex")
        self.assertFalse([line for line in emitted if line.startswith("[EDA]")])


class TestOptimizeObjective(unittest.TestCase):
    def test_die_area_is_the_default_and_is_reported(self):
        result = _session().optimize("ibex")

        self.assertEqual(result["objective"], "die_area")
        self.assertIn("die_area", result["objective_note"])

    def test_power_objective_picks_the_clock_on_the_die_area_floorplan(self):
        """Power is a clock decision applied on the best floorplan: same util
        and aspect as the die-area best, a relaxed clock, lower power."""
        by_die = _session().optimize("ibex", objective="die_area")
        by_power = _session().optimize("ibex", objective="power")

        self.assertEqual(by_power["objective"], "power")
        die, power = by_die["objective_best"], by_power["objective_best"]
        self.assertEqual(power["floorplan"], die["floorplan"])
        self.assertLessEqual(power["power_mw"], die["power_mw"])
        if power["clock_ns"] != die["clock_ns"]:
            self.assertGreater(power["clock_ns"], die["clock_ns"])

    def test_objective_best_respects_the_congestion_gate(self):
        chosen = _session().optimize("ibex", objective="power")["objective_best"]
        self.assertFalse(chosen["congested"])
        self.assertLessEqual(chosen["congestion"], 1.0)

    def test_unknown_objective_is_refused_with_the_valid_list(self):
        result = _session().optimize("ibex", objective="area51")

        self.assertIn("error", result)
        self.assertEqual(result["valid_objectives"], sorted(OBJECTIVES))

    def test_reply_claiming_the_other_objective_is_corrected(self):
        last = {
            "verdict": "CLEAN - congestion 0.9737 is BELOW the 1.0 limit",
            "source": "CACHED_ORFS",
            "objective": "die_area",
        }
        note = contradiction_note("I minimised power for you.", last)

        self.assertIsNotNone(note)
        self.assertIn("objective was die_area", note)

    def test_faithful_objective_reply_is_accepted(self):
        last = {
            "verdict": "CLEAN - congestion 0.9737 is BELOW the 1.0 limit",
            "source": "CACHED_ORFS",
            "objective": "die_area",
        }
        self.assertIsNone(contradiction_note("Minimised die area to 35944.4 um2.", last))


class TestUnknownArguments(unittest.TestCase):
    def test_unknown_arg_is_dropped_and_named(self):
        emitted = []
        session = _session(emitted)
        with mock.patch.object(chat, "_live_runs_allowed", return_value=False):
            result = session.dispatch("run_flow", {"design": "ibex", "util": 80,
                                                   "clock_period": 2.0})

        # The call still succeeded rather than raising a TypeError.
        self.assertIn("verdict", result)
        self.assertIn("clock_period", result["ignored_parameters"])
        self.assertIn("Do not offer any other parameter", result["ignored_parameters"])
        self.assertTrue([line for line in emitted if "ignored unknown parameter" in line])

    def test_valid_parameters_are_listed(self):
        session = _session()
        result = session.dispatch("tradeoff", {"design": "ibex", "nonsense": 1})

        for param in ("design",):
            self.assertIn(param, result["ignored_parameters"])

    def test_unknown_tool_lists_the_real_tools(self):
        result = _session().dispatch("make_chip", {})

        self.assertIn("error", result)
        self.assertIn("optimize", result["valid_tools"])
        self.assertIn("tradeoff", result["valid_tools"])

    def test_known_args_still_work_untouched(self):
        result = _session().dispatch("tradeoff", {"design": "ibex"})
        self.assertNotIn("ignored_parameters", result)


if __name__ == "__main__":
    unittest.main()


class TestPowerComparison(unittest.TestCase):
    """Utilization is a weak power knob; the result must say so rather than
    presenting a rounding-error saving as a win."""

    def test_power_objective_reports_percent_vs_die_area_best(self):
        result = _session().optimize("ibex", objective="power")
        comparison = result["power_vs_die_area_best"]

        self.assertIsNotNone(comparison)
        self.assertIn("percent", comparison)
        self.assertIn("baseline_mw", comparison)
        self.assertLess(comparison["percent"], 0.0)  # power objective is not worse

    def test_a_power_saving_states_its_frequency_cost(self):
        """Measured ibex: relaxing the clock on the best floorplan saves power;
        the verdict must carry the clock change and the reg-to-reg fmax cost."""
        result = _session().optimize("ibex", objective="power")
        comparison = result["power_vs_die_area_best"]
        if comparison["clock_ns"] == comparison["baseline_clock_ns"]:
            self.skipTest("no relaxed clock measured on the best floorplan")
        self.assertLess(comparison["percent"], -1.0)
        self.assertFalse(comparison["negligible"])
        self.assertIn("clock relaxed", result["verdict"])
        self.assertIn("reg-to-reg fmax", result["verdict"])

    def test_sub_one_percent_saving_is_called_negligible_in_the_verdict(self):
        """Guard the threshold on fixed data: 21.579 vs 21.643 mW is -0.30%."""
        session = _session()
        chosen = {"util": 75.0, "power_mw": 21.579, "die_area_um2": 38314.1, "clock_ns": 2.2}
        with mock.patch.object(
            type(session), "_select_by_objective",
            return_value=({"util": 80.0, "power_mw": 21.643, "die_area_um2": 35944.4, "clock_ns": 2.2}, None),
        ):
            comparison = session._power_vs_die_area_best("ibex", "power", chosen)

        self.assertLess(abs(comparison["percent"]), 1.0)
        self.assertTrue(comparison["negligible"])
        self.assertIn("negligible (<1%)", comparison["summary"])
        self.assertIn("clock period is the real power knob", comparison["summary"])

    def test_die_area_objective_has_no_power_comparison(self):
        result = _session().optimize("ibex", objective="die_area")

        self.assertIsNone(result["power_vs_die_area_best"])
        self.assertNotIn("negligible", result["verdict"])

    def test_a_large_saving_would_not_be_called_negligible(self):
        """Guard the threshold itself: a 10% saving must not be dismissed."""
        session = _session()
        chosen = {"util": 75.0, "power_mw": 19.0, "die_area_um2": 38314.1}
        with mock.patch.object(
            type(session), "_select_by_objective",
            return_value=({"util": 80.0, "power_mw": 21.643, "die_area_um2": 35944.4}, None),
        ):
            comparison = session._power_vs_die_area_best("ibex", "power", chosen)

        self.assertFalse(comparison["negligible"])
        self.assertNotIn("negligible", comparison["summary"])
        self.assertLess(comparison["percent"], -10.0)


class TestNegligiblePowerIsNotSoldAsAWin(unittest.TestCase):
    VERDICT = {
        "verdict": ("CLEAN - congestion 0.9523 is BELOW the 1.0 limit | power 21.579 mW "
                    "vs 21.643 mW at the die-area best (util 80%): -0.30%; difference is "
                    "negligible (<1%) at this clock; the clock period is the real power knob"),
        "source": "CACHED_ORFS",
        "objective": "power",
    }

    def test_reply_omitting_the_caveat_gets_corrected(self):
        reply = "The power for ibex has been decreased to 21.579 mW at 75% utilization."
        note = contradiction_note(reply, self.VERDICT)

        self.assertIsNotNone(note)
        self.assertIn("negligible (<1%)", note)
        self.assertIn("clock period", note)

    def test_reply_that_conveys_it_is_accepted(self):
        reply = ("Power drops to 21.579 mW, but the difference is negligible; "
                 "the clock period is the real power knob.")
        self.assertIsNone(contradiction_note(reply, self.VERDICT))

    def test_reply_saying_under_one_percent_is_accepted(self):
        reply = "Power improves by less than 1 % , which is within noise."
        self.assertIsNone(contradiction_note(reply, self.VERDICT))

    def test_a_meaningful_saving_is_not_flagged(self):
        verdict = {
            "verdict": "CLEAN - congestion 0.95 is BELOW the 1.0 limit | power -12.00%",
            "source": "CACHED_ORFS",
            "objective": "power",
        }
        self.assertIsNone(contradiction_note("Power dropped substantially.", verdict))


class TestFrequencyCostIsNeverHidden(unittest.TestCase):
    """A power saving bought by relaxing the clock costs frequency. Quoting the
    saving alone is a half-truth, so the reply must carry both sides."""

    VERDICT = {
        "verdict": ("CLEAN - congestion 0.95 is BELOW the 1.0 limit | power 18.0 mW vs "
                    "21.6 mW at the die-area best (util 80%): -16.67%; clock relaxed "
                    "2.2ns -> 2.75ns (+25.0%), so the design runs correspondingly "
                    "slower; fmax 455 -> 364 MHz"),
        "source": "CACHED_ORFS",
        "objective": "power",
    }

    def test_reply_quoting_only_the_saving_is_corrected(self):
        note = contradiction_note("Power is down to 18.0 mW.", self.VERDICT)

        self.assertIsNotNone(note)
        self.assertIn("relaxing the clock", note)
        self.assertIn("runs slower", note)

    def test_reply_mentioning_the_clock_is_accepted(self):
        self.assertIsNone(
            contradiction_note(
                "Power drops to 18.0 mW because the clock was relaxed to 2.75ns.",
                self.VERDICT,
            )
        )

    def test_reply_mentioning_fmax_is_accepted(self):
        self.assertIsNone(
            contradiction_note("Power 18.0 mW; fmax falls to 364 MHz.", self.VERDICT)
        )

    def test_a_saving_without_a_clock_change_is_not_flagged(self):
        """Utilization-only savings have no frequency cost to disclose."""
        verdict = {
            "verdict": "CLEAN - x | power 21.579 mW vs 21.643 mW: -0.30%",
            "source": "CACHED_ORFS",
            "objective": "power",
        }
        note = contradiction_note("Power is down slightly to 21.579 mW.", verdict)
        self.assertNotIn("relaxing the clock", note or "")
