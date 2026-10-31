"""FloorplanAgent: generate candidates, screen, rank, run finalists, learn.

Screening runs each candidate to global placement (3_3_place_gp), which is
enough for die/core area, I/O HPWL, placement wirelength and routability
congestion. Finalists then run the full flow for WNS. A finalist's full run
lives in a different variant from its screening run, so its global-placement
congestion is an independent re-measurement; the report checks they agree.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from ..types import FlowConfig, Proposal
from .knobs import FloorplanKnobs, Stage
from .memory import FloorplanMemory

OBJECTIVES = ("balanced", "congestion", "wirelength", "io_hpwl", "area", "timing")
CONGESTION_LIMIT = 1.0

# Study name -> what it varies. "default" picks by whether the design has macros.
STUDIES = ("aspect", "margin", "util", "pins", "macros_auto", "macros_manual")
# none, best, best_clean (the winner only if its congestion is <= 1.0), per_family
FULL_MODES = ("none", "best", "best_clean", "per_family")
ASPECT_RATIOS = (0.5, 0.75, 1.0, 1.5, 2.0)
MARGINS = (2.0, 5.0, 10.0)
# RTL-MP halo sweep; None is the platform default (nangate45: 22.4 x 15.12 um).
HALOS = (None, (10.0, 10.0), (5.0, 5.0))


def family(knobs: FloorplanKnobs) -> str:
    return knobs.family


# Study -> the scope it searches, for card titles ("util 50%, shape-only").
STUDY_SCOPES = {"aspect": "shape", "margin": "shape", "util": "shape", "pins": "pins",
                "macros_auto": "macros", "macros_manual": "macros"}


def study_scope(studies: list) -> str:
    scopes = sorted({STUDY_SCOPES.get(s, s) for s in studies})
    return "shape-only" if scopes == ["shape"] else "+".join(scopes)


def _value(result: dict, key: str) -> Optional[float]:
    value = (result.get("metrics") or {}).get(key)
    return float(value) if isinstance(value, (int, float)) else None


def usable(result: dict) -> bool:
    return bool(result.get("ok")) and result.get("legal") is not False and _value(result, "congestion") is not None


def score(result: dict, objective: str, pool: list[dict]) -> float:
    """Lower is better. Failed/illegal candidates sort last, congested ones
    (> 1.0, the same gate the DSE agent uses) after every clean one."""
    if not usable(result):
        return float("inf")
    # Large enough to dominate any metric (die areas run to 1e5 um2, wirelength
    # to 1e6 um): a congested floorplan must never outrank a clean one.
    penalty = 1e12 if _value(result, "congestion") > CONGESTION_LIMIT else 0.0
    if objective == "congestion":
        return penalty + _value(result, "congestion")
    if objective == "wirelength":
        return penalty + (_value(result, "gp_wirelength_um") or float("inf"))
    if objective == "io_hpwl":
        return penalty + (_value(result, "io_hpwl_um") or float("inf"))
    if objective == "area":
        return penalty + (_value(result, "die_area_um2") or float("inf"))
    if objective == "timing":
        wns = _value(result, "wns_ns")
        if wns is None:
            wns = _value(result, "gp_wns_ns")
        return penalty + (-wns if wns is not None else float("inf"))
    # balanced: mean ratio to the best value in the pool, per metric.
    keys = ("congestion", "gp_wirelength_um", "io_hpwl_um", "die_area_um2")
    ratios = []
    for key in keys:
        values = [_value(r, key) for r in pool if usable(r) and _value(r, key)]
        mine = _value(result, key)
        if values and mine:
            ratios.append(mine / min(values))
    return penalty + (sum(ratios) / len(ratios) if ratios else float("inf"))


def ranked(pool: list[dict], objective: str) -> list[dict]:
    """Best first. A FULL run ties ahead of its own SCREEN twin: same
    floorplan, but it also has WNS and is the measurement worth showing."""
    return sorted(pool, key=lambda r: (score(r, objective, pool), r.get("stage") != "FULL"))


def timing_status(result: dict) -> Optional[str]:
    """MET / ACCEPTABLE / VIOLATED against the design's rules-base limit
    (metrics.timing_threshold_ns, the slack ORFS's own metadata check allows).
    None for a screen or a run that never reached finish timing."""
    m = result.get("metrics") or {}
    wns, limit = m.get("wns_ns"), m.get("timing_threshold_ns")
    if result.get("stage") != "FULL" or wns is None:
        return None
    if wns >= 0:
        return "MET"
    return "ACCEPTABLE" if limit is not None and wns >= limit else "VIOLATED"


class FloorplanAgent:
    def __init__(
        self,
        runner=None,
        memory: Optional[FloorplanMemory] = None,
        emit: Callable[[str], None] = print,
        decide: Optional[Callable[[Proposal, FlowConfig], Optional[Proposal]]] = None,
        on_image: Optional[Callable[[dict], None]] = None,
        parallel: int = 2,
        allow_runs: bool = True,
    ) -> None:
        if runner is None:
            from .runner import FloorplanRunner

            runner = FloorplanRunner(emit=emit)
        self.runner = runner
        self.memory = memory or FloorplanMemory()
        self.emit = emit
        self.decide = decide
        self.on_image = on_image or (lambda frame: None)
        self.parallel = max(1, int(parallel))
        # False: replay cached candidates only (an interactive chat turn must
        # not block on a multi-hour sweep); uncached ones are reported NOT_RUN.
        self.allow_runs = allow_runs

    # --- candidates ---

    def default_studies(self, has_macros: bool) -> list[str]:
        return ["macros_auto", "macros_manual"] if has_macros else ["aspect", "pins"]

    def candidates(
        self, design: str, studies: list[str], has_macros: bool,
        utils: Optional[list] = None, aspects: Optional[list] = None,
    ) -> list[FloorplanKnobs]:
        """utils: every candidate is repeated at each of these utilizations
        (None keeps the design's stock). aspects: replaces the aspect sweep."""
        base = FloorplanKnobs(design=design, macro_mode="auto" if has_macros else "none")
        out: list[FloorplanKnobs] = []
        for study in studies:
            if study == "aspect":
                out += [base.tweaked(aspect_ratio=float(ar)) for ar in (aspects or ASPECT_RATIOS)]
            elif study == "util":
                if not utils:
                    raise ValueError("the util study needs utils=[...]")
                out.append(base)
            elif study == "margin":
                out += [base.tweaked(core_margin=m) for m in MARGINS]
            elif study == "pins":
                out += [base.tweaked(aspect_ratio=1.0, io_strategy=s)
                        for s in ("default", "bus_sides", "in_left_out_right")]
            elif study == "macros_auto":
                if not has_macros:
                    self.emit(f"[Floorplan] {design} has no macros; skipping macros_auto")
                    continue
                out += [base.tweaked(macro_mode="auto", macro_halo=h) for h in HALOS]
            elif study == "macros_manual":
                if not has_macros:
                    self.emit(f"[Floorplan] {design} has no macros; skipping macros_manual")
                    continue
                for strategy in ("periphery", "edge_cluster"):
                    out.append(base.tweaked(macro_mode="manual", macro_strategy=strategy))
                # An engineer's earlier moves come back as their own candidate.
                moves = self.memory.latest_macro_moves(design)
                if moves:
                    self.emit(f"[Floorplan] memory: re-proposing {len(moves)} engineer macro move(s)")
                    out.append(base.tweaked(
                        macro_mode="manual", macro_strategy="explicit",
                        macro_moves=tuple(sorted((n, x, y) for n, (x, y) in moves.items())),
                    ))
            else:
                raise ValueError(f"unknown study {study!r}; expected {STUDIES}")
        if utils:
            out = [k.tweaked(core_utilization=float(u)) for k in out for u in utils]
        unique: dict[str, FloorplanKnobs] = {}
        for knobs in out:
            unique.setdefault(knobs.key(), knobs)
        return list(unique.values())

    # --- running ---

    def _run_one(self, knobs: FloorplanKnobs, stage: str, synth_info: Optional[dict]) -> dict:
        cached = self.runner.cached(knobs, stage)
        if cached is not None:
            cached["source"] = "CACHED"
            return cached
        if not self.allow_runs:
            return {
                "design": knobs.design, "knobs": knobs.as_dict(), "label": knobs.label(),
                "stage": stage, "variant": knobs.variant(stage), "ok": False, "legal": None,
                "source": "NOT_RUN", "metrics": {},
                "failure": "not cached; run `python -m adaptive_iicpilot.floorplan` to measure it",
            }
        try:
            return self.runner.run(knobs, stage, synth_info)
        except Exception as exc:  # one broken candidate must not sink the study
            self.emit(f"[Floorplan] {knobs.label()} ({stage}) crashed: {type(exc).__name__}: {exc}")
            return {
                "design": knobs.design, "knobs": knobs.as_dict(), "label": knobs.label(),
                "stage": stage, "variant": knobs.variant(stage), "ok": False, "legal": None,
                "source": "ERROR", "metrics": {}, "failure": f"{type(exc).__name__}: {exc}",
            }

    # --- pipeline stage ---

    def choose(self, design: str, objective: str = "balanced", tech: str = "nangate45") -> Optional[dict]:
        """The floorplan a DSE search should run with, from what is measured.

        Utilization belongs to DSE, so floorplans are only compared at ONE
        common utilization: the one with the most distinct candidates (ties
        go to the design's stock). Across utilizations die area and
        congestion move with density, not with the floorplan, and the winner
        would just be "the run at the highest util". Manual macro placements
        and explicit die/core areas are skipped: both are tied to one core
        size, and DSE changes the core with every utilization step.
        """
        from .knobs import knobs_from_dict

        objective = objective if objective in OBJECTIVES else "balanced"
        groups: dict = {}
        for row in self.memory.runs(design, tech=tech):
            knobs = knobs_from_dict(json.loads(row["knobs_json"]))
            result = {
                "knobs": knobs.as_dict(), "label": row["label"], "stage": row["kind"],
                "ok": bool(row["ok"]), "legal": None if row["legal"] is None else bool(row["legal"]),
                "svg": row["svg_path"], "variant": row["variant"],
                "metrics": {"congestion": row["congestion"], "io_hpwl_um": row["io_hpwl_um"],
                            "gp_wirelength_um": row["gp_wirelength_um"],
                            "die_area_um2": row["die_area_um2"], "wns_ns": row["wns_ns"]},
            }
            shape_key = knobs.tweaked(core_utilization=None).key()
            group = groups.setdefault(knobs.core_utilization, {})
            # One entry per floorplan; a FULL run outranks its own screen.
            if shape_key not in group or row["kind"] == "FULL":
                group[shape_key] = (knobs, result)
        if not groups:
            return None
        util = max(groups, key=lambda u: (len(groups[u]), u is None))
        skipped = [k.label() for k, _r in groups[util].values()
                   if k.macro_mode == "manual" or k.die_area is not None]
        pool = [r for k, r in groups[util].values()
                if k.macro_mode != "manual" and k.die_area is None and usable(r)]
        if not pool:
            return None
        best = ranked(pool, objective)[0]
        knobs = knobs_from_dict(best["knobs"])
        stock = self.runner.stock_util(design, tech) if hasattr(self.runner, "stock_util") else None
        basis_util = util if util is not None else stock
        return {
            "label": knobs.tweaked(core_utilization=None).label(),
            "aspect_ratio": knobs.aspect_ratio,
            "floorplan": {
                "label": knobs.tweaked(core_utilization=None).label(),
                "core_margin": knobs.core_margin,
                "io_strategy": knobs.io_strategy,
                "macro_mode": knobs.macro_mode,
                "macro_halo": list(knobs.macro_halo) if knobs.macro_halo else None,
            },
            "objective": objective,
            "basis": (f"best of {len(pool)} floorplan(s) measured at util "
                      f"{basis_util:g}%" if basis_util is not None else f"best of {len(pool)} floorplan(s)"),
            "metrics": best["metrics"],
            "kind": best["stage"],
            "svg": best.get("svg"),
            "skipped_manual": skipped,
        }

    def run_many(self, knobs_list: list[FloorplanKnobs], stage: str, synth_info: Optional[dict]) -> list[dict]:
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            results = list(pool.map(lambda k: self._run_one(k, stage, synth_info), knobs_list))
        for result, knobs in zip(results, knobs_list):
            result["family"] = family(knobs)
            if result.get("source") not in ("NOT_RUN", "ERROR"):
                self.memory.record(result)
        return results

    # --- HITL ---

    def _proposal(self, result: dict) -> Proposal:
        knobs = result["knobs"]
        changes: dict = {}
        for key in ("core_utilization", "aspect_ratio", "core_margin"):
            if knobs.get(key) is not None:
                changes[key] = knobs[key]
        for macro in result.get("macros") or []:
            changes[f"{macro['name']}.x"] = round(macro["x"], 3)
            changes[f"{macro['name']}.y"] = round(macro["y"], 3)
        m = result.get("metrics") or {}
        rationale = (
            f"Run the full flow on '{result['label']}': screened congestion {m.get('congestion')}, "
            f"IO HPWL {m.get('io_hpwl_um')} um, placement WL {m.get('gp_wirelength_um')} um, "
            f"die {m.get('die_area_um2')} um2. Editing a macro's x/y turns this into a manual "
            f"placement and is remembered as a correction."
        )
        svg = None
        if result.get("svg") and Path(result["svg"]).is_file():
            svg = Path(result["svg"]).read_text(encoding="utf-8")
        return Proposal(changes=changes, rationale=rationale, triggered_by="FloorplanAgent", image_svg=svg)

    def _apply_edit(self, result: dict, proposal: Proposal, decided: Proposal) -> FloorplanKnobs:
        """Turn an edited proposal into new knobs, recording every change as
        an engineer correction."""
        from .knobs import knobs_from_dict

        knobs = knobs_from_dict(result["knobs"])
        design = knobs.design
        shape = {}
        for key in ("core_utilization", "aspect_ratio", "core_margin"):
            if key in decided.changes and decided.changes[key] != proposal.changes.get(key):
                shape[key] = float(decided.changes[key])
                self.memory.record_correction(design, "knob", key, proposal.changes.get(key),
                                              shape[key], "HITL edit of a floorplan finalist")
        if shape:
            knobs = knobs.tweaked(**shape)
        positions = {}
        moved = False
        for macro in result.get("macros") or []:
            name = macro["name"]
            old = (proposal.changes.get(f"{name}.x"), proposal.changes.get(f"{name}.y"))
            new = (float(decided.changes.get(f"{name}.x", old[0])), float(decided.changes.get(f"{name}.y", old[1])))
            positions[name] = new
            if abs(new[0] - old[0]) > 1e-6 or abs(new[1] - old[1]) > 1e-6:
                moved = True
                self.memory.record_correction(design, "macro_move", name, list(old), list(new),
                                              f"HITL move on '{result['label']}'")
                self.emit(f"[Floorplan] correction: {name} moved ({old[0]:g},{old[1]:g}) -> ({new[0]:g},{new[1]:g})")
        if moved:
            knobs = knobs.tweaked(
                macro_mode="manual", macro_strategy="explicit",
                macro_moves=tuple(sorted((n, x, y) for n, (x, y) in positions.items())),
            )
        return knobs

    def _finalist_knobs(self, result: dict) -> Optional[FloorplanKnobs]:
        """Ask the engineer (if HITL is on) before spending a full run."""
        from .knobs import knobs_from_dict

        knobs = knobs_from_dict(result["knobs"])
        if self.decide is None:
            return knobs
        proposal = self._proposal(result)
        decided = self.decide(proposal, FlowConfig(design=knobs.design))
        if decided is None:
            self.emit(f"[Floorplan] engineer rejected the full run of '{result['label']}'")
            return None
        if decided.changes == proposal.changes:
            return knobs
        return self._apply_edit(result, proposal, decided)

    # --- the study ---

    def run(
        self,
        design: str,
        objective: str = "balanced",
        studies: Optional[list[str]] = None,
        full: str = "best",
        tech: str = "nangate45",
        utils: Optional[list] = None,
        aspects: Optional[list] = None,
    ) -> dict:
        """full: "none", "best" (overall winner), "best_clean" (the winner, but
        only if its screened congestion is <= 1.0) or "per_family" (best of
        each study family, e.g. best RTL-MP and best manual)."""
        if objective not in OBJECTIVES:
            raise ValueError(f"objective must be one of {OBJECTIVES}")
        if full not in FULL_MODES:
            raise ValueError(f"full must be one of {FULL_MODES}")
        report = self.runner.config_report(design, tech)
        hard = report["command_line_required"]
        self.emit(
            f"[Floorplan] {design} config.mk: plain '=' (command line required): "
            f"{', '.join(k + '=' + v['value'] for k, v in hard.items()) or 'none'}; "
            f"'?=': {', '.join(f'{k}={v}' for k, v in report['env_overridable'].items()) or 'none'}"
        )
        busy = [f for f in self.runner.active_flows() if not f["variant"].startswith("fp_")]
        if busy:
            self.emit(
                "[Floorplan] other ORFS flows running: "
                + ", ".join(f"{f['design']}/{f['variant']}" for f in busy)
                + " - floorplan runs stay in their own fp_* variants"
            )

        synth_info = None
        has_macros = None
        if self.allow_runs:
            synth_info = self.runner.ensure_synth(design, tech)
            has_macros = bool(synth_info.get("macros"))
        else:
            has_macros = any(r.get("family", "").startswith("macros") or "macro" in (r.get("label") or "")
                             for r in self.memory.runs(design)) or design not in ("ibex", "gcd")
        if not self.runner.supports_variable("MACRO_PLACE_CHANNEL") and has_macros:
            self.emit("[Floorplan] MACRO_PLACE_CHANNEL is not read by this ORFS (rtl_macro_placer has no "
                      "channel option); auto mode sweeps MACRO_PLACE_HALO, channel applies to manual strategies")
        studies = studies or self.default_studies(has_macros)
        cands = self.candidates(design, studies, has_macros, utils, aspects)
        stock_util = self.runner.stock_util(design, tech) if hasattr(self.runner, "stock_util") else None
        scope = study_scope(studies)
        shown = sorted({float(u) for u in utils}) if utils else ([stock_util] if stock_util else [])
        util_text = ("util " + "/".join(f"{u:g}" for u in shown) + "%") if shown else "util stock"
        self.emit(f"[Floorplan] {design}: screening {len(cands)} candidate(s) to global placement ({', '.join(studies)})")

        screened = self.run_many(cands, Stage.SCREEN, synth_info)
        order = ranked(screened, objective)

        finals: list[dict] = []
        if full != "none":
            picks: list[dict] = []
            if full == "per_family":
                seen = set()
                for r in order:
                    if usable(r) and r["family"] not in seen:
                        seen.add(r["family"])
                        picks.append(r)
            elif order and usable(order[0]):
                clean = (_value(order[0], "congestion") or 99.0) <= CONGESTION_LIMIT
                if full == "best" or clean:
                    picks.append(order[0])
                else:
                    self.emit(f"[Floorplan] no screened candidate is at congestion <= {CONGESTION_LIMIT}; "
                              f"no full run (best was '{order[0]['label']}' at {_value(order[0], 'congestion')})")
            final_knobs = []
            for pick in picks:
                knobs = self._finalist_knobs(pick)
                if knobs is not None:
                    final_knobs.append(knobs)
            # An edited finalist is new: screen it too so it has both numbers.
            new = [k for k in final_knobs if k.key() not in {c.key() for c in cands}]
            if new:
                extra = self.run_many(new, Stage.SCREEN, synth_info)
                screened += extra
                order = ranked(screened, objective)
            if final_knobs:
                self.emit(f"[Floorplan] full flow on {len(final_knobs)} finalist(s)")
                finals = self.run_many(final_knobs, Stage.FULL, synth_info)

        # Screening validation: same knobs, independent variants.
        checks = []
        for final in finals:
            twin = next((s for s in screened if s["knobs"] == final["knobs"]), None)
            if twin is None:
                continue
            a, b = _value(twin, "congestion"), _value(final, "congestion")
            checks.append({
                "label": final["label"], "screen_congestion": a, "full_congestion": b,
                "match": a is not None and b is not None and abs(a - b) < 1e-9,
            })
            self.emit(f"[Floorplan] screen vs full congestion for '{final['label']}': {a} vs {b} -> "
                      f"{'MATCH' if checks[-1]['match'] else 'MISMATCH'}")

        pool = screened + finals
        overall = ranked(pool, objective)
        best = next((r for r in overall if usable(r)), None)
        if best is not None:
            self.memory.set_best(design, objective, best, score(best, objective, pool))
            if best.get("svg") and Path(best["svg"]).is_file():
                self.on_image({
                    "title": f"{design}: best floorplan ({objective}) - {util_text}, {scope}",
                    "caption": f"{best['label']} - {best['stage'].lower()} run, variant {best['variant']}",
                    "svg": Path(best["svg"]).read_text(encoding="utf-8"),
                    "path": best["svg"],
                })
        checks += self.cache_checks(screened, tech)
        return {
            "design": design,
            "objective": objective,
            "studies": studies,
            "scope": f"{util_text}, {scope}",
            "config_mk": report,
            "table": [self.row(r, i + 1, objective, pool) for i, r in enumerate(overall)],
            "best": self.row(best, 1, objective, pool) if best else None,
            "best_svg_path": best.get("svg") if best else None,
            "screen_vs_full": checks,
        }

    def cache_checks(self, screened: list[dict], tech: str = "nangate45") -> list[dict]:
        """Screened congestion vs the DSE agent's own full runs in the ORFS
        result cache, for candidates that are physically the same config:
        only utilization and/or aspect changed, auto density, stock clock."""
        orfs = getattr(self.runner, "orfs", None)
        if orfs is None or not hasattr(self.runner, "stock_util"):
            return []
        from .knobs import knobs_from_dict

        checks = []
        for result in screened:
            if result.get("stage") != Stage.SCREEN or not usable(result):
                continue
            k = knobs_from_dict(result["knobs"])
            if not (k.family == "shape" and k.core_margin is None and k.die_area is None):
                continue
            util = k.core_utilization or self.runner.stock_util(k.design, tech)
            if util is None:
                continue
            config = FlowConfig(design=k.design, tech=tech, core_utilization=float(util),
                                placement_density=0.0, aspect_ratio=k.aspect_ratio or 1.0)
            config = config.tweaked(clock_period_ns=orfs.effective_clock_ns(config))
            if not orfs.is_cached(config):
                continue
            full = orfs._load_cached(config)
            a, b = _value(result, "congestion"), full.metrics.congestion
            checks.append({
                "label": result["label"], "screen_congestion": a, "full_congestion": b,
                "full_source": "DSE full run (ORFS cache, base variant)",
                "match": a is not None and abs(a - b) < 1e-9,
            })
            self.emit(f"[Floorplan] screen vs cached DSE full run for '{result['label']}': {a} vs {b} -> "
                      f"{'MATCH' if checks[-1]['match'] else 'MISMATCH'}")
        return checks

    @staticmethod
    def renders_of(result: dict) -> dict:
        """OpenROAD PNG renders of a run (render/render.json next to its SVG),
        view -> path; empty when the run was never rendered."""
        svg = result.get("svg")
        if not svg:
            return {}
        path = Path(svg).parent / "render" / "render.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {view: data[view] for view in ("floorplan", "placement", "congestion") if data.get(view)}

    @staticmethod
    def row(result: dict, rank: int, objective: str, pool: list[dict]) -> dict:
        m = result.get("metrics") or {}
        s = score(result, objective, pool)
        cp = m.get("cp_delay_ns")
        return {
            "rank": rank,
            "label": result.get("label"),
            "family": result.get("family"),
            "util": result.get("util_text"),
            "scope": result.get("scope"),
            "kind": result.get("stage"),
            "source": result.get("source"),
            "ok": result.get("ok"),
            "legal": result.get("legal"),
            "die_area_um2": m.get("die_area_um2"),
            "core_area_um2": m.get("core_area_um2"),
            "congestion": m.get("congestion"),
            "io_hpwl_um": m.get("io_hpwl_um"),
            "gp_wirelength_um": m.get("gp_wirelength_um"),
            "wns_ns": m.get("wns_ns"),
            "timing_limit_ns": m.get("timing_threshold_ns"),
            "timing_status": timing_status(result),
            # Reg-to-reg (real clock period_min); FULL runs only.
            "fmax_mhz": round(1000.0 / cp, 1) if cp else None,
            "power_mw": round(m["power_mw"], 3) if m.get("power_mw") else None,
            "drc_errors": m.get("drc_errors"),
            "renders": FloorplanAgent.renders_of(result),
            "elapsed_s": result.get("elapsed_s"),
            "score": None if s == float("inf") else round(s, 4),
            "svg": result.get("svg"),
            "variant": result.get("variant"),
            "failure": result.get("failure"),
            "violations": (result.get("violations") or [])[:3],
        }


PIPELINE_OBJECTIVES = {"die_area": "balanced", "power": "balanced", "timing": "timing"}
FLOORPLAN_POLICIES = ("apply", "apply_if_cached", "stock")


def floorplan_stage(
    base: FlowConfig,
    memory: FloorplanMemory,
    emit: Callable[[str], None],
    objective: str = "die_area",
    policy: str = "apply",
    is_measured: Optional[Callable[[FlowConfig], bool]] = None,
) -> tuple[FlowConfig, Optional[dict]]:
    """The floorplan step of the optimize pipeline, ahead of DSE.

    Returns the base config the search should start from and a report of
    what was decided. It never runs a flow: the choice comes from what the
    FloorplanAgent has already measured for the design.

    policy "apply_if_cached" keeps the stock floorplan unless `is_measured`
    says every config the search may visit is already measured under the
    chosen floorplan — an interactive turn must not start hours of flow.
    """
    if policy not in FLOORPLAN_POLICIES:
        raise ValueError(f"floorplan policy must be one of {FLOORPLAN_POLICIES}")
    report = {"applied": False, "policy": policy}
    if os.environ.get("ADAPTIVE_IICPILOT_BACKEND", "mock").strip().lower() != "orfs":
        # The mock charges 0.1 congestion per unit of |aspect - 1|; a real
        # floorplan choice would only make its numbers more wrong.
        emit("[Floorplan] stage skipped: mock backend (floorplans are only applied to real ORFS runs)")
        return base, {**report, "reason": "mock backend"}
    if policy == "stock":
        emit("[Floorplan] stage: stock floorplan requested")
        return base, {**report, "reason": "stock requested"}
    if base.floorplan is not None or base.aspect_ratio != 1.0:
        emit(f"[Floorplan] stage: request already fixes the floorplan (aspect {base.aspect_ratio:g}); kept as given")
        return base, {**report, "reason": "set by request"}

    from .runner import FloorplanRunner

    # choose() runs no flow; the runner only reads the design's stock util.
    agent = FloorplanAgent(runner=FloorplanRunner(emit=lambda line: None), memory=memory,
                           emit=emit, allow_runs=False)
    choice = agent.choose(base.design, PIPELINE_OBJECTIVES.get(objective, "balanced"), base.tech)
    if choice is None:
        emit(f"[Floorplan] stage: no floorplan study measured for {base.design}; stock floorplan "
             f"(run `python -m adaptive_iicpilot.floorplan --design {base.design}` to measure one)")
        return base, {**report, "reason": "no floorplan study in memory"}
    report.update(choice=choice)
    if choice["skipped_manual"]:
        emit(f"[Floorplan] stage: manual macro placements not applicable across utilizations, "
             f"skipped: {', '.join(choice['skipped_manual'])}")

    staged = base.tweaked(
        aspect_ratio=choice["aspect_ratio"] if choice["aspect_ratio"] is not None else base.aspect_ratio,
        floorplan=choice["floorplan"],
    )
    m = choice["metrics"]
    emit(f"[Floorplan] stage: chose '{choice['label']}' ({choice['basis']}, {choice['objective']}): "
         f"congestion {m.get('congestion')}, IO HPWL {m.get('io_hpwl_um')} um")
    if policy == "apply_if_cached" and not (is_measured and is_measured(staged)):
        emit(f"[Floorplan] stage: '{choice['label']}' has no measured utilization ladder yet; "
             f"this search keeps the stock floorplan")
        return base, {**report, "reason": "chosen floorplan not measured across the ladder"}
    emit(f"[Floorplan] stage: every DSE run uses '{choice['label']}'")
    return staged, {**report, "applied": True}


def format_table(rows: list[dict]) -> str:
    """Plain-text ranked table for the CLI and logs."""
    def f(value, spec):
        return format(value, spec) if isinstance(value, (int, float)) else "-"
    head = f"{'#':>2} {'candidate':<34} {'kind':<6} {'die um2':>9} {'cong':>7} {'IO HPWL':>9} {'GP WL':>9} {'WNS':>8} {'legal':>5} {'sec':>6}"
    lines = [head, "-" * len(head)]
    for r in rows:
        legal = "-" if r["legal"] is None else ("yes" if r["legal"] else "NO")
        lines.append(
            f"{r['rank']:>2} {str(r['label'])[:34]:<34} {str(r['kind'])[:6]:<6} "
            f"{f(r['die_area_um2'], '9.0f')} {f(r['congestion'], '7.4f')} {f(r['io_hpwl_um'], '9.0f')} "
            f"{f(r['gp_wirelength_um'], '9.0f')} {f(r['wns_ns'], '8.4f')} {legal:>5} {f(r['elapsed_s'], '6.0f')}"
            + ("" if r["ok"] else f"  [{r.get('failure') or 'failed'}]")
        )
    return "\n".join(lines)
