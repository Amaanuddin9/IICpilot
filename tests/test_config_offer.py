"""Chat floorplan mode="config": an unmeasured config gets an offer that
runs only after the engineer confirms it, and a request that leaves
utilization out is never answered with a substituted config."""
import os
import unittest
from unittest import mock

from adaptive_iicpilot.measurements import Measurements


def session():
    from adaptive_iicpilot import chat

    with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
        return chat.Session(emit=lambda line: None)


def rows():
    base = {"core_margin": None, "pin_strategy": "default", "macro_strategy": "stock", "ok": True,
            "congested": False, "timing_status": "ACCEPTABLE", "timing_threshold_ns": -0.11, "renders": None,
            "aspect_ratio": 0.5, "clock_ns": 2.2, "congestion": 0.97, "wns_ns": -0.02, "fmax_mhz": 450.0,
            "power_mw": 21.7}
    return [dict(base, util=u, floorplan=f"util {u:g}, AR 0.5", die_area_um2=d)
            for u, d in ((80.0, 35989.3), (85.0, 33895.8))]


class TestConfigOffer(unittest.TestCase):
    def setUp(self):
        self.s = session()
        for target, name, value in ((self.s, "tradeoff", {"measured_points": rows()}),
                                    (Measurements, "points", []),
                                    (Measurements, "median_full_seconds", 948.0)):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unmeasured_config_gets_an_offer_and_nothing_runs(self):
        with mock.patch.object(self.s, "_run_config") as run:
            result = self.s.floorplan(design="ibex", util=60)
        run.assert_not_called()
        self.assertEqual(result["source"], "NOT_RUN")
        self.assertEqual(result["offer"]["choices"], ["screen", "full"])
        self.assertIn("OFFER: reply 'screen it'", result["verdict"])
        self.assertIn("median measured 16 min", result["verdict"])
        self.assertIn("nothing runs until you confirm", result["verdict"])
        self.assertEqual(self.s.pending_offer, {"design": "ibex", "knobs": {"util": 60.0},
                                                "choices": ["screen", "full"]})

    def test_a_model_supplied_run_is_ignored_without_confirmation(self):
        with mock.patch.object(self.s, "_run_config") as run:
            self.s.floorplan(design="ibex", util=60, run="full")
        run.assert_not_called()

    def test_a_confirmed_offer_runs_exactly_that_config(self):
        offer = {"design": "ibex", "knobs": {"util": 60.0}, "choices": ["screen", "full"]}
        self.s.confirm_offer(offer, "screen")
        with mock.patch.object(self.s, "_run_config", return_value=None) as run:
            self.s.floorplan(design="ibex", mode="config", util=60.0, run="screen")
        self.assertEqual(run.call_args.args[:3], ("ibex", {"util": 60.0}, "screen"))
        # The confirmation is for that config only.
        self.s.confirm_offer(offer, "screen")
        with mock.patch.object(self.s, "_run_config") as run:
            self.s.floorplan(design="ibex", util=65.0, run="screen")
        run.assert_not_called()

    def test_missing_util_with_several_measured_is_asked_back_not_substituted(self):
        result = self.s.floorplan(design="ibex", aspect=0.5)
        self.assertEqual(result["source"], "NOT_RUN")
        self.assertIn("AMBIGUOUS", result["verdict"])
        self.assertIn("util 80, 85", result["verdict"])
        self.assertNotIn("measured", result)
        self.assertEqual(result["measured_utils"], [80.0, 85.0])
        self.assertIsNone(self.s.pending_offer)


class TestConfigVerdict(unittest.TestCase):
    def test_a_measured_config_leads_with_its_status_so_narration_is_held_to_it(self):
        from adaptive_iicpilot.chat import contradiction_note

        s = session()
        legacy = dict(rows()[0], util=60.0, aspect_ratio=1.0, floorplan="util 60, AR 1", fmax_mhz=None)
        with mock.patch.object(s, "tradeoff", return_value={"measured_points": [legacy]}), \
                mock.patch.object(s, "_show_renders", return_value=[]):
            result = s.floorplan(design="ibex", util=60)
        self.assertTrue(result["verdict"].startswith("CLEAN - ibex util 60, AR 1 at 2.2 ns"))
        self.assertIn("reg-to-reg fmax not measured", result["verdict"])
        self.assertNotIn("None MHz", result["verdict"])
        self.assertIsNotNone(contradiction_note("It is routable but congested.", s.last_verdict))


class TestOfferChoice(unittest.TestCase):
    OFFER = {"design": "ibex", "knobs": {"util": 60.0}, "choices": ["screen", "full"]}

    def test_choices(self):
        from adaptive_iicpilot.chat import offer_choice

        self.assertEqual(offer_choice("yes", self.OFFER), "screen")
        self.assertEqual(offer_choice("screen it", self.OFFER), "screen")
        self.assertEqual(offer_choice("run the full flow", self.OFFER), "full")
        self.assertIsNone(offer_choice("no, don't run anything", self.OFFER))
        self.assertIsNone(offer_choice("optimize ibex", self.OFFER))
        self.assertIsNone(offer_choice("yes", None))
        self.assertEqual(offer_choice("yes", {**self.OFFER, "choices": ["full"]}), "full")

    def test_the_offer_lives_for_one_turn(self):
        from adaptive_iicpilot import chat

        s = session()
        s.pending_offer = dict(self.OFFER)
        client = mock.MagicMock()
        client.chat.completions.create.return_value.choices = [mock.MagicMock(message=mock.MagicMock(content="ok"))]
        with mock.patch.object(s, "dispatch", return_value={"verdict": "x"}) as dispatch:
            chat.run_turn(client, s, "yes", lambda: None, lambda r: None, lambda r: None)
        self.assertEqual(dispatch.call_args.args, ("floorplan", {"design": "ibex", "mode": "config",
                                                                 "util": 60.0, "run": "screen"}))
        self.assertEqual(s._confirmed_offer["run"], "screen")
        # A "no" is answered in Python: nothing runs, no model, no numbers.
        s.pending_offer = dict(self.OFFER)
        replies = []
        client.reset_mock()
        with mock.patch.object(s, "dispatch") as dispatch:
            chat.run_turn(client, s, "no thanks", lambda: None, replies.append, lambda r: None)
        dispatch.assert_not_called()
        client.chat.completions.create.assert_not_called()
        self.assertEqual(replies, ["Nothing was run. ibex util 60 stays NOT_RUN (unmeasured)."])
        # An unrelated reply drops the offer: a later "yes" confirms nothing.
        s.pending_offer = dict(self.OFFER)
        with mock.patch.object(s, "dispatch", return_value={"verdict": "x"}):
            chat.run_turn(client, s, "what is congestion?", lambda: None, lambda r: None, lambda r: None)
        self.assertIsNone(s.pending_offer)


if __name__ == "__main__":
    unittest.main()
