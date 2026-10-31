"""Adaptive IICPilot: a six-agent copilot that steers an EDA flow toward
better PPA and remembers what it learns across sessions.

EDA drives the flow (mock or real ORFS); DSE decides which parameters to
try next; Memory (JSONMemory, persisted to disk) lets that search start
warm instead of from scratch on repeat runs; the LLM agent plays the
User-Proxy (turning a free-text request into a FlowConfig) and Monitor
(judging proposals, narrating events, summarizing runs) roles; Control
wires all of that into one request/response cycle.
"""
from .types import (
    STAGES,
    FlowConfig,
    Metrics,
    RunResult,
    Proposal,
    MemorySeed,
    MemoryPort,
)
from .eda_interface import EDAInterface, MockORFSAdapter, ORFSAdapter
from .eda_agent import EDAAgent
from .dse_agent import DSEAgent
from .llm_interface import LLMPort, AnthropicLLM, MockLLM
from .llm_agent import LLMAgent
from .memory import JSONMemory
from .control_agent import ControlAgent

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "STAGES",
    "FlowConfig",
    "Metrics",
    "RunResult",
    "Proposal",
    "MemorySeed",
    "MemoryPort",
    "EDAInterface",
    "MockORFSAdapter",
    "ORFSAdapter",
    "EDAAgent",
    "DSEAgent",
    "LLMPort",
    "AnthropicLLM",
    "MockLLM",
    "LLMAgent",
    "JSONMemory",
    "ControlAgent",
]
