"""The EDA interface shared by the EDA agent and the DSE agent.

MockORFSAdapter is a deterministic, pure-Python physical model calibrated
against IICPilot Table I (nangate45) so the whole system is demoable
offline, with no OpenROAD-flow-scripts install required.

ORFSAdapter is the real skeleton for a future OpenROAD-flow-scripts
integration (Phase 3) — it deliberately raises NotImplementedError so it
can be swapped in later without touching EDAAgent/DSEAgent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from .types import STAGES, FlowConfig, Metrics, RunResult

# Real ORFS stage prefixes in flow order; the last one present in a
# metadata.json says how far the flow actually got.
STAGES_ORFS = [
    "synth",
    "floorplan",
    "globalplace",
    "placeopt",
    "detailedplace",
    "cts",
    "globalroute",
    "detailedroute",
    "finish",
]


# The discrete utilization rungs the real backend explores. DSE and
# sweep_ibex.py share this list on purpose: the sweep warms the cache at
# exactly the configs DSE will later ask for, so a demo run replays from
# cache instead of triggering 10-30 minute flows.
ORFS_UTIL_LADDER = [50.0, 60.0, 70.0, 80.0, 85.0, 90.0]

# The single line every nangate45 constraint.sdc uses to set the clock period.
_CLK_PERIOD_RE = re.compile(r"set\s+clk_period\s+([0-9.]+)")

_SDC_SET_RE = re.compile(r"^\s*set\s+(\w+)\s+(\S+)\s*$", re.MULTILINE)
_CREATE_CLOCK_RE = re.compile(r"^\s*create_clock\b(.*)$", re.MULTILINE)
# create_clock options that take a value; whatever is left over is the clock's
# source object (a port/pin). A clock with no source is a VIRTUAL clock.
_CLOCK_VALUE_OPTS = ("-name", "-period", "-waveform", "-comment")


def sdc_clocks(sdc_text: str) -> list[dict]:
    """Every create_clock in an SDC, with its resolved name and whether it is
    virtual (no source object). Only plain `set var value` lines are resolved,
    which is all the nangate45 constraint files use."""
    variables: dict = {}

    def resolve(token: str) -> str:
        return re.sub(r"\$\{?(\w+)\}?", lambda m: variables.get(m.group(1), m.group(0)), token)

    # In definition order, as Tcl evaluates them (`set b vclk_$a`).
    for var, value in _SDC_SET_RE.findall(re.sub(r"#.*", "", sdc_text)):
        variables[var] = resolve(value)

    clocks = []
    for args in _CREATE_CLOCK_RE.findall(re.sub(r"#.*", "", sdc_text)):
        # Keep [...] and {...} groups whole so `[get_ports clk]` is one token.
        tokens = re.findall(r"\[[^\]]*\]|\{[^}]*\}|\S+", args)
        name, period, rest, i = None, None, [], 0
        while i < len(tokens):
            token = tokens[i]
            if token in _CLOCK_VALUE_OPTS and i + 1 < len(tokens):
                if token == "-name":
                    name = resolve(tokens[i + 1])
                elif token == "-period":
                    try:
                        period = float(resolve(tokens[i + 1]))
                    except ValueError:
                        period = None  # an expression, e.g. [expr ...]
                i += 2
                continue
            if not token.startswith("-"):
                rest.append(token)
            i += 1
        clocks.append({"name": name, "virtual": not rest, "source": " ".join(rest) or None,
                       "period": period})
    return clocks


def real_clock_name(sdc_text: str) -> Optional[str]:
    """The one non-virtual clock, or None when there is not exactly one (then
    no fmax is reported rather than guessing which clock it belongs to)."""
    real = [c["name"] for c in sdc_clocks(sdc_text) if not c["virtual"] and c["name"]]
    return real[0] if len(set(real)) == 1 else None


# The floorplan fields the ORFS adapter knows how to apply. Manual macro
# placement is not among them: it needs the core geometry of each run and a
# legality check first, which only the FloorplanAgent's own runner does.
FLOORPLAN_APPLIED_FIELDS = ("core_margin", "io_strategy", "macro_halo")


def floorplan_applied(floorplan: Optional[dict]) -> dict:
    """The part of a FlowConfig.floorplan that changes the flow (labels and
    provenance are left out, so they never split a cache entry)."""
    if not floorplan:
        return {}
    out = {}
    for name in FLOORPLAN_APPLIED_FIELDS:
        value = floorplan.get(name)
        if value is None or (name == "io_strategy" and value == "default"):
            continue
        out[name] = [round(float(v), 4) for v in value] if name == "macro_halo" else (
            round(float(value), 4) if name == "core_margin" else value
        )
    return out


def _default_cache_dir() -> Path:
    """Defaults inside the repo rather than under $HOME because the sweep runs
    in WSL while the web UI runs on Windows — a home-relative path would give
    them two different caches, and the demo depends on sharing one."""
    override = os.environ.get("ADAPTIVE_IICPILOT_CACHE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".orfs_cache"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class EDAInterface(ABC):
    @abstractmethod
    def configure(self, config: FlowConfig) -> None:
        ...

    @abstractmethod
    def run(self, from_stage: str, to_stage: str) -> RunResult:
        ...

    @abstractmethod
    def collect(self, stage: str) -> Metrics:
        ...


class MockORFSAdapter(EDAInterface):
    """Deterministic physical model. No external EDA tools required."""

    # cells, critical_path_ns, power_mw, cell_area_um2 — nangate45, Table I
    DESIGN_LIBRARY = {
        "gcd": (1100, 0.41, 3.0, 430),
        "picorv32": (12000, 1.93, 24.0, 18000),
        "ibex": (18000, 2.26, 102.0, 22000),
        "aes": (15000, 1.15, 418.0, 24000),
        "_default": (10000, 1.5, 20.0, 15000),
    }

    ROUTE_CONGESTION_LIMIT = 0.55
    # The mock's own stock clock, used when a config asks for the default.
    DEFAULT_CLOCK_NS = 2.5
    LOG_TAIL_LEN = 10

    def __init__(self) -> None:
        self._config: FlowConfig | None = None

    def effective_clock_ns(self, config: FlowConfig) -> float:
        """Clock 0 means "stock"; for the mock that is its own default period.
        Reported so the log never shows 0.00ns, which is not a real clock."""
        return config.clock_period_ns or self.DEFAULT_CLOCK_NS

    def configure(self, config: FlowConfig) -> None:
        self._config = config

    def collect(self, stage: str) -> Metrics:
        if self._config is None:
            raise RuntimeError("configure() must be called before collect()")
        cfg = self._config
        cells, crit, power_base, cell_area = self.DESIGN_LIBRARY.get(
            cfg.design, self.DESIGN_LIBRARY["_default"]
        )

        util = cfg.core_utilization / 100.0
        density = cfg.placement_density
        congestion = _clamp(
            0.9 * util + 0.5 * density - 0.55 + 0.1 * abs(cfg.aspect_ratio - 1.0),
            0.0,
            1.0,
        )

        area_um2 = (cell_area / max(util, 0.05)) * (1 + 0.3 * congestion)

        pre_cts = STAGES.index(stage) < STAGES.index("cts")
        clock_ns = cfg.clock_period_ns or self.DEFAULT_CLOCK_NS
        wns_ns = clock_ns - crit - 0.4 * congestion
        if pre_cts:
            wns_ns += 0.2
        tns_ns = min(0.0, wns_ns) * max(1, cells // 2000)

        power_mw = power_base * (0.6 + 0.4 * (crit / clock_ns))
        if wns_ns < 0:
            power_mw *= 1 + 0.15 * min(-wns_ns, 2.0)

        return Metrics(
            stage=stage,
            wns_ns=wns_ns,
            tns_ns=tns_ns,
            congestion=congestion,
            cell_count=cells,
            area_um2=area_um2,
            power_mw=power_mw,
        )

    def run(self, from_stage: str, to_stage: str) -> RunResult:
        if self._config is None:
            raise RuntimeError("configure() must be called before run()")
        if from_stage not in STAGES or to_stage not in STAGES:
            raise ValueError(f"unknown stage(s): {from_stage!r}, {to_stage!r}")
        i0, i1 = STAGES.index(from_stage), STAGES.index(to_stage)
        if i0 > i1:
            raise ValueError("from_stage must not come after to_stage")

        log: list[str] = []
        metrics = None
        for i in range(i0, i1 + 1):
            stage = STAGES[i]
            metrics = self.collect(stage)
            log.append(
                f"[{stage}] wns={metrics.wns_ns:.3f}ns "
                f"congestion={metrics.congestion:.2f} area={metrics.area_um2:.1f}um2"
            )
            if stage == "route" and metrics.congestion > self.ROUTE_CONGESTION_LIMIT:
                failure = "routing congestion exceeds routable limit"
                log.append(f"[route] ERROR: {failure}")
                return RunResult(
                    config=self._config,
                    reached_stage=stage,
                    metrics=metrics,
                    ok=False,
                    failure=failure,
                    log_tail=log[-self.LOG_TAIL_LEN:],
                )

        return RunResult(
            config=self._config,
            reached_stage=STAGES[i1],
            metrics=metrics,
            ok=True,
            failure=None,
            log_tail=log[-self.LOG_TAIL_LEN:],
        )


class ORFSAdapter(EDAInterface):
    """Real OpenROAD-flow-scripts adapter.

    Drives a native ORFS build (Docker is unusable here: the official image
    dies at CTS with SIGILL on AVX-512). Knobs are passed to make as
    environment overrides because every config.mk declares them with `?=` —
    config.mk is never edited. Clock period lives in constraint.sdc and is
    deliberately not driven.

    A real ibex run takes 10-30 minutes, so every result is cached by config
    hash; a repeat config replays in milliseconds without invoking make.
    """

    # Verified against real metadata.json (Metrics_2.1.2).
    KEY_MAP = {
        "wns_ns": "finish__timing__setup__ws",
        "tns_ns": "finish__timing__setup__tns",
        "die_area_um2": "finish__design__die__area",
        "core_area_um2": "finish__design__core__area",
        "area_um2": "finish__design__instance__area",
        "cell_count": "finish__design__instance__count",
        "congestion": "globalplace__gpl__routability__congestion",
        "drc_errors": "detailedroute__route__drc_errors",
    }
    POWER_KEY = "finish__power__total"  # Watts
    # Max over ALL clocks, virtual ones included: for ibex that is the virtual
    # I/O clock (in2out paths), not core_clock. Kept only as a labelled
    # headline; fmax/cp_delay come from the real clock's per-clock key.
    FMAX_KEY = "finish__timing__fmax"  # Hz
    FMAX_CLOCK_PREFIX = "finish__timing__fmax__clock__"
    # Class-level: reading a stock clock shells into WSL, and the value is a
    # property of the design on disk, not of an adapter instance.
    _stock_clocks: dict = {}
    _real_clocks: dict = {}
    _timing_thresholds: dict = {}
    CONGESTION_LIMIT = 1.0
    LOG_TAIL_LEN = 15

    def __init__(
        self,
        orfs_root: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.orfs_root = orfs_root or os.environ.get("ORFS_ROOT", "~/OpenROAD-flow-scripts")
        self.cache_dir = Path(cache_dir) if cache_dir else _default_cache_dir()
        # Called with each line of the main flow's output as it is produced,
        # so a live run can be watched instead of blocking silently.
        self.on_line = on_line
        self._config: Optional[FlowConfig] = None
        self._metrics: Optional[Metrics] = None

    def configure(self, config: FlowConfig) -> None:
        self._config = config

    def collect(self, stage: str) -> Metrics:
        if self._metrics is None:
            raise RuntimeError("run() must be called before collect()")
        return self._metrics

    # --- caching ---

    def cache_key(self, config: FlowConfig) -> str:
        fields = {
            "design": config.design,
            "tech": config.tech,
            "util": round(config.core_utilization, 4),
            "density": round(config.placement_density, 4),
            "aspect": round(config.aspect_ratio, 4),
            # Clock is a real knob now, so two runs that differ only in
            # clock period are different measurements. The RESOLVED period
            # is keyed, so "stock" and its explicit value are one entry.
            "clock_ns": round(self.effective_clock_ns(config), 4),
        }
        applied = floorplan_applied(config.floorplan)
        if applied:
            # Only when a floorplan is set, so every stock-floorplan entry
            # cached before the floorplan stage existed keeps its key.
            fields["floorplan"] = applied
        payload = json.dumps(fields, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _floorplan_overrides(self, config: FlowConfig) -> str:
        """make command-line variables for the FloorplanAgent's choice.

        Command line rather than environment: a design whose config.mk sets
        CORE_ASPECT_RATIO / CORE_MARGIN with plain `=` (tinyRocket) would
        silently ignore an env override."""
        if not config.floorplan:
            return ""
        applied = floorplan_applied(config.floorplan)
        out = [f"CORE_ASPECT_RATIO={config.aspect_ratio:g}"]
        if "core_margin" in applied:
            out.append(f"CORE_MARGIN={applied['core_margin']:g}")
        if "macro_halo" in applied:
            out.append("MACRO_PLACE_HALO='" + " ".join(f"{h:g}" for h in applied["macro_halo"]) + "'")
        if applied.get("io_strategy", "default") != "default":
            path = self._io_constraints_file(config, applied["io_strategy"])
            if path is None:
                raise RuntimeError(
                    f"could not write IO_CONSTRAINTS for {config.design} "
                    f"({applied['io_strategy']}); refusing to run and label it as that floorplan"
                )
            out.append(f"IO_CONSTRAINTS={path}")
        return " ".join(out)

    def _io_constraints_file(self, config: FlowConfig, strategy: str) -> Optional[str]:
        """The same generator the FloorplanAgent's own runs use, fed with the
        design's ports from its shared synthesis."""
        from .floorplan import iotcl
        from .floorplan.runner import FloorplanRunner, to_wsl

        runner = FloorplanRunner(orfs_root=self.orfs_root, emit=lambda line: None)
        info = runner.ensure_synth(config.design, config.tech)
        text = iotcl.generate(runner.ports_of(info), strategy, config.design, config.aspect_ratio)
        if text is None:
            return None
        path = runner.artifacts / config.design / "pipeline" / f"io_{strategy}_ar{config.aspect_ratio:g}.tcl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return to_wsl(path)

    # --- clock period ---

    def sdc_path(self, design: str) -> str:
        nickname = self._design_nickname(design)
        return f"{self.orfs_root}/flow/designs/nangate45/{nickname}/constraint.sdc"

    def stock_clock_ns(self, design: str) -> Optional[float]:
        """The clock period the design ships with, read from its SDC."""
        if design in self._stock_clocks:
            return self._stock_clocks[design]
        ok, text = self._shell(f"cat {self.sdc_path(design)}")
        period = None
        if ok:
            match = _CLK_PERIOD_RE.search(text)
            if match:
                period = float(match.group(1))
            else:
                # Some SDCs (tinyRocket) give the period inline on create_clock.
                real = [c["period"] for c in sdc_clocks(text) if not c["virtual"] and c["period"]]
                period = real[0] if len(set(real)) == 1 else None
        self._stock_clocks[design] = period
        return period

    def real_clock_name(self, design: str) -> Optional[str]:
        """Name of the design's real (non-virtual) clock, from its SDC.

        Read from the create_clock that has a source object, never guessed from
        naming (vclk_*): fmax must not silently come from a virtual clock.
        A rewritten-period SDC keeps the names, so the stock file is enough."""
        if design not in self._real_clocks:
            ok, text = self._shell(f"cat {self.sdc_path(design)}")
            self._real_clocks[design] = real_clock_name(text) if ok else None
        return self._real_clocks[design]

    def effective_clock_ns(self, config: FlowConfig) -> float:
        """The clock period this run is really constrained with.

        Clock 0 means "the design's stock period", so it resolves here — once —
        and every downstream decision (cache key, SDC rewrite, logging) uses the
        resolved value. Without that, a config could be cached under 0 and
        another under 2.2 for the same physical run.
        """
        if config.clock_period_ns:
            return config.clock_period_ns
        stock = self.stock_clock_ns(config.design)
        return stock if stock is not None else 0.0

    # --- timing allowance ---

    FALLBACK_TIMING_FRACTION = 0.02  # 2% of the clock period

    def timing_threshold_ns(self, design: str, clock_ns: float) -> float:
        """Negative slack ORFS tolerates for this design.

        Taken from the design's own rules-base.json (the file ORFS's
        metadata-check gates on), so "acceptable" means what the flow's own
        maintainers consider acceptable rather than a number we invented. With
        no rules file, fall back to 2% of the clock period.
        """
        if design not in self._timing_thresholds:
            nickname = self._design_nickname(design)
            path = (
                f"{self.orfs_root}/flow/designs/nangate45/{nickname}/rules-base.json"
            )
            ok, text = self._shell(f"cat {path}")
            threshold = None
            if ok and text.strip():
                try:
                    rule = (json.loads(text) or {}).get("finish__timing__setup__ws")
                    if isinstance(rule, dict):
                        threshold = float(rule.get("value"))
                    elif rule is not None:
                        threshold = float(rule)
                except (json.JSONDecodeError, TypeError, ValueError):
                    threshold = None
            self._timing_thresholds[design] = threshold
        from_rules = self._timing_thresholds[design]
        if from_rules is not None:
            return from_rules
        return -abs(clock_ns) * self.FALLBACK_TIMING_FRACTION

    def _write_clock_sdc(self, design: str, clock_ns: float) -> Optional[str]:
        """Copy the design's SDC with the period replaced, into a temp file.

        The ORFS tree is never edited: the copy is handed to make as a
        command-line SDC_FILE. It must be command-line rather than an env var
        because config.mk assigns SDC_FILE with plain `=`, which beats the
        environment but loses to a command-line variable.
        """
        ok, text = self._shell(f"cat {self.sdc_path(design)}")
        if not ok or not _CLK_PERIOD_RE.search(text):
            return None
        rewritten, count = _CLK_PERIOD_RE.subn(
            lambda _m: f"set clk_period {clock_ns:g}", text, count=1
        )
        if count != 1:
            return None

        target = f"/tmp/adaptive_iicpilot_{design}_{clock_ns:g}ns.sdc".replace(" ", "")
        payload = base64.b64encode(rewritten.encode()).decode()
        # base64 so the SDC's quotes, brackets and $ survive the shell hop.
        wrote, _ = self._shell(f"echo {payload} | base64 -d > {target}")
        return target if wrote else None

    def _cache_path(self, config: FlowConfig) -> Path:
        return self.cache_dir / f"{config.design}-{self.cache_key(config)}.json"

    def migrate_cache_clocks(self, dry_run: bool = False) -> dict:
        """Re-key cache entries written before clock became part of the key.

        Those runs all used their design's stock clock, but recorded
        FlowConfig's old 2.5ns placeholder. Left alone they would be both
        mislabelled and unreachable, since the key now includes the clock.
        """
        migrated, skipped = [], []
        for path in sorted(self.cache_dir.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            config_data = entry.get("config") or {}
            design = config_data.get("design")
            if not design:
                continue
            stock = self.stock_clock_ns(design)
            if stock is None:
                skipped.append(f"{path.name} (no stock clock for {design})")
                continue

            config = FlowConfig(
                design=design,
                tech=config_data.get("tech", "nangate45"),
                clock_period_ns=stock,
                core_utilization=config_data.get("core_utilization", 0.0),
                placement_density=config_data.get("placement_density", 0.0),
                aspect_ratio=config_data.get("aspect_ratio", 1.0),
            )
            target = self._cache_path(config)
            if target == path and config_data.get("clock_period_ns") == stock:
                continue

            config_data["clock_period_ns"] = stock
            entry["config"] = config_data
            migrated.append(
                f"{design} util={config.core_utilization:g} "
                f"clock {config_data.get('clock_period_ns')}->{stock:g}ns: "
                f"{path.name} -> {target.name}"
            )
            if not dry_run:
                target.write_text(json.dumps(entry, indent=2), encoding="utf-8")
                if target != path:
                    path.unlink()
        return {"migrated": migrated, "skipped": skipped, "count": len(migrated)}

    def cached_entries(self, design: str) -> list[dict]:
        """Every cached result for a design, as stored on disk.

        The cache is the only record of real measurements, so the estimator
        reads it through here rather than duplicating the file layout.
        """
        entries = []
        for path in sorted(self.cache_dir.glob(f"{design}-*.json")):
            try:
                entries.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return entries

    def is_cached(self, config: FlowConfig) -> bool:
        """Whether this config replays instantly. Callers use it to avoid
        blocking an interactive session on a 10-30 minute flow."""
        if self._cache_path(config).is_file():
            return True
        return self._floorplan_full(config) is not None

    def _floorplan_full(self, config: FlowConfig):
        """The FloorplanAgent's FULL run of this exact ConfigKey, if any.

        Same physical run, other producer: replaying it is what stops DSE from
        re-running a configuration the FloorplanAgent already measured (util 85
        at AR 0.5 for ibex). A failed run is returned too: it is a measurement.
        """
        from .configkey import from_flow_config
        from .measurements import FULL, Measurements

        key = from_flow_config(config.tweaked(clock_period_ns=self.effective_clock_ns(config)),
                               self.stock_clock_ns(config.design))
        if not hasattr(self, "_fp_points"):
            self._fp_points = {}
        if config.design not in self._fp_points:
            self._fp_points[config.design] = [
                p for p in Measurements(adapter=self)._from_floorplan(config.design) if p.kind == FULL
            ]
        return next((p for p in self._fp_points[config.design] if p.key == key), None)

    def _load_floorplan_full(self, config: FlowConfig) -> Optional[RunResult]:
        point = self._floorplan_full(config)
        if point is None:
            return None
        m = point.metrics
        metrics = Metrics(
            stage=m.get("reached", "unknown"), wns_ns=m.get("wns_ns") or 0.0,
            tns_ns=m.get("tns_ns") or 0.0, congestion=m.get("congestion") or 0.0,
            cell_count=0, area_um2=0.0, power_mw=m.get("power_mw") or 0.0,
            die_area_um2=m.get("die_area_um2") or 0.0, core_area_um2=m.get("core_area_um2") or 0.0,
            drc_errors=int(m.get("drc_errors") or 0), cp_delay_ns=m.get("cp_delay_ns"),
            fmax_clock=m.get("fmax_clock"), cp_delay_note=m.get("cp_delay_note"),
            timing_threshold_ns=self.timing_threshold_ns(config.design, self.effective_clock_ns(config)),
            congestion_limit=self.CONGESTION_LIMIT,
        )
        return RunResult(
            config=config, reached_stage=metrics.stage, metrics=metrics, ok=point.ok,
            failure=None if point.ok else "ORFS flow returned non-zero",
            log_tail=[f"replayed FloorplanAgent FULL run {point.variant} ({point.origin})"],
        )

    def _load_cached(self, config: FlowConfig) -> Optional[RunResult]:
        path = self._cache_path(config)
        if not path.is_file():
            return self._load_floorplan_full(config)
        data = json.loads(path.read_text(encoding="utf-8"))
        stored = dict(data["metrics"])
        # The timing allowance is a property of the design's rules file, not of
        # the run, so it is applied on load. Cached entries written before the
        # field existed would otherwise default to 0 and make every run that
        # closes with a hair of negative slack read as VIOLATED.
        stored["timing_threshold_ns"] = self.timing_threshold_ns(
            config.design, self.effective_clock_ns(config)
        )
        metrics = Metrics(**stored)
        return RunResult(
            config=config,
            reached_stage=data["reached_stage"],
            metrics=metrics,
            ok=data["ok"],
            failure=data.get("failure"),
            log_tail=data.get("log_tail", []),
        )

    def _store_cached(self, config: FlowConfig, result: RunResult) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "config": asdict(config),
            "reached_stage": result.reached_stage,
            "ok": result.ok,
            "failure": result.failure,
            "log_tail": result.log_tail,
            "metrics": asdict(result.metrics),
        }
        path = self._cache_path(config)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # --- running the real flow ---

    def _shell(
        self,
        command: str,
        timeout: Optional[int] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> tuple[bool, str]:
        """ORFS lives in WSL; on Windows every command is relayed through it.

        With on_line, output is read line by line as it is produced (stderr
        merged into stdout) instead of being buffered until exit."""
        if platform.system() == "Windows":
            argv = ["wsl.exe", "-e", "bash", "-lc", command]
        else:
            argv = ["bash", "-lc", command]
        if on_line is None:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")

        lines: list[str] = []
        with subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as proc:
            for raw in proc.stdout:
                line = raw.rstrip()
                lines.append(line)
                if line.strip():
                    on_line(line)
            returncode = proc.wait(timeout=timeout)
        return returncode == 0, "\n".join(lines)

    def _knob_env(self, config: FlowConfig) -> str:
        knobs = [f"CORE_UTILIZATION={config.core_utilization}"]
        # A non-positive density means "let ORFS compute it", which is the
        # regime the reference numbers were measured in.
        if config.placement_density > 0:
            knobs.append(f"PLACE_DENSITY={config.placement_density}")
        if config.aspect_ratio != 1.0:
            knobs.append(f"CORE_ASPECT_RATIO={config.aspect_ratio}")
        return " ".join(knobs)

    def _design_nickname(self, design: str) -> str:
        ok, out = self._shell(
            f"grep -E '^\\s*(export\\s+)?DESIGN_NICKNAME' "
            f"{self.orfs_root}/flow/designs/nangate45/{design}/config.mk || true"
        )
        match = re.search(r"DESIGN_NICKNAME\s*[:?]?=\s*(\S+)", out) if ok else None
        return match.group(1) if match else design

    def _make_overrides(self, design: str) -> str:
        """make command-line variables needed for CORE_UTILIZATION to apply.

        Some designs (aes) pin a fixed floorplan with a hard `FLOORPLAN_DEF =`
        assignment. floorplan.tcl treats FLOORPLAN_DEF and CORE_UTILIZATION as
        mutually exclusive and exits 1 when both are set, so every utilization
        rung would die at floorplan. An env override cannot beat a hard `=`;
        a make command-line variable can, and an empty value disables it."""
        ok, out = self._shell(
            f"grep -E '^\\s*(export\\s+)?FLOORPLAN_DEF\\s*[:?]?=' "
            f"{self.orfs_root}/flow/designs/nangate45/{design}/config.mk || true"
        )
        return "FLOORPLAN_DEF=" if ok and out.strip() else ""

    def run(self, from_stage: str, to_stage: str) -> RunResult:
        if self._config is None:
            raise RuntimeError("configure() must be called before run()")
        # Normalise the clock up front so the RunResult describes the period the
        # flow was actually constrained with. Without this, a config asking for
        # "stock" (0) would be recorded in memory under 0 while an explicit
        # 2.2ns run recorded 2.2 — two keys for one physical configuration.
        config = self._config.tweaked(clock_period_ns=self.effective_clock_ns(self._config))

        cached = self._load_cached(config)
        if cached is not None:
            self._metrics = cached.metrics
            return cached

        flow = f"{self.orfs_root}/flow"
        design_config = f"./designs/nangate45/{config.design}/config.mk"
        knobs = self._knob_env(config)
        overrides = f"{self._make_overrides(config.design)} {self._floorplan_overrides(config)}".strip()
        prefix = f"cd {flow} && source ../env.sh >/dev/null 2>&1"

        # A clock different from the design's stock period needs a rewritten
        # SDC, passed as a command-line variable so it beats config.mk's
        # `export SDC_FILE = ...` assignment.
        stock = self.stock_clock_ns(config.design)
        clock_ns = self.effective_clock_ns(config)
        if stock is not None and abs(clock_ns - stock) > 1e-9:
            sdc = self._write_clock_sdc(config.design, clock_ns)
            if sdc is None:
                # Running at stock while labelling the result as a different
                # clock would poison the cache with a mislabelled measurement.
                raise RuntimeError(
                    f"could not write a {clock_ns:g}ns SDC for {config.design}; "
                    f"refusing to run at the stock clock and label it {clock_ns:g}ns"
                )
            overrides = f"{overrides} SDC_FILE={sdc}".strip()

        # clean_all is mandatory, not hygiene: make is timestamp-incremental
        # and a changed CORE_UTILIZATION does not invalidate any result file,
        # so without it make no-ops and metadata-generate re-harvests the
        # PREVIOUS run's numbers — every utilization would report identically.
        self._shell(f"{prefix} && make clean_all DESIGN_CONFIG={design_config}")

        flow_ok, log = self._shell(
            f"{prefix} && {knobs} make DESIGN_CONFIG={design_config} {overrides}",
            on_line=self.on_line,
        )
        # metadata.json is NOT written by the default flow target; the
        # separate metadata-generate target harvests it from the stage logs,
        # and works after a failed flow too. The sibling `metadata` target is
        # avoided on purpose — it also runs metadata-check, which gates on a
        # QoR rules file and fails deliberately-aggressive runs.
        self._shell(
            f"{prefix} && {knobs} make metadata-generate "
            f"DESIGN_CONFIG={design_config} {overrides}"
        )

        log_tail = log.strip().splitlines()[-self.LOG_TAIL_LEN :]
        nickname = self._design_nickname(config.design)
        meta_ok, meta_raw = self._shell(
            f"cat {flow}/reports/nangate45/{nickname}/base/metadata.json"
        )
        if not meta_ok:
            result = RunResult(
                config=config,
                reached_stage="unknown",
                metrics=Metrics(
                    stage="unknown", wns_ns=0.0, tns_ns=0.0, congestion=0.0,
                    cell_count=0, area_um2=0.0, power_mw=0.0,
                    congestion_limit=self.CONGESTION_LIMIT,
                ),
                ok=False,
                failure="flow produced no metadata.json",
                log_tail=log_tail,
            )
            self._metrics = result.metrics
            return result

        metrics = self._parse_metadata(json.loads(meta_raw), config)
        result = RunResult(
            config=config,
            reached_stage=metrics.stage,
            metrics=metrics,
            ok=flow_ok,
            failure=None if flow_ok else "ORFS flow returned non-zero",
            log_tail=log_tail,
        )
        self._metrics = metrics
        self._store_cached(config, result)
        return result

    def _parse_metadata(self, data: dict, config: Optional[FlowConfig] = None) -> Metrics:
        def num(key: str) -> float:
            value = data.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        prefixes = {key.split("__")[0] for key in data}
        reached = [s for s in STAGES_ORFS if s in prefixes]
        fmax = self.fmax_fields(data, config.design if config is not None else None)
        return Metrics(
            stage=reached[-1] if reached else "unknown",
            wns_ns=num(self.KEY_MAP["wns_ns"]),
            tns_ns=num(self.KEY_MAP["tns_ns"]),
            congestion=num(self.KEY_MAP["congestion"]),
            cell_count=int(num(self.KEY_MAP["cell_count"])),
            area_um2=num(self.KEY_MAP["area_um2"]),
            power_mw=num(self.POWER_KEY) * 1000.0,
            die_area_um2=num(self.KEY_MAP["die_area_um2"]),
            core_area_um2=num(self.KEY_MAP["core_area_um2"]),
            drc_errors=int(num(self.KEY_MAP["drc_errors"])),
            **fmax,
            timing_threshold_ns=(
                self.timing_threshold_ns(config.design, self.effective_clock_ns(config))
                if config is not None else 0.0
            ),
            congestion_limit=self.CONGESTION_LIMIT,
        )

    def fmax_fields(self, data: dict, design: Optional[str]) -> dict:
        """cp_delay/fmax from the REAL clock's per-clock key (its period_min,
        i.e. reg-to-reg), plus ORFS's headline fmax as a separate labelled
        field. With no real clock or no per-clock key, cp_delay is None with a
        note; the headline is never used as a fallback."""
        def hz(key: str) -> Optional[float]:
            try:
                value = float(data[key])
            except (KeyError, TypeError, ValueError):
                return None
            return value if value > 0 and value != float("inf") else None

        per_clock = {key[len(self.FMAX_CLOCK_PREFIX):]: hz(key)
                     for key in data if key.startswith(self.FMAX_CLOCK_PREFIX)}
        headline = hz(self.FMAX_KEY)
        out = {
            "cp_delay_ns": None,
            "fmax_clock": None,
            "cp_delay_note": None,
            "orfs_fmax_headline_hz": headline,
            "orfs_fmax_headline_clock": next(
                (c for c, f in per_clock.items() if headline and f == headline), None),
        }
        clock = self.real_clock_name(design) if design else None
        if clock is None:
            out["cp_delay_note"] = "no single real (non-virtual) clock in the design's SDC"
        elif per_clock.get(clock) is None:
            out["cp_delay_note"] = f"metadata has no {self.FMAX_CLOCK_PREFIX}{clock}"
        else:
            out.update(cp_delay_ns=1e9 / per_clock[clock], fmax_clock=clock)
        return out


def make_adapter(backend: Optional[str] = None) -> EDAInterface:
    """Backend selection seam. Mock stays the default so tests, dev, and the
    offline web demo keep working with no environment set."""
    name = (backend or os.environ.get("ADAPTIVE_IICPILOT_BACKEND", "mock")).strip().lower()
    if name == "orfs":
        return ORFSAdapter()
    return MockORFSAdapter()
