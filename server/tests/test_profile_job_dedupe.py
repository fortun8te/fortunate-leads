"""One target read survives duplicate queue history; all databases are temporary."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import accounts
import db
import server


class ProfileJobDedupeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(Path(self.tmp.name) / 'jobs.sqlite')
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        for lane in ('a', 'b'):
            self.conn.execute("INSERT INTO accounts(lane_id,ig_id,handle,role,last_seen,version) "
                              "VALUES(?,?,?,'bios',?,'3.9.17')",
                              (lane, lane, 'collector' + lane, self.now.isoformat()))

    def job(self, handle, priority=10):
        return self.conn.execute("INSERT INTO jobs(kind,handle,priority,created_at) "
                                 "VALUES('profile',?,?,?)",
                                 (handle, priority, self.now.isoformat())).lastrowid

    def lease(self, jid, lane):
        self.conn.execute("UPDATE jobs SET state='leased',lane=?,lease_token=?,leased_until=?,attempts=1 WHERE id=?",
                          (lane, 'token' + lane, (self.now + timedelta(minutes=10)).isoformat(), jid))

    def test_duplicate_target_cannot_lease_on_second_lane(self):
        first = self.job('same_target')
        second = self.job('SAME_TARGET', 10000)
        self.job('different', 1)
        selected = accounts.pick_job(self.conn, 'a', ['profile'], self.now)
        self.assertEqual(selected['handle'], 'same_target')
        self.assertEqual(selected['priority'], 10000)
        self.lease(selected['id'], 'a')
        next_job = accounts.pick_job(self.conn, 'b', ['profile'], self.now)
        self.assertEqual(next_job['handle'], 'different')
        rows = self.conn.execute('SELECT id,state FROM jobs WHERE id IN (?,?)', (first, second)).fetchall()
        self.assertEqual(sorted(r['state'] for r in rows), ['cancelled', 'leased'])

    def test_rate_warning_revokes_every_already_leased_duplicate(self):
        first = self.job('same_target', 10000)
        second = self.job('SAME_TARGET', 9000)
        self.lease(first, 'a')
        self.lease(second, 'b')
        self.conn.commit()
        server.ext_error(self.conn, {}, {'lane_id': 'a', 'account': {'ig_id': 'a', 'handle': 'collectora'},
                         'job_id': first, 'lease_token': 'tokena', 'code': 'rate_limit',
                         'reason': 'please_wait_page', 'route': 'profile_page'})
        rows = self.conn.execute('SELECT * FROM jobs WHERE id IN (?,?)', (first, second)).fetchall()
        self.assertTrue(all(r['state'] != 'leased' and r['lease_token'] is None and r['lane'] is None for r in rows))
        self.assertTrue(all(accounts.utc(r['retry_not_before']) > self.now for r in rows))
        self.assertTrue(all(r['limit_hits'] == 1 for r in rows))
        self.assertTrue(server.stale_lease(self.conn, rows[1], {}, {'lane_id': 'b', 'lease_token': 'tokenb'}))
        event = self.conn.execute('SELECT route,reason FROM collector_events ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(event), ('profile_page', 'please_wait_page'))

    def test_bound_identity_survives_handle_change_and_manual_priority(self):
        pid = db.upsert_person(self.conn, {'handle': 'old_name', 'ig_id': '101'})
        first = self.job('old_name', 1)
        accounts.coalesce_profile_jobs(self.conn, self.conn.execute('SELECT * FROM jobs WHERE id=?', (first,)).fetchone())
        self.assertEqual(self.conn.execute('SELECT target_ig_id FROM jobs WHERE id=?', (first,)).fetchone()[0], '101')
        db.upsert_person(self.conn, {'handle': 'new_name', 'ig_id': '101'})
        second = self.job('new_name', 100)
        self.conn.commit()
        server.api_read(self.conn, {}, {}, pid)
        pending = self.conn.execute("SELECT * FROM jobs WHERE state IN ('queued','leased')").fetchall()
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]['handle'], pending[0]['target_ig_id'], pending[0]['priority']),
                         ('new_name', '101', server.READ_PRIORITY))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (second,)).fetchone()[0], 'cancelled')
        with self.assertRaises(server.Bad):
            self.lease(pending[0]['id'], 'a')
            server.ext_profile(self.conn, {}, {'lane_id': 'a', 'job_id': pending[0]['id'], 'lease_token': 'tokena',
                               'profile': {'handle': 'new_name', 'ig_id': '202', 'bio': 'wrong account'}})

    def test_list_only_pick_never_reads_profile_groups_and_queries_are_indexed(self):
        first = self.job('same_target')
        self.job('same_target')
        traces = []
        self.conn.set_trace_callback(traces.append)
        self.assertIsNone(accounts.pick_job(self.conn, 'a', ['list'], self.now))
        self.conn.set_trace_callback(None)
        self.assertFalse(any('known_ig_id' in sql for sql in traces))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0], 2)
        plan = ' '.join(str(tuple(r)) for r in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE kind='profile' AND state IN ('queued','leased') "
            "AND target_ig_id=? UNION SELECT id FROM jobs WHERE kind='profile' AND state IN ('queued','leased') "
            "AND handle COLLATE NOCASE IN (?)", ('101', 'same_target')))
        self.assertIn('jobs_profile_target', plan)
        self.assertIn('jobs_profile_handle', plan)


if __name__ == '__main__':
    unittest.main()
