"""Backup, restore, merge, and the wrong-platform warning.

These exist because two divergent memory.db files appeared in practice: a
Windows interpreter and the WSL one each resolved their own Path.home().
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adaptive_iicpilot.sqlite_memory import (
    SQLiteMemory,
    backup_database,
    is_in_use,
    restore_database,
)
from adaptive_iicpilot.types import FlowConfig, Metrics, RunResult


def _result(design="ibex", util=80.0, ok=True, congestion=0.97, die=35944.4):
    config = FlowConfig(design=design, core_utilization=util, placement_density=0.0)
    metrics = Metrics(
        stage="finish", wns_ns=-0.007, tns_ns=-0.25, congestion=congestion,
        cell_count=22894, area_um2=29287.9, power_mw=21.64,
        die_area_um2=die if ok else 0.0, core_area_um2=die * 0.98,
        congestion_limit=1.0,
    )
    return RunResult(config=config, reached_stage="finish", metrics=metrics, ok=ok)


class TestBackupRestore(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows a sqlite handle that outlives the
        # test would otherwise turn teardown into a spurious failure.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _memory(self, path):
        """Open a memory database that is guaranteed to be closed first."""
        memory = SQLiteMemory(path)
        self.addCleanup(memory.close)
        return memory

    @staticmethod
    def _describe(path):
        memory = SQLiteMemory(path)
        try:
            return memory.describe()
        finally:
            memory.close()

    def _seed(self, path):
        memory = SQLiteMemory(path)
        memory.record_run(_result())
        memory.record_correction("ibex", "nangate45", "core_utilization", 85, 75)
        memory.close()

    def test_backup_captures_wal_resident_commits(self):
        """The reason `cp` is not good enough: in WAL mode recent commits can
        live in the sidecar, so a copy of the .db alone can miss them."""
        source = self.tmp / "memory.db"
        live = self._memory(source)
        live.record_run(_result())
        # Deliberately leave the connection open, so the WAL is not checkpointed.
        info = backup_database(source, self.tmp / "backup.db")
        live.close()

        self.assertEqual(info["runs"], 1)
        self.assertEqual(self._describe(self.tmp / "backup.db")["total_runs"], 1)

    def test_backup_copies_every_table(self):
        source = self.tmp / "memory.db"
        self._seed(source)
        info = backup_database(source, self.tmp / "backup.db")

        self.assertEqual(info["runs"], 1)
        self.assertEqual(info["corrections"], 1)
        self.assertEqual(info["heuristics"], 1)

    def test_restore_replaces_content_and_keeps_a_safety_copy(self):
        good = self.tmp / "good.db"
        self._seed(good)
        target = self.tmp / "target.db"
        wrong = SQLiteMemory(target)
        wrong.record_run(_result(design="gcd", util=50.0, die=1399.1))
        wrong.close()

        info = restore_database(good, target)

        restored = self._describe(target)
        self.assertIn("ibex::nangate45", restored["designs"])
        self.assertNotIn("gcd::nangate45", restored["designs"])
        # The overwritten database is still recoverable.
        self.assertTrue(Path(info["safety_copy"]).exists())
        self.assertIn("gcd::nangate45", self._describe(info["safety_copy"])["designs"])

    def test_restore_clears_stale_sidecars(self):
        good = self.tmp / "good.db"
        self._seed(good)
        target = self.tmp / "target.db"
        self._seed(target)
        # An unclean exit leaves these behind.
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").write_bytes(b"stale")

        info = restore_database(good, target)

        self.assertCountEqual(
            info["stale_sidecars_removed"], ["target.db-wal", "target.db-shm"]
        )
        for suffix in ("-wal", "-shm"):
            leftover = Path(f"{target}{suffix}")
            if leftover.exists():
                self.assertNotEqual(leftover.read_bytes(), b"stale")

    def test_restore_refuses_a_non_memory_database(self):
        junk = self.tmp / "junk.db"
        conn = sqlite3.connect(junk)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()

        with self.assertRaises(ValueError):
            restore_database(junk, self.tmp / "target.db")

    def test_restore_refuses_while_the_database_is_held(self):
        good = self.tmp / "good.db"
        self._seed(good)
        target = self.tmp / "target.db"
        self._seed(target)

        holder = sqlite3.connect(target)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            self.assertTrue(is_in_use(target))
            with self.assertRaises(RuntimeError) as caught:
                restore_database(good, target)
            self.assertIn("--force", str(caught.exception))
        finally:
            holder.rollback()
            holder.close()

    def test_idle_database_is_not_reported_in_use(self):
        target = self.tmp / "memory.db"
        self._seed(target)
        self.assertFalse(is_in_use(target))


class TestMergeKnowledge(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows a sqlite handle that outlives the
        # test would otherwise turn teardown into a spurious failure.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _memory(self, path):
        """Open a memory database that is guaranteed to be closed first."""
        memory = SQLiteMemory(path)
        self.addCleanup(memory.close)
        return memory

    def test_copies_only_missing_corrections_and_heuristics(self):
        other = self.tmp / "other.db"
        source = self._memory(other)
        source.record_correction("ibex", "nangate45", "core_utilization", 85, 75)
        source.record_correction("gcd", "nangate45", "placement_density", 0.9, 0.7)
        source.close()

        target = self._memory(self.tmp / "target.db")
        # Already knows about the ibex one.
        target.record_correction("ibex", "nangate45", "core_utilization", 85, 75)

        result = target.merge_knowledge_from(other)

        self.assertEqual(len(result["corrections_copied"]), 1)
        self.assertIn("gcd", result["corrections_copied"][0])
        self.assertEqual(result["corrections_seen"], 2)
        self.assertEqual(len(target.corrections()), 2)

    def test_merge_is_idempotent(self):
        other = self.tmp / "other.db"
        source = self._memory(other)
        source.record_correction("ibex", "nangate45", "core_utilization", 85, 75)
        source.close()

        target = self._memory(self.tmp / "target.db")
        target.merge_knowledge_from(other)
        second = target.merge_knowledge_from(other)

        self.assertEqual(second["corrections_copied"], [])
        self.assertEqual(second["heuristics_copied"], [])


class TestPlatformWarning(unittest.TestCase):
    def test_warns_only_when_the_path_was_defaulted_on_windows(self):
        from adaptive_iicpilot.sqlite_memory import _warn_if_wrong_platform

        with mock.patch("adaptive_iicpilot.sqlite_memory.os.name", "nt"):
            with mock.patch("sys.stderr") as err:
                _warn_if_wrong_platform("C:/x/memory.db", was_defaulted=True)
                self.assertTrue(err.write.called)
            with mock.patch("sys.stderr") as err:
                _warn_if_wrong_platform("C:/x/memory.db", was_defaulted=False)
                self.assertFalse(err.write.called)

    def test_silent_on_posix(self):
        from adaptive_iicpilot.sqlite_memory import _warn_if_wrong_platform

        with mock.patch("adaptive_iicpilot.sqlite_memory.os.name", "posix"):
            with mock.patch("sys.stderr") as err:
                _warn_if_wrong_platform("/home/u/.adaptive_iicpilot/memory.db", True)
                self.assertFalse(err.write.called)

    def test_explicit_path_does_not_warn(self):
        """Tests and deliberate env vars must not emit the warning."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.stderr") as err:
                SQLiteMemory(Path(tmp) / "memory.db").close()
                self.assertFalse(err.write.called)


if __name__ == "__main__":
    unittest.main()
