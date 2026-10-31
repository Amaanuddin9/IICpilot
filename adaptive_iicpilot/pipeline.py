"""The screened optimize pipeline: Control -> Floorplan (propose + screen) -> DSE.

  1. Control proposes candidates over the knobs that move die area together:
     utilization x core aspect ratio. Cold: a grid. Warm: memory's best
     measured config, one rung tighter on the same floorplan (unless memory
     already knows that rung is a wall), and nothing else.
  2. FloorplanAgent screens every candidate to global placement (~1/3 of a
     full flow), which already gives die area and routability congestion.
     Congested (> 1.0) or failed screens are pruned.
  3. DSE runs the FULL flow only on survivors, smallest screened die first,
     and stops at the first one that is clean in the full flow (routable and
     timing not VIOLATED): every survivor after it has a larger die.
  4. Signoff (signoff.SignoffAgent) runs on the FINAL best config only,
     after the caller has applied its objective (chat optimize(signoff=True)):
     KLayout DRC + router DRC + antenna, stored in memory by ConfigKey.

Every measurement is looked up by ConfigKey in both producers (ORFS result
cache, FloorplanAgent results), so a screen or full run already measured by
anyone replays instead of re-running. An interactive caller can forbid new
runs; an unmeasured candidate is then reported NOT_RUN, never estimated.

Time saved is an ESTIMATE and labelled as one: full flows avoided x the median
measured full-flow time of this design, minus the measured screening time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .configkey import ConfigKey, from_flow_config
from .eda_interface import ORFS_UTIL_LADDER
from .measurements import FULL, SCREEN, Measurements, Point
from .types import FlowConfig, MemorySeed, Proposal, RunResult

SCREEN_UTILS = (75.0, 80.0, 85.0, 90.0)
SCREEN_ASPECTS = (0.5, 0.75, 1.0)
CONGESTION_LIMIT = 1.0


@dataclass
class ScreenStats:
    mode: str = "cold"
    candidates: list = field(default_factory=list)  # labels, in proposal order
    screens_replayed: int = 0
    screens_run: int = 0
    screens_not_run: list = field(default_factory=list)
    pruned: list = field(default_factory=list)  # {"label", "congestion", "reason"}
    survivors: list = field(default_factory=list)  # {"label", "die_area_um2", "congestion"}
    full_runs_replayed: int = 0
    full_runs_run: int = 0
    full_not_run: list = field(default_factory=list)
    screen_seconds: float = 0.0
    median_full_seconds: Optional[float] = None
    full_runs_avoided: int = 0
    time_saved_s: Optional[float] = None
    skipped_by_memory: list = field(default_factory=list)

    @property
    def screens_total(self) -> int:
        return self.screens_replayed + self.screens_run

    @property
    def full_runs(self) -> int:
        return self.full_runs_replayed + self.full_runs_run

    def summary(self) -> str:
        saved = (f"est. {self.time_saved_s / 60:.0f} min saved ({self.full_runs_avoided} full flow(s) avoided "
                 f"x median {self.median_full_seconds / 60:.1f} min measured full flow, minus "
                 f"{self.screen_seconds / 60:.1f} min measured screening)"
                 if self.time_saved_s is not None else "time saved: unknown (no measured full-flow time)")
        return (f"{self.mode.upper()}: {len(self.candidates)} candidate(s), {self.screens_total} screen(s) "
                f"({self.screens_replayed} replayed, {self.screens_run} run), {len(self.pruned)} pruned, "
                f"{len(self.survivors)} survivor(s), {self.full_runs} full run(s) "
                f"({self.full_runs_replayed} replayed, {self.full_runs_run} run); {saved}")

    def as_dict(self) -> dict:
        return {
            "mode": self.mode, "candidates": self.candidates, "screens_run": self.screens_run,
            "screens_replayed": self.screens_replayed, "screens_total": self.screens_total,
            "screens_not_run": self.screens_not_run, "pruned": self.pruned, "survivors": self.survivors,
            "full_runs": self.full_runs, "full_runs_run": self.full_runs_run,
            "full_runs_replayed": self.full_runs_replayed, "full_not_run": self.full_not_run,
            "skipped_by_memory": self.skipped_by_memory,
            "screen_seconds": round(self.screen_seconds, 1),
            "median_full_seconds": self.median_full_seconds, "full_runs_avoided": self.full_runs_avoided,
            "time_saved_s": None if self.time_saved_s is None else round(self.time_saved_s, 1),
            "time_saved_is_estimate": True, "summary": self.summary(),
        }


def _knobs_for(key: ConfigKey):
    """FloorplanKnobs that run this key. AR 1 is written as stock (None), the
    form every existing ibex screen was measured under."""
    from .floorplan.knobs import FloorplanKnobs

    return FloorplanKnobs(design=key.design, tech=key.tech, core_utilization=key.util,
                          aspect_ratio=None if key.aspect == 1.0 else key.aspect,
                          core_margin=key.margin, io_strategy=key.pin_strategy)


def _next_rung(util: float) -> Optional[float]:
    higher = [u for u in ORFS_UTIL_LADDER if u > util]
    return higher[0] if higher else None


def propose(base: FlowConfig, seed: Optional[MemorySeed], stock_clock: Optional[float],
            emit: Callable[[str], None], stats: ScreenStats) -> list[ConfigKey]:
    """Control's candidate list."""
    stock = from_flow_config(base, stock_clock)
    if seed is not None and seed.best_config is not None:
        stats.mode = "warm"
        best = from_flow_config(seed.best_config, stock_clock)
        # The pipeline screens at the stock clock; clock is chosen afterwards.
        best = best.tweaked(clock_ns=stock.clock_ns)
        out = [best]
        tighter = _next_rung(best.util)
        wall = (seed.congestion_walls or {}).get(best.wall_key)
        if tighter is not None:
            if wall is not None and tighter >= wall:
                stats.skipped_by_memory.append(f"{best.tweaked(util=tighter).label(False)} (memory: wall at util {wall:g})")
            else:
                out.append(best.tweaked(util=tighter))
        emit(f"[Control] WARM proposal: start from memory's best measured config ({best.label(False)}); "
             f"{'probe one rung tighter' if len(out) > 1 else 'no tighter rung worth probing'}")
        return out
    stats.mode = "cold"
    out = [stock.tweaked(util=u, aspect=ar) for u in SCREEN_UTILS for ar in SCREEN_ASPECTS]
    emit(f"[Control] COLD proposal: {len(SCREEN_UTILS)} utilizations x {len(SCREEN_ASPECTS)} aspect ratios "
         f"= {len(out)} candidates")
    return out


