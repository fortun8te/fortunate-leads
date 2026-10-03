"""Operator scope changes preserve saved lists and safety state."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base
import db


class CollectionScopeTests(Base):
    def change(self, scope):
        return self.call('/api/control', {'action': 'set_list_scope', 'scope': scope})

    def stop(self):
        self.assertEqual(self.call('/api/control', {'action': 'pause', 'stage': 'collection'})[0], 200)

    def test_scope_roundtrip_preserves_warning_cursors_and_processing(self):
        self.stop()
        warning = {'lane': 'warned', 'ig_id': '100', 'message': 'Review warning', 'at': 'original'}
        db.set_setting(self.conn, 'instagram_scraping_warning', warning)
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list','seed','followers','queued')")
        self.conn.execute("INSERT INTO lists(seed,direction,state,cursor) VALUES('seed','followers','partial','saved-cursor')")
        self.conn.commit()
        self.assertEqual(self.call('/api/control')[1]['collection_scope'], 'both')
        for scope in ('following', 'both'):
            status, result = self.change(scope)
            self.assertEqual(status, 200)
            self.assertEqual(result['collection_scope'], scope)
            self.assertTrue(result['stop_acknowledged'])
            self.assertEqual(db.get_setting(self.conn, 'instagram_scraping_warning'), warning)
            self.assertFalse(db.get_setting(self.conn, 'qualify'))
            self.assertEqual(self.conn.execute("SELECT cursor,state FROM lists WHERE seed='seed'").fetchone()[:], ('saved-cursor', 'partial'))

    def test_running_active_and_unconfirmed_request_refuse_scope_change(self):
        self.assertEqual(self.change('following')[0], 400)
        self.stop()
        for until in (time.time() + 100, time.time() - 100):
            db.set_setting(self.conn, 'instagram_request_gate', {'active': {'lane': 'alt', 'kind': 'list', 'until': until}})
            self.conn.commit()
            self.assertEqual(self.change('following')[0], 400)
            self.assertEqual(self.call('/api/control')[1]['collection_scope'], 'both')
        db.set_setting(self.conn, 'instagram_request_gate', {'active': None})
        self.conn.commit()
        self.assertEqual(self.change('following')[0], 200)

    def test_pending_start_and_invalid_scope_refuse_change(self):
        self.stop()
        for state in ('opening', 'waiting'):
            db.set_setting(self.conn, 'collection_startup', {'state': state})
            self.conn.commit()
            self.assertEqual(self.change('following')[0], 400)
        db.set_setting(self.conn, 'collection_startup', None)
        self.conn.commit()
        for scope in ('followers', '', None, True, []):
            self.assertEqual(self.change(scope)[0], 400)
        self.assertEqual(self.change('following')[0], 200)

    def test_following_queue_and_remaining_work_exclude_saved_follower_backlog(self):
        self.call('/api/scraper/seeds', {'handles':['seed'], 'directions':['followers','following']})
        self.conn.execute("UPDATE lists SET total=1000 WHERE direction='followers'")
        self.conn.execute("UPDATE lists SET total=20 WHERE direction='following'")
        self.conn.commit()
        self.stop()
        self.assertEqual(self.change('following')[0], 200)
        controls = self.call('/api/control')[1]
        self.assertEqual(next(stage for stage in controls['stages'] if stage['id']=='lists')['queue'], 1)
        report = self.call('/api/scraper')[1]
        self.assertEqual(report['queue']['list'], 1)
        self.assertEqual(report['collection']['pending'], 1)
        self.assertEqual(report['collection']['total'], 1)
        self.assertEqual(report['progress']['lists']['left'], 20)
        self.assertEqual(len(report['lists']), 2)
        self.assertEqual(report['coverage']['lists']['total_lists'], 2)
        self.assertEqual(self.change('both')[0], 200)
        report = self.call('/api/scraper')[1]
        self.assertEqual(report['queue']['list'], 2)
        self.assertEqual(report['collection']['total'], 2)
        self.assertEqual(report['progress']['lists']['left'], 1020)

    def test_following_rate_ignores_follower_pages_and_partial_backlog(self):
        self.call('/api/scraper/seeds', {'handles':['seed'], 'directions':['followers','following']})
        follower = self.conn.execute("SELECT id FROM jobs WHERE direction='followers'").fetchone()[0]
        self.conn.execute("INSERT INTO pages(job_id,cursor,at,users) VALUES(?,'page',?,50000)", (follower, db.now()))
        self.conn.execute("UPDATE lists SET state='partial',received=10,total=1000 WHERE direction='followers'")
        self.conn.execute("UPDATE lists SET total=20 WHERE direction='following'")
        self.conn.commit()
        self.stop()
        self.assertEqual(self.change('following')[0], 200)
        report = self.call('/api/scraper')[1]
        progress = report['progress']['lists']
        self.assertIsNone(progress['per_hour'])
        self.assertIsNone(progress['per_minute'])
        self.assertIsNone(progress['eta_h'])
        self.assertEqual(progress['incomplete_lists'], 0)
        self.assertEqual(report['collection']['partial'], 0)
        self.assertEqual(self.change('both')[0], 200)
        report = self.call('/api/scraper')[1]
        progress = report['progress']['lists']
        self.assertGreater(progress['per_hour'], 0)
        self.assertEqual(progress['per_minute'], 50000)
        self.assertEqual(progress['incomplete_lists'], 1)
        self.assertEqual(report['collection']['partial'], 1)
