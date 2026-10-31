"""Floorplan knobs, the variant hash, and how each knob reaches make.

Every knob goes on the make COMMAND LINE rather than into the environment:
a command-line variable beats both `?=` and plain `=` in config.mk, while an
environment variable only beats `?=`. tinyRocket, for one, sets
CORE_UTILIZATION / CORE_ASPECT_RATIO / CORE_MARGIN with plain `=`, so an env
override there would be silently ignored and the run mislabelled.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional

# How far a run goes. SCREEN stops after global placement (3_3_place_gp), which
# is where the routability congestion metric is produced; FULL runs to finish.
class Stage:
    SCREEN = "SCREEN"
    FULL = "FULL"

    ALL = (SCREEN, FULL)


IO_STRATEGIES = ("default", "bus_sides", "in_left_out_right")
MACRO_MODES = ("none", "auto", "manual")
# "explicit": every macro position comes from macro_moves — what an engineer's
# HITL edit of an RTL-MP result turns into.
MACRO_STRATEGIES = ("periphery", "edge_cluster", "explicit")

FAMILY_SCOPES = {
    "shape": "shape-only",
    "pins": "pin strategy",
    "macros_auto": "macros: RTL-MP",
    "macros_manual": "macros: manual",
}

# The prefix every floorplan variant carries. The runner refuses to clean or
# write any variant without it, so "base" is out of reach by construction.
VARIANT_PREFIX = "fp_"


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    return value


@dataclass(frozen=True)
class FloorplanKnobs:
    design: str
    tech: str = "nangate45"
    # Shape. None means "whatever the design's config.mk / platform says".
    core_utilization: Optional[float] = None
    aspect_ratio: Optional[float] = None
    core_margin: Optional[float] = None
    # Explicit area mode: (x0, y0, x1, y1) in microns. Mutually exclusive with
    # utilization in floorplan.tcl, so setting these blanks CORE_UTILIZATION.
    die_area: Optional[tuple] = None
    core_area: Optional[tuple] = None
    # Pins.
    io_strategy: str = "default"
    # Macros.
    macro_mode: str = "none"
    macro_halo: Optional[tuple] = None  # (x, y) microns
    # Gap between adjacent macros in a MANUAL strategy. RTL-MP in this ORFS has
    # no channel parameter (MACRO_PLACE_CHANNEL was removed), so for auto mode
    # the halo is the only spacing knob; see FloorplanRunner.check_knobs.
    macro_channel: Optional[float] = None
    macro_strategy: Optional[str] = None
    # Engineer moves applied on top of a manual strategy: name -> (x, y).
    macro_moves: tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.io_strategy not in IO_STRATEGIES:
            raise ValueError(f"io_strategy must be one of {IO_STRATEGIES}")
        if self.macro_mode not in MACRO_MODES:
            raise ValueError(f"macro_mode must be one of {MACRO_MODES}")
        if self.macro_mode == "manual" and self.macro_strategy not in MACRO_STRATEGIES:
            raise ValueError(f"manual macro mode needs macro_strategy in {MACRO_STRATEGIES}")
        if (self.die_area is None) != (self.core_area is None):
            raise ValueError("die_area and core_area must be given together")
        if self.die_area is not None and self.core_utilization is not None:
            raise ValueError("explicit die/core area and core_utilization are mutually exclusive")

    def tweaked(self, **changes: Any) -> "FloorplanKnobs":
        return replace(self, **changes)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["macro_moves"] = [list(m) for m in self.macro_moves]
        return {k: _rounded(v) for k, v in data.items()}

    def key(self) -> str:
        """Canonical identity of the knob set (stage excluded). Memory keys on
        this, so SCREEN and FULL results for one floorplan line up."""
        return json.dumps(self.as_dict(), sort_keys=True)

    def variant(self, stage: str) -> str:
        """FLOW_VARIANT for this knob set at this stage.

        The stage is part of the hash on purpose: a full run gets its own
        directories, so its global-placement congestion is an independent
        measurement that can be compared with the screening run's.
        """
        if stage not in Stage.ALL:
            raise ValueError(f"stage must be one of {Stage.ALL}")
        payload = json.dumps({"knobs": self.as_dict(), "stage": stage}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        return f"{VARIANT_PREFIX}{stage.lower()}_{digest}"

    @property
    def family(self) -> str:
        """Which kind of floorplan decision this candidate varies."""
        if self.macro_mode == "auto":
            return "macros_auto"
        if self.macro_mode == "manual":
            return "macros_manual"
        if self.io_strategy != "default":
            return "pins"
        return "shape"

    @property
    def scope(self) -> str:
        """The search scope, as printed on cards: "shape-only" means only the
        core shape (aspect/margin/area/util) differs from stock."""
        return FAMILY_SCOPES[self.family]

    def label(self) -> str:
        """Short human-readable name for tables and logs."""
        parts = []
        if self.die_area is not None:
            parts.append(f"die={self.die_area[2] - self.die_area[0]:g}x{self.die_area[3] - self.die_area[1]:g}")
        if self.core_utilization is not None:
            parts.append(f"util={self.core_utilization:g}")
        if self.aspect_ratio is not None:
            parts.append(f"AR={self.aspect_ratio:g}")
        if self.core_margin is not None:
            parts.append(f"margin={self.core_margin:g}")
        if self.io_strategy != "default":
            parts.append(f"io={self.io_strategy}")
        if self.macro_mode == "auto":
            halo = "stock" if self.macro_halo is None else "x".join(f"{h:g}" for h in self.macro_halo)
            parts.append(f"rtlmp halo={halo}")
        elif self.macro_mode == "manual":
            parts.append(f"manual={self.macro_strategy}")
            if self.macro_channel is not None:
                parts.append(f"ch={self.macro_channel:g}")
            if self.macro_moves:
                parts.append(f"moves={len(self.macro_moves)}")
        return " ".join(parts) or "stock"

    def make_vars(self, io_tcl: Optional[str] = None, macro_tcl: Optional[str] = None) -> dict:
        """make command-line variables for this knob set.

        An empty value is meaningful: floorplan.tcl tests
        env_var_exists_and_non_empty, so `CORE_UTILIZATION=` switches the
        utilization method off when explicit areas are used (and vice versa,
        for designs like bp_fe whose config.mk pins DIE_AREA with plain `=`).
        """
        out: dict[str, str] = {}
        if self.die_area is not None:
            out["DIE_AREA"] = " ".join(f"{v:g}" for v in self.die_area)
            out["CORE_AREA"] = " ".join(f"{v:g}" for v in self.core_area)
            out["CORE_UTILIZATION"] = ""
            out["FLOORPLAN_DEF"] = ""
        else:
            if self.core_utilization is not None:
                out["CORE_UTILIZATION"] = f"{self.core_utilization:g}"
                out["DIE_AREA"] = ""
                out["CORE_AREA"] = ""
                out["FLOORPLAN_DEF"] = ""
            if self.aspect_ratio is not None:
                out["CORE_ASPECT_RATIO"] = f"{self.aspect_ratio:g}"
            if self.core_margin is not None:
                out["CORE_MARGIN"] = f"{self.core_margin:g}"
        if io_tcl:
            out["IO_CONSTRAINTS"] = io_tcl
        if self.macro_halo is not None:
            out["MACRO_PLACE_HALO"] = " ".join(f"{h:g}" for h in self.macro_halo)
        if macro_tcl:
            out["MACRO_PLACEMENT_TCL"] = macro_tcl
        return out


def knobs_from_dict(data: dict) -> FloorplanKnobs:
    data = dict(data)
    for key in ("die_area", "core_area", "macro_halo"):
        if data.get(key) is not None:
            data[key] = tuple(data[key])
    data["macro_moves"] = tuple(tuple(m) for m in data.get("macro_moves") or ())
    return FloorplanKnobs(**data)


def shell_quote_vars(variables: dict) -> str:
    """Render make variables for a bash command line. Single quotes keep
    spaces ("10 10") and the empty string intact."""
    parts = []
    for name, value in variables.items():
        escaped = str(value).replace("'", "'\"'\"'")
        parts.append(f"{name}='{escaped}'")
    return " ".join(parts)
