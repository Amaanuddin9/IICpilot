"""Drive ORFS for one floorplan candidate, in its own FLOW_VARIANT.

Isolation rules, all enforced here:
  * every run uses FLOW_VARIANT=fp_<stage>_<hash(design, knobs, stage)>, so it
    writes only to results|logs|reports|objects/<tech>/<design>/<variant>/
  * clean_all is only ever issued with that FLOW_VARIANT on the same command
    line; a variant without the fp_ prefix ("base") is refused outright
  * every make call on a variant holds `flock -n` on a per-variant lock file,
    so two flows can never run in the same variant at once
  * the ORFS tree is never edited: knobs go on the make command line and the
    generated Tcl lives in this repo's .floorplan_runs/

Synthesis does not depend on any floorplan knob, so it runs once per design in
a dedicated fp_synth_* variant and is copied (timestamps preserved, so make
sees it as up to date) into each candidate's variant.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from ..eda_interface import ORFSAdapter
from . import config_mk, defsvg, iotcl, macros
from .knobs import VARIANT_PREFIX, FloorplanKnobs, Stage, shell_quote_vars
from .macros import Macro, Rect

BUSY_MARKER = "FP_VARIANT_BUSY"
BUSY_EXIT = 75
SCREEN_TARGET = "3_3_place_gp.odb"
DIR_KINDS = ("results", "logs", "reports", "objects")

# Reads an odb, writes its geometry as JSON and (optionally) a DEF. Written to
# disk once; the odb and output paths come in through the environment.
PROBE_TCL = r"""
read_db $::env(FP_ODB)
set block [ord::get_db_block]
set dbu [$block getDbUnitsPerMicron]
proc um {v} { global dbu; return [format %.4f [expr {double($v) / $dbu}]] }
proc rect_json {r} {
  return "\[[um [$r xMin]], [um [$r yMin]], [um [$r xMax]], [um [$r yMax]]\]"
}
proc jstr {s} { return "\"[string map {\\ \\\\ \" \\\"} $s]\"" }
set macros {}
foreach inst [$block getInsts] {
  set m [$inst getMaster]
  if {[$m isBlock]} {
    lappend macros "{\"name\": [jstr [$inst getName]], \"master\": [jstr [$m getName]], \"width\": [um [$m getWidth]], \"height\": [um [$m getHeight]], \"bbox\": [rect_json [$inst getBBox]], \"status\": [jstr [$inst getPlacementStatus]], \"orient\": [jstr [$inst getOrient]]}"
  }
}
set pins {}
foreach bt [$block getBTerms] {
  lappend pins "{\"name\": [jstr [$bt getName]], \"direction\": [jstr [$bt getIoType]], \"sig_type\": [jstr [$bt getSigType]], \"status\": [jstr [$bt getFirstPinPlacementStatus]], \"bbox\": [rect_json [$bt getBBox]]}"
}
set f [open $::env(FP_JSON) w]
puts $f "{\"dbu\": $dbu, \"die\": [rect_json [$block getDieArea]], \"core\": [rect_json [$block getCoreArea]], \"macros\": \[[join $macros ,]\], \"pins\": \[[join $pins ,]\]}"
close $f
if {[info exists ::env(FP_DEF)] && $::env(FP_DEF) ne ""} {
  write_def $::env(FP_DEF)
}
"""


def default_artifacts_dir() -> Path:
    """Inside the repo, like .orfs_cache: the backend runs in WSL and the
    editor on Windows, and both must see the same files."""
    override = os.environ.get("ADAPTIVE_IICPILOT_FLOORPLAN_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".floorplan_runs"


def to_wsl(path: Path | str) -> str:
    """Path as ORFS (inside WSL) sees it. A no-op when already in WSL."""
    text = str(path)
    if os.name != "nt":
        return text
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if not match:
        return text.replace("\\", "/")
    return f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}"


def _num(data: dict, key: str) -> Optional[float]:
    try:
        return float(data[key])
    except (KeyError, TypeError, ValueError):
        return None


class VariantError(RuntimeError):
    pass


class FloorplanRunner:
    def __init__(
        self,
        orfs_root: Optional[str] = None,
        artifacts_dir: Optional[Path] = None,
        emit: Callable[[str], None] = print,
        raw_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.orfs = ORFSAdapter(orfs_root=orfs_root)
        self.flow = f"{self.orfs.orfs_root}/flow"
        self.artifacts = Path(artifacts_dir) if artifacts_dir else default_artifacts_dir()
        self.emit = emit
        self.raw_log = raw_log
        self._nicknames: dict[str, str] = {}

    # --- plumbing ---

    def _sh(self, command: str, on_line: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
        return self.orfs._shell(command, on_line=on_line)

    @property
    def _prefix(self) -> str:
        return f"cd {self.flow} && source ../env.sh >/dev/null 2>&1"

    def nickname(self, design: str) -> str:
        if design not in self._nicknames:
            self._nicknames[design] = self.orfs._design_nickname(design)
        return self._nicknames[design]

    def design_config(self, knobs_or_design, tech: str = "nangate45") -> str:
        design = getattr(knobs_or_design, "design", knobs_or_design)
        tech = getattr(knobs_or_design, "tech", tech)
        return f"./designs/{tech}/{design}/config.mk"

    def variant_dirs(self, design: str, variant: str, tech: str = "nangate45") -> dict:
        self.assert_isolated(variant)
        nick = self.nickname(design)
        return {kind: f"{kind}/{tech}/{nick}/{variant}" for kind in DIR_KINDS}

    @staticmethod
    def assert_isolated(variant: str) -> None:
        """The guard every destructive or writing call goes through."""
        if not variant or not variant.startswith(VARIANT_PREFIX) or "/" in variant or ".." in variant:
            raise VariantError(f"refusing to touch FLOW_VARIANT={variant!r}: not a floorplan variant")

    def _lock(self, design: str, variant: str, tech: str, wait: bool = False) -> str:
        self.assert_isolated(variant)
        path = f"/tmp/adaptive_iicpilot_fp_{tech}_{design}_{variant}.lock"
        mode = "-w 14400" if wait else "-n"
        return f"flock {mode} -E {BUSY_EXIT} {path}"

    def make_command(
        self, knobs_or_design, variant: str, variables: dict, target: str = "",
        tech: str = "nangate45", wait: bool = False,
    ) -> str:
        """The exact make invocation. FLOW_VARIANT is always on the command
        line, ahead of everything else, and always a floorplan variant."""
        design = getattr(knobs_or_design, "design", knobs_or_design)
        tech = getattr(knobs_or_design, "tech", tech)
        self.assert_isolated(variant)
        rendered = shell_quote_vars(variables)
        return (
            f"{self._prefix} && {self._lock(design, variant, tech, wait)} "
            f"make DESIGN_CONFIG={self.design_config(design, tech)} FLOW_VARIANT={variant} "
            f"{rendered} {target}"
        ).rstrip()

    def _run_make(self, command: str, label: str) -> tuple[bool, str]:
        wrapped = f"{command}; rc=$?; [ $rc -eq {BUSY_EXIT} ] && echo {BUSY_MARKER}; exit $rc"
        ok, out = self._sh(wrapped, on_line=self.raw_log)
        if BUSY_MARKER in out:
            raise VariantError(f"{label}: another flow already holds this variant; not running a second one")
        return ok, out

    def active_flows(self) -> list[dict]:
        """make processes currently running ORFS designs, with their variant.
        Used to say out loud when a sweep is running next to us."""
        ok, out = self._sh("ps -eo pid,args | grep -E 'make .*DESIGN_CONFIG' | grep -v grep || true")
        flows = []
        for line in out.splitlines():
            design = re.search(r"DESIGN_CONFIG=\S*/designs/\S+?/([^/\s]+)/config\.mk", line)
            if not design:
                continue
            variant = re.search(r"FLOW_VARIANT=(\S+)", line)
            flows.append(
                {"design": design.group(1), "variant": variant.group(1) if variant else "base",
                 "pid": line.split()[0]}
            )
        unique = {(f["design"], f["variant"]): f for f in flows}
        return list(unique.values())

    # --- config.mk ---

    def config_report(self, design: str, tech: str = "nangate45") -> dict:
        ok, text = self._sh(f"cat {self.flow}/designs/{tech}/{design}/config.mk")
        _, platform = self._sh(f"cat {self.flow}/platforms/{tech}/config.mk")
        report = config_mk.classify(text if ok else "")
        report["platform_defaults"] = config_mk.classify(platform)["env_overridable"]
        report["config_mk"] = f"flow/designs/{tech}/{design}/config.mk"
        return report

    def stock_util(self, design: str, tech: str = "nangate45") -> Optional[float]:
        """CORE_UTILIZATION as the design's config.mk sets it (`=` or `?=`),
        i.e. what a candidate without its own util runs at."""
        cache = self.__dict__.setdefault("_stock_utils", {})
        if (design, tech) not in cache:
            report = self.config_report(design, tech)
            raw = (report["command_line_required"].get("CORE_UTILIZATION") or {}).get("value") \
                or report["env_overridable"].get("CORE_UTILIZATION")
            try:
                cache[(design, tech)] = float(raw)
            except (TypeError, ValueError):
                cache[(design, tech)] = None
        return cache[(design, tech)]

    def util_text(self, knobs: FloorplanKnobs) -> str:
        if knobs.die_area is not None:
            return "explicit die/core area"
        util = knobs.core_utilization
        if util is None:
            util = self.stock_util(knobs.design, knobs.tech)
        return f"util {util:g}%" if util is not None else "util stock"

    def supports_variable(self, name: str) -> bool:
        """Whether this ORFS reads a variable at all (it is documented in
        variables.yaml or referenced by a flow script)."""
        ok, out = self._sh(
            f"grep -lE '(^{name}:|env\\({name}\\)|{name})' {self.flow}/scripts/variables.yaml "
            f"{self.flow}/scripts/*.tcl 2>/dev/null | head -1"
        )
        return ok and bool(out.strip())

    # --- synthesis, shared per design ---

    def synth_variant(self, design: str, tech: str = "nangate45") -> str:
        _, cfg = self._sh(f"cat {self.flow}/designs/{tech}/{design}/config.mk")
        _, sdc = self._sh(f"cat {self.orfs.sdc_path(design)}")
        digest = hashlib.sha256(f"{design}|{tech}|{cfg}|{sdc}".encode()).hexdigest()[:12]
        return f"{VARIANT_PREFIX}synth_{digest}"

    def ensure_synth(self, design: str, tech: str = "nangate45") -> dict:
        """Synthesize once into the shared synth variant, then return its
        port/macro inventory (probed from 1_synth.odb)."""
        variant = self.synth_variant(design, tech)
        dirs = self.variant_dirs(design, variant, tech)
        info_path = self.artifacts / design / f"{variant}.json"
        done = f"{self.flow}/{dirs['results']}/1_synth.odb"
        ok, _ = self._sh(f"test -s {done}")
        if not ok:
            self.emit(f"[Floorplan] synthesizing {design} once into {variant} (shared by every candidate)")
            started = time.monotonic()
            ok, out = self._run_make(self.make_command(design, variant, {}, "synth", tech, wait=True), variant)
            if not ok:
                tail = "\n".join(out.strip().splitlines()[-8:])
                raise RuntimeError(f"synthesis of {design} failed:\n{tail}")
            self.emit(f"[Floorplan] synthesis done in {time.monotonic() - started:.0f}s")
        if info_path.is_file():
            return json.loads(info_path.read_text(encoding="utf-8"))
        info = self.probe(design, variant, "1_synth.odb", tech=tech)
        info["synth_variant"] = variant
        info_path.parent.mkdir(parents=True, exist_ok=True)
        info_path.write_text(json.dumps(info, indent=1), encoding="utf-8")
        return info

    def seed_synth(self, design: str, variant: str, tech: str = "nangate45") -> None:
        source = self.variant_dirs(design, self.synth_variant(design, tech), tech)
        target = self.variant_dirs(design, variant, tech)
        steps = [f"cd {self.flow}"]
        for kind in DIR_KINDS:
            steps.append(f"mkdir -p {target[kind]}")
            steps.append(f"(cp -a {source[kind]}/. {target[kind]}/ 2>/dev/null || true)")
        ok, out = self._sh(" && ".join(steps))
        if not ok:
            raise RuntimeError(f"could not seed synthesis into {variant}: {out.strip()[-300:]}")

    # --- probing an odb ---

    def _probe_script(self) -> Path:
        path = self.artifacts / "probe.tcl"
        if not path.is_file() or path.read_text(encoding="utf-8") != PROBE_TCL:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PROBE_TCL, encoding="utf-8", newline="\n")
        return path

    def probe(
        self, design: str, variant: str, odb: str, def_out: Optional[Path] = None,
        tech: str = "nangate45",
    ) -> dict:
        """Geometry of one odb in the variant: die, core, macros, pins."""
        dirs = self.variant_dirs(design, variant, tech)
        out_dir = self.artifacts / design / variant
        out_dir.mkdir(parents=True, exist_ok=True)
        json_out = out_dir / f"{Path(odb).stem}.probe.json"
        env = (
            f"FP_ODB={self.flow}/{dirs['results']}/{odb} FP_JSON={to_wsl(json_out)} "
            f"FP_DEF={to_wsl(def_out) if def_out else ''}"
        )
        ok, out = self._sh(
            f"{self._prefix} && {env} openroad -no_init -no_splash -exit {to_wsl(self._probe_script())}"
        )
        if not ok or not json_out.is_file():
            raise RuntimeError(f"probe of {variant}/{odb} failed: {out.strip()[-400:]}")
        return json.loads(json_out.read_text(encoding="utf-8"))

    @staticmethod
    def ports_of(info: dict) -> list[iotcl.Port]:
        return [
            iotcl.Port(p["name"], p["direction"], p.get("sig_type", "SIGNAL"))
            for p in info.get("pins", [])
        ]

    @staticmethod
    def macros_of(info: dict) -> list[Macro]:
        return [Macro(m["name"], m["master"], m["width"], m["height"]) for m in info.get("macros", [])]

    # --- the candidate run ---

    def result_path(self, knobs: FloorplanKnobs, stage: str) -> Path:
        return self.artifacts / knobs.design / knobs.variant(stage) / "result.json"

    def cached(self, knobs: FloorplanKnobs, stage: str) -> Optional[dict]:
        path = self.result_path(knobs, stage)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def io_aspect(knobs: FloorplanKnobs) -> float:
        """Die height/width the pin strategy should plan for."""
        if knobs.die_area is not None:
            x0, y0, x1, y1 = knobs.die_area
            return (y1 - y0) / (x1 - x0)
        return knobs.aspect_ratio if knobs.aspect_ratio is not None else 1.0

    def default_halo(self, knobs: FloorplanKnobs) -> tuple:
        """The halo a candidate actually runs with: its own, else what make
        resolves for the design (platform default for nangate45: 22.4 15.12)."""
        if knobs.macro_halo is not None:
            return tuple(float(v) for v in knobs.macro_halo)
        _, out = self._sh(
            f"{self._prefix} && make DESIGN_CONFIG={self.design_config(knobs)} print-MACRO_PLACE_HALO 2>/dev/null"
        )
        match = re.search(r"MACRO_PLACE_HALO:\s*([0-9.]+)\s+([0-9.]+)", out)
        return (float(match.group(1)), float(match.group(2))) if match else (0.0, 0.0)

    def run(
        self, knobs: FloorplanKnobs, stage: str = Stage.SCREEN, synth_info: Optional[dict] = None,
        force: bool = False,
    ) -> dict:
        cached = None if force else self.cached(knobs, stage)
        if cached is not None:
            cached["source"] = "CACHED"
            return cached

        variant = knobs.variant(stage)
        dirs = self.variant_dirs(knobs.design, variant, knobs.tech)
        art = self.artifacts / knobs.design / variant
        art.mkdir(parents=True, exist_ok=True)
        synth_info = synth_info or self.ensure_synth(knobs.design, knobs.tech)
        tag = f"[Floorplan] {knobs.design} {knobs.label()} ({stage}, {variant})"
        result: dict = {
            "design": knobs.design, "tech": knobs.tech, "knobs": knobs.as_dict(),
            "label": knobs.label(), "stage": stage, "variant": variant,
            "ok": False, "legal": None, "violations": [], "proof": [], "source": "REAL_ORFS",
        }
        started = time.monotonic()

        # 1. Clean ONLY this variant (FLOW_VARIANT on the same command line).
        self._run_make(self.make_command(knobs, variant, {}, "clean_all"), tag)
        self.seed_synth(knobs.design, variant, knobs.tech)

        # 2. Pin constraints.
        ports = self.ports_of(synth_info)
        aspect = self.io_aspect(knobs)
        io_text = iotcl.generate(ports, knobs.io_strategy, knobs.design, aspect)
        io_path = None
        if io_text is not None:
            io_path = art / "io.tcl"
            io_path.write_text(io_text, encoding="utf-8", newline="\n")
            self.emit(
                f"[Floorplan] {knobs.io_strategy}: pins per side {iotcl.side_summary(ports, knobs.io_strategy, aspect)}"
            )
        result["pin_groups"] = iotcl.pin_groups(ports, knobs.io_strategy)
        result["util_text"], result["scope"] = self.util_text(knobs), knobs.scope

        halo = self.default_halo(knobs) if knobs.macro_mode != "none" else (0.0, 0.0)
        result["halo"] = list(halo)
        macro_path = art / "macros.tcl" if knobs.macro_mode == "manual" else None
        variables = knobs.make_vars(
            io_tcl=to_wsl(io_path) if io_path else None,
            macro_tcl=to_wsl(macro_path) if macro_path else None,
        )
        result["make_vars"] = variables
        self.emit(f"{tag}: starting")

        # 3. Manual macros: initialize the floorplan, read the real core, then
        #    generate and legality-check the placement BEFORE going further.
        if macro_path is not None:
            target = f"{dirs['results']}/2_1_floorplan.odb"
            ok, out = self._run_make(self.make_command(knobs, variant, variables, target), tag)
            if not ok:
                return self._fail(result, art, out, started, "floorplan initialization failed")
            fp = self.probe(knobs.design, variant, "2_1_floorplan.odb", tech=knobs.tech)
            core = Rect(*fp["core"])
            try:
                placement = macros.generate(
                    knobs.macro_strategy, self.macros_of(fp), core, halo, knobs.macro_channel,
                    moves=knobs.macro_moves,
                )
            except macros.PlacementError as exc:
                return self._illegal(result, art, [str(exc)], started)
            violations = macros.check_legality(placement, core, halo)
            result["planned_macros"] = [p.__dict__ for p in placement]
            if violations:
                return self._illegal(result, art, violations, started)
            self.emit(
                f"[Floorplan] pre-run legality OK: {len(placement)} macros inside core "
                f"{core.width:.1f}x{core.height:.1f}um, no overlaps, {halo[0]:g}x{halo[1]:g}um halos clear"
            )
            macro_path.write_text(macros.to_tcl(placement, knobs.macro_strategy), encoding="utf-8", newline="\n")

        # 4. The flow itself.
        target = f"{dirs['results']}/{SCREEN_TARGET}" if stage == Stage.SCREEN else ""
        ok, out = self._run_make(self.make_command(knobs, variant, variables, target), tag)
        self._sh(self.make_command(knobs, variant, variables, "metadata-generate") + " >/dev/null 2>&1")
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        if not ok:
            result["failure"] = "ORFS flow returned non-zero"
            result["log_tail"] = out.strip().splitlines()[-12:]

        meta_ok, meta_raw = self._sh(f"cat {self.flow}/{dirs['reports']}/metadata.json")
        meta = json.loads(meta_raw) if meta_ok and meta_raw.strip().startswith("{") else {}
        result["metrics"] = self.metrics_from(meta, knobs, synth_info)
        result["ok"] = ok and bool(meta)

        # 5. Layout export, proofs, post-run legality, picture.
        try:
            self._export_and_render(knobs, variant, art, result, halo)
        except RuntimeError as exc:
            self.emit(f"[Floorplan] layout export failed: {exc}")
        result["proof"] = self.collect_proof(knobs, variant, variables) + self.measured_proof(knobs, result)
        for line in result["proof"]:
            self.emit(f"[Floorplan] proof {line}")
        self._write(art, result)
        m = result["metrics"]
        self.emit(
            f"{tag}: {'OK' if result['ok'] else 'FAILED'} in {result['elapsed_s']:.0f}s "
            f"congestion={m.get('congestion')} io_hpwl={m.get('io_hpwl_um')}um "
            f"gp_wl={m.get('gp_wirelength_um')}um die={m.get('die_area_um2')}um2"
            + (f" wns={m.get('wns_ns')}ns" if stage == Stage.FULL else "")
        )
        return result

    def _write(self, art: Path, result: dict) -> None:
        tmp = art / "result.json.tmp"
        tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
        os.replace(tmp, art / "result.json")

    def _fail(self, result: dict, art: Path, out: str, started: float, why: str) -> dict:
        result.update(ok=False, failure=why, elapsed_s=round(time.monotonic() - started, 1),
                      log_tail=out.strip().splitlines()[-12:], metrics={})
        self.emit(f"[Floorplan] {result['label']}: FAILED ({why})")
        self._write(art, result)
        return result

    def _illegal(self, result: dict, art: Path, violations: list, started: float) -> dict:
        result.update(ok=False, legal=False, violations=violations, failure="illegal macro placement",
                      elapsed_s=round(time.monotonic() - started, 1), metrics={})
        self.emit(f"[Floorplan] {result['label']}: ILLEGAL, not run: {'; '.join(violations[:3])}")
        self._write(art, result)
        return result

    def metrics_from(self, meta: dict, knobs: FloorplanKnobs, synth_info: dict) -> dict:
        dbu = float(synth_info.get("dbu") or 2000)
        io_hpwl = _num(meta, "design__io__hpwl")
        stage_secs = [
            float(v) for k, v in meta.items()
            if k.endswith("__elapsed_seconds") and not k.startswith("synth")
            and isinstance(v, (int, float, str)) and str(v).replace(".", "", 1).isdigit()
        ]
        prefixes = {k.split("__")[0] for k in meta}
        from ..eda_interface import STAGES_ORFS
        reached = [s for s in STAGES_ORFS if s in prefixes]
        return {
            "reached": reached[-1] if reached else "none",
            "die_area_um2": _num(meta, "floorplan__design__die__area"),
            "core_area_um2": _num(meta, "floorplan__design__core__area"),
            "stdcell_util": _num(meta, "floorplan__design__instance__utilization"),
            "macro_count": _num(meta, "floorplan__design__instance__count__macros"),
            "io_hpwl_um": round(io_hpwl / dbu, 1) if io_hpwl is not None else None,
            "gp_wirelength_um": _num(meta, "globalplace__route__wirelength__estimated"),
            "congestion": _num(meta, "globalplace__gpl__routability__congestion"),
            "gp_wns_ns": _num(meta, "globalplace__timing__setup__ws"),
            "wns_ns": _num(meta, "finish__timing__setup__ws"),
            "tns_ns": _num(meta, "finish__timing__setup__tns"),
            "routed_wirelength_um": _num(meta, "detailedroute__route__wirelength"),
            "drc_errors": _num(meta, "detailedroute__route__drc_errors"),
            "power_mw": (_num(meta, "finish__power__total") or 0) * 1000 or None,
            # Reg-to-reg fmax from the real clock's per-clock key, as the DSE
            # path does; a screen run (no finish stage) gets None with a note.
            **self.orfs.fmax_fields(meta, knobs.design),
            "flow_elapsed_s": round(sum(stage_secs), 1),
            "timing_threshold_ns": self.orfs.timing_threshold_ns(
                knobs.design, self.orfs.stock_clock_ns(knobs.design) or 0.0
            ),
        }

    def _export_and_render(
        self, knobs: FloorplanKnobs, variant: str, art: Path, result: dict, halo: tuple,
    ) -> None:
        """write_def from the floorplan odb; pins from 3_2_place_iop when the
        run got that far, since that is where place_pins honours the
        constraints. Then post-run legality on where the macros really are."""
        fp_def = art / "floorplan.def"
        fp = self.probe(knobs.design, variant, "2_floorplan.odb", def_out=fp_def, tech=knobs.tech)
        ok, _ = self._sh(f"test -s {self.flow}/{self.variant_dirs(knobs.design, variant, knobs.tech)['results']}/3_2_place_iop.odb")
        if ok:
            self.probe(knobs.design, variant, "3_2_place_iop.odb", def_out=art / "place_iop.def", tech=knobs.tech)
        result["def"] = str(fp_def)

        core = Rect(*fp["core"])
        result["die_rect"], result["core_rect"] = fp["die"], fp["core"]
        placed = [
            macros.Placement(m["name"], m["master"], m["bbox"][0], m["bbox"][1],
                             m["bbox"][2] - m["bbox"][0], m["bbox"][3] - m["bbox"][1])
            for m in fp["macros"]
        ]
        result["macros"] = [p.__dict__ for p in placed]
        if placed:
            violations = macros.check_legality(placed, core, halo)
            result["legal"] = not violations
            result["violations"] = violations
            self.emit(
                f"[Floorplan] post-run legality ({len(placed)} macros as placed): "
                + ("LEGAL" if not violations else f"ILLEGAL: {'; '.join(violations[:3])}")
            )

        self._render(knobs, art, result, fp, halo)

    def _render(self, knobs: FloorplanKnobs, art: Path, result: dict, fp: dict, halo: tuple) -> None:
        """SVG from the floorplan DEF, with pins from the IO-placement DEF
        when the run got that far (that is where place_pins honours the
        constraints)."""
        layout = defsvg.parse_def(
            (art / "floorplan.def").read_text(encoding="utf-8", errors="replace"),
            macro_masters={m["master"] for m in fp["macros"]},
        )
        pin_source = "2_floorplan.odb"
        iop_def = art / "place_iop.def"
        if iop_def.is_file():
            layout.pins = defsvg.parse_def(iop_def.read_text(encoding="utf-8", errors="replace"), macro_masters=set()).pins
            pin_source = "3_2_place_iop.odb"
        result["pin_source"] = pin_source
        core = Rect(*fp["core"])
        m = result.get("metrics") or {}
        subtitle = (
            f"die {m.get('die_area_um2') or 0:.0f} um2 | congestion {m.get('congestion') or 0:.4f} | "
            f"IO HPWL {m.get('io_hpwl_um') or 0:.0f} um | pins from {pin_source}"
        )
        svg = defsvg.render_svg(
            layout, core=core, macro_sizes={mm["master"]: (mm["width"], mm["height"]) for mm in fp["macros"]},
            halo=halo, pin_groups=result.get("pin_groups"),
            title=f"{knobs.design} - {knobs.label()} ({result['stage'].lower()}) | "
                  f"{result.get('util_text') or self.util_text(knobs)}, {knobs.scope}",
            subtitle=subtitle,
        )
        svg_path = art / "floorplan.svg"
        svg_path.write_text(svg, encoding="utf-8")
        result["svg"] = str(svg_path)

    def refresh_proof(self, knobs: FloorplanKnobs, stage: str) -> Optional[dict]:
        """Recompute the proof lines of a finished run from its own logs and
        the floorplan probe already on disk. No flow runs."""
        result = self.cached(knobs, stage)
        if result is None:
            return None
        art = self.artifacts / knobs.design / result["variant"]
        probe = art / "2_floorplan.probe.json"
        if probe.is_file():
            fp = json.loads(probe.read_text(encoding="utf-8"))
            result["die_rect"], result["core_rect"] = fp["die"], fp["core"]
        variables = result.get("make_vars") or knobs.make_vars()
        result["util_text"], result["scope"] = self.util_text(knobs), knobs.scope
        result["proof"] = self.collect_proof(knobs, result["variant"], variables) + self.measured_proof(knobs, result)
        if probe.is_file() and (art / "floorplan.def").is_file():
            self._render(knobs, art, result, fp, tuple(result.get("halo") or (0.0, 0.0)))
        self._write(art, result)
        return result

    @staticmethod
    def measured_proof(knobs: FloorplanKnobs, result: dict) -> list[str]:
        """Geometry read back from the floorplan odb, compared with what was
        asked for. This is the only proof for explicit DIE_AREA/CORE_AREA,
        whose initialize_floorplan call ORFS does not echo to the log."""
        lines = []
        die, core = result.get("die_rect"), result.get("core_rect")
        if knobs.die_area is not None and die:
            lines.append(
                f"measured die ({die[0]:g},{die[1]:g})-({die[2]:g},{die[3]:g}) "
                f"for DIE_AREA {' '.join(f'{v:g}' for v in knobs.die_area)}"
            )
        if knobs.aspect_ratio is not None and core and core[2] > core[0]:
            measured = (core[3] - core[1]) / (core[2] - core[0])
            lines.append(f"measured core aspect (h/w) {measured:.3f} for CORE_ASPECT_RATIO {knobs.aspect_ratio:g}")
        if knobs.core_margin is not None and die and core:
            gaps = (core[0] - die[0], core[1] - die[1], die[2] - core[2], die[3] - core[3])
            lines.append(
                f"measured die-to-core gaps {min(gaps):.2f}-{max(gaps):.2f}um for CORE_MARGIN {knobs.core_margin:g}"
            )
        return [f"geometry: {line}" for line in lines]

    def collect_proof(self, knobs: FloorplanKnobs, variant: str, variables: dict) -> list[str]:
        """Evidence, from this variant's own logs and make's own resolution,
        that each override actually reached the flow."""
        dirs = self.variant_dirs(knobs.design, variant, knobs.tech)
        logs = f"{self.flow}/{dirs['logs']}"
        proofs: list[str] = []
        names = [n for n, v in variables.items() if v != ""]
        if names:
            _, out = self._sh(
                f"{self._prefix} && make DESIGN_CONFIG={self.design_config(knobs)} FLOW_VARIANT={variant} "
                f"{shell_quote_vars(variables)} {' '.join('print-' + n for n in names)} 2>/dev/null"
            )
            for line in out.splitlines():
                if any(line.startswith(f"{n}:") for n in names):
                    proofs.append(f"make resolves {line.strip()}")
        _, out = self._sh(
            f"grep -hE '^initialize_floorplan|\\[Floorplan\\]|^source .*(io|macros)\\.tcl|^rtl_macro_placer|MPL-0035' "
            f"{logs}/2_1_floorplan.log {logs}/2_2_floorplan_macro.log 2>/dev/null | head -40"
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("[INFO MPL-0035]"):
                line = line.replace("[INFO MPL-0035] ", "log: place_macro -> ")
            elif line:
                line = f"log: {line}"
            if line and line not in proofs:
                proofs.append(line[:300])
        return proofs
