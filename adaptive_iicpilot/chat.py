"""Conversational agent layer: a local LLM drives the DSE/EDA/memory stack.

This is the "User Proxy + Monitor replaced by an LLM" deliverable. The model
does not contain any design logic — it only decides which of four tools to
call and how to explain the result. Every number it reports comes back from a
tool; the system prompt forbids inventing metrics.

Nothing underneath is modified: the tools wrap the existing Orchestrator,
DSEAgent, EDAAgent and memory as-is.

Backend: the real ORFS cache when a config is already cached (instant replay);
for a fast design (LIVE_DESIGNS, e.g. gcd) a live ORFS run that streams its
log; otherwise the mock, clearly labelled — an interactive prompt must never
block on a 10-30 minute flow. ADAPTIVE_IICPILOT_CACHE_ONLY=1 disables live runs.

Run with `ollama serve` up and llama3.1:8b pulled:
    python -m adaptive_iicpilot.chat
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
import time
from dataclasses import asdict
from typing import Any, Callable, Optional

from .eda_interface import ORFS_UTIL_LADDER, MockORFSAdapter, ORFSAdapter
from .estimator import CalibratedEstimator
from .types import FlowConfig, Proposal

# Designs whose full ORFS flow is short enough (gcd: ~80 s) to run live from
# a chat turn when the config is uncached. Everything else still falls back
# to the mock, because ibex/aes take 10-30 minutes a run.
LIVE_DESIGNS = frozenset({"gcd"})


def _live_runs_allowed(design: str) -> bool:
    """Setting ADAPTIVE_IICPILOT_CACHE_ONLY (to anything but 0/false/no)
    forbids live flows entirely, e.g. for a demo box without ORFS."""
    cache_only = os.environ.get("ADAPTIVE_IICPILOT_CACHE_ONLY", "").strip().lower()
    if cache_only not in ("", "0", "false", "no"):
        return False
    return design in LIVE_DESIGNS


# ORFS names every step <N>_<M>_<step>, and the leading digit is the flow
# stage. Verified against real make output: steps 2+ announce themselves as
# "scripts/flow.sh 2_1_floorplan floorplan" / "Running floorplan.tcl, stage
# 2_1_floorplan"; yosys (stage 1) only shows up via its logs/<plat>/<design>/
# <variant>/1_... path on the synth.sh line.
LIVE_STAGES = ("synth", "floorplan", "place", "cts", "route", "finish")
_STEP_LOG_RE = re.compile(
    r"(?:flow\.sh |, stage |logs/[^/\s]+/[^/\s]+/[^/\s]+/)([1-6])_\d+_"
)


def _mmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class _LiveRunTracker:
    """Condenses a live flow's raw make output for the chat.

    Every raw line goes to `raw_log` (the JSONL frontend points it at stderr,
    which the extension mirrors into its Output channel). The chat itself only
    sees one "[EDA] <stage> started (mm:ss)" line per stage transition, a
    status callback for the running indicator, and a final summary.
    """

    def __init__(
        self,
        emit: Callable[[str], None],
        on_status: Callable[[dict], None],
        raw_log: Optional[Callable[[str], None]],
    ) -> None:
        self.emit = emit
        self.on_status = on_status
        self.raw_log = raw_log
        self.stage: Optional[str] = None
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def _status(self, state: str) -> None:
        self.on_status(
            {"state": state, "stage": self.stage or "starting",
             "elapsed_s": round(self.elapsed(), 1)}
        )

    def start(self) -> None:
        self.started = time.monotonic()
        self._status("running")

    def __call__(self, line: str) -> None:
        if self.raw_log is not None:
            self.raw_log(line)
        match = _STEP_LOG_RE.search(line)
        if not match:
            return
        stage = LIVE_STAGES[int(match.group(1)) - 1]
        # Forward only: a later step can mention an earlier step's log.
        if self.stage is not None and LIVE_STAGES.index(stage) <= LIVE_STAGES.index(self.stage):
            return
        self.stage = stage
        self.emit(f"[EDA] {stage} started ({_mmss(self.elapsed())})")
        self._status("running")

    def finish(self, result) -> None:
        took = _mmss(self.elapsed())
        if result is None:
            self.emit(f"[EDA] live run aborted after {took}")
        elif result.ok:
            self.emit(f"[EDA] live run finished in {took}: reached {result.reached_stage}")
        else:
            self.emit(
                f"[EDA] live run FAILED after {took} at {result.reached_stage}: {result.failure}"
            )
        self._status("done")


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
MAX_TOOL_HOPS = 4

# ANSI labels per backend actor, so a tool's internal chatter reads like a
# transcript rather than a log dump.
_TAG_COLORS = {
    "Control": "37",
    "EDA": "36",
    "DSE": "33",
    "Memory": "32",
    "LLM": "35",
    "Tool": "34",
    "Floorplan": "95",
    "Signoff": "91",
}

SYSTEM_PROMPT = """\
You are the interface agent for an IC backend-design system. Underneath you \
are a DSE agent (chooses physical-design parameters) and an EDA agent (runs \
the OpenROAD flow). You do not design anything yourself; you call tools.

Rules you must follow:
- NEVER state a metric you did not get from a tool result. Do not estimate, \
guess, or recall numbers. If you lack data, call a tool or say you need to run one.
- Tool results contain a "verdict" or "key_findings" field that already states \
the conclusion. Repeat that conclusion; do NOT re-derive it by comparing numbers \
yourself, and never contradict it.
- Use run_flow to run a single configuration. Use optimize to search for the \
best configuration. Use query_memory to report what has been learned. Use \
explain to answer "why" questions from data already gathered this session. Use floorplan for die shape, aspect ratio, I/O pin placement or macro placement questions; it returns a ranked table of real measured floorplans. Use signoff for DRC, antenna or LVS questions.
- congestion > 1.0 means the design is CONGESTED (unroutable in practice). \
congestion <= 1.0 is clean.
- The objective is the SMALLEST die area (die_area_um2) that stays at \
congestion <= 1.0. Utilization is the main knob: higher utilization shrinks \
the die but raises congestion.
- A small negative WNS is normal for this design and is NOT a failure.
- ibex is the demo design. Default to it when the user does not say otherwise.
- Keep replies short and concrete: a couple of sentences, real numbers, units. \
No bullet-point essays, no restating these rules.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_flow",
            "description": (
                "Run ONE physical-design flow configuration and return its real "
                "metrics (die area, congestion, WNS, power). Use when the user "
                "names a specific utilization or asks to run/try one config."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "design": {"type": "string", "description": "Design name, e.g. ibex"},
                    "util": {
                        "type": "number",
                        "minimum": 20,
                        "maximum": 95,
                        "description": (
                            "Core utilization as a PERCENTAGE between 20 and 95, "
                            "e.g. 80 for 80%. Never a fraction like 0.8."
                        ),
                    },
                    "density": {
                        "type": ["number", "null"],
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "Placement density between 0 and 1, and at least "
                            "util/100 + 0.02. Use null or 0 to let ORFS choose."
                        ),
                    },
                },
                "required": ["design", "util"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize",
            "description": (
                "Search for the best configuration: smallest die area with "
                "congestion <= 1.0. Returns a run report including how many flow "
                "runs it took and whether memory from a previous session was "
                "used. Use when the user asks to optimize, improve, or minimize."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "design": {"type": "string", "description": "Design name, e.g. ibex"},
                    "max_iters": {
                        "type": "integer",
                        "description": "Maximum search steps, default 6",
                    },
                    "signoff": {"type": "boolean",
                                "description": "true to run signoff (DRC) on the final best config"},
                    "objective": {
                        "type": "string",
                        "enum": ["die_area", "power"],
                        "description": (
                            "What to minimise: die_area (default) or power. "
                            "Both keep congestion at or below 1.0."
                        ),
                    },
                },
                "required": ["design"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tradeoff",
            "description": (
                "Return the design's real measured table (utilization, die area, "
                "power, congestion, WNS). Runs no flow. Use for any what-if or "
                "comparison question, such as whether a bigger die lowers power."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "design": {"type": "string", "description": "Design name, e.g. ibex"},
                },
                "required": ["design"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_memory",
            "description": (
                "Report what the system has learned and remembered for a design: "
                "the best known configuration and whether memory is warm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "design": {"type": "string", "description": "Design name, e.g. ibex"},
                },
                "required": ["design"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain",
            "description": (
                "Return the measured data gathered so far (runs this session plus "
                "previously measured configurations) so you can answer a 'why' or "
                "'what changed' question. Runs no flow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "What the user is asking about, e.g. 'why did util 90 congest'",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "floorplan",
            "description": (
                "Compare floorplans for a design: core aspect ratio and margin, I/O pin "
                "placement strategies, and hard-macro placement (automatic RTL-MP with a "
                "halo sweep vs manual periphery / edge-cluster). Returns a ranked table of "
                "real ORFS measurements (die/core area, congestion, IO HPWL, placement "
                "wirelength, WNS for full runs) and shows the best floorplan's picture."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "design": {"type": "string", "description": "Design name, e.g. ibex or tinyRocket"},
                    "objective": {
                        "type": "string",
                        "enum": ["balanced", "congestion", "wirelength", "io_hpwl", "area", "timing"],
                        "description": "What to rank by; default balanced.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["best", "compare", "config"],
                        "description": (
                            "best: the best measured floorplan with its OpenROAD renders "
                            "(default when no strategies are given); compare: ranked comparison; "
                            "config: the measurement of the exact util/aspect/margin/clock given "
                            "(default when any of those is given)."
                        ),
                    },
                    "util": {"type": ["number", "null"], "description": "Core utilization in percent, e.g. 85"},
                    "aspect": {"type": ["number", "null"], "description": "Core aspect ratio (height/width), e.g. 0.5"},
                    "margin": {"type": ["number", "null"], "description": "Core margin in um"},
                    "clock_ns": {"type": ["number", "null"], "description": "Clock period in ns; default the stock clock"},
                    "run": {"type": ["string", "null"], "enum": ["screen", "full", None],
                            "description": ("Only after the user confirmed an OFFER for this exact config: "
                                            "run its screen or full flow. Ignored otherwise.")},
                    "strategies": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "string",
                            "enum": ["aspect", "margin", "pins", "macros_auto", "macros_manual"],
                        },
                        "description": (
                            "Which studies to compare. Omit for the default: aspect+pins "
                            "for designs without macros, macros_auto+macros_manual otherwise."
                        ),
                    },
                },
                "required": ["design"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoff",
            "description": (
                "Physical signoff of a measured run: KLayout FreePDK45 DRC on its 6_final.gds "
                "(violations per rule category), the router's DRT-0199 count and ANT-0001/0002 "
                "antenna counts; LVS is not available. Default: the best measured floorplan. "
                "Use for 'is it DRC clean', 'run DRC', antenna or signoff questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "design": {"type": "string", "description": "Design name, e.g. ibex"},
                    "util": {"type": ["number", "null"], "description": "Utilization of a specific run"},
                    "aspect": {"type": ["number", "null"], "description": "Aspect ratio of a specific run"},
                    "clock_ns": {"type": ["number", "null"], "description": "Clock of a specific run"},
                    "run": {"type": "boolean",
                            "description": "true to run DRC (~5 min) when no stored result exists"},
                },
                "required": ["design"],
            },
        },
    },
]


AGENTS = ("Control", "DSE", "EDA", "Memory", "Tool", "Verdict", "Floorplan", "Signoff")

# Canonical provenance tokens. Every tool result that produces metrics carries
# one, so the UI can label a number's origin without trusting the model to
# mention it.
REAL_ORFS = "REAL_ORFS"
CACHED_ORFS = "CACHED_ORFS"
ESTIMATED_MOCK = "ESTIMATED_MOCK"
# Fitted from this design's own cached ORFS measurements.
ESTIMATED_FROM_REAL = "ESTIMATED_FROM_REAL"
# The design has real data, but none at the requested clock. Reported as
# unknown rather than fitted from another clock's measurements.
UNKNOWN_AT_CLOCK = "UNKNOWN_AT_CLOCK"

