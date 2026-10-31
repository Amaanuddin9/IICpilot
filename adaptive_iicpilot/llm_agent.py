"""The LLM agent: the merged User-Proxy + Monitor role.

User-Proxy: turns a free-text engineer request into a FlowConfig.
Monitor: judges each DSE proposal (the HITL gate), narrates events for a
live UI, and summarizes a finished run in plain language.

This is the "LLM-swap seam" made real: DSEAgent.propose() stays the rule
engine that decides WHICH change to try, and DSEAgent.explore()'s `decide`
seam stays exactly as it was — this agent just sits on the other side of
that seam, judging proposals instead of a human doing it directly (though
a human can still be consulted, via ask_human).
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from .llm_interface import LLMPort
from .types import FlowConfig, Metrics, Proposal, RunResult

_CONFIG_FIELDS = {
    "design",
    "tech",
    "clock_period_ns",
    "core_utilization",
    "placement_density",
    "aspect_ratio",
}

_PARSE_REQUEST_SYSTEM = (
    "TASK: parse_request\n"
    "You are the front door of an IC physical-design flow assistant. "
    "Given an engineer's free-text request, output ONLY a JSON object with "
    "zero or more of these optional keys: design, tech, clock_period_ns, "
    "core_utilization, placement_density, aspect_ratio. Include a key only "
    "if the text clearly states or implies it. No prose, no markdown fences."
)

_DECIDE_PROPOSAL_SYSTEM = (
    "TASK: decide_proposal\n"
    "You are approving a parameter change to an IC physical-design flow. "
    "Given the current config, the metrics that triggered the change, and "
    "a proposed change with its rationale, output ONLY JSON: "
    '{"action": "accept"|"reject"|"ask_human", "changes": null or an '
    'object of field overrides, "comment": "<one sentence>"}. Use '
    "ask_human when the change is a judgment call or outside typical "
    "ranges for this kind of flow."
)

_MONITOR_COMMENT_SYSTEM = (
    "TASK: monitor_comment\n"
    "In one short sentence, comment on this flow event for an engineer "
    "watching a live dashboard."
)

_SUMMARIZE_RUN_SYSTEM = (
    "TASK: summarize_run\n"
    "In 2-3 sentences, summarize this finished flow run for an engineer: "
    "what stage it reached, whether it succeeded, and what changed along "
    "the way."
)


class LLMAgent:
    def __init__(self, llm: LLMPort, emit: Callable[[str], None] = lambda msg: None) -> None:
        self.llm = llm
        self.emit = emit

    def parse_request(self, user_text: str, base: FlowConfig) -> FlowConfig:
        raw = self._call("parse_request", _PARSE_REQUEST_SYSTEM, user_text)
        parsed = self._safe_json(raw, fallback={})
        changes = {k: v for k, v in parsed.items() if k in _CONFIG_FIELDS}
        try:
            return base.tweaked(**changes)
        except (TypeError, ValueError):
            self.emit(f"[LLM] could not apply parsed request {changes!r}; using base config")
            return base

    def decide_proposal(
        self, proposal: Proposal, config: FlowConfig, metrics: Optional[Metrics]
    ) -> dict:
        prompt = (
            f"config: {config}\n"
            f"metrics: {metrics}\n"
            f"triggered_by: {proposal.triggered_by}\n"
            f"changes: {proposal.changes}\n"
            f"rationale: {proposal.rationale}\n"
            f"seeded_from_memory: {proposal.seeded_from_memory}"
        )
        raw = self._call("decide_proposal", _DECIDE_PROPOSAL_SYSTEM, prompt)
        fallback = {
            "action": "ask_human",
            "changes": None,
            "comment": "could not parse LLM response; escalating to be safe.",
        }
        verdict = self._safe_json(raw, fallback=fallback)
        if verdict.get("action") not in ("accept", "reject", "ask_human"):
            return fallback
        return verdict

    def monitor_comment(self, event: str) -> str:
        raw = self._call("monitor_comment", _MONITOR_COMMENT_SYSTEM, event)
        return raw.strip() or event

    def summarize_run(self, result: RunResult, history: list[Proposal]) -> str:
        triggers = [p.triggered_by for p in history]
        prompt = (
            f"reached_stage: {result.reached_stage}\n"
            f"ok: {result.ok}\n"
            f"failure: {result.failure}\n"
            f"metrics: {result.metrics}\n"
            f"proposals_tried: {triggers}"
        )
        raw = self._call("summarize_run", _SUMMARIZE_RUN_SYSTEM, prompt)
        if raw.strip():
            return raw.strip()
        status = "succeeded" if result.ok else f"failed ({result.failure})"
        return (
            f"Run {status} at stage {result.reached_stage} after "
            f"{len(history)} proposal(s); wns={result.metrics.wns_ns:.3f}ns."
        )

    def _call(self, task: str, system: str, prompt: str) -> str:
        try:
            return self.llm.complete(system, prompt)
        except Exception as exc:  # network/auth/rate-limit errors must not crash the DSE loop
            self.emit(f"[LLM] {task} call failed: {exc}")
            return ""

    @staticmethod
    def _safe_json(raw: str, fallback: dict) -> dict:
        if not raw:
            return dict(fallback)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        return dict(fallback)
