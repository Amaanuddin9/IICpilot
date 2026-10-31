"""Floorplan memory: results keyed by every floorplan knob, SCREEN or FULL.

Lives in its own tables inside the same memory.db SQLiteMemory uses, so one
file holds everything the system has learned, but the existing runs /
corrections tables (and every query over them) are untouched.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..sqlite_memory import DEFAULT_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS floorplan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    knobs_key TEXT NOT NULL,
    knobs_json TEXT NOT NULL,
    label TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('SCREEN', 'FULL')),
    variant TEXT,
    ok INTEGER,
    legal INTEGER,
    die_area_um2 REAL,
    core_area_um2 REAL,
    io_hpwl_um REAL,
    gp_wirelength_um REAL,
    congestion REAL,
    wns_ns REAL,
    elapsed_s REAL,
    svg_path TEXT,
    UNIQUE (design, tech, knobs_key, kind)
);
CREATE TABLE IF NOT EXISTS floorplan_best (
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    objective TEXT NOT NULL,
    knobs_json TEXT NOT NULL,
    label TEXT,
    kind TEXT,
    score REAL,
    svg_path TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (design, tech, objective)
);
CREATE TABLE IF NOT EXISTS floorplan_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    proposed TEXT,
    final TEXT,
    rationale TEXT
);
"""


class FloorplanMemory:
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

    def record(self, result: dict) -> None:
        """Upsert one candidate result. A rerun of the same knobs at the same
        kind replaces the row: it is the same measurement, re-taken."""
        m = result.get("metrics") or {}
        knobs = result["knobs"]
        key = json.dumps(knobs, sort_keys=True)
        legal = result.get("legal")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO floorplan_runs (ts, design, tech, knobs_key, knobs_json, label, kind,
                    variant, ok, legal, die_area_um2, core_area_um2, io_hpwl_um, gp_wirelength_um,
                    congestion, wns_ns, elapsed_s, svg_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (design, tech, knobs_key, kind) DO UPDATE SET
                    ts=excluded.ts, variant=excluded.variant, ok=excluded.ok, legal=excluded.legal,
                    die_area_um2=excluded.die_area_um2, core_area_um2=excluded.core_area_um2,
                    io_hpwl_um=excluded.io_hpwl_um, gp_wirelength_um=excluded.gp_wirelength_um,
                    congestion=excluded.congestion, wns_ns=excluded.wns_ns,
                    elapsed_s=excluded.elapsed_s, svg_path=excluded.svg_path, label=excluded.label
                """,
                (
                    time.time(), result["design"], result.get("tech", "nangate45"), key, key,
                    result.get("label"), result["stage"], result.get("variant"),
                    int(bool(result.get("ok"))), None if legal is None else int(bool(legal)),
                    m.get("die_area_um2"), m.get("core_area_um2"), m.get("io_hpwl_um"),
                    m.get("gp_wirelength_um"), m.get("congestion"), m.get("wns_ns"),
                    result.get("elapsed_s"), result.get("svg"),
                ),
            )
            self._conn.commit()

    def lookup(self, knobs: dict, kind: str, tech: str = "nangate45") -> Optional[dict]:
        key = json.dumps(knobs, sort_keys=True)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM floorplan_runs WHERE design=? AND tech=? AND knobs_key=? AND kind=?",
                (knobs["design"], tech, key, kind),
            ).fetchone()
        return dict(row) if row else None

    def runs(self, design: str, kind: Optional[str] = None, tech: str = "nangate45") -> list[dict]:
        query = "SELECT * FROM floorplan_runs WHERE design=? AND tech=?"
        args: list[Any] = [design, tech]
        if kind:
            query += " AND kind=?"
            args.append(kind)
        with self._lock:
            return [dict(r) for r in self._conn.execute(query + " ORDER BY ts", args)]

    def set_best(self, design: str, objective: str, result: dict, score: float) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO floorplan_best (design, tech, objective, knobs_json, label, kind, score, svg_path, ts)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT (design, tech, objective) DO UPDATE SET knobs_json=excluded.knobs_json,
                    label=excluded.label, kind=excluded.kind, score=excluded.score,
                    svg_path=excluded.svg_path, ts=excluded.ts
                """,
                (
                    design, result.get("tech", "nangate45"), objective,
                    json.dumps(result["knobs"], sort_keys=True), result.get("label"),
                    result.get("stage"), score, result.get("svg"), time.time(),
                ),
            )
            self._conn.commit()

    def best(self, design: str, objective: Optional[str] = None, tech: str = "nangate45") -> list[dict]:
        query = "SELECT * FROM floorplan_best WHERE design=? AND tech=?"
        args: list[Any] = [design, tech]
        if objective:
            query += " AND objective=?"
            args.append(objective)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(query, args)]
        for row in rows:
            row["knobs"] = json.loads(row.pop("knobs_json"))
        return rows

    def record_correction(
        self, design: str, kind: str, subject: str, proposed: Any, final: Any,
        rationale: str = "", tech: str = "nangate45",
    ) -> None:
        """An engineer override of something the agent proposed — e.g. a
        macro moved by hand in a HITL card (kind="macro_move")."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO floorplan_corrections (ts, design, tech, kind, subject, proposed, final, rationale)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (time.time(), design, tech, kind, subject, json.dumps(proposed), json.dumps(final), rationale),
            )
            self._conn.commit()

    def corrections(self, design: str, kind: Optional[str] = None, tech: str = "nangate45") -> list[dict]:
        query = "SELECT * FROM floorplan_corrections WHERE design=? AND tech=?"
        args: list[Any] = [design, tech]
        if kind:
            query += " AND kind=?"
            args.append(kind)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(query + " ORDER BY ts", args)]
        for row in rows:
            row["proposed"] = json.loads(row["proposed"]) if row["proposed"] else None
            row["final"] = json.loads(row["final"]) if row["final"] else None
        return rows

    def latest_macro_moves(self, design: str, tech: str = "nangate45") -> dict:
        """macro name -> (x, y) from the most recent engineer move of each
        macro, so the next manual candidate starts from the corrected spot."""
        moves = {}
        for row in self.corrections(design, "macro_move", tech):
            if isinstance(row["final"], (list, tuple)) and len(row["final"]) == 2:
                moves[row["subject"]] = tuple(row["final"])
        return moves

    def close(self) -> None:
        with self._lock:
            self._conn.close()
