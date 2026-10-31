"""Inspect, reset, back up and restore persistent memory.

    python -m adaptive_iicpilot.memory_cli show [--design ibex]
    python -m adaptive_iicpilot.memory_cli reset [--design ibex]
    python -m adaptive_iicpilot.memory_cli import-cache [--design ibex]
    python -m adaptive_iicpilot.memory_cli import-floorplan [--design ibex]
    python -m adaptive_iicpilot.memory_cli backup  <dest.db>
    python -m adaptive_iicpilot.memory_cli restore <src.db> [--force]
    python -m adaptive_iicpilot.memory_cli merge   <other.db>

Every command prints the database path it resolved, because the same project
run under a Windows interpreter and under WSL resolves two different files.
"""
from __future__ import annotations

import argparse

from .sqlite_memory import SQLiteMemory, backup_database, restore_database


def _show(memory: SQLiteMemory, design: str | None) -> None:
    info = memory.describe(design)
    print(f"db: {info['db_path']}")
    print(
        f"unique measured configs: {info['total_unique_configs']}  "
        f"(replays served from cache: {info['total_replays']})"
    )
    print(
        f"failures: {info['total_failures']}  congested: {info['total_congested']}"
    )

    if not info["designs"]:
        print("\n(no memory yet - cold)")
        return

    for key, entry in info["designs"].items():
        print(f"\n=== {key} ===")
        print(f"  state            : {'WARM' if entry['warm'] else 'COLD'}")
        print(
            f"  unique configs   : {entry['unique_configs']} "
            f"({entry['congested_count']} congested, {entry['failure_count']} failed)"
        )
        print(f"  cache replays    : {entry['replay_count']}")
        config = entry["best_config"]
        if config:
            density = config["placement_density"] or "auto"
            floorplan = config.get("floorplan") or {}
            extra = "".join(f" {k}={v}" for k, v in floorplan.items() if v is not None)
            print(
                f"  best clean config: util={config['core_utilization']:.0f}% "
                f"density={density} aspect={config['aspect_ratio']}{extra}"
            )
            print(f"  best score       : {entry['best_score']:.4e}")
        else:
            print("  best clean config: (none yet)")
        walls = entry.get("congestion_wall_by_density") or {}
        if walls:
            for key, wall in sorted(walls.items(), key=lambda kv: (str(kv[0]))):
                density, clock = key if isinstance(key, tuple) else (key, None)
                dlabel = "auto" if not density else f"{density:g}"
                clabel = "stock" if not clock else f"{clock:g}ns"
                print(f"  wall (density {dlabel}, clock {clabel}): "
                      f"util >= {wall:.0f}% congests or fails")
        if entry["congestion_wall_util"] is not None:
            print(
                f"  wall             : util >= {entry['congestion_wall_util']:.0f}% "
                f"congests or fails"
            )
        print(f"  decisions        : {entry['decisions'] or '(none)'}")
        if entry["heuristics"]:
            print("  heuristics       :")
            for text in entry["heuristics"]:
                print(f"    - {text}")
        else:
            print("  heuristics       : (none)")

    corrections = memory.corrections(design)
    if corrections:
        print(f"\n=== corrections ({len(corrections)}) ===")
        for row in corrections:
            print(
                f"  {row['design']}: {row['knob']} proposed={row['proposed']} "
                f"-> final={row['final']}"
                + (f"  [{row['rationale']}]" if row["rationale"] else "")
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or reset persistent memory.")
    parser.add_argument(
        "command",
        choices=["show", "reset", "import-cache", "import-floorplan", "backup", "restore", "merge",
                 "purge-synthetic", "dedupe", "migrate-clocks"],
    )
    parser.add_argument("path", nargs="?", default=None,
                        help="destination for backup, source for restore/merge")
    parser.add_argument("--design", default=None, help="limit to one design")
    parser.add_argument("--force", action="store_true",
                        help="restore even if the database is open elsewhere")
    args = parser.parse_args()

    if args.command == "backup":
        if not args.path:
            parser.error("backup needs a destination path")
        memory = SQLiteMemory()
        info = backup_database(memory.db_path, args.path)
        print(f"backed up {info['src']}\n        -> {info['dest']} ({info['bytes']} bytes)")
        print(f"  runs={info['runs']} decisions={info['decisions']} "
              f"corrections={info['corrections']} heuristics={info['heuristics']}")
        return

    if args.command == "restore":
        if not args.path:
            parser.error("restore needs a source path")
        target = SQLiteMemory().db_path
        try:
            info = restore_database(args.path, target, force=args.force)
        except RuntimeError as exc:
            raise SystemExit(f"refused: {exc}")
        print(f"restored {info['src']}\n      -> {info['dest']} ({info['bytes']} bytes)")
        if info["safety_copy"]:
            print(f"  previous database kept at {info['safety_copy']}")
        if info["stale_sidecars_removed"]:
            print(f"  cleared stale files: {', '.join(info['stale_sidecars_removed'])}")
        print(f"  runs={info['runs']} corrections={info['corrections']} "
              f"heuristics={info['heuristics']}")
        return

    if args.command == "merge":
        if not args.path:
            parser.error("merge needs a source database path")
        memory = SQLiteMemory()
        print(f"db: {memory.db_path}")
        result = memory.merge_knowledge_from(args.path)
        print(f"source: {args.path}")
        print(f"  corrections: {len(result['corrections_copied'])} copied "
              f"of {result['corrections_seen']} seen")
        for item in result["corrections_copied"]:
            print(f"    + {item}")
        print(f"  heuristics : {len(result['heuristics_copied'])} copied "
              f"of {result['heuristics_seen']} seen")
        for item in result["heuristics_copied"]:
            print(f"    + {item}")
        return

    memory = SQLiteMemory()
    if args.command == "show":
        _show(memory, args.design)
        return

    if args.command == "migrate-clocks":
        memory = SQLiteMemory()
        print(f"db: {memory.db_path}")
        result = memory.migrate_clocks()
        print(f"repointed {result['count']} design(s) to their stock clock:")
        for item in result["updated"]:
            print(f"  - {item}")
        _show(memory, args.design)
        return

    if args.command == "dedupe":
        memory = SQLiteMemory()
        print(f"db: {memory.db_path}")
        result = memory.deduplicate_runs()
        print(f"collapsed {result['rows_removed']} duplicate row(s):")
        for item in result["collapsed"]:
            print(f"  - {item}")
        _show(memory, args.design)
        return

    if args.command == "purge-synthetic":
        memory = SQLiteMemory()
        print(f"db: {memory.db_path}")
        result = memory.purge_synthetic_runs()
        print(f"purged {result['count']} non-real run(s):")
        for item in result["purged"]:
            print(f"  - {item}")
        _show(memory, args.design)
        return

    if args.command == "import-floorplan":
        stats = memory.ingest_floorplan_runs(args.design)
        print(f"db: {memory.db_path}")
        print(f"ingested {stats['added']} FloorplanAgent FULL run(s) under their ConfigKey, "
              f"skipped {stats['skipped']} already stored.")
        _show(memory, args.design)
        return

    if args.command == "import-cache":
        stats = memory.ingest_cached_runs(args.design)
        print(
            f"ingested {stats['added']} cached run(s) "
            f"({stats['failed_runs_added']} failed), skipped {stats['skipped']} already stored."
        )
        _show(memory, args.design)
        return

    memory.reset(args.design)
    scope = args.design or "all designs"
    print(f"memory reset ({scope}); next run will be cold.")


if __name__ == "__main__":
    main()
