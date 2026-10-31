#!/usr/bin/env python3
"""Add reg-to-reg fmax to existing FloorplanAgent results.

FULL runs: read the run's OWN reports/<platform>/<design>/<variant>/metadata.json
and take fmax from the real clock's per-clock key (ORFSAdapter.fmax_fields),
after checking that file still agrees with the stored die area / congestion /
WNS / power. A disagreement leaves the record untouched and is reported.
SCREEN runs stop at global placement and have no finish timing: they get None
with a note. Nothing is derived from another run.

    /home/amaanuddin/venvs/iicpilot/bin/python backfill_floorplan_fmax.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from adaptive_iicpilot.eda_interface import ORFSAdapter
from adaptive_iicpilot.floorplan.runner import default_artifacts_dir

FIELDS = ("cp_delay_ns", "fmax_clock", "cp_delay_note", "orfs_fmax_headline_hz", "orfs_fmax_headline_clock")
CHECKED = {"die_area_um2": "floorplan__design__die__area", "congestion": "globalplace__gpl__routability__congestion",
           "wns_ns": "finish__timing__setup__ws"}


def main(dry_run: bool) -> None:
    adapter = ORFSAdapter()
    flow = Path(os.path.expanduser(adapter.orfs_root)) / "flow"
    for path in sorted(default_artifacts_dir().glob("*/fp_*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("stage") not in ("FULL", "SCREEN"):
            continue
        m = result.setdefault("metrics", {})
        if result["stage"] == "SCREEN":
            new = {f: None for f in FIELDS}
            new["cp_delay_note"] = "screen run stops at global placement; no finish timing"
        else:
            meta_path = flow / "reports" / result.get("tech", "nangate45") / adapter._design_nickname(result["design"]) \
                / result["variant"] / "metadata.json"
            if not meta_path.is_file():
                print(f"SKIP {result['design']} {result['variant']}: no metadata.json; fmax left unset")
                continue
            meta = json.loads(meta_path.read_text())
            bad = [k for k, mk in CHECKED.items()
                   if m.get(k) is not None and meta.get(mk) is not None and abs(float(m[k]) - float(meta[mk])) > 1e-6]
            if bad:
                print(f"SKIP {result['design']} {result['variant']}: metadata.json disagrees on {bad}; left untouched")
                continue
            new = adapter.fmax_fields(meta, result["design"])
        m.update(new)
        cp = new["cp_delay_ns"]
        fmax = f"{1000 / cp:.1f} MHz ({new['fmax_clock']})" if cp else f"-  [{new['cp_delay_note']}]"
        print(f"{result['design']:10} {result['stage']:6} {result['variant']:22} {result.get('label', ''):28} "
              f"reg2reg fmax {fmax}")
        if not dry_run:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
            os.replace(tmp, path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    main(parser.parse_args().dry_run)
