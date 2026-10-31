"""Every real measurement of a design, from every producer, under ConfigKey.

Producers:
  * ORFS result cache (.orfs_cache): DSE/EDA runs and the proven clock sweeps.
  * FloorplanAgent results (.floorplan_runs/<design>/fp_*/result.json): SCREEN
    runs (to global placement) and FULL runs, with SVG/PNG renders.

Only measured data comes out of here; nothing is estimated. When two producers
measured the same key, the point keeps both (`also_measured_by`) and says
whether they agree on die area, congestion, WNS and power, so a disagreement is
visible instead of silently resolved.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .configkey import ConfigKey, from_flow_config, from_floorplan_knobs
from .types import FlowConfig

FULL, SCREEN = "FULL", "SCREEN"
_UTIL_TEXT_RE = re.compile(r"util\s+([0-9.]+)%")
COMPARED = ("die_area_um2", "congestion", "wns_ns", "power_mw")


@dataclass
class Point:
    key: ConfigKey
    kind: str  # FULL or SCREEN
    source: str  # "orfs_cache" or "floorplan"
    ok: bool
    metrics: dict
    proven: bool = False
    variant: Optional[str] = None
    origin: Optional[str] = None
    svg: Optional[str] = None
    renders: dict = field(default_factory=dict)
    elapsed_s: Optional[float] = None
    also_measured_by: list = field(default_factory=list)

    @property
    def congestion(self) -> Optional[float]:
        return self.metrics.get("congestion")

    @property
    def congested(self) -> bool:
        c = self.congestion
        return c is not None and c > 1.0

    @property
    def fmax_mhz(self) -> Optional[float]:
        cp = self.metrics.get("cp_delay_ns")
        return 1000.0 / cp if cp else None

    def timing_status(self, limit: Optional[float]) -> Optional[str]:
        wns = self.metrics.get("wns_ns")
        if wns is None or self.kind != FULL:
            return None
        if wns >= 0:
            return "MET"
        return "ACCEPTABLE" if limit is not None and wns >= limit else "VIOLATED"

    def usable(self, limit: Optional[float]) -> bool:
        """Clean full-flow point: finished, routable, timing not VIOLATED."""
        return (self.kind == FULL and self.ok and not self.congested
                and (self.metrics.get("die_area_um2") or 0) > 0
                and self.timing_status(limit) in ("MET", "ACCEPTABLE"))


class Measurements:
    def __init__(self, adapter=None, artifacts_dir: Optional[Path] = None) -> None:
        if adapter is None:
            from .eda_interface import ORFSAdapter

            adapter = ORFSAdapter()
        self.adapter = adapter
        if artifacts_dir is None:
            from .floorplan.runner import default_artifacts_dir

            artifacts_dir = default_artifacts_dir()
        self.artifacts = Path(artifacts_dir)

    # --- producers ---

    def _from_cache(self, design: str) -> list[Point]:
        out = []
        stock = self.adapter.stock_clock_ns(design)
        for entry in self.adapter.cached_entries(design):
            cfg = entry.get("config") or {}
            config = FlowConfig(**{k: v for k, v in cfg.items() if k in FlowConfig.__dataclass_fields__})
            m = dict(entry.get("metrics") or {})
            out.append(Point(
                key=from_flow_config(config, stock), kind=FULL, source="orfs_cache",
                ok=bool(entry.get("ok", True)) and (m.get("die_area_um2") or 0) > 0,
                metrics=m, proven=bool(entry.get("proven")),
                origin=f".orfs_cache/{design}-{self.adapter.cache_key(config)}.json",
            ))
        return out

    def _from_floorplan(self, design: str) -> list[Point]:
        out = []
        stock_clock = self.adapter.stock_clock_ns(design)
        for path in sorted((self.artifacts / design).glob("fp_*/result.json")):
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if result.get("stage") not in (FULL, SCREEN) or not result.get("knobs"):
                continue
            match = _UTIL_TEXT_RE.search(result.get("util_text") or "")
            stock_util = float(match.group(1)) if match else None
            m = dict(result.get("metrics") or {})
            renders = {}
            render_json = path.parent / "render" / "render.json"
            if render_json.is_file():
                try:
                    data = json.loads(render_json.read_text(encoding="utf-8"))
                    renders = {view: data[view] for view in ("floorplan", "placement", "congestion")
                               if data.get(view)}
                except (OSError, json.JSONDecodeError):
                    pass
            out.append(Point(
                key=from_floorplan_knobs(result["knobs"], stock_util, stock_clock),
                kind=result["stage"], source="floorplan",
                ok=bool(result.get("ok")) and result.get("legal") is not False,
                metrics=m, variant=result.get("variant"),
                origin=str(path.parent.relative_to(self.artifacts.parent)).replace("\\", "/"),
                svg=result.get("svg"), renders=renders, elapsed_s=result.get("elapsed_s"),
            ))
        return out

    # --- the index ---

    def points(self, design: str, kind: str = FULL) -> list[Point]:
        """One point per ConfigKey. For FULL, a proven cache entry is the
        primary record; a duplicate from the other producer is attached (and
        lends its renders) rather than dropped."""
        raw = [p for p in self._from_cache(design) + self._from_floorplan(design) if p.kind == kind]
        merged: dict = {}
        for p in sorted(raw, key=lambda p: (not p.proven, p.source != "orfs_cache", p.metrics.get("cp_delay_ns") is None)):
            primary = merged.get(p.key)
            if primary is None:
                merged[p.key] = p
                continue
            agree = all(
                primary.metrics.get(k) is None or p.metrics.get(k) is None
                or abs(float(primary.metrics[k]) - float(p.metrics[k])) < 1e-6
                for k in COMPARED
            )
            primary.also_measured_by.append({"source": p.source, "origin": p.origin, "agrees": agree})
            primary.renders = primary.renders or p.renders
            primary.svg = primary.svg or p.svg
            primary.variant = primary.variant or p.variant
        return list(merged.values())

    def find(self, key: ConfigKey, kind: str = FULL) -> Optional[Point]:
        return next((p for p in self.points(key.design, kind) if p.key == key), None)

    def timing_limit(self, design: str, clock_ns: float) -> Optional[float]:
        return self.adapter.timing_threshold_ns(design, clock_ns)

    def median_full_seconds(self, design: str) -> Optional[float]:
        """Median measured wall time of a full flow for this design (floorplan
        FULL runs and clock-sweep runs record it); None if none recorded."""
        times = [p.elapsed_s for p in self._from_floorplan(design) if p.kind == FULL and p.elapsed_s]
        results = Path(__file__).resolve().parent.parent / "sweep_logs" / "clock_sweep" / "results"
        for path in results.glob(f"{design}-*.json"):
            try:
                times.append(float(json.loads(path.read_text(encoding="utf-8"))["runtime_s"]))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        if not times:
            return None
        times.sort()
        mid = len(times) // 2
        return times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2
