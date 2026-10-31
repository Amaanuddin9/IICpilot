"""Shared data types for the Adaptive IICPilot execution core.

These types are the contract between the EDA agent, the DSE agent, the
EDA interface (mock or real), and the (not-yet-built) memory subsystem.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Protocol

STAGES = ["synth", "floorplan", "place", "cts", "route", "finish"]


@dataclass
class FlowConfig:
    design: str
    tech: str = "nangate45"
    # 0 means "use the design's stock clock period" (read from its SDC for a
    # real run, 2.5ns for the mock). An explicit value is a deliberate request,
    # so asking for 2.5ns on a 2.2ns design is no longer indistinguishable from
    # not having asked at all.
    clock_period_ns: float = 0.0
    core_utilization: float = 45.0  # percent, 0..100
    placement_density: float = 0.60  # 0..1
    aspect_ratio: float = 1.0
    # Floorplan decided by the FloorplanAgent's pipeline stage, beyond the
    # aspect ratio: {"core_margin", "io_strategy", "macro_mode", "macro_halo",
    # "label"}. None means the design's stock floorplan, which keeps every
    # cache key and memory row written before the stage existed valid.
    floorplan: Optional[dict] = None

    def tweaked(self, **changes: Any) -> "FlowConfig":
        """Return a copy with the given fields changed. Never mutates self."""
        return replace(self, **changes)


@dataclass
class Metrics:
    stage: str
    wns_ns: float
    tns_ns: float
    congestion: float  # mock: 0..1; real ORFS: 0..1+ scalar
    cell_count: int
    area_um2: float
    power_mw: float
    # Real-flow fields. Left at 0 by the mock, which keeps its original
    # scoring; ORFSAdapter fills them and switches the objective below.
    die_area_um2: float = 0.0
    core_area_um2: float = 0.0
    drc_errors: int = 0
    # REG-TO-REG critical-path delay: 1e9 / finish__timing__fmax__clock__<real
    # clock>, the real (non-virtual) clock's period_min. Unlike wns_ns this is
    # independent of the clock the run was constrained with, so it is what makes
    # runs at different clock periods comparable. 0/None means "not measured";
    # cp_delay_note says why.
    cp_delay_ns: Optional[float] = 0.0
    fmax_clock: Optional[str] = None
    cp_delay_note: Optional[str] = None
    # ORFS's raw finish__timing__fmax (max over ALL clocks, virtual included)
    # and the clock it came from. Informational only; never used as fmax.
    orfs_fmax_headline_hz: Optional[float] = None
    orfs_fmax_headline_clock: Optional[str] = None

    # Negative slack ORFS itself tolerates for this design, from its
    # rules-base.json. 0 means "no allowance": only non-negative slack passes.
    timing_threshold_ns: float = 0.0

    @property
    def timing_status(self) -> str:
        """MET / ACCEPTABLE / VIOLATED.

        Three states rather than two because essentially every real run here
        closes with a hair of negative slack — ibex stock sits at -0.007 ns —
        and ORFS itself ships a per-design allowance. A binary met/not-met
        would either reject the entire measured corpus or, worse, let a
        negative-slack run be described as "met".
        """
        if self.wns_ns >= 0:
            return "MET"
        if self.timing_threshold_ns and self.wns_ns >= self.timing_threshold_ns:
            return "ACCEPTABLE"
        return "VIOLATED"

    @property
    def fmax_mhz(self) -> Optional[float]:
        """Achievable frequency, or None when the critical path was not measured."""
        if not self.cp_delay_ns:
            return None
        return 1000.0 / self.cp_delay_ns
    # The congestion scales differ per backend, so the gate travels with the
    # metrics instead of being hardcoded: mock congests above 0.30, real
    # nangate45 runs only above 1.0 (measured: util 50 -> 0.970 clean,
    # util 90/density 0.95 -> 1.041 congested).
    congestion_limit: float = 0.30

    @property
    def timing_met(self) -> bool:
        return self.wns_ns >= 0

    @property
    def congested(self) -> bool:
        return self.congestion > self.congestion_limit

    def ppa_score(self) -> float:
        """Higher is better."""
        if self.die_area_um2:
            # Real-flow objective: smallest die that stays inside the
            # congestion gate. Congestion is a hard gate rather than a
            # penalty because a congested config is not a worse design
            # point, it is an unusable one. Timing is deliberately not a
            # failure term here — real ibex sits at a small negative WNS
            # at every utilization, so scoring it would just add noise.
            if self.congested:
                return 1e-12
            return 1.0 / self.die_area_um2

        # A real flow that died before finish (e.g. DPL-0038 at CTS) reports
        # no area or power at all: not a design point, so it scores below
        # even a congested one instead of dividing by zero.
        if self.area_um2 <= 0 or self.power_mw <= 0:
            return 0.0
        score = 1.0 / (self.area_um2 * self.power_mw)
        if not self.timing_met:
            score *= 0.1
        if self.congested:
            score *= 0.5
        return score


@dataclass
class RunResult:
    config: FlowConfig
    reached_stage: str
    metrics: Metrics
    ok: bool = True
    failure: Optional[str] = None
    log_tail: list = field(default_factory=list)


@dataclass
class Proposal:
    changes: dict
    rationale: str
    triggered_by: str
    seeded_from_memory: bool = False
    # Optional picture of what is being proposed (an SVG floorplan), shown on
    # the HITL card before the engineer decides.
    image_svg: Optional[str] = None

    def apply_to(self, config: FlowConfig) -> FlowConfig:
        return config.tweaked(**self.changes)


@dataclass
class MemorySeed:
    """The only mechanism that makes DSE adaptive across sessions.

    A cold seed (all fields empty) makes DSE behave like vanilla IICPilot,
    re-deriving everything from scratch. A warm seed lets DSE start from a
    remembered good configuration and remembered engineer decisions.
    """
    best_config: Optional[FlowConfig] = None
    best_score: float = 0.0
    # Die area of best_config, carried explicitly rather than inverted out of
    # best_score, so reporting it can never drift from the scoring formula.
    best_die_area_um2: Optional[float] = None
    decisions: dict = field(default_factory=dict)
    heuristics: list = field(default_factory=list)
    # Lowest utilization ever observed to congest for this design. Persistent
    # memory records congested runs, so the wall itself is learnable — which
    # saves re-probing it on every later session.
    congestion_wall_util: Optional[float] = None
    # The wall is density-dependent; this keeps the whole picture for reporting
    # while congestion_wall_util carries the one matching the search's density.
    congestion_wall_by_density: dict = field(default_factory=dict)
    # Every known wall, keyed by ConfigKey.wall_key (density, clock, aspect,
    # margin, pins, macros); congestion_wall_by_density is its stock-floorplan view.
    congestion_walls: dict = field(default_factory=dict)
    # Knob -> value an engineer last overrode by hand. Kept structured rather
    # than only as English in `heuristics`, so the search can actually act on
    # it instead of pattern-matching prose.
    corrections: dict = field(default_factory=dict)

    @property
    def is_cold(self) -> bool:
        return (
            self.best_config is None
            and self.best_score == 0.0
            and not self.decisions
            and not self.heuristics
            and self.congestion_wall_util is None
            and not self.corrections
        )


class MemoryPort(Protocol):
    """Seam for the not-yet-built memory subsystem."""

    def seed_for(self, design: str, tech: str, stage: str) -> MemorySeed: ...

    def record_run(self, result: RunResult) -> None: ...
