"""The --jsonl protocol. Runs offline: the model client is stubbed, so these
never touch Ollama."""
import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from adaptive_iicpilot.types import FlowConfig, Proposal

ENV = {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}


class _FakeClient:
    """Minimal stand-in for the OpenAI client shape used by run_turn."""

    def __init__(self, content="done", tool_calls=None, error=None):
        self._content = content
        self._tool_calls = tool_calls
        self._error = error
        self.chat = SimpleNamespace(completions=self)

    def create(self, **_kwargs):
        if self._error is not None:
            raise self._error
        message = SimpleNamespace(content=self._content, tool_calls=self._tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _frames(text):
    """Every stdout line must be a standalone JSON object."""
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


class TestLogSplitting(unittest.TestCase):
    def test_known_agent_is_extracted(self):
        from adaptive_iicpilot.chat import _split_log_line

        self.assertEqual(_split_log_line("[EDA] run #1: ibex"), ("EDA", "run #1: ibex"))
        self.assertEqual(_split_log_line("[Memory] WARM"), ("Memory", "WARM"))

    def test_unknown_tag_falls_back_to_control(self):
        from adaptive_iicpilot.chat import _split_log_line

        agent, text = _split_log_line("no tag here")
        self.assertEqual(agent, "Control")
        self.assertEqual(text, "no tag here")


class TestTurnFrames(unittest.TestCase):
    def _run(self, client, text="hello"):
        from adaptive_iicpilot.chat_jsonl import JsonlBackend

        with mock.patch.dict(os.environ, ENV):
            backend = JsonlBackend()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            backend._handle_user(client, text)
        return _frames(buffer.getvalue())

    def test_stdout_is_only_valid_json(self):
        frames = self._run(_FakeClient(content="a reply"))
        self.assertTrue(frames)  # _frames() would have raised on non-JSON

    def test_turn_ends_with_turn_end(self):
        frames = self._run(_FakeClient(content="a reply"))
        self.assertEqual(frames[-1], {"type": "turn_end"})

    def test_thinking_precedes_assistant(self):
        frames = self._run(_FakeClient(content="a reply"))
        kinds = [f["type"] for f in frames]
        self.assertIn("thinking", kinds)
        self.assertIn("assistant", kinds)
        self.assertLess(kinds.index("thinking"), kinds.index("assistant"))
        self.assertEqual(frames[kinds.index("assistant")]["text"], "a reply")

    def test_model_error_becomes_an_error_frame_then_turn_end(self):
        frames = self._run(_FakeClient(error=RuntimeError("connection refused")))
        kinds = [f["type"] for f in frames]

        self.assertIn("error", kinds)
        self.assertIn("connection refused", frames[kinds.index("error")]["text"])
        self.assertEqual(frames[-1], {"type": "turn_end"})

    def test_empty_text_still_closes_the_turn(self):
        frames = self._run(_FakeClient(content="unused"), text="   ")
        self.assertEqual(frames, [{"type": "turn_end"}])


class TestHitlDecisions(unittest.TestCase):
    def _backend(self, hitl=True):
        from adaptive_iicpilot.chat_jsonl import JsonlBackend

        with mock.patch.dict(os.environ, ENV):
            backend = JsonlBackend()
        backend.hitl = hitl
        return backend

    def _proposal(self):
        return Proposal(
            changes={"core_utilization": 85.0},
            rationale="raise utilization to shrink the die",
            triggered_by="die-area-shrink",
        )

    def _decide_async(self, backend, proposal):
        """_decide blocks, exactly as it does inside a live run."""
        result = {}
        buffer = io.StringIO()

        def worker():
            with redirect_stdout(buffer):
                result["value"] = backend._decide(proposal, FlowConfig(design="ibex"))

        thread = threading.Thread(target=worker)
        thread.start()
        return thread, result, buffer

    def test_auto_mode_accepts_without_emitting_a_proposal(self):
        backend = self._backend(hitl=False)
        proposal = self._proposal()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            decided = backend._decide(proposal, FlowConfig(design="ibex"))

        self.assertIs(decided, proposal)
        self.assertEqual(buffer.getvalue(), "")

    def test_decision_accept_unblocks_the_proposal(self):
        backend = self._backend()
        proposal = self._proposal()
        thread, result, buffer = self._decide_async(backend, proposal)

        backend.decisions.put({"type": "decision", "id": "p1", "decision": "accept"})
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), "decide() never unblocked")
        self.assertIs(result["value"], proposal)
        frame = _frames(buffer.getvalue())[0]
        self.assertEqual(frame["type"], "proposal")
        self.assertEqual(frame["id"], "p1")
        self.assertEqual(frame["changes"], {"core_utilization": 85.0})

    def test_decision_reject_returns_none(self):
        backend = self._backend()
        thread, result, _ = self._decide_async(backend, self._proposal())

        backend.decisions.put({"type": "decision", "id": "p1", "decision": "reject"})
        thread.join(timeout=10)

        self.assertIsNone(result["value"])

    def test_decision_edit_carries_the_new_changes(self):
        backend = self._backend()
        original = self._proposal()
        thread, result, _ = self._decide_async(backend, original)

        backend.decisions.put(
            {
                "type": "decision",
                "id": "p1",
                "decision": "edit",
                "changes": {"core_utilization": 75.0},
            }
        )
        thread.join(timeout=10)

        edited = result["value"]
        self.assertEqual(edited.changes, {"core_utilization": 75.0})
        # Same trigger/rationale so the Orchestrator still records it as a
        # correction against the original proposal.
        self.assertEqual(edited.triggered_by, original.triggered_by)

    def test_stale_decision_for_another_proposal_is_ignored(self):
        backend = self._backend()
        thread, result, _ = self._decide_async(backend, self._proposal())

        backend.decisions.put({"type": "decision", "id": "p99", "decision": "reject"})
        thread.join(timeout=2)
        self.assertTrue(thread.is_alive(), "a stale decision should not resolve p1")

        backend.decisions.put({"type": "decision", "id": "p1", "decision": "accept"})
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(result["value"])


if __name__ == "__main__":
    unittest.main()
