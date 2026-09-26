"""Manual record HTTP regressions; all writes use Base's temporary database."""
from concurrent.futures import ThreadPoolExecutor
from test_server import Base, db


class ManualRecordRevisionTest(Base):
    def setUp(self):
        super().setUp()
        self.pid = db.upsert_person(self.conn, {'handle': 'manual_case', 'bio': 'designer'})
        self.conn.commit()
        self.path = f'/api/person/{self.pid}/mark'

    def test_stale_note_is_rejected_and_explicit_retry_succeeds(self):
        initial = self.call(f'/api/person/{self.pid}')[1]['mark_rev']
        code, first = self.call(self.path, {'note': 'saved elsewhere', 'if_match': initial})
        self.assertEqual(code, 200)
        code, conflict = self.call(self.path, {'note': 'my draft', 'if_match': initial})
        self.assertEqual(code, 409)
        self.assertEqual(conflict['note'], 'saved elsewhere')
        self.assertEqual(conflict['current']['mark_rev'], first['mark_rev'])
        self.assertEqual(self.call(f'/api/person/{self.pid}')[1]['note'], 'saved elsewhere')
        self.assertEqual(self.call(self.path, {'note': 'my draft', 'mark_rev': conflict['mark_rev']})[0], 200)

    def test_two_simultaneous_saves_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda note: self.call(self.path, {'note': note, 'if_match': ''}), ['one', 'two']))
        self.assertEqual(sorted(code for code, body in results), [200, 409])

    def test_partial_legacy_save_preserves_other_field_and_bulk_changes_revision(self):
        _, first = self.call(self.path, {'note': 'keep note', 'status': 'client'})
        result = self.call('/api/people/bulk', {'ids': [self.pid, self.pid, 99999], 'status': 'talking'})[1]
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['updated_ids'], [self.pid])
        self.assertEqual(result['missing_ids'], [99999])
        self.assertEqual(self.call(self.path, {'note': 'stale', 'mark_rev': first['mark_rev']})[0], 409)
        self.assertEqual(self.call(self.path, {'status': None})[1]['note'], 'keep note')

    def test_bad_revision_and_tag_shapes_do_not_mutate(self):
        for revision in (None, 0, [], {}):
            self.assertEqual(self.call(self.path, {'note': 'bad', 'mark_rev': revision})[0], 400)
        for path, base in ((f'/api/person/{self.pid}/tags', {}), ('/api/people/bulk', {'ids': [self.pid]})):
            self.assertEqual(self.call(path, dict(base, add='tag'))[0], 400)
        self.assertIsNone(self.call(f'/api/person/{self.pid}')[1]['note'])

    def test_remove_is_manual_only_and_restores_matching_rule(self):
        self.conn.execute("INSERT INTO tag_rules(tag, grp, field, match) VALUES('designer','role','bio','designer')")
        self.conn.execute("INSERT INTO tags VALUES(?, 'designer', 'role', 'manual')", (self.pid,))
        self.conn.execute("INSERT INTO tags VALUES(?, 'automatic', 'signal', 'auto')", (self.pid,))
        self.conn.commit()
        self.assertEqual(self.call(f'/api/person/{self.pid}/tags', {'remove': ['designer', 'automatic']})[0], 200)
        self.assertEqual(dict(self.conn.execute('SELECT tag, source FROM tags WHERE person_id=?', (self.pid,))),
                         {'designer': 'rule', 'automatic': 'auto'})
