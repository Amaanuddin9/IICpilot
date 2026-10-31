"""The DSE agent: decides WHICH parameters to try. This is the project's
core adaptive claim — the same rule-based hill-climb runs whether or not a
memory seed is available, but a warm seed changes where it starts, so a
previously-learned fix doesn't need to be re-derived every session.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from .eda_agent import EDAAgent
from .eda_interface import ORFS_UTIL_LADDER
from .types import FlowConfig, MemorySeed, Metrics, Proposal, RunResult

Decider = Callable[[Proposal, FlowConfig], Optional[Proposal]]


# Knobs a human correction may seed the search with. Clock is excluded: it
# lives in constraint.sdc and is not driven in the real regime.
_CORRECTABLE_KNOBS = ("core_utilization", "placement_density", "aspect_ratio")


def _orfs_backend() -> bool:
    return os.environ.get("ADAPTIVE_IICPILOT_BACKEND", "mock").strip().lower() == "orfs"


def _ladder_step(util: float, up: bool) -> Optional[float]:
    """Next rung of the shared utilization ladder, or None at the end of it.
    Stepping in rungs rather than by a delta keeps every config DSE asks for
    on a rung the sweep already cached."""
    rungs = ORFS_UTIL_LADDER if up else list(reversed(ORFS_UTIL_LADDER))
    for rung in rungs:
        if (rung > util) if up else (rung < util):
            return rung
    return None


class DSEAgent:
    def __init__(self, eda_agent: EDAAgent, target_stage: str = "route") -> None:
        self.eda_agent = eda_agent
        self.target_stage = target_stage

    def starting_config(
        self, base: FlowConfig, seed: Optional[MemorySeed]
    ) -> tuple[FlowConfig, bool]:
        """The cold-vs-warm switch: no usable memory means start from base
        (vanilla IICPilot behavior); a warm seed means resume from the last
        remembered good configuration and decisions instead of guessing."""
        if seed is None or seed.is_cold:
            return base, False

        config = base
        if seed.best_config is not None:
            config = config.tweaked(
                core_utilization=seed.best_config.core_utilization,
                placement_density=seed.best_config.placement_density,
                aspect_ratio=seed.best_config.aspect_ratio,
            )
        elif _orfs_backend() and seed.corrections:
            # Nothing measured for this design yet, but an engineer has
            # overridden a knob here before. Their value is a better starting
            # point than the base default, so the search opens there instead of
            # re-walking ground a human already ruled on. Deliberately `elif`:
            # once there IS a measured best config, measurement outranks a
            # remembered hand-edit.
            hints = {
                knob: value
                for knob, value in seed.corrections.items()
                if knob in _CORRECTABLE_KNOBS
            }
            if hints:
                config = config.tweaked(**hints)
        if seed.decisions:
            config = config.tweaked(**seed.decisions)
        if base.floorplan is not None:
            # The floorplan stage owns the shape. Memory's best_config and
            # corrections still seed utilization/density, but must not swap
            # the chosen floorplan back for whatever shape won last session.
            config = config.tweaked(aspect_ratio=base.aspect_ratio, floorplan=base.floorplan)
        return config, True

    def propose(
        self, config: FlowConfig, metrics: Metrics, seed: Optional[MemorySeed]
    ) -> Optional[Proposal]:
        if _orfs_backend():
            return self._propose_orfs(config, metrics, seed)

        # --- LLM-swap seam ---
        # This method is intentionally simple, rule-based, and fully
        # transparent (every Proposal carries a rationale + trigger). A
        # future LLM-backed proposer can replace this method's body without
        # touching explore()'s hill-climb or the HITL gate, as long as it
        # keeps returning Proposal | None.
        if metrics.congested:
            if seed is not None and any(
                "utilization" in h.lower() for h in seed.heuristics
            ):
                changes = {"core_utilization": config.core_utilization - 5}
                return Proposal(
                    changes=changes,
                    rationale=(
                        "Memory heuristic prefers lowering core utilization over "
                        "placement density to fix congestion on this design."
                    ),
                    triggered_by="congestion-utilization(memory)",
                    seeded_from_memory=True,
                )
            changes = {"placement_density": round(config.placement_density - 0.10, 10)}
            return Proposal(
                changes=changes,
                rationale=(
                    f"Congestion {metrics.congestion:.2f} exceeds 0.30; reducing "
                    "placement density to relieve routing pressure."
                ),
                triggered_by="congestion-density",
                seeded_from_memory=False,
            )

        if not metrics.timing_met:
            changes = {"clock_period_ns": round(config.clock_period_ns + 0.2, 10)}
            return Proposal(
                changes=changes,
                rationale=(
                    f"WNS {metrics.wns_ns:.3f}ns is negative; relaxing clock "
                    "period to recover timing."
                ),
                triggered_by="timing-violation",
                seeded_from_memory=False,
            )

        if metrics.wns_ns > 0.15 and config.core_utilization < 65:
            changes = {"core_utilization": config.core_utilization + 5}
            return Proposal(
                changes=changes,
                rationale=(
                    f"WNS {metrics.wns_ns:.3f}ns has headroom above 0.15ns with "
                    "utilization below 65%; raising utilization to shrink area."
                ),
                triggered_by="area-margin",
                seeded_from_memory=False,
            )

        return None

    def _propose_orfs(
        self, config: FlowConfig, metrics: Metrics, seed: Optional[MemorySeed]
    ) -> Optional[Proposal]:
        """Real-flow regime: walk the utilization ladder to shrink the die,
        and treat congestion as the wall to stay behind.

        Utilization is the primary knob because it is what actually moves
        die area on a real run (instance area barely moves with it). Clock
        period is never touched — it lives in constraint.sdc — and the small
        negative WNS real ibex shows at every rung is not chased.
        """
        util = config.core_utilization

        if metrics.congested:
            backed_off = _ladder_step(util, up=False)
            if backed_off is None:
                return None
            if seed is not None and any("density" in h.lower() for h in seed.heuristics):
                return Proposal(
                    changes={
                        "core_utilization": backed_off,
                        "placement_density": round(config.placement_density - 0.05, 10),
                    },
                    rationale=(
                        f"Congestion {metrics.congestion:.3f} exceeds the routable "
                        f"limit; memory says to ease placement density alongside "
                        f"utilization on this design."
                    ),
                    triggered_by="congestion-recovery(memory)",
                    seeded_from_memory=True,
                )
            return Proposal(
                changes={"core_utilization": backed_off},
                rationale=(
                    f"Congestion {metrics.congestion:.3f} exceeds the routable limit; "
                    f"backing utilization off to {backed_off:.0f}% to recover."
                ),
                triggered_by="congestion-recovery",
                seeded_from_memory=False,
            )

        tighter = _ladder_step(util, up=True)
        if tighter is None:
            return None
        wall = seed.congestion_wall_util if seed is not None else None
        if wall is not None and tighter >= wall:
            # Memory already paid for this probe in an earlier session; there is
            # nothing to learn by congesting the same rung again.
            return None
        return Proposal(
            changes={"core_utilization": tighter},
            rationale=(
                f"Congestion {metrics.congestion:.3f} is inside the limit with die "
                f"area {metrics.die_area_um2:.0f}um2; raising utilization to "
                f"{tighter:.0f}% to shrink the die."
            ),
            triggered_by="die-area-shrink",
            seeded_from_memory=False,
        )

    def explore(
        self,
        base: FlowConfig,
        seed: Optional[MemorySeed] = None,
        max_iters: int = 6,
        decide: Optional[Decider] = None,
    ) -> tuple[RunResult, list[Proposal]]:
        # HITL-gate seam: `decide` sees every proposal before it is applied
        # and returns the proposal to accept as-is, a modified proposal to
        # edit-and-accept, or None to reject it and stop exploring.
        decide = decide or (lambda proposal, config: proposal)

        config, _seeded = self.starting_config(base, seed)
        result = self.eda_agent.run_flow(config, self.target_stage)
        best = result
        history: list[Proposal] = []

        for _ in range(max_iters):
            proposal = self.propose(config, result.metrics, seed)
            if proposal is None:
                break

            decided = decide(proposal, config)
            if decided is None:
                break
            history.append(decided)

            config = decided.apply_to(config)
            result = self.eda_agent.run_flow(config, self.target_stage)

            if not result.ok:
                # Still failing: step onward and keep trying to clear it.
                continue

            if (not best.ok) or result.metrics.ppa_score() > best.metrics.ppa_score():
                best = result
                continue

            # Clean (or at least not-failing) run with no gain: converge.
            break

        return best, history
