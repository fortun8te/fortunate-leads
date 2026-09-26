"""Complete workspace exports from temporary databases only."""

import csv
import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import export_workspace as exporter
from export_workspace import export_workspace


class ExportWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'workspace.sqlite'
        self.conn = db.init(self.database)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_full_export_preserves_identity_evidence_coverage_and_values(self):
        c = self.conn
        c.execute("INSERT INTO seeds(handle,ig_id,is_me,added_at) VALUES('friend','seed-1',0,'2026-01-01')")
        c.execute("INSERT INTO lists(seed,direction,state,received,total,updated_at) "
                  "VALUES('friend','followers','partial',501,1000,'2026-02-02')")
        people = [(i, f'id-{i}', f'person_{i}', 'Élodie 🌿' if i == 1 else None,
                   '=HYPERLINK("bad")' if i == 1 else '', 0 if i == 1 else None,
                   '2026-01-01', '2026-02-02') for i in range(1, 503)]
        c.executemany('INSERT INTO people(id,ig_id,handle,name,bio,followers,first_seen,updated_at) '
                      'VALUES(?,?,?,?,?,?,?,?)', people)
        c.executemany('INSERT INTO edges(seed,person_id,direction,first_seen) VALUES(?,?,?,?)',
                      [('friend', i, 'followers', '2026-01-02') for i in range(1, 503)])
        c.execute("INSERT INTO edge_evidence VALUES('friend',1,'followers',1,'2026-02-02','2026-02-02')")
        c.execute("INSERT INTO edge_evidence VALUES('friend',2,'followers',0,NULL,'2026-02-03')")
        c.commit()

        bundle = self.root / 'bundle'
        output = bundle / 'workspace.json'
        counts = export_workspace(self.database, bundle_dir=bundle)
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual({key: counts[key] for key in ('profiles', 'seeds', 'coverage', 'connections', 'edge_evidence')},
                         {'profiles': 502, 'seeds': 1, 'coverage': 1, 'connections': 502, 'edge_evidence': 2})
        self.assertEqual(len(result['profiles']), 502)
        self.assertEqual(len(result['connections']), 502)
        self.assertEqual(result['profiles'][0]['name'], 'Élodie 🌿')
        self.assertEqual(result['profiles'][0]['followers'], 0)
        self.assertIsNone(result['profiles'][1]['followers'])
        self.assertEqual([e['evidence_state'] for e in result['connections'][:3]],
                         ['observed', 'absent', 'unverified'])
        self.assertEqual(result['connections'][0]['seed_ig_id'], 'seed-1')
        self.assertEqual(result['connections'][0]['person_ig_id'], 'id-1')
        self.assertEqual(result['connections'][0]['observed_at'], '2026-02-02')
        self.assertEqual(result['connections'][1]['checked_at'], '2026-02-03')
        self.assertIsNone(result['connections'][2]['active'])
        self.assertEqual(result['coverage'][0]['received'], 501)
        self.assertEqual(result['coverage'][0]['total'], 1000)
        self.assertEqual(len(result['edge_evidence']), 2)

        with (self.root / 'bundle' / 'profiles.csv').open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 502)
        self.assertEqual(rows[0]['name'], 'Élodie 🌿')
        self.assertEqual(rows[0]['bio'], "'=HYPERLINK(\"bad\")")
        self.assertEqual(rows[0]['followers'], '0')
        self.assertEqual(rows[1]['followers'], '')
        with (self.root / 'bundle' / 'connections.csv').open(encoding='utf-8-sig', newline='') as file:
            edges = list(csv.DictReader(file))
        self.assertEqual(len(edges), 502)
        self.assertEqual(edges[0]['person_id'], '1')
        self.assertEqual(edges[2]['evidence_state'], 'unverified')
        with (self.root / 'bundle' / 'edge_evidence.csv').open(encoding='utf-8-sig', newline='') as file:
            evidence = list(csv.DictReader(file))
        self.assertEqual(len(evidence), 2)

        # A read-only export leaves the source database usable and unchanged.
        self.assertEqual(c.execute('SELECT count(*) FROM people').fetchone()[0], 502)
        self.assertEqual(c.execute('SELECT count(*) FROM edges').fetchone()[0], 502)

    def test_invalid_database_does_not_replace_existing_output(self):
        invalid = self.root / 'invalid.sqlite'
        sqlite3.connect(invalid).close()
        output = self.root / 'snapshot.json'
        output.write_text('previous', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'lacks required tables'):
            export_workspace(invalid, output)
        self.assertEqual(output.read_text(encoding='utf-8'), 'previous')
        self.assertEqual(list(self.root.glob('.snapshot.json.*.tmp')), [])

    def test_source_cannot_be_output(self):
        with self.assertRaisesRegex(ValueError, 'must not be the database'):
            export_workspace(self.database, self.database)

    def test_empty_database_and_atomic_replacement(self):
        output = self.root / 'snapshot.json'
        output.write_text('old snapshot', encoding='utf-8')
        self.assertTrue(all(count == 0 for count in export_workspace(self.database, output).values()))
        snapshot = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(snapshot['profiles'], [])
        self.assertEqual(snapshot['connections'], [])
        self.assertEqual(list(self.root.glob('.snapshot.json.*.tmp')), [])

    def test_bundle_contains_provenance_and_checksums_without_credentials(self):
        c = self.conn
        c.execute("INSERT INTO jobs(id,kind,seed,direction,state) VALUES(1,'list','friend','following','done')")
        c.execute("INSERT INTO list_runs(job_id,first_page_seen,total,total_source) VALUES(1,1,0,'current_run')")
        c.execute("INSERT INTO list_page_requests(job_id,requested_cursor,next_cursor) VALUES(1,'','')")
        c.execute("INSERT INTO pages(job_id,cursor,at) VALUES(1,'','2026-01-01')")
        c.execute("INSERT OR REPLACE INTO settings VALUES('private_example','do-not-export')")
        c.commit()
        bundle = self.root / 'bundle'
        counts = export_workspace(self.database, bundle_dir=bundle)
        data = json.loads((bundle / 'workspace.json').read_text())
        self.assertEqual(data['list_runs'][0]['total'], 0)
        self.assertEqual(data['list_runs'][0]['total_source'], 'current_run')
        self.assertEqual(data['jobs'][0]['seed'], 'friend')
        self.assertNotIn('lease_token', data['jobs'][0])
        self.assertNotIn('settings', data)
        self.assertEqual(counts['list_page_requests'], 1)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        self.assertEqual(manifest['counts'], counts)
        self.assertEqual(manifest['exported_at'], data['exported_at'])
        for name, info in manifest['files'].items():
            payload = (bundle / name).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), info['sha256'])
            self.assertEqual(len(payload), info['bytes'])
        with self.assertRaises(FileExistsError):
            export_workspace(self.database, bundle_dir=bundle)
        self.assertEqual(json.loads((bundle / 'manifest.json').read_text()), manifest)

    def test_failed_bundle_never_publishes_partial_files(self):
        bundle = self.root / 'bundle'
        original = exporter._write_csv
        calls = []
        def fail_later(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('disk full')
            return original(*args)
        with mock.patch.object(exporter, '_write_csv', side_effect=fail_later):
            with self.assertRaisesRegex(OSError, 'disk full'):
                export_workspace(self.database, bundle_dir=bundle)
        self.assertFalse(bundle.exists())
        self.assertEqual(list(self.root.glob('.bundle.*')), [])

    def test_json_and_csv_hold_same_snapshot_while_source_changes(self):
        self.conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(1,'before','now','now')")
        self.conn.commit()
        original = exporter._write_json
        def concurrent_change(*args):
            temporary = original(*args)
            self.conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(2,'after','now','now')")
            self.conn.commit()
            return temporary
        bundle = self.root / 'bundle'
        with mock.patch.object(exporter, '_write_json', side_effect=concurrent_change):
            counts = export_workspace(self.database, bundle_dir=bundle)
        data = json.loads((bundle / 'workspace.json').read_text())
        with (bundle / 'profiles.csv').open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(counts['profiles'], 1)
        self.assertEqual([row['handle'] for row in rows], ['before'])
        self.assertEqual([row['handle'] for row in data['profiles']], ['before'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 2)

    def test_csv_rejects_independent_paths_and_escapes_bom_formulas(self):
        with self.assertRaisesRegex(ValueError, 'bundle_dir'):
            export_workspace(self.database, self.root / 'out.json', self.root / 'csv')
        self.assertEqual(exporter._safe_cell('\ufeff=1+1'), "'\ufeff=1+1")
        self.assertEqual(exporter._safe_cell(0), 0)
        self.assertEqual(exporter._safe_cell(None), '')

    def test_hard_link_to_database_is_rejected(self):
        import os
        output = self.root / 'alias.json'
        os.link(self.database, output)
        with self.assertRaisesRegex(ValueError, 'must not be the database'):
            export_workspace(self.database, output)

    def test_database_companion_output_is_rejected(self):
        for suffix in ('-wal', '-shm', '-journal'):
            with self.assertRaisesRegex(ValueError, 'companion'):
                export_workspace(self.database, Path(str(self.database) + suffix))

    def test_failed_open_cleans_bundle_staging(self):
        bundle = self.root / 'bundle'
        with mock.patch.object(exporter.sqlite3, 'connect', side_effect=sqlite3.OperationalError('cannot open')):
            with self.assertRaises(sqlite3.OperationalError):
                export_workspace(self.database, bundle_dir=bundle)
        self.assertFalse(bundle.exists())
        self.assertEqual(list(self.root.glob('.bundle.*')), [])

    def test_publication_failure_removes_complete_staging(self):
        bundle = self.root / 'bundle'
        with mock.patch.object(exporter.os, 'rename', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                export_workspace(self.database, bundle_dir=bundle)
        self.assertFalse(bundle.exists())
        self.assertEqual(list(self.root.glob('.bundle.*')), [])


if __name__ == '__main__':
    unittest.main()
