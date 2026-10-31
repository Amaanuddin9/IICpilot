"""The EDA agent: decides HOW to run a flow, never WHICH parameters to try.

Parameter choices are entirely the DSE agent's job. This agent just drives
the EDAInterface, keeps run/failure counters, and emits status lines that a
future Claude-Code-style UI can subscribe to.
"""
from __future__ import annotations

from typing import Callable, Optional

from .eda_interface import EDAInterface
from .types import STAGES, FlowConfig, MemoryPort, RunResult


class EDAAgent:
    def __init__(
        self,
        interface: EDAInterface,
        memory: Optional[MemoryPort] = None,
        emit: Callable[[str], None] = lambda msg: None,
    ) -> None:
        self.interface = interface
        self.memory = memory
        self.emit = emit
        self.runs = 0
        self.failures = 0

    def run_flow(self, config: FlowConfig, target_stage: str = "route") -> RunResult:
        if target_stage not in STAGES:
            raise ValueError(f"unknown stage {target_stage!r}; expected one of {STAGES}")

        # FlowConfig's default clock (2.5ns) is a mock-era placeholder and is
        # not what a real run is constrained with — ibex ships at 2.2ns. Ask the
        # backend for the period it will actually apply so the log cannot claim
        # a clock that was never used.
        clock_ns = config.clock_period_ns
        effective = getattr(self.interface, "effective_clock_ns", None)
        if callable(effective):
            clock_ns = effective(config)

        self.emit(
            f"[EDA] run #{self.runs + 1}: {config.design} -> {target_stage} "
            f"(util={config.core_utilization:.1f}% density={config.placement_density:.2f} "
            f"clock={clock_ns:.2f}ns aspect={config.aspect_ratio:.2f})"
        )
        self.interface.configure(config)
        result = self.interface.run("synth", target_stage)
        self.runs += 1

        if not result.ok:
            self.failures += 1
            self.emit(f"[EDA] FAILURE at {result.reached_stage}: {result.failure}")
        else:
            m = result.metrics
            # Report die area, the quantity actually being optimized, whenever
            # the backend produces it. The mock has no die area, so it keeps
            # reporting instance area rather than printing a misleading 0.
            area = (
                f"die_area={m.die_area_um2:.1f}um2"
                if m.die_area_um2
                else f"area={m.area_um2:.1f}um2"
            )
            self.emit(
                f"[EDA] OK reached {result.reached_stage}: "
                f"wns={m.wns_ns:.3f}ns congestion={m.congestion:.2f} "
                f"{area} power={m.power_mw:.2f}mW"
            )

        if self.memory is not None:
            self.memory.record_run(result)

        return result
