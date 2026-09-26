"""HTTP tests run with the server suite's qualification stub, after pure qualifier tests."""
from unittest.mock import patch

from test_server import Base
import db
import server


class ConnectionAPITests(Base):
    def test_search_explore_validation_and_origin(self):
        self.call('/api/ext/list-page', {'seed': 'start', 'ig_id': '1', 'direction': 'following',
                                       'users': [{'handle': 'target', 'ig_id': '2'}], 'done': True, 'total': 1})
        status, search = self.call('/api/connections/search?q=tar')
        self.assertEqual(status, 200)
        self.assertEqual(search['accounts'][0]['id'], 'ig:2')
        status, data = self.call('/api/connections?from=start&to=target&mode=following')
        self.assertEqual(status, 200)
        self.assertEqual(data['paths'][0]['nodes'], ['ig:1', 'ig:2'])
        self.assertEqual(data['edges'][0]['evidence'][0]['seed_ig_id'], '1')
        self.assertEqual(self.call('/api/connections?from=absent&to=target')[0], 404)
        for path in ('/api/connections?from=start&to=target&mode=bad', '/api/connections',
                     '/api/connections/search?limit=0', '/api/connections/search?limit=51',
                     '/api/connections?from=start&to=target&limit=invalid'):
            self.assertEqual(self.call(path)[0], 400, path)
        self.assertEqual(self.call('/api/connections?from=start&to=target', origin='https://other.test')[0], 403)

    def test_page_retry_does_not_make_follow_evidence_newer(self):
        db.queue_list(self.conn, 'source', 'following')
        self.conn.commit()
        job = self.conn.execute("SELECT id FROM jobs WHERE seed='source'").fetchone()[0]
        body = {'job_id': job, 'seed': 'source', 'ig_id': '1', 'direction': 'following',
                'users': [{'handle': 'target', 'ig_id': '2'}], 'done': True, 'total': 1}
        with patch.object(db, 'now', return_value='2020-01-01T00:00:00+00:00'):
            self.assertEqual(self.call('/api/ext/list-page', body)[0], 200)
        with patch.object(db, 'now', return_value='2026-09-26T00:00:00+00:00'):
            self.assertTrue(self.call('/api/ext/list-page', body)[1]['duplicate'])
        evidence = self.conn.execute('SELECT first_seen,last_seen FROM edge_observations').fetchone()
        self.assertEqual(tuple(evidence), ('2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'))

    def test_overview_discloses_actual_display_limit(self):
        for i in range(15):
            pid = db.upsert_person(self.conn, {'handle': f'person{i}', 'ig_id': str(i + 100)})
            db.add_edge(self.conn, 'source', pid, 'followers', seed_ig_id='1')
        self.conn.commit()
        data = self.call('/api/map?scope=all&limit=10')[1]
        self.assertEqual(data['display'], {'shown_people': 10, 'matched_people': 15, 'limit': 10, 'truncated': True})
        self.assertIn('not direct follows', data['semantics']['seed_links'])
