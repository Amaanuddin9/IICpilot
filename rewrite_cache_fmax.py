#!/usr/bin/env python3
"""Re-derive fmax/cp_delay of every ORFS cache entry from the real clock.

Entries written before the fix took cp_delay from finish__timing__fmax (max
over all clocks, so the virtual I/O clock for ibex). For each entry:

  * a clock_sweep run (variant clk_<cache key>) whose metadata.json still
    exists: re-parse that file with the fixed ORFSAdapter._parse_metadata and
    replace the fmax fields (other metrics are compared, not overwritten);
  * anything else (legacy runs in the shared `base` variant, whose metadata
    was overwritten by later runs): fmax/cp_delay set to None with a note.
    Never re-derived from another run's files.

The matching sweep_logs/clock_sweep/results/*.json records get the same
fields. Run after the sweep has finished (its driver holds the old parser):
    /home/amaanuddin/venvs/iicpilot/bin/python rewrite_cache_fmax.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from adaptive_iicpilot.eda_interface import ORFSAdapter
from adaptive_iicpilot.types import FlowConfig

REPO = Path(__file__).resolve().parent
RESULTS = REPO / "sweep_logs" / "clock_sweep" / "results"
FMAX_FIELDS = ("cp_delay_ns", "fmax_clock", "cp_delay_note",
               "orfs_fmax_headline_hz", "orfs_fmax_headline_clock")
LEGACY_NOTE = ("legacy run: its metadata.json no longer exists (shared base variant, "
               "overwritten), so reg-to-reg fmax cannot be read; not re-derived")


def main(dry_run: bool) -> None:
    adapter = ORFSAdapter()
    flow = Path(adapter.orfs_root).expanduser() / "flow"
    for path in sorted(adapter.cache_dir.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        config = FlowConfig(**{k: v for k, v in entry["config"].items() if k in FlowConfig.__dataclass_fields__})
        key = path.stem.split("-", 1)[1]
        nick = adapter._design_nickname(config.design)
        meta = flow / "reports" / "nangate45" / nick / f"clk_{key}" / "metadata.json"
        old = entry["metrics"].get("cp_delay_ns")
        if meta.is_file():
            parsed = adapter._parse_metadata(json.loads(meta.read_text()), config)
            mismatched = [f for f in ("wns_ns", "power_mw", "die_area_um2", "congestion")
                          if abs((entry["metrics"].get(f) or 0) - getattr(parsed, f)) > 1e-9]
            if mismatched:
                print(f"SKIP {path.name}: {meta} disagrees with the entry on {mismatched}; left untouched")
                continue
            new = {f: getattr(parsed, f) for f in FMAX_FIELDS}
            source = f"re-parsed {meta.parent.name}/metadata.json"
        else:
            new = {f: None for f in FMAX_FIELDS}
            new["cp_delay_note"] = LEGACY_NOTE
            source = "legacy -> None"
        entry["metrics"].update(new)
        cp = new["cp_delay_ns"]
        print(f"{path.name:30} {config.design:5} util={config.core_utilization:g} clock={config.clock_period_ns:g} "
              f"cp_delay {old} -> {cp if cp is None else round(cp, 5)} "
              f"({new['fmax_clock'] or '-'}; headline {new['orfs_fmax_headline_clock'] or '-'}) [{source}]")
        if not dry_run:
            path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        result = next(RESULTS.glob(f"{config.design}-{key}.json"), None)
        if result and not dry_run:
            rec = json.loads(result.read_text(encoding="utf-8"))
            if rec.get("metrics"):
                rec["metrics"].update(new)
                result.write_text(json.dumps(rec, indent=2), encoding="utf-8")

    # Determinism re-run records are not in the cache; refresh them the same way.
    for result in sorted(RESULTS.glob("*-r2.json")):
        rec = json.loads(result.read_text(encoding="utf-8"))
        meta = flow / "reports" / "nangate45" / adapter._design_nickname(rec["design"]) / rec["variant"] / "metadata.json"
        if rec.get("metrics") and meta.is_file():
            parsed = adapter._parse_metadata(json.loads(meta.read_text()), FlowConfig(**rec["config"]))
            rec["metrics"].update({f: getattr(parsed, f) for f in FMAX_FIELDS})
            print(f"{result.name}: re-run record cp_delay -> {parsed.cp_delay_ns}")
            if not dry_run:
                result.write_text(json.dumps(rec, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    main(parser.parse_args().dry_run)
