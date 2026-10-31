"""Compatibility and core orchestrator interfaces for Adaptive IICPilot web integration.

Implements Task, RunReport, ConsoleInterface, InMemoryMemory, and Orchestrator,
duck-typing and wrapping the existing agent types and execution core without
modifying any existing agent files.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..dse_agent import DSEAgent
from ..eda_agent import EDAAgent
from ..eda_interface import MockORFSAdapter, make_adapter
from ..types import FlowConfig, MemoryPort, MemorySeed, Metrics, Proposal, RunResult, STAGES


def _orfs_backend() -> bool:
    import os

    return os.environ.get("ADAPTIVE_IICPILOT_BACKEND", "mock").strip().lower() == "orfs"


@dataclass
class Task:
    design: str
    tech: str = "nangate45"
    target_stage: str = "route"
    base_config: Optional[FlowConfig] = None
    max_iters: int = 6
    # Floorplan stage (FloorplanAgent) ahead of DSE. objective picks what the
    # floorplan is ranked by; policy is "apply", "apply_if_cached" or "stock".
    objective: str = "die_area"
    floorplan_policy: str = "apply"
    # For "apply_if_cached": says whether a staged config's whole search is
    # already measured (an interactive caller must not start hours of flow).
    floorplan_measured: Optional[Callable[[FlowConfig], bool]] = field(default=None, compare=False)
    # Screened pipeline (Control -> Floorplan screen -> DSE on survivors).
    # None = on for real ORFS designs, off for the mock. The allow_* flags say
    # whether an unmeasured screen / full flow may be RUN; when False it is
    # reported NOT_RUN (an interactive turn must not start a flow).
    screen: Optional[bool] = None
    allow_screen_runs: bool = False
    allow_full_runs: bool = False

    def __post_init__(self) -> None:
        if self.base_config is None:
            self.base_config = FlowConfig(design=self.design, tech=self.tech)


@dataclass
class RunReport:
    best: RunResult
    history: list[Proposal]
    flow_runs: int
    failures: int
    congestion_fights: int
    seeded: bool
    # What the floorplan stage decided: {"applied", "policy", "choice", "reason"}.
    floorplan: Optional[dict] = None
    # Screened pipeline bookkeeping (ScreenStats.as_dict()); None when off.
    pipeline: Optional[dict] = None

    @property
    def screens_run(self) -> int:
        return (self.pipeline or {}).get("screens_run", 0)

    @property
    def full_runs(self) -> int:
        return (self.pipeline or {}).get("full_runs", self.flow_runs)

    @property
    def time_saved_s(self) -> Optional[float]:
        return (self.pipeline or {}).get("time_saved_s")


class ConsoleInterface:
    """Console streaming and human-in-the-loop seam."""

    def __init__(self, mode: str = "auto") -> None:
        self.mode = mode

    def emit(self, msg: str) -> None:
        print(msg)

    def decide(self, proposal: Proposal, config: FlowConfig) -> Optional[Proposal]:
        if self.mode == "auto":
            return proposal
        print(f"[HITL] Proposal ({proposal.triggered_by}): {proposal.rationale}")
        print(f"       Changes: {proposal.changes}")
        ans = input("Decision [accept/reject/edit]: ").strip().lower()
        if ans.startswith("r"):
            return None
        return proposal


class InMemoryMemory:
    """In-memory implementation of MemoryPort that learns and persists across runs.

    Demonstrates cross-session memory in the web UI: a 2nd run of the same design
    is warm, starting from the learned clean configuration and avoiding failures.
    """

    def __init__(self) -> None:
        self._designs: dict[str, dict[str, Any]] = {}
        self.run_count: int = 0
        self.failure_count: int = 0

    @staticmethod
    def _key(design: str, tech: str) -> str:
        return f"{design}::{tech}"

    def seed_for(self, design: str, tech: str, stage: str) -> MemorySeed:
        entry = self._designs.get(self._key(design, tech))
        if entry is None or not entry.get("best_config"):
            return MemorySeed()

        best_cfg = entry["best_config"]
        best_config = FlowConfig(**best_cfg)
        return MemorySeed(
            best_config=best_config,
            best_score=entry.get("best_score", 0.0),
            decisions=dict(entry.get("decisions", {})),
            heuristics=list(entry.get("heuristics", [])),
        )

    def record_run(self, result: RunResult) -> None:
        self.run_count += 1
        if not result.ok:
            self.failure_count += 1
        key = self._key(result.config.design, result.config.tech)
        entry = self._designs.setdefault(key, {})
        entry["run_count"] = entry.get("run_count", 0) + 1
        if not result.ok:
            entry["failure_count"] = entry.get("failure_count", 0) + 1

    def record_session(self, best: RunResult, history: list[Proposal]) -> None:
        key = self._key(best.config.design, best.config.tech)
        entry = self._designs.setdefault(key, {})

        if best.ok:
            score = best.metrics.ppa_score()
            if score > entry.get("best_score", 0.0):
                entry["best_score"] = score
                entry["best_config"] = {
                    "design": best.config.design,
                    "tech": best.config.tech,
                    "clock_period_ns": best.config.clock_period_ns,
                    "core_utilization": best.config.core_utilization,
                    "placement_density": best.config.placement_density,
                    "aspect_ratio": best.config.aspect_ratio,
                }
                decisions: dict[str, Any] = {}
                default_clock = FlowConfig(design=best.config.design).clock_period_ns
                if best.config.clock_period_ns != default_clock:
                    decisions["clock_period_ns"] = best.config.clock_period_ns
                entry["decisions"] = decisions

            heuristics = entry.setdefault("heuristics", [])
            for p in history:
                if not p.seeded_from_memory and p.rationale not in heuristics:
                    heuristics.append(p.rationale)


def _describe_seed(seed: MemorySeed) -> str:
    """Say WHY a seed counts as warm, from whatever it actually holds.

    A seed can be warm on measured runs, on a hand correction, on a known
    congestion wall, or on heuristics alone — "WARM" on its own hid which,
    and a correction-only seed printed no detail at all.
    """
    parts: list[str] = []
    both = seed.best_config is not None and bool(seed.corrections)

    if seed.best_config is not None:
        from ..configkey import from_flow_config

        # The whole key: "util=85%" alone hid that the seed is the AR 0.5
        # floorplan, which is what makes util 85 routable at all.
        text = f"best measured config {from_flow_config(seed.best_config, None).label(with_clock=False)}"
        if seed.best_die_area_um2:
            text += f" (die {seed.best_die_area_um2:.1f}um2)"
        if both:
            text += " [steering]"
        parts.append(text)

    if seed.corrections:
        knobs = ", ".join(f"{knob}={value:g}" for knob, value in seed.corrections.items())
        text = f"engineer correction {knobs}"
        if both:
            text += " [not steering: measured best wins]"
        parts.append(text)

    if seed.congestion_wall_util is not None:
        parts.append(f"known congestion wall: util >= {seed.congestion_wall_util:g}%")

    if seed.heuristics:
        count = len(seed.heuristics)
        parts.append(f"{count} heuristic{'s' if count != 1 else ''}")

    return "; ".join(parts) if parts else "no details"


class Orchestrator:
    """Ties Memory, MockORFSAdapter, EDAAgent, and DSEAgent into one execution run."""

    def __init__(
        self,
        memory: MemoryPort,
        interface: Any,
        control: Optional[Any] = None,
    ) -> None:
        self.memory = memory
        self.interface = interface
        self.control = control

    def _wrap_decide(self, decide: Callable, task: Task) -> Callable:
        """Human-in-the-loop learning hook: when the engineer EDITS a proposal
        rather than accepting or rejecting it, that override is recorded as a
        correction. Accept and reject teach nothing — only an edit expresses a
        judgement the system did not already have."""
        record = getattr(self.memory, "record_correction", None)
        if record is None:
            return decide

        def wrapped(proposal: Proposal, config: FlowConfig) -> Optional[Proposal]:
            decided = decide(proposal, config)
            if decided is not None and decided.changes != proposal.changes:
                for knob, final in decided.changes.items():
                    proposed = proposal.changes.get(knob)
                    if proposed != final:
                        record(task.design, task.tech, knob, proposed, final,
                               proposal.triggered_by)
            return decided

        return wrapped

    def _floorplan_stage(self, task: Task, emit: Callable[[str], None]) -> tuple[FlowConfig, Optional[dict]]:
        """FloorplanAgent decides the floorplan; DSE then searches utilization
        and density with it. Floorplan memory lives in the same database as
        the run memory (a throwaway one for a non-SQLite memory)."""
        from ..floorplan.agent import floorplan_stage
        from ..floorplan.memory import FloorplanMemory

        memory = FloorplanMemory(getattr(self.memory, "db_path", None) or ":memory:")
        try:
            return floorplan_stage(
                task.base_config, memory, emit, objective=task.objective,
                policy=task.floorplan_policy, is_measured=task.floorplan_measured,
            )
        finally:
            memory.close()

    def run(self, task: Task) -> RunReport:
        emit = self.interface.emit
        decide = self._wrap_decide(self.interface.decide, task)

        emit(
            f"[Control] starting task: design={task.design} tech={task.tech} "
            f"target_stage={task.target_stage} max_iters={task.max_iters}"
        )

        seed = self.memory.seed_for(task.design, task.tech, task.target_stage)
        seeded = not seed.is_cold

        if seeded:
            emit(
                f"[Memory] WARM seed for {task.design}/{task.tech}: "
                f"{_describe_seed(seed)}"
            )
        else:
            emit(f"[Memory] COLD seed for {task.design}/{task.tech} (no prior memory)")

        adapter = make_adapter()
        eda_agent = EDAAgent(adapter, memory=self.memory, emit=emit)
        dse_agent = DSEAgent(eda_agent, target_stage=task.target_stage)
        screen = task.screen if task.screen is not None else _orfs_backend()
        pipeline = None

        if screen and _orfs_backend():
            from ..pipeline import screened_search

            emit("[Control] pipeline: Control -> Floorplan (propose + screen) -> DSE (full flow on survivors)")
            best, history, stats = screened_search(
                task.base_config, seed, eda_agent, adapter, emit, decide,
                allow_screen_runs=task.allow_screen_runs, allow_full_runs=task.allow_full_runs,
            )
            pipeline = stats.as_dict()
            floorplan = {"applied": best is not None, "policy": "screened",
                         "reason": "chosen by screening utilization x aspect ratio"}
            if best is None:
                # Nothing survived to a measured full run: fall back to the
                # plain search so the caller still gets a measured result.
                emit("[Control] pipeline produced no measured full run; falling back to the ladder search")
                base, floorplan = self._floorplan_stage(task, emit)
                best, more = dse_agent.explore(base=base, seed=seed, max_iters=task.max_iters, decide=decide)
                history += more
        else:
            base, floorplan = self._floorplan_stage(task, emit)
            best, history = dse_agent.explore(
                base=base,
                seed=seed,
                max_iters=task.max_iters,
                decide=decide,
            )

        congestion_fights = sum(1 for p in history if "congestion" in p.triggered_by)

        if hasattr(self.memory, "record_session"):
            self.memory.record_session(best, history)

        m = best.metrics
        status = "OK" if best.ok else "FAILURE"
        area = (
            f"die_area={m.die_area_um2:.1f}um2" if m.die_area_um2
            else f"area={m.area_um2:.1f}um2"
        )
        emit(
            f"[Control] run complete: reached={best.reached_stage} status={status} "
            f"{area} power={m.power_mw:.2f}mW "
            f"wns={m.wns_ns:.3f}ns ppa={m.ppa_score():.3e}"
        )

        return RunReport(
            best=best,
            history=history,
            flow_runs=eda_agent.runs,
            failures=eda_agent.failures,
            congestion_fights=congestion_fights,
            seeded=seeded,
            floorplan=floorplan,
            pipeline=pipeline,
        )


# inject into sys.modules for transparency if not already present
if "adaptive_iicpilot.orchestrator" not in sys.modules:
    mod = types.ModuleType("adaptive_iicpilot.orchestrator")
    mod.Orchestrator = Orchestrator
    mod.Task = Task
    mod.ConsoleInterface = ConsoleInterface
    mod.RunReport = RunReport
    sys.modules["adaptive_iicpilot.orchestrator"] = mod

if "adaptive_iicpilot.memory_stub" not in sys.modules:
    mod = types.ModuleType("adaptive_iicpilot.memory_stub")
    mod.InMemoryMemory = InMemoryMemory
    sys.modules["adaptive_iicpilot.memory_stub"] = mod
