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
