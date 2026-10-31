#!/usr/bin/env python3
"""Tables, charts and comparisons for clock_sweep.py results.

Reads ONLY the proven records in sweep_logs/clock_sweep/results/ (a record whose
proof failed is listed as discarded, never tabulated). Stdlib apart from
matplotlib, which is optional (charts are skipped without it):

    ~/venvs/plots/bin/python clock_sweep_report.py
"""
from __future__ import annotations

import json
from pathlib import Path

from clock_sweep_paths import analyse, setup_paths


def analyse_paths(row: dict) -> list:
    return setup_paths(row["rec"]["design"], row["rec"]["variant"])

REPO = Path(__file__).resolve().parent
RESULTS = REPO / "sweep_logs" / "clock_sweep" / "results"
LEGACY = REPO / ".orfs_cache" / "legacy_unproven"
FIGURES = REPO / "sweep_logs" / "figures"
REPORT = REPO / "sweep_logs" / "clock_sweep_report.md"
CONGESTION_LIMIT = 1.0
# IICPilot paper, Table I, ibex row (nangate45).
PAPER_IBEX = {"cp_delay_ns": 2.2569, "area_um2": 48305.0}
COMPARED = ("wns", "tns", "die", "power_w", "fmax_hz", "congestion", "drc")


def status(wns: float, limit: float) -> str:
    if wns >= 0:
        return "MET"
    return "ACCEPTABLE" if wns >= limit else "VIOLATED"


def row_of(rec: dict) -> dict:
    raw = rec["raw"]
    paths = analyse(rec)
    # cp_delay/fmax are REG-TO-REG ONLY: the real clock's period_min (paths
    # launched and captured by it). ORFS's headline finish__timing__fmax is the
    # max over clocks, which for ibex is the virtual I/O clock (in2out paths).
    fmax = paths["real_clock_fmax_hz"]
    headline = float(raw["fmax_hz"]) if raw.get("fmax_hz") else None
    wns = float(raw["wns"]) if raw.get("wns") is not None else None
    congestion = float(raw["congestion"]) if raw.get("congestion") is not None else None
    return {
        "clock_ns": rec["key"]["clock_ns"],
        "mult": rec["multiplier"],
        "fmax_mhz": fmax / 1e6 if fmax else None,
        "cp_ns": 1e9 / fmax if fmax else None,
        "wns": wns,
        "tns": float(raw["tns"]) if raw.get("tns") is not None else None,
        "limit": rec["timing_limit_ns"],
        "status": status(wns, rec["timing_limit_ns"]) if wns is not None else "NO DATA",
        "congestion": congestion,
        "congested": congestion is not None and congestion > CONGESTION_LIMIT,
        "drc": raw.get("drc"),
        "power_mw": float(raw["power_w"]) * 1000 if raw.get("power_w") is not None else None,
        "die": float(raw["die"]) if raw.get("die") is not None else None,
        "runtime_min": rec["runtime_s"] / 60,
        "flow_ok": rec["flow_ok"],
        "proof": rec["proof"]["ok"],
        "abc_D": rec["proof"].get("abc_D"),
        "headline_fmax_mhz": headline / 1e6 if headline else None,
        "headline_clock": paths["headline_fmax_clock"],
        "real_clock": paths["real_clock"],
        "worst_path": paths["worst_path"],
        "worst_reg2reg": paths["worst_reg2reg_ns"],
        "rec": rec,
    }


