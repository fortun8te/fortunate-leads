"""A failed background step must not leak uncommitted writes into the next step."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
import db  # noqa: E402


class StopAfterTwoSteps:
    waits = 0

    def is_set(self):
        return self.waits >= 2

    def wait(self, _):
        self.waits += 1


class WorkerTransactionTest(unittest.TestCase):
    def test_failed_step_is_rolled_back_before_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'worker.sqlite')
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE items (value TEXT)')
            conn.commit()
            conn.close()
            calls, observed = [0], []

            def step(db_conn):
                calls[0] += 1
                if calls[0] == 1:
                    db_conn.execute('INSERT INTO items VALUES (?)', ('uncommitted',))
                    raise RuntimeError('step failed after write')
                observed.append(db_conn.execute('SELECT count(*) FROM items').fetchone()[0])
                return False

            with patch.dict(server.CFG, {'db': path}), patch.object(server.traceback, 'print_exc'):
                server.worker(StopAfterTwoSteps(), step, 0, 0)
            self.assertEqual(observed, [0])

    def test_rule_batch_releases_writer_for_extension_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'leads.sqlite')
            setup = db.init(path)
            try:
                setup.executemany('INSERT INTO people(handle,first_seen,updated_at) VALUES(?,?,?)',
                                  [(f'person{i}', db.now(), db.now()) for i in range(100)])
                setup.commit()
            finally:
                setup.close()

            started = threading.Event()
            errors = []
            original = server.requalify

            def slow_requalify(*args):
                started.set()
                time.sleep(0.02)
                return original(*args)

            def qualify():
                conn = db.connect(path)
                try:
                    server.qualify_batch(conn)
                except Exception as error:
                    errors.append(error)
                finally:
                    conn.close()

            with patch.object(server, 'requalify', side_effect=slow_requalify):
                thread = threading.Thread(target=qualify)
                thread.start()
                try:
                    self.assertTrue(started.wait(2), 'qualifier never reached a write transaction')
                    beat = db.connect(path)
                    beat.execute('PRAGMA busy_timeout=1300')
                    try:
                        with patch.object(server, 'ext_state', return_value={}):
                            server.ext_heartbeat(beat, {}, {'lane_id': 'lane-a'})
                    finally:
                        beat.close()
                finally:
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            verify = db.connect(path)
            try:
                self.assertIsNotNone(verify.execute("SELECT last_seen FROM accounts WHERE lane_id='lane-a'").fetchone())
                while server.qualify_batch(verify):
                    pass
                self.assertEqual(verify.execute('SELECT count(*) FROM verdicts').fetchone()[0], 100)
            finally:
                verify.close()


if __name__ == '__main__':
    unittest.main()
