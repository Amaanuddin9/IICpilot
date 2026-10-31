"""Runnable demo: python -m adaptive_iicpilot.demo_run

Runs picorv32 twice from the same aggressive base config:
  RUN 1 COLD: no memory seed (~vanilla IICPilot) — fails routing once, then
              fights congestion back to a routable, clean config.
  RUN 2 WARM: seeded with a remembered good config + an engineer heuristic
              from a prior session — starts clean, never fails, never
              re-derives the congestion fix.
"""
from __future__ import annotations

from .dse_agent import DSEAgent
from .eda_agent import EDAAgent
from .eda_interface import MockORFSAdapter
from .types import FlowConfig, MemorySeed, Proposal, RunResult


def run_once(label: str, base: FlowConfig, seed: MemorySeed | None):
    interface = MockORFSAdapter()
    eda_agent = EDAAgent(interface, emit=lambda msg: print(f"[{label}] {msg}"))
    dse_agent = DSEAgent(eda_agent, target_stage="route")

    print(f"\n=== {label} RUN ===")
    best, history = dse_agent.explore(base, seed=seed, max_iters=6)

    fights = sum(1 for p in history if "congestion" in p.triggered_by)
    return {
        "label": label,
        "flow_runs": eda_agent.runs,
        "failures": eda_agent.failures,
        "congestion_fights": fights,
        "reached_stage": best.reached_stage,
        "wns_ns": best.metrics.wns_ns,
        "area_um2": best.metrics.area_um2,
        "ppa_score": best.metrics.ppa_score(),
        "history": history,
    }


def main() -> None:
    base = FlowConfig(
        design="picorv32",
        clock_period_ns=2.5,
        core_utilization=72.0,
        placement_density=0.95,
    )

    cold_seed = None

    warm_seed = MemorySeed(
        best_config=FlowConfig(
            design="picorv32",
            clock_period_ns=2.5,
            core_utilization=40.0,
            placement_density=0.55,
        ),
        best_score=1.0,
        decisions={"core_utilization": 40.0},
        heuristics=[
            "On picorv32, prefer lowering core utilization over placement "
            "density to fix congestion (engineer correction, session 1)."
        ],
    )

    cold = run_once("COLD", base, cold_seed)
    warm = run_once("WARM", base, warm_seed)

    print("\n=== SUMMARY ===")
    header = (
        f"{'run':<6}{'flow runs':>11}{'failures':>10}{'cong-fights':>13}"
        f"{'reached':>10}{'WNS(ns)':>10}{'area(um2)':>12}{'PPA score':>14}"
    )
    print(header)
    print("-" * len(header))
    for r in (cold, warm):
        print(
            f"{r['label']:<6}{r['flow_runs']:>11}{r['failures']:>10}"
            f"{r['congestion_fights']:>13}{r['reached_stage']:>10}"
            f"{r['wns_ns']:>10.3f}{r['area_um2']:>12.1f}{r['ppa_score']:>14.3e}"
        )

    print("\n=== ADAPTIVE DELTA ===")
    print(
        f"COLD (no memory) had to fail routing once and fight congestion "
        f"{cold['congestion_fights']} times across {cold['flow_runs']} flow runs "
        f"before reaching a routable, clean configuration - this is IICPilot's "
        f"vanilla, session-blind behavior.\n"
        f"WARM (seeded from a remembered good config + a prior engineer "
        f"correction) reached an equally routable configuration in "
        f"{warm['flow_runs']} flow runs with {warm['failures']} failures and "
        f"{warm['congestion_fights']} congestion fights - it started from the "
        f"remembered clean region, so the whole recovery phase never happened.\n"
        f"Final PPA is comparable ({cold['ppa_score']:.3e} cold vs "
        f"{warm['ppa_score']:.3e} warm) because both runs share the same DSE "
        f"hill-climb machinery; the win from persistent memory is eliminating "
        f"failures and re-derivation, not a better PPA optimum."
    )


if __name__ == "__main__":
    main()
