"""Evidence dates describe received page members, never the freshness of a whole list."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import migrate


class ObservationTest(unittest.TestCase):
    def setUp(self):
        # Defer loading server until discovery has configured the legacy server-test stub.
        global server
        import server
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.conn = db.init(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def job(self, seed='brand', direction='followers'):
        jid = self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list',?,?,'leased')",
                                (seed, direction)).lastrowid
        self.conn.commit()
        return jid

    def page(self, users, job_id=None, cursor=None, ts='2026-09-01T00:00:00+00:00', **extra):
        body = dict(seed='brand', direction='followers', users=users, job_id=job_id, next_cursor=cursor)
        body.update(extra)
        if job_id:   # the extension re-leases its list before sending the next page
            self.conn.execute("UPDATE jobs SET state='leased' WHERE id=? AND state='queued'", (job_id,))
            self.conn.commit()
        with patch.object(db, 'now', return_value=ts):
            return server.ext_list_page(self.conn, {}, body)

    def rows(self, table):
        return [tuple(r) for r in self.conn.execute('SELECT * FROM ' + table)]

    def test_managed_replay_does_not_refresh_or_add_members(self):
        jid = self.job()
        self.page([{'handle': 'alice'}], jid, 'p1')
        before = {t: self.rows(t) for t in ('people', 'edges', 'edge_observations', 'lists', 'pages')}
        out = self.page([{'handle': 'alice', 'name': 'changed'}, {'handle': 'injected'}], jid, 'p1',
                        ts='2026-09-20T00:00:00+00:00')
        self.assertTrue(out['duplicate'])
        for t, expected in before.items():
            self.assertEqual(self.rows(t), expected, t)

    def test_partial_refresh_only_updates_observed_members_and_never_deletes(self):
        self.page([{'handle': 'alice'}, {'handle': 'bob'}], self.job(), done=True)
        self.page([{'handle': 'alice'}], self.job(), ts='2026-09-20T00:00:00+00:00', done=True)
        dates = dict(self.conn.execute('SELECT p.handle,max(o.observed_at) FROM people p '
                                       'JOIN edge_observations o ON o.person_id=p.id GROUP BY p.id'))
        self.assertEqual(dates, {'alice': '2026-09-20T00:00:00+00:00', 'bob': '2026-09-01T00:00:00+00:00'})
        self.assertEqual(len(self.rows('edges')), 2)
        self.assertEqual(self.conn.execute('SELECT received FROM lists').fetchone()[0], 1)   # members seen this run
        self.assertEqual(len(self.rows('edge_observations')), 3)

    def test_terminal_jobs_cannot_introduce_new_pages(self):
        for state in ('done', 'error'):
            jid = self.job()
            self.conn.execute('UPDATE jobs SET state=? WHERE id=?', (state, jid))
            self.conn.commit()
            self.assertTrue(self.page([{'handle': state}], jid, state)['stale'])   # finished work takes no pages
        self.assertEqual(self.rows('people'), [])
        self.assertEqual(self.rows('pages'), [])
        self.assertEqual(self.rows('edge_observations'), [])

    def test_mismatched_and_unknown_job_rejected_before_writes(self):
        jid = self.job()
        for options in ({'seed': 'other'}, {'direction': 'following'}, {'job_id': 999}):
            payload = dict(job_id=jid)
            payload.update(options)
            with self.assertRaises(server.Bad):
                self.page([{'handle': 'alice'}], **payload)
        for t in ('people', 'edges', 'pages', 'edge_observations', 'seeds'):
            self.assertEqual(self.rows(t), [], t)

    def test_seed_handle_reuse_does_not_reassign_history(self):
        jid = self.job()
        self.page([{'handle': 'alice'}], jid, 'p1', ig_id='original-id')
        self.conn.execute("UPDATE jobs SET state='leased' WHERE id=?", (jid,))   # handed out again
        self.conn.commit()
        tables = ('people', 'edges', 'pages', 'edge_observations', 'seeds', 'lists', 'jobs')
        before = {t: self.rows(t) for t in tables}
        with self.assertRaises(server.Bad):
            self.page([{'handle': 'bob'}], jid, 'p2', ig_id='different-account-id')
        for t, expected in before.items():
            self.assertEqual(self.rows(t), expected, t)
        self.page([{'handle': 'bob'}], jid, 'p2')
        self.assertEqual(self.conn.execute('SELECT ig_id FROM seeds').fetchone()[0], 'original-id')

    def test_seed_id_must_match_existing_profile_and_have_valid_type(self):
        db.upsert_person(self.conn, {'handle': 'brand', 'ig_id': 'original-id'})
        self.conn.commit()
        for value in ('different-account-id', True, ['original-id'], {}, ''):
            with self.assertRaises(server.Bad):
                self.page([{'handle': 'alice'}], ig_id=value)
        self.assertEqual(self.rows('seeds'), [])
        self.assertEqual(self.rows('ingested_list_pages'), [])
        self.assertEqual(self.rows('edges'), [])

    def test_profile_seed_identity_conflict_rejects_all_writes(self):
        self.page([{'handle': 'alice'}], ig_id='original-id')
        before = {t: self.rows(t) for t in ('people', 'seeds', 'edges', 'edge_observations', 'jobs')}
        with self.assertRaises(server.Bad):
            server.ext_profile(self.conn, {}, {'profile': {'handle': 'brand', 'ig_id': 'other-id', 'bio': 'new'}})
        for t, expected in before.items():
            self.assertEqual(self.rows(t), expected, t)
        server.ext_profile(self.conn, {}, {'profile': {'handle': 'brand', 'ig_id': 'original-id', 'bio': 'valid'}})
        self.assertEqual(self.conn.execute("SELECT bio FROM people WHERE handle='brand'").fetchone()[0], 'valid')

    def test_profile_cannot_reassign_idless_seed_with_known_person_identity(self):
        db.upsert_person(self.conn, {'handle': 'brand', 'ig_id': 'original-id'})
        self.conn.commit()
        self.page([{'handle': 'alice'}])
        self.assertIsNone(self.conn.execute('SELECT ig_id FROM seeds').fetchone()[0])
        tables = ('people', 'seeds', 'edges', 'edge_observations', 'jobs')
        before = {t: self.rows(t) for t in tables}
        with self.assertRaises(server.Bad):
            server.ext_profile(self.conn, {}, {'profile': {'handle': 'brand', 'ig_id': 'other-id'}})
        for t, expected in before.items():
            self.assertEqual(self.rows(t), expected, t)
        server.ext_profile(self.conn, {}, {'profile': {'handle': 'brand', 'ig_id': 'original-id'}})
        self.assertEqual(self.conn.execute('SELECT ig_id FROM seeds').fetchone()[0], 'original-id')

    def test_legacy_import_cannot_reassign_idless_seed_with_known_person_identity(self):
        db.upsert_person(self.conn, {'handle': 'brand', 'ig_id': 'original-id'})
        self.conn.commit()
        # Reuse the conflict fixture but retain the original identity only in people.
        self.test_legacy_import_cannot_reassign_known_seed_identity(clear_seed_id=True)

    def test_legacy_import_cannot_reassign_known_seed_identity(self, clear_seed_id=False):
        self.page([{'handle': 'alice'}], ig_id='original-id')
        if clear_seed_id:
            self.conn.execute('UPDATE seeds SET ig_id=NULL')
            self.conn.commit()
        before = {t: self.rows(t) for t in ('people', 'seeds', 'edges', 'edge_observations', 'jobs', 'lists')}
        source = Path(self.tmp.name) / 'old.sqlite'
        old = sqlite3.connect(source)
        old.executescript("""
            CREATE TABLE entities(id TEXT, platform TEXT, handle TEXT, platform_id TEXT, created_at TEXT);
            CREATE TABLE collections(source_entity TEXT);
            CREATE TABLE profile_current(entity_id TEXT, field TEXT, value_json TEXT, observed_at TEXT);
            CREATE TABLE exporter_commands(payload TEXT, type TEXT, created_at TEXT);
            INSERT INTO entities VALUES('S','instagram','brand','other-id','2020-01-01');
            INSERT INTO collections VALUES('S');
        """)
        old.close()
        with self.assertRaisesRegex(ValueError, 'seed account identity changed'):
            migrate.migrate(source, self.path, None)
        for t, expected in before.items():
            self.assertEqual(self.rows(t), expected, t)

    def test_unmanaged_replay_ignores_order_profile_changes_and_duplicate_members(self):
        self.page([{'handle': 'alice', 'ig_id': 1}, {'handle': 'bob'}])
        before = self.rows('edge_observations')
        result = self.page([{'handle': 'BOB'}, {'handle': 'newalice', 'ig_id': '1', 'name': 'New'},
                            {'handle': 'bob'}], ts='2026-09-20T00:00:00+00:00')
        self.assertTrue(result['duplicate'])
        self.assertEqual(self.rows('edge_observations'), before)
        self.assertEqual(len(self.rows('ingested_list_pages')), 1)
        self.page([{'handle': 'alice', 'ig_id': 1}, {'handle': 'bob'}], direction='following')
        self.assertEqual(len(self.rows('edge_observations')), 4)

    def test_failure_rolls_back_entire_page_and_can_retry(self):
        for managed in (True, False):
            jid = self.job() if managed else None
            tables = ('people', 'edges', 'edge_observations', 'pages', 'ingested_list_pages', 'lists', 'seeds', 'jobs')
            before = {t: self.rows(t) for t in tables}
            with patch.object(server.rules, 'sync', side_effect=RuntimeError('injected failure')):
                with self.assertRaises(RuntimeError):
                    self.page([{'handle': 'alice'}], jid)
            for t, expected in before.items():
                self.assertEqual(self.rows(t), expected, t)
            self.assertNotIn('duplicate', self.page([{'handle': 'alice'}], jid))

    def test_legacy_migration_is_idempotent_and_does_not_invent_observations(self):
        pid = db.upsert_person(self.conn, {'handle': 'legacy'})
        db.add_edge(self.conn, 'brand', pid, 'followers', '2020-01-01')
        self.conn.execute('DROP TABLE edge_observations')
        self.conn.execute('DROP TABLE ingested_list_pages')
        self.conn.commit()
        self.conn.close()
        self.conn = db.init(self.path)
        self.conn.close()
        self.conn = db.init(self.path)
        self.assertEqual(self.rows('edge_observations'), [])
        self.assertEqual(self.rows('edges'), [('brand', pid, 'followers', '2020-01-01')])
        # Old INSERT INTO edges VALUES callers remain compatible.
        self.conn.execute('INSERT INTO edges VALUES(?,?,?,?)', ('brand', pid, 'following', '2020-02-01'))

    def test_failure_after_list_update_rolls_back_cursor_and_evidence(self):
        jid = self.job()
        self.page([{'handle': 'alice'}], jid, 'p1')
        tables = ('people', 'edges', 'edge_observations', 'pages', 'lists', 'jobs')
        before = {t: self.rows(t) for t in tables}
        self.conn.execute("CREATE TRIGGER fail_job_update BEFORE UPDATE ON jobs BEGIN "
                          "SELECT RAISE(ABORT, 'injected final-write failure'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.page([{'handle': 'bob'}], jid, 'p2', done=True)
        for t, expected in before.items():
            self.assertEqual(self.rows(t), expected, t)
        self.conn.execute('DROP TRIGGER fail_job_update')
        self.conn.commit()
        self.assertNotIn('duplicate', self.page([{'handle': 'bob'}], jid, 'p2', done=True))

    def test_identity_merge_preserves_distinct_evidence_and_earliest_edge(self):
        keep = db.upsert_person(self.conn, {'handle': 'stable', 'ig_id': '123'})
        drop = db.upsert_person(self.conn, {'handle': 'alias'})
        db.add_edge(self.conn, 'brand', keep, 'followers', '2026-09-02')
        db.add_edge(self.conn, 'brand', drop, 'followers', '2026-09-01')
        for pid, key, ts in ((keep, 'same', '2026-09-02'), (drop, 'same', '2026-09-01'),
                             (keep, 'one', '2026-09-03'), (drop, 'two', '2026-09-04')):
            db.observe_edge(self.conn, 'brand', pid, 'followers', key, None, ts)
        self.assertEqual(db.upsert_person(self.conn, {'handle': 'alias', 'ig_id': '123'}), keep)
        self.assertEqual(self.rows('edges'), [('brand', keep, 'followers', '2026-09-01')])
        self.assertEqual({r[1] for r in self.rows('edge_observations')}, {keep})
        self.assertEqual(dict(self.conn.execute('SELECT page_key,observed_at FROM edge_observations')),
                         {'same': '2026-09-01', 'one': '2026-09-03', 'two': '2026-09-04'})
        self.assertEqual(len(self.rows('people')), 1)


if __name__ == '__main__':
    unittest.main()
