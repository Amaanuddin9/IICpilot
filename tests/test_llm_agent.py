import unittest

from adaptive_iicpilot import FlowConfig, LLMAgent, LLMPort, Metrics, Proposal, RunResult
from adaptive_iicpilot.llm_interface import MockLLM


class _GarbageLLM(LLMPort):
    def complete(self, system: str, prompt: str) -> str:
        return "not json at all"


class TestLLMAgentWithMock(unittest.TestCase):
    def setUp(self):
        self.agent = LLMAgent(MockLLM())
        self.base = FlowConfig(design="ibex")

    def test_parse_request_extracts_design_and_clock(self):
        config = self.agent.parse_request("run gcd with clock period 2.2ns", self.base)
        self.assertEqual(config.design, "gcd")
        self.assertEqual(config.clock_period_ns, 2.2)

    def test_parse_request_no_match_keeps_base(self):
        config = self.agent.parse_request("do the usual thing", self.base)
        self.assertEqual(config, self.base)

    def test_decide_proposal_accepts_congestion(self):
        proposal = Proposal(changes={}, rationale="r", triggered_by="congestion-density")
        verdict = self.agent.decide_proposal(proposal, self.base, metrics=None)
        self.assertEqual(verdict["action"], "accept")

    def test_decide_proposal_accepts_timing_violation(self):
        proposal = Proposal(changes={}, rationale="r", triggered_by="timing-violation")
        verdict = self.agent.decide_proposal(proposal, self.base, metrics=None)
        self.assertEqual(verdict["action"], "accept")

    def test_decide_proposal_asks_human_for_area_margin(self):
        proposal = Proposal(changes={}, rationale="r", triggered_by="area-margin")
        verdict = self.agent.decide_proposal(proposal, self.base, metrics=None)
        self.assertEqual(verdict["action"], "ask_human")

    def test_decide_proposal_rejects_unknown_trigger(self):
        proposal = Proposal(changes={}, rationale="r", triggered_by="unknown-trigger")
        verdict = self.agent.decide_proposal(proposal, self.base, metrics=None)
        self.assertEqual(verdict["action"], "reject")

    def test_monitor_comment(self):
        comment = self.agent.monitor_comment("run started")
        self.assertIn("run started", comment)

    def test_summarize_run(self):
        metrics = Metrics(stage="route", wns_ns=0.1, tns_ns=0.0, congestion=0.1,
                           cell_count=100, area_um2=10.0, power_mw=1.0)
        result = RunResult(config=self.base, reached_stage="route", metrics=metrics, ok=True)
        summary = self.agent.summarize_run(result, [])
        self.assertIn("mock", summary)


class TestLLMAgentFallbacks(unittest.TestCase):
    def setUp(self):
        self.agent = LLMAgent(_GarbageLLM())
        self.base = FlowConfig(design="ibex")

    def test_parse_request_falls_back_to_base(self):
        config = self.agent.parse_request("anything", self.base)
        self.assertEqual(config, self.base)

    def test_decide_proposal_falls_back_to_ask_human(self):
        proposal = Proposal(changes={}, rationale="r", triggered_by="congestion-density")
        verdict = self.agent.decide_proposal(proposal, self.base, metrics=None)
        self.assertEqual(verdict["action"], "ask_human")


if __name__ == "__main__":
    unittest.main()
