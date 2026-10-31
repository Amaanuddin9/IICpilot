#!/usr/bin/env python3
"""Classify each proven run's worst setup paths from its finish report.

Read-only: parses reports/<platform>/<nickname>/<variant>/6_finish.rpt and
metadata.json of runs that already finished; never starts a flow.

OpenSTA groups paths by CAPTURE clock, so a path group name does not say
reg2reg vs in2reg. Each path is classified from its startpoint/endpoint
descriptions instead:
  start: input port -> in, latch/flip-flop -> reg
  end:   output port -> out, flip-flop -> reg, clock gating-check -> clock gating,
         recovery/removal check -> asynchronous (reset)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from adaptive_iicpilot.eda_interface import real_clock_name

FLOW = Path.home() / "OpenROAD-flow-scripts" / "flow"
NICK = {"ibex": "ibex", "aes": "aes", "gcd": "gcd"}

_SECTION_RE = re.compile(r"^=+\n(?:finish )?(.+?)\n-+\n(.*?)(?=^=+\n|\Z)", re.MULTILINE | re.DOTALL)
_SLACK_RE = re.compile(r"^\s*(-?[0-9.]+)\s+slack \((MET|VIOLATED)\)", re.MULTILINE)


def _point(block: str, label: str) -> tuple[str, str]:
    """Name and its parenthesised description (which may wrap to the next line)."""
    m = re.search(rf"^{label}: (\S+)\s*(?:\((.*?)\))?\s*$\n?(?:\s+\((.*?)\))?", block, re.MULTILINE)
    if not m:
        return "?", ""
    return m.group(1), (m.group(2) or m.group(3) or "")


def classify(start_desc: str, end_desc: str) -> str:
    if "gating" in end_desc:
        return "clock gating"
    if "recovery" in end_desc or "removal" in end_desc:
        return "asynchronous"
    src = "in" if "input port" in start_desc else "reg"
    dst = "out" if "output port" in end_desc else "reg"
    return f"{src}2{dst}"


def setup_paths(design: str, variant: str) -> list[dict]:
    rpt = FLOW / "reports" / "nangate45" / NICK[design] / variant / "6_finish.rpt"
    text = rpt.read_text(errors="replace")
    sections = {title.strip(): body for title, body in _SECTION_RE.findall(text)}
    body = sections.get("report_checks -path_delay max", "")
    paths = []
    for block in re.split(r"(?m)^(?=Startpoint:)", body):
        if not block.startswith("Startpoint:"):
            continue
        start, sdesc = _point(block, "Startpoint")
        end, edesc = _point(block, "Endpoint")
        group = re.search(r"^Path Group: (.+)$", block, re.MULTILINE)
        slack = _SLACK_RE.search(block)
        paths.append({
            "startpoint": start, "start_desc": sdesc, "endpoint": end, "end_desc": edesc,
            "path_group": group.group(1).strip() if group else "?",
            "class": classify(sdesc, edesc),
            "slack_ns": float(slack.group(1)) if slack else None,
        })
    return paths


def analyse(record: dict) -> dict:
    design, variant, clock = record["design"], record["variant"], record["key"]["clock_ns"]
    meta = json.loads((FLOW / "reports" / "nangate45" / NICK[design] / variant / "metadata.json").read_text())
    paths = [p for p in setup_paths(design, variant) if p["slack_ns"] is not None]
    worst = min(paths, key=lambda p: p["slack_ns"]) if paths else None
    reg2reg = [p for p in paths if p["class"] == "reg2reg"]
    per_clock = {k.split("__clock__", 1)[1]: v for k, v in meta.items()
                 if k.startswith("finish__timing__fmax__clock__")}
    # The real clock is the SDC's create_clock with a source object (not a
    # naming heuristic); a virtual clock is never used for fmax.
    sdc = FLOW / "designs" / "nangate45" / NICK[design] / "constraint.sdc"
    real_clock = real_clock_name(sdc.read_text())
    real_fmax = per_clock.get(real_clock) if real_clock else None
    headline = meta.get("finish__timing__fmax")
    return {
        "wns_ns": meta.get("finish__timing__setup__ws"),
        "worst_path": worst,
        "worst_reg2reg_report_ns": min((p["slack_ns"] for p in reg2reg), default=None),
        "real_clock": real_clock,
        "real_clock_fmax_hz": real_fmax,
        # period_min of the real clock covers paths launched AND captured by it,
        # i.e. reg2reg; clock - that period is the worst reg2reg slack at full precision.
        "worst_reg2reg_ns": (clock - 1e9 / real_fmax) if real_fmax else None,
        "headline_fmax_hz": headline,
        "headline_fmax_clock": next((c for c, f in per_clock.items() if f == headline), None),
        "per_clock_fmax_hz": per_clock,
    }


if __name__ == "__main__":
    results = Path(__file__).resolve().parent / "sweep_logs" / "clock_sweep" / "results"
    for path in sorted(results.glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not rec["proof"]["ok"]:
            continue
        a = analyse(rec)
        w = a["worst_path"]
        print(f"{rec['design']:4} {rec['key']['clock_ns']:<6g}{' r2' if rec['rerun'] else '   '} "
              f"WNS {a['wns_ns']:+.5f} | worst: {w['class']:<12} group={w['path_group']:<16} slack {w['slack_ns']:+.2f} "
              f"{w['startpoint']} -> {w['endpoint']} | worst reg2reg {a['worst_reg2reg_ns']:+.5f} "
              f"(report {a['worst_reg2reg_report_ns']}) | headline fmax {a['headline_fmax_hz'] / 1e6:.1f} MHz from "
              f"{a['headline_fmax_clock']}; {a['real_clock']} {a['real_clock_fmax_hz'] / 1e6:.1f} MHz")
        for p in setup_paths(rec["design"], rec["variant"]):
            print(f"       {p['class']:<12} group={p['path_group']:<16} slack {p['slack_ns']:+.2f}  "
                  f"{p['startpoint']} -> {p['endpoint']}")
