"""Presentation renders of finished floorplan runs, from OpenROAD's own GUI.

    python3 -m adaptive_iicpilot.floorplan.render run   <variant_dir> [--window W H]
    python3 -m adaptive_iicpilot.floorplan.render compare <out_prefix> <variant_dir>...

Runs inside WSL with a Python that has Pillow (the system python3 does; the
project venv does not need it). OpenROAD renders headless
(QT_QPA_PLATFORM=offscreen) through save_image with a FIXED area and
resolution, so every image of every run is at the same microns-per-pixel and
the same canvas: panels are directly comparable side by side.

Views (display options only; layouts are never edited):
  floorplan   2_floorplan.odb: PDN straps, die/core outlines, rows dimmed,
              metal1 follow-pin rails hidden, I/O pins as large markers
              colored by pin group (positions from 3_2_place_iop, where
              place_pins honoured the constraints)
  placement   3_place.odb: placed standard cells, power grid dimmed
  congestion  5_1_grt.odb: global-route GCell congestion heat map over the
              placement, fixed 0-100% scale for every run

The GUI's Tcl API toggles visibility but cannot set colors or opacity, so
"dimmed" is done by rendering the layer on its own and compositing it at
reduced opacity. Legend swatches are sampled from those same renders, so they
match what OpenROAD actually drew.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - reported at the CLI
    Image = ImageDraw = ImageFont = None

ORFS_ROOT = Path(os.environ.get("ORFS_ROOT", Path.home() / "OpenROAD-flow-scripts"))
RES_UM_PER_PX = 0.25
CAPTION_H = 226
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
PIN_PALETTE = (
    (66, 105, 208), (239, 177, 24), (255, 114, 92), (108, 197, 176), (60, 169, 81),
    (255, 138, 183), (164, 99, 242), (151, 187, 245), (156, 107, 78), (148, 152, 160),
    (212, 166, 200), (90, 180, 172),
)
DIM_PDN = 0.35
DIM_ROWS = 0.22


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


# --- OpenROAD side ---

TCL_HEAD = """
read_db $::env(FP_ODB)
gui::clear_highlights -1
gui::clear_selections
proc base_view {} {
  gui::set_display_controls "*" visible false
  gui::set_display_controls "Layers/*" visible true
  gui::set_display_controls "Layers/metal1" visible false
  gui::set_display_controls "Nets/*" visible true
  gui::set_display_controls "Instances/*" visible true
  gui::set_display_controls "Shape Types/*" visible true
  gui::set_display_controls "Misc/Instances/*" visible false
  # OpenROAD's own I/O markers: replaced by the group-colored overlay.
  gui::set_display_controls "Shape Types/Pins" visible false
}
proc only_layer {layer} {
  gui::set_display_controls "*" visible false
  gui::set_display_controls "Layers/$layer" visible true
  gui::set_display_controls "Nets/*" visible true
  gui::set_display_controls "Shape Types/*" visible true
}
proc shot {name} {
  save_image -area $::env(FP_AREA) -resolution $::env(FP_RES) $::env(FP_OUT)/$name
}
"""

TCL_FLOORPLAN = TCL_HEAD + """
base_view
shot fp_base.png
gui::set_display_controls "*" visible false
gui::set_display_controls "Rows/*" visible true
shot fp_rows.png
only_layer metal4
shot sw_metal4.png
only_layer metal7
shot sw_metal7.png
exit
"""

TCL_PLACEMENT = TCL_HEAD + """
base_view
gui::set_display_controls "Nets/Power" visible false
gui::set_display_controls "Nets/Ground" visible false
gui::set_display_controls "Shape Types/Routing/*" visible false
shot pl_cells.png
gui::set_display_controls "*" visible false
gui::set_display_controls "Layers/*" visible true
gui::set_display_controls "Layers/metal1" visible false
gui::set_display_controls "Nets/Power" visible true
gui::set_display_controls "Nets/Ground" visible true
gui::set_display_controls "Shape Types/*" visible true
shot pl_pdn.png
exit
"""

TCL_CONGESTION = TCL_HEAD + """
base_view
gui::set_display_controls "Nets/*" visible false
gui::set_display_controls "Instances/Physical/*" visible false
gui::set_display_controls "Heat Maps/Routing Congestion" visible true
gui::set_heatmap Routing DisplayMin 0
gui::set_heatmap Routing DisplayMax 100
gui::set_heatmap Routing ShowLegend 1
gui::set_heatmap Routing rebuild
shot cg_heat.png
gui::dump_heatmap Routing $::env(FP_OUT)/cg_heat.csv
exit
"""


def _openroad(tcl: str, odb: Path, out: Path, area: tuple, res: float) -> None:
    script = out / "_render.tcl"
    # In -gui mode a Tcl error leaves the window open, waiting forever: catch
    # everything and exit non-zero instead.
    body = tcl.replace("\nexit\n", "\n")
    script.write_text(
        "if {[catch {\n" + body + "\n} err]} {\n  puts \"render error: $err\"\n  exit 1\n}\nexit\n",
        encoding="utf-8",
    )
    env = (
        f"FP_ODB={odb} FP_OUT={out} FP_RES={res} "
        f"FP_AREA='{area[0]:g} {area[1]:g} {area[2]:g} {area[3]:g}' QT_QPA_PLATFORM=offscreen"
    )
    cmd = f"cd {ORFS_ROOT} && source env.sh >/dev/null 2>&1 && {env} openroad -gui -no_splash {script}"
    proc = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=600,
                          stdin=subprocess.DEVNULL)
    # The GUI build can exit 0 after a caught error, so check the marker too.
    if proc.returncode != 0 or "render error:" in proc.stdout + proc.stderr:
        raise RuntimeError(f"openroad render of {odb.name} failed:\n{(proc.stdout + proc.stderr)[-1500:]}")


# --- compositing ---

class Frame:
    """Maps microns to pixels for a fixed save_image area/resolution."""

    def __init__(self, area: tuple, res: float, size: tuple):
        self.x0, self.y0, self.x1, self.y1 = area
        self.res = res
        self.w, self.h = size

    def px(self, x: float, y: float) -> tuple:
        return ((x - self.x0) / self.res, (self.y1 - y) / self.res)

    def rect(self, r: list) -> tuple:
        ax, ay = self.px(r[0], r[3])
        bx, by = self.px(r[2], r[1])
        return (ax, ay, bx, by)


def _overlay(base: "Image.Image", layer: "Image.Image", opacity: float) -> "Image.Image":
    """Paste the non-background pixels of `layer` onto `base` at `opacity`."""
    base = base.convert("RGB")
    layer = layer.convert("RGB")
    mask = layer.convert("L").point(lambda v: int(255 * opacity) if v > 12 else 0)
    return Image.composite(layer, base, mask)


def _swatch_color(path: Path) -> tuple:
    """Most common non-black color of a single-layer render."""
    im = Image.open(path).convert("RGB")
    colors = im.getcolors(maxcolors=1 << 20) or []
    colors = [(n, c) for n, c in colors if sum(c) > 60]
    return max(colors)[1] if colors else (200, 200, 200)


def _dashed_rect(draw, box, color, dash=10, gap=6, width=2):
    x0, y0, x1, y1 = box
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        length = max(abs(bx - ax), abs(by - ay))
        steps = int(length // (dash + gap)) + 1
        for i in range(steps):
            s = i * (dash + gap) / length if length else 0
            e = min(1.0, (i * (dash + gap) + dash) / length) if length else 0
            draw.line((ax + (bx - ax) * s, ay + (by - ay) * s, ax + (bx - ax) * e, ay + (by - ay) * e),
                      fill=color, width=width)


def _group_colors(result: dict, pins: list) -> dict:
    groups = result.get("pin_groups") or {}
    counts: dict = {}
    for pin in pins:
        g = groups.get(pin["name"], "other")
        counts[g] = counts.get(g, 0) + 1
    ranked = sorted(counts, key=lambda g: (-counts[g], g))
    return {g: PIN_PALETTE[i % len(PIN_PALETTE)] for i, g in enumerate(ranked)}, counts


def _draw_pins(im, frame: Frame, result: dict, pins: list, die: list, size: int = 11):
    """Pins as large squares straddling the die edge, colored by group."""
    draw = ImageDraw.Draw(im)
    colors, _ = _group_colors(result, pins)
    groups = result.get("pin_groups") or {}
    for pin in pins:
        x0, y0, x1, y1 = pin["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        px, py = frame.px(cx, cy)
        # Push the marker half outside the die so it reads as an edge pin.
        dl, dr, db, dt = cx - die[0], die[2] - cx, cy - die[1], die[3] - cy
        side = min((dl, "l"), (dr, "r"), (db, "b"), (dt, "t"))[1]
        off = size / 2 - 1
        px += {"l": -off, "r": off}.get(side, 0)
        py += {"b": off, "t": -off}.get(side, 0)
        color = colors.get(groups.get(pin["name"], "other"), (220, 220, 220))
        draw.rectangle((px - size / 2, py - size / 2, px + size / 2, py + size / 2),
                       fill=color, outline=(0, 0, 0))


def _scale_bar(im, frame: Frame, length_um: float = 50.0):
    draw = ImageDraw.Draw(im)
    x, y = 16, 18
    w = length_um / frame.res
    draw.line((x, y, x + w, y), fill=(235, 235, 235), width=3)
    for t in (x, x + w):
        draw.line((t, y - 6, t, y + 6), fill=(235, 235, 235), width=2)
    draw.text((x + w + 8, y - 9), f"{length_um:g} um  ({frame.res:g} um/px)", fill=(235, 235, 235), font=_font(15))


def _timing(m: dict) -> str:
    wns, limit = m.get("wns_ns"), m.get("timing_threshold_ns")
    if wns is None:
        return "WNS n/a (screen run)"
    status = "MET" if wns >= 0 else ("ACCEPTABLE" if limit is not None and wns >= limit else "VIOLATED")
    lim = f", limit {limit:+.3f}" if limit is not None else ""
    cp = m.get("cp_delay_ns")
    fmax = f"    reg-to-reg fmax {1000.0 / cp:.1f} MHz" if cp else ""
    return f"WNS {wns:+.4f} ns {status}{lim}{fmax}"


def _knob_text(result: dict) -> tuple:
    k = result["knobs"]
    util = result.get("util_text") or (f"util {k['core_utilization']:g}%" if k.get("core_utilization") else "util stock")
    ar = k.get("aspect_ratio")
    return util, f"AR {ar:g}" if ar is not None else "AR 1 (stock)"


def _caption(width: int, result: dict, die: list, core: list, view: str, legend: list, extra: str = "") -> "Image.Image":
    m = result.get("metrics") or {}
    strip = Image.new("RGB", (width, CAPTION_H), (18, 18, 20))
    d = ImageDraw.Draw(strip)
    util, ar = _knob_text(result)
    dw, dh = die[2] - die[0], die[3] - die[1]
    cw, ch = core[2] - core[0], core[3] - core[1]
    io = result["knobs"].get("io_strategy", "default")
    d.text((16, 10), f"{result['design']}  |  {util}  |  {ar}  |  pins: {io}  |  {view}",
           fill=(245, 245, 245), font=_font(22, bold=True))
    d.text((16, 44),
           f"die {dw:.1f} x {dh:.1f} um = {m.get('die_area_um2') or dw * dh:,.0f} um2    "
           f"core {cw:.1f} x {ch:.1f} um",
           fill=(220, 220, 220), font=_font(17))
    power = m.get("power_mw")
    d.text((16, 70),
           f"congestion {m.get('congestion'):.4f} (global place; <= 1.0 clean)    {_timing(m)}"
           + (f"    power {power:.2f} mW" if power else ""),
           fill=(220, 220, 220), font=_font(17))
    if extra:
        d.text((16, 96), extra, fill=(200, 200, 200), font=_font(15))
    x, y = 16, 128
    for label, color, kind in legend:
        if x > width - 220:
            x, y = 16, y + 24
        if kind == "line":
            d.line((x, y + 8, x + 22, y + 8), fill=color, width=4)
        elif kind == "outline":
            d.rectangle((x, y + 1, x + 15, y + 15), outline=color, width=2)
        elif kind == "dash":
            _dashed_rect(d, (x, y + 1, x + 22, y + 15), color, dash=4, gap=3, width=2)
        else:
            d.rectangle((x, y + 1, x + 15, y + 15), fill=color, outline=(0, 0, 0))
        d.text((x + 28, y), label, fill=(225, 225, 225), font=_font(15))
        x += 28 + int(d.textlength(label, font=_font(15))) + 22
    d.text((width - 16 - d.textlength(result["variant"], font=_font(13)), 12), result["variant"],
           fill=(130, 130, 135), font=_font(13))
    return strip


def _stack(image, caption):
    out = Image.new("RGB", (image.width, image.height + caption.height), (0, 0, 0))
    out.paste(image, (0, 0))
    out.paste(caption, (0, image.height))
    return out


def render_run(run_dir: Path, window: tuple, res: float = RES_UM_PER_PX, views=("floorplan", "placement", "congestion")) -> dict:
    """Render the views of one finished FULL run into <run_dir>/render/."""
    # Absolute: OpenROAD is launched from inside the ORFS tree.
    run_dir = Path(run_dir).resolve()
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    fp = json.loads((run_dir / "2_floorplan.probe.json").read_text(encoding="utf-8"))
    iop = run_dir / "3_2_place_iop.probe.json"
    pins = json.loads(iop.read_text(encoding="utf-8"))["pins"] if iop.is_file() else []
    pins = [p for p in pins if p.get("sig_type", "SIGNAL") not in ("POWER", "GROUND")]
    die, core = fp["die"], fp["core"]
    area = (0.0, 0.0, float(window[0]), float(window[1]))
    results = ORFS_ROOT / "flow" / "results" / result["tech"] / result["design"] / result["variant"]
    out = run_dir / "render"
    out.mkdir(exist_ok=True)
    made = {}

    if "floorplan" in views:
        _openroad(TCL_FLOORPLAN, results / "2_floorplan.odb", out, area, res)
        base = Image.open(out / "fp_base.png")
        frame = Frame(area, res, base.size)
        im = _overlay(base, Image.open(out / "fp_rows.png"), DIM_ROWS)
        draw = ImageDraw.Draw(im)
        draw.rectangle(frame.rect(die), outline=(235, 235, 235), width=2)
        _dashed_rect(draw, frame.rect(core), (250, 250, 250))
        _draw_pins(im, frame, result, pins, die)
        _scale_bar(im, frame)
        m4, m7 = _swatch_color(out / "sw_metal4.png"), _swatch_color(out / "sw_metal7.png")
        colors, counts = _group_colors(result, pins)
        legend = [("metal4 PDN straps", m4, "line"), ("metal7 PDN straps", m7, "line"),
                  ("rows (dimmed)", (110, 110, 120), "line"), ("die", (235, 235, 235), "line"),
                  ("core", (250, 250, 250), "dash")]
        legend += [(f"{g} ({counts[g]})", c, "box") for g, c in colors.items()]
        cap = _caption(im.width, result, die, core, "floorplan", legend,
                       "metal1 follow-pin rails hidden; pins colored by group (positions from 3_2_place_iop)")
        made["floorplan"] = out / "floorplan.png"
        _stack(im, cap).save(made["floorplan"])

    if "placement" in views:
        _openroad(TCL_PLACEMENT, results / "3_place.odb", out, area, res)
        cells = Image.open(out / "pl_cells.png")
        frame = Frame(area, res, cells.size)
        im = _overlay(cells, Image.open(out / "pl_pdn.png"), DIM_PDN)
        draw = ImageDraw.Draw(im)
        draw.rectangle(frame.rect(die), outline=(235, 235, 235), width=2)
        _draw_pins(im, frame, result, pins, die, size=9)
        _scale_bar(im, frame)
        m = result.get("metrics") or {}
        legend = [("standard cells (3_place, outlined)", (150, 150, 150), "outline"),
                  ("power grid, dimmed 35%", (150, 150, 110), "line"),
                  ("I/O pins by group", (239, 177, 24), "box"), ("die", (235, 235, 235), "line")]
        util = m.get("stdcell_util")
        cap = _caption(im.width, result, die, core, "placement", legend,
                       f"detailed placement; core cell utilization {util * 100:.1f}%" if util else "detailed placement")
        made["placement"] = out / "placement.png"
        _stack(im, cap).save(made["placement"])

    if "congestion" in views:
        _openroad(TCL_CONGESTION, results / "5_1_grt.odb", out, area, res)
        im = Image.open(out / "cg_heat.png").convert("RGB")
        frame = Frame(area, res, im.size)
        draw = ImageDraw.Draw(im)
        draw.rectangle(frame.rect(die), outline=(235, 235, 235), width=2)
        _scale_bar(im, frame)
        stats = heatmap_stats(out / "cg_heat.csv")
        result["grt_heatmap"] = stats
        legend = [("GCell congestion heat map (global route), fixed 0-100% scale", (220, 60, 40), "box"),
                  ("die", (235, 235, 235), "line")]
        extra = (f"global-route GCells: max {stats['max']:.0f}%, mean {stats['mean']:.1f}%, "
                 f">=80%: {stats['ge80_pct']:.2f}% of GCells, >=100%: {stats['ge100']} GCells"
                 if stats else "global-route heat map")
        cap = _caption(im.width, result, die, core, "routing congestion", legend, extra)
        made["congestion"] = out / "congestion.png"
        _stack(im, cap).save(made["congestion"])

    (out / "render.json").write_text(json.dumps({k: str(v) for k, v in made.items()} | {
        "window_um": list(window), "um_per_px": res, "grt_heatmap": result.get("grt_heatmap")}, indent=1))
    return made


def heatmap_stats(path: Path) -> Optional[dict]:
    """Summary of dump_heatmap's CSV (one row per GCell, value in %)."""
    if not path.is_file():
        return None
    values = []
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        col = -1
        if header:
            lowered = [h.strip().lower() for h in header]
            col = lowered.index("value") if "value" in lowered else len(header) - 1
        for row in reader:
            try:
                values.append(float(row[col]))
            except (ValueError, IndexError):
                continue
    if not values:
        return None
    n = len(values)
    return {
        "gcells": n, "max": max(values), "mean": sum(values) / n,
        "ge80_pct": 100.0 * sum(v >= 80 for v in values) / n, "ge100": sum(v >= 100 for v in values),
    }


