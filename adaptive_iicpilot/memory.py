"""The Memory agent: the real, persistent MemoryPort implementation.

Where MemorySeed/MemoryPort in types.py describe the contract, and
demo_run.py fakes a "warm" session by hand-writing a MemorySeed, JSONMemory
is what actually closes the loop — it writes what was learned to disk so
the next OS-level session (a different `python -m adaptive_iicpilot`
invocation, hours or days later) starts warm on its own, with no one
hand-authoring a seed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .types import FlowConfig, MemorySeed, Proposal, RunResult


class JSONMemory:
    DEFAULT_PATH = Path.home() / ".adaptive_iicpilot" / "memory.json"
    HEURISTIC_CAP = 20

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else self.DEFAULT_PATH

    # --- MemoryPort ---

    def seed_for(self, design: str, tech: str, stage: str) -> MemorySeed:
        entry = self._load().get("designs", {}).get(self._key(design, tech))
        if entry is None:
            return MemorySeed()

        best_config = FlowConfig(**entry["best_config"]) if entry.get("best_config") else None
        heuristics = [h["text"] for h in entry.get("heuristics", {}).values()]
        return MemorySeed(
            best_config=best_config,
            best_score=entry.get("best_score", 0.0),
            decisions=dict(entry.get("decisions", {})),
            heuristics=heuristics,
        )

    def record_run(self, result: RunResult) -> None:
        data = self._load()
        entry = self._entry(data, result.config.design, result.config.tech)
        entry["run_count"] = entry.get("run_count", 0) + 1
        if not result.ok:
            entry["failure_count"] = entry.get("failure_count", 0) + 1
        entry["updated_at"] = self._now()
        self._save(data)

    # --- beyond MemoryPort: the real learning step ---

    def record_session(self, best: RunResult, history: list[Proposal]) -> None:
        data = self._load()
        entry = self._entry(data, best.config.design, best.config.tech)

        if best.ok and best.metrics.ppa_score() > entry.get("best_score", 0.0):
            entry["best_score"] = best.metrics.ppa_score()
            entry["best_config"] = {
                "design": best.config.design,
                "tech": best.config.tech,
                "clock_period_ns": best.config.clock_period_ns,
                "core_utilization": best.config.core_utilization,
                "placement_density": best.config.placement_density,
                "aspect_ratio": best.config.aspect_ratio,
            }
            decisions = {}
            if best.config.clock_period_ns != FlowConfig(design=best.config.design).clock_period_ns:
                decisions["clock_period_ns"] = best.config.clock_period_ns
            entry["decisions"] = decisions

        if best.ok:
            heuristics = entry.setdefault("heuristics", {})
            for proposal in history:
                if proposal.seeded_from_memory:
                    continue
                heuristics[proposal.triggered_by] = {
                    "text": proposal.rationale,
                    "hits": heuristics.get(proposal.triggered_by, {}).get("hits", 0) + 1,
                    "last_seen": self._now(),
                }
            self._evict_stale_heuristics(heuristics)

        entry["updated_at"] = self._now()
        self._save(data)

    # --- internals ---

    def _entry(self, data: dict, design: str, tech: str) -> dict:
        designs = data.setdefault("designs", {})
        return designs.setdefault(self._key(design, tech), {})

    def _evict_stale_heuristics(self, heuristics: dict) -> None:
        while len(heuristics) > self.HEURISTIC_CAP:
            oldest_key = min(heuristics, key=lambda k: heuristics[k]["last_seen"])
            del heuristics[oldest_key]

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "designs": {}}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.path)

    @staticmethod
    def _key(design: str, tech: str) -> str:
        return f"{design}::{tech}"

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
