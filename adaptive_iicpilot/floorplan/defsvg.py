"""Parse the parts of a DEF a floorplan picture needs, and render it as SVG.

Drawn: die outline, core area, hard macros with their halos, and I/O pins
colored by pin group. Standard cells are deliberately not drawn — at
floorplan stage they are unplaced, and the picture is about the floorplan.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Optional

from .macros import Rect

_NUM = r"(-?\d+(?:\.\d+)?)"
_POINT_RE = re.compile(r"\(\s*" + _NUM + r"\s+" + _NUM + r"\s*\)")
_ORIENT_SWAPS = {"E", "W", "FE", "FW"}

# Distinguishable on both a light and a dark page; the SVG carries its own
# background, so it does not depend on the host theme.
PALETTE = (
    "#4269d0", "#efb118", "#ff725c", "#6cc5b0", "#3ca951", "#ff8ab7",
    "#a463f2", "#97bbf5", "#9c6b4e", "#9498a0", "#d4a6c8", "#5ab4ac",
)


@dataclass
class DefComponent:
    name: str
    master: str
    x: float
    y: float
    orient: str
    status: str


@dataclass
class DefPin:
    name: str
    direction: str
    x: float
    y: float
    layer: str = ""


@dataclass
class DefLayout:
    dbu: float = 1000.0
    die: Optional[Rect] = None
    rows: list = field(default_factory=list)  # (x, y, n, step_x) in microns
    components: list = field(default_factory=list)
    pins: list = field(default_factory=list)

    def core_from_rows(self, row_height: Optional[float] = None) -> Optional[Rect]:
        if not self.rows:
            return None
        ys = sorted({r[1] for r in self.rows})
        if row_height is None:
            row_height = (ys[1] - ys[0]) if len(ys) > 1 else 0.0
        x0 = min(r[0] for r in self.rows)
        x1 = max(r[0] + r[2] * r[3] for r in self.rows)
        return Rect(x0, ys[0], x1, ys[-1] + row_height)


def _clean(name: str) -> str:
    """DEF escapes brackets and slashes in names; the netlist does not."""
    return name.replace("\\", "")


def _section(text: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s+\d+\s*;(.*?)^\s*END\s+{name}", text, re.S | re.M)
    return match.group(1) if match else ""


def parse_def(text: str, macro_masters: Optional[set] = None) -> DefLayout:
    """Parse a DEF. With macro_masters, only those components are kept —
    an ibex DEF has 14k standard cells nobody wants to draw."""
    layout = DefLayout()
    units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)", text)
    if units:
        layout.dbu = float(units.group(1))
    dbu = layout.dbu

    die = re.search(r"DIEAREA(.*?);", text, re.S)
    if die:
        points = [(float(a) / dbu, float(b) / dbu) for a, b in _POINT_RE.findall(die.group(1))]
        if points:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            layout.die = Rect(min(xs), min(ys), max(xs), max(ys))

    for match in re.finditer(
        r"^\s*ROW\s+\S+\s+\S+\s+" + _NUM + r"\s+" + _NUM + r"\s+\S+\s+DO\s+(\d+)\s+BY\s+\d+\s+STEP\s+" + _NUM,
        text, re.M,
    ):
        x, y, n, step = match.groups()
        layout.rows.append((float(x) / dbu, float(y) / dbu, int(n), float(step) / dbu))

    for entry in _section(text, "COMPONENTS").split(";"):
        entry = entry.strip()
        if not entry.startswith("-"):
            continue
        tokens = entry[1:].split()
        if len(tokens) < 2:
            continue
        name, master = _clean(tokens[0]), tokens[1]
        if macro_masters is not None and master not in macro_masters:
            continue
        placed = re.search(r"\+\s*(PLACED|FIXED|COVER)\s*" + _POINT_RE.pattern + r"\s*(\w+)", entry)
        if not placed:
            continue
        status, x, y, orient = placed.groups()
        layout.components.append(
            DefComponent(name, master, float(x) / dbu, float(y) / dbu, orient, status)
        )

    for entry in _section(text, "PINS").split(";"):
        entry = entry.strip()
        if not entry.startswith("-"):
            continue
        name = _clean(entry[1:].split()[0])
        direction = re.search(r"DIRECTION\s+(\w+)", entry)
        use = re.search(r"USE\s+(\w+)", entry)
        if use and use.group(1).upper() in ("POWER", "GROUND"):
            continue
        placed = re.search(r"\+\s*(?:PLACED|FIXED|COVER)\s*" + _POINT_RE.pattern, entry)
        if not placed:
            continue
        layer = re.search(r"LAYER\s+(\S+)", entry)
        layout.pins.append(
            DefPin(
                name,
                direction.group(1).upper() if direction else "INPUT",
                float(placed.group(1)) / dbu,
                float(placed.group(2)) / dbu,
                layer.group(1) if layer else "",
            )
        )
    return layout


def macro_rects(layout: DefLayout, sizes: dict) -> list:
    """Placement-style (name, master, Rect) for each macro, honouring
    rotation. sizes: master -> (width, height) in microns."""
    out = []
    for comp in layout.components:
        if comp.master not in sizes:
            continue
        w, h = sizes[comp.master]
        if comp.orient in _ORIENT_SWAPS:
            w, h = h, w
        out.append((comp.name, comp.master, Rect(comp.x, comp.y, comp.x + w, comp.y + h)))
    return out


def render_svg(
    layout: DefLayout,
    core: Optional[Rect] = None,
    macro_sizes: Optional[dict] = None,
    halo: tuple = (0.0, 0.0),
    pin_groups: Optional[dict] = None,
    title: str = "",
    subtitle: str = "",
    width_px: int = 560,
) -> str:
    """Self-contained SVG text (no external refs, no script)."""
    die = layout.die or core
    if die is None:
        raise ValueError("DEF has no DIEAREA and no core was given")
    core = core or layout.core_from_rows()
    macro_sizes = macro_sizes or {}
    pin_groups = pin_groups or {}

    pad, header, legend_w = 14.0, 40.0, 150.0
    scale = (width_px - 2 * pad - legend_w) / max(die.width, 1e-9)
    plot_h = die.height * scale
    # A wide die (aspect 0.5) is short; the legend still needs its room.
    legend_h = 14 + len(PALETTE) * 13 + 70
    height_px = int(header + 2 * pad + max(plot_h, legend_h))
    ox, oy = pad, header + pad

    def sx(x: float) -> float:
        return ox + (x - die.x0) * scale

    def sy(y: float) -> float:
        return oy + (die.y1 - y) * scale  # DEF y grows up, SVG y grows down

    def rect(r: Rect, **attrs: str) -> str:
        extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
        return (
            f'<rect x="{sx(r.x0):.2f}" y="{sy(r.y1):.2f}" width="{r.width * scale:.2f}" '
            f'height="{r.height * scale:.2f}" {extra}/>'
        )

    esc = html.escape
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {width_px} {height_px}" font-family="Segoe UI, Helvetica, Arial, sans-serif">',
        f'<rect x="0" y="0" width="{width_px}" height="{height_px}" fill="#fbfbfa"/>',
        f'<text x="{pad}" y="18" font-size="13" font-weight="600" fill="#1d1d1f">{esc(title)}</text>',
        f'<text x="{pad}" y="33" font-size="10.5" fill="#55565a">{esc(subtitle)}</text>',
        rect(die, fill="#ffffff", stroke="#1d1d1f", stroke_width="1.5"),
    ]
    if core is not None:
        parts.append(rect(core, fill="#f1f3f6", stroke="#7a7f88", stroke_width="1", stroke_dasharray="4 3"))

    hx, hy = float(halo[0]), float(halo[1])
    drawn = macro_rects(layout, macro_sizes)
    for name, master, r in drawn:
        if hx or hy:
            parts.append(
                rect(r.grown(hx, hy), fill="none", stroke="#c2410c", stroke_width="0.8",
                     stroke_dasharray="3 2", opacity="0.85")
            )
        parts.append(rect(r, fill="#fde3cf", stroke="#c2410c", stroke_width="1"))
        if r.width * scale > 40 and r.height * scale > 14:
            label = master.replace("fakeram45_", "")
            parts.append(
                f'<text x="{sx(r.x0) + 3:.2f}" y="{sy(r.y1) + 11:.2f}" font-size="9" '
                f'fill="#7c2d12"><title>{esc(name)}</title>{esc(label)}</text>'
            )

    # Pin groups ranked by size, so the legend shows the groups that matter.
    counts: dict[str, int] = {}
    for pin in layout.pins:
        group = pin_groups.get(pin.name, "ungrouped")
        counts[group] = counts.get(group, 0) + 1
    ranked = sorted(counts, key=lambda g: (-counts[g], g))
    shown = ranked[: len(PALETTE) - 1]
    colors = {g: PALETTE[i] for i, g in enumerate(shown)}
    other = PALETTE[-1]

    # Fixed on-screen size: at ibex scale (~1.6 px/um) a physical-size pin
    # would be an invisible dot.
    marker = 5.0
    for pin in layout.pins:
        group = pin_groups.get(pin.name, "ungrouped")
        color = colors.get(group, other)
        parts.append(
            f'<rect x="{sx(pin.x) - marker / 2:.2f}" y="{sy(pin.y) - marker / 2:.2f}" '
            f'width="{marker:.2f}" height="{marker:.2f}" fill="{color}">'
            f"<title>{esc(pin.name)} ({esc(group)}, {pin.direction.lower()})</title></rect>"
        )

    lx = width_px - legend_w + 4
    ly = oy + 4
    parts.append(f'<text x="{lx}" y="{ly}" font-size="10.5" font-weight="600" fill="#1d1d1f">Pin groups</text>')
    rows = [(g, colors[g], counts[g]) for g in shown]
    if len(ranked) > len(shown):
        rows.append((f"{len(ranked) - len(shown)} more", other, sum(counts[g] for g in ranked[len(shown):])))
    for i, (group, color, count) in enumerate(rows):
        y = ly + 14 + i * 13
        parts.append(f'<rect x="{lx}" y="{y - 8}" width="8" height="8" fill="{color}"/>')
        # Keep the tail: bus names share long prefixes (auto_tl_master_xing_...).
        label = group if len(group) <= 17 else "…" + group[-16:]
        parts.append(
            f'<text x="{lx + 12}" y="{y}" font-size="9.5" fill="#33353a">{esc(label)} ({count})</text>'
        )
    key_y = ly + 14 + len(rows) * 13 + 10
    if drawn:
        parts.append(f'<rect x="{lx}" y="{key_y - 8}" width="8" height="8" fill="#fde3cf" stroke="#c2410c"/>')
        parts.append(
            f'<text x="{lx + 12}" y="{key_y}" font-size="9.5" fill="#33353a">macro, dashed = halo</text>'
        )
    parts.append(
        f'<rect x="{lx}" y="{key_y + 5}" width="8" height="8" fill="#f1f3f6" stroke="#7a7f88" stroke-dasharray="2 1"/>'
    )
    parts.append(f'<text x="{lx + 12}" y="{key_y + 13}" font-size="9.5" fill="#33353a">core area</text>')
    parts.append(
        f'<text x="{lx}" y="{key_y + 30}" font-size="9" fill="#55565a">die {die.width:.1f} x {die.height:.1f} um</text>'
    )
    if core is not None:
        parts.append(
            f'<text x="{lx}" y="{key_y + 42}" font-size="9" fill="#55565a">core {core.width:.1f} x {core.height:.1f} um</text>'
        )
    if drawn:
        parts.append(
            f'<text x="{lx}" y="{key_y + 54}" font-size="9" fill="#55565a">{len(drawn)} macros, halo {hx:g} x {hy:g} um</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)
