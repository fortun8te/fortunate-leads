"""Offline regression coverage for durable refreshes and complete-list proof."""
import importlib.util
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import db
import test_connection_evidence


class DurableEvidenceTests(unittest.TestCase):
    setUp = test_connection_evidence.ConnectionEvidenceTest.setUp
    tearDown = test_connection_evidence.ConnectionEvidenceTest.tearDown
    run_row = test_connection_evidence.ConnectionEvidenceTest.run_row
    def test_missing_cyclic_and_disconnected_pages_cannot_disprove(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'old'})
        db.add_edge(c, 'fortun8te', pid, 'following', '2026-01-01')
        self.run_row(2, [], total=0)
        for pages in ([], [('', 'missing')], [('', 'cycle'), ('cycle', 'cycle')], [('', None), ('orphan', None)]):
            with self.subTest(pages=pages):
                c.execute('DELETE FROM list_page_requests')
                c.executemany('INSERT INTO list_page_requests VALUES(2,?,?)', pages)
                self.assertFalse(db.list_run_complete(c, 2))
                self.assertEqual(db.complete_list_snapshot(c, 'fortun8te', 'following', 2), set())
                self.assertEqual(c.execute('SELECT active FROM edge_evidence').fetchone()[0], 1)

    def test_old_completion_and_resumed_member_do_not_overwrite_newer_evidence(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'old'})
        db.add_edge(c, 'fortun8te', pid, 'following', '2026-03-01')
        self.run_row(2, [], total=0)
        self.assertEqual(db.complete_list_snapshot(c, 'fortun8te', 'following', 2, '2026-02-02'), set())
        c.execute("UPDATE edge_evidence SET active=0,checked_at='2026-03-02'")
        self.run_row(3, [pid], total=1)
        self.assertEqual(db.complete_list_snapshot(c, 'fortun8te', 'following', 3, '2026-04-01'), set())
        self.assertEqual(c.execute('SELECT active FROM edge_evidence').fetchone()[0], 0)

    def test_seed_move_queue_survives_reopen_and_old_ack_cannot_erase_new_change(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'member'})
        db.add_edge(c, 'fortun8te', pid, 'following')
        c.execute('DELETE FROM network_dirty')
        db.move_seed(c, 'fortun8te', 'renamed')
        first = c.execute('SELECT change_id FROM network_dirty WHERE person_id=?', (pid,)).fetchone()[0]
        c.commit()
        second = db.connect(Path(self.tmp.name) / 'evidence.sqlite')
        try:
            self.assertEqual(second.execute('SELECT change_id FROM network_dirty WHERE person_id=?', (pid,)).fetchone()[0], first)
            db.mark_network_dirty(second, [pid]); second.commit()
            c.execute('DELETE FROM network_dirty WHERE person_id=? AND change_id=?', (pid, first))
            self.assertGreater(c.execute('SELECT change_id FROM network_dirty WHERE person_id=?', (pid,)).fetchone()[0], first)
        finally:
            second.close()

    def test_resume_clones_complete_prefix_without_changing_old_run(self):
        c = self.conn
        self.run_row(2, [], state='error', total=0)
        c.execute("UPDATE list_page_requests SET next_cursor='next'")
        c.execute("INSERT INTO jobs(id,kind,seed,direction,state) VALUES(3,'list','fortun8te','following','queued')")
        db.start_list_run(c, 3, 'fortun8te', 'following')
        c.execute("INSERT INTO list_page_requests VALUES(3,'next',NULL)")
        self.assertTrue(db.list_run_complete(c, 3))
        self.assertFalse(db.list_run_complete(c, 2))

    def test_merge_keeps_latest_site_and_its_evidence(self):
        import qual_api
        c = self.conn
        keep = db.upsert_person(c, {'handle': 'keep', 'ig_id': '1'})
        drop = db.upsert_person(c, {'handle': 'drop'})
        qual_api._ensure(c)
        for pid, url, at in ((keep, 'https://old.test', '2026-01-01'), (drop, 'https://new.test', '2026-02-01')):
            c.execute('INSERT INTO site_reads(person_id,url,at) VALUES(?,?,?)', (pid, url, at))
            c.execute('INSERT INTO site_evidence VALUES(?,?,?)', (pid, url, url))
        db.merge_people(c, keep, drop)
        self.assertEqual(tuple(c.execute('SELECT person_id,url FROM site_reads').fetchone()), (keep, 'https://new.test'))
        self.assertEqual(tuple(c.execute('SELECT person_id,url FROM site_evidence').fetchone()), (keep, 'https://new.test'))
        self.assertEqual(c.execute('SELECT person_id FROM network_dirty').fetchone()[0], keep)

    def test_edge_change_queues_other_members_for_seed_yield_refresh(self):
        c = self.conn
        first = db.upsert_person(c, {'handle': 'first'})
        second = db.upsert_person(c, {'handle': 'second'})
        db.add_edge(c, 'fortun8te', first, 'following')
        c.execute('DELETE FROM network_dirty')
        db.add_edge(c, 'fortun8te', second, 'following')
        self.assertEqual({r[0] for r in c.execute('SELECT person_id FROM network_dirty')}, {first, second})
        c.execute('DELETE FROM network_dirty')
        db.merge_people(c, first, second)
        self.assertEqual({r[0] for r in c.execute('SELECT person_id FROM network_dirty')}, {first})

    def test_data_revision_catches_same_count_and_timestamp_edits(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'person'}, '2026-01-01')
        previous = int(db.get_setting(c, 'lead_data_rev'))
        c.execute("UPDATE people SET name='Changed' WHERE id=?", (pid,))
        self.assertGreater(int(db.get_setting(c, 'lead_data_rev')), previous)
        c.execute("INSERT INTO tags VALUES(?,'old','signal','manual')", (pid,))
        previous = int(db.get_setting(c, 'lead_data_rev'))
        c.execute("UPDATE tags SET tag='new' WHERE person_id=?", (pid,))
        self.assertGreater(int(db.get_setting(c, 'lead_data_rev')), previous)
        previous = int(db.get_setting(c, 'lead_data_rev'))
        c.execute("UPDATE seeds SET is_me=0")
        self.assertGreater(int(db.get_setting(c, 'lead_data_rev')), previous)

    def test_same_time_rename_does_not_change_identity(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'original', 'ig_id': '5'}, '2026-01-01')
        db.upsert_person(c, {'handle': 'tied', 'ig_id': '5'}, '2026-01-01')
        self.assertEqual(c.execute('SELECT handle FROM people WHERE id=?', (pid,)).fetchone()[0], 'original')


