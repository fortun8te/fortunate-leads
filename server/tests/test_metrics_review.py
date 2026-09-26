"""Regression checks for public-bio speed and worker liveness metrics."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import public_bios


class PublicBioMetricsReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'test.sqlite')
        public_bios.init(self.conn)
        db.set_setting(self.conn, 'public_bios', {**public_bios.DEFAULTS, 'on': True})
        self.now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def status(self):
        with patch.object(public_bios.db, 'utc_now', return_value=self.now), \
             patch.object(public_bios.time, 'time', return_value=self.now.timestamp()):
            return public_bios.status(self.conn)

    def test_older_observation_completes_job_without_counting_a_fresh_save(self):
        newer_at = self.now + timedelta(minutes=1)
        older_at = self.now - timedelta(minutes=1)
        db.upsert_person(self.conn, {'handle': 'person', 'ig_id': '123',
                                     'bio': 'newer', 'bio_at': newer_at.isoformat()}, newer_at.isoformat())
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','person',?)",
                          (self.now.isoformat(),))
        self.conn.commit()
        with patch.object(public_bios.db, 'now', return_value=self.now.isoformat()), \
             patch.object(public_bios.db, 'utc_now', return_value=self.now):
            job = public_bios.lease(self.conn, 'test')
            completed = public_bios.import_item(self.conn, job, {
                'id': '123', 'username': 'person', 'biography': 'older',
                '_scrape': {'bioStatus': 'observed', 'source': 'test',
                            'observedAt': older_at.isoformat(), 'cacheHit': False}})

        self.assertTrue(completed)
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE handle='person'").fetchone()[0], 'done')
        self.assertEqual(self.conn.execute("SELECT bio FROM people WHERE handle='person'").fetchone()[0], 'newer')
        self.assertEqual(self.conn.execute('SELECT outcome FROM public_bio_results').fetchone()[0], 'reused')
        result = self.status()
        self.assertEqual(result['successes'], 1)
        self.assertEqual((result['freshPerMinute'], result['freshFiveMinutes']), (0, 0))

    def test_future_result_and_heartbeat_do_not_count_as_current_activity(self):
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','person',?)",
                          (self.now.isoformat(),))
        self.conn.execute('''INSERT INTO public_bio_results
            (job_id,handle,at,outcome,cache_hit) VALUES(1,'person',?,'success',0)''',
                          ((self.now + timedelta(hours=1)).isoformat(),))
        db.set_setting(self.conn, 'public_bios_heartbeat', {
            'epoch': self.now.timestamp() + 3600, 'healthyEgresses': 1})
        self.conn.commit()
        result = self.status()
        self.assertEqual((result['freshPerMinute'], result['freshFiveMinutes']), (0, 0))
        self.assertFalse(result['running'])
        self.assertIsNone(result['etaSeconds'])

        db.set_setting(self.conn, 'public_bios_heartbeat', {
            'epoch': self.now.timestamp(), 'healthyEgresses': 1})
        self.conn.execute('''INSERT INTO public_bio_results
            (job_id,handle,at,outcome,cache_hit) VALUES(1,'person',?,'success',0)''',
                          ((self.now - timedelta(seconds=30)).isoformat(),))
        self.conn.commit()
        result = self.status()
        self.assertTrue(result['running'])
        self.assertEqual((result['freshPerMinute'], result['freshFiveMinutes']), (1, 1))


if __name__ == '__main__':
    unittest.main()
