"""Worker lifecycle checks with only temporary databases and local barriers."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.runtime import WorkerSupervisor


class WorkerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'runtime.sqlite')
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE events(value TEXT)')
        self.errors = []
        self.opened = []
        def connect():
            conn = sqlite3.connect(self.path)
            self.opened.append(conn)
            return conn
        self.runtime = WorkerSupervisor(connect, benchmark_probe=lambda conn: False,
                                        log_error=lambda: self.errors.append('error'))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.runtime.close)

    def test_uncommitted_step_rolls_back_before_next_iteration_and_closes(self):
        seen = []
        done = threading.Event()
        def step(conn):
            if not seen:
                seen.append('first')
                conn.execute("INSERT INTO events VALUES('unfinished')")
            else:
                seen.append(conn.execute('SELECT count(*) FROM events').fetchone()[0])
                done.set()
                self.runtime.stop.set()
            return True
        self.runtime.start('rollback', step, 0, 0)
        self.assertTrue(done.wait(2))
        self.assertEqual(self.runtime.close(), [])
        self.assertEqual(seen, ['first', 0])
        self.assertEqual(len(self.opened), 1)
        self.assertFalse(self.runtime.busy())
        # Check on the opening thread via sqlite's own closed marker would be
        # masked by thread ownership. A fresh reader verifies released locks.
        with sqlite3.connect(self.path, timeout=.1) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0], 0)

    def test_error_releases_activity_and_retry_has_clean_transaction(self):
        attempted = []
        done = threading.Event()
        def step(conn):
            attempted.append(1)
            if len(attempted) == 1:
                conn.execute("INSERT INTO events VALUES('failed')")
                raise ValueError('temporary test failure')
            self.assertFalse(conn.in_transaction)
            conn.execute("INSERT INTO events VALUES('committed')")
            conn.commit()
            self.runtime.stop.set()
            done.set()
        self.runtime.start('retry', step, 0, 0)
        self.assertTrue(done.wait(2))
        self.assertEqual(self.runtime.close(), [])
        self.assertEqual(self.errors, ['error'])
        self.assertFalse(self.runtime.busy())
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT value FROM events').fetchall(), [('committed',)])

    def test_stop_wakes_long_idle_wait_and_refuses_new_workers(self):
        done = threading.Event()
        self.runtime.start('idle', lambda conn: done.set() and False, 900, 900)
        self.assertTrue(done.wait(2))
        began = time.monotonic()
        self.assertEqual(self.runtime.close(timeout=.5), [])
        self.assertLess(time.monotonic() - began, .5)
        with self.assertRaises(RuntimeError):
            self.runtime.start('late', lambda conn: False, 1, 1)

    def test_close_reports_blocked_worker_within_budget(self):
        entered, release = threading.Event(), threading.Event()
        def step(conn):
            entered.set()
            release.wait(2)
        self.runtime.start('blocked', step, 0, 0)
        self.assertTrue(entered.wait(2))
        try:
            began = time.monotonic()
            self.assertEqual(self.runtime.close(timeout=.02), ['fortunate-blocked'])
            self.assertLess(time.monotonic() - began, .5)
        finally:
            release.set()
        self.assertEqual(self.runtime.close(timeout=1), [])


if __name__ == '__main__':
    unittest.main()
