"""Read-only doctor checks against disposable databases."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


OPS = Path(__file__).resolve().parent


class DoctorTests(unittest.TestCase):
    def test_newest_backup_uses_mtime_and_checks_that_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'leads.sqlite'
            backups = root / 'backups'
            backups.mkdir()
            for path in (db, backups / 'leads-before-main.sqlite'):
                with sqlite3.connect(path) as conn:
                    conn.execute('CREATE TABLE test (value TEXT)')
            old = backups / 'leads-before-main.sqlite'
            fresh = backups / 'leads-20260927-120000-1-1.sqlite'
            fresh.write_bytes(b'not a sqlite database')
            now = time.time()
            os.utime(old, (now - 86400, now - 86400))
            os.utime(fresh, (now, now))

            result = subprocess.run(
                ['bash', str(OPS / 'doctor.sh'), '--quick', '--db', str(db), '--port', '1'],
                env=dict(os.environ, PYTHON=sys.executable, HOME=str(root)),
                text=True, capture_output=True, timeout=30)
            self.assertIn('newest ' + fresh.name, result.stdout)
            self.assertIn('FAIL  backup-check   cannot read newest backup:', result.stdout)


if __name__ == '__main__':
    unittest.main()
