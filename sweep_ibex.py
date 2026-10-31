#!/usr/bin/env python3
"""Warm the ORFS result cache by sweeping ibex up the utilization ladder.

Run this once, offline, before a demo. Each rung is a real 10-30 minute ORFS
run; afterwards every one of them replays from cache in milliseconds, which
is what lets the orchestrator demo show real silicon numbers live.

The ladder is shared with the DSE agent (ORFS_UTIL_LADDER), so the configs
warmed here are exactly the ones DSE will later ask for.

For other designs use sweep.py (--design <name> --utils 50,60,...).

Usage (from the repo root, WSL or Windows):
    ADAPTIVE_IICPILOT_BACKEND=orfs python3 sweep_ibex.py
    ADAPTIVE_IICPILOT_BACKEND=orfs python3 sweep_ibex.py --design ibex --utils 50 60
"""
from __future__ import annotations

import argparse

from adaptive_iicpilot.eda_interface import ORFS_UTIL_LADDER
from sweep import sweep


def main() -> None:
    """Kept for its original CLI (space-separated --utils, ibex default);
    the flow itself lives in sweep.py."""
    parser = argparse.ArgumentParser(description="Warm the ORFS cache for a design.")
    parser.add_argument("--design", default="ibex")
    parser.add_argument(
        "--utils",
        type=float,
        nargs="*",
        default=ORFS_UTIL_LADDER,
        help="utilization rungs to sweep (default: the shared DSE ladder)",
    )
    parser.add_argument(
        "--density",
        type=float,
        default=0.0,
        help="PLACE_DENSITY; 0 (default) lets ORFS compute it",
    )
    args = parser.parse_args()
    sweep(args.design, args.utils, args.density)


if __name__ == "__main__":
    main()
