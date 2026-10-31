"""SignoffAgent: physical verification of a finished run, keyed by ConfigKey.

For one measured full-flow run it reports
  * KLayout DRC of the run's 6_final.gds with the repo's FreePDK45 deck
    (signoff/decks/FreePDK45_full.lydrc: the klayoutmatthias FreePDK45 deck
    with CONTACT.3 rewritten as cont.not(active).not(poly).not(metal1)),
    violation count per rule and per rule category, parsed from the .lyrdb;
  * the detailed router's final DRT-0199 violation count and the ANT-0002 /
    ANT-0001 antenna net / pin violation counts, from 5_2_route.log;
  * LVS: not available, because ORFS ships no Nangate45 LVS deck.

Everything it writes goes under signoff/runs/<design>/<variant>/ in the repo;
the ORFS tree is only read. Results are stored in the shared memory.db
(signoff_runs, keyed by the run's ConfigKey) and replayed from there.

Only a run whose own 6_final.gds still exists can be signed off: a FloorplanAgent
FULL run (fp_full_*) or a proven clock-sweep run (clk_<cache key>). A legacy run
in ORFS's shared `base` variant was overwritten and is reported NOT_RUN.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Optional

from .configkey import ConfigKey
from .sqlite_memory import DEFAULT_DB

REPO = Path(__file__).resolve().parents[1]
DECK = REPO / "signoff" / "decks" / "FreePDK45_full.lydrc"
LVS_STATUS = "not available (ORFS ships no Nangate45 LVS deck)"

_DRT_RE = re.compile(r"\[INFO DRT-0199\]\s+Number of violations = (\d+)")
_ANT_NET_RE = re.compile(r"\[INFO ANT-0002\] Found (\d+) net violations")
_ANT_PIN_RE = re.compile(r"\[INFO ANT-0001\] Found (\d+) pin violations")


def default_runs_dir() -> Path:
    override = os.environ.get("ADAPTIVE_IICPILOT_SIGNOFF_DIR")
    return Path(override) if override else REPO / "signoff" / "runs"


def rule_category(rule: str) -> str:
    """METAL2.3 -> METAL2, CONTACT.3 -> CONTACT, METAL4_ANTENNA -> ANTENNA."""
    if rule.endswith("_ANTENNA"):
        return "ANTENNA"
    return rule.split(".", 1)[0]


def parse_lyrdb(text: str) -> dict:
    """KLayout report database -> violation counts. Every rule the deck
    defined is listed (so "0 over N rules" is checkable); an item's
    multiplicity counts as that many violations."""
    root = ET.fromstring(text)
    rules = {c.findtext("name"): 0 for c in root.iterfind("./categories/category")}
    for item in root.iterfind("./items/item"):
        rule = (item.findtext("category") or "").strip().strip("'")
        try:
            n = int(item.findtext("multiplicity") or 1)
        except ValueError:
            n = 1
        rules[rule] = rules.get(rule, 0) + n
    categories: dict = {}
    for rule, n in rules.items():
        categories[rule_category(rule)] = categories.get(rule_category(rule), 0) + n
    return {
        "total": sum(rules.values()),
        "rules_checked": len(rules),
        "by_category": dict(sorted(categories.items())),
        "violating_rules": {r: n for r, n in sorted(rules.items()) if n},
        "top_cell": root.findtext("top-cell"),
    }


def parse_route_log(text: str) -> dict:
    """The router's own checks. DRT-0199 is printed after every detailed
    routing iteration; the last one is what the routed design has. Antenna
    checks run more than once too; the last pair is the final state."""
    def last(regex: re.Pattern) -> Optional[int]:
        found = regex.findall(text)
        return int(found[-1]) if found else None

    return {
        "router_drc": last(_DRT_RE),
        "router_iterations": len(_DRT_RE.findall(text)),
        "antenna_net": last(_ANT_NET_RE),
        "antenna_pin": last(_ANT_PIN_RE),
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signoff_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    config_key TEXT NOT NULL,
    label TEXT,
    variant TEXT,
    gds_path TEXT,
    deck TEXT,
    drc_total INTEGER,
    drc_rules_checked INTEGER,
    drc_by_category TEXT,
    drc_violating_rules TEXT,
    router_drc INTEGER,
    antenna_net INTEGER,
    antenna_pin INTEGER,
    lvs TEXT,
    clean INTEGER,
    lyrdb_path TEXT,
    elapsed_s REAL,
    UNIQUE (design, tech, config_key)
);
"""


def key_json(key: ConfigKey) -> str:
    return json.dumps(key.as_dict(), sort_keys=True)


