"""Dig deeper reuses evidence and respects local-only and collection holds."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import db, server
import qual_api


class DeeperLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'test.sqlite'))
        db.set_setting(self.conn, 'qualify', False)
        db.set_setting(self.conn, 'local_laya', True)
        self.pid = db.upsert_person(self.conn, {'handle': 'brand', 'website': 'https://example.org'})
        self.conn.commit()
        self.call = qual_api.routes(server)[1][2]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_held_collection_reads_website_locally_without_hidden_jobs(self):
        db.set_setting(self.conn, 'paused', True)
        self.conn.commit()
        with patch.object(qual_api, 'fetch', return_value=('https://example.org', '<title>Store</title><p>Handmade shoes</p>')) as fetch, patch.object(qual_api, 'summarise') as model:
            first = self.call(self.conn, {}, {}, self.pid)
            second = self.call(self.conn, {}, {}, self.pid)
        self.assertEqual(fetch.call_count, 1)
        model.assert_not_called()
        self.assertEqual(first['state'], 'ready')
        self.assertEqual(first['bio']['state'], 'held')
        self.assertEqual(first['site']['summary'], 'Handmade shoes')
        self.assertEqual(first['site']['summary_source'], 'page_excerpt')
        self.assertIsNone(first['site']['error'])
        self.assertTrue(second['reused'])
        self.assertFalse(first['external_ai'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_fresh_bio_is_reused_and_no_site_has_no_additional_data(self):
        self.conn.execute('UPDATE people SET website=NULL,bio_at=? WHERE id=?', (db.now(), self.pid))
        self.conn.commit()
        result = self.call(self.conn, {}, {}, self.pid)
        self.assertEqual(result['state'], 'no_additional_data')
        self.assertEqual(result['bio']['state'], 'fresh')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_repeated_click_reuses_pending_bio_job(self):
        self.conn.execute('UPDATE people SET website=NULL WHERE id=?', (self.pid,))
        self.conn.commit()
        first = self.call(self.conn, {}, {}, self.pid)
        second = self.call(self.conn, {}, {}, self.pid)
        self.assertTrue(first['bio_queued'])
        self.assertFalse(second['bio_queued'])
        self.assertEqual(second['state'], 'pending')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_shared_safety_hold_prevents_new_profile_job_even_with_bios_enabled(self):
        self.conn.execute('UPDATE people SET website=NULL WHERE id=?', (self.pid,))
        self.conn.commit()
        with patch.object(server, 'workspace_cooldown', return_value=True):
            result = self.call(self.conn, {}, {}, self.pid)
        self.assertEqual(result['bio']['state'], 'held')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_site_errors_are_backed_off(self):
        db.set_setting(self.conn, 'paused', True)
        self.conn.commit()
        with patch.object(qual_api, 'fetch', side_effect=ValueError('timed out')) as fetch:
            first = self.call(self.conn, {}, {}, self.pid)
            second = self.call(self.conn, {}, {}, self.pid)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first['state'], 'error')
        self.assertTrue(second['reused'])

    def test_mode_enabling_during_fetch_cannot_turn_local_click_into_paid_call(self):
        def fetch(url):
            db.set_setting(self.conn, 'qualify', True)
            self.conn.commit()
            return url, '<title>Store</title>Shoes'
        with patch.object(qual_api, 'fetch', side_effect=fetch), patch.object(qual_api, 'summarise') as model:
            qual_api.read_site(self.conn, self.pid, 'https://example.org')
        model.assert_not_called()

    def test_concurrent_clicks_share_one_website_read(self):
        db.set_setting(self.conn, 'paused', True)
        self.conn.commit()
        qual_api._ensure(self.conn)
        self.conn.commit()
        entered, release = threading.Event(), threading.Event()
        results = []
        def fetch(url):
            entered.set()
            self.assertTrue(release.wait(5))
            return url, '<title>Store</title>Shoes'
        def run():
            conn = db.connect(str(Path(self.tmp.name) / 'test.sqlite'))
            try:
                results.append(self.call(conn, {}, {}, self.pid))
            finally:
                conn.close()
        with patch.object(qual_api, 'fetch', side_effect=fetch) as request:
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                second = self.call(self.conn, {}, {}, self.pid)
                self.assertEqual(second['state'], 'pending')
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(request.call_count, 1)
        self.assertEqual(results[0]['state'], 'ready')

    def test_local_workers_run_during_collection_hold_external_does_not(self):
        db.set_setting(self.conn, 'paused', True)
        self.conn.commit()
        self.assertTrue(server.laya_allowed(self.conn))
        with patch.object(server.qualify, 'llm_verdicts') as model, patch.object(server, 'research') as research:
            self.assertEqual(server.run_llm(self.conn, [], {}), 0)
        model.assert_not_called()
        research.assert_not_called()


if __name__ == '__main__':
    unittest.main()
