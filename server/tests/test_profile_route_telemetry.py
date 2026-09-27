"""Profile route observations use temporary databases and no Instagram requests."""
import tempfile
import unittest
from pathlib import Path

import db
import server


class ProfileRouteTelemetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'routes.sqlite'))
        self.addCleanup(self.conn.close)

    def ingest(self, **extra):
        body = dict(profile={'handle': 'alice', 'ig_id': '101', 'bio': 'Saved bio'},
                    route='profile_page', event_id='one', captured_at=db.now())
        body.update(extra)
        return server.ext_profile(self.conn, {'lane': ['lane-a']}, body)

    def test_successful_page_capture_records_route_once_across_outbox_retry(self):
        captured = db.now()
        self.ingest(captured_at=captured)
        self.ingest(captured_at=captured)
        rows = self.conn.execute('SELECT kind,outcome,reason,route,lane FROM collector_events').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('profile', 'profile', 'saved', 'profile_page', 'lane-a')])

    def test_stale_and_partial_captures_are_distinguished_from_saved_bios(self):
        self.ingest()
        self.ingest(event_id='old', captured_at='2026-01-01T00:00:00Z')
        self.ingest(event_id='partial', profile={'handle': 'alice', 'ig_id': '101', 'name': 'Alice'})
        self.assertEqual([r[0] for r in self.conn.execute('SELECT reason FROM collector_events ORDER BY id')],
                         ['saved', 'stale_capture', 'partial_profile'])

    def test_passive_capture_is_separate_and_invalid_routes_do_not_pollute_counts(self):
        self.ingest(route='passive')
        self.ingest(event_id='bad', route='made-up')
        self.assertEqual([r[0] for r in self.conn.execute('SELECT route FROM collector_events')], ['passive'])

    def test_legacy_outbox_without_route_still_imports(self):
        result = server.ext_profile(self.conn, {}, {'profile': {'handle': 'alice', 'bio': 'Legacy bio'}})
        self.assertIsInstance(result['id'], int)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collector_events').fetchone()[0], 0)
