"""Offline regression checks for operational safety; run: python3 -m unittest ops/test_safety.py."""

import os
import pathlib
from datetime import date, timedelta
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest


OPS = pathlib.Path(__file__).resolve().parent


class SafetyTests(unittest.TestCase):
    @staticmethod
    def backup_env(root):
        home = root / "home"
        home.mkdir()
        return dict(os.environ, HOME=str(home), PYTHON=sys.executable)

    @staticmethod
    def create_workspace(con):
        con.executescript("""
            CREATE TABLE people(id INTEGER);
            CREATE TABLE seeds(handle TEXT);
            CREATE TABLE lists(seed TEXT);
            CREATE TABLE edges(seed TEXT);
            CREATE TABLE accounts(lane_id TEXT);
            CREATE TABLE settings(key TEXT);
            INSERT INTO people VALUES(1);
        """)

    def test_backups_do_not_overwrite_within_one_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / "leads.sqlite"
            con = sqlite3.connect(db)
            self.create_workspace(con)
            con.commit()
            con.close()
            env = self.backup_env(root)
            backup_dir = root / "backups"
            backup_dir.mkdir()
            stale_part = backup_dir / ".leads-backup.XXXXXXXX.partial"
            stale_part.write_bytes(b"stale partial backup")
            for _ in range(2):
                subprocess.run(
                    ["bash", str(OPS / "backup.sh"), "--db", str(db), "--dest", str(backup_dir), "--no-rotate"],
                    env=env, check=True, capture_output=True, text=True,
                )
            self.assertEqual(stale_part.read_bytes(), b"stale partial backup")
            backups = sorted(backup_dir.glob("leads-*.sqlite"))
            self.assertEqual(len(backups), 2)
            self.assertEqual((root / "backups").stat().st_mode & 0o777, 0o700)
            for backup in backups:
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                with sqlite3.connect(backup) as con:
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(con.execute("SELECT count(*) FROM people").fetchone()[0], 1)

    def test_wal_backup_includes_uncheckpointed_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / "leads.sqlite"
            dest = root / "backups"
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            self.create_workspace(con)
            con.commit()
            self.assertTrue(pathlib.Path(str(db) + "-wal").exists())
            result = subprocess.run(
                ["bash", str(OPS / "backup.sh"), "--db", str(db), "--dest", str(dest), "--no-rotate"],
                env=self.backup_env(root), capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            backup = next(dest.glob("leads-*.sqlite"))
            with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as copy:
                self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(copy.execute("SELECT count(*) FROM people").fetchone()[0], 1)
            self.assertEqual([p.name for p in dest.iterdir()], [backup.name])
            con.close()

    def test_wrong_database_is_not_published_or_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / "wrong.sqlite"
            dest = root / "backups"
            dest.mkdir()
            old = dest / "leads-20200101-000000-1-1.sqlite"
            old.write_bytes(b"existing backup")
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE unrelated(x INTEGER)")
            result = subprocess.run(
                ["bash", str(OPS / "backup.sh"), "--db", str(db), "--dest", str(dest),
                 "--keep", "1", "--no-rotate"],
                env=self.backup_env(root), capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a Fortunate Leads database", result.stderr)
            self.assertEqual([p.name for p in dest.iterdir()], [old.name])
            self.assertEqual(old.read_bytes(), b"existing backup")

    def test_retention_preserves_unrelated_files_and_stale_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / 'leads.sqlite'
            with sqlite3.connect(db) as con:
                self.create_workspace(con)
            dest = root / 'backups'
            dest.mkdir()
            old = dest / 'leads-20200101-000000-1-1.sqlite'
            old.write_bytes(b'old backup')
            active = dest / 'leads-20200101-000000-3-1.sqlite'
            active.write_bytes(b'opened backup')
            pathlib.Path(str(active) + '-wal').write_bytes(b'journal')
            preserved = [dest / 'leads-manual.sqlite', dest / '.leads-backup.stale']
            for path in preserved:
                path.write_bytes(b'keep')
            link = dest / 'leads-20200101-000000-2-1.sqlite'
            link.symlink_to(db)
            run = subprocess.run(['bash', str(OPS / 'backup.sh'), '--db', str(db),
                                  '--dest', str(dest), '--keep', '1', '--no-rotate'],
                                 env=self.backup_env(root), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(link.is_symlink())
            for path in preserved:
                self.assertEqual(path.read_bytes(), b'keep')

    def test_many_same_day_backups_keep_older_daily_recovery_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / 'leads.sqlite'
            with sqlite3.connect(db) as con:
                self.create_workspace(con)
            dest = root / 'backups'
            dest.mkdir()
            today = date.today()
            prior = []
            for days_ago in (2, 1):
                day = (today - timedelta(days=days_ago)).strftime('%Y%m%d')
                path = dest / f'leads-{day}-030000-1-1.sqlite'
                path.write_bytes(b'old snapshot')
                prior.append(path)
            same_day = []
            for hour in range(5):
                path = dest / f'leads-{today:%Y%m%d}-{hour:02d}0000-1-1.sqlite'
                path.write_bytes(b'manual snapshot')
                same_day.append(path)
            run = subprocess.run(['bash', str(OPS / 'backup.sh'), '--db', str(db),
                                  '--dest', str(dest), '--keep', '2', '--no-rotate'],
                                 env=self.backup_env(root), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse(prior[0].exists())
            self.assertTrue(prior[1].exists())
            self.assertFalse(same_day[0].exists())
            self.assertTrue(same_day[-1].exists())
            self.assertEqual(len(list(dest.glob('leads-*.sqlite'))), 3)

    def test_zero_retention_is_rejected_without_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / 'leads.sqlite'
            with sqlite3.connect(db) as con:
                self.create_workspace(con)
            dest = root / 'backups'
            dest.mkdir()
            old = dest / 'leads-20200101-000000-1-1.sqlite'
            old.write_bytes(b'keep')
            run = subprocess.run(['bash', str(OPS / 'backup.sh'), '--db', str(db),
                                  '--dest', str(dest), '--keep', '00', '--no-rotate'],
                                 env=self.backup_env(root), capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(list(dest.iterdir()), [old])
            self.assertEqual(old.read_bytes(), b'keep')

    def test_refuses_to_stop_unowned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.Popen(["sleep", "20"], cwd=directory)
            try:
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; stop_pids "$2"', "test", str(OPS / "lib.sh"), str(proc.pid)],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing to stop", result.stderr)
                self.assertIsNone(proc.poll())
            finally:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
