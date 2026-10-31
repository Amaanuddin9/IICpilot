"""Manual macro placement: strategies, legality, and the Tcl ORFS sources.

Legality is checked BEFORE a flow runs (on the generated placement against
the real core area read from 2_1_floorplan.odb) and again AFTER it (on the
macros as they ended up in the DEF, since place_macro snaps to the track
grid). Rules:
  1. every macro lies inside the core
  2. no two macros overlap
  3. halos are respected: no macro intrudes on another macro's halo, i.e.
     the gap between two macros is at least the halo on that axis
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

EPS = 1e-6


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def contains(self, other: "Rect") -> bool:
        return (
            other.x0 >= self.x0 - EPS and other.y0 >= self.y0 - EPS
            and other.x1 <= self.x1 + EPS and other.y1 <= self.y1 + EPS
        )

    def intersects(self, other: "Rect") -> bool:
        """Positive-area overlap; touching edges do not count."""
        return (
            self.x0 < other.x1 - EPS and other.x0 < self.x1 - EPS
            and self.y0 < other.y1 - EPS and other.y0 < self.y1 - EPS
        )

    def grown(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)


@dataclass(frozen=True)
class Macro:
    name: str
    master: str
    width: float
    height: float


@dataclass(frozen=True)
class Placement:
    name: str
    master: str
    x: float
    y: float
    width: float
    height: float
    orient: str = "R0"

    @property
    def rect(self) -> Rect:
        return Rect(self.x, self.y, self.x + self.width, self.y + self.height)


def check_legality(
    placements: Iterable[Placement], core: Rect, halo: tuple = (0.0, 0.0)
) -> list[str]:
    """Return every violation as a sentence; empty means legal."""
    placed = list(placements)
    hx, hy = float(halo[0]), float(halo[1])
    problems: list[str] = []
    for p in placed:
        if not core.contains(p.rect):
            problems.append(
                f"{p.name} at ({p.x:.2f},{p.y:.2f})-({p.rect.x1:.2f},{p.rect.y1:.2f}) "
                f"is outside the core ({core.x0:.2f},{core.y0:.2f})-({core.x1:.2f},{core.y1:.2f})"
            )
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if a.rect.intersects(b.rect):
                problems.append(f"{a.name} overlaps {b.name}")
            elif a.rect.grown(hx, hy).intersects(b.rect):
                problems.append(
                    f"{b.name} intrudes on the {hx:g}x{hy:g}um halo of {a.name}"
                )
    return problems


class PlacementError(ValueError):
    """The strategy cannot fit the macros into the core."""


def _gaps(halo: tuple, channel: Optional[float]) -> tuple[float, float]:
    """Spacing between neighbouring macros: the halo is a floor (rule 3), the
    channel widens it when asked."""
    hx, hy = float(halo[0]), float(halo[1])
    ch = float(channel or 0.0)
    return max(hx, ch), max(hy, ch)


def _by_size(macros: list[Macro]) -> list[Macro]:
    return sorted(macros, key=lambda m: (-m.width * m.height, m.name))


def periphery(
    macros: list[Macro], core: Rect, halo: tuple, channel: Optional[float] = None,
    clearance: float = 1.0,
) -> list[Placement]:
    """All macros against the core boundary, spread over the edges: largest
    first, round-robin bottom/top/left/right, each edge's macros centred on
    it. Opposite edges first, so two macros end up facing each other rather
    than in one corner (which is what edge_cluster is for). Falls back to a
    tight walk around the ring when the spread layout does not fit."""
    gx, gy = _gaps(halo, channel)
    inner = core.grown(-clearance, -clearance)
    order = ("bottom", "top", "left", "right")
    buckets: dict[str, list[Macro]] = {edge: [] for edge in order}
    for i, macro in enumerate(_by_size(macros)):
        buckets[order[i % len(order)]].append(macro)

    spread: list[Placement] = []
    for edge, group in buckets.items():
        if not group:
            continue
        horizontal = edge in ("bottom", "top")
        gap = gx if horizontal else gy
        total = sum(m.width if horizontal else m.height for m in group) + gap * (len(group) - 1)
        cursor = (inner.x0 + (inner.width - total) / 2) if horizontal else (inner.y0 + (inner.height - total) / 2)
        for m in group:
            x, y = {
                "bottom": (cursor, inner.y0),
                "top": (cursor, inner.y1 - m.height),
                "left": (inner.x0, cursor),
                "right": (inner.x1 - m.width, cursor),
            }[edge]
            spread.append(Placement(m.name, m.master, x, y, m.width, m.height))
            cursor += (m.width if horizontal else m.height) + gap
    if not check_legality(spread, inner, (gx, gy)):
        return spread
    return _ring_walk(macros, inner, gx, gy)


def _ring_walk(macros: list[Macro], inner: Rect, gx: float, gy: float) -> list[Placement]:
    """Macros packed tightly around the ring: bottom edge left->right, right
    edge up, top edge right->left, left edge down."""
    placed: list[Placement] = []

    def fits(candidate: Placement) -> bool:
        return inner.contains(candidate.rect) and not check_legality(
            placed + [candidate], inner, (gx, gy)
        )

    for macro in _by_size(macros):
        w, h = macro.width, macro.height
        # Tight packing: along an edge the only useful positions are the edge's
        # start and those abutting an already-placed macro at exactly one gap.
        xs = [inner.x0, inner.x1 - w] + [q.rect.x1 + gx for q in placed] + [q.x - gx - w for q in placed]
        ys = [inner.y0, inner.y1 - h] + [q.rect.y1 + gy for q in placed] + [q.y - gy - h for q in placed]
        walk = (
            [(x, inner.y0) for x in sorted(xs)]               # bottom, left -> right
            + [(inner.x1 - w, y) for y in sorted(ys)]         # right, bottom -> top
            + [(x, inner.y1 - h) for x in sorted(xs, reverse=True)]  # top, right -> left
            + [(inner.x0, y) for y in sorted(ys, reverse=True)]      # left, top -> bottom
        )
        slot = None
        for x, y in walk:
            cand = Placement(macro.name, macro.master, x, y, w, h)
            if fits(cand):
                slot = cand
                break
        if slot is None:
            raise PlacementError(f"periphery: no room on the core ring for {macro.name} ({w:g}x{h:g}um)")
        placed.append(slot)
    return placed


def edge_cluster(
    macros: list[Macro], core: Rect, halo: tuple, channel: Optional[float] = None,
    edge: str = "bottom", clearance: float = 1.0,
) -> list[Placement]:
    """All macros packed into shelves against one edge, shelves growing
    inward. Concentrates the macros so the rest of the core is one block of
    standard-cell area."""
    gx, gy = _gaps(halo, channel)
    inner = core.grown(-clearance, -clearance)
    placed: list[Placement] = []
    # Pack in a local frame where the chosen edge is "bottom", then map back.
    horizontal = edge in ("bottom", "top")
    span = inner.width if horizontal else inner.height
    depth_limit = inner.height if horizontal else inner.width

    cursor, shelf_base, shelf_depth = 0.0, 0.0, 0.0
    for macro in _by_size(macros):
        along, deep = (macro.width, macro.height) if horizontal else (macro.height, macro.width)
        if along > span + EPS:
            raise PlacementError(f"edge_cluster: {macro.name} is wider than the {edge} edge")
        if cursor > 0 and cursor + along > span + EPS:
            shelf_base += shelf_depth + (gy if horizontal else gx)
            cursor, shelf_depth = 0.0, 0.0
        if shelf_base + deep > depth_limit + EPS:
            raise PlacementError(f"edge_cluster: macros do not fit against the {edge} edge")
        if edge == "bottom":
            x, y = inner.x0 + cursor, inner.y0 + shelf_base
        elif edge == "top":
            x, y = inner.x0 + cursor, inner.y1 - shelf_base - macro.height
        elif edge == "left":
            x, y = inner.x0 + shelf_base, inner.y0 + cursor
        elif edge == "right":
            x, y = inner.x1 - shelf_base - macro.width, inner.y0 + cursor
        else:
            raise ValueError(f"unknown edge {edge!r}")
        placed.append(Placement(macro.name, macro.master, x, y, macro.width, macro.height))
        cursor += along + (gx if horizontal else gy)
        shelf_depth = max(shelf_depth, deep)
    return placed


STRATEGIES = {"periphery": periphery, "edge_cluster": edge_cluster}


def explicit(macros: list[Macro], moves: Iterable) -> list[Placement]:
    """Every macro at an engineer-given position; none may be left out."""
    where = {name: (float(x), float(y)) for name, x, y in moves}
    missing = [m.name for m in macros if m.name not in where]
    if missing:
        raise PlacementError(f"explicit placement is missing {len(missing)} macro(s): {', '.join(missing[:3])}")
    return [Placement(m.name, m.master, *where[m.name], m.width, m.height) for m in _by_size(macros)]


def generate(
    strategy: str, macros: list[Macro], core: Rect, halo: tuple,
    channel: Optional[float] = None, moves: Iterable = (),
) -> list[Placement]:
    """Placement for a strategy, with engineer moves applied on top."""
    if not macros:
        raise PlacementError("design has no macros")
    if strategy == "explicit":
        return explicit(macros, moves)
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown macro strategy {strategy!r}; expected {sorted(STRATEGIES) + ['explicit']}")
    return apply_moves(STRATEGIES[strategy](macros, core, halo, channel), moves)


def apply_moves(placements: list[Placement], moves: Iterable) -> list[Placement]:
    """Apply engineer moves (name, x, y) on top of a generated placement."""
    by_name = {p.name: p for p in placements}
    for name, x, y in moves:
        if name not in by_name:
            raise PlacementError(f"move refers to unknown macro {name!r}")
        p = by_name[name]
        by_name[name] = Placement(p.name, p.master, float(x), float(y), p.width, p.height, p.orient)
    return [by_name[p.name] for p in placements]


def to_tcl(placements: list[Placement], strategy: str = "") -> str:
    """The MACRO_PLACEMENT_TCL file. place_macro LOCKs each macro, so RTL-MP
    (which ORFS still calls afterwards) treats them as fixed."""
    lines = [
        "# MACRO_PLACEMENT_TCL generated by adaptive_iicpilot FloorplanAgent",
        f'puts "\\[Floorplan\\] MACRO_PLACEMENT_TCL strategy={strategy} macros={len(placements)}"',
    ]
    for p in placements:
        lines.append(
            f"place_macro -macro_name {{{p.name}}} -location {{{p.x:.3f} {p.y:.3f}}} "
            f"-orientation {p.orient}"
        )
    return "\n".join(lines) + "\n"
