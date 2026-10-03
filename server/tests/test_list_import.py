import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import acquisition
import db
import list_import


class ListImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = db.init(self.root / 'offline.sqlite')
        self.addCleanup(self.conn.close)
        self.file = self.root / 'lists.jsonl'

    def page(self, owner='101', member='102', **overrides):
        return dict({'source_owner_id': owner, 'source_owner_handle': 'person' + owner,
                     'direction': 'following', 'observed_at': '2025-01-02T00:00:00Z',
                     'members': [{'ig_id': member, 'handle': 'person' + member}]}, **overrides)

    def write(self, pages):
        self.file.write_text(''.join(json.dumps(page) + '\n' for page in pages))
        return hashlib.sha256(self.file.read_bytes()).hexdigest()

    def count(self, table):
        return self.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_preview_no_mutation_apply_integrates_map_without_jobs_or_complete_coverage(self):
        sha = self.write([self.page()])
        before = self.conn.total_changes
        out = list_import.preview(self.conn, self.file)
        self.assertEqual(out['sha256'], sha)
        self.assertEqual(out['members'], 1)
        self.assertEqual(self.conn.total_changes, before)
        result = list_import.apply(self.conn, self.file, sha)
        self.assertEqual(result['new_canonical_edges'], 1)
        self.assertEqual(self.count('current_edges'), 1)
        self.assertEqual(self.count('map_person_degree'), 1)
        for table in ('jobs', 'lists', 'list_runs', 'list_members', 'list_page_requests'):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.conn.execute('SELECT observed_at FROM edge_observations').fetchone()[0], '2025-01-02T00:00:00+00:00')
        source = self.conn.execute('SELECT source_kind,source_ref FROM acquisition_events').fetchone()
        self.assertEqual(source['source_kind'], 'local_import')
        self.assertEqual(json.loads(source['source_ref'])['sha256'], sha)

    def test_replay_relocated_file_and_inverse_edge_have_one_canonical_fact(self):
        sha = self.write([self.page(), self.page('102', '101', direction='followers')])
        result = list_import.apply(self.conn, self.file, sha, batch_size=1)
        self.assertEqual(result['new_canonical_edges'], 1)
        self.assertEqual(self.count('acquisition_facts'), 1)
        moved = self.root / 'renamed.jsonl'
        shutil.copyfile(self.file, moved)
        before = self.count('edge_observations')
        self.assertTrue(list_import.apply(self.conn, moved, sha)['replayed'])
        self.assertEqual(self.count('edge_observations'), before)
        self.assertEqual(self.count('acquisition_events'), 2)

    def test_crash_rolls_back_chunk_and_resumes_exact_checkpoint(self):
        sha = self.write([self.page(member=str(i)) for i in range(102, 108)])
        record = acquisition.record_page
        def crash(*args, **kwargs):
            if kwargs['event_id'].endswith(':4'):
                raise RuntimeError('simulated interruption')
            return record(*args, **kwargs)
        with patch.object(acquisition, 'record_page', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                list_import.apply(self.conn, self.file, sha, batch_size=2)
        self.assertEqual(self.conn.execute('SELECT last_line FROM local_list_imports').fetchone()[0], 2)
        self.assertEqual(self.count('acquisition_events'), 2)
        self.assertEqual(self.count('edges'), 2)
        self.assertFalse(self.conn.in_transaction)
        result = list_import.apply(self.conn, self.file, sha, batch_size=2)
        self.assertEqual(result['last_line'], 6)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(self.count('edges'), 6)
        self.assertEqual(self.count('acquisition_events'), 6)

    def test_identity_conflict_fails_before_row_writes(self):
        db.upsert_person(self.conn, {'ig_id': '999', 'handle': 'person102'})
        self.conn.commit()
        sha = self.write([self.page()])
        with self.assertRaisesRegex(ValueError, 'another saved account'):
            list_import.preview(self.conn, self.file)
        with self.assertRaisesRegex(ValueError, 'another saved account'):
            list_import.apply(self.conn, self.file, sha)
        self.assertEqual(self.count('edges'), 0)
        self.assertEqual(self.count('people'), 1)
        self.assertEqual(self.count('acquisition_events'), 0)

    def test_existing_renamed_identity_uses_known_handle_and_keeps_live_work(self):
        pid = db.upsert_person(self.conn, {'ig_id': '101', 'handle': 'currentname'})
        db.queue_list(self.conn, 'currentname', 'following')
        self.conn.execute("UPDATE jobs SET state='leased',lease_token='keep',lane='keep'")
        self.conn.commit()
        before = [tuple(r) for r in self.conn.execute('SELECT * FROM jobs')]
        sha = self.write([self.page()])
        list_import.apply(self.conn, self.file, sha)
        self.assertEqual(self.conn.execute('SELECT handle FROM people WHERE id=?', (pid,)).fetchone()[0], 'currentname')
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM jobs')], before)
        self.assertEqual(self.conn.execute('SELECT seed FROM edges').fetchone()[0], 'currentname')

    def test_older_import_does_not_reactivate_newer_retirement(self):
        pid = db.upsert_person(self.conn, {'ig_id': '102', 'handle': 'person102'})
        db.add_edge(self.conn, 'person101', pid, 'following', '2025-05-01T00:00:00+00:00')
        self.conn.execute('UPDATE edge_evidence SET active=0')
        self.conn.commit()
        sha = self.write([self.page()])
        list_import.apply(self.conn, self.file, sha)
        self.assertEqual(self.count('current_edges'), 0)
        self.assertEqual(self.count('edge_observations'), 1)

    def test_unknown_handles_stage_ids_without_fabricating_profiles(self):
        sha = self.write([self.page(source_owner_handle=None, members=[{'ig_id': '102'}, {'ig_id': '103'}])])
        result = list_import.apply(self.conn, self.file, sha)
        self.assertEqual(result['staged_unresolved_ids'], 3)
        self.assertEqual(self.count('people'), 0)
        self.assertEqual(self.count('edges'), 0)
        self.assertEqual(self.count('acquisition_facts'), 2)

    def test_id_only_known_people_map_and_self_entry_is_ignored(self):
        for i in ('101', '102'):
            db.upsert_person(self.conn, {'ig_id': i, 'handle': 'person' + i})
        self.conn.commit()
        sha = self.write([self.page(source_owner_handle=None, members=[{'ig_id': '102'}, {'ig_id': '101'}])])
        out = list_import.apply(self.conn, self.file, sha)
        self.assertEqual(out['staged_unresolved_ids'], 0)
        self.assertEqual(self.count('edges'), 1)
        self.assertEqual(self.count('acquisition_facts'), 1)

    def test_changed_file_rejected_before_any_data_writes(self):
        sha = self.write([self.page()])
        self.write([self.page(member='103')])
        with self.assertRaisesRegex(ValueError, 'differs from the preview'):
            list_import.apply(self.conn, self.file, sha)
        self.assertEqual(self.count('people'), 0)

    def test_invalid_page_dates_ids_handles_and_unbounded_page(self):
        for override in ({'observed_at': '2025-01-01'}, {'observed_at': '2999-01-01T00:00:00Z'},
                         {'source_owner_id': True}, {'source_owner_id': 'NaN'}, {'direction': 'friends'},
                         {'source_owner_handle': 'https://instagram.com/someone'},
                         {'members': [{'ig_id': '0'}]}, {'members': [{'ig_id': '102'}] * 2001}):
            with self.subTest(override=list(override)):
                self.write([self.page(**override)])
                with self.assertRaises(ValueError):
                    list_import.preview(self.conn, self.file)
        self.assertEqual(self.count('people'), 0)

    def test_row_payload_extras_do_not_escape_to_profiles_or_evidence(self):
        sha = self.write([self.page(biography='SECRET_BIO', password='SECRET_PASSWORD',
            members=[{'ig_id': '102', 'handle': 'person102', 'bio': 'SECRET_MEMBER_BIO'}],
            source_context={'route': 'following', 'run_id': 'export1', 'warmup': False, 'cookie': 'SECRET_COOKIE'})])
        list_import.apply(self.conn, self.file, sha)
        dump = '\n'.join(self.conn.iterdump())
        self.assertNotIn('SECRET_', dump)
        self.assertIn('export1', dump)

    def test_conflicting_handles_across_file_rejected_by_readonly_preview(self):
        self.write([self.page(), self.page(members=[{'ig_id': '103', 'handle': 'person102'}])])
        with self.assertRaisesRegex(ValueError, 'File assigns'):
            list_import.preview(self.conn, self.file)
        self.assertEqual(self.count('people'), 0)

    def test_collector_holds_budgets_and_controls_unchanged(self):
        for key, value in [('paused', True), ('collect_bios', False), ('stage_lists_paused', True)]:
            db.set_setting(self.conn, key, value)
        self.conn.execute("INSERT INTO accounts(lane_id,hold,budget,cooldown_until) VALUES('x','security','{\"list\":0}','2999-01-01')")
        self.conn.commit()
        before = [tuple(r) for r in self.conn.execute('SELECT * FROM accounts')]
        sha = self.write([self.page()])
        list_import.apply(self.conn, self.file, sha)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM accounts')], before)
        self.assertTrue(db.get_setting(self.conn, 'paused'))
        self.assertFalse(db.get_setting(self.conn, 'collect_bios'))
        self.assertTrue(db.get_setting(self.conn, 'stage_lists_paused'))

    def test_source_changes_after_snapshot_cannot_change_imported_bytes(self):
        sha = self.write([self.page()])
        original = acquisition.ensure
        changed = [False]
        def change_source(conn):
            if not changed[0]:
                self.write([self.page(member='999')])
                changed[0] = True
            return original(conn)
        with patch.object(acquisition, 'ensure', side_effect=change_source):
            list_import.apply(self.conn, self.file, sha)
        ids = {r[0] for r in self.conn.execute('SELECT ig_id FROM people')}
        self.assertEqual(ids, {'101', '102'})

    def test_malformed_later_row_rejects_entire_file_before_first_commit(self):
        self.file.write_text(json.dumps(self.page()) + '\n{"broken":\n')
        sha = hashlib.sha256(self.file.read_bytes()).hexdigest()
        before = self.conn.total_changes
        with self.assertRaisesRegex(ValueError, 'Line 2'):
            list_import.apply(self.conn, self.file, sha, batch_size=1)
        self.assertEqual(self.conn.total_changes, before)
        for table in ('acquisition_events', 'local_list_imports', 'people', 'edges'):
            self.assertEqual(self.count(table), 0, table)
        self.assertFalse(self.conn.in_transaction)

    def test_cli_defaults_readonly_and_apply_requires_hash(self):
        import subprocess
        self.write([self.page()])
        command = [sys.executable, str(Path(__file__).resolve().parents[2] / 'ops/import_lists.py'),
                   '--db', str(self.root / 'offline.sqlite'), '--file', str(self.file)]
        output = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(output.stdout)['mode'], 'preview')
        self.assertEqual(self.count('people'), 0)
        denied = subprocess.run(command + ['--apply'], capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('--sha256', denied.stderr)
        output = subprocess.run(command + ['--apply', '--sha256', hashlib.sha256(self.file.read_bytes()).hexdigest()],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(output.stdout)['completed'], 1)
        self.assertEqual(self.count('edges'), 1)


    def test_late_cross_row_identity_conflict_rejects_file_without_preview(self):
        sha = self.write([self.page(), self.page(members=[{'ig_id': '103', 'handle': 'person102'}])])
        before = self.conn.total_changes
        with self.assertRaisesRegex(ValueError, 'Line 2: File assigns'):
            list_import.apply(self.conn, self.file, sha, batch_size=1)
        self.assertEqual(self.conn.total_changes, before)
        for table in ('acquisition_events', 'local_list_imports', 'people', 'edges'):
            self.assertEqual(self.count(table), 0, table)

    def test_new_member_refreshes_existing_neighbors_once_per_page(self):
        owner = db.upsert_person(self.conn, {'ig_id': '101', 'handle': 'person101'})
        earlier = db.upsert_person(self.conn, {'ig_id': '102', 'handle': 'person102'})
        db.add_edge(self.conn, 'person101', earlier, 'following', '2024-01-01T00:00:00+00:00')
        self.conn.execute('DELETE FROM network_dirty')
        self.conn.commit()
        sha = self.write([self.page(members=[{'ig_id': '103', 'handle': 'person103'}, {'ig_id': '104', 'handle': 'person104'}])])
        with patch.object(db, 'dirty_seed_members', wraps=db.dirty_seed_members) as dirty:
            list_import.apply(self.conn, self.file, sha)
        dirty.assert_called_once_with(self.conn, 'person101')
        ids = {row[0] for row in self.conn.execute('SELECT person_id FROM network_dirty')}
        self.assertIn(owner, ids)
        self.assertIn(earlier, ids)
        self.assertEqual(len(ids), 4)
