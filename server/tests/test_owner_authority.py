"""Owner facts take precedence without rewriting historical research or manual labels."""
import tempfile
import unittest
from pathlib import Path

from test_server import db, server
import owner
import qualify


class OwnerAuthority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'test.sqlite'))
        server.deepscout.ensure(self.conn)
        self.pid = db.upsert_person(self.conn, {'handle': 'known_client', 'bio': 'Personal account', 'followers': 900000})
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self):
        return server.with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (self.pid,)).fetchone()))

    def test_manual_client_fact_hides_rejection_and_preserves_storage(self):
        self.conn.executemany('INSERT INTO tags VALUES(?,?,?,?)', [
            (self.pid, 'Client', 'signal', 'manual'), (self.pid, 'Not reachable', 'scout', 'auto'),
            (self.pid, 'Scout: No', 'scout', 'auto'), (self.pid, 'Close friend', 'signal', 'manual')])
        server.requalify(self.conn, self.person(), None)
        # An old saved auto label is filtered immediately, before the next background pass.
        self.conn.execute("INSERT OR IGNORE INTO tags VALUES(?,'Not reachable','scout','auto')", (self.pid,))
        result = server.api_person(self.conn, {}, {}, self.pid)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE person_id=? AND tag='Not reachable'", (self.pid,)).fetchone()[0], 1)
        self.assertEqual(result['owner_status'], 'client')
        self.assertTrue(result['reachable'])
        self.assertIn('Existing client', result['reason'])
        names = {t['tag'] for t in result['tags']}
        self.assertNotIn('Not reachable', names)
        self.assertNotIn('Scout: No', names)
        self.assertIn('Client', names)
        self.assertIn('Close friend', names)
        self.assertGreaterEqual(qualify.prefilter(self.person(), [], laya_fit=0), 65)
        server.set_status(self.conn, [self.pid], status='client')
        self.assertIn('Client', {t['tag'] for t in server.api_person(self.conn, {}, {}, self.pid)['tags']})
        self.assertEqual(self.conn.execute("SELECT source FROM tags WHERE person_id=? AND tag='Client'", (self.pid,)).fetchone()[0], 'manual')

    def test_explicit_no_preserves_client_history_and_controls_business_fit(self):
        self.conn.execute("INSERT INTO tags VALUES(?,'Client','signal','manual')", (self.pid,))
        server.set_status(self.conn, [self.pid], status='no')
        server.requalify(self.conn, self.person(), None)
        result = server.api_person(self.conn, {}, {}, self.pid)
        self.assertEqual(result['owner_status'], 'no')
        self.assertIsNone(result['owner_conflict'])
        self.assertIn('Client', {t['tag'] for t in result['tags']})
        self.assertEqual(result['score'], 0)
        self.assertEqual(qualify.prefilter(self.person(), [], laya_fit=100), 0)

    def test_scout_cannot_restore_rejection_over_owner_relationship(self):
        server.requalify(self.conn, self.person(), None)
        server.deepscout.apply(self.conn, self.person(), {'verdict': 'no', 'reachable': False,
            'summary': 'Could not find a route.', 'tags': [], 'sources': []}, verification=(True, None))
        server.set_status(self.conn, [self.pid], status='client')
        server.requalify(self.conn, self.person(), None)
        result = server.api_person(self.conn, {}, {}, self.pid)
        self.assertTrue(result['scout']['overridden_by_owner'])
        self.assertFalse(result['scout']['reachable'])  # original evidence remains history
        self.assertTrue(result['reachable'])
        self.assertNotEqual(result['verdict']['model'], 'leadscout')
        self.assertNotIn('Not reachable', {t['tag'] for t in result['tags']})
        server.set_status(self.conn, [self.pid], status=None, relationships=[])
        server.requalify(self.conn, self.person(), None)
        self.assertIn('Not reachable', {t['tag'] for t in server.api_person(self.conn, {}, {}, self.pid)['tags']})

    def test_full_story_is_saved_hashed_and_source_labelled(self):
        note = 'We met through a friend. ' * 35 + 'He is founder of a skincare brand.'
        server.set_status(self.conn, [self.pid], note=note)
        person = self.person()
        self.assertEqual(person['note'], note)
        self.assertNotIn('He is founder of a skincare brand.', '\n'.join(qualify.owner_lines(person)))
        before = qualify.input_hash(person)
        self.assertEqual(before, qualify.input_hash(dict(person, note=note + ' We talked yesterday.')))
        verdict = qualify._verdict({'role': 'buyer', 'fit': 80, 'evidence': ['founder of a skincare brand'],
            'decision_maker': True}, person, [], 'offline-test', 'test')
        self.assertIsNone(verdict)  # Private note cannot support an external verdict.

    def test_local_laya_cannot_be_bypassed_by_omitting_flag(self):
        db.set_setting(self.conn, 'qualify', False)
        db.set_setting(self.conn, 'local_laya', True)
        self.conn.commit()
        with self.assertRaises(server.Bad):
            server.api_qualify(self.conn, {}, {'on': True})
        self.assertFalse(db.get_setting(self.conn, 'qualify'))
        self.assertTrue(db.get_setting(self.conn, 'local_laya'))

    def test_automatic_only_conflicts_never_erase_manual_corrections(self):
        p = {'status': 'client'}
        tags = [{'tag': 'Not reachable', 'source': 'manual'}, {'tag': 'Scout: No', 'source': 'auto'}]
        self.assertEqual(owner.visible_tags(p, tags), tags[:1])
        self.assertIn('conflicts', owner.owner_conflict(dict(p, manual_tags=['Not reachable'])))

    def test_filters_and_facets_use_current_owner_facts_preserving_saved_tags(self):
        other = db.upsert_person(self.conn, {'handle': 'unknown_person'})
        self.conn.executemany('INSERT INTO tags VALUES(?,?,?,?)', [
            (self.pid, 'Client', 'signal', 'manual'),
            (self.pid, 'Not reachable', 'scout', 'auto'),
            (other, 'Not reachable', 'scout', 'auto')])
        self.conn.commit()
        def ids(query):
            where, args = server.lead_filter(query)
            return {r[0] for r in self.conn.execute('SELECT p.id ' + server.PEOPLE_FROM + ' WHERE ' + ' AND '.join(where), args)}
        self.assertEqual(ids({'tags': ['Not reachable']}), {other})
        self.assertEqual(ids({'any': ['Not reachable']}), {other})
        self.assertEqual(ids({'not': ['Not reachable']}), {self.pid})
        self.assertEqual(ids({'tags': ['Client']}), {self.pid})
        facets = {(r['tag'], r['source']): r for r in server.tag_facets(self.conn, {})}
        self.assertEqual(facets['Not reachable', 'auto']['count'], 1)
        self.assertEqual(facets['Not reachable', 'auto']['total'], 1)
        # Explicit manual disagreement stays discoverable and is flagged in details.
        self.conn.execute("UPDATE tags SET source='manual' WHERE person_id=? AND tag='Not reachable'", (self.pid,))
        self.assertEqual(ids({'tags': ['Not reachable']}), {self.pid, other})
        server.set_status(self.conn, [self.pid], status='client')
        self.assertEqual(ids({'tags': ['Client']}), {self.pid})  # Client is a relationship, independent of stage
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tags').fetchone()[0], 3)

    def test_sql_visibility_matches_presentation_for_supported_relationships(self):
        tags = ['Not reachable', 'Scout: No', 'Too big', 'Other market', 'Scout: Strong',
                'Scout: Possible', 'AI: Top fit', 'Fit: strong', 'Fit: good', 'Client', 'Friend']
        for status in (None, 'interested', 'talking', 'client', 'no'):
            for source in ('auto', 'rule', 'manual'):
                self.conn.execute('DELETE FROM tags')
                self.conn.execute('DELETE FROM marks')
                if status:
                    self.conn.execute('INSERT INTO marks VALUES(?,?,NULL,?)', (self.pid, status, db.now()))
                self.conn.executemany('INSERT INTO tags VALUES(?,?,?,?)', [(self.pid, t, 'signal', source) for t in tags])
                raw = [dict(r) for r in self.conn.execute('SELECT * FROM tags ORDER BY tag')]
                expected = {t['tag'] for t in owner.visible_tags(self.person(), raw)}
                actual = {r[0] for r in self.conn.execute('SELECT t.tag FROM tags t LEFT JOIN marks tm ON tm.person_id=t.person_id WHERE ' + owner.visible_tag_sql())}
                self.assertEqual(actual, expected, (status, source))
