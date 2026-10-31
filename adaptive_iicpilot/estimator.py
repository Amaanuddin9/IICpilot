"""Estimate an unmeasured configuration from that design's REAL measurements.

The mock in eda_interface.py is calibrated against a paper, not against this
machine's ORFS build, so using it for a design we have actually measured throws
away better information: ibex has a whole cached utilization ladder. Die area is
very nearly linear in 1/utilization (the core area is filled to `utilization`,
so area scales as 1/utilization), which makes a two-parameter fit of the real
points accurate to a fraction of a percent.

What this deliberately does NOT do is invent congestion or timing outside the
measured range. Those are not smooth functions of utilization — congestion has
a wall in it — so beyond the measured span they are reported as unknown rather
than extrapolated. That is the whole point: an estimate that admits what it
cannot know.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .eda_interface import ORFSAdapter

# Fitting a line needs two points; one measurement tells us nothing about slope.
MIN_POINTS = 2


@dataclass
class MeasuredPoint:
    util: float
    die_area_um2: float
    core_area_um2: float
    instance_area_um2: float
    power_mw: float
    congestion: float
    wns_ns: float
    tns_ns: float
    cell_count: int


def _interpolate(x: float, points: list[tuple[float, float]]) -> Optional[float]:
    """Linear interpolation between the neighbouring measured utilizations.
    Returns None outside the measured span — callers must treat that as unknown
    rather than extrapolating a non-monotonic quantity."""
    if not points:
        return None
    ordered = sorted(points)
    if x < ordered[0][0] or x > ordered[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            span = (x - x0) / (x1 - x0)
            return y0 + span * (y1 - y0)
    return ordered[-1][1]


def _nearest(x: float, points: list[tuple[float, float]]) -> Optional[float]:
    if not points:
        return None
    return min(points, key=lambda point: abs(point[0] - x))[1]


class CalibratedEstimator:
    """Predicts metrics for one design from its own cached ORFS runs."""

    def __init__(self, design: str, points: list[MeasuredPoint],
                 clock_ns: Optional[float] = None) -> None:
        self.design = design
        # The fit is only valid at one clock period: relaxing the clock changes
        # cell sizing, buffering and therefore area and power. Mixing clocks
        # would produce a confident number with no physical meaning.
        self.clock_ns = clock_ns
        # A failed run is cached with zero area; it carries no usable geometry.
        self.points = sorted(
            (point for point in points if point.die_area_um2 > 0),
            key=lambda point: point.util,
        )

    @property
    def usable(self) -> bool:
        return len(self.points) >= MIN_POINTS

    @property
    def estimated_point_count(self) -> int:
        return len(self.points)

    @property
    def util_range(self) -> tuple[float, float]:
        return self.points[0].util, self.points[-1].util

    @classmethod
    def measured_clocks(cls, design: str, adapter: Optional[ORFSAdapter] = None) -> list[float]:
        """Clock periods this design actually has measurements at."""
        adapter = adapter or ORFSAdapter()
        clocks = set()
        for entry in adapter.cached_entries(design):
            config = entry.get("config") or {}
            if config.get("placement_density") or not entry.get("ok", False):
                continue
            clock = config.get("clock_period_ns")
            if clock:
                clocks.add(round(float(clock), 4))
        return sorted(clocks)

    @classmethod
    def from_cache(cls, design: str, adapter: Optional[ORFSAdapter] = None,
                   clock_ns: Optional[float] = None,
                   floorplan_key: Optional[tuple] = None) -> "CalibratedEstimator":
        """floorplan_key: ConfigKey.floorplan_key (aspect, margin, pins,
        macros) the fit is for; default the stock floorplan. A curve over
        utilization is only meaningful on ONE floorplan: util 85 congests at
        AR 1 and routes at AR 0.5."""
        from .configkey import STOCK_MACROS, STOCK_PINS, from_flow_config
        from .types import FlowConfig

        adapter = adapter or ORFSAdapter()
        if clock_ns is None:
            clock_ns = adapter.stock_clock_ns(design)
        if floorplan_key is None:
            floorplan_key = (1.0, None, STOCK_PINS, STOCK_MACROS)
        points: list[MeasuredPoint] = []
        for entry in adapter.cached_entries(design):
            config = entry.get("config") or {}
            metrics = entry.get("metrics") or {}
            fields = {k: v for k, v in config.items() if k in FlowConfig.__dataclass_fields__}
            if from_flow_config(FlowConfig(**fields), clock_ns).floorplan_key != floorplan_key:
                continue
            # util 0 is the design's own FLOORPLAN_DEF (aes), not a utilization.
            if not config.get("core_utilization"):
                continue
            # Only the auto-density ladder is comparable; a run with an explicit
            # placement density is a different experiment.
            if config.get("placement_density"):
                continue
            if not entry.get("ok", False):
                continue
            # Points at another clock are a different experiment entirely.
            if clock_ns is not None:
                entry_clock = config.get("clock_period_ns")
                if not entry_clock or abs(float(entry_clock) - clock_ns) > 1e-9:
                    continue
            points.append(
                MeasuredPoint(
                    util=float(config.get("core_utilization", 0.0)),
                    die_area_um2=float(metrics.get("die_area_um2", 0.0)),
                    core_area_um2=float(metrics.get("core_area_um2", 0.0)),
                    instance_area_um2=float(metrics.get("area_um2", 0.0)),
                    power_mw=float(metrics.get("power_mw", 0.0)),
                    congestion=float(metrics.get("congestion", 0.0)),
                    wns_ns=float(metrics.get("wns_ns", 0.0)),
                    tns_ns=float(metrics.get("tns_ns", 0.0)),
                    cell_count=int(metrics.get("cell_count", 0) or 0),
                )
            )
        return cls(design, points, clock_ns)

    def _fit_vs_inverse_util(self, value: str) -> tuple[float, float]:
        """Least-squares fit of a metric against 1/utilization."""
        xs = [100.0 / point.util for point in self.points]
        ys = [getattr(point, value) for point in self.points]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:
            return 0.0, mean_y
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = sxy / sxx
        return slope, mean_y - slope * mean_x

    def estimate_die_area(self, util: float) -> float:
        slope, intercept = self._fit_vs_inverse_util("die_area_um2")
        return max(0.0, slope * (100.0 / util) + intercept)

    def estimate(self, util: float) -> dict:
        """Metrics-summary shaped dict. Unknown quantities are None, never 0,
        so a caller cannot mistake "not measured" for "measured as zero"."""
        low, high = self.util_range
        in_range = low <= util <= high

        die_area = round(self.estimate_die_area(util), 1)
        core_area = round(max(0.0, self._fit_value("core_area_um2", util)), 1)
        instance_area = _interpolate(
            util, [(p.util, p.instance_area_um2) for p in self.points]
        )
        power = _interpolate(util, [(p.util, p.power_mw) for p in self.points])
        if power is None:
            # Power barely varies with utilization on a fixed netlist, so the
            # nearest measurement is a defensible stand-in outside the span.
            power = _nearest(util, [(p.util, p.power_mw) for p in self.points])

        congestion = _interpolate(util, [(p.util, p.congestion) for p in self.points])
        wns = _interpolate(util, [(p.util, p.wns_ns) for p in self.points])
        tns = _interpolate(util, [(p.util, p.tns_ns) for p in self.points])
        cells = _nearest(util, [(p.util, float(p.cell_count)) for p in self.points])

        clock_label = f"{self.clock_ns:g}ns" if self.clock_ns else "the measured clock"
        unknown_note = (
            f"unknown (outside measured range {low:g}-{high:g}% utilization "
            f"at {clock_label})"
        )
        summary = {
            "die_area_um2": die_area,
            "core_area_um2": core_area,
            "instance_area_um2": round(instance_area, 1) if instance_area is not None else None,
            "power_mw": round(power, 3) if power is not None else None,
            "congestion": round(congestion, 4) if congestion is not None else None,
            "wns_ns": round(wns, 4) if wns is not None else None,
            "tns_ns": round(tns, 4) if tns is not None else None,
            "cell_count": int(cells) if cells is not None else None,
            "congested": (congestion > 1.0) if congestion is not None else None,
            "measured_util_range": [low, high],
            "measured_at_clock_ns": self.clock_ns,
            "estimated_from_points": len(self.points),
        }

        if congestion is None:
            summary["verdict"] = (
                f"UNKNOWN - congestion is {unknown_note}, so routability cannot be "
                f"judged; only die area was extrapolated"
            )
            summary["congestion_note"] = unknown_note
            summary["wns_note"] = unknown_note
        elif congestion > 1.0:
            summary["verdict"] = (
                f"CONGESTED - congestion {congestion:.4f} is ABOVE the 1.0 limit, "
                f"so this configuration is not usable (estimated)"
            )
        else:
            summary["verdict"] = (
                f"CLEAN - congestion {congestion:.4f} is BELOW the 1.0 limit, "
                f"so this configuration is routable (estimated)"
            )
        if not in_range:
            summary["range_note"] = (
                f"utilization {util:g}% is outside the measured range "
                f"{low:g}-{high:g}%; die area is extrapolated"
            )
        return summary

    def _fit_value(self, field: str, util: float) -> float:
        slope, intercept = self._fit_vs_inverse_util(field)
        return slope * (100.0 / util) + intercept
