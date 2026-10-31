import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from adaptive_iicpilot import ControlAgent, FlowConfig, JSONMemory, MockORFSAdapter
from adaptive_iicpilot.llm_interface import MockLLM


class TestControlAgentEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.memory_path = Path(self._tmpdir.name) / "memory.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_handle_request_returns_result_and_learns(self):
        memory = JSONMemory(path=self.memory_path)
        control = ControlAgent(MockORFSAdapter(), MockLLM(), memory)

        result = control.handle_request("run gcd", FlowConfig(design="gcd"))

        self.assertEqual(result.config.design, "gcd")
        seed = memory.seed_for("gcd", "nangate45", "route")
        self.assertFalse(seed.is_cold)

    def test_ask_human_invokes_human_answer(self):
        memory = JSONMemory(path=self.memory_path)
        calls = []

        def human_answer(proposal, config, comment):
            calls.append(proposal.triggered_by)
            return None  # reject via human

        control = ControlAgent(
            MockORFSAdapter(), MockLLM(), memory, human_answer=human_answer
        )
        # Aggressive config that will hit an area-margin proposal (ask_human in MockLLM)
        base = FlowConfig(design="gcd", clock_period_ns=3.0, core_utilization=40.0, placement_density=0.5)
        control.handle_request("run gcd", base)

        self.assertIn("area-margin", calls)


if __name__ == "__main__":
    unittest.main()