# Objectives optimize() can be asked for, both under the congestion <= 1.0 gate.
OBJECTIVES = ("die_area", "power", "timing")

# Supported utilization window. Below 20% the die is mostly empty and the flow
# stops being informative; above 95% nothing legalises.
UTIL_MIN = 20.0
UTIL_MAX = 95.0
# Clock bounds, as multiples of the design's own stock period.
CLOCK_MIN_FACTOR = 0.5
CLOCK_MAX_FACTOR = 2.0
# Nothing was executed (input validation refused the configuration).
NOT_RUN = "NOT_RUN"


def _terminal_emit(line: str) -> None:
    """Print a backend line with its actor tag colorized."""
    tag = ""
    if line.startswith("[") and "]" in line:
        tag = line[1 : line.index("]")]
    color = _TAG_COLORS.get(tag)
    if color:
        rest = line[line.index("]") + 1 :]
        print(f"  \033[1;{color}m[{tag}]\033[0m\033[{color}m{rest}\033[0m", flush=True)
    else:
        print(f"  {line}", flush=True)


def _split_log_line(line: str) -> tuple[str, str]:
    """Split an "[Agent] text" backend line into its agent and message."""
    if line.startswith("[") and "]" in line:
        tag = line[1 : line.index("]")]
        if tag in AGENTS:
            return tag, line[line.index("]") + 1 :].strip()
    return "Control", line.strip()


class _AgentInterface:
    """Duck-types web.compat.ConsoleInterface for the Orchestrator.

    `decide` is the existing HITL seam. With no handler it auto-accepts, which
    is the default and what the terminal REPL uses; the JSONL frontend installs
    a handler that surfaces the proposal and blocks for a human answer. Either
    way the Orchestrator still wraps this, so an edit is recorded as a
    correction in memory exactly as before.
    """

    def __init__(self, emit: Callable[[str], None], decide_handler=None) -> None:
        self.mode = "auto" if decide_handler is None else "hitl"
        self.emit = emit
        self._decide_handler = decide_handler

    def decide(self, proposal: Proposal, config: FlowConfig) -> Optional[Proposal]:
        if self._decide_handler is None:
            return proposal
        return self._decide_handler(proposal, config)


def _as_int(value: Any, default: int) -> int:
    """Small local models routinely send numbers as strings despite the
    schema, so every numeric argument is coerced rather than trusted."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timing_phrase(metrics) -> str:
    """Never let a negative-slack run read as "met"."""
    status = metrics.timing_status
    # fmax is the real clock's period_min, i.e. reg-to-reg only; WNS covers
    # every path group. Labelled so neither is mistaken for the other.
    fmax = f", reg-to-reg fmax {metrics.fmax_mhz:.1f} MHz" if metrics.fmax_mhz else ""
    if status == "MET":
        return f"timing MET: WNS {metrics.wns_ns:+.4f} ns{fmax}"
    limit = metrics.timing_threshold_ns
    return (
        f"timing {status}: WNS {metrics.wns_ns:+.4f} ns, "
        f"ORFS limit {limit:+.4f} ns{fmax}"
    )


def _verdict(metrics) -> str:
    """A ready-made sentence about congestion. An 8B model compares floats
    unreliably (it read 0.97 as 'above 1.0'), so it is given the conclusion
    to copy instead of the comparison to perform."""
    if metrics.congested:
        return (
            f"CONGESTED - congestion {metrics.congestion:.4f} is ABOVE the 1.0 "
            f"limit, so this configuration is not usable | {_timing_phrase(metrics)}"
        )
    return (
        f"CLEAN - congestion {metrics.congestion:.4f} is BELOW the 1.0 limit, "
        f"so this configuration is routable | {_timing_phrase(metrics)}"
    )


FMAX_KIND = "reg-to-reg fmax (real clock period_min)"


def _config_label(config: FlowConfig) -> str:
    from .configkey import from_flow_config

    return from_flow_config(config, None).label(with_clock=False)


def _fmax_phrase(row: dict) -> str:
    """Reg-to-reg fmax of a table row; a run whose metadata no longer has the
    real clock's key says so instead of printing None."""
    if row.get("fmax_mhz"):
        return f"reg-to-reg fmax {row['fmax_mhz']} MHz"
    return "reg-to-reg fmax not measured (no real-clock key survives for this run)"


def _usable(row: dict) -> bool:
    """A measured point the objectives may choose: finished, routable and
    timing not VIOLATED (ORFS's own per-design allowance)."""
    return (bool(row.get("ok")) and not row.get("congested") and (row.get("die_area_um2") or 0) > 0
            and row.get("timing_status") in ("MET", "ACCEPTABLE"))


def _row_key(design: str, row: dict):
    from .configkey import ConfigKey

    return ConfigKey(
        design=design, tech="nangate45", util=row["util"], density=0.0,
        aspect=row.get("aspect_ratio", 1.0), margin=row.get("core_margin"),
        pin_strategy=row.get("pin_strategy") or "default",
        macro_strategy=row.get("macro_strategy") or "stock", clock_ns=row["clock_ns"],
    )


def _local_path(path: str) -> str:
    """Render paths are recorded as WSL paths; a Windows interpreter reads
    the same file through the drive letter."""
    if os.name == "nt" and path.startswith("/mnt/") and len(path) > 6:
        return f"{path[5].upper()}:{path[6:]}".replace("/", "\\")
    return path


def _read_png(path: Optional[str]) -> Optional[str]:
    import base64

    if not path:
        return None
    try:
        with open(_local_path(path), "rb") as handle:
            return base64.b64encode(handle.read()).decode("ascii")
    except OSError:
        return None


def _floorplan_summary(stage: Optional[dict]) -> Optional[dict]:
    """What the pipeline's floorplan stage decided, for the tool result."""
    if not stage:
        return None
    choice = stage.get("choice") or {}
    return {
        "applied": stage.get("applied", False),
        "floorplan": choice.get("label") if stage.get("applied") else "stock",
        "recommended": choice.get("label"),
        "basis": choice.get("basis"),
        "reason_if_not_applied": None if stage.get("applied") else stage.get("reason"),
    }


def _metrics_summary(metrics) -> dict:
    return {
        "verdict": _verdict(metrics),
        "timing_status": metrics.timing_status,
        "timing_threshold_ns": round(metrics.timing_threshold_ns, 4),
        "fmax_mhz": round(metrics.fmax_mhz, 1) if metrics.fmax_mhz else None,
        "fmax_kind": "reg-to-reg fmax (real clock period_min)",
        "die_area_um2": round(metrics.die_area_um2, 1),
        "core_area_um2": round(metrics.core_area_um2, 1),
        "instance_area_um2": round(metrics.area_um2, 1),
        "congestion": round(metrics.congestion, 4),
        "congested": metrics.congested,
        "wns_ns": round(metrics.wns_ns, 4),
        "tns_ns": round(metrics.tns_ns, 4),
        "power_mw": round(metrics.power_mw, 3),
        "cell_count": metrics.cell_count,
    }


