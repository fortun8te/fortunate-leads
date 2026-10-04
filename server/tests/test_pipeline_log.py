"""Durable evidence stays bounded/private and distinguishes observation from confirmation."""
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import control
import pipeline_log


class PipelineEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'pipeline.sqlite')
        self.now = datetime.now(timezone.utc)
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,role,last_seen) VALUES('turtles','2','lists',?)", (self.now.isoformat(),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_token_is_hashed_and_transaction_rollback_removes_event(self):
        pipeline_log.record(self.conn, 'request_acquired', 'turtles', 'list', token='secret', ig_id='2', now=self.now)
        row = dict(self.conn.execute('SELECT * FROM pipeline_events').fetchone())
        self.assertEqual(row['token_ref'], pipeline_log.token_ref('secret'))
        self.assertNotIn('secret', str(row))
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pipeline_events').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            pipeline_log.record(self.conn, 'request_acquired', reason='<html>private</html>')

    def page(self, at, viewer='2', outcome='page', reason=None):
        self.conn.execute('INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,reason,'
                          'returned_count,saved_entries,new_links,viewer_ig_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (at.isoformat(), 'turtles', 1, 'list', 'following', outcome, reason, 20, 19, 10, viewer))

    def test_summary_distinguishes_rows_saved_links_cadence_and_identity(self):
        self.page(self.now - timedelta(seconds=20))
        self.page(self.now - timedelta(seconds=4))
        self.page(self.now - timedelta(seconds=2), viewer='3')
        self.page(self.now, outcome='other', reason='<html>private biography</html>')
        report = pipeline_log.summary(self.conn, (self.now - timedelta(hours=1)).isoformat(), 'turtles', self.now, ig_id='2')
        self.assertEqual(report['directions']['following'], {'pages': 2, 'returned': 40, 'saved': 38, 'new_links': 20})
        self.assertEqual(report['cadence']['active_mean_seconds'], 16)
        self.assertEqual(report['cadence']['observed_pages_per_hour'], 2)
        self.assertEqual(report['failures'], {'other': 1})
        self.assertNotIn('biography', str(report))
        self.assertTrue(report['coverage']['verified_identity_only'])
        self.assertEqual(report['last_failure']['code'], 'other')

    def test_truncation_is_explicit_not_called_full_counts(self):
        for i in range(4):
            self.page(self.now - timedelta(seconds=i))
        with patch.object(pipeline_log, 'SUMMARY_ROWS', 2):
            report = pipeline_log.summary(self.conn, now=self.now)
        self.assertFalse(report['coverage']['complete'])
        self.assertEqual(report['coverage']['scope'], 'latest_rows_in_window')
        self.assertEqual(report['directions']['following']['pages'], 2)

    def test_input_boundaries(self):
        for since in ('bad', '2026-10-04', (self.now - timedelta(days=8)).isoformat()):
            with self.assertRaises(ValueError):
                pipeline_log.summary(self.conn, since, now=self.now)
        with self.assertRaises(ValueError):
            pipeline_log.summary(self.conn, lane='private/url', now=self.now)

    def test_bounded_lifecycle_retention(self):
        with patch.object(pipeline_log, 'MAX_ROWS', 128):
            for _ in range(384):
                pipeline_log.record(self.conn, 'request_acquired', 'turtles', 'list', ig_id='2', now=self.now)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pipeline_events').fetchone()[0], 128)

    def warning(self, at=None, lane='dih', identity='3'):
        return {'lane': lane, 'ig_id': identity, 'at': (at or self.now - timedelta(days=1)).isoformat(), 'message': 'Old warning'}

    def test_old_excluded_warning_does_not_mask_actual_request_stop(self):
        warning = self.warning()
        request = {'lane': 'turtles', 'at': self.now.isoformat(), 'message': 'Request completion unknown'}
        db.set_setting(self.conn, 'instagram_scraping_warning', warning)
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {'turtles': '2'}, 'at': (self.now - timedelta(hours=1)).isoformat()})
        db.set_setting(self.conn, 'instagram_request_attention', request)
        db.set_setting(self.conn, 'paused_lists', True)
        state = control.snapshot(self.conn)
        self.assertEqual(state['instagram_request_attention'], request)
        self.assertEqual(state['instagram_scraping_warning'], warning)
        self.assertEqual(state['stages'][0]['now'], request['message'])
        self.assertEqual([(r['code'], r['blocking']) for r in state['collection_blockers']], [('request_unconfirmed', True), ('scraping_warning', False)])
        self.assertEqual(db.get_setting(self.conn, 'instagram_scraping_warning'), warning)

    def test_new_warning_or_stale_identity_cannot_be_hidden_by_isolation(self):
        for warning in (self.warning(at=self.now), self.warning(identity='2')):
            db.set_setting(self.conn, 'instagram_scraping_warning', warning)
            db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {'turtles': '2'}, 'at': (self.now - timedelta(hours=1)).isoformat()})
            db.set_setting(self.conn, 'instagram_request_attention', {'lane': 'turtles', 'message': 'Unknown request'})
            self.assertEqual(control.collection_attention(self.conn), warning)
            self.assertTrue(control.snapshot(self.conn)['collection_blockers'][-1]['blocking'])
        db.set_setting(self.conn, 'instagram_scraping_warning', self.warning())
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {'turtles': '999'}, 'at': self.now.isoformat()})
        self.assertTrue(control.snapshot(self.conn)['collection_blockers'][-1]['blocking'])

    def test_pending_warning_only_is_not_mislabeled_request_failure(self):
        warning = self.warning()
        warning['pending'] = {'other': self.warning(at=self.now, lane='other', identity='4')}
        db.set_setting(self.conn, 'instagram_scraping_warning', warning)
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {'turtles': '2'}, 'at': (self.now - timedelta(hours=1)).isoformat()})
        state = control.snapshot(self.conn)
        self.assertEqual(state['instagram_request_attention'], warning['pending']['other'])
        self.assertEqual([r['code'] for r in state['collection_blockers']], ['scraping_warning', 'scraping_warning'])
        self.assertTrue(state['collection_blockers'][-1]['blocking'])

    def setup_review(self):
        marker = {'lane': 'turtles', 'ig_id': '2', 'at': self.now.isoformat(), 'token_ref': 'a' * 16,
                  'kind': 'list', 'message': 'Unknown request'}
        db.set_setting(self.conn, 'instagram_request_attention', marker)
        db.set_setting(self.conn, 'paused_lists', True)
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        return {'action': 'review_unconfirmed_request', 'lane': 'turtles', 'ig_id': '2',
                'attention_at': marker['at'], 'reviewed': True, 'checked_account_tab': True}

    def test_explicit_operator_review_preserves_pauses_and_progress(self):
        body = self.setup_review()
        db.set_setting(self.conn, 'cooldown', (self.now + timedelta(hours=1)).isoformat())
        self.conn.execute("UPDATE accounts SET list_endpoint_until=? WHERE lane_id='turtles'", ((self.now + timedelta(hours=1)).isoformat(),))
        control.apply(self.conn, body)
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_attention'))
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
        self.assertIsNotNone(db.get_setting(self.conn, 'cooldown'))
        self.assertIsNotNone(self.conn.execute("SELECT list_endpoint_until FROM accounts WHERE lane_id='turtles'").fetchone()[0])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_review')['outcome'], 'operator_confirmed_stopped')
        self.assertEqual(self.conn.execute('SELECT code FROM pipeline_events').fetchone()[0], 'request_operator_reviewed')

    def test_review_rejects_stale_cas_or_security_or_active_request(self):
        for change in ('stale', 'identity', 'main', 'hold', 'active', 'unchecked', 'warning'):
            self.conn.execute("UPDATE accounts SET ig_id='2',is_main=0,hold=NULL WHERE lane_id='turtles'")
            db.set_setting(self.conn, 'instagram_request_gate', None)
            db.set_setting(self.conn, 'instagram_scraping_warning', None)
            body = self.setup_review()
            if change == 'stale':
                body['attention_at'] = (self.now - timedelta(seconds=1)).isoformat()
            elif change == 'identity':
                self.conn.execute("UPDATE accounts SET ig_id='3' WHERE lane_id='turtles'")
            elif change == 'main':
                self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='turtles'")
            elif change == 'hold':
                self.conn.execute("UPDATE accounts SET hold='challenge' WHERE lane_id='turtles'")
            elif change == 'active':
                db.set_setting(self.conn, 'instagram_request_gate', {'active': {'lane': 'turtles', 'ig_id': '2', 'token': 'secret', 'until': self.now.timestamp() + 50}})
            elif change == 'unchecked':
                body['checked_account_tab'] = False
            else:
                db.set_setting(self.conn, 'instagram_scraping_warning', self.warning(at=self.now))
            self.conn.commit()
            with self.assertRaises(ValueError, msg=change):
                control.apply(self.conn, body)
            self.assertIsNotNone(db.get_setting(self.conn, 'instagram_request_attention'))
            self.assertTrue(db.get_setting(self.conn, 'paused_lists'))

    def test_uninstrumented_or_pruned_window_does_not_claim_complete_lifecycle(self):
        report = pipeline_log.summary(self.conn, now=self.now)
        self.assertFalse(report['coverage']['complete'])
        self.assertFalse(report['coverage']['lifecycle_complete'])
        self.assertTrue(report['coverage']['collector_complete'])
        pipeline_log.record(self.conn, 'request_acquired', 'turtles', 'list', ig_id='2', now=self.now)
        report = pipeline_log.summary(self.conn, now=self.now)
        self.assertEqual(report['coverage']['lifecycle_available_since'], self.now.isoformat())
        self.assertFalse(report['coverage']['lifecycle_complete'])

    def test_legacy_review_requires_selection_bound_before_stop_and_no_live_gate(self):
        body = self.setup_review()
        marker = db.get_setting(self.conn, 'instagram_request_attention')
        marker.pop('ig_id')
        db.set_setting(self.conn, 'instagram_request_attention', marker)
        self.conn.commit()
        with self.assertRaises(ValueError):
            control.apply(self.conn, body)
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {'turtles': '2'}, 'at': (self.now - timedelta(hours=1)).isoformat()})
        self.conn.commit()
        control.apply(self.conn, body)
        self.assertTrue(db.get_setting(self.conn, 'instagram_request_review')['legacy_identity_bound'])
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))

    def test_expired_gate_without_persisted_attention_can_be_explicitly_reviewed(self):
        active = {'lane': 'turtles', 'ig_id': '2', 'kind': 'list', 'token': 'secret',
                  'until': self.now.timestamp() - 1}
        db.set_setting(self.conn, 'instagram_request_gate', {'active': active})
        db.set_setting(self.conn, 'paused_lists', True)
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        marker = control.snapshot(self.conn)['instagram_request_attention']
        self.assertEqual(marker['ig_id'], '2')
        self.assertEqual(marker['token_ref'], pipeline_log.token_ref('secret'))
        self.assertEqual(marker['at'], control.snapshot(self.conn)['instagram_request_attention']['at'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_attention'))
        body = {'action': 'review_unconfirmed_request', 'lane': 'turtles', 'ig_id': '2',
                'attention_at': marker['at'], 'reviewed': True, 'checked_account_tab': True}
        control.apply(self.conn, body)
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_review')['token_ref'], marker['token_ref'])
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))

    def test_invalid_gate_expiry_never_synthesizes_reviewable_marker(self):
        for until in (float('nan'), float('inf'), -1, True, 10 ** 500):
            request = {'lane': 'turtles', 'ig_id': '2', 'kind': 'list', 'token': 'secret', 'until': until}
            self.assertIsNone(control.expired_request_attention(request, self.now))
