"""Public queue integration checks against a real temporary SQLite database."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db
import public_bios as p


class PipelineIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'pipeline.sqlite')
        p.init(self.conn)
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True})
        db.upsert_person(self.conn, {'handle': 'person', 'ig_id': '123'})
        self.conn.execute("INSERT INTO jobs(kind,handle) VALUES('profile','person')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def item(self, **values):
        return {'id': '123', 'username': 'person', 'biography': 'new',
                '_scrape': {'bioStatus': 'observed', 'source': 'test',
                            'observedAt': db.now(), 'cacheHit': False}, **values}

    def test_expired_lease_cannot_save_or_be_renewed(self):
        job = p.lease(self.conn, 'lane')
        expired = (db.utc_now() - timedelta(seconds=1)).isoformat()
        self.conn.execute('UPDATE jobs SET leased_until=?', (expired,))
        self.conn.commit()
        p.renew(self.conn, 'lane')
        self.assertEqual(self.conn.execute('SELECT leased_until FROM jobs').fetchone()[0], expired)
        self.assertFalse(p.import_item(self.conn, job, self.item()))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute('SELECT bio_at FROM people').fetchone()[0])

    def test_returned_identity_snapshot_blocks_reassigned_handle(self):
        job = p.lease(self.conn, 'lane')
        self.assertEqual(job['expected_ig_id'], '123')
        self.conn.execute("UPDATE people SET ig_id='456' WHERE handle='person'")
        self.conn.commit()
        self.assertFalse(p.import_item(self.conn, job, self.item(id='456')))
        self.assertIsNone(self.conn.execute('SELECT bio_at FROM people').fetchone()[0])

    def test_future_observation_is_not_saved_or_counted_fresh(self):
        job = p.lease(self.conn, 'lane')
        item = self.item()
        item['_scrape']['observedAt'] = (db.utc_now() + timedelta(seconds=30)).isoformat()
        self.assertFalse(p.import_item(self.conn, job, item))
        self.assertEqual(p.status(self.conn)['freshPerMinute'], 0)
        self.assertIsNone(self.conn.execute('SELECT bio_at FROM people').fetchone()[0])

    def test_equal_observation_is_reused_and_cannot_replace_bio(self):
        observed = db.now()
        self.conn.execute("UPDATE people SET bio='first',bio_at=?", (observed,))
        self.conn.commit()
        job = p.lease(self.conn, 'lane')
        item = self.item()
        item['_scrape']['observedAt'] = observed
        self.assertTrue(p.import_item(self.conn, job, item))
        self.assertEqual(self.conn.execute('SELECT bio FROM people').fetchone()[0], 'first')
        self.assertEqual(self.conn.execute('SELECT outcome FROM public_bio_results').fetchone()[0], 'reused')

    def test_permanent_error_finishes_without_five_retries(self):
        job = p.lease(self.conn, 'lane')
        self.assertFalse(p.import_item(self.conn, job, {'error': 'invalid_input'}))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'error')
        self.assertEqual(p.status(self.conn)['deferred'], 0)

    def test_saving_shorter_local_wait_preserves_durable_server_deadline(self):
        eg = SimpleNamespace(spec=SimpleNamespace(kind='proxy', name='lane', url='http://host:99'),
                             parked_until=1600, retired_until=0, strikes=1, rate_strikes=1,
                             limiter=SimpleNamespace(mode='adaptive', rpm=10))
        pool = SimpleNamespace(egresses=[eg])
        with patch.object(p.time, 'time', return_value=100000), patch.object(p.time, 'monotonic', return_value=1000):
            p.save_cooldowns(self.conn, pool)
            eg.parked_until = 1100
            p.save_cooldowns(self.conn, pool)
            eg.parked_until = 0
            p.restore_cooldowns(self.conn, pool)
        self.assertEqual(eg.parked_until, 1600)

    def test_parked_seeds_are_not_permanent_phantom_backlog(self):
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True, 'scope': 'missing'})
        self.conn.execute("INSERT INTO seeds(handle) VALUES('old~123')")
        self.conn.commit()
        self.assertEqual(p.outstanding(self.conn), 1)

    def test_cooling_queue_does_not_starve_missing_profiles(self):
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True, 'scope': 'missing'})
        self.conn.execute('INSERT INTO public_bio_retries VALUES(1,?,0)',
                          ((db.utc_now() + timedelta(hours=1)).isoformat(),))
        db.upsert_person(self.conn, {'handle': 'next_person'})
        self.conn.commit()
        self.assertEqual(p.plan(self.conn, capacity=1), 1)
        self.assertEqual(p.lease(self.conn, 'lane')['handle'], 'next_person')

    def test_explicit_job_has_priority_over_automatic_missing(self):
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True, 'scope': 'missing'})
        db.upsert_person(self.conn, {'handle': 'auto_person'})
        self.conn.commit()
        p.plan(self.conn, capacity=2)
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','explicit_person',100)")
        self.conn.commit()
        self.assertEqual(p.lease(self.conn, 'lane')['handle'], 'explicit_person')

    def test_failed_import_rolls_back_before_other_commits(self):
        job = p.lease(self.conn, 'lane')
        with patch.object(p.rules, 'sync', side_effect=RuntimeError('injected rule failure')):
            with self.assertRaises(RuntimeError):
                p.import_item(self.conn, job, self.item())
        db.set_setting(self.conn, 'heartbeat_test', True)
        self.conn.commit()
        self.assertIsNone(self.conn.execute('SELECT bio_at FROM people').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'leased')

    def test_seed_rename_import_exposes_dirty_members_to_another_process(self):
        self.conn.execute("INSERT INTO seeds(handle,ig_id) VALUES('person','123')")
        member = db.upsert_person(self.conn, {'handle': 'member', 'ig_id': '456'})
        db.add_edge(self.conn, 'person', member, 'following')
        self.conn.execute('DELETE FROM network_dirty')
        self.conn.commit()
        job = p.lease(self.conn, 'lane')
        self.assertTrue(p.import_item(self.conn, job, self.item(username='renamed')))
        other = db.connect(Path(self.tmp.name) / 'pipeline.sqlite')
        try:
            self.assertEqual(other.execute('SELECT handle FROM seeds').fetchone()[0], 'renamed')
            self.assertIn(member, [r[0] for r in other.execute('SELECT person_id FROM network_dirty')])
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
