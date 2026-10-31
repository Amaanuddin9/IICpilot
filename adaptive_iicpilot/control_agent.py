"""The Control agent: the orchestrator that wires the other five agents
together into one request/response cycle.

User text -> LLMAgent.parse_request -> Memory.seed_for -> DSEAgent.explore
(with this agent's _decide plugged into the HITL-gate seam, judged by
LLMAgent.decide_proposal) -> Memory.record_session -> LLMAgent.summarize_run.

EDAAgent and DSEAgent are built here, not passed in, so this is the one
object a UI (or a script) needs to drive the whole system.
"""
from __future__ import annotations

from typing import Callable, Optional

from .dse_agent import DSEAgent
from .eda_agent import EDAAgent
from .eda_interface import EDAInterface
from .llm_agent import LLMAgent
from .llm_interface import LLMPort
from .memory import JSONMemory
from .types import FlowConfig, Proposal, RunResult

HumanAnswer = Callable[[Proposal, FlowConfig, str], Optional[Proposal]]


class _ResultTrackingProxy:
    """Wraps EDAAgent to stash the latest RunResult.

    DSEAgent's Decider type is fixed at (Proposal, FlowConfig) -> ... with
    no Metrics param, and must not change. This proxy is how ControlAgent
    still gets the Metrics that triggered a proposal — DSEAgent.propose()
    read result.metrics one line earlier in explore()'s loop, and this
    stashes that exact same result.
    """

    def __init__(self, inner: EDAAgent, on_result: Callable[[RunResult], None]) -> None:
        self._inner = inner
        self._on_result = on_result

    def run_flow(self, config: FlowConfig, target_stage: str = "route") -> RunResult:
        result = self._inner.run_flow(config, target_stage)
        self._on_result(result)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ControlAgent:
    def __init__(
        self,
        eda_interface: EDAInterface,
        llm: LLMPort,
        memory: JSONMemory,
        target_stage: str = "route",
        emit: Callable[[str], None] = lambda msg: None,
        human_answer: Optional[HumanAnswer] = None,
        floorplan_memory=None,
        floorplan_policy: str = "apply",
    ) -> None:
        self.memory = memory
        self.emit = emit
        self.human_answer = human_answer
        # Optional FloorplanAgent stage ahead of DSE: a FloorplanMemory to
        # choose from. None keeps the request's own (stock) floorplan.
        self.floorplan_memory = floorplan_memory
        self.floorplan_policy = floorplan_policy
        self.last_floorplan: Optional[dict] = None

        self.llm_agent = LLMAgent(llm, emit=emit)
        eda_agent = EDAAgent(eda_interface, memory=memory, emit=emit)
        self._last_result: Optional[RunResult] = None
        tracked_eda_agent = _ResultTrackingProxy(eda_agent, on_result=self._on_result)
        self.dse_agent = DSEAgent(tracked_eda_agent, target_stage=target_stage)

    def handle_request(self, user_text: str, base: FlowConfig, max_iters: int = 6) -> RunResult:
        config = self.llm_agent.parse_request(user_text, base)
        self.emit(f"[Control] request parsed -> {config}")
        if self.floorplan_memory is not None:
            from .floorplan.agent import floorplan_stage

            config, self.last_floorplan = floorplan_stage(
                config, self.floorplan_memory, self.emit, policy=self.floorplan_policy
            )

        seed = self.memory.seed_for(config.design, config.tech, self.dse_agent.target_stage)
        self.emit(f"[Memory] {'WARM' if not seed.is_cold else 'COLD'} seed for {config.design}/{config.tech}")

        best, history = self.dse_agent.explore(
            config, seed=seed, max_iters=max_iters, decide=self._decide
        )

        self.memory.record_session(best, history)
        summary = self.llm_agent.summarize_run(best, history)
        self.emit(f"[LLM] {summary}")
        return best

    def _decide(self, proposal: Proposal, config: FlowConfig) -> Optional[Proposal]:
        self.emit(f"[DSE] proposal ({proposal.triggered_by}): {proposal.rationale}")
        metrics = self._last_result.metrics if self._last_result is not None else None

        verdict = self.llm_agent.decide_proposal(proposal, config, metrics)
        action = verdict.get("action", "ask_human")
        comment = verdict.get("comment", "")
        self.emit(f"[LLM] verdict={action}: {comment}")

        if action == "reject":
            return None
        if action == "ask_human":
            if self.human_answer is None:
                self.emit("[Control] no human_answer configured; treating ask_human as reject")
                return None
            return self.human_answer(proposal, config, comment)

        changes = verdict.get("changes") or proposal.changes
        return Proposal(
            changes=changes,
            rationale=proposal.rationale,
            triggered_by=proposal.triggered_by,
            seeded_from_memory=proposal.seeded_from_memory,
        )

    def _on_result(self, result: RunResult) -> None:
        self._last_result = result