class SignoffMemory:
    """signoff_runs in the same memory.db SQLiteMemory and FloorplanMemory use."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        path = db_path or os.environ.get("ADAPTIVE_IICPILOT_MEMORY_DB") or DEFAULT_DB
        self.db_path = str(path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record(self, key: ConfigKey, result: dict) -> None:
        drc, router = result["drc"], result["router"]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO signoff_runs (ts, design, tech, config_key, label, variant, gds_path, deck,
                    drc_total, drc_rules_checked, drc_by_category, drc_violating_rules, router_drc,
                    antenna_net, antenna_pin, lvs, clean, lyrdb_path, elapsed_s)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (design, tech, config_key) DO UPDATE SET
                    ts=excluded.ts, label=excluded.label, variant=excluded.variant,
                    gds_path=excluded.gds_path, deck=excluded.deck, drc_total=excluded.drc_total,
                    drc_rules_checked=excluded.drc_rules_checked, drc_by_category=excluded.drc_by_category,
                    drc_violating_rules=excluded.drc_violating_rules, router_drc=excluded.router_drc,
                    antenna_net=excluded.antenna_net, antenna_pin=excluded.antenna_pin, lvs=excluded.lvs,
                    clean=excluded.clean, lyrdb_path=excluded.lyrdb_path, elapsed_s=excluded.elapsed_s
                """,
                (
                    time.time(), key.design, key.tech, key_json(key), result.get("label"),
                    result.get("variant"), result.get("gds"), drc.get("deck"), drc["total"],
                    drc["rules_checked"], json.dumps(drc["by_category"]), json.dumps(drc["violating_rules"]),
                    router.get("router_drc"), router.get("antenna_net"), router.get("antenna_pin"),
                    result["lvs"], int(bool(result["clean"])), drc.get("lyrdb"), result.get("elapsed_s"),
                ),
            )
            self._conn.commit()

    def lookup(self, key: ConfigKey) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM signoff_runs WHERE design=? AND tech=? AND config_key=?",
                (key.design, key.tech, key_json(key)),
            ).fetchone()
        return _from_row(dict(row)) if row else None

    def runs(self, design: str, tech: str = "nangate45") -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM signoff_runs WHERE design=? AND tech=? ORDER BY ts", (design, tech)).fetchall()
        return [_from_row(dict(r)) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _from_row(row: dict) -> dict:
    """A stored row in the same shape SignoffAgent.signoff returns."""
    return {
        "design": row["design"], "config_key": json.loads(row["config_key"]), "label": row["label"],
        "variant": row["variant"], "gds": row["gds_path"],
        "drc": {"total": row["drc_total"], "rules_checked": row["drc_rules_checked"],
                "by_category": json.loads(row["drc_by_category"] or "{}"),
                "violating_rules": json.loads(row["drc_violating_rules"] or "{}"),
                "deck": row["deck"], "lyrdb": row["lyrdb_path"]},
        "router": {"router_drc": row["router_drc"], "antenna_net": row["antenna_net"],
                   "antenna_pin": row["antenna_pin"]},
        "lvs": row["lvs"], "clean": bool(row["clean"]), "elapsed_s": row["elapsed_s"],
    }


def verdict_line(result: dict) -> str:
    """One deterministic line; the chat repeats it, never re-derives it."""
    drc, router = result["drc"], result["router"]
    status = "DRC CLEAN" if result["clean"] else "DRC NOT CLEAN"

    def count(value: Optional[int]) -> str:
        return "?" if value is None else str(value)

    worst = ", ".join(f"{r} {n}" for r, n in sorted(drc["violating_rules"].items(),
                                                     key=lambda rn: -rn[1])[:3])
    return (
        f"{status} - {result['design']} {result['label']} ({result['variant']}): KLayout FreePDK45 DRC "
        f"{drc['total']} violation(s) over {drc['rules_checked']} rules"
        + (f" (worst: {worst})" if worst else "")
        + f"; router DRT-0199 {count(router.get('router_drc'))}; antenna {count(router.get('antenna_net'))} net / "
        f"{count(router.get('antenna_pin'))} pin (ANT-0002/ANT-0001); LVS {result['lvs']}"
    )


class SignoffAgent:
    def __init__(
        self,
        adapter=None,
        memory: Optional[SignoffMemory] = None,
        emit: Callable[[str], None] = print,
        runs_dir: Optional[Path] = None,
        deck: Optional[Path] = None,
    ) -> None:
        if adapter is None:
            from .eda_interface import ORFSAdapter

            adapter = ORFSAdapter()
        self.adapter = adapter
        self.memory = memory if memory is not None else SignoffMemory()
        self.emit = emit
        self.runs_dir = Path(runs_dir) if runs_dir else default_runs_dir()
        self.deck = Path(deck) if deck else DECK
        self.flow = f"{adapter.orfs_root}/flow"

    # --- plumbing ---

    def _sh(self, command: str) -> tuple[bool, str]:
        return self.adapter._shell(command)

    def _read(self, path: str) -> Optional[str]:
        ok, text = self._sh(f"cat {path}")
        return text if ok else None

    def _dir(self, kind: str, key: ConfigKey, variant: str) -> str:
        nick = self.adapter._design_nickname(key.design)
        return f"{self.flow}/{kind}/{key.tech}/{nick}/{variant}"

    def variant_of(self, point) -> Optional[str]:
        """The ORFS variant whose files are THIS run's: the FloorplanAgent's
        fp_full_* variant, else a clock-sweep clk_<cache key>. A candidate
        counts only if its 6_final.gds exists."""
        candidates = []
        if point.variant:
            candidates.append(point.variant)
        origin = point.origin or ""
        if origin.startswith(".orfs_cache/"):
            candidates.append("clk_" + Path(origin).stem.split("-", 1)[1])
        for variant in candidates:
            ok, _ = self._sh(f"test -s {self._dir('results', point.key, variant)}/6_final.gds")
            if ok:
                return variant
        return None

    # --- the stage ---

    def signoff(self, key: ConfigKey, point=None, run: bool = True, force: bool = False) -> dict:
        """Signoff result for one ConfigKey: from memory when stored, else
        measured now (run=True) or NOT_RUN (run=False). Never estimated."""
        stored = None if force else self.memory.lookup(key)
        if stored is not None:
            stored.update(source="MEMORY", verdict=verdict_line(stored))
            self.emit(f"[Signoff] {key.label()}: replayed from memory (signoff_runs)")
            return stored
        base = {"design": key.design, "config_key": key.as_dict(), "label": key.label()}
        if point is None:
            from .measurements import FULL, Measurements

            point = Measurements(adapter=self.adapter).find(key, FULL)
        if point is None or not point.ok:
            why = "no successful full-flow run of this config is measured"
            return {**base, "source": "NOT_RUN", "verdict": f"NOT_RUN - {key.design} {key.label()}: {why}"}
        variant = self.variant_of(point)
        if variant is None:
            why = ("its 6_final.gds no longer exists (a legacy run in the shared base variant, "
                   "overwritten by later runs)")
            return {**base, "source": "NOT_RUN", "verdict": f"NOT_RUN - {key.design} {key.label()}: {why}"}
        if not run:
            return {**base, "variant": variant, "source": "NOT_RUN",
                    "verdict": f"NOT_RUN - {key.design} {key.label()} ({variant}) has not been DRC-checked yet; "
                               f"ask to run DRC on it (KLayout, about 5 min)"}
        return self._measure(key, variant, base)

    def _measure(self, key: ConfigKey, variant: str, base: dict) -> dict:
        from .floorplan.runner import to_wsl

        started = time.monotonic()
        gds = f"{self._dir('results', key, variant)}/6_final.gds"
        out = self.runs_dir / key.design / variant
        out.mkdir(parents=True, exist_ok=True)
        lyrdb, log = out / "6_drc.lyrdb", out / "drc.log"
        if lyrdb.exists():
            lyrdb.unlink()  # a stale report must never be read as this run's
        self.emit(f"[Signoff] KLayout DRC on {variant}/6_final.gds with {self.deck.name}")
        ok, text = self._sh(
            f"klayout -b -r {to_wsl(self.deck)} -rd in_gds={gds} -rd report_file={to_wsl(lyrdb)} "
            f"> {to_wsl(log)} 2>&1"
        )
        if not ok or not lyrdb.is_file():
            tail = log.read_text(errors="replace").strip().splitlines()[-5:] if log.is_file() else [text]
            return {**base, "variant": variant, "source": "FAILED",
                    "verdict": f"FAILED - KLayout DRC of {variant} did not produce a report: {' | '.join(tail)}"}
        drc = parse_lyrdb(lyrdb.read_text(encoding="utf-8"))
        drc.update(deck=str(self.deck.relative_to(REPO)).replace("\\", "/") if self.deck.is_relative_to(REPO)
                   else str(self.deck), lyrdb=str(lyrdb.relative_to(REPO)).replace("\\", "/")
                   if lyrdb.is_relative_to(REPO) else str(lyrdb))
        route_log = self._read(f"{self._dir('logs', key, variant)}/5_2_route.log") or ""
        router = parse_route_log(route_log)
        clean = (drc["total"] == 0 and router["router_drc"] == 0
                 and router["antenna_net"] == 0 and router["antenna_pin"] == 0)
        result = {**base, "variant": variant, "gds": gds, "drc": drc, "router": router, "lvs": LVS_STATUS,
                  "clean": clean, "elapsed_s": round(time.monotonic() - started, 1), "source": "MEASURED"}
        (out / "signoff.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        self.memory.record(key, result)
        result["verdict"] = verdict_line(result)
        self.emit(f"[Signoff] {result['verdict']}")
        return result
