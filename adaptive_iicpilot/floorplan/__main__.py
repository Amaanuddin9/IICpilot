"""Run a floorplan study from the shell.

    python -m adaptive_iicpilot.floorplan --design ibex --studies aspect,pins --full per_family
    python -m adaptive_iicpilot.floorplan --design tinyRocket --full per_family

Results land in .floorplan_runs/<design>/<variant>/ (result.json, io.tcl,
macros.tcl, floorplan.def, floorplan.svg) and in floorplan memory, so the chat
tool replays them instantly afterwards.
"""
from __future__ import annotations

import argparse
import json
import sys

from .agent import FULL_MODES, OBJECTIVES, STUDIES, FloorplanAgent, format_table
from .runner import FloorplanRunner


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adaptive_iicpilot.floorplan")
    parser.add_argument("--design", required=True)
    parser.add_argument("--objective", default="balanced", choices=OBJECTIVES)
    parser.add_argument("--studies", default="", help=f"comma list of {', '.join(STUDIES)}")
    parser.add_argument("--full", default="best", choices=FULL_MODES)
    parser.add_argument("--util", default="", help="comma list: repeat every candidate at these utilizations")
    parser.add_argument("--aspects", default="", help="comma list replacing the aspect-ratio sweep")
    parser.add_argument("--list", action="store_true",
                        help="print every finished run of this design from disk, then exit")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--json", dest="json_out", default="", help="also write the report here")
    parser.add_argument("--raw", action="store_true", help="stream raw make output to stderr")
    parser.add_argument("--refresh-proof", action="store_true",
                        help="recompute override proofs for this design's finished runs, then exit")
    args = parser.parse_args(argv)

    def floats(text: str):
        return [float(v) for v in text.split(",") if v.strip()] or None

    if args.list:
        print(list_runs(FloorplanRunner(emit=lambda s: None), args.design))
        return 0

    if args.refresh_proof:
        from .knobs import knobs_from_dict

        runner = FloorplanRunner(emit=print)
        for path in sorted((runner.artifacts / args.design).glob("fp_*/result.json")):
            stored = json.loads(path.read_text(encoding="utf-8"))
            result = runner.refresh_proof(knobs_from_dict(stored["knobs"]), stored["stage"])
            print(f"{stored['stage']:<6} {stored['label']}")
            for line in (result or {}).get("proof", []):
                print(f"    {line}")
        return 0

    def emit(line: str) -> None:
        print(line, flush=True)

    raw = (lambda line: print(line, file=sys.stderr, flush=True)) if args.raw else None
    agent = FloorplanAgent(runner=FloorplanRunner(emit=emit, raw_log=raw), emit=emit, parallel=args.parallel)
    studies = [s.strip() for s in args.studies.split(",") if s.strip()] or None
    report = agent.run(args.design, args.objective, studies, full=args.full,
                       utils=floats(args.util), aspects=floats(args.aspects))
    print(f"scope: {report['scope']}")

    print()
    print(format_table(report["table"]))
    for check in report["screen_vs_full"]:
        source = check.get("full_source", "floorplan full run")
        print(f"screen vs full congestion '{check['label']}': {check['screen_congestion']} vs "
              f"{check['full_congestion']} ({source}) -> {'MATCH' if check['match'] else 'MISMATCH'}")
    if report["best"]:
        print(f"best ({args.objective}): {report['best']['label']}  svg: {report['best_svg_path']}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1, default=str)
    return 0


def list_runs(runner: FloorplanRunner, design: str) -> str:
    """Every finished run of a design, from the result files on disk."""
    from .knobs import knobs_from_dict

    rows = []
    for path in (runner.artifacts / design).glob("fp_*/result.json"):
        r = json.loads(path.read_text(encoding="utf-8"))
        k = knobs_from_dict(r["knobs"])
        m = r.get("metrics") or {}
        util = k.core_utilization if k.core_utilization is not None else runner.stock_util(design)
        rows.append((
            r["stage"], k.aspect_ratio if k.aspect_ratio is not None else 1.0,
            k.core_margin if k.core_margin is not None else "stock", k.io_strategy, util,
            m.get("die_area_um2"), m.get("congestion"), m.get("io_hpwl_um"), m.get("wns_ns"),
            k.macro_mode if k.macro_mode != "none" else "", r["variant"],
        ))
    rows.sort(key=lambda t: (t[4] or 0, t[3], t[1], t[0] != "SCREEN"))

    def f(v, spec):
        return format(v, spec) if isinstance(v, (int, float)) else str(v)

    head = (f"{'kind':<6} {'AR':>5} {'margin':>6} {'pins':<17} {'util':>5} {'die um2':>9} "
            f"{'cong':>8} {'IO HPWL':>9} {'WNS ns':>8}  variant")
    lines = [head, "-" * len(head)]
    for kind, ar, margin, pins, util, die, cong, io, wns, mm, variant in rows:
        lines.append(
            f"{kind:<6} {f(ar, '5.2f')} {f(margin, '>6')} {pins:<17} {f(util, '5.0f')} {f(die, '9.0f')} "
            f"{f(cong, '8.4f')} {f(io, '9.0f')} {f(wns, '8.4f') if wns is not None else '       -'}  {variant}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
