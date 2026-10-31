"""Machine-readable frontend for the conversational agent.

    python -m adaptive_iicpilot.chat --jsonl

One JSON object per line in on stdin, one JSON object per line out on stdout.
stdout carries protocol frames and nothing else — every stray print, traceback
and log goes to stderr — because the VS Code extension parses stdout strictly.

The agent, tools, orchestrator and memory are untouched: this module only
translates their existing callbacks into protocol frames.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from typing import Any, Optional

from .chat import (
    OLLAMA_MODEL,
    Session,
    _make_client,
    _split_log_line,
    run_turn,
)
from .types import FlowConfig, Proposal

# A decision that never arrives must not wedge the backend forever.
DECISION_TIMEOUT_S = 600


def _write(frame: dict) -> None:
    """Emit one protocol frame. Flushed per line so the extension sees it live."""
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _log(text: str) -> None:
    agent, message = _split_log_line(text)
    _write({"type": "log", "agent": agent, "text": message})


def _run_status(status: dict) -> None:
    """Live-run progress: {"state": "running"|"done", "stage", "elapsed_s"}."""
    _write({"type": "run_status", **status})


def _image(image: dict) -> None:
    """A rendered picture (an SVG floorplan) for the chat's image card."""
    _write({"type": "image", **image})


def _raw_log(line: str) -> None:
    """The full flow log goes to stderr, which the extension mirrors into its
    Output channel — never to stdout, which is protocol-only."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class JsonlBackend:
    def __init__(self) -> None:
        self.hitl = False
        self.decisions: queue.Queue[dict] = queue.Queue()
        self.inbox: queue.Queue[Optional[dict]] = queue.Queue()
        self._proposal_seq = 0
        self.session = Session(
            emit=_log,
            decide_handler=self._decide,
            on_report=self._report,
            on_status=_run_status,
            raw_log=_raw_log,
            on_image=_image,
        )

    # --- protocol out ---

    def _report(self, report: dict) -> None:
        _write({"type": "report", **report})

    def _decide(self, proposal: Proposal, config: FlowConfig) -> Optional[Proposal]:
        """HITL gate. Surfaces the proposal, then blocks this (worker) thread
        until a matching decision arrives on stdin."""
        if not self.hitl:
            return proposal

        self._proposal_seq += 1
        proposal_id = f"p{self._proposal_seq}"
        frame = {
            "type": "proposal",
            "id": proposal_id,
            "rationale": proposal.rationale,
            "triggered_by": proposal.triggered_by,
            "changes": proposal.changes,
            "seeded_from_memory": proposal.seeded_from_memory,
        }
        # Only floorplan proposals carry a picture; others keep the old frame.
        if getattr(proposal, "image_svg", None):
            frame["image_svg"] = proposal.image_svg
        _write(frame)

        while True:
            try:
                message = self.decisions.get(timeout=DECISION_TIMEOUT_S)
            except queue.Empty:
                _write(
                    {
                        "type": "error",
                        "text": f"No decision for proposal {proposal_id} within "
                        f"{DECISION_TIMEOUT_S}s; rejecting it to unblock the run.",
                    }
                )
                return None
            # Ignore stale decisions for proposals already resolved.
            if message.get("id") not in (None, proposal_id):
                continue
            break

        decision = (message.get("decision") or "accept").lower()
        if decision == "reject":
            return None
        if decision == "edit":
            changes = message.get("changes") or proposal.changes
            # Returned via the normal seam, so the Orchestrator sees changed
            # values and records the correction in memory as it always has.
            return Proposal(
                changes=changes,
                rationale=proposal.rationale,
                triggered_by=proposal.triggered_by,
                seeded_from_memory=proposal.seeded_from_memory,
                image_svg=proposal.image_svg,
            )
        return proposal

    # --- stdin ---

    def _read_stdin(self) -> None:
        """Decisions are split off into their own queue so they can be consumed
        by a blocked _decide while a turn is still in flight."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write({"type": "error", "text": f"invalid JSON on stdin: {exc}"})
                continue
            if message.get("type") == "decision":
                self.decisions.put(message)
            else:
                self.inbox.put(message)
        self.inbox.put(None)  # EOF

    # --- lifecycle ---

    def _memory_state(self, design: str = "ibex") -> str:
        seed = self.session.memory.seed_for(design, "nangate45", "route")
        return "COLD" if seed.is_cold else "WARM"

    def _backend_name(self) -> str:
        return "orfs" if self.session._ladder_cached("ibex") else "mock"

    def run(self) -> None:
        client = _make_client()

        _write(
            {
                "type": "ready",
                "model": OLLAMA_MODEL,
                "backend": self._backend_name(),
                "memory": self._memory_state(),
            }
        )

        threading.Thread(target=self._read_stdin, daemon=True).start()

        while True:
            message = self.inbox.get()
            if message is None:
                return

            kind = message.get("type")
            if kind == "user":
                self._handle_user(client, message.get("text", ""))
            elif kind == "config":
                if "hitl" in message:
                    # _decide consults self.hitl on every call, so toggling the
                    # flag is enough — the interface never needs rebuilding.
                    self.hitl = bool(message["hitl"])
                    _log(f"[Control] human-in-the-loop {'on' if self.hitl else 'off'}")
                _write({"type": "turn_end"})
            elif kind == "reset_memory":
                if hasattr(self.session.memory, "reset"):
                    self.session.memory.reset()
                _log("[Memory] reset; next run will be cold")
                _write(
                    {
                        "type": "ready",
                        "model": OLLAMA_MODEL,
                        "backend": self._backend_name(),
                        "memory": self._memory_state(),
                    }
                )
                _write({"type": "turn_end"})
            else:
                _write({"type": "error", "text": f"unknown message type {kind!r}"})
                _write({"type": "turn_end"})

    def _handle_user(self, client: Any, text: str) -> None:
        if not text.strip():
            _write({"type": "turn_end"})
            return
        try:
            run_turn(
                client,
                self.session,
                text,
                on_thinking=lambda: _write({"type": "thinking"}),
                on_assistant=lambda reply: _write({"type": "assistant", "text": reply}),
                on_error=lambda err: _write({"type": "error", "text": err}),
            )
        except Exception as exc:  # a crashed turn must still close cleanly
            _write({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            _write({"type": "turn_end"})


def main_jsonl() -> None:
    # stdout is the protocol channel: keep it byte-clean and never let the
    # model's unicode kill the stream.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        JsonlBackend().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main_jsonl()
