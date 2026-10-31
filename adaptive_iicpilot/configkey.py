"""One identity for a physical-design configuration, whoever measured it.

    (design, tech, util, density, aspect, margin, pin_strategy, macro_strategy, clock)

Three producers write measurements: the DSE/EDA path (ORFS result cache, keyed
by FlowConfig), the FloorplanAgent (keyed by FloorplanKnobs) and the memory
database (runs rows). Each used to carry a different subset of knobs, so the
same physical run could appear under several names (aspect None vs 1.0, util
None vs the design's stock 50) and two different runs could collide. Every
reader now normalises to ConfigKey, resolving "stock" to a concrete value:

  util      stock -> the design's CORE_UTILIZATION; 0.0 = the design's own
            FLOORPLAN_DEF (aes), which no utilization run can merge with
  density   0.0 = auto (ORFS computes PLACE_DENSITY)
  aspect    stock -> 1.0
  margin    None = the design's/platform's CORE_MARGIN
  pins      "default" = ORFS place_pins with no IO_CONSTRAINTS
  macros    "stock" = the design's default (no macros, or RTL-MP with the
            platform halo); otherwise "rtlmp halo=XxY" or "manual:<strategy>"
  clock     stock -> the design's SDC period

Rows written before a knob existed carry no value for it and therefore mean
the stock value, which is exactly what the defaults below produce.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

STOCK_PINS = "default"
STOCK_MACROS = "stock"


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 4)


def macro_strategy_of(macro_mode: Optional[str], macro_strategy: Optional[str] = None,
                      macro_halo=None, macro_moves=None) -> str:
    """Canonical macro-placement name. RTL-MP at the platform halo is what the
    stock flow does for a design with macros, so it is "stock" too."""
    mode = macro_mode or "none"
    if mode == "manual":
        name = f"manual:{macro_strategy or 'explicit'}"
        return f"{name}+{len(macro_moves)}moves" if macro_moves else name
    if macro_halo:
        return "rtlmp halo=" + "x".join(f"{float(h):g}" for h in macro_halo)
    return STOCK_MACROS


@dataclass(frozen=True)
class ConfigKey:
    design: str
    tech: str
    util: float
    density: float
    aspect: float
    margin: Optional[float]
    pin_strategy: str
    macro_strategy: str
    clock_ns: float

    def __post_init__(self) -> None:
        # Rounded once, here, so equality and hashing never depend on how a
        # producer happened to format a float (85 vs 85.0 vs 85.00001).
        for name in ("util", "density", "aspect", "clock_ns"):
            object.__setattr__(self, name, _r(getattr(self, name)) or 0.0)
        object.__setattr__(self, "margin", _r(self.margin))

    # --- views ---

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def floorplan_key(self) -> tuple:
        """Everything but utilization, density and clock: the floorplan a
        DSE search holds fixed while it walks utilization."""
        return (self.aspect, self.margin, self.pin_strategy, self.macro_strategy)

    @property
    def wall_key(self) -> tuple:
        """What a congestion wall (lowest util that congests) is specific to."""
        return (self.density, self.clock_ns) + self.floorplan_key

    def is_stock_floorplan(self) -> bool:
        return (self.aspect == 1.0 and self.margin is None and self.pin_strategy == STOCK_PINS
                and self.macro_strategy == STOCK_MACROS)

    def label(self, with_clock: bool = True) -> str:
        parts = ["own FLOORPLAN_DEF" if self.util == 0 else f"util {self.util:g}", f"AR {self.aspect:g}"]
        if self.density:
            parts.append(f"density {self.density:g}")
        if self.margin is not None:
            parts.append(f"margin {self.margin:g}")
        if self.pin_strategy != STOCK_PINS:
            parts.append(f"pins {self.pin_strategy}")
        if self.macro_strategy != STOCK_MACROS:
            parts.append(f"macros {self.macro_strategy}")
        if with_clock:
            parts.append(f"{self.clock_ns:g} ns")
        return ", ".join(parts)

    def floorplan_dict(self) -> Optional[dict]:
        """FlowConfig.floorplan for the knobs FlowConfig has no field for.
        None for a stock margin/pins/macros, so stock configs keep the cache
        keys they were written under."""
        out: dict = {}
        if self.margin is not None:
            out["core_margin"] = self.margin
        if self.pin_strategy != STOCK_PINS:
            out["io_strategy"] = self.pin_strategy
        if self.macro_strategy.startswith("rtlmp halo="):
            out["macro_mode"] = "auto"
            out["macro_halo"] = [float(v) for v in self.macro_strategy.split("=", 1)[1].split("x")]
        elif self.macro_strategy.startswith("manual:"):
            out["macro_mode"] = "manual"
            out["macro_strategy"] = self.macro_strategy.split(":", 1)[1].split("+", 1)[0]
        return out or None

    def to_flow_config(self):
        from .types import FlowConfig

        return FlowConfig(
            design=self.design, tech=self.tech, clock_period_ns=self.clock_ns,
            core_utilization=self.util, placement_density=self.density,
            aspect_ratio=self.aspect, floorplan=self.floorplan_dict(),
        )

    def tweaked(self, **changes: Any) -> "ConfigKey":
        data = self.as_dict()
        data.update(changes)
        return ConfigKey(**data)


# --- normalisers, one per producer ---

def from_flow_config(config, stock_clock_ns: Optional[float]) -> ConfigKey:
    fp = config.floorplan or {}
    return ConfigKey(
        design=config.design, tech=config.tech,
        util=config.core_utilization, density=config.placement_density or 0.0,
        aspect=config.aspect_ratio if config.aspect_ratio is not None else 1.0,
        margin=fp.get("core_margin"),
        pin_strategy=fp.get("io_strategy") or STOCK_PINS,
        macro_strategy=macro_strategy_of(fp.get("macro_mode"), fp.get("macro_strategy"),
                                         fp.get("macro_halo"), fp.get("macro_moves")),
        clock_ns=config.clock_period_ns or stock_clock_ns or 0.0,
    )


def from_floorplan_knobs(knobs: dict, stock_util: Optional[float], stock_clock_ns: Optional[float],
                         clock_ns: Optional[float] = None) -> ConfigKey:
    """knobs: FloorplanKnobs.as_dict(). FloorplanAgent runs use the stock SDC
    unless a clock is given."""
    util = knobs.get("core_utilization")
    if knobs.get("die_area") is not None:
        util = -1.0  # explicit die/core area: never equal to any utilization run
    return ConfigKey(
        design=knobs["design"], tech=knobs.get("tech", "nangate45"),
        util=util if util is not None else (stock_util or 0.0),
        density=0.0,
        aspect=knobs.get("aspect_ratio") if knobs.get("aspect_ratio") is not None else 1.0,
        margin=knobs.get("core_margin"),
        pin_strategy=knobs.get("io_strategy") or STOCK_PINS,
        macro_strategy=macro_strategy_of(knobs.get("macro_mode"), knobs.get("macro_strategy"),
                                         knobs.get("macro_halo"), knobs.get("macro_moves")),
        clock_ns=clock_ns or stock_clock_ns or 0.0,
    )


def from_row(row) -> ConfigKey:
    """A memory `runs` row; columns added later may be absent or NULL."""
    keys = row.keys() if hasattr(row, "keys") else row
    get = (lambda k, d=None: row[k] if k in keys and row[k] is not None else d)
    return ConfigKey(
        design=get("design"), tech=get("tech", "nangate45"),
        util=get("util", 0.0), density=get("density", 0.0), aspect=get("aspect", 1.0),
        margin=get("margin"), pin_strategy=get("pin_strategy", STOCK_PINS),
        macro_strategy=get("macro_strategy", STOCK_MACROS), clock_ns=get("clock_ns", 0.0),
    )
