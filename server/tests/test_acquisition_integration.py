"""Collector ingestion, preserved permits/leases and experimental dispatch."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acquisition
import acquisition_dispatch as dispatch
import db
import server


class AcquisitionIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(Path(self.temp.name) / 'test.sqlite')
        self.addCleanup(self.conn.close)
        self.q = {'lane': ['test-lane'], 'kinds': ['list'], 'version': ['3.9.31']}
        db.upsert_person(self.conn, {'handle': 'owner', 'ig_id': '100'})
        db.queue_list(self.conn, 'owner', 'following')
        self.conn.commit()

    def page(self, users, **changes):
        job = server.ext_next(self.conn, self.q, {})['job']
        body = dict(job_id=job['id'], lease_token=job['lease_token'], seed=job['seed'],
                    ig_id='100', direction='following', requested_cursor=job['cursor'],
                    users=users, done=True, total=len(users), total_source='current_run',
                    captured_at='2026-01-01T00:00:00+00:00', http_status=200)
        body.update(changes)
        return server.ext_list_page(self.conn, self.q, body), body

    def test_first_capture_uses_ids_source_date_and_replay_is_free(self):
        result, body = self.page([{'handle': 'peer', 'ig_id': '200'}])
        event = self.conn.execute('SELECT * FROM acquisition_events').fetchone()
        self.assertEqual(event['new_canonical_edges'], 1)
        self.assertEqual(event['source_kind'], 'collector_page')
        self.assertEqual(event['observed_at'], '2026-01-01T00:00:00.000000+00:00')
        self.assertGreater(event['ingested_at'], event['observed_at'])
        before = acquisition.stats(self.conn)
        replay = server.ext_list_page(self.conn, self.q, body)
        self.assertTrue(replay['duplicate'])
        self.assertEqual(acquisition.stats(self.conn), before)
        event_key = self.conn.execute('SELECT event_id FROM collector_events').fetchone()[0]
        self.assertEqual(event['event_id'], 'collector:' + event_key)

    def test_historical_inverse_is_known_before_page_is_inserted(self):
        owner = self.conn.execute("SELECT id FROM people WHERE ig_id='100'").fetchone()[0]
        db.upsert_person(self.conn, {'handle': 'peer', 'ig_id': '200'})
        self.conn.execute("INSERT INTO seeds(handle,ig_id) VALUES('peer','200')")
        db.add_edge(self.conn, 'peer', owner, 'followers', '2025-01-01T00:00:00+00:00')
        self.conn.commit()
        self.page([{'handle': 'peer', 'ig_id': '200'}])
        event = self.conn.execute('SELECT * FROM acquisition_events').fetchone()
        self.assertEqual((event['new_canonical_edges'], event['known_canonical_edges']), (0, 1))
        self.assertEqual(acquisition.stats(self.conn)['baseline_edges'], 1)

    def test_malformed_member_ids_do_not_break_original_list_ingestion(self):
        users = [{'handle': 'member' + str(i), 'ig_id': bad} for i, bad in enumerate(
            ({'bad': 1}, ['bad'], True, float('nan'), 'x' * 500, None))]
        self.page(users)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM list_members').fetchone()[0], 6)
        event = self.conn.execute('SELECT * FROM acquisition_events').fetchone()
        self.assertEqual(event['unresolved_members'], 6)
        self.assertEqual(event['new_canonical_edges'], 0)

    def test_missing_capture_date_stays_unknown_and_stale_lease_adds_nothing(self):
        _, body = self.page([{'handle': 'peer', 'ig_id': '200'}], captured_at=None,
                            lease_token='wrong')
        self.assertEqual(acquisition.stats(self.conn)['events'], 0)
        # Recover the lease directly without changing collection settings.
        row = self.conn.execute('SELECT * FROM jobs WHERE id=?', (body['job_id'],)).fetchone()
        body['lease_token'] = row['lease_token']
        server.ext_list_page(self.conn, self.q, body)
        self.assertIsNone(self.conn.execute('SELECT observed_at FROM acquisition_events').fetchone()[0])

    def test_policy_read_is_readonly_and_setting_preserves_warning_and_budget(self):
        db.set_setting(self.conn, 'instagram_request_attention', {'reason': 'warning'})
        before = dict(self.conn.execute('SELECT key,value FROM settings'))
        self.conn.commit()
        count = self.conn.total_changes
        report = server.api_acquisition(self.conn, {}, {})
        self.assertEqual(report['policy'], 'current')
        self.assertFalse(report['daily_throughput_verified'])
        self.assertEqual(self.conn.total_changes, count)
        server.api_acquisition(self.conn, {}, {'policy': 'observed_yield'})
        after = dict(self.conn.execute('SELECT key,value FROM settings'))
        self.assertEqual({k: v for k, v in after.items() if k != dispatch.POLICY_KEY}, before)
        with self.assertRaises(ValueError):
            server.api_acquisition(self.conn, {}, {'policy': 'unlimited'})

    def test_manual_retry_does_not_inherit_automatic_job_identity(self):
        job = self.conn.execute("SELECT * FROM jobs WHERE seed='owner'").fetchone()
        self.conn.execute("INSERT INTO collection_discovery(handle,state,at,reason,directions,job_id) "
                          "VALUES('owner','queued','2026-01-01','connections','[]',?)", (job['id'],))
        self.assertTrue(dispatch.is_automatic(self.conn, job))
        self.conn.execute("UPDATE jobs SET state='partial' WHERE id=?", (job['id'],))
        db.queue_list(self.conn, 'owner', 'following', refresh=True)
        manual = self.conn.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertNotEqual(job['id'], manual['id'])
        self.assertFalse(dispatch.is_automatic(self.conn, manual))

    def test_experimental_selection_never_invents_eligibility_or_overrides_priority(self):
        rows = [dict(id=i,kind='list',seed='list' + str(i),priority=0,owner='lane',
                     experiment_viewer_ig_id=None) for i in (1, 2)]
        dispatch.set_policy(self.conn, 'observed_yield')
        self.assertIn(dispatch.select(self.conn, rows), rows)
        rows[0]['priority'] = 100
        self.assertEqual(dispatch.select(self.conn, rows), rows[0])
        rows[0].update(priority=0, experiment_viewer_ig_id='999')
        self.assertEqual(dispatch.select(self.conn, rows), rows[0])
        self.assertIsNone(dispatch.select(self.conn, []))
