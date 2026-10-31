#!/usr/bin/env python3
"""Re-run one already-measured configuration and compare against its cached result.

Two jobs in one run:
  * capture metrics the cached entry lacks (cp_delay/fmax was not parsed when
    the original runs were cached), and
  * measure run-to-run noise, which is what decides whether a small delta --
    the 0.3% power difference between utilizations, say -- is signal or noise.

The cached entry is moved aside rather than overwritten, so both measurements
survive and can be compared.

    python repro_check.py --design ibex --util 80          # stock clock
    python repro_check.py --design ibex --util 80 --wait-for-sweep
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from adaptive_iicpilot.eda_interface import ORFSAdapter
from adaptive_iicpilot.types import FlowConfig

# Fields worth comparing; cp_delay is expected to differ (it was not captured
# originally), so it is reported but not counted as a mismatch.
COMPARED = ("die_area_um2", "core_area_um2", "area_um2", "power_mw",
            "wns_ns", "tns_ns", "congestion", "cell_count")


def _sweep_running() -> bool:
    found = subprocess.run(["pgrep", "-f", "sweep.py"], capture_output=True, text=True)
    return bool(found.stdout.strip())


def wait_for_sweep(poll_s: int = 60) -> None:
    while _sweep_running():
        print(f"[{time.strftime('%H:%M:%S')}] sweep still running; waiting...", flush=True)
        time.sleep(poll_s)
    print(f"[{time.strftime('%H:%M:%S')}] sweep finished; starting the re-run", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-run a config and compare to cache.")
    parser.add_argument("--design", default="ibex")
    parser.add_argument("--util", type=float, default=80.0)
    parser.add_argument("--density", type=float, default=0.0)
    parser.add_argument("--clock", type=float, default=0.0,
                        help="clock period in ns; 0 (default) means the stock clock")
    parser.add_argument("--wait-for-sweep", action="store_true",
                        help="block until no sweep.py process remains")
    args = parser.parse_args()

    if args.wait_for_sweep:
        wait_for_sweep()

    adapter = ORFSAdapter(on_line=None)
    config = FlowConfig(
        design=args.design,
        core_utilization=args.util,
        placement_density=args.density,
        clock_period_ns=args.clock,
    )
    clock_ns = adapter.effective_clock_ns(config)
    path = adapter._cache_path(config)
    print(f"design={args.design} util={args.util:g} clock={clock_ns:g}ns")
    print(f"cache entry: {path}")

    if not path.is_file():
        print("No cached entry for this config; nothing to compare against.")
        return

    baseline = json.loads(path.read_text(encoding="utf-8"))
    kept = path.with_suffix(".json.baseline")
    shutil.copy2(path, kept)
    path.unlink()  # force a real run; the baseline is preserved beside it
    print(f"baseline preserved at: {kept}")

    started = time.time()
    adapter.configure(config)
    result = adapter.run("synth", "route")
    elapsed = time.time() - started
    print(f"re-run finished in {elapsed:.1f}s (ok={result.ok})")

    fresh = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if fresh is None:
        print("The re-run wrote no cache entry; restoring the baseline.")
        shutil.copy2(kept, path)
        return

    old, new = baseline["metrics"], fresh["metrics"]
    print()
    print(f"{'metric':<18}{'cached':>14}{'re-run':>14}{'delta':>14}{'delta %':>10}")
    print("-" * 70)
    mismatches = []
    for field in COMPARED:
        a, b = old.get(field), new.get(field)
        if a is None or b is None:
            print(f"{field:<18}{str(a):>14}{str(b):>14}{'':>14}{'':>10}")
            continue
        delta = b - a
        pct = (100.0 * delta / a) if a else 0.0
        print(f"{field:<18}{a:>14.4f}{b:>14.4f}{delta:>+14.4f}{pct:>+9.3f}%")
        if abs(delta) > 1e-9:
            mismatches.append((field, a, b, pct))

    print()
    print(f"{'cp_delay_ns':<18}{str(old.get('cp_delay_ns')):>14}"
          f"{str(new.get('cp_delay_ns')):>14}   (newly captured)")
    cp = new.get("cp_delay_ns") or 0.0
    if cp:
        print(f"{'fmax_mhz':<18}{'n/a':>14}{1000.0 / cp:>14.1f}   (newly captured)")

    print()
    if not mismatches:
        print("VERDICT: bit-identical on every compared metric -> the flow is "
              "deterministic, so small deltas between configurations are real "
              "signal, not run-to-run noise.")
    else:
        worst = max(mismatches, key=lambda m: abs(m[3]))
        print(f"VERDICT: {len(mismatches)} metric(s) differ; largest is "
              f"{worst[0]} at {worst[3]:+.3f}%.")
        print("         Run-to-run noise is therefore at least that large, so any "
              "delta smaller than it cannot be called a real difference.")


if __name__ == "__main__":
    main()
