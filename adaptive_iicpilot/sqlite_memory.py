"""Persistent memory: a drop-in MemoryPort backed by SQLite.

This is what makes the project's central claim literally true. InMemoryMemory
demonstrates cross-session learning only within one process; this survives
process restarts, so a second `optimize ibex` tomorrow starts from what was
learned today.

Only configurations and metrics are stored — never raw flow logs. Congested and
failed runs are stored too: knowing where the wall is is exactly as valuable as
knowing the best point.

The wall is derived from real ORFS rows only. The mock backend reports
congestion on a different scale (it congests above 0.30, real nangate45 above
1.0), so letting mock rows vote would place the wall below a utilization that
real silicon routes cleanly — which is precisely what happened once.

Concurrency: the web server runs the orchestrator on background threads and
chat.py may share the same file, so the connection allows cross-thread use,
every operation takes a lock, and the journal is WAL.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .configkey import STOCK_MACROS, STOCK_PINS, ConfigKey, from_flow_config, from_row
from .types import FlowConfig, MemorySeed, Proposal, RunResult

DEFAULT_DB = Path.home() / ".adaptive_iicpilot" / "memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    util REAL,
    density REAL,
    aspect REAL,
    clock_ns REAL,
    backend TEXT,
    ok INTEGER,
    congested INTEGER,
    wns_ns REAL,
    tns_ns REAL,
    die_area_um2 REAL,
    congestion REAL,
    power_mw REAL,
    score REAL,
    -- How many times this same measurement has been served again from the
    -- result cache. A replay is not a new measurement, so it bumps this
    -- instead of inserting a row.
    replays INTEGER NOT NULL DEFAULT 1,
    first_seen REAL,
    -- Floorplan knobs of the unified ConfigKey. NULL (rows written before
    -- they existed) means the stock value: design margin, default pins,
    -- stock macro placement.
    margin REAL,
    pin_strategy TEXT,
    macro_strategy TEXT,
    -- Reg-to-reg critical path (real clock period_min); NULL = not measured.
    cp_delay_ns REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    knob TEXT NOT NULL,
    value REAL,
    ts REAL NOT NULL,
    PRIMARY KEY (design, tech, knob)
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    knob TEXT NOT NULL,
    proposed REAL,
    final REAL,
    rationale TEXT
);
CREATE TABLE IF NOT EXISTS heuristics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    design TEXT NOT NULL,
    tech TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_design_idx ON runs (design, tech);
"""


def _warn_if_wrong_platform(db_path: str, was_defaulted: bool) -> None:
    """Warn when a Windows process is about to use its own default memory.

    ORFS, Ollama and the VS Code extension host all run in WSL, so a Windows
    interpreter resolving Path.home() gets a DIFFERENT memory.db than the one
    the app actually learns into. That silently produced two divergent
    databases once already. Goes to stderr because stdout is the JSONL
    protocol channel.
    """
    if os.name != "nt" or not was_defaulted:
        return
    print(
        f"WARNING: adaptive_iicpilot memory is running on Windows and defaulted to\n"
        f"           {db_path}\n"
        f"         ORFS and the VS Code extension run inside WSL and use\n"
        f"           /home/<user>/.adaptive_iicpilot/memory.db\n"
        f"         These are separate databases. Run under the WSL interpreter, or\n"
        f"         set ADAPTIVE_IICPILOT_MEMORY_DB to choose one explicitly.",
        file=sys.stderr,
    )