class Session:
    """Holds conversation history, the shared memory instance (so a second
    optimize in the same chat is warm), and every measured datapoint the
    session has seen — which is what `explain` grounds its answers in."""

    def __init__(
        self,
        emit: Optional[Callable[[str], None]] = None,
        decide_handler=None,
        on_report: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[dict], None]] = None,
        raw_log: Optional[Callable[[str], None]] = None,
        on_image: Optional[Callable[[dict], None]] = None,
    ) -> None:
        from .sqlite_memory import make_memory

        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Persistent by default: what this chat learns is still there after the
        # REPL is closed and relaunched.
        self.memory = make_memory()
        self.emit = emit or _terminal_emit
        self.on_report = on_report or (lambda report: None)
        # Live-run progress ({"state", "stage", "elapsed_s"}) and the raw flow
        # log; the terminal REPL leaves both unset and just sees stage lines.
        self.on_status = on_status or (lambda status: None)
        self.raw_log = raw_log
        # A rendered picture ({"title", "caption", "svg", "path"}): the JSONL
        # frontend turns it into an image card; the terminal just names the file.
        self.on_image = on_image or (
            lambda image: self.emit(f"[Floorplan] picture: {image.get('path')}")
        )
        self.interface = _AgentInterface(self.emit, decide_handler)
        self.runs: list[dict] = []
        self.last_report: Optional[dict] = None
        self.last_verdict: Optional[dict] = None
        self.last_record: Optional[dict] = None
        self._last_tradeoff: Optional[dict] = None
        self._verdicts_shown: set = set()
        # An offer to run an unmeasured config ({"design", "knobs", "choices"})
        # lives for exactly one turn; the confirmed one is what lets a run go.
        self.pending_offer: Optional[dict] = None
        self._confirmed_offer: Optional[dict] = None

    def begin_turn(self) -> None:
        """Turn isolation: a verdict, record or picture caption from the
        previous turn must never be reported (or contradiction-checked) as this
        turn's result. History in self.messages is kept for follow-ups."""
        self.last_verdict = None
        self.last_record = None
        self._verdicts_shown = set()
        self._confirmed_offer = None

    def confirm_offer(self, offer: Optional[dict], choice: str) -> None:
        self._confirmed_offer = {**offer, "run": choice} if offer else None

    def _run_confirmed(self, design: str, knobs: dict, run: Optional[str]) -> Optional[str]:
        """run as asked if it is the confirmed offer for this exact config,
        else None: a model-supplied run= never starts a flow on its own."""
        if run is None:
            return None
        c = self._confirmed_offer
        if c and c["design"] == design and c["knobs"] == knobs and c["run"] == run and run in c["choices"]:
            return run
        self.emit(f"[Control] floorplan run={run!r} ignored: not a confirmed offer for this config")
        return None

    def _frame_verdict(self, verdict: Optional[str]) -> Optional[str]:
        """A verdict line goes on the first picture of a turn that carries it,
        not on every view of the same result."""
        if not verdict or verdict in self._verdicts_shown:
            return None
        self._verdicts_shown.add(verdict)
        return verdict

    # --- backend selection ---

    def _pick_adapter(
        self, config: FlowConfig
    ) -> tuple[Any, str, str, Optional[_LiveRunTracker]]:
        """Returns (adapter, canonical source token, human detail, tracker)."""
        orfs = ORFSAdapter()
        if orfs.is_cached(config):
            return orfs, CACHED_ORFS, "real ORFS run (cached)", None
        if _live_runs_allowed(config.design):
            # The result is cached on completion, so this config replays
            # instantly from then on.
            tracker = _LiveRunTracker(self.emit, self.on_status, self.raw_log)
            live = ORFSAdapter(on_line=tracker)
            return live, REAL_ORFS, "real ORFS run (live, uncached)", tracker
        return (
            MockORFSAdapter(),
            ESTIMATED_MOCK,
            "MOCK model (this config has no real cached run)",
            None,
        )

    def _ladder_cached(self, design: str) -> bool:
        orfs = ORFSAdapter()
        return all(
            orfs.is_cached(
                FlowConfig(design=design, core_utilization=util, placement_density=0.0)
            )
            for util in ORFS_UTIL_LADDER
        )

    def _try_estimate(self, config: FlowConfig) -> Optional[dict]:
        """Estimate from the design's own real measurements, when we have them.

        Only reached for a config that is neither cached nor eligible for a live
        run. Preferred over the mock because the mock is calibrated against a
        paper rather than this ORFS build. Nothing is written to memory: an
        estimate is not a measurement and must not become one.
        """
        orfs = ORFSAdapter()
        if orfs.is_cached(config) or _live_runs_allowed(config.design):
            return None
        if config.placement_density:
            # The fit is built from the auto-density ladder only.
            return None

        clock_ns = orfs.effective_clock_ns(config)
        from .configkey import from_flow_config

        estimator = CalibratedEstimator.from_cache(
            config.design, orfs, clock_ns=clock_ns,
            floorplan_key=from_flow_config(config, clock_ns).floorplan_key,
        )
        if not estimator.usable:
            measured = CalibratedEstimator.measured_clocks(config.design, orfs)
            if not measured:
                return None  # no real data at all; the mock is the honest fallback
            # Real data exists, but not at this clock. Area and power depend on
            # the clock through cell sizing and buffering, so another clock's
            # fit would be a confident guess at something unmeasured.
            message = (
                f"no measurements for {config.design} at {clock_ns:g}ns; measured "
                f"clocks are {', '.join(f'{c:g}ns' for c in measured)}. Run this "
                f"configuration to measure it, or ask about a measured clock."
            )
            self.emit(f"[Control] {message}")
            verdict = f"UNKNOWN - {message}"
            self.emit(f"[Verdict] {verdict} | source: {UNKNOWN_AT_CLOCK}")
            self.last_verdict = {
                "verdict": verdict, "source": UNKNOWN_AT_CLOCK, "objective": None,
            }
            self.last_record = None
            return {
                "design": config.design,
                "util": config.core_utilization,
                "clock_ns": clock_ns,
                "source": UNKNOWN_AT_CLOCK,
                "verdict": verdict,
                "measured_clocks_ns": measured,
                "die_area_um2": None,
                "power_mw": None,
                "congestion": None,
            }

        util = config.core_utilization
        summary = estimator.estimate(util)
        low, high = estimator.util_range
        self.emit(
            f"[Control] run_flow design={config.design} util={util} density=auto"
        )
        self.emit(
            f"[Control] backend: estimated from {estimator.estimated_point_count} real "
            f"cached runs ({low:g}-{high:g}% utilization); no flow was run"
        )
        if "range_note" in summary:
            self.emit(f"[Control] {summary['range_note']}")

        record = {
            "design": config.design,
            "util": util,
            "density": None,
            "source": ESTIMATED_FROM_REAL,
            "source_detail": (
                f"fitted from {estimator.estimated_point_count} real cached ORFS runs "
                f"({low:g}-{high:g}% utilization)"
            ),
            "ok": True,
            "reached_stage": "estimated",
            **summary,
        }
        self.on_report(
            {
                "flow_runs": 0,
                "seeded": None,
                "source": ESTIMATED_FROM_REAL,
                "source_detail": record["source_detail"],
                "best": summary,
            }
        )
        self._note_verdict(record)
        self.runs.append(record)
        return record

    def _power_vs_die_area_best(
        self, design: str, objective: str, chosen: Optional[dict]
    ) -> Optional[dict]:
        """How much power the power-optimal config actually saves.

        Utilization is a weak power knob: across the whole measured ibex ladder
        power moves well under a percent, so "optimised for power" can sound
        like a win while being noise. When the saving is under 1%, say so and
        point at the knob that does move power — the clock period, which lives
        in constraint.sdc and is deliberately not swept here.
        """
        if objective != "power" or not chosen:
            return None
        die_best, _ = self._select_by_objective(
            design, "die_area", table=self._last_tradeoff
        )
        if not die_best or not die_best.get("power_mw"):
            return None

        baseline = die_best["power_mw"]
        delta = chosen["power_mw"] - baseline
        percent = 100.0 * delta / baseline
        summary = (
            f"power {chosen['power_mw']:g} mW vs {baseline:g} mW at the die-area "
            f"best (util {die_best['util']:g}%): {percent:+.2f}%"
        )

        # Power bought by relaxing the clock costs frequency. Stating only the
        # saving would be a half-truth, so both sides travel together.
        freq_note = None
        chosen_clock, base_clock = chosen.get("clock_ns"), die_best.get("clock_ns")
        if chosen_clock and base_clock and abs(chosen_clock - base_clock) > 1e-9:
            clock_pct = 100.0 * (chosen_clock - base_clock) / base_clock
            freq_note = (
                f"clock relaxed {base_clock:g}ns -> {chosen_clock:g}ns "
                f"({clock_pct:+.1f}%), so the design runs correspondingly slower"
            )
            if chosen.get("fmax_mhz") and die_best.get("fmax_mhz"):
                freq_note += (
                    f"; reg-to-reg fmax {die_best['fmax_mhz']:g} -> {chosen['fmax_mhz']:g} MHz"
                )
            summary += f"; {freq_note}"

        negligible = abs(percent) < 1.0
        if negligible:
            summary += (
                "; difference is negligible (<1%) at this clock; "
                "the clock period is the real power knob"
            )
        return {
            "summary": summary,
            "frequency_note": freq_note,
            "clock_ns": chosen_clock,
            "baseline_clock_ns": base_clock,
            "percent": round(percent, 3),
            "delta_mw": round(delta, 4),
            "baseline_mw": baseline,
            "baseline_util": die_best["util"],
            "negligible": negligible,
        }

    def _timing_vs_die_area_best(
        self, design: str, objective: str, chosen: Optional[dict]
    ) -> Optional[dict]:
        """What the fastest measured clock on the best floorplan buys, and what
        it costs, against the die-area best at the stock clock. Also names the
        tighter clocks that were measured and failed, so "fastest" is never
        read as "nothing faster was tried"."""
        if objective != "timing" or not chosen:
            return None
        die_best, _ = self._select_by_objective(design, "die_area", table=self._last_tradeoff)
        if not die_best:
            return None
        rows = (self._last_tradeoff or {}).get("measured_points", [])
        tighter = sorted(
            [r for r in rows if r["clock_ns"] and r["clock_ns"] < chosen["clock_ns"] - 1e-9
             and not _usable(r)],
            key=lambda r: r["clock_ns"],
        )
        same_point = abs((chosen["clock_ns"] or 0) - (die_best["clock_ns"] or 0)) < 1e-9
        summary = (
            f"fastest measured on {chosen['floorplan']}: {chosen['clock_ns']:g} ns, reg-to-reg fmax "
            f"{chosen['fmax_mhz']} MHz, timing {chosen['timing_status']} (WNS {chosen['wns_ns']:+.4f} ns, "
            f"limit {chosen['timing_threshold_ns']:+.3f} ns), power {chosen['power_mw']:g} mW"
        )
        if same_point:
            summary += "; that is the stock clock - no measured clock on this floorplan is faster and usable"
        else:
            delta = 100.0 * (chosen["power_mw"] - die_best["power_mw"]) / die_best["power_mw"]
            summary += (f"; vs stock {die_best['clock_ns']:g} ns: reg-to-reg fmax {die_best['fmax_mhz']} -> "
                        f"{chosen['fmax_mhz']} MHz, power {die_best['power_mw']:g} -> {chosen['power_mw']:g} mW "
                        f"({delta:+.1f}%)")
        if tighter:
            summary += "; tighter clocks measured but not usable: " + ", ".join(
                f"{r['clock_ns']:g} ns on {r['floorplan']} ({'congested' if r['congested'] else r['timing_status']})"
                for r in tighter[:4]
            )
        if chosen["timing_status"] != "MET":
            met = [r for r in rows if _usable(r) and r["floorplan"] == chosen["floorplan"]
                   and r["timing_status"] == "MET" and r.get("fmax_mhz")]
            if met:
                fastest_met = max(met, key=lambda r: r["fmax_mhz"])
                summary += (f"; fastest that actually MEETS timing: {fastest_met['clock_ns']:g} ns, "
                            f"reg-to-reg fmax {fastest_met['fmax_mhz']} MHz")
        return {"summary": summary, "clock_ns": chosen["clock_ns"], "fmax_mhz": chosen["fmax_mhz"],
                "timing_status": chosen["timing_status"], "baseline_fmax_mhz": die_best.get("fmax_mhz")}

    def _show_renders(self, design: str, row: Optional[dict], title: str, verdict: str,
                      views: tuple = ("placement", "congestion")) -> list:
        """Send the OpenROAD PNG renders of a measured point to the UI.

        A point measured only by a clock sweep has no renders of its own; the
        same floorplan's stock-clock run does, and the caption says so."""
        if not row:
            return []
        renders = row.get("renders") or {}
        note = ""
        if not renders:
            for other in (self._last_tradeoff or {}).get("measured_points", []):
                if other["floorplan"] == row["floorplan"] and other.get("renders"):
                    renders = other["renders"]
                    note = f" (layout rendered from the {other['clock_ns']:g} ns run of this floorplan)"
                    break
        shown = []
        for view in views:
            path = renders.get(view)
            data = _read_png(path) if path else None
            if data is None:
                continue
            self.on_image({
                "title": f"{design}: {title} - {view}",
                "caption": f"{row['floorplan']} at {row['clock_ns']:g} ns{note}",
                "png": data,
                "path": path,
                "verdict": self._frame_verdict(verdict),
            })
            shown.append(path)
        return shown

    def _reject(
        self, design: str, util: float, density: Optional[float], message: str
    ) -> dict:
        """Refuse a configuration before any flow runs."""
        self.emit(f"[Verdict] REJECTED - {message} | source: {NOT_RUN}")
        # A rejection has no metrics, but the model has been seen to narrate one
        # as "CONGESTED" anyway, so it still needs to be the authoritative last
        # word for the contradiction check.
        self.last_verdict = {
            "verdict": f"REJECTED - {message}",
            "source": NOT_RUN,
            "objective": None,
        }
        # Nothing ran, so there is no record to summarise from.
        self.last_record = None
        return {
            "rejected": True,
            "error": message,
            "design": design,
            "util": util,
            "density": density,
        }

    def _select_by_objective(
        self, design: str, objective: str, table: Optional[dict] = None
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Best real measured point for an objective, across every knob.

          die_area: smallest die at the stock clock over utilization AND aspect
                    ratio (and any measured floorplan), congestion <= 1.0, timing
                    not VIOLATED.
          power / timing: the CLOCK, applied on that best floorplan: lowest
                    power, or highest reg-to-reg fmax, among the floorplan's
                    usable measured clocks.

        Returns (row, full metrics summary). The summary is read back from the
        measurement itself, so the headline numbers are that configuration's.
        """
        table = table if table is not None else self.tradeoff(design)
        usable = [row for row in table["measured_points"] if _usable(row)]
        if not usable:
            return None, None
        stock = _stock_clock_ns(design)
        at_stock = [
            r for r in usable if stock is None or abs((r["clock_ns"] or 0.0) - stock) < 1e-9
        ] or usable
        die_best = min(at_stock, key=lambda r: (r["die_area_um2"], r["congestion"]))
        row = die_best
        if objective in ("power", "timing"):
            same = [r for r in usable if r["floorplan"] == die_best["floorplan"]]
            if objective == "power":
                row = min(same, key=lambda r: r["power_mw"])
            else:
                with_fmax = [r for r in same if r.get("fmax_mhz")]
                row = max(with_fmax, key=lambda r: r["fmax_mhz"]) if with_fmax else die_best

        orfs = ORFSAdapter()
        cached = orfs._load_cached(_row_key(design, row).to_flow_config())
        summary = _metrics_summary(cached.metrics) if cached is not None else None
        return row, summary

    def _note_verdict(self, record: dict) -> None:
        """Record and announce the verdict without going through the model.

        The model is asked to repeat the verdict, but an 8B model sometimes
        inverts it, so the authoritative line is printed from Python and kept
        for the contradiction check at the end of the turn.
        """
        verdict = record.get("verdict")
        if not verdict:
            return
        source = record.get("source", "")
        self.last_verdict = {
            "verdict": verdict,
            "source": source,
            "objective": record.get("objective"),
        }
        self.last_record = record
        self.emit(f"[Verdict] {verdict} | source: {source}")

    # --- tools ---

    def run_flow(self, design: str = "ibex", util: float = 50.0,
                 density: Optional[float] = None,
                 clock_ns: Optional[float] = None) -> dict:
        from .eda_agent import EDAAgent

        util = _as_float(util, 50.0)
        density = None if density is None else _as_float(density, 0.0)
        clock_ns = None if clock_ns is None else _as_float(clock_ns, 0.0)

        problem = validate_knobs(util, density)
        if problem is None:
            problem = validate_clock(clock_ns, _stock_clock_ns(design))
        if problem:
            return self._reject(design, util, density, problem)

        config = FlowConfig(
            design=design,
            core_utilization=util,
            placement_density=0.0 if density is None else density,
            clock_period_ns=clock_ns or 0.0,
        )

        estimate = self._try_estimate(config)
        if estimate is not None:
            return estimate

        adapter, source, detail, tracker = self._pick_adapter(config)
        self.emit(f"[Control] run_flow design={design} util={util} density={density or 'auto'}")
        self.emit(f"[Control] backend: {detail}")

        agent = EDAAgent(adapter, memory=self.memory, emit=self.emit)
        if tracker is None:
            result = agent.run_flow(config, target_stage="route")
        else:
            if self.raw_log is not None:
                self.raw_log(f"=== live ORFS run: {design} util={util} "
                             f"density={density or 'auto'} ===")
            tracker.start()
            result = None
            try:
                result = agent.run_flow(config, target_stage="route")
            finally:
                # Always closes the running indicator, even if the run raised.
                tracker.finish(result)

        summary = _metrics_summary(result.metrics)
        if not result.ok:
            # Congestion alone would read "CLEAN" for a flow that died at CTS
            # (it is measured at global place), and the model is told to
            # repeat the verdict verbatim.
            summary["verdict"] = (
                f"FAILED - the flow stopped at {result.reached_stage} "
                f"({result.failure}), so this configuration is not usable"
            )
        self.on_report(
            {
                "flow_runs": 1,
                "seeded": None,
                "source": source,
                "source_detail": detail,
                "best": summary,
            }
        )
        record = {
            "design": design,
            "util": util,
            "density": density,
            "source": source,
            "source_detail": detail,
            "ok": result.ok,
            "reached_stage": result.reached_stage,
            **summary,
        }
        self._note_verdict(record)
        self.runs.append(record)
        return record

    def optimize(
        self, design: str = "ibex", max_iters: Any = 6, objective: str = "die_area",
        signoff: Any = False,
    ) -> dict:
        from .measurements import Measurements
        from .web.compat import Orchestrator, Task

        max_iters = _as_int(max_iters, 6)
        objective = (objective or "die_area").strip().lower()
        if objective not in OBJECTIVES:
            return {
                "error": (
                    f"unknown objective {objective!r}; valid objectives are: "
                    f"{', '.join(sorted(OBJECTIVES))}"
                ),
                "valid_objectives": sorted(OBJECTIVES),
            }
        live = _live_runs_allowed(design)
        # Real backend whenever the design has real measurements (from either
        # producer) or may run live; the mock only for an unmeasured design.
        use_orfs = live or bool(Measurements().points(design))
        orfs = ORFSAdapter()
        pre_cached = {
            util
            for util in ORFS_UTIL_LADDER
            if orfs.is_cached(
                FlowConfig(design=design, core_utilization=util, placement_density=0.0)
            )
        }
        if use_orfs:
            os.environ["ADAPTIVE_IICPILOT_BACKEND"] = "orfs"
            base = FlowConfig(design=design, core_utilization=ORFS_UTIL_LADDER[0],
                              placement_density=0.0)
            detail = ("real ORFS (screened pipeline: Control -> Floorplan screen -> DSE full flow on survivors; "
                      + ("live runs allowed" if live else "measured data only, no new flows") + ")")
        else:
            os.environ.pop("ADAPTIVE_IICPILOT_BACKEND", None)
            base = FlowConfig(design=design)
            detail = "MOCK model (no real ORFS measurement for this design)"

        problem = validate_knobs(base.core_utilization, base.placement_density)
        if problem:
            return self._reject(design, base.core_utilization,
                                base.placement_density, problem)

        self.emit(f"[Control] optimize design={design} objective={objective} max_iters={max_iters}")
        self.emit(f"[Control] backend: {detail}")

        def ladder_measured(config: FlowConfig) -> bool:
            # Fallback (non-screened) path only: a chosen floorplan is used
            # only if every rung the search can visit is already measured.
            return all(
                orfs.is_cached(config.tweaked(core_utilization=util, placement_density=0.0))
                for util in ORFS_UTIL_LADDER
            )

        orch = Orchestrator(memory=self.memory, interface=self.interface)
        report = orch.run(Task(
            design=design, base_config=base, max_iters=max_iters,
            objective="timing" if objective == "timing" else "die_area",
            floorplan_policy="apply_if_cached", floorplan_measured=ladder_measured,
            allow_screen_runs=live, allow_full_runs=live,
        ))
        pipeline = report.pipeline

        if not use_orfs:
            source = ESTIMATED_MOCK
        elif pipeline is not None:
            source = REAL_ORFS if (pipeline["screens_run"] or pipeline["full_runs_run"]) else CACHED_ORFS
        elif report.best.config.core_utilization in pre_cached:
            source = CACHED_ORFS
        else:
            source = REAL_ORFS

        memory_note = (
            f"WARM: memory was seeded from an earlier session"
            if report.seeded
            else "COLD: no prior memory for this design"
        )
        if pipeline is not None:
            memory_note += (f"; screened {pipeline['screens_total']} candidate(s), "
                            f"{pipeline['full_runs']} full flow(s)")
        else:
            memory_note += f"; the search took {report.flow_runs} flow runs"

        # The search optimises die area over util x aspect; power and timing
        # then pick the CLOCK on that floorplan from real measurements.
        self._last_tradeoff = self.tradeoff(design)
        chosen, chosen_summary = self._select_by_objective(
            design, objective, table=self._last_tradeoff
        )
        objective_note = f"objective: {objective} (congestion <= 1.0 gate, timing not VIOLATED)"
        if chosen:
            objective_note += (
                f"; best measured for {objective} is {chosen['floorplan']} at {chosen['clock_ns']:g} ns -> "
                f"die {chosen['die_area_um2']} um2, {chosen['power_mw']} mW, "
                f"reg-to-reg fmax {chosen['fmax_mhz']} MHz"
            )
        self.emit(f"[Control] {objective_note}")

        headline = chosen_summary or _metrics_summary(report.best.metrics)
        headline_util = chosen["util"] if chosen else report.best.config.core_utilization
        dse_util = report.best.config.core_utilization

        power_comparison = self._power_vs_die_area_best(design, objective, chosen)
        timing_comparison = self._timing_vs_die_area_best(design, objective, chosen)
        for comparison in (power_comparison, timing_comparison):
            if comparison:
                headline["verdict"] = f"{headline['verdict']} | {comparison['summary']}"
        if pipeline is not None and objective == "die_area":
            headline["verdict"] = f"{headline['verdict']} | pipeline {pipeline['summary']}"
        # Signoff: the FINAL best config only, never the candidates on the way.
        signoff_result = None
        if (signoff is True or str(signoff).strip().lower() in ("true", "1", "yes")) and chosen and use_orfs:
            agent = self._signoff_agent()
            try:
                signoff_result = agent.signoff(_row_key(design, chosen), run=True)
            finally:
                agent.memory.close()
            headline["verdict"] = f"{headline['verdict']} | signoff: {signoff_result['verdict']}"

        summary = {
            "design": design,
            "source": source,
            "source_detail": detail,
            "objective": objective,
            "objective_note": objective_note,
            "objective_best": chosen,
            "best_floorplan": chosen["floorplan"] if chosen else None,
            "clock_ns": chosen["clock_ns"] if chosen else None,
            "power_vs_die_area_best": power_comparison,
            "timing_vs_die_area_best": timing_comparison,
            "memory_note": memory_note,
            "seeded_from_memory": report.seeded,
            "pipeline": pipeline,
            "signoff": signoff_result,
            "screens_run": report.screens_run,
            "full_runs": report.full_runs,
            "time_saved_s": report.time_saved_s,
            "flow_runs": report.flow_runs,
            "failures": report.failures,
            "congestion_fights": report.congestion_fights,
            "best_utilization_percent": headline_util,
            "best_aspect_ratio": chosen["aspect_ratio"] if chosen else report.best.config.aspect_ratio,
            "best_density": report.best.config.placement_density or "auto",
            "die_area_optimal_util": dse_util,
            "floorplan": _floorplan_summary(report.floorplan),
            "steps_taken": [
                {"change": p.changes, "reason": p.triggered_by} for p in report.history
            ],
            **headline,
        }
        summary["renders"] = self._show_renders(
            design, chosen, f"optimize ({objective})", summary["verdict"]
        )
        self.runs.append({**summary, "util": headline_util})
        self.last_report = summary
        self.on_report(
            {
                "flow_runs": report.flow_runs,
                "seeded": report.seeded,
                "source": source,
                "source_detail": detail,
                "objective": objective,
                "best_utilization_percent": headline_util,
                "best": headline,
                "pipeline": pipeline,
            }
        )
        self._note_verdict(summary)
        return summary

    def tradeoff(self, design: str = "ibex") -> dict:
        """Every real measured full-flow point of the design, across every knob
        of the ConfigKey (util, aspect, margin, pins, macros, clock) and from
        both producers (ORFS result cache, FloorplanAgent full runs). No flow
        is run.

        Exists so what-if questions ("does a bigger die reduce power?") are
        answered from measurements instead of from the model's intuition.
        """
        from .measurements import Measurements

        index = Measurements()
        stock = _stock_clock_ns(design)
        rows = []
        for point in index.points(design):
            key, m = point.key, point.metrics
            if key.density:
                continue  # auto-density points only, so the rows are comparable
            limit = index.timing_limit(design, key.clock_ns) if key.clock_ns else 0.0
            wns = m.get("wns_ns") or 0.0
            cp = m.get("cp_delay_ns")
            rows.append(
                {
                    "util": key.util,
                    "aspect_ratio": key.aspect,
                    "core_margin": key.margin,
                    "pin_strategy": key.pin_strategy,
                    "macro_strategy": key.macro_strategy,
                    "floorplan": key.label(with_clock=False),
                    "clock_ns": key.clock_ns,
                    "die_area_um2": round(m.get("die_area_um2") or 0.0, 1),
                    "power_mw": round(m.get("power_mw") or 0.0, 3),
                    "congestion": round(m.get("congestion") or 0.0, 4),
                    "wns_ns": round(wns, 4),
                    "cp_delay_ns": round(cp, 4) if cp else None,
                    "fmax_mhz": round(1000.0 / cp, 1) if cp else None,
                    "fmax_kind": FMAX_KIND,
                    "timing_met": wns >= 0,
                    "timing_status": point.timing_status(limit) or "UNKNOWN",
                    "timing_threshold_ns": round(limit or 0.0, 4),
                    "ok": point.ok,
                    "congested": point.congested,
                    "source": CACHED_ORFS,
                    "producer": point.source,
                    "proven": point.proven,
                    "renders": point.renders or None,
                }
            )
        rows.sort(key=lambda r: (r["clock_ns"] or 0.0, r["util"] or 0.0, r["aspect_ratio"]))
        self.emit(f"[Control] tradeoff table for {design}: {len(rows)} real measured points")

        usable = [r for r in rows if _usable(r)]
        at_stock = [r for r in usable if stock is None or abs((r["clock_ns"] or 0.0) - stock) < 1e-9]
        findings = []
        if at_stock:
            smallest = min(at_stock, key=lambda r: r["die_area_um2"])
            lowest_power = min(at_stock, key=lambda r: r["power_mw"])
            findings.append(
                f"Smallest routable die at the stock clock: {smallest['floorplan']} -> "
                f"{smallest['die_area_um2']} um2 at {smallest['power_mw']} mW."
            )
            findings.append(
                f"Lowest power among routable configs at the stock clock: {lowest_power['floorplan']} -> "
                f"{lowest_power['power_mw']} mW at {lowest_power['die_area_um2']} um2."
            )
            spread = max(r["power_mw"] for r in at_stock) - min(r["power_mw"] for r in at_stock)
            biggest = max(at_stock, key=lambda r: r["die_area_um2"])
            direction = (
                "a bigger die does NOT buy lower power here"
                if biggest["power_mw"] >= lowest_power["power_mw"]
                else "a bigger die does reduce power here"
            )
            findings.append(
                f"At the stock clock power varies only {spread:.3f} mW across every measured "
                f"utilization and floorplan, and {direction}: the largest die "
                f"({biggest['floorplan']}) uses {biggest['power_mw']} mW."
            )
        clocks = sorted({r["clock_ns"] for r in usable if r["clock_ns"]})
        if len(clocks) > 1:
            low = min(usable, key=lambda r: r["power_mw"])
            findings.append(
                f"The clock period is the real power knob: the lowest routable power measured is "
                f"{low['power_mw']} mW at {low['clock_ns']:g} ns ({low['floorplan']}), "
                f"reg-to-reg fmax {low['fmax_mhz']} MHz."
            )
        return {
            "design": design,
            "source": CACHED_ORFS,
            "measured_points": rows,
            "key_findings": findings,
            "note": ("All rows are real ORFS full-flow measurements at auto placement density; "
                     "fmax_mhz is reg-to-reg (real clock period_min)."),
        }

    def _floorplan_memory(self):
        """Floorplan tables live in the same database as the session's memory;
        a non-SQLite (in-memory) session gets a throwaway one."""
        from .floorplan.memory import FloorplanMemory

        return FloorplanMemory(getattr(self.memory, "db_path", None) or ":memory:")

    def _floorplan_recall(self, design: str) -> Optional[dict]:
        memory = self._floorplan_memory()
        try:
            best = memory.best(design)
            moves = memory.corrections(design, "macro_move")
        finally:
            memory.close()
        if not best and not moves:
            return None
        return {
            "best_by_objective": {
                row["objective"]: {"floorplan": row["label"], "kind": row["kind"], "svg": row["svg_path"]}
                for row in best
            },
            "engineer_macro_moves": [
                {"macro": m["subject"], "from": m["proposed"], "to": m["final"]} for m in moves
            ],
        }

    def query_memory(self, design: str = "ibex") -> dict:
        seed = self.memory.seed_for(design, "nangate45", "route")
        self.emit(f"[Memory] query for {design}: {'WARM' if not seed.is_cold else 'COLD'}")
        floorplan = self._floorplan_recall(design)
        if seed.is_cold:
            result = {
                "design": design,
                "warm": False,
                "note": "No configuration learned yet for this design.",
            }
            if floorplan:
                result["floorplan"] = floorplan
            return result
        result = {
            "design": design,
            "warm": True,
            "memory_note": "WARM: this design has been run before and the results persist on disk.",
            "best_utilization_percent": (
                seed.best_config.core_utilization if seed.best_config else None
            ),
            "best_config": (
                _config_label(seed.best_config) if seed.best_config else None
            ),
            "best_density": (
                (seed.best_config.placement_density or "auto") if seed.best_config else None
            ),
            "best_score": seed.best_score,
            "settled_knobs": seed.decisions,
            "learned_notes": list(seed.heuristics),
        }
        if seed.congestion_wall_util is not None:
            wall = seed.congestion_wall_util
            best_util = seed.best_config.core_utilization if seed.best_config else None
            statement = (
                f"The wall is at utilization {wall:g}%: util {wall:g}% or higher "
                f"congests or fails."
            )
            if best_util is not None:
                statement += (
                    f" Util {best_util:g}% is measured CLEAN and is the best known "
                    f"configuration. Do not describe {best_util:g}% as the wall."
                )
            result["congestion_wall"] = statement
            # Printed from Python as well: the model has been seen to paraphrase
            # an 85% wall as "above 80%".
            self.emit(f"[Memory] {statement}")
        if seed.congestion_wall_by_density:
            result["congestion_wall_by_density"] = {
                ("auto" if not density else f"{density:g}"): wall_util
                for density, wall_util in sorted(seed.congestion_wall_by_density.items())
            }
        if floorplan:
            result["floorplan"] = floorplan
        return result

    def explain(self, topic: str) -> dict:
        """No backend call: hand back the real data already on hand."""
        self.emit(f"[Control] explain (no flow): {topic}")
        orfs = ORFSAdapter()
        measured = []
        for util in ORFS_UTIL_LADDER:
            config = FlowConfig(design="ibex", core_utilization=util, placement_density=0.0)
            cached = orfs._load_cached(config) if orfs.is_cached(config) else None
            if cached is not None:
                measured.append(
                    {
                        "design": "ibex",
                        "util": util,
                        **_metrics_summary(cached.metrics),
                    }
                )
        clean = [m for m in measured if not m["congested"]]
        congested = [m for m in measured if m["congested"]]
        findings = []
        if clean:
            best = min(clean, key=lambda m: m["die_area_um2"])
            findings.append(
                f"The best clean configuration is utilization {best['util']:.0f}%, "
                f"die area {best['die_area_um2']} um2, congestion {best['congestion']}."
            )
        if congested:
            first = min(congested, key=lambda m: m["util"])
            findings.append(
                f"Congestion first exceeds the 1.0 limit at utilization "
                f"{first['util']:.0f}% (congestion {first['congestion']}). Every "
                f"utilization at or above that is congested."
            )
            findings.append(
                "Cause: raising utilization packs cells into a smaller die, which "
                "leaves less routing space, so congestion rises as die area falls."
            )
        return {
            "topic": topic,
            "congestion_rule": "congested when congestion > 1.0",
            "objective": "minimize die_area_um2 subject to congestion <= 1.0",
            "key_findings": findings,
            "runs_this_session": self.runs,
            "last_optimize_report": self.last_report,
            "previously_measured_configs": measured,
        }

    def floorplan(
        self, design: str = "ibex", objective: str = "balanced",
        strategies: Optional[list] = None, mode: str = "auto",
        util: Optional[float] = None, aspect: Optional[float] = None,
        margin: Optional[float] = None, clock_ns: Optional[float] = None,
        run: Optional[str] = None,
    ) -> dict:
        """Floorplans from measured data.

        mode "best": the best measured floorplan of the design - smallest
        routable die over every knob (util, aspect, pins, macros) at the stock
        clock, from the full-flow measurements - with its OpenROAD renders.
        mode "compare" (default for explicit strategies): the FloorplanAgent's
        ranked comparison. mode "config" (default when util/aspect/margin/clock
        is given): the measurement of exactly that ConfigKey; knobs left out
        mean their stock value (AR 1, default pins/macros, stock clock). Cached candidates replay instantly; uncached ones
        run live only for fast designs (or ADAPTIVE_IICPILOT_FLOORPLAN_LIVE=1)
        and are otherwise reported NOT_RUN, so a chat turn never blocks on a
        multi-hour study."""
        if isinstance(strategies, str):
            strategies = [s.strip() for s in strategies.split(",") if s.strip()]
        mode = (mode or "auto").strip().lower()
        knobs = {name: _as_float(value, 0.0) for name, value in
                 (("util", util), ("aspect", aspect), ("margin", margin), ("clock_ns", clock_ns))
                 if value is not None}
        if mode == "auto":
            mode = "config" if knobs else "compare" if strategies else "best"
        if mode == "config":
            return self._config_floorplan(design, knobs, run=self._run_confirmed(design, knobs, run))
        if mode == "best":
            return self._best_floorplan(design)
        return self._compare_floorplans(design, objective, strategies)

    def _best_floorplan(self, design: str) -> dict:
        self._last_tradeoff = self.tradeoff(design)
        row, summary = self._select_by_objective(design, "die_area", table=self._last_tradeoff)
        if row is None:
            return {"design": design, "source": NOT_RUN,
                    "verdict": f"no routable full-flow measurement of {design} yet"}
        others = [r for r in self._last_tradeoff["measured_points"]
                  if r["clock_ns"] == row["clock_ns"] and r["floorplan"] != row["floorplan"]]
        rejected = sorted([r for r in others if r["die_area_um2"] < row["die_area_um2"]],
                          key=lambda r: r["die_area_um2"])
        verdict = (
            f"Best measured floorplan for {design}: {row['floorplan']} - die {row['die_area_um2']} um2, "
            f"congestion {row['congestion']} (routable, <= 1.0), timing {row['timing_status']} "
            f"WNS {row['wns_ns']:+.4f} ns (limit {row['timing_threshold_ns']:+.3f} ns), "
            f"{_fmax_phrase(row)}, power {row['power_mw']} mW at {row['clock_ns']:g} ns; "
            f"smallest routable die of {len(others) + 1} measured full-flow floorplans at this clock"
        )
        if rejected:
            verdict += "; smaller dies were measured but rejected: " + ", ".join(
                f"{r['floorplan']} ({r['die_area_um2']} um2, "
                f"{'congestion ' + str(r['congestion']) if r['congested'] else r['timing_status']})"
                for r in rejected[:3]
            )
        result = {
            "design": design, "mode": "best", "source": CACHED_ORFS,
            "best": {k: v for k, v in row.items() if k != "renders"},
            "verdict": verdict,
        }
        result["renders"] = self._show_renders(design, row, "best floorplan", verdict,
                                               views=("floorplan", "placement", "congestion"))
        self.emit(f"[Floorplan] best measured floorplan: {row['floorplan']} at {row['clock_ns']:g} ns "
                  f"({len(result['renders'])} render(s))")
        self._note_verdict({**result, **(summary or {}), "verdict": verdict})
        return result

    def _config_floorplan(self, design: str, knobs: dict, run: Optional[str] = None) -> dict:
        """The measured result of one named config. A full run answers with
        timing; with only a screen (global placement) it answers die area and
        congestion and says timing is unmeasured; with neither it is NOT_RUN,
        never estimated. An unmeasured config gets an OFFER (screen / full
        run); run="screen" or "full" executes it, which only happens once the
        engineer has confirmed the offer. A request that leaves utilization
        out while several utilizations are measured on that floorplan is
        asked back, never answered with one of them."""
        from .configkey import STOCK_MACROS, STOCK_PINS
        from .measurements import SCREEN, Measurements

        stock = _stock_clock_ns(design)
        clock = knobs.get("clock_ns") or stock
        want = {"util": knobs.get("util"), "aspect_ratio": knobs.get("aspect", 1.0),
                "core_margin": knobs.get("margin")}

        def same_floorplan(row: dict) -> bool:
            return ((want["util"] is None or abs((row["util"] or 0) - want["util"]) < 1e-6)
                    and abs(row["aspect_ratio"] - want["aspect_ratio"]) < 1e-6
                    and row["core_margin"] == want["core_margin"]
                    and row["pin_strategy"] == STOCK_PINS and row["macro_strategy"] == STOCK_MACROS)

        def same_screen(p) -> bool:
            return ((want["util"] is None or abs(p.key.util - want["util"]) < 1e-6)
                    and abs(p.key.aspect - want["aspect_ratio"]) < 1e-6 and p.key.margin == want["core_margin"]
                    and p.key.pin_strategy == STOCK_PINS and p.key.macro_strategy == STOCK_MACROS)

        self._last_tradeoff = self.tradeoff(design)
        rows = [r for r in self._last_tradeoff["measured_points"] if same_floorplan(r)]
        asked = ", ".join(f"{k.replace('_ns', '')} {v:g}" for k, v in knobs.items())
        index = Measurements()
        screens = [p for p in index.points(design, SCREEN) if same_screen(p)]

        if want["util"] is None:
            utils = sorted({r["util"] for r in rows} | {p.key.util for p in screens})
            if len(utils) > 1:
                verdict = (f"{design} {asked}: AMBIGUOUS - no utilization given and this floorplan is measured "
                           f"at util " + ", ".join(f"{u:g}" for u in utils)
                           + "; name one (e.g. 'util " + f"{utils[0]:g}" + "') - no config was substituted")
                self.emit(f"[Floorplan] config {asked}: ambiguous, {len(utils)} utilizations measured")
                self._note_verdict({"verdict": verdict, "source": NOT_RUN})
                return {"design": design, "mode": "config", "source": NOT_RUN, "verdict": verdict,
                        "measured_utils": utils}

        if run in ("screen", "full"):
            ran = self._run_config(design, knobs, run, clock)
            if ran is not None:
                return ran
            return self._config_floorplan(design, knobs)

        at_clock = [r for r in rows if clock is not None and abs(r["clock_ns"] - clock) < 1e-6]
        if at_clock:
            row = min(at_clock, key=lambda r: (not r["ok"], r["die_area_um2"]))
            status = "CONGESTED" if row["congested"] else "FAILED" if not row["ok"] else "routable"
            # Leading status word, like every other verdict, so the
            # contradiction check can hold the narration to it.
            word = "CLEAN" if status == "routable" else status
            verdict = (
                f"{word} - {design} {row['floorplan']} at {row['clock_ns']:g} ns (measured full flow): "
                f"die {row['die_area_um2']} um2, congestion {row['congestion']} ({status}), "
                f"timing {row['timing_status']} WNS {row['wns_ns']:+.4f} ns "
                f"(limit {row['timing_threshold_ns']:+.3f} ns), {_fmax_phrase(row)}, "
                f"power {row['power_mw']} mW"
            )
            others = sorted({r["clock_ns"] for r in rows} - {row["clock_ns"]})
            if others:
                verdict += " | also measured on this floorplan at " + ", ".join(f"{c:g}" for c in others) + " ns"
            result = {"design": design, "mode": "config", "source": CACHED_ORFS,
                      "measured": {k: v for k, v in row.items() if k != "renders"}, "verdict": verdict}
            result["renders"] = self._show_renders(design, row, row["floorplan"], verdict)
            self.emit(f"[Floorplan] config {asked}: full-flow measurement found")
            self._note_verdict({**result, "verdict": verdict})
            return result

        offer = self._offer(design, knobs, index, screened=bool(screens))
        if screens:
            point = screens[0]
            c = point.congestion
            verdict = (
                f"{design} {point.key.label(False)}: screened to global placement only - die "
                f"{point.metrics.get('die_area_um2')} um2, congestion {c} "
                f"({'over the 1.0 gate' if point.congested else 'under the 1.0 gate'}); "
                f"no full flow measured{' at ' + format(clock, 'g') + ' ns' if clock else ''}, "
                f"so timing and power are not measured"
            )
            if rows:
                verdict += " | full flows of this floorplan exist at " + ", ".join(
                    f"{r['clock_ns']:g}" for r in rows) + " ns"
            if offer:
                verdict += f" | {offer['text']}"
            result = {"design": design, "mode": "config", "source": CACHED_ORFS, "verdict": verdict,
                      "offer": offer,
                      "screen": {"floorplan": point.key.label(False), "die_area_um2": point.metrics.get("die_area_um2"),
                                 "congestion": c}}
            self.emit(f"[Floorplan] config {asked}: screen only")
            self._note_verdict({**result, "verdict": verdict})
            return result

        verdict = (f"{design} {asked}: NOT_RUN - no full flow or screen of this config has been measured"
                   + (f"; this floorplan was measured at " + ", ".join(f"{r['clock_ns']:g}" for r in rows) + " ns"
                      if rows else "")
                   + (f" | {offer['text']}" if offer else ""))
        self.emit(f"[Floorplan] config {asked}: not measured")
        self._note_verdict({"verdict": verdict, "source": NOT_RUN})
        return {"design": design, "mode": "config", "source": NOT_RUN, "verdict": verdict, "offer": offer}

    def _offer(self, design: str, knobs: dict, index, screened: bool) -> Optional[dict]:
        """What can be run for an unmeasured config, with MEASURED median
        times. Remembered as the session's pending offer: nothing runs until
        the next turn confirms it."""
        if knobs.get("util") is None:
            return None
        choices = [] if screened else ["screen"]
        choices.append("full")
        full_s = index.median_full_seconds(design)
        screen_times = sorted(p.elapsed_s for p in index.points(design, "SCREEN") if p.elapsed_s)
        screen_s = screen_times[len(screen_times) // 2] if screen_times else None

        def took(seconds: Optional[float]) -> str:
            return f"median measured {seconds / 60:.0f} min" if seconds else "time unknown"

        parts = []
        if "screen" in choices:
            parts.append(f"'screen it' (to global placement: die area + congestion, {took(screen_s)})")
        parts.append(f"'run the full flow' (timing + power, {took(full_s)})")
        text = (f"OFFER: reply " + " or ".join(parts)
                + f"; nothing runs until you confirm ('yes' = {'screen' if choices[0] == 'screen' else 'full flow'})")
        self.pending_offer = {"design": design, "knobs": dict(knobs), "choices": choices}
        return {"choices": choices, "text": text}

    def _run_config(self, design: str, knobs: dict, run: str, clock: Optional[float]) -> Optional[dict]:
        """Execute a confirmed offer: a FloorplanAgent screen, or the full flow
        through the EDA agent (stored in the ORFS cache and memory). Returns a
        result only on failure; on success the caller re-reads the measurement."""
        from .configkey import from_flow_config
        from .eda_agent import EDAAgent
        from .pipeline import _knobs_for

        config = FlowConfig(design=design, core_utilization=knobs["util"], placement_density=0.0,
                            aspect_ratio=knobs.get("aspect", 1.0), clock_period_ns=clock or 0.0,
                            floorplan={"core_margin": knobs["margin"]} if knobs.get("margin") is not None else None)
        key = from_flow_config(config, _stock_clock_ns(design))
        self.pending_offer = None
        if run == "screen":
            from .floorplan.runner import FloorplanRunner

            self.emit(f"[Floorplan] confirmed: screening {key.label(False)} to global placement")
            result = FloorplanRunner(emit=self.emit, raw_log=self.raw_log).run(_knobs_for(key), "SCREEN")
            ok = bool(result.get("ok"))
        else:
            self.emit(f"[DSE] confirmed: full flow on {key.label()}")
            result = EDAAgent(ORFSAdapter(), memory=self.memory, emit=self.emit).run_flow(key.to_flow_config(), "route")
            ok = result.ok
        if ok:
            return None
        verdict = f"{design} {key.label()}: {run} run FAILED - see the [Floorplan]/[EDA] log"
        self._note_verdict({"verdict": verdict, "source": REAL_ORFS})
        return {"design": design, "mode": "config", "source": REAL_ORFS, "verdict": verdict}

    def _compare_floorplans(self, design: str, objective: str, strategies: Optional[list]) -> dict:
        from .floorplan.agent import OBJECTIVES as FP_OBJECTIVES, FloorplanAgent
        from .floorplan.runner import FloorplanRunner

        objective = objective if objective in FP_OBJECTIVES else "balanced"
        live = os.environ.get("ADAPTIVE_IICPILOT_FLOORPLAN_LIVE", "").strip().lower() in ("1", "true", "yes")
        agent = FloorplanAgent(
            runner=FloorplanRunner(emit=self.emit, raw_log=self.raw_log),
            memory=self._floorplan_memory(),
            emit=self.emit,
            decide=self.interface.decide if self.interface.mode == "hitl" else None,
            on_image=self.on_image,
            allow_runs=live or _live_runs_allowed(design),
        )
        # A macro comparison needs a full run of EACH placement style (legality,
        # congestion, HPWL, timing side by side), not only of the winner.
        macro_families = {s for s in (strategies or []) if str(s).startswith("macros")}
        report = agent.run(design, objective, strategies or None,
                           full="per_family" if len(macro_families) > 1 else "best")
        table = [
            {k: v for k, v in row.items() if k not in ("svg", "variant", "renders") and v is not None}
            for row in report["table"]
        ]
        sources = {row.get("source") for row in report["table"]}
        not_run = sum(1 for row in report["table"] if row.get("source") == "NOT_RUN")
        if "REAL_ORFS" in sources:
            source = REAL_ORFS
        elif "CACHED" in sources:
            source = CACHED_ORFS
        else:
            source = NOT_RUN
        result = {
            "design": design,
            "mode": "compare",
            "objective": objective,
            "studies": report["studies"],
            "ranked_table": table,
            "best": {k: v for k, v in (report["best"] or {}).items() if k not in ("svg", "renders")} or None,
            "best_svg_path": report["best_svg_path"],
            "screen_vs_full_congestion": report["screen_vs_full"],
            "config_mk_plain_assignments": sorted(report["config_mk"]["command_line_required"]),
            "source": source,
        }
        if not_run:
            result["note"] = (
                f"{not_run} candidate(s) have no measurement yet; run "
                f"`python -m adaptive_iicpilot.floorplan --design {design}` to measure them."
            )
        # One line per study family from its best FULL run (screens have no
        # timing): legality, congestion, IO HPWL, WNS and reg-to-reg fmax.
        families = {}
        for row in report["table"]:
            if row.get("kind") == "FULL" and row.get("ok") and row.get("family") not in families:
                families[row["family"]] = row
        lines = []
        for fam, row in families.items():
            legal = {True: "legal", False: "ILLEGAL", None: "legality n/a"}[row.get("legal")]
            cong = row.get("congestion")
            lines.append(
                f"{fam}: '{row['label']}' {legal}, congestion {cong} "
                f"({'congested' if cong is not None and cong > 1.0 else 'routable'}), "
                f"IO HPWL {row['io_hpwl_um']} um, placement HPWL {row.get('gp_wirelength_um')} um, "
                f"timing {row.get('timing_status')} WNS {row['wns_ns']} ns (limit {row.get('timing_limit_ns')} ns), "
                f"reg-to-reg fmax {row.get('fmax_mhz')} MHz, power {row.get('power_mw')} mW"
            )
        if report["best"]:
            best = report["best"]
            result["verdict"] = (
                f"Best floorplan for {design} by {objective}: {best['label']} "
                f"(congestion {best['congestion']}, IO HPWL {best['io_hpwl_um']} um, "
                f"placement wirelength {best['gp_wirelength_um']} um"
                + (f", WNS {best['wns_ns']} ns" if best.get("wns_ns") is not None else "")
                + (f", reg-to-reg fmax {best['fmax_mhz']} MHz" if best.get("fmax_mhz") else "")
                + ")"
                + (" | full runs per family: " + "; ".join(lines) if lines else "")
            )
            self.emit(f"[Floorplan] ranked {len(report['table'])} candidate(s); best {best['label']}")
        shown = []
        for fam, row in families.items():
            renders = row.get("renders") or {}
            for view in ("placement", "congestion"):
                data = _read_png(renders.get(view)) if renders.get(view) else None
                if data is None:
                    continue
                self.on_image({
                    "title": f"{design}: {row['label']} ({fam}) - {view}",
                    "caption": f"full run {row.get('variant')}",
                    "png": data, "path": renders[view],
                    "verdict": self._frame_verdict(
                        next((l for l in lines if l.startswith(fam)), result.get("verdict", ""))),
                })
                shown.append(renders[view])
        result["renders"] = shown
        if result.get("verdict"):
            self._note_verdict({**result, "verdict": result["verdict"]})
        return result

    def _signoff_agent(self):
        from .signoff import SignoffAgent, SignoffMemory

        memory = SignoffMemory(getattr(self.memory, "db_path", None) or ":memory:")
        return SignoffAgent(memory=memory, emit=self.emit)

    def signoff(
        self, design: str = "ibex", util: Optional[float] = None, aspect: Optional[float] = None,
        clock_ns: Optional[float] = None, run: Any = False,
    ) -> dict:
        """Physical signoff (KLayout DRC, router DRC, antenna; LVS not
        available) of one measured run: the named util/aspect/clock, else the
        best measured floorplan (smallest routable die at the stock clock).
        A stored result answers at once; run=True measures it (~5 min) when
        it is not stored yet."""
        run = run is True or str(run).strip().lower() in ("true", "1", "yes")
        if util is not None:
            stock = _stock_clock_ns(design)
            key = _row_key(design, {
                "util": _as_float(util, 0.0),
                "aspect_ratio": _as_float(aspect, 1.0) if aspect is not None else 1.0,
                "clock_ns": _as_float(clock_ns, 0.0) if clock_ns else stock,
            })
            which = key.label()
        else:
            row, _ = self._select_by_objective(design, "die_area")
            if row is None:
                verdict = f"NOT_RUN - no routable full-flow measurement of {design} to sign off"
                self._note_verdict({"verdict": verdict, "source": NOT_RUN})
                return {"design": design, "source": NOT_RUN, "verdict": verdict}
            key = _row_key(design, row)
            which = f"best measured floorplan {key.label()}"
        self.emit(f"[Signoff] {design}: {which} ({'run if not stored' if run else 'memory only'})")
        agent = self._signoff_agent()
        try:
            result = agent.signoff(key, run=run)
        finally:
            agent.memory.close()
        self._note_verdict({"verdict": result["verdict"], "source": result["source"]})
        return result

    def dispatch(self, name: str, args: dict) -> dict:
        tools = {
            "run_flow": self.run_flow,
            "optimize": self.optimize,
            "query_memory": self.query_memory,
            "explain": self.explain,
            "tradeoff": self.tradeoff,
            "floorplan": self.floorplan,
            "signoff": self.signoff,
        }
        fn = tools.get(name)
        if fn is None:
            return {
                "error": f"unknown tool {name!r}",
                "valid_tools": sorted(tools),
            }

        # Drop arguments the tool does not accept rather than failing the call,
        # and say what the real parameters are — otherwise the model invents a
        # parameter, sees an exception, and offers the invented one to the user.
        valid = [
            param
            for param in inspect.signature(fn).parameters
            if param != "self"
        ]
        unknown = sorted(set(args) - set(valid))
        accepted = {key: value for key, value in args.items() if key in valid}

        try:
            result = fn(**accepted)
        except Exception as exc:  # surfaced to the model so it can apologize usefully
            return {"error": f"{type(exc).__name__}: {exc}", "valid_parameters": valid}

        if unknown and isinstance(result, dict):
            result["ignored_parameters"] = (
                f"{name} has no parameter(s) {', '.join(unknown)}; they were ignored. "
                f"The only valid parameters for {name} are: {', '.join(valid)}. "
                f"Do not offer any other parameter to the user."
            )
            self.emit(
                f"[Control] {name}: ignored unknown parameter(s) "
                f"{', '.join(unknown)}; valid: {', '.join(valid)}"
            )
        return result


BANNER = """\
\033[1mAdaptive IICPilot — conversational agent\033[0m
A local llama3.1:8b drives the DSE and EDA agents. Every number it quotes comes
from an actual tool call.

Try:
  run ibex at 80% utilization          run one configuration
  optimize ibex                        search for the smallest routable die
  ask it again                         second run is warm from memory
  why did util 90 congest?             explains from measured data
  what do you remember about ibex?     reports learned state
  /exit                                quit
"""


def _make_client():
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("The openai client is required: pip install openai")
    return OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")


def _utf8_streams() -> None:
    # The model writes um2 as "μm²" and similar; the default Windows console
    # codec (cp1252) cannot encode those and would kill the REPL mid-reply.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


SOURCE_LABELS = {
    REAL_ORFS: "real ORFS run",
    CACHED_ORFS: "real ORFS, cached",
    ESTIMATED_FROM_REAL: "estimated from real data",
    ESTIMATED_MOCK: "estimated by the mock model, no real data for this design",
    UNKNOWN_AT_CLOCK: "unknown - not measured at this clock",
    NOT_RUN: "not run",
}

_TOOL_NAMES = ("run_flow", "optimize", "query_memory", "explain", "floorplan")


def _stock_clock_ns(design: str) -> Optional[float]:
    """The design's stock period, or None if the backend cannot say.

    Not every backend knows about clocks — the mock does not — and None simply
    means there is no stock period to bound a request against.
    """
    getter = getattr(ORFSAdapter(), "stock_clock_ns", None)
    return getter(design) if callable(getter) else None


def validate_clock(clock_ns: Optional[float], stock_ns: Optional[float]) -> Optional[str]:
    """Keep the clock within half to double the design's stock period.

    Outside that band the result stops being informative: far too tight and
    nothing closes, far too loose and the flow is unconstrained. Bounds are
    relative to stock because stock periods differ 10x across these designs
    (ibex 2.2ns, gcd 0.25ns).
    """
    if not clock_ns:
        return None  # 0 means "use the design's stock clock"
    if clock_ns <= 0:
        return f"clock period {clock_ns:g}ns must be positive"
    if stock_ns is None:
        return None  # unknown stock period; nothing to bound against
    low, high = stock_ns * CLOCK_MIN_FACTOR, stock_ns * CLOCK_MAX_FACTOR
    if not (low - 1e-9 <= clock_ns <= high + 1e-9):
        return (
            f"clock period {clock_ns:g}ns is outside {CLOCK_MIN_FACTOR:g}x-"
            f"{CLOCK_MAX_FACTOR:g}x the stock period ({stock_ns:g}ns); "
            f"use {low:g}-{high:g}ns"
        )
    return None


def validate_knobs(util: float, density: Optional[float]) -> Optional[str]:
    """Reject a configuration the flow cannot meaningfully run, before running it.

    Returns an error message, or None when the knobs are usable. A real row of
    `util=0.7` reached memory once — a placement density that landed in the
    utilization slot — so a fraction is called out explicitly rather than being
    run as 0.7% utilization.
    """
    if util < 1:
        return (
            f"utilization {util:g} looks like a fraction, not a percentage; "
            f"did you mean {util * 100:g}%? Utilization must be between "
            f"{UTIL_MIN:g} and {UTIL_MAX:g} percent."
        )
    if not (UTIL_MIN <= util <= UTIL_MAX):
        return (
            f"utilization {util:g}% is outside the supported range "
            f"{UTIL_MIN:g}-{UTIL_MAX:g}%"
        )

    if density is None:
        return None
    if not (0.0 <= density <= 1.0):
        return (
            f"placement density {density:g} must be between 0 and 1 "
            f"(0 lets ORFS choose it)"
        )
    # Density 0 is the "let ORFS compute it" sentinel, not a real value, so the
    # floor does not apply to it. The model passes density: 0 routinely.
    floor = round(util / 100.0 + 0.02, 4)
    if density > 0 and density < floor:
        return (
            f"placement density {density:g} is below the utilization; "
            f"placement density must be at least the utilization; "
            f"try >= {floor:g} for {util:g}%"
        )
    return None


def is_garbled(reply: str) -> bool:
    """True when the reply is unusable as a user-facing answer.

    A small local model sometimes returns nothing, narrates the tool call it was
    supposed to have made, or leaks raw JSON. Any of those is worse than no
    prose at all, because the real result is already in hand.
    """
    text = reply.strip()
    if not text:
        return True
    lowered = text.lower()
    if any(name in lowered for name in _TOOL_NAMES):
        return True
    # Leaked JSON / function-call scaffolding.
    if '{"' in text or '":' in text or "tool_call" in lowered:
        return True
    # Telling the user to go call a tool themselves.
    if re.search(
        r"\b(call|invoke|use|run)\b[^.]{0,40}\b(the\s+)?(tool|function)\b", lowered
    ):
        return True
    if re.search(r"\b(i|you)\s+(should|will|need to|must)\s+call\b", lowered):
        return True
    return False


KNOWN_DESIGNS = ("ibex", "aes", "gcd", "tinyRocket", "picorv32", "jpeg", "swerv", "dynamic_node")


_NUM = r"(\d+(?:\.\d+)?|\.\d+)"
_KNOB_PATTERNS = (
    ("util", (rf"\butil(?:i[sz]ation)?\s*(?:of|=|at|:)?\s*{_NUM}\s*%?", rf"{_NUM}\s*%\s*util")),
    ("aspect", (rf"\b(?:ar|aspect(?:[ -]?ratio)?)\s*(?:of|=|:)?\s*{_NUM}",)),
    ("margin", (rf"\bmargin\s*(?:of|=|:)?\s*{_NUM}",)),
    ("clock_ns", (rf"{_NUM}\s*ns\b",)),
)


def extract_knobs(text: str) -> dict:
    """The floorplan/clock knobs a request names explicitly ("util 85",
    "85% util", "AR 0.5", "aspect ratio 0.75", "margin 2", "2.75 ns"). Only
    what is written is returned; nothing is defaulted here."""
    t = text.lower()
    out = {}
    for name, patterns in _KNOB_PATTERNS:
        for pattern in patterns:
            m = re.search(pattern, t)
            if m:
                out[name] = float(m.group(1))
                break
    return out


def route_intent(text: str) -> Optional[tuple[str, dict, str]]:
    """Map a request to (tool, args, reason) in Python, or None for the model.

    An 8B model once answered "make ibex as fast as possible" by calling
    optimize(objective='power'). Requests whose tool is unambiguous are routed
    here instead; the model only narrates the result. Order matters: layout /
    macro words win over "power" or "fast" in the same sentence.
    """
    t = text.lower()
    design = next((d for d in KNOWN_DESIGNS if re.search(rf"\b{d.lower()}\b", t)), "ibex")
    knobs = extract_knobs(text)
    named_floorplan = bool(knobs.keys() & {"util", "aspect", "margin"})
    # Signoff words win over "layout" ("is the best ibex layout DRC clean?").
    if re.search(r"\b(drc|signoff|sign[- ]off|lvs|antennas?)\b", t):
        if re.match(r"\s*(optimi[sz]e|minimi[sz]e)\b", t):
            return ("optimize", {"design": design, "objective": "die_area", "signoff": True},
                    "optimize, then signoff on the final best config")
        args = {"design": design, "run": bool(re.search(r"\b(run|check|do|verify)\b", t))}
        args.update({k: v for k, v in knobs.items() if k in ("util", "aspect", "clock_ns")})
        return ("signoff", args, "physical signoff (DRC, router DRC, antenna) from the signoff stage")
    if re.search(r"\b(floor ?plans?|aspect[ -]?ratios?|layouts?|pins?|macros?)\b", t) or (
        named_floorplan and re.search(r"\b(show|measured|what|how)\b", t)
    ):
        if re.search(r"\bmacros?\b", t):
            return ("floorplan", {"design": design, "mode": "compare",
                                  "strategies": ["macros_auto", "macros_manual"]},
                    "macro placement comparison")
        if named_floorplan:
            return ("floorplan", {"design": design, "mode": "config", **knobs},
                    "the measured config the request names")
        if re.search(r"\b(compare|comparison|rank|versus|vs)\b", t):
            return ("floorplan", {"design": design, "mode": "compare"}, "floorplan comparison")
        return ("floorplan", {"design": design, "mode": "best"}, "best measured floorplan with renders")
    if re.search(r"\bpower\b", t):
        return ("optimize", {"design": design, "objective": "power"}, "power: clock on the best floorplan")
    if re.search(r"\b(fast|faster|fastest|speed|speedy|quick|quicker|frequency|fmax|performance)\b", t):
        return ("optimize", {"design": design, "objective": "timing"},
                "speed: highest reg-to-reg fmax on the best floorplan")
    if re.match(r"\s*(optimi[sz]e|minimi[sz]e)\b", t):
        return ("optimize", {"design": design, "objective": "die_area"}, "die area over util x aspect")
    return None


def offer_declined(text: str, offer: Optional[dict]) -> bool:
    return bool(offer) and bool(re.search(r"\b(no|nope|don'?t|do not|cancel|skip|stop)\b", text.lower()))


def offer_choice(text: str, offer: Optional[dict]) -> Optional[str]:
    """Which offered run this reply confirms: "screen", "full", or None.
    A bare yes takes the first (cheapest) offered choice; anything negative
    or unrelated confirms nothing."""
    if not offer:
        return None
    t = text.lower().strip()
    if offer_declined(text, offer):
        return None
    if re.search(r"\bfull\b", t) and "full" in offer["choices"]:
        return "full"
    if re.search(r"\bscreen", t) and "screen" in offer["choices"]:
        return "screen"
    if re.fullmatch(r"(yes|yep|yeah|ok|okay|sure|go( ahead)?|do it|run it)[.! ]*", t):
        return offer["choices"][0]
    return None


FALLBACK_MAX_CHARS = 300


def short_fallback(verdict: str, limit: int = FALLBACK_MAX_CHARS) -> str:
    """The verdict cut to whole " | " segments that fit in `limit` characters.
    Used when the model cannot narrate: the full verdict is already in the
    [Verdict] log line, so the reply only has to carry the headline."""
    segments = [seg.strip() for seg in verdict.split(" | ") if seg.strip()]
    if not segments:
        return verdict
    out = segments[0]
    if len(out) > limit:
        cut = max(out.rfind("; ", 0, limit), out.rfind(", ", 0, limit))
        return out[: cut if cut > 0 else limit].rstrip(" ,;") + " ... (full verdict in the log)"
    for seg in segments[1:]:
        if len(out) + 3 + len(seg) > limit:
            return out + " ... (full verdict in the log)"
        out = f"{out} | {seg}"
    return out


def dedupe_reply(text: str) -> str:
    """Drop sentences the model repeated (an 8B model restates the verdict two
    or three times when narrating one tool result). Line structure is kept."""
    seen: set = set()
    paragraphs = []
    for para in re.split(r"\n\s*\n", text.strip()):
        lines = []
        for line in para.split("\n"):
            kept = []
            for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
                norm = re.sub(r"\W+", " ", sentence).strip().lower()
                if norm and norm in seen:
                    continue
                if norm:
                    seen.add(norm)
                kept.append(sentence)
            if any(k.strip() for k in kept):
                lines.append(" ".join(kept))
        if lines:
            paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def deterministic_summary(record: Optional[dict]) -> str:
    """Build the answer from the tool result, bypassing the model entirely."""
    if not record:
        return "The run finished, but no result was captured."

    design = record.get("design", "the design")
    util = record.get("util")
    source = record.get("source", "")
    label = SOURCE_LABELS.get(source, source or "unknown source")

    head = f"{design}" + (f" at {util:g}%" if isinstance(util, (int, float)) else "")
    head = f"{head} ({label})"

    def fmt(key: str, unit: str, spec: str = "g") -> Optional[str]:
        value = record.get(key)
        if value is None:
            return None
        return f"{value:{spec}}{unit}"

    facts = []
    die = fmt("die_area_um2", " um2", ".1f")
    if die:
        facts.append(f"die ~{die}")
    power = fmt("power_mw", " mW", ".2f")
    if power:
        facts.append(f"power ~{power}")
    congestion = record.get("congestion")
    if congestion is not None:
        facts.append(f"congestion {congestion:.4f}")
    else:
        facts.append("congestion n/a")

    verdict = record.get("verdict") or "no verdict"
    body = ", ".join(facts) if facts else "no metrics available"
    return f"{head}: {body}, verdict {verdict}"


def contradiction_note(reply: str, last_verdict: Optional[dict]) -> Optional[str]:
    """Return a correction line when the model's prose fights the tool result.

    Small local models invert CLEAN/CONGESTED and drop the "estimated" caveat.
    The tool result is authoritative, so rather than trusting the prose we
    append the verdict whenever the two disagree.
    """
    if not last_verdict:
        return None
    verdict = last_verdict.get("verdict", "")
    source = last_verdict.get("source", "")
    text = reply.lower()

    contradicted = False
    if verdict.startswith("CLEAN"):
        # "not congested" / "isn't congested" agree with CLEAN; a bare
        # "congested" does not.
        for match in re.finditer(r"congested", text):
            prefix = text[max(0, match.start() - 24) : match.start()]
            if not re.search(r"\b(not|isn't|is not|non-|never|avoid(s|ed)?)\s*$", prefix):
                contradicted = True
                break
    elif verdict.startswith("CONGESTED"):
        if re.search(r"\b(clean|routable)\b", text) and not re.search(
            r"\b(not|isn't|is not)\s+(clean|routable)\b", text
        ):
            contradicted = True
    elif verdict.startswith("FAILED"):
        if re.search(r"\b(clean|routable|succeeded)\b", text):
            contradicted = True
    elif verdict.startswith("REJECTED"):
        # Nothing ran, so any claim about the outcome is unfounded.
        if re.search(r"\b(clean|congested|routable|succeeded)\b", text):
            contradicted = True

    if source == ESTIMATED_MOCK and not re.search(r"estimat|mock|simulat", text):
        contradicted = True

    # A power saving bought with a slower clock must not be quoted alone.
    if "clock relaxed" in verdict and re.search(r"\bpower\b", text) and not re.search(
        r"clock|frequenc|fmax|slower|mhz", text
    ):
        return (
            "Note: that power saving comes from relaxing the clock, so the design "
            "runs slower - see the clock/fmax change in the verdict"
        )

    # Presenting a sub-1% power change as a win is a material omission: the
    # verdict already says it is noise, so the reply must not imply otherwise.
    if "negligible (<1%)" in verdict and not re.search(
        r"negligible|marginal|insignificant|clock period|within noise"
        r"|(?:<|less than|under|below)\s*1\s*(?:%|percent)",
        text,
    ):
        return (
            "Note: the power difference is negligible (<1%) at this clock; "
            "the clock period is the real power knob"
        )

    # Claiming the wrong objective is as misleading as claiming the wrong verdict.
    objective = last_verdict.get("objective")
    if objective:
        other = "power" if objective == "die_area" else "die_area"
        other_words = r"\bpower\b" if other == "power" else r"\bdie\s*area\b"
        mine = r"\bpower\b" if objective == "power" else r"\bdie\s*area\b"
        if re.search(other_words, text) and not re.search(mine, text):
            return f"Note: the optimization objective was {objective}, not {other}"

    if not contradicted:
        return None
    return f"Note: tool verdict is {verdict} ({source})"


VERDICT_ONLY_TOOLS = frozenset({"signoff"})


def _routed_turn(client, session: "Session", route: tuple, on_thinking, on_assistant) -> None:
    """Run the routed tool in Python, then let the model narrate it with NO
    tools offered (it cannot re-route). If the model is unreachable or garbles
    the answer, the verdict is reported directly."""
    name, args, reason = route
    session.emit(f"[Control] routed in Python ({reason}) -> {name}")
    session.emit(f"[Tool] {name}({args})")
    result = session.dispatch(name, args)
    call_id = f"routed-{len(session.messages)}"
    this_turn = [
        session.messages[-1],  # the user's request
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": call_id, "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, default=str)},
    ]
    session.messages.extend(this_turn[1:])
    verdict = (result or {}).get("verdict") if isinstance(result, dict) else None
    if name in VERDICT_ONLY_TOOLS and verdict:
        # A pass/fail answer: the verdict line IS the reply; no narration.
        on_assistant(verdict)
        session.messages.append({"role": "assistant", "content": verdict})
        return
    fallback = short_fallback(verdict) if verdict else deterministic_summary(session.last_record)
    on_thinking()
    try:
        # Turn isolation: the narration sees the system prompt and THIS turn
        # only, so it cannot quote an earlier turn's numbers as this answer.
        response = client.chat.completions.create(model=OLLAMA_MODEL,
                                                  messages=[session.messages[0], *this_turn])
        reply = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        session.emit(f"[Control] model unreachable ({type(exc).__name__}); reporting the tool result directly")
        on_assistant(fallback)
        return
    if not reply or is_garbled(reply):
        session.emit("[Control] model reply unusable; reporting the tool result directly")
        on_assistant(fallback)
    else:
        reply = dedupe_reply(reply)
        note = contradiction_note(reply, session.last_verdict)
        on_assistant(f"{reply}\n\n{note}" if note else reply)
    session.messages.append({"role": "assistant", "content": reply})


def run_turn(
    client,
    session: "Session",
    text: str,
    on_thinking: Callable[[], None],
    on_assistant: Callable[[str], None],
    on_error: Callable[[str], None],
) -> None:
    """One user turn: model call, optional tool calls, final reply.

    Shared verbatim by the terminal REPL and the JSONL frontend — the only
    difference between them is where these three callbacks send their output,
    and where session.emit sends the agents' log lines.
    """
    offer = session.pending_offer
    session.begin_turn()
    session.pending_offer = None  # an offer is answered in the very next turn or not at all
    session.messages.append({"role": "user", "content": text})

    choice = offer_choice(text, offer)
    if choice is None and offer_declined(text, offer):
        # Deterministic: a model answering a "no" has invented numbers for
        # the very config that was just reported as unmeasured.
        asked = ", ".join(f"{k.replace('_ns', '')} {v:g}" for k, v in offer["knobs"].items())
        reply = f"Nothing was run. {offer['design']} {asked} stays NOT_RUN (unmeasured)."
        session.emit(f"[Control] offer declined for {offer['design']} {asked}; nothing run")
        on_assistant(reply)
        session.messages.append({"role": "assistant", "content": reply})
        return
    if choice is not None:
        session.confirm_offer(offer, choice)
        route = ("floorplan", {"design": offer["design"], "mode": "config", **offer["knobs"], "run": choice},
                 f"engineer confirmed the offered {choice} run")
    else:
        route = route_intent(text)
    if route is not None:
        _routed_turn(client, session, route, on_thinking, on_assistant)
        return

    for _ in range(MAX_TOOL_HOPS):
        on_thinking()
        try:
            response = client.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=session.messages,
                tools=TOOLS,
            )
        except Exception as exc:
            on_error(
                f"Cannot reach Ollama at {OLLAMA_BASE_URL}\n"
                f"  {type(exc).__name__}: {exc}\n"
                f"  Start it with:  ollama serve\n"
                f"  And make sure the model is pulled:  ollama pull {OLLAMA_MODEL}"
            )
            return

        message = response.choices[0].message
        calls = message.tool_calls or []

        if not calls:
            reply = (message.content or "").strip()
            if session.last_record is not None and is_garbled(reply):
                # The result is already in hand; a broken narration of it is
                # strictly worse than stating it plainly.
                session.emit("[Control] model reply unusable; reporting the tool result directly")
                on_assistant(deterministic_summary(session.last_record))
                session.messages.append({"role": "assistant", "content": reply})
                return
            reply = dedupe_reply(reply)
            note = contradiction_note(reply, session.last_verdict)
            on_assistant(f"{reply}\n\n{note}" if note else reply)
            session.messages.append(
                {"role": "assistant", "content": message.content or ""}
            )
            return

        session.messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ],
            }
        )

        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            session.emit(f"[Tool] {call.function.name}({args})")
            result = session.dispatch(call.function.name, args)
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                }
            )


def main() -> None:
    _utf8_streams()
    client = _make_client()
    session = Session()

    print(BANNER)

    while True:
        try:
            line = input("\033[1m> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return

        run_turn(
            client,
            session,
            line,
            on_thinking=lambda: None,
            on_assistant=lambda text: print(f"\n{text}\n"),
            on_error=lambda text: print(f"\n\033[31m{text}\033[0m\n"),
        )


if __name__ == "__main__":
    if "--jsonl" in sys.argv[1:]:
        from .chat_jsonl import main_jsonl

        main_jsonl()
    else:
        main()
