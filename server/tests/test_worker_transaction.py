"""A failed background step must not leak uncommitted writes into the next step."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
