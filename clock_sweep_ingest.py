#!/usr/bin/env python3
"""Record proven clock_sweep.py measurements in persistent memory.

Only records whose proof passed are ingested, and only measured metrics (no
estimator output). Goes through SQLiteMemory.record_run rather than
`memory_cli import-cache`: import-cache skips any (design, util, density, clock,
ok) already stored, which would keep a legacy row's numbers even where the
proven run differs. record_run inserts a differing measurement as its own row
and only bumps the replay count of an identical one.

Back up first:  python -m adaptive_iicpilot.memory_cli backup <dest.db>
    /home/amaanuddin/venvs/iicpilot/bin/python clock_sweep_ingest.py
"""
from __future__ import annotations

import json
from pathlib import Path

from adaptive_iicpilot.sqlite_memory import SQLiteMemory
from adaptive_iicpilot.types import FlowConfig, Metrics, RunResult

RESULTS = Path(__file__).resolve().parent / "sweep_logs" / "clock_sweep" / "results"


def main() -> None:
    memory = SQLiteMemory()
    print(f"db: {memory.db_path}")
    added = skipped = 0
    for path in sorted(RESULTS.glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec["rerun"] or not rec["proof"]["ok"] or not rec["metrics"]:
            print(f"  skip {path.name}: {'determinism re-run' if rec['rerun'] else 'not proven / no metrics'}")
            skipped += 1
            continue
        result = RunResult(
            config=FlowConfig(**rec["config"]),
            reached_stage=rec["metrics"]["stage"],
            metrics=Metrics(**rec["metrics"]),
            ok=rec["flow_ok"],
            failure=None if rec["flow_ok"] else "ORFS flow returned non-zero",
        )
        memory.record_run(result)
        added += 1
        m = rec["metrics"]
        print(f"  + {rec['design']} util={rec['key']['util']} clock={rec['key']['clock_ns']:g}ns "
              f"wns={m['wns_ns']:+.4f} power={m['power_mw']:.3f}mW die={m['die_area_um2']:.1f}")
    print(f"recorded {added} proven run(s), skipped {skipped}")


if __name__ == "__main__":
    main()