def screened_search(
    base: FlowConfig,
    seed: Optional[MemorySeed],
    eda_agent,
    adapter,
    emit: Callable[[str], None],
    decide: Callable[[Proposal, FlowConfig], Optional[Proposal]],
    allow_screen_runs: bool = False,
    allow_full_runs: bool = False,
    floorplan_runner=None,
    index: Optional[Measurements] = None,
) -> tuple[Optional[RunResult], list[Proposal], ScreenStats]:
    index = index or Measurements(adapter=adapter)
    stock_clock = adapter.stock_clock_ns(base.design)
    stats = ScreenStats()
    history: list[Proposal] = []

    # 1. Control
    candidates = propose(base, seed, stock_clock, emit, stats)
    stats.candidates = [k.label(False) for k in candidates]

    # 2. Floorplan: screen
    screens = {p.key: p for p in index.points(base.design, SCREEN)}
    measured: list[tuple[ConfigKey, Point]] = []
    for key in candidates:
        point = screens.get(key)
        if point is not None:
            stats.screens_replayed += 1
        elif allow_screen_runs:
            runner = floorplan_runner
            if runner is None:
                from .floorplan.runner import FloorplanRunner

                runner = FloorplanRunner(emit=emit)
            emit(f"[Floorplan] screening {key.label(False)} to global placement")
            runner.run(_knobs_for(key), "SCREEN")
            index_now = {p.key: p for p in index.points(base.design, SCREEN)}
            point = index_now.get(key)
            stats.screens_run += 1
        if point is None:
            stats.screens_not_run.append(key.label(False))
            emit(f"[Floorplan] {key.label(False)}: NOT_RUN (no screen measured; live screening is off)")
            continue
        stats.screen_seconds += point.elapsed_s or 0.0
        measured.append((key, point))
        c = point.congestion
        verdict = ("FAILED" if not point.ok else
                   f"congestion {c:.4f} > {CONGESTION_LIMIT} -> pruned" if point.congested else
                   f"congestion {c:.4f} -> survives")
        emit(f"[Floorplan] screen {key.label(False)}: die {point.metrics.get('die_area_um2')} um2, {verdict}")
        if not point.ok or point.congested:
            stats.pruned.append({"label": key.label(False), "congestion": c,
                                 "reason": "failed" if not point.ok else "congested at global placement"})

    survivors = sorted(
        [(k, p) for k, p in measured if p.ok and not p.congested],
        key=lambda kp: (kp[1].metrics.get("die_area_um2") or float("inf"), kp[1].congestion or 0.0),
    )
    stats.survivors = [{"label": k.label(False), "die_area_um2": p.metrics.get("die_area_um2"),
                        "congestion": p.congestion} for k, p in survivors]
    emit(f"[Floorplan] {len(survivors)} survivor(s) of {len(measured)} screened: "
         + ", ".join(s["label"] for s in stats.survivors))

    # 3. DSE: full flow on survivors, smallest screened die first
    best: Optional[RunResult] = None
    for key, point in survivors:
        config = key.to_flow_config()
        proposal = Proposal(
            changes={"core_utilization": key.util, "aspect_ratio": key.aspect},
            rationale=(f"Full flow on screened survivor {key.label(False)}: screened die "
                       f"{point.metrics.get('die_area_um2')} um2, congestion {point.congestion}."),
            triggered_by="screened-survivor",
            seeded_from_memory=stats.mode == "warm",
        )
        decided = decide(proposal, config)
        if decided is None:
            emit(f"[DSE] engineer rejected the full run of {key.label(False)}; stopping")
            break
        history.append(decided)
        if decided.changes != proposal.changes:
            config = decided.apply_to(config)
        cached = adapter.is_cached(config)
        if not cached and not allow_full_runs:
            stats.full_not_run.append(key.label(False))
            emit(f"[DSE] {key.label(False)}: full flow NOT_RUN (not measured; live full runs are off)")
            continue
        emit(f"[DSE] full flow on {key.label(False)} ({'replay' if cached else 'live run'})")
        result = eda_agent.run_flow(config, "route")
        if cached:
            stats.full_runs_replayed += 1
        else:
            stats.full_runs_run += 1
        m = result.metrics
        clean = result.ok and not m.congested and m.timing_status != "VIOLATED"
        emit(f"[DSE] {key.label(False)}: {'CLEAN' if clean else 'not clean'} in the full flow "
             f"(die {m.die_area_um2} um2, congestion {m.congestion:.4f}, {m.timing_status} WNS {m.wns_ns:+.4f} ns)")
        if clean:
            best = result
            break
        if best is None or (result.ok and result.metrics.ppa_score() > best.metrics.ppa_score()):
            best = result

    median = index.median_full_seconds(base.design)
    stats.median_full_seconds = median
    # Only candidates that were actually screened count: an unscreened one
    # saved nothing, it simply was not measured.
    stats.full_runs_avoided = max(0, len(measured) - stats.full_runs - len(stats.full_not_run))
    if median:
        stats.time_saved_s = stats.full_runs_avoided * median - stats.screen_seconds
    emit(f"[Control] pipeline: {stats.summary()}")
    return best, history, stats


def clock_for_objective(index: Measurements, floorplan: ConfigKey, objective: str,
                        limit_for: Callable[[float], Optional[float]]) -> tuple[Optional[Point], list[Point]]:
    """Power and timing pick the CLOCK on a fixed floorplan (util, density,
    aspect, margin, pins, macros) from full runs measured on it.

    power:  lowest total power among usable points (routable, timing not
            VIOLATED).
    timing: highest reg-to-reg fmax among usable points. ACCEPTABLE counts
            (ORFS's own allowance); the caller reports the status explicitly.
    Returns (chosen, every usable point on that floorplan)."""
    same = [p for p in index.points(floorplan.design, FULL)
            if p.key.tweaked(clock_ns=floorplan.clock_ns) == floorplan]
    usable = [p for p in same if p.usable(limit_for(p.key.clock_ns))]
    if not usable:
        return None, []
    if objective == "power":
        return min(usable, key=lambda p: p.metrics.get("power_mw") or float("inf")), usable
    with_fmax = [p for p in usable if p.fmax_mhz]
    if not with_fmax:
        return None, usable
    return max(with_fmax, key=lambda p: p.fmax_mhz), usable
