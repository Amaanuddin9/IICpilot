#!/usr/bin/env python3
"""Warm the ORFS result cache by sweeping any design up a utilization ladder.

Run this offline, before a demo. Each uncached rung is a real ORFS run
(ORFSAdapter.run: clean_all, full flow, metadata-generate, cache write);
afterwards every rung replays from cache in milliseconds. Already-cached
rungs replay instantly and are reported like fresh ones.

Usage (from the repo root, WSL or Windows):
    python sweep.py --design aes
    python sweep.py --design gcd --utils 50,60,70,80,85,90
    python sweep.py --design aes --utils 50,60 --stream   # echo ORFS output
    python sweep.py --design ibex --utils 80 --clocks 0.8,0.9,1.0,1.1,1.25

--clocks are MULTIPLES of the design's stock SDC period (ibex 2.2ns, aes 0.82ns,
gcd 0.25ns), so the same sweep is meaningful across designs.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from typing import Iterable

from adaptive_iicpilot.eda_interface import ORFS_UTIL_LADDER, ORFSAdapter
from adaptive_iicpilot.types import FlowConfig


def _parse_utils(text: str) -> list[float]:
    try:
        utils = [float(part) for part in text.replace(" ", "").split(",") if part]
    except ValueError:
        raise argparse.ArgumentTypeError(f"--utils must be comma-separated numbers, got {text!r}")
    if not utils:
        raise argparse.ArgumentTypeError("--utils is empty")
    return utils


def sweep(design: str, utils: Iterable[float], density: float = 0.0,
          stream: bool = False, clock_multiples: Iterable[float] = (1.0,)) -> None:
    on_line = (lambda line: print(f"    | {line}", flush=True)) if stream else None
    adapter = ORFSAdapter(on_line=on_line)
    stock = adapter.stock_clock_ns(design)
    print(f"cache dir: {adapter.cache_dir}")
    print(f"stock clock for {design}: {stock} ns")

    for util in utils:
        for multiple in clock_multiples:
            # Multiples of the stock period, so one sweep is comparable across
            # designs whose stock clocks differ by 10x.
            clock_ns = round(stock * multiple, 4) if stock else 0.0
            config = FlowConfig(
                design=design,
                core_utilization=util,
                placement_density=density,
                clock_period_ns=clock_ns,
            )
            cached = adapter.is_cached(config)
            print(f"\n=== {design} util={util:.0f} density={density or 'auto'} "
                  f"clock={clock_ns:g}ns ({multiple:g}x stock) "
                  f"({'cached' if cached else 'running'}) ===", flush=True)

            started = time.time()
            adapter.configure(config)
            result = adapter.run("synth", "route")
            elapsed = time.time() - started

            m = result.metrics
            print(json.dumps(asdict(m), indent=2), flush=True)
            print(
                f"clock={clock_ns:g}ns reg2reg cp_delay="
                f"{f'{m.cp_delay_ns:.4f}ns' if m.cp_delay_ns else 'n/a'} "
                f"wns={m.wns_ns:+.4f}ns power={m.power_mw:.3f}mW "
                f"die={m.die_area_um2:.1f}um2",
                flush=True,
            )
            print(
                f"ok={result.ok} congested={m.congested} timing_met={m.timing_met} "
                f"score={m.ppa_score():.3e} elapsed={elapsed:.1f}s",
                flush=True,
            )
            if result.failure:
                print(f"failure: {result.failure}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Warm the ORFS cache for a design.")
    parser.add_argument("--design", required=True, help="nangate45 design, e.g. aes, gcd, ibex")
    parser.add_argument(
        "--utils",
        type=_parse_utils,
        default=ORFS_UTIL_LADDER,
        help="comma-separated utilization rungs (default: the shared DSE ladder "
             + ",".join(f"{u:.0f}" for u in ORFS_UTIL_LADDER) + ")",
    )
    parser.add_argument(
        "--density",
        type=float,
        default=0.0,
        help="PLACE_DENSITY; 0 (default) lets ORFS compute it",
    )
    parser.add_argument(
        "--clocks",
        type=_parse_utils,
        default=[1.0],
        help="comma-separated MULTIPLES of the design's stock clock period "
             "(default: 1.0, the stock clock)",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="echo ORFS flow output line by line while each rung runs",
    )
    args = parser.parse_args()
    sweep(args.design, args.utils, args.density, args.stream, args.clocks)


if __name__ == "__main__":
    main()
