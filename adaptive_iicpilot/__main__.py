"""Entry point: python -m adaptive_iicpilot (or the `iicpilot` console script).

Picks a real Anthropic backend when ANTHROPIC_API_KEY is set, otherwise
falls back to the deterministic MockLLM - an explicit choice, not a
silent fallback-on-exception, mirroring MockORFSAdapter/ORFSAdapter.
"""
from __future__ import annotations

import os

from .eda_interface import MockORFSAdapter
from .llm_interface import AnthropicLLM, LLMPort, MockLLM
from .memory import JSONMemory


def _pick_llm() -> LLMPort:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicLLM()
    return MockLLM()


def main() -> None:
    from .tui import IICPilotApp

    IICPilotApp(eda_interface=MockORFSAdapter(), llm=_pick_llm(), memory=JSONMemory()).run()


if __name__ == "__main__":
    main()
