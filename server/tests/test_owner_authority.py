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
        self.assertNotIn('Client', {t['tag'] for t in server.api_person(self.conn, {}, {}, self.pid)['tags']})
        self.assertEqual(self.conn.execute("SELECT source FROM tags WHERE person_id=? AND tag='Client'", (self.pid,)).fetchone()[0], 'manual')

    def test_explicit_no_has_one_clear_owner_conflict_and_blocks_positive_recommendation(self):
        self.conn.execute("INSERT INTO tags VALUES(?,'Client','signal','manual')", (self.pid,))
        server.set_status(self.conn, [self.pid], status='no')
        server.requalify(self.conn, self.person(), None)
        result = server.api_person(self.conn, {}, {}, self.pid)
        self.assertEqual(result['owner_status'], 'no')
        self.assertIn('conflicts', result['owner_conflict'])
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
        server.set_status(self.conn, [self.pid], status=None)
        server.requalify(self.conn, self.person(), None)
        self.assertIn('Not reachable', {t['tag'] for t in server.api_person(self.conn, {}, {}, self.pid)['tags']})

    def test_full_story_is_saved_hashed_and_source_labelled(self):
        note = 'We met through a friend. ' * 35 + 'He is founder of a skincare brand.'
        server.set_status(self.conn, [self.pid], note=note)
        person = self.person()
        self.assertEqual(person['note'], note)
        self.assertIn('He is founder of a skincare brand.', '\n'.join(qualify.owner_lines(person)))
        before = qualify.input_hash(person)
        self.assertNotEqual(before, qualify.input_hash(dict(person, note=note + ' We talked yesterday.')))
        verdict = qualify._verdict({'role': 'buyer', 'fit': 80, 'evidence': ['founder of a skincare brand'],
            'decision_maker': True}, person, [], 'offline-test', 'test')
        self.assertEqual(verdict['role'], 'buyer')
        self.assertIn('Your note', verdict['reason'])
        self.assertIn('Your note', verdict['evidence'][0])

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