def load() -> tuple[dict, dict, list]:
    main, reruns, discarded = {}, {}, []
    for path in sorted(RESULTS.glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not rec["proof"]["ok"]:
            discarded.append(rec)
            continue
        (reruns if rec["rerun"] else main).setdefault(rec["design"], []).append(row_of(rec))
    for rows in list(main.values()) + list(reruns.values()):
        rows.sort(key=lambda r: r["clock_ns"])
    return main, reruns, discarded


def fmt(value, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def design_table(design: str, rows: list) -> list[str]:
    stock = next((r for r in rows if abs(r["mult"] - 1.0) < 1e-9), None)
    util = rows[0]["rec"]["key"]["util"]
    out = [f"### {design} (util {util}, AR {rows[0]['rec']['key']['aspect']:g}, density auto; "
           f"stock clock {rows[0]['rec']['stock_clock_ns']:g} ns; timing limit {rows[0]['limit']:g} ns from rules-base.json)", "",
           f"fmax_mhz and cp_delay_ns are **reg-to-reg only** ({rows[0]['real_clock']} period_min). "
           "WNS / timing_status cover every path group; worst_path_group is the class of the path that sets WNS. "
           "`ORFS fmax` is the raw finish__timing__fmax (max over clocks) and the clock it came from.", "",
           "| clock_ns | x stock | fmax_mhz (reg2reg only) | cp_delay_ns (reg2reg only) | ORFS fmax (clock) | wns_ns | worst_path_group | worst reg2reg slack | timing_status (limit) | congestion | power_mw | power vs stock | die_um2 | runtime | flow rc | SDC proof |",
           "|---:|---:|---:|---:|---|---:|---|---:|---|---:|---:|---:|---:|---:|---|---|"]
    for r in rows:
        pct = (100 * (r["power_mw"] - stock["power_mw"]) / stock["power_mw"]
               if stock and r["power_mw"] is not None and stock["power_mw"] else None)
        cong = fmt(r["congestion"], ".4f") + (" **congested**" if r["congested"] else "")
        out.append(
            f"| {r['clock_ns']:g} | {r['mult']:g} | {fmt(r['fmax_mhz'], '.1f')} | {fmt(r['cp_ns'], '.4f')} | "
            f"{fmt(r['headline_fmax_mhz'], '.1f')} ({r['headline_clock']}) | {fmt(r['wns'], '+.4f')} | "
            f"{r['worst_path']['class']} ({r['worst_path']['path_group']}) | {fmt(r['worst_reg2reg'], '+.4f')} | "
            f"{r['status']} ({r['limit']:g}) | {cong} | {fmt(r['power_mw'], '.3f')} | "
            f"{fmt(pct, '+.1f')}% | {fmt(r['die'], ',.1f')} | {r['runtime_min']:.1f} min | "
            f"{'0' if r['flow_ok'] else 'non-zero'} | OK (ABC -D {', '.join(f'{d:g}' for d in r['abc_D'])}) |"
        )
    return out


def fastest_clean(rows: list) -> dict | None:
    ok = [r for r in rows if r["status"] in ("MET", "ACCEPTABLE") and not r["congested"]]
    return min(ok, key=lambda r: r["clock_ns"]) if ok else None


def legacy_comparison(rows: list) -> list[str]:
    out = []
    for r in rows:
        path = LEGACY / f"{r['rec']['design']}-{r['rec']['cache_key']}.json"
        if not path.is_file():
            continue
        m = json.loads(path.read_text(encoding="utf-8"))["metrics"]
        diffs = {
            "wns": (m.get("wns_ns"), r["wns"]),
            "power_mw": (m.get("power_mw"), r["power_mw"]),
            "die": (m.get("die_area_um2"), r["die"]),
            "congestion": (m.get("congestion"), r["congestion"]),
        }
        same = all(a is not None and b is not None and abs(a - b) < 1e-9 for a, b in diffs.values())
        out.append(f"| {r['clock_ns']:g} | " + " | ".join(
            f"{fmt(a, '.5g')} -> {fmt(b, '.5g')}" for a, b in diffs.values()) + f" | {'identical' if same else 'DIFFERENT'} |")
    if out:
        out = ["| clock_ns | wns (legacy -> proven) | power_mw | die_um2 | congestion | verdict |",
               "|---:|---|---|---|---|---|"] + out
    return out


def determinism(main_rows: list, rerun_rows: list) -> list[str]:
    out = []
    for rr in rerun_rows:
        base = next((r for r in main_rows if abs(r["clock_ns"] - rr["clock_ns"]) < 1e-9), None)
        if not base:
            continue
        a_raw, b_raw = base["rec"]["raw"], rr["rec"]["raw"]
        out += [f"Clock {rr['clock_ns']:g} ns, two fresh proven runs (variants `{base['rec']['variant']}` and `{rr['rec']['variant']}`):", "",
                "| metric | run 1 | run 2 | difference |", "|---|---:|---:|---:|"]
        identical = True
        for k in COMPARED:
            a, b = a_raw.get(k), b_raw.get(k)
            if a is None or b is None:
                out.append(f"| {k} | {a} | {b} | n/a |")
                continue
            d = float(b) - float(a)
            identical &= d == 0
            out.append(f"| {k} | {a} | {b} | {d:+.6g} |")
        a_cp, b_cp = base["rec"]["metrics"].get("cp_delay_ns"), rr["rec"]["metrics"].get("cp_delay_ns")
        if a_cp is not None and b_cp is not None:
            identical &= a_cp == b_cp
            out.append(f"| cp_delay_ns (reg2reg, {base['rec']['metrics'].get('fmax_clock')}) | {a_cp:.6f} | {b_cp:.6f} | {b_cp - a_cp:+.6g} |")
        out += ["", f"Runtime: {base['runtime_min']:.1f} min vs {rr['runtime_min']:.1f} min.", "",
                f"**Verdict: {'bit-identical on every metric' if identical else 'NOT identical - see differences above'}.**", ""]
    return out


def charts(main: dict) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    FIGURES.mkdir(parents=True, exist_ok=True)
    ink, ink2, grid, surface, series = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#2a78d6"
    status_style = {  # shape + color + legend label: never color alone
        "MET": ("o", "#0ca30c"), "ACCEPTABLE": ("s", "#fab219"), "VIOLATED": ("X", "#d03b3b")}
    written = []
    for metric, ylabel, fname in (("power_mw", "Total power (mW)", "power_vs_clock.png"),
                                  ("fmax_mhz", "fmax, reg-to-reg only (MHz)", "fmax_vs_clock.png")):
        designs = [d for d in ("ibex", "aes", "gcd") if d in main]
        fig, axes = plt.subplots(1, len(designs), figsize=(4.6 * len(designs), 4.0), facecolor=surface, squeeze=False)
        for ax, design in zip(axes[0], designs):
            rows = [r for r in main[design] if r[metric] is not None]
            xs, ys = [r["clock_ns"] for r in rows], [r[metric] for r in rows]
            ax.set_facecolor(surface)
            ax.plot(xs, ys, color=series, linewidth=2, zorder=2)
            for r in rows:
                shape, color = status_style.get(r["status"], ("o", ink2))
                ax.scatter(r["clock_ns"], r[metric], marker=shape, s=110 if shape == "X" else 70, color=color,
                           edgecolors=surface, linewidths=2, zorder=3)
                if r["congested"]:
                    ax.annotate("congested", (r["clock_ns"], r[metric]), textcoords="offset points",
                                xytext=(0, 9), ha="center", fontsize=8, color=ink2)
            stock = rows[0]["rec"]["stock_clock_ns"] if rows else None
            if stock:
                ax.axvline(stock, color=ink2, linestyle=(0, (3, 3)), linewidth=1, zorder=1)
                ax.annotate("stock", (stock, 0), xycoords=("data", "axes fraction"), xytext=(3, 4),
                            textcoords="offset points", fontsize=8, color=ink2)
            spread = 100 * (max(ys) - min(ys)) / min(ys) if ys else 0
            ax.set_title(f"{design}  (range {min(ys):.4g}-{max(ys):.4g}, {spread:.1f}% spread)" if ys else design,
                         color=ink, fontsize=10, loc="left")
            ax.set_xlabel("Clock period (ns)", color=ink2, fontsize=9)
            ax.set_ylabel(ylabel, color=ink2, fontsize=9)
            ax.tick_params(colors=ink2, labelsize=8)
            ax.grid(True, color=grid, linewidth=0.8)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color(grid)
        handles = [plt.Line2D([], [], marker=s, color="none", markerfacecolor=c, markersize=8, label=k)
                   for k, (s, c) in status_style.items()]
        fig.legend(handles=handles, loc="upper right", ncol=3, frameon=False, fontsize=8, labelcolor=ink2)
        fig.suptitle(f"{ylabel.split(' (')[0]} vs clock period - measured, real ORFS (nangate45)",
                     x=0.01, ha="left", color=ink, fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.93))
        path = FIGURES / fname
        fig.savefig(path, dpi=150, facecolor=surface)
        plt.close(fig)
        written.append(path)
    return written


def main() -> None:
    main_rows, reruns, discarded = load()
    lines = ["# Clock-period sweeps - real ORFS, nangate45", "",
             "Every row is a measured run whose period was proven from its own synthesis log (ABC `-D`) and "
             "6_final.sdc; floorplan knobs were proven from 2_1_floorplan.log. No estimates.", ""]
    for design in ("ibex", "aes", "gcd"):
        if design not in main_rows:
            continue
        rows = main_rows[design]
        lines += design_table(design, rows) + [""]
        classes = sorted({r["worst_path"]["class"] for r in rows})
        io_rows = [f"{r['clock_ns']:g} ns" for r in rows if r["worst_path"]["class"] != "reg2reg"]
        virtual = sorted({r["headline_clock"] for r in rows if r["headline_clock"] != r["real_clock"]})
        lines.append(f"**Where {design}'s timing status comes from:** WNS (finish__timing__setup__ws, all path groups) "
                     f"vs the design's ORFS limit; the WNS path is {' / '.join(classes)}"
                     + (f" (non-reg2reg at {', '.join(io_rows)})" if io_rows else " at every clock")
                     + ". fmax/cp_delay columns are reg2reg only."
                     + (f" ORFS's headline fmax comes from the virtual I/O clock {', '.join(virtual)} (in2out paths), "
                        "so it is NOT used for cp_delay." if virtual else " ORFS's headline fmax is the same real clock."))
        near = []
        for r in rows:
            worst = r["worst_path"]
            for p in analyse_paths(r):
                if p["class"] != worst["class"] and p["slack_ns"] is not None and p["slack_ns"] - worst["slack_ns"] <= 0.011:
                    near.append(f"{r['clock_ns']:g} ns: {p['class']} {p['slack_ns']:+.2f} vs {worst['class']} {worst['slack_ns']:+.2f}")
        if near:
            lines.append("Near-tie (within 0.01 ns at the report's 2-decimal resolution, so the class of the WNS path is "
                         "not robust): " + "; ".join(near) + ".")
        lines.append("")
        best = fastest_clean(rows)
        lines.append(f"**Fastest clock that is not VIOLATED and not congested:** "
                     + (f"{best['clock_ns']:g} ns ({best['status']}, wns {best['wns']:+.4f} ns, fmax "
                        f"{fmt(best['fmax_mhz'], '.1f')} MHz, power {best['power_mw']:.3f} mW)" if best else "none in this sweep"))
        lines.append("")
        if design in reruns:
            lines += ["#### Determinism check", ""] + determinism(rows, reruns[design])
        legacy = legacy_comparison(rows)
        if legacy:
            lines += ["#### Proven runs vs the earlier unproven cache entries", ""] + legacy + [""]
    ibex28 = next((r for r in main_rows.get("ibex", []) if abs(r["clock_ns"] - 2.8) < 1e-9), None)
    if ibex28:
        lines += ["### ibex @ 2.8 ns vs IICPilot paper (different OpenROAD versions, not a direct benchmark)", "",
                  "| | cp_delay_ns | area_um2 |", "|---|---:|---:|",
                  f"| IICPilot paper (ibex row) | {PAPER_IBEX['cp_delay_ns']} | {PAPER_IBEX['area_um2']:,.0f} |",
                  f"| this run (util 80, 2.8 ns) | {fmt(ibex28['cp_ns'], '.4f')} | {fmt(ibex28['die'], ',.1f')} (die) / "
                  f"{fmt(ibex28['rec']['metrics']['area_um2'] if ibex28['rec']['metrics'] else None, ',.1f')} (cell) |", "",
                  "The paper does not say whether its area is die or cell area, so both are shown.", ""]
    if discarded:
        lines += ["### Discarded runs (proof failed)", ""] + [
            f"- {d['design']} {d['key']['clock_ns']:g} ns: {'; '.join(d['proof']['problems'])}" for d in discarded] + [""]
    figs = charts(main_rows)
    if figs:
        lines += ["Charts: " + ", ".join(str(p.relative_to(REPO)) for p in figs), ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
