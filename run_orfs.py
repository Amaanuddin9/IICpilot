#!/usr/bin/env python3
"""Standalone OpenROAD-flow-scripts runner + metrics parser (Milestone B).

Proves we can drive a REAL ORFS flow and parse its metadata.json, tested on
its own before ORFSAdapter is wired up to it (Milestone C). Deliberately
self-contained and stdlib-only: it runs from the ORFS tree in WSL, where the
adaptive_iicpilot package is not importable.

Knobs are passed to make as environment variables because every ORFS
config.mk declares them with `?=` — config.mk itself is never edited.

Usage (in WSL, after `cd ~/OpenROAD-flow-scripts && source env.sh`):
    python3 run_orfs.py --design ibex --util 50 --clean
    python3 run_orfs.py --design ibex --util 82 --density 0.90 --clean
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ORFS_ROOT = Path(os.environ.get("ORFS_ROOT", Path.home() / "OpenROAD-flow-scripts"))
FLOW_DIR = ORFS_ROOT / "flow"
PLATFORM = "nangate45"
LOG_TAIL_LEN = 15

# Verified against a real metadata.json (Metrics_2.1.2).
METRIC_KEYS = {
    "wns_ns": "finish__timing__setup__ws",
    "tns_ns": "finish__timing__setup__tns",
    "area_um2": "finish__design__instance__area",
    "cell_count": "finish__design__instance__count",
    "congestion": "globalplace__gpl__routability__congestion",
    "drc_errors": "detailedroute__route__drc_errors",
}
POWER_KEY = "finish__power__total"  # Watts

# ORFS stage prefixes in flow order; the last one present names how far it got.
STAGE_PREFIXES = [
    "synth",
    "floorplan",
    "globalplace",
    "placeopt",
    "detailedplace",
    "cts",
    "globalroute",
    "detailedroute",
    "finish",
]

_NICKNAME_RE = re.compile(r"^\s*(?:export\s+)?DESIGN_NICKNAME\s*[:?]?=\s*(\S+)", re.MULTILINE)


@dataclass
class Metrics:
    stage: str
    wns_ns: float
    tns_ns: float
    area_um2: float
    cell_count: int
    congestion: float
    drc_errors: int
    power_mw: float
    ok: bool

    @property
    def timing_met(self) -> bool:
        return self.wns_ns >= 0

    @property
    def congested(self) -> bool:
        return self.drc_errors > 0 or self.congestion > 1.0


def design_dir(design: str) -> Path:
    return FLOW_DIR / "designs" / PLATFORM / design


def design_config(design: str) -> str:
    # Relative on purpose: make is invoked with cwd=FLOW_DIR.
    return f"./designs/{PLATFORM}/{design}/config.mk"


def design_nickname(design: str) -> str:
    """Reports land under DESIGN_NICKNAME, which can differ from DESIGN_NAME
    (ibex vs ibex_core) and in principle from the directory name."""
    config = design_dir(design) / "config.mk"
    match = _NICKNAME_RE.search(config.read_text(encoding="utf-8"))
    return match.group(1) if match else design


def metadata_path(design: str) -> Path:
    return FLOW_DIR / "reports" / PLATFORM / design_nickname(design) / "base" / "metadata.json"


def validate_design(design: str) -> None:
    if not FLOW_DIR.is_dir():
        sys.exit(f"ERROR: ORFS flow dir not found: {FLOW_DIR}\nSet ORFS_ROOT if it lives elsewhere.")
    if not (design_dir(design) / "config.mk").is_file():
        available = sorted(p.name for p in (FLOW_DIR / "designs" / PLATFORM).iterdir() if p.is_dir())
        sys.exit(f"ERROR: unknown design {design!r} for {PLATFORM}.\nAvailable: {', '.join(available)}")


def _make(args: list[str], env: dict) -> tuple[bool, list[str]]:
    """Run make in the flow dir, echoing output live (a real run takes
    10-30 min, so silence would be indistinguishable from a hang)."""
    print(f"$ make {' '.join(args)}", flush=True)
    tail: deque[str] = deque(maxlen=LOG_TAIL_LEN)
    process = subprocess.Popen(
        ["make", *args],
        cwd=FLOW_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        line = line.rstrip("\n")
        print(line, flush=True)
        tail.append(line)
    return process.wait() == 0, list(tail)


def _flow_env(util: float, density: Optional[float]) -> dict:
    env = os.environ.copy()
    env["CORE_UTILIZATION"] = str(util)
    if density is not None:
        env["PLACE_DENSITY"] = str(density)
    return env


def run_flow(design: str, util: float, density: Optional[float]) -> tuple[bool, list[str]]:
    return _make([f"DESIGN_CONFIG={design_config(design)}"], _flow_env(util, density))


def generate_metadata(design: str, util: float, density: Optional[float]) -> None:
    """metadata.json is NOT written by the default flow target — it comes from
    the separate metadata-generate target (util/utils.mk), which harvests
    genMetrics.py output from whatever logs exist. Safe after a failed flow
    too, which is how a congested run still reports its metrics. The sibling
    `metadata` target is avoided on purpose: it also runs metadata-check,
    which gates on a QoR rules file and fails a deliberately aggressive run."""
    ok, _ = _make(
        ["metadata-generate", f"DESIGN_CONFIG={design_config(design)}"],
        _flow_env(util, density),
    )
    if not ok:
        print("WARNING: metadata-generate returned non-zero.", flush=True)


def clean_design(design: str) -> None:
    ok, _ = _make(["clean_all", f"DESIGN_CONFIG={design_config(design)}"], os.environ.copy())
    if not ok:
        print("WARNING: clean_all returned non-zero; continuing anyway.", flush=True)


def _num(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _last_stage(data: dict) -> str:
    prefixes = {key.split("__")[0] for key in data}
    reached = [stage for stage in STAGE_PREFIXES if stage in prefixes]
    return reached[-1] if reached else "unknown"


def parse_metrics(design: str, ok: bool) -> Metrics:
    path = metadata_path(design)
    if not path.is_file():
        sys.exit(
            f"ERROR: no metrics at {path}\n"
            "The flow died before writing metadata.json — check the make output above."
        )
    data = json.loads(path.read_text(encoding="utf-8"))

    return Metrics(
        stage=_last_stage(data),
        wns_ns=_num(data.get(METRIC_KEYS["wns_ns"])),
        tns_ns=_num(data.get(METRIC_KEYS["tns_ns"])),
        area_um2=_num(data.get(METRIC_KEYS["area_um2"])),
        cell_count=int(_num(data.get(METRIC_KEYS["cell_count"]))),
        congestion=_num(data.get(METRIC_KEYS["congestion"])),
        drc_errors=int(_num(data.get(METRIC_KEYS["drc_errors"]))),
        power_mw=_num(data.get(POWER_KEY)) * 1000.0,
        ok=ok,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real ORFS flow and parse its metrics.")
    parser.add_argument("--design", default="ibex")
    parser.add_argument("--util", type=float, default=50.0, help="CORE_UTILIZATION")
    parser.add_argument("--density", type=float, default=None, help="PLACE_DENSITY")
    parser.add_argument("--clean", action="store_true", help="make clean_all before running")
    args = parser.parse_args()

    validate_design(args.design)

    knobs = f"CORE_UTILIZATION={args.util}"
    if args.density is not None:
        knobs += f" PLACE_DENSITY={args.density}"
    print(f"=== {args.design} ({design_nickname(args.design)}) @ {knobs} ===", flush=True)

    if args.clean:
        clean_design(args.design)

    ok, tail = run_flow(args.design, args.util, args.density)
    if not ok:
        print(f"\n=== FLOW FAILED — last {LOG_TAIL_LEN} lines ===", flush=True)
        for line in tail:
            print(line, flush=True)

    generate_metadata(args.design, args.util, args.density)
    metrics = parse_metrics(args.design, ok)
    print("\n=== METRICS ===", flush=True)
    print(json.dumps(asdict(metrics), indent=2), flush=True)
    print(f"timing_met={metrics.timing_met} congested={metrics.congested}", flush=True)


if __name__ == "__main__":
    main()
