"""The LLM interface shared by the LLM agent (User-Proxy + Monitor roles).

Mirrors eda_interface.py's mock/real split: AnthropicLLM is the real
adapter (one Anthropic API call per request); MockLLM is a deterministic,
offline stand-in so tests and the demo/TUI run with no network access and
no API key, exactly like MockORFSAdapter needs no OpenROAD-flow-scripts
install. All prompt construction and response parsing lives one layer up,
in LLMAgent — this module only knows how to complete a (system, prompt)
pair into text.
"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from typing import Optional


class LLMPort(ABC):
    @abstractmethod
    def complete(self, system: str, prompt: str) -> str:
        ...


class AnthropicLLM(LLMPort):
    """Real Claude backend. Reads ANTHROPIC_API_KEY from the environment."""

    DEFAULT_MODEL = "claude-sonnet-5"
    MAX_TOKENS = 512

    def __init__(self, model: Optional[str] = None) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ADAPTIVE_IICPILOT_MODEL", self.DEFAULT_MODEL)

    def complete(self, system: str, prompt: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


class MockLLM(LLMPort):
    """Deterministic, offline stand-in. Dispatches on the leading
    "TASK: <name>" line LLMAgent._call always puts at the top of `system`.
    """

    _DESIGN_RE = re.compile(r"\b(gcd|picorv32|ibex|aes)\b", re.IGNORECASE)
    _FIELD_RE = {
        "clock_period_ns": re.compile(r"clock[^0-9]{0,15}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
        "core_utilization": re.compile(r"util(?:ization)?[^0-9]{0,15}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
        "placement_density": re.compile(r"densit(?:y|ies)[^0-9]{0,15}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
        "aspect_ratio": re.compile(r"aspect[^0-9]{0,15}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    }
    _ACCEPT_TRIGGERS = ("congestion", "timing-violation")
    _ASK_HUMAN_TRIGGERS = ("area-margin",)

    def complete(self, system: str, prompt: str) -> str:
        task = system.splitlines()[0].removeprefix("TASK:").strip() if system else ""

        if task == "parse_request":
            return self._parse_request(prompt)
        if task == "decide_proposal":
            return self._decide_proposal(prompt)
        if task == "monitor_comment":
            return f"(mock) {prompt}"
        if task == "summarize_run":
            return f"(mock) run summary: {prompt}"
        return ""

    def _parse_request(self, prompt: str) -> str:
        import json

        changes: dict = {}
        design_match = self._DESIGN_RE.search(prompt)
        if design_match:
            changes["design"] = design_match.group(1).lower()
        for field, pattern in self._FIELD_RE.items():
            match = pattern.search(prompt)
            if match:
                changes[field] = float(match.group(1))
        return json.dumps(changes)

    def _decide_proposal(self, prompt: str) -> str:
        import json

        triggered_by = ""
        for line in prompt.splitlines():
            if line.lower().startswith("triggered_by:"):
                triggered_by = line.split(":", 1)[1].strip()
                break

        if triggered_by.startswith(self._ACCEPT_TRIGGERS):
            verdict = {"action": "accept", "changes": None, "comment": "(mock) looks routine, accepting."}
        elif triggered_by.startswith(self._ASK_HUMAN_TRIGGERS):
            verdict = {"action": "ask_human", "changes": None, "comment": "(mock) judgment call, please confirm."}
        else:
            verdict = {"action": "reject", "changes": None, "comment": "(mock) not a recognized fix pattern."}
        return json.dumps(verdict)