class SQLiteMemory:
    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        env_path = os.environ.get("ADAPTIVE_IICPILOT_MEMORY_DB")
        path = db_path or env_path or DEFAULT_DB
        self.db_path = str(path)
        # Only the fully defaulted case is a footgun; an explicit path (tests,
        # or a deliberate env var) is a choice already made.
        _warn_if_wrong_platform(self.db_path, db_path is None and not env_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.db_path != ":memory:":
                # WAL lets the web server and chat.py hold the same file open.
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so an
        older memory.db would otherwise be missing the replay bookkeeping.
        Caller holds the lock.
        """
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "replays" not in columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN replays INTEGER NOT NULL DEFAULT 1"
            )
        if "first_seen" not in columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN first_seen REAL")
            self._conn.execute("UPDATE runs SET first_seen = ts WHERE first_seen IS NULL")
        for name, kind in (("margin", "REAL"), ("pin_strategy", "TEXT"),
                           ("macro_strategy", "TEXT"), ("cp_delay_ns", "REAL")):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")

    def deduplicate_runs(self) -> dict:
        """Collapse rows that are repeats of the same measurement.

        Repairs databases written before replays were tracked, where every cache
        replay inserted a row. Keeps the earliest row per identical measurement
        and folds the rest into its replay count.
        """
        with self._lock:
            groups = self._conn.execute(
                """
                SELECT design, tech, util, density, aspect, clock_ns, backend, ok,
                       die_area_um2, congestion,
                       COUNT(*) AS n, MIN(id) AS keep_id, SUM(replays) AS total
                  FROM runs
                 GROUP BY design, tech, util, density, aspect, clock_ns, backend, ok,
                          die_area_um2, congestion
                HAVING COUNT(*) > 1
                """
            ).fetchall()

            collapsed = []
            for group in groups:
                self._conn.execute(
                    "UPDATE runs SET replays = ? WHERE id = ?",
                    (group["total"], group["keep_id"]),
                )
                self._conn.execute(
                    """
                    DELETE FROM runs
                     WHERE id != ? AND design = ? AND tech = ? AND util = ?
                       AND density = ? AND aspect = ? AND clock_ns = ?
                       AND backend = ? AND ok = ? AND die_area_um2 = ?
                       AND congestion = ?
                    """,
                    (
                        group["keep_id"], group["design"], group["tech"], group["util"],
                        group["density"], group["aspect"], group["clock_ns"],
                        group["backend"], group["ok"], group["die_area_um2"],
                        group["congestion"],
                    ),
                )
                collapsed.append(
                    f"{group['design']} util={group['util']:g} "
                    f"density={group['density']:g}: {group['n']} rows -> 1 "
                    f"({group['total']} replays)"
                )
            self._conn.commit()
        return {"collapsed": collapsed, "rows_removed": sum(
            group["n"] - 1 for group in groups
        )}

    # --- MemoryPort ---

    def seed_for(self, design: str, tech: str, stage: str) -> MemorySeed:
        with self._lock:
            best = self._conn.execute(
                """
                SELECT * FROM runs
                 WHERE design = ? AND tech = ? AND ok = 1 AND congested = 0
                   AND die_area_um2 > 0
                 ORDER BY die_area_um2 ASC LIMIT 1
                """,
                (design, tech),
            ).fetchone()
            if best is None:
                # Mock-backend rows carry no die area; rank those by score.
                best = self._conn.execute(
                    """
                    SELECT * FROM runs
                     WHERE design = ? AND tech = ? AND ok = 1 AND congested = 0
                     ORDER BY score DESC LIMIT 1
                    """,
                    (design, tech),
                ).fetchone()

            decision_rows = self._conn.execute(
                "SELECT knob, value FROM decisions WHERE design = ? AND tech = ?",
                (design, tech),
            ).fetchall()
            heuristic_rows = self._conn.execute(
                "SELECT text FROM heuristics WHERE design = ? AND tech = ? ORDER BY id",
                (design, tech),
            ).fetchall()
            correction_rows = self._conn.execute(
                """
                SELECT knob, final FROM corrections
                 WHERE design = ? AND tech = ? AND final IS NOT NULL
                 ORDER BY id
                """,
                (design, tech),
            ).fetchall()
            walls = self._walls(design, tech)

        best_config = None
        best_score = 0.0
        best_die_area = None
        best_key = None
        if best is not None:
            best_die_area = best["die_area_um2"] or None
            # The whole ConfigKey, floorplan knobs included: the best measured
            # ibex config is util 85 at AR 0.5, and seeding util alone would
            # land the search on a different (congested) floorplan.
            best_key = from_row(best)
            best_config = best_key.to_flow_config()
            best_score = best["score"] or 0.0

        return MemorySeed(
            best_config=best_config,
            best_score=best_score,
            best_die_area_um2=best_die_area,
            decisions={row["knob"]: row["value"] for row in decision_rows},
            heuristics=[row["text"] for row in heuristic_rows],
            # The wall depends on density, clock and the floorplan, so report
            # the one matching the configuration the search will start from.
            congestion_wall_util=(
                walls.get(best_key.wall_key) if best_key is not None
                else self._stock_view(walls).get((0.0, None))
            ),
            congestion_wall_by_density=self._stock_view(walls),
            congestion_walls=walls,
            # Later rows overwrite earlier ones: the most recent override wins.
            corrections={row["knob"]: row["final"] for row in correction_rows},
        )

    def _walls(self, design: str, tech: str) -> dict:
        """Lowest utilization known to congest or fail, per ConfigKey.wall_key
        (density, clock, aspect, margin, pins, macros).

        Caller must already hold self._lock (seed_for does); threading.Lock is
        not reentrant, so acquiring it here would deadlock.

        Rules, each learned from a wrong answer:
          * REAL runs only. A mock row congests above 0.30 while real nangate45
            congests above 1.0, so mock/estimated rows would wall a utilization
            that real silicon routes cleanly.
          * Per density, per clock and per floorplan: util 85 congests at AR 1
            but routes cleanly at AR 0.5, so a wall found on one floorplan says
            nothing about another.
          * A utilization with a clean real run under the same key can never be
            the wall, no matter what else is recorded there.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM runs
             WHERE design = ? AND tech = ? AND backend = 'orfs'
               AND density IS NOT NULL AND util IS NOT NULL
            """,
            (design, tech),
        ).fetchall()
        bad: dict = {}
        clean: dict = {}
        for row in rows:
            key = from_row(row)
            target = clean if (row["ok"] and not row["congested"]) else bad
            target.setdefault(key.wall_key, set()).add(key.util)
        walls = {}
        for wall_key, utils in bad.items():
            candidates = sorted(u for u in utils if u not in clean.get(wall_key, set()))
            if candidates:
                walls[wall_key] = candidates[0]
        return walls

    @staticmethod
    def _stock_view(walls: dict) -> dict:
        """(density, clock) -> wall for the STOCK floorplan only: the view
        reports and older callers use. Non-stock floorplans live only in the
        full-key dict."""
        stock = (1.0, None, STOCK_PINS, STOCK_MACROS)
        return {key[:2]: util for key, util in walls.items() if key[2:] == stock}

    def migrate_clocks(self, legacy_default: float = 2.5) -> dict:
        """Point rows at the clock they were really constrained with.

        Rows written before clock became a knob recorded FlowConfig's old 2.5ns
        placeholder, not the design's stock period. Now that walls and best
        configs are keyed by clock, leaving them at 2.5 would file every
        historical measurement under a clock that was never run.
        """
        from .eda_interface import ORFSAdapter

        adapter = ORFSAdapter()
        updated = []
        with self._lock:
            designs = [
                row["design"]
                for row in self._conn.execute(
                    "SELECT DISTINCT design FROM runs WHERE clock_ns = ?",
                    (legacy_default,),
                ).fetchall()
            ]
        for design in designs:
            stock = adapter.stock_clock_ns(design)
            if stock is None or abs(stock - legacy_default) < 1e-9:
                continue
            with self._lock:
                count = self._conn.execute(
                    "UPDATE runs SET clock_ns = ? WHERE design = ? AND clock_ns = ?",
                    (stock, design, legacy_default),
                ).rowcount
                self._conn.commit()
            updated.append(f"{design}: {count} row(s) {legacy_default:g} -> {stock:g}ns")
        return {"updated": updated, "count": len(updated)}

    def purge_synthetic_runs(self) -> dict:
        """Delete mock and estimated rows. Only real measurements belong in
        memory: everything else is reproducible, and mixing scales corrupts
        the wall."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT design, util, density, backend FROM runs WHERE backend != 'orfs'"
            ).fetchall()
            purged = [
                f"{row['design']} util={row['util']:g} density={row['density']:g} "
                f"({row['backend']})"
                for row in rows
            ]
            self._conn.execute("DELETE FROM runs WHERE backend != 'orfs'")
            self._conn.commit()
        return {"purged": purged, "count": len(purged)}

    def record_run(self, result: RunResult) -> None:
        config, metrics = result.config, result.metrics
        # RunResult carries no backend label, so it is inferred from the
        # congestion scale, which ORFSAdapter sets to 1.0 and the mock leaves at
        # 0.30. Die area cannot be used for this: a FAILED real run reports zero
        # area, and mislabelling those as mock would drop them from the wall —
        # the very runs that prove a utilization is unusable.
        backend = "orfs" if metrics.congestion_limit >= 1.0 else "mock"
        key = from_flow_config(config, None)
        floorplan = (key.margin, key.pin_strategy, key.macro_strategy)
        now = time.time()
        with self._lock:
            # A cache replay produces byte-identical metrics for a config that
            # was already measured. Recording it as a new row would inflate the
            # run count without adding information — five identical util-80 rows
            # once made memory look like five separate measurements. Only an
            # actually different result for the same config inserts a new row.
            existing = self._conn.execute(
                """
                SELECT id, replays FROM runs
                 WHERE design = ? AND tech = ? AND util = ? AND density = ?
                   AND aspect = ? AND clock_ns = ? AND backend = ? AND ok = ?
                   AND die_area_um2 = ? AND congestion = ?
                   AND margin IS ? AND COALESCE(pin_strategy, ?) = ?
                   AND COALESCE(macro_strategy, ?) = ?
                 LIMIT 1
                """,
                (
                    config.design, config.tech, config.core_utilization,
                    config.placement_density, config.aspect_ratio,
                    config.clock_period_ns, backend, int(result.ok),
                    metrics.die_area_um2, metrics.congestion,
                    floorplan[0], STOCK_PINS, floorplan[1], STOCK_MACROS, floorplan[2],
                ),
            ).fetchone()

            if existing is not None:
                self._conn.execute(
                    "UPDATE runs SET replays = replays + 1, ts = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                self._conn.commit()
                return

            self._conn.execute(
                """
                INSERT INTO runs (ts, first_seen, design, tech, util, density, aspect,
                                  clock_ns, backend, ok, congested, wns_ns, tns_ns,
                                  die_area_um2, congestion, power_mw, score, replays,
                                  margin, pin_strategy, macro_strategy, cp_delay_ns)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                """,
                (
                    now, now, config.design, config.tech, config.core_utilization,
                    config.placement_density, config.aspect_ratio, config.clock_period_ns,
                    backend, int(result.ok), int(metrics.congested), metrics.wns_ns,
                    metrics.tns_ns, metrics.die_area_um2, metrics.congestion,
                    metrics.power_mw, metrics.ppa_score(),
                    floorplan[0], floorplan[1], floorplan[2], metrics.cp_delay_ns or None,
                ),
            )
            self._conn.commit()

    def has_run(self, design: str, tech: str, util: float, density: float, ok: bool,
                clock_ns: Optional[float] = None) -> bool:
        """Whether an equivalent run is already stored. Used to make cache
        ingestion idempotent.

        Clock is part of the identity: without it, a run at a different clock
        period looks like a duplicate of the stock-clock run at the same
        utilization and gets silently dropped on ingest.
        """
        return self.has_key(
            ConfigKey(design=design, tech=tech, util=util, density=density, aspect=1.0, margin=None,
                      pin_strategy=STOCK_PINS, macro_strategy=STOCK_MACROS, clock_ns=clock_ns or 0.0),
            ok, any_floorplan=True,
        )

    def has_key(self, key: ConfigKey, ok: bool, any_floorplan: bool = False) -> bool:
        """Whether a run with this full ConfigKey (and outcome) is stored.
        Aspect and the floorplan knobs are part of the identity: util 85 at
        AR 0.5 is not a duplicate of util 85 at AR 1. any_floorplan keeps the
        old has_run contract (callers that never knew about floorplans)."""
        query = """
            SELECT 1 FROM runs
             WHERE design = ? AND tech = ? AND util = ? AND density = ? AND ok = ?
               AND clock_ns IS ?
        """
        args: list = [key.design, key.tech, key.util, key.density, int(ok), key.clock_ns]
        if not any_floorplan:
            query += """
               AND aspect = ? AND margin IS ? AND COALESCE(pin_strategy, ?) = ?
               AND COALESCE(macro_strategy, ?) = ?
            """
            args += [key.aspect, key.margin, STOCK_PINS, key.pin_strategy, STOCK_MACROS, key.macro_strategy]
        with self._lock:
            row = self._conn.execute(query + " LIMIT 1", args).fetchone()
        return row is not None

    def ingest_cached_runs(self, design: Optional[str] = None) -> dict:
        """Record real ORFS runs held only in the result cache.

        A failed run is a measurement: it is the strongest possible evidence
        that a configuration is unusable. The sweep scripts write straight to
        the cache without going through EDAAgent, so those results — including
        the failures that define the wall — never reached memory. This backfills
        them, skipping anything already stored.
        """
        from .eda_interface import ORFSAdapter
        from .types import FlowConfig, Metrics, RunResult

        adapter = ORFSAdapter()
        designs = [design] if design else sorted(
            {path.name.split("-")[0] for path in adapter.cache_dir.glob("*.json")}
        )

        added = 0
        skipped = 0
        failures = 0
        for name in designs:
            for entry in adapter.cached_entries(name):
                config_data = entry.get("config") or {}
                metrics_data = entry.get("metrics") or {}
                ok = bool(entry.get("ok", True))
                config = FlowConfig(
                    design=config_data.get("design", name),
                    tech=config_data.get("tech", "nangate45"),
                    clock_period_ns=config_data.get("clock_period_ns", 2.5),
                    core_utilization=config_data.get("core_utilization", 0.0),
                    placement_density=config_data.get("placement_density", 0.0),
                    aspect_ratio=config_data.get("aspect_ratio", 1.0),
                    floorplan=config_data.get("floorplan"),
                )
                if self.has_key(from_flow_config(config, None), ok):
                    skipped += 1
                    continue

                metrics = Metrics(
                    stage=metrics_data.get("stage", "unknown"),
                    wns_ns=metrics_data.get("wns_ns", 0.0),
                    tns_ns=metrics_data.get("tns_ns", 0.0),
                    congestion=metrics_data.get("congestion", 0.0),
                    cell_count=int(metrics_data.get("cell_count", 0) or 0),
                    area_um2=metrics_data.get("area_um2", 0.0),
                    power_mw=metrics_data.get("power_mw", 0.0),
                    die_area_um2=metrics_data.get("die_area_um2", 0.0),
                    core_area_um2=metrics_data.get("core_area_um2", 0.0),
                    drc_errors=int(metrics_data.get("drc_errors", 0) or 0),
                    cp_delay_ns=metrics_data.get("cp_delay_ns"),
                    congestion_limit=metrics_data.get("congestion_limit", 1.0),
                )
                self.record_run(
                    RunResult(
                        config=config,
                        reached_stage=entry.get("reached_stage", "unknown"),
                        metrics=metrics,
                        ok=ok,
                        failure=entry.get("failure"),
                    )
                )
                added += 1
                if not ok:
                    failures += 1

        return {"added": added, "skipped": skipped, "failed_runs_added": failures}

    def ingest_floorplan_runs(self, design: Optional[str] = None) -> dict:
        """Record the FloorplanAgent's FULL runs as runs under their ConfigKey.

        They are real full-flow measurements (util 85 at AR 0.5 is the best
        ibex die measured), but they lived only in the floorplan tables, so the
        warm seed and the walls never saw them. SCREEN runs stay out: they stop
        at global placement and are not full measurements. A key the result
        cache already holds (measured by both producers) is skipped here, so
        one physical run is one row.
        """
        from .measurements import FULL, Measurements
        from .types import Metrics, RunResult

        index = Measurements()
        designs = [design] if design else sorted(
            p.name for p in index.artifacts.iterdir() if p.is_dir() and any(p.glob("fp_full_*"))
        )
        added = skipped = 0
        for name in designs:
            for point in index._from_floorplan(name):
                if point.kind != FULL:
                    continue
                if self.has_key(point.key, point.ok):
                    skipped += 1
                    continue
                m = point.metrics
                metrics = Metrics(
                    stage=m.get("reached", "unknown"), wns_ns=m.get("wns_ns") or 0.0,
                    tns_ns=m.get("tns_ns") or 0.0, congestion=m.get("congestion") or 0.0,
                    cell_count=0, area_um2=0.0, power_mw=m.get("power_mw") or 0.0,
                    die_area_um2=m.get("die_area_um2") or 0.0, core_area_um2=m.get("core_area_um2") or 0.0,
                    drc_errors=int(m.get("drc_errors") or 0), cp_delay_ns=m.get("cp_delay_ns"),
                    timing_threshold_ns=m.get("timing_threshold_ns") or 0.0, congestion_limit=1.0,
                )
                self.record_run(RunResult(config=point.key.to_flow_config(), reached_stage=metrics.stage,
                                          metrics=metrics, ok=point.ok))
                added += 1
        return {"added": added, "skipped": skipped}

    # --- learning hooks beyond MemoryPort ---

    def record_session(self, best: RunResult, history: list[Proposal]) -> None:
        """Settle the knobs the search converged on. Individual runs are already
        stored by record_run, so this only records the conclusion."""
        if not best.ok or best.metrics.congested:
            return
        with self._lock:
            for knob, value in (
                ("core_utilization", best.config.core_utilization),
                ("placement_density", best.config.placement_density),
            ):
                self._conn.execute(
                    """
                    INSERT INTO decisions (design, tech, knob, value, ts)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(design, tech, knob) DO UPDATE SET value=excluded.value, ts=excluded.ts
                    """,
                    (best.config.design, best.config.tech, knob, value, time.time()),
                )
            self._conn.commit()

    def record_correction(
        self, design: str, tech: str, knob: str,
        proposed: Any, final: Any, rationale: str = "",
    ) -> None:
        """A human-in-the-loop edit: the engineer overrode a proposed knob.
        Stored as a correction and distilled into a human-readable heuristic."""
        text = (
            f"On {design}, engineer set {knob} to {final} instead of proposed {proposed}"
        )
        if rationale:
            text = f"{text} ({rationale})"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO corrections (ts, design, tech, knob, proposed, final, rationale)
                VALUES (?,?,?,?,?,?,?)
                """,
                (time.time(), design, tech, knob, _as_number(proposed), _as_number(final), rationale),
            )
            existing = self._conn.execute(
                "SELECT 1 FROM heuristics WHERE design = ? AND tech = ? AND text = ?",
                (design, tech, text),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO heuristics (ts, design, tech, text) VALUES (?,?,?,?)",
                    (time.time(), design, tech, text),
                )
            self._conn.commit()

    def merge_knowledge_from(self, other_db: str | Path) -> dict:
        """Copy corrections and heuristics that exist only in `other_db`.

        Runs are measurements and come from the shared result cache, but a
        correction is a human judgement recorded in one database only — it
        cannot be regenerated, so consolidating must not drop it.
        """
        other_db = str(other_db)
        if not Path(other_db).exists():
            raise FileNotFoundError(f"no database at {other_db}")

        source = sqlite3.connect(f"file:{other_db}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            corrections = source.execute("SELECT * FROM corrections ORDER BY id").fetchall()
            heuristics = source.execute("SELECT * FROM heuristics ORDER BY id").fetchall()
        finally:
            source.close()

        copied_corrections = []
        copied_heuristics = []
        with self._lock:
            for row in corrections:
                existing = self._conn.execute(
                    """
                    SELECT 1 FROM corrections
                     WHERE design = ? AND tech = ? AND knob = ?
                       AND proposed IS ? AND final IS ?
                     LIMIT 1
                    """,
                    (row["design"], row["tech"], row["knob"], row["proposed"], row["final"]),
                ).fetchone()
                if existing:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO corrections (ts, design, tech, knob, proposed, final, rationale)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (row["ts"], row["design"], row["tech"], row["knob"],
                     row["proposed"], row["final"], row["rationale"]),
                )
                copied_corrections.append(
                    f"{row['design']}: {row['knob']} {row['proposed']} -> {row['final']}"
                )

            for row in heuristics:
                existing = self._conn.execute(
                    "SELECT 1 FROM heuristics WHERE design = ? AND tech = ? AND text = ? LIMIT 1",
                    (row["design"], row["tech"], row["text"]),
                ).fetchone()
                if existing:
                    continue
                self._conn.execute(
                    "INSERT INTO heuristics (ts, design, tech, text) VALUES (?,?,?,?)",
                    (row["ts"], row["design"], row["tech"], row["text"]),
                )
                copied_heuristics.append(f"{row['design']}: {row['text']}")
            self._conn.commit()

        return {
            "corrections_copied": copied_corrections,
            "heuristics_copied": copied_heuristics,
            "corrections_seen": len(corrections),
            "heuristics_seen": len(heuristics),
        }

    def reset(self, design: Optional[str] = None) -> None:
        with self._lock:
            for table in ("runs", "decisions", "corrections", "heuristics"):
                if design is None:
                    self._conn.execute(f"DELETE FROM {table}")
                else:
                    self._conn.execute(f"DELETE FROM {table} WHERE design = ?", (design,))
            self._conn.commit()

    # --- reporting ---

    def describe(self, design: Optional[str] = None) -> dict:
        """Shape the web UI's /api/memory and memory_cli both render."""
        where, params = "", ()
        if design is not None:
            where, params = " WHERE design = ?", (design,)

        with self._lock:
            totals = self._conn.execute(
                f"SELECT COUNT(*) AS runs, SUM(ok = 0) AS failures,"
                f" SUM(congested = 1) AS congested, SUM(replays) AS replays"
                f" FROM runs{where}",
                params,
            ).fetchone()
            pairs = self._conn.execute(
                f"SELECT DISTINCT design, tech FROM runs{where}", params
            ).fetchall()

        designs = {}
        for row in pairs:
            seed = self.seed_for(row["design"], row["tech"], "route")
            with self._lock:
                counts = self._conn.execute(
                    "SELECT COUNT(*) AS runs, SUM(ok = 0) AS failures,"
                    " SUM(congested = 1) AS congested, SUM(replays) AS replays"
                    " FROM runs WHERE design = ? AND tech = ?",
                    (row["design"], row["tech"]),
                ).fetchone()
            designs[f"{row['design']}::{row['tech']}"] = {
                "has_best_config": seed.best_config is not None,
                "best_config": _config_dict(seed.best_config),
                "best_score": seed.best_score,
                "decisions": seed.decisions,
                "heuristics": seed.heuristics,
                "congestion_wall_util": seed.congestion_wall_util,
                "congestion_wall_by_density": seed.congestion_wall_by_density,
                # "runs" here counts distinct measurements, not tool calls:
                # replays of a cached result are folded into replay_count.
                "unique_configs": counts["runs"] or 0,
                "run_count": counts["runs"] or 0,
                "replay_count": counts["replays"] or 0,
                "failure_count": counts["failures"] or 0,
                "congested_count": counts["congested"] or 0,
                "warm": not seed.is_cold,
            }

        return {
            "db_path": self.db_path,
            "total_unique_configs": totals["runs"] or 0,
            "total_runs": totals["runs"] or 0,
            "total_replays": totals["replays"] or 0,
            "total_failures": totals["failures"] or 0,
            "total_congested": totals["congested"] or 0,
            "designs": designs,
        }

    def corrections(self, design: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM corrections"
        params: tuple = ()
        if design is not None:
            query += " WHERE design = ?"
            params = (design,)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _sidecars(path: str | Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm")]


def is_in_use(path: str | Path) -> bool:
    """Whether another process currently holds a write lock on the database.

    A lingering -wal file is NOT proof of use (an unclean exit leaves one
    behind), so this actually tries to take the lock instead of guessing.
    """
    if not Path(path).exists():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            return False
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return True


def backup_database(src: str | Path, dest: str | Path) -> dict:
    """Consistent online copy via the sqlite3 backup API.

    Used instead of `cp` because these databases run in WAL mode: recent
    commits can live entirely in the -wal sidecar, so a file copy of the .db
    alone can silently omit them.
    """
    src, dest = str(src), str(dest)
    if not Path(src).exists():
        raise FileNotFoundError(f"no database at {src}")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
        counts = {
            table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "decisions", "corrections", "heuristics")
        }
    finally:
        target.close()
        source.close()
    return {"src": src, "dest": dest, "bytes": Path(dest).stat().st_size, **counts}


def restore_database(src: str | Path, dest: str | Path, force: bool = False) -> dict:
    """Overwrite `dest` with `src`. Refuses while `dest` is held by a backend."""
    src, dest = str(src), str(dest)
    if not Path(src).exists():
        raise FileNotFoundError(f"no database at {src}")

    probe = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        probe.execute("SELECT 1 FROM runs LIMIT 1")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"{src} is not an adaptive_iicpilot memory database: {exc}")
    finally:
        probe.close()

    if is_in_use(dest) and not force:
        raise RuntimeError(
            f"{dest} is currently open by another process (the chat backend or web "
            f"server). Stop it and retry, or pass --force to overwrite anyway."
        )

    # Keep the pre-restore state; overwriting learned memory is not reversible.
    safety = None
    if Path(dest).exists():
        safety = f"{dest}.pre_restore"
        backup_database(dest, safety)

    # Any sidecar left now belongs to the version being replaced.
    removed = []
    for sidecar in _sidecars(dest):
        if sidecar.exists():
            sidecar.unlink()
            removed.append(sidecar.name)

    Path(dest).unlink(missing_ok=True)
    result = backup_database(src, dest)
    result["safety_copy"] = safety
    result["stale_sidecars_removed"] = removed
    return result


def _as_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _config_dict(config: Optional[FlowConfig]) -> Optional[dict]:
    if config is None:
        return None
    return {
        "design": config.design,
        "tech": config.tech,
        "clock_period_ns": config.clock_period_ns,
        "core_utilization": config.core_utilization,
        "placement_density": config.placement_density,
        "aspect_ratio": config.aspect_ratio,
        "floorplan": config.floorplan,
    }


def make_memory(kind: Optional[str] = None):
    """Memory selection seam. SQLite is the default so learning persists;
    tests ask for inmemory (or a ":memory:" SQLite db)."""
    name = (kind or os.environ.get("ADAPTIVE_IICPILOT_MEMORY", "sqlite")).strip().lower()
    if name == "inmemory":
        from .web.compat import InMemoryMemory

        return InMemoryMemory()
    return SQLiteMemory()