def compare(view: str, run_dirs: list, out_path: Path, title: str) -> Path:
    """Panels side by side; every panel shares the same window and scale."""
    panels = [Image.open(Path(d) / "render" / f"{view}.png").convert("RGB") for d in run_dirs]
    gap, head = 18, 64
    w = sum(p.width for p in panels) + gap * (len(panels) + 1)
    h = max(p.height for p in panels) + head + gap
    sheet = Image.new("RGB", (w, h), (8, 8, 10))
    d = ImageDraw.Draw(sheet)
    d.text((gap, 16), title, fill=(245, 245, 245), font=_font(28, bold=True))
    x = gap
    for p in panels:
        sheet.paste(p, (x, head))
        x += p.width + gap
    sheet.save(out_path)
    return out_path


def main(argv=None) -> int:
    if Image is None:
        print("Pillow is required: run this with a python3 that has PIL (WSL system python3 does)")
        return 2
    parser = argparse.ArgumentParser(prog="python3 -m adaptive_iicpilot.floorplan.render")
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("run")
    one.add_argument("run_dir")
    one.add_argument("--window", nargs=2, type=float, required=True, metavar=("W_UM", "H_UM"))
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("out_prefix")
    cmp_.add_argument("run_dirs", nargs="+")
    cmp_.add_argument("--window", nargs=2, type=float, default=None, metavar=("W_UM", "H_UM"))
    cmp_.add_argument("--title", default="")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        for view, path in render_run(Path(args.run_dir), tuple(args.window)).items():
            print(view, path)
        return 0

    dirs = [Path(d) for d in args.run_dirs]
    window = args.window
    if window is None:  # smallest window holding every die, plus a margin
        dies = [json.loads((d / "2_floorplan.probe.json").read_text())["die"] for d in dirs]
        window = (max(x[2] for x in dies) + 3, max(x[3] for x in dies) + 3)
    for d in dirs:
        print("rendering", d.name, flush=True)
        render_run(d, tuple(window))
    title = args.title or "same scale"
    for view in ("placement", "congestion", "floorplan"):
        path = compare(view, dirs, Path(f"{args.out_prefix}_{view}.png"),
                       f"{title} - {view} ({window[0]:g} x {window[1]:g} um window, {RES_UM_PER_PX:g} um/px)")
        print(view, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
