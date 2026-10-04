"""Restore test for every backup type, on small generated databases only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
sys.path.insert(0, str(REPO / 'server'))
import db  # noqa: E402
import dbsafe  # noqa: E402

spec = importlib.util.spec_from_file_location('restore_check', OPS / 'restore_check.py')
restore_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore_check)
spec = importlib.util.spec_from_file_location('backup_workspace', OPS / 'backup-workspace.py')
backup_workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_workspace)


def populate(path, people=6):
    conn = db.init(str(path))
    for i in range(people):
        pid = db.upsert_person(conn, {'handle': f'person{i}', 'ig_id': str(500 + i), 'bio': f'bio {i}'}, '2026-01-01')
        db.add_edge(conn, 'seed', pid, 'followers', '2026-01-01', observed=False)
    conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(1,'interested','n','2026-01-01')")
    conn.commit()
    conn.close()


class RestoreCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.backups = self.data / 'backups'
        self.backups.mkdir(parents=True)
        self.expected = restore_check.expected_schema()

    def run_backup_script(self):
        live = self.root / 'live' / 'leads.sqlite'
        live.parent.mkdir()
        populate(live)
        home = self.root / 'home'
        home.mkdir(exist_ok=True)
        result = subprocess.run(['bash', str(OPS / 'backup.sh'), '--db', str(live), '--dest', str(self.backups),
                                 '--no-rotate'], env=dict(os.environ, HOME=str(home), PYTHON=sys.executable),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return live

    def test_daily_backup_made_by_the_real_script_restores_and_serves(self):
        self.run_backup_script()
        candidates, _ = restore_check.discover(self.data)
        self.assertEqual([c['kind'] for c in candidates], ['daily'])
        scratch = self.root / 'scratch'
        scratch.mkdir()
        row = restore_check.check_database(candidates[0], scratch, self.expected, full=True, serve_port=9120,
                                           python=sys.executable, allow_copy=True)
        self.assertEqual(row['status'], 'PASS', row)
        self.assertEqual(row['counts']['people'], 6)
        self.assertEqual(row['journal_mode'], 'delete')
        self.assertTrue(row['server']['ok'], row['server'])
        self.assertEqual(row['server']['total'], 6)
        self.assertEqual(row['server']['exit_code'], 0)
        # the originals were not opened: no sidecar appeared beside the backup
        self.assertEqual([p.name for p in self.backups.iterdir()], [Path(candidates[0]['path']).name])

    def test_every_backup_type_is_classified_and_stray_sidecars_are_listed(self):
        for name in ('leads-20261001-033003-123-456.sqlite', 'leads-20260925-0331.sqlite',
                     'leads.backup-pre-statuses-1790276242.sqlite', 'before-modes-protection-20261001.sqlite'):
            (self.data / ('' if name.startswith('leads.backup') else 'backups') / name).write_bytes(b'x')
        (self.backups / 'before-modes-protection-20261001.sqlite-wal').write_bytes(b'')
        (self.backups / 'leads-20260926-0222.sqlite.partial-shm').write_bytes(b'x')
        (self.data / 'leads.sqlite').write_bytes(b'live database is not a backup')
        candidates, ignored = restore_check.discover(self.data)
        kinds = {Path(c['path']).name: c['kind'] for c in candidates}
        self.assertEqual(kinds, {'leads-20261001-033003-123-456.sqlite': 'daily',
                                 'before-modes-protection-20261001.sqlite': 'checkpoint',
                                 'leads-20260925-0331.sqlite': 'legacy-dated',
                                 'leads.backup-pre-statuses-1790276242.sqlite': 'legacy-root'})
        self.assertEqual([Path(i['path']).name for i in ignored], ['leads-20260926-0222.sqlite.partial-shm'])
        checkpoint = [c for c in candidates if c['kind'] == 'checkpoint'][0]
        self.assertEqual(checkpoint['sidecars'], ['before-modes-protection-20261001.sqlite-wal'])

    def test_checkpoint_in_wal_mode_and_an_old_schema_both_restore(self):
        live = self.root / 'live.sqlite'
        populate(live)
        checkpoint = self.backups / 'before-change.sqlite'
        shutil.copyfile(live, checkpoint)
        (self.backups / 'before-change.sqlite-wal').write_bytes(b'')
        old = self.backups / 'leads-20260925-0331.sqlite'
        shutil.copyfile(live, old)
        with sqlite3.connect(old) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')
            if sqlite3.sqlite_version_info >= (3, 35):
                conn.execute('ALTER TABLE people DROP COLUMN pic_attempts')
                conn.execute('ALTER TABLE collector_events DROP COLUMN route')
                conn.execute('ALTER TABLE accounts DROP COLUMN list_endpoint_until')
        candidates, _ = restore_check.discover(self.data)
        scratch = self.root / 'scratch'
        scratch.mkdir()
        rows = {Path(c['path']).name: restore_check.check_database(c, scratch, self.expected, full=True, serve_port=0,
                                                                    python=sys.executable, allow_copy=True)
                for c in candidates}
        self.assertEqual(rows['before-change.sqlite']['status'], 'PASS', rows['before-change.sqlite'])
        self.assertEqual(rows['leads-20260925-0331.sqlite']['status'], 'PASS', rows['leads-20260925-0331.sqlite'])
        if sqlite3.sqlite_version_info >= (3, 35):
            self.assertGreaterEqual(rows['leads-20260925-0331.sqlite']['needs_upgrade']['columns'], 3)
        self.assertEqual(rows['before-change.sqlite']['needs_upgrade'], {'tables': 0, 'columns': 0})

    def test_damaged_backup_fails_and_the_run_exits_nonzero(self):
        live = self.root / 'live.sqlite'
        populate(live)
        good = self.backups / 'leads-20261001-033003-1-1.sqlite'
        dbsafe.backup_database(live, good)
        bad = self.backups / 'leads-20261002-033003-1-1.sqlite'
        bad.write_bytes(good.read_bytes()[:4096 * 3])
        scratch = self.root / 'scratch'
        status = restore_check.main(['--data', str(self.data), '--scratch', str(scratch), '--json', str(self.root / 'r.json')])
        self.assertEqual(status, 1)
        report = json.loads((self.root / 'r.json').read_text())
        by_name = {r['name']: r['status'] for r in report['rows']}
        self.assertEqual(by_name, {good.name: 'PASS', bad.name: 'FAIL'})
        self.assertEqual(report['totals'], {'PASS': 1, 'WARN': 0, 'FAIL': 1})
        self.assertFalse(scratch.exists())            # scratch is removed at the end
        self.assertEqual(bad.stat().st_size, 4096 * 3)  # and the original was left alone

    def test_workspace_bundle_restores_database_and_photos(self):
        repo = self.root / 'repo'
        (repo / 'data' / 'pfp').mkdir(parents=True)
        populate(repo / 'data' / 'leads.sqlite')
        for i in range(3):
            (repo / 'data' / 'pfp' / f'{i + 1}.jpg').write_bytes(b'\xff\xd8\xff' + bytes(40) + b'\xff\xd9')
        (repo / 'ops').mkdir()
        (repo / 'ops/startup_profiles.json').write_text('{}')
        bundle = self.backups / 'snapshot-20261001'
        backup_workspace.create_snapshot(repo, bundle)
        candidates, _ = restore_check.discover(self.data)
        self.assertEqual([c['kind'] for c in candidates], ['bundle'])
        scratch = self.root / 'scratch'
        scratch.mkdir()
        row = restore_check.check_bundle(candidates[0], scratch, self.expected, serve_port=0, python=sys.executable)
        self.assertEqual(row['status'], 'PASS', row)
        self.assertEqual(row['photos'], {'in_manifest': 3, 'restored': 3})
        self.assertEqual(row['database']['counts']['people'], 6)

    def test_export_bundle_is_reported_but_not_restored_here(self):
        export = self.backups / 'export-001'
        export.mkdir()
        (export / 'manifest.json').write_text(json.dumps({'format': 'fortunate-leads-bundle-v1', 'files': {}}))
        candidates, _ = restore_check.discover(self.data)
        scratch = self.root / 'scratch'
        scratch.mkdir()
        row = restore_check.check_bundle(candidates[0], scratch, self.expected, serve_port=0, python=sys.executable)
        self.assertEqual(row['kind'], 'export-bundle')
        self.assertEqual(row['status'], 'WARN')


if __name__ == '__main__':
    unittest.main()