class BaseMigrationTests(unittest.TestCase):
    def test_base_copy_upgrade_keeps_manual_data_history_and_profile(self):
        root = Path(__file__).resolve().parents[2]
        code = subprocess.check_output(['git', 'show', '9bac874:server/db.py'], cwd=root, text=True)
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            module_path = temp / 'base_db.py'; module_path.write_text(code)
            spec = importlib.util.spec_from_file_location('baseline_db', module_path)
            baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
            old = baseline.init(temp / 'base.sqlite')
            pid = baseline.upsert_person(old, {'handle': 'person', 'ig_id': '99', 'bio': 'saved bio'})
            old.execute("INSERT INTO marks VALUES(?,'client','Human history','2026-01-01')", (pid,))
            old.execute("INSERT INTO tags VALUES(?,'Known personally','signal','manual')", (pid,))
            baseline.add_edge(old, 'seed', pid, 'following')
            old.commit()
            copy = sqlite3.connect(temp / 'upgrade.sqlite'); old.backup(copy); copy.close(); old.close()
            for _ in range(2):
                upgraded = db.init(temp / 'upgrade.sqlite')
                self.assertEqual(tuple(upgraded.execute('SELECT status,note FROM marks').fetchone()), ('client', 'Human history'))
                self.assertEqual(tuple(upgraded.execute('SELECT tag,source FROM tags').fetchone()), ('Known personally', 'manual'))
                self.assertEqual(upgraded.execute('SELECT bio FROM people').fetchone()[0], 'saved bio')
                self.assertEqual(upgraded.execute('SELECT count(*) FROM edges').fetchone()[0], 1)
                # Edges from before evidence tracking stay current until a fresh run of their list.
                self.assertEqual(upgraded.execute('SELECT count(*) FROM current_edges').fetchone()[0], 1)
                self.assertEqual(upgraded.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                upgraded.close()


if __name__ == '__main__':
    unittest.main()
