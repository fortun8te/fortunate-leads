"""Retired optional APIs and the single canonical Client relationship."""
from test_server import Base, db


class SimplifiedLeadApi(Base):
    def setUp(self):
        super().setUp()
        self.pid = db.upsert_person(self.conn, {'handle': 'client_case'})
        self.conn.execute("INSERT INTO saved_views(name,query) VALUES('Preserved view','tags=Friend')")
        self.conn.commit()
        self.tags = f'/api/person/{self.pid}/tags'
        self.mark = f'/api/person/{self.pid}/mark'

    def test_retired_routes_are_absent_and_saved_data_is_preserved(self):
        for path, body in [('/api/people/bulk', {'ids': [self.pid], 'status': 'no'}),
                           ('/api/leads/export', {'query': ''}), ('/api/views', None),
                           ('/api/views', {'name': 'new', 'query': ''}), ('/api/views/1/delete', {})]:
            self.assertEqual(self.call(path, body)[0], 404, path)
        self.assertEqual(self.conn.execute('SELECT name,query FROM saved_views').fetchone()[:],
                         ('Preserved view', 'tags=Friend'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM marks').fetchone()[0], 0)

    def test_new_client_label_becomes_relationship_without_duplicate_and_keeps_note(self):
        self.call(self.mark, {'note': 'We worked together.'})
        code, out = self.call(self.tags, {'add': ['Friend', ' cLiEnT ']})
        self.assertEqual(code, 200)
        self.assertEqual(out['converted_to_status'], 'client')
        person = self.call(f'/api/person/{self.pid}')[1]
        self.assertEqual(person['status'], 'client')
        self.assertTrue(person['reachable'])
        self.assertEqual(person['note'], 'We worked together.')
        self.assertEqual([r[0] for r in self.conn.execute('SELECT tag FROM tags')], ['Friend'])
        self.assertEqual(self.call(self.tags, {'add': ['Client']})[0], 200)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM activity WHERE kind='status'").fetchone()[0], 1)

    def test_explicit_client_submission_replaces_prior_status_and_preserves_history(self):
        self.conn.execute("INSERT INTO tags VALUES(?,'Client','signal','manual')", (self.pid,))
        self.conn.commit()
        for status in ('interested', 'contacted', 'talking', 'no'):
            self.call(self.mark, {'status': status, 'note': 'Keep my note'})
            code, out = self.call(self.tags, {'add': ['New', 'Client']})
            self.assertEqual(code, 200)
            self.assertEqual(out['converted_to_status'], 'client')
            self.assertEqual(self.conn.execute('SELECT status,note FROM marks').fetchone()[:], ('client', 'Keep my note'))
            self.assertEqual({r[0] for r in self.conn.execute('SELECT tag FROM tags')}, {'Client', 'New'})
            latest = self.conn.execute("SELECT before_value,after_value FROM activity WHERE kind='status' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(latest[:], ('"' + status + '"', '"client"'))

    def test_legacy_client_label_survives_and_can_be_removed(self):
        self.conn.execute("INSERT INTO tags VALUES(?,'Client','signal','manual')", (self.pid,))
        self.conn.commit()
        self.assertEqual(self.call(f'/api/person/{self.pid}')[1]['owner_status'], 'client')
        self.call(self.tags, {'add': ['Friend']})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE tag='Client'").fetchone()[0], 1)
        self.call(self.tags, {'remove': ['Client']})
        self.assertIsNone(self.call(f'/api/person/{self.pid}')[1]['owner_status'])
        self.assertEqual(self.call('/api/tags/rename', {'from': 'Friend', 'to': 'Client'})[0], 400)
