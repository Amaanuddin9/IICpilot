#!/usr/bin/env python3
"""Clock-period sweeps on real ORFS, with every run's period PROVEN.

Why this exists next to sweep.py: ORFSAdapter.run() reuses the shared `base`
FLOW_VARIANT and never checks which period the flow really saw, so a result it
caches cannot later be proven. This driver reuses the adapter for everything
it already does right (cache key, stock clock, timing allowance, metadata
parsing, cache writes) and adds the missing guarantees:

  * FLOW_VARIANT=clk_<cache key> per point, so runs never share results/
    dirs, and `make clean_all` scoped to that variant before every run;
  * knobs and SDC_FILE as make COMMAND-LINE variables (config.mk assigns
    SDC_FILE with plain `=`, which beats the environment);
  * an SDC copy that differs from the design's only in the period line;
  * proof from the run's own logs: every ABC `-D` value in 1_2_yosys.log, the
    create_clock period in 6_final.sdc, and the floorplan knobs in
    2_1_floorplan.log must match the request, or the run is discarded;
  * runtime recorded per run.

Proven results go to sweep_logs/clock_sweep/results/ and, for the main run of
each point, into the adapter cache that chat/DSE/estimator read. A legacy
(unproven) cache entry at the same key is moved to .orfs_cache/legacy_unproven/.

Run in WSL, detached (one flow at a time; resumes from the first unproven point):
    setsid nohup /home/amaanuddin/venvs/iicpilot/bin/python clock_sweep.py \
        > sweep_logs/clock_sweep/driver.log 2>&1 < /dev/null &
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from adaptive_iicpilot.eda_interface import ORFSAdapter
from adaptive_iicpilot.types import FlowConfig, RunResult

REPO = Path(__file__).resolve().parent
LOG_DIR = REPO / "sweep_logs"
WORK = LOG_DIR / "clock_sweep"
ORFS = Path(os.path.expanduser(os.environ.get("ORFS_ROOT", "~/OpenROAD-flow-scripts")))
FLOW = ORFS / "flow"
PLATFORM = "nangate45"

MULTIPLES = [0.8, 0.9, 1.0, 1.1, 1.25]
# util None = the design's own floorplan (aes ships a FLOORPLAN_DEF and no
# CORE_UTILIZATION); it is keyed as util 0 so it can never merge with a
# CORE_UTILIZATION run.
PLAN = {
    "gcd": {"util": 55.0, "aspect": 1.0, "clocks": [round(0.25 * m, 4) for m in MULTIPLES]},
    "aes": {"util": None, "aspect": 1.0, "clocks": [round(0.82 * m, 4) for m in MULTIPLES]},
    "ibex": {"util": 80.0, "aspect": 1.0, "clocks": [1.76, 1.98, 2.2, 2.42, 2.75, 2.8]},
}
# Fresh second run of a point, for the determinism check.
RERUNS = {("ibex", 2.2)}

# Named plans that replace a design's default plan (--plan NAME). The best
# measured ibex floorplan (util 85, AR 0.5) was only run at the stock clock by
# the FloorplanAgent; these put it on the clock axis. 2.2 is last on purpose: it
# cross-checks this route against the FloorplanAgent's own full run of the point.
NAMED_PLANS = {
    "ibex_u85_ar05": {"design": "ibex", "util": 85.0, "aspect": 0.5, "clocks": [2.42, 2.75, 2.8, 2.2]},
}

RAW_KEYS = {
    "wns": "finish__timing__setup__ws",
    "tns": "finish__timing__setup__tns",
    "die": "finish__design__die__area",
    "power_w": "finish__power__total",
    # ORFS's HEADLINE fmax: the max over all clocks, which for ibex is the
    # virtual I/O clock. Kept for the record under its historical name; never
    # reported as fmax (that is metrics.cp_delay_ns, from the real clock).
    "fmax_hz": "finish__timing__fmax",
    "congestion": "globalplace__gpl__routability__congestion",
    "drc": "detailedroute__route__drc_errors",
}

# `abc_new` is what synth.tcl calls instead of `abc` for some designs (aes).
_ABC_D_RE = re.compile(r"^abc(?:_new)? .*?-D\s+([0-9.]+)", re.MULTILINE)
_PERIOD_RE = re.compile(r"create_clock\s+-name\s+(\S+)\s+-period\s+([0-9.]+)")
_IFP_RE = re.compile(r"initialize_floorplan\s+-utilization\s+([0-9.]+)\s+-aspect_ratio\s+([0-9.]+)")


def landed_line(design: str, clock: float, proof: dict, flow_ok: bool, runtime: float,
                raw: dict, metrics: Optional[dict]) -> str:
    """The driver-log line for a landed run. fmax is reg-to-reg, from the real
    clock (metrics.cp_delay_ns); the headline is printed only as a labelled
    aside, so it can never be read as fmax again."""
    def scaled(name: str, factor: float, spec: str) -> str:
        value = raw.get(name)
        return "n/a" if value is None else format(float(value) * factor, spec)

    m = metrics or {}
    cp = m.get("cp_delay_ns")
    fmax = f"{1000.0 / cp:.1f}MHz ({m.get('fmax_clock')})" if cp else f"n/a ({m.get('cp_delay_note')})"
    headline_clock = m.get("orfs_fmax_headline_clock") or "?"
    return (
        f"LANDED {design} clock={clock:g}ns proof={'OK' if proof['ok'] else 'FAILED ' + '; '.join(proof['problems'])} "
        f"flow_ok={flow_ok} runtime={runtime / 60:.1f}min | wns={raw.get('wns')} tns={raw.get('tns')} "
        f"reg2reg_fmax={fmax} [orfs headline {scaled('fmax_hz', 1e-6, '.1f')}MHz from {headline_clock}, not fmax] "
        f"power={scaled('power_w', 1000, '.3f')}mW die={raw.get('die')} congestion={raw.get('congestion')} drc={raw.get('drc')}"
    )


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Sweep:
    def __init__(self, plan_name: Optional[str] = None) -> None:
        self.adapter = ORFSAdapter(orfs_root=str(ORFS))
        self.plans = dict(PLAN)
        if plan_name:
            plan = dict(NAMED_PLANS[plan_name])
            self.plans[plan.pop("design") if "design" in plan else plan_name] = plan
            self.reruns = set()
        else:
            self.reruns = set(RERUNS)
        for sub in ("results", "sdc", "runs"):
            (WORK / sub).mkdir(parents=True, exist_ok=True)

    # --- bookkeeping ---

    def log(self, design: str, text: str) -> None:
        line = f"[{now()}] {text}"
        print(line, flush=True)
        with open(LOG_DIR / f"{design}_clocks.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def config_for(self, design: str, clock: float) -> FlowConfig:
        plan = self.plans[design]
        return FlowConfig(
            design=design,
            core_utilization=plan["util"] or 0.0,
            placement_density=0.0,
            aspect_ratio=plan["aspect"],
            clock_period_ns=clock,
        )

    def result_path(self, design: str, key: str, rerun: bool) -> Path:
        return WORK / "results" / f"{design}-{key}{'-r2' if rerun else ''}.json"

    def proven(self, path: Path) -> Optional[dict]:
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        return record if record.get("proof", {}).get("ok") else None

    # --- the run ---

    def write_sdc(self, design: str, clock: float) -> Path:
        original = Path(os.path.expanduser(self.adapter.sdc_path(design))).read_text(encoding="utf-8")
        rewritten, count = re.subn(r"(?m)^(\s*set\s+clk_period\s+)[0-9.]+", rf"\g<1>{clock:g}", original, count=1)
        if count != 1:
            raise RuntimeError(f"no clk_period line in {design} SDC")
        a, b = original.splitlines(), rewritten.splitlines()
        changed = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(a) != len(b) or len(changed) > 1:
            raise RuntimeError(f"SDC copy for {design} differs in more than the period line")
        path = WORK / "sdc" / f"{design}_{clock:g}ns.sdc"
        path.write_text(rewritten, encoding="utf-8", newline="\n")
        return path

    def make_vars(self, design: str, variant: str, sdc: Path) -> str:
        plan = self.plans[design]
        parts = [f"DESIGN_CONFIG=./designs/{PLATFORM}/{design}/config.mk", f"FLOW_VARIANT={variant}"]
        if plan["util"] is not None:
            parts += [f"CORE_UTILIZATION={plan['util']:g}", f"CORE_ASPECT_RATIO={plan['aspect']:g}"]
        parts.append(f"SDC_FILE={sdc}")
        return " ".join(parts)

    def make(self, target: str, mk_vars: str, logfile: Path) -> tuple[bool, float]:
        cmd = f"cd {FLOW} && source ../env.sh >/dev/null 2>&1 && make {target} {mk_vars}"
        started = time.time()
        with open(logfile, "a", encoding="utf-8") as fh:
            fh.write(f"\n### [{now()}] $ make {target} {mk_vars}\n")
            fh.flush()
            rc = subprocess.run(["bash", "-lc", cmd], stdout=fh, stderr=subprocess.STDOUT).returncode
        return rc == 0, time.time() - started

    def wait_for_idle(self, design: str) -> None:
        """One flow at a time: never start while any other ORFS make runs."""
        while True:
            out = subprocess.run(["pgrep", "-af", "make .*DESIGN_CONFIG="], capture_output=True, text=True).stdout
            others = [l for l in out.splitlines() if "pgrep" not in l]
            if not others:
                return
            self.log(design, f"waiting: another ORFS flow is running: {others[0][:160]}")
            time.sleep(60)

    def prove(self, design: str, variant: str, clock: float) -> dict:
        nick = self.adapter._design_nickname(design)
        logs = FLOW / "logs" / PLATFORM / nick / variant
        results = FLOW / "results" / PLATFORM / nick / variant
        proof: dict = {"requested_ns": clock, "problems": []}

        synth_log = logs / "1_2_yosys.log"
        d_values = [float(v) for v in _ABC_D_RE.findall(synth_log.read_text(errors="replace"))] if synth_log.is_file() else []
        proof["abc_D"] = sorted(set(d_values))
        if not d_values:
            proof["problems"].append(f"no ABC -D value in {synth_log}")
        elif any(abs(v - clock) > 1e-6 for v in d_values):
            proof["problems"].append(f"ABC -D {proof['abc_D']} != {clock:g}")

        final_sdc = results / "6_final.sdc"
        periods = {n: float(p) for n, p in _PERIOD_RE.findall(final_sdc.read_text())} if final_sdc.is_file() else {}
        proof["final_sdc_periods"] = periods
        if not periods:
            proof["problems"].append(f"no create_clock in {final_sdc} (flow may not have finished)")
        elif any(abs(p - clock) > 5e-5 for p in periods.values()):
            proof["problems"].append(f"6_final.sdc periods {periods} != {clock:g}")

        fp_log = logs / "2_1_floorplan.log"
        fp_text = fp_log.read_text(errors="replace") if fp_log.is_file() else ""
        plan = self.plans[design]
        if plan["util"] is None:
            proof["floorplan"] = "read_def -floorplan_initialize" if "read_def -floorplan_initialize" in fp_text else None
            if not proof["floorplan"]:
                proof["problems"].append("expected the design's FLOORPLAN_DEF; not found in floorplan log")
        else:
            m = _IFP_RE.search(fp_text)
            proof["floorplan"] = {"util": float(m.group(1)), "aspect": float(m.group(2))} if m else None
            if not m or abs(float(m.group(1)) - plan["util"]) > 1e-6 or abs(float(m.group(2)) - plan["aspect"]) > 1e-6:
                proof["problems"].append(f"floorplan knobs {proof['floorplan']} != util {plan['util']:g} AR {plan['aspect']:g}")

        proof["ok"] = not proof["problems"]
        return proof

    def run_point(self, design: str, clock: float, rerun: bool) -> dict:
        config = self.config_for(design, clock)
        key = self.adapter.cache_key(config)
        variant = f"clk_{key}" + ("_r2" if rerun else "")
        sdc = self.write_sdc(design, clock)
        mk_vars = self.make_vars(design, variant, sdc)
        logfile = WORK / "runs" / f"{design}_{variant}.make.log"
        logfile.write_text("", encoding="utf-8")
        stock = self.adapter.stock_clock_ns(design)

        self.wait_for_idle(design)
        self.log(design, f"START {design} clock={clock:g}ns ({clock / stock:.3g}x stock) "
                         f"variant={variant}{' [determinism re-run]' if rerun else ''}")
        self.make("clean_all", mk_vars, logfile)
        flow_ok, runtime = self.make("", mk_vars, logfile)
        self.make("metadata-generate", mk_vars, logfile)

        nick = self.adapter._design_nickname(design)
        meta_path = FLOW / "reports" / PLATFORM / nick / variant / "metadata.json"
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        proof = self.prove(design, variant, clock)
        if not meta:
            proof["problems"].append(f"no metadata.json at {meta_path}")
            proof["ok"] = False

        metrics = self.adapter._parse_metadata(meta, config) if meta else None
        record = {
            "design": design,
            "key": {"util": config.core_utilization if self.plans[design]["util"] is not None else "stock FLOORPLAN_DEF",
                    "density": "auto", "aspect": config.aspect_ratio, "clock_ns": clock},
            "config": asdict(config),
            "cache_key": key,
            "variant": variant,
            "rerun": rerun,
            "stock_clock_ns": stock,
            "multiplier": round(clock / stock, 4),
            "flow_ok": flow_ok,
            "runtime_s": round(runtime, 1),
            "finished": now(),
            "make_vars": mk_vars,
            "sdc_file": str(sdc),
            "proof": proof,
            "raw": {name: meta.get(k) for name, k in RAW_KEYS.items()},
            "metrics": asdict(metrics) if metrics else None,
            "timing_limit_ns": self.adapter.timing_threshold_ns(design, clock),
        }
        self.result_path(design, key, rerun).write_text(json.dumps(record, indent=2), encoding="utf-8")

        if proof["ok"] and metrics and not rerun:
            self.store_in_cache(config, metrics, flow_ok, logfile)

        self.log(design, landed_line(design, clock, proof, flow_ok, runtime, record["raw"], record["metrics"])
                 if meta else f"LANDED {design} clock={clock:g}ns with NO metadata; proof problems: {proof['problems']}")
        return record

    def store_in_cache(self, config: FlowConfig, metrics, flow_ok: bool, logfile: Path) -> None:
        path = self.adapter._cache_path(config.tweaked(clock_period_ns=self.adapter.effective_clock_ns(config)))
        if path.is_file():
            old = json.loads(path.read_text(encoding="utf-8"))
            if not old.get("proven"):
                legacy = path.parent / "legacy_unproven"
                legacy.mkdir(exist_ok=True)
                shutil.move(str(path), legacy / path.name)
        tail = logfile.read_text(errors="replace").strip().splitlines()[-self.adapter.LOG_TAIL_LEN:]
        result = RunResult(config=config, reached_stage=metrics.stage, metrics=metrics, ok=flow_ok,
                           failure=None if flow_ok else "ORFS flow returned non-zero", log_tail=tail)
        self.adapter._store_cached(config, result)
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["proven"] = True  # marks it as written by clock_sweep with a passing proof
        path.write_text(json.dumps(entry, indent=2), encoding="utf-8")

    def main(self, designs: list[str]) -> None:
        for design in designs:
            for clock in self.plans[design]["clocks"]:
                for rerun in ([False, True] if (design, clock) in self.reruns else [False]):
                    key = self.adapter.cache_key(self.config_for(design, clock))
                    path = self.result_path(design, key, rerun)
                    if self.proven(path) or self.reprove(path):
                        self.log(design, f"SKIP {design} clock={clock:g}ns{' r2' if rerun else ''}: already proven ({path.name})")
                        continue
                    self.run_point(design, clock, rerun)
        print(f"[{now()}] ALL DONE", flush=True)

    def reprove(self, path: Path) -> bool:
        """Re-check a record whose proof failed against that run's OWN logs,
        still on disk in its unique variant (e.g. after fixing the checker).
        Nothing is re-run; if the logs are gone or still fail, it is re-run."""
        if not path.is_file():
            return False
        record = json.loads(path.read_text(encoding="utf-8"))
        if not record.get("metrics"):
            return False
        proof = self.prove(record["design"], record["variant"], record["key"]["clock_ns"])
        if not proof["ok"]:
            return False
        proof["reproved_at"] = now()
        proof["first_check_problems"] = record["proof"]["problems"]
        record["proof"] = proof
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        config = FlowConfig(**record["config"])
        if not record["rerun"]:
            from adaptive_iicpilot.types import Metrics
            logfile = WORK / "runs" / f"{record['design']}_{record['variant']}.make.log"
            self.store_in_cache(config, Metrics(**record["metrics"]), record["flow_ok"], logfile)
        self.log(record["design"], f"REPROVED {record['design']} clock={record['key']['clock_ns']:g}ns from its own logs "
                                   f"(ABC -D {proof['abc_D']}); first check failed: {proof['first_check_problems']}")
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--designs", default="gcd,aes,ibex")
    parser.add_argument("--plan", choices=sorted(NAMED_PLANS), default=None,
                        help="run a named plan instead of the design's default one")
    args = parser.parse_args()
    if args.plan:
        Sweep(args.plan).main([NAMED_PLANS[args.plan]["design"]])
    else:
        Sweep().main([d for d in args.designs.split(",") if d])
