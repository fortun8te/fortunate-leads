"""A backup refuses to start when the disk cannot hold it; doctor and the server use the same lines."""
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

OPS = Path(__file__).resolve().parent


def environment(root, **extra):
    home = root / 'home'
    home.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(home), PYTHON=sys.executable, **extra)


def make_db(path):
    con = sqlite3.connect(path)
    for name in ('people', 'seeds', 'lists', 'edges', 'accounts', 'settings'):
        con.execute(f'CREATE TABLE {name}(x)')
    con.execute('INSERT INTO people VALUES(1)')
    con.commit()
    con.close()


class BackupDiskSpaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'leads.sqlite'
        make_db(self.db)
        self.dest = self.root / 'backups'

    def run_backup(self, **extra):
        return subprocess.run(['bash', str(OPS / 'backup.sh'), '--db', str(self.db), '--dest', str(self.dest),
                               '--no-rotate'], env=environment(self.root, **extra), capture_output=True, text=True,
                              timeout=60)

    def test_backup_stops_before_writing_anything_when_space_is_short(self):
        result = self.run_backup(FL_BACKUP_MIN_FREE_MB='999999999')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('free on', result.stderr)
        self.assertIn('needs about', result.stderr)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_backup_runs_when_there_is_room_and_reports_how_long_it_took(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'backup ok: .* \(\d+(\.\d+)? \w+, people=1, \d+s\)')
        self.assertEqual(len(list(self.dest.glob('leads-*.sqlite'))), 1)

    def test_doctor_warns_and_fails_at_the_same_lines_as_the_server(self):
        text = (OPS / 'doctor.sh').read_text()
        fail = int(re.search(r'DISK_FAIL_KB=\$\(\((\d+) / 1024\)\)', text).group(1))
        warn = int(re.search(r'DISK_WARN_KB=\$\(\((\d+) / 1024\)\)', text).group(1))
        sys.path.insert(0, str(OPS.parent / 'server'))
        import disk_guard
        self.assertEqual(fail, disk_guard.DEFAULT_PAUSE_GB * disk_guard.GB)
        self.assertEqual(warn, disk_guard.DEFAULT_WARN_GB * disk_guard.GB)


if __name__ == '__main__':
    unittest.main()
