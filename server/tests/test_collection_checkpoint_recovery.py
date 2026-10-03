"""File-backed collection recovery across database reconnects and import failures."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import server


class CollectionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'recovery.sqlite'
        self.conn = db.init(self.path)
        self.q = {'lane': ['recovery-lane'], 'kinds': ['list'], 'version': ['3.9.17']}
        db.queue_list(self.conn, 'seed', 'following')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def restart(self):
        self.conn.close()
        self.conn = db.init(self.path)

    def next(self):
        return server.ext_next(self.conn, self.q, {})['job']

    def body(self, job, handle, cursor=None):
        return dict(job_id=job['id'], lease_token=job['lease_token'], seed='seed',
                    direction='following', requested_cursor=job['cursor'],
                    users=[{'handle': handle}], next_cursor=cursor,
                    done=cursor is None, has_more=cursor is not None,
                    total=2, total_source='current_run')

    def checkpoint(self):
        return tuple(self.conn.execute(
            'SELECT state,cursor,received FROM lists WHERE seed=? AND direction=?',
            ('seed', 'following')).fetchone())

    def test_lost_ack_replays_after_restart_without_duplicate_evidence(self):
        saved = self.body(self.next(), 'alice', 'next-page')
        self.assertEqual(server.ext_list_page(self.conn, self.q, saved)['received'], 1)
        self.restart()  # server committed but the extension retained its outbox
        self.assertTrue(server.ext_list_page(self.conn, self.q, saved)['duplicate'])
        self.assertEqual(self.checkpoint(), ('running', 'next-page', 1))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pages').fetchone()[0], 1)
        resumed = self.next()
        self.assertEqual((resumed['cursor'], resumed['received']), ('next-page', 1))
        server.ext_list_page(self.conn, self.q, self.body(resumed, 'bob'))
        self.restart()
        self.assertEqual(self.checkpoint(), ('done', None, 2))
        self.assertTrue(db.list_run_complete(self.conn, resumed['id']))

    def test_interrupted_import_rolls_back_all_evidence_and_can_retry(self):
        job = self.next()
        page = self.body(job, 'alice', 'next-page')
        with patch.object(server, 'refresh_network', side_effect=RuntimeError('interrupted import')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted import'):
                server.ext_list_page(self.conn, self.q, page)
        self.assertFalse(self.conn.in_transaction)
        self.restart()
        self.assertEqual(self.checkpoint(), ('running', None, 0))
        for table in ('pages', 'list_page_requests', 'edge_observations', 'collector_events'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0, table)
        self.assertEqual(server.ext_list_page(self.conn, self.q, page)['received'], 1)
        server.ext_list_page(self.conn, self.q, self.body(self.next(), 'bob'))
        self.assertEqual(self.checkpoint(), ('done', None, 2))
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_uncommitted_request_releases_no_cursor_and_old_worker_cannot_write(self):
        first = self.next()
        self.restart()  # worker stopped before posting a result
        self.assertIsNone(self.next())  # the original request still owns its live lease
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01T00:00:00Z' WHERE id=?", (first['id'],))
        self.conn.commit()  # emulate the protective lease expiring, only in the temp DB
        resumed = self.next()
        self.assertEqual((resumed['id'], resumed['cursor'], resumed['received']),
                         (first['id'], None, 0))
        self.assertNotEqual(resumed['lease_token'], first['lease_token'])
        self.assertTrue(server.ext_list_page(self.conn, self.q,
                                             self.body(first, 'old-worker', 'wrong-page'))['stale'])
        self.assertEqual(self.checkpoint(), ('running', None, 0))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)
        server.ext_list_page(self.conn, self.q, self.body(resumed, 'alice', 'next-page'))
        server.ext_list_page(self.conn, self.q, self.body(self.next(), 'bob'))
        self.assertEqual(self.checkpoint(), ('done', None, 2))


if __name__ == '__main__':
    unittest.main()
