"""Selected note identities and bounded local context do not assert graph facts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import note_mentions as mentions
import owner_notes as notes


class NoteMentions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'notes.sqlite'))
        self.pid = db.upsert_person(self.conn, {'handle': 'subject'})
        self.friend = db.upsert_person(self.conn, {'handle': 'alex', 'name': 'Alex'})
        self.note = 'He is friends with @alex.'
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (self.pid, None, self.note, 'now'))
        db.set_setting(self.conn, 'local_laya', True)
        self.refs = [{'person_id': self.friend, 'token': '@alex'}]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_selected_identity_survives_handle_rename(self):
        mentions.save(self.conn, self.pid, self.note, self.refs)
        self.conn.execute("UPDATE people SET handle='alex_new' WHERE id=?", (self.friend,))
        mentions.save(self.conn, self.pid, self.note, self.refs)
        result = mentions.get(self.conn, self.pid)
        self.assertEqual(result[0]['person_id'], self.friend)
        self.assertEqual(result[0]['token'], '@alex')
        self.assertEqual(result[0]['handle'], 'alex_new')

    def test_unselected_tokens_do_not_resolve_and_mismatch_rejected(self):
        self.assertEqual(mentions.get(self.conn, self.pid), [])
        with self.assertRaises(ValueError):
            mentions.save(self.conn, self.pid, 'Friend @someone', [{'person_id': self.friend, 'token': '@someone'}])
        with self.assertRaises(ValueError):
            mentions.save(self.conn, self.pid, '@alexander', self.refs)
        with self.assertRaises(ValueError):
            mentions.save(self.conn, self.pid, '@alex.other', self.refs)
        self.assertFalse(mentions.contains('email@alex', '@alex'))
        self.assertTrue(mentions.contains('@alex.', '@alex'))

    def test_note_removal_and_edit_drop_reference(self):
        mentions.save(self.conn, self.pid, self.note, self.refs)
        mentions.save(self.conn, self.pid, 'He is friends with someone else.')
        self.assertEqual(mentions.get(self.conn, self.pid), [])
        mentions.save(self.conn, self.pid, self.note, self.refs)
        self.assertEqual(mentions.get(self.conn, self.pid, ''), [])
        mentions.save(self.conn, self.pid, '')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM note_mentions').fetchone()[0], 0)

    def test_saved_relationship_changes_invalidate_subject_note(self):
        mentions.save(self.conn, self.pid, self.note, self.refs)
        self.conn.commit()
        with patch.object(notes, 'interpret', return_value=[]) as call:
            notes.step(self.conn)
        context = call.call_args.kwargs['context']
        self.assertEqual(context['mentions'][0]['person_id'], self.friend)
        self.assertNotIn('bio', context['mentions'][0])
        self.assertEqual(notes.result(self.conn, self.pid)['state'], 'ready')
        self.conn.execute('INSERT INTO owner_context VALUES(?,?,?,?)', (self.friend, '["friend"]', 'close', 'now'))
        self.assertEqual(notes.result(self.conn, self.pid)['state'], 'pending')
        with patch.object(notes, 'interpret', return_value=[]) as call:
            notes.step(self.conn)
        self.assertEqual(call.call_args.kwargs['context']['mentions'][0]['owner_saved']['familiarity'], 'close')
        self.conn.execute('INSERT INTO tags(person_id,tag,grp,source) VALUES(?,?,?,?)', (self.friend, 'trusted', 'manual', 'manual'))
        self.assertEqual(notes.result(self.conn, self.pid)['state'], 'pending')

    def test_third_party_claim_never_promotes_owner_relationship(self):
        for kind in ('friend', 'close', 'current_client', 'knows_person', 'follow_up', 'business_context'):
            self.assertEqual(notes.validate({'facts': [{'kind': kind, 'quote': self.note}]}, self.note), [])
        fact = notes.validate({'facts': [{'kind': 'mentioned_connection', 'quote': self.note}]}, self.note)[0]
        self.assertNotIn('relationships', notes.actionable(fact))
        self.assertNotIn('status', notes.actionable(fact))
        self.assertNotIn('familiarity', notes.actionable(fact))
        mentions.save(self.conn, self.pid, self.note, self.refs)
        self.conn.commit()
        with patch.object(notes, 'interpret', return_value=[fact]):
            notes.step(self.conn)
        self.assertIsNone(notes.local_context(self.conn, self.pid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM owner_context').fetchone()[0], 0)

    def test_search_treats_wildcards_as_literal_and_bounds_output(self):
        self.assertEqual(mentions.search(self.conn, 'al')['people'][0]['id'], self.friend)
        self.assertEqual(mentions.search(self.conn, '%')['people'], [])
        self.assertLessEqual(len(mentions.search(self.conn, '')['people']), 8)

    def test_suggestion_only_shows_saved_relationships_and_profile_photo(self):
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', ('alex.jpg', self.friend))
        self.conn.execute('INSERT INTO owner_context VALUES(?,?,?,?)',
                          (self.friend, '["friend", "colleague", "acquaintance"]', None, 'now'))
        person = mentions.search(self.conn, 'alex')['people'][0]
        self.assertEqual(person['pic'], f'/img/{self.friend}')
        self.assertEqual(person['badges'], ['Colleague', 'Friend'])
        self.conn.execute('DELETE FROM owner_context WHERE person_id=?', (self.friend,))
        self.assertEqual(mentions.search(self.conn, 'alex')['people'][0]['badges'], [])

    def test_verified_identity_merge_preserves_references(self):
        mentions.save(self.conn, self.pid, self.note, self.refs)
        survivor = db.upsert_person(self.conn, {'handle': 'alex_survivor'})
        mentions.merge(self.conn, survivor, self.friend)
        self.conn.execute('DELETE FROM people WHERE id=?', (self.friend,))
        self.assertEqual(mentions.get(self.conn, self.pid)[0]['person_id'], survivor)
        self.assertEqual(mentions.get(self.conn, self.pid)[0]['token'], '@alex')

    def test_context_change_during_inference_discards_stale_result(self):
        mentions.save(self.conn, self.pid, self.note, self.refs)
        self.conn.commit()
        def infer(note, context=None):
            self.conn.execute('INSERT INTO owner_context VALUES(?,?,?,?)', (self.friend, '["friend"]', 'close', 'now'))
            self.conn.commit()
            return [{'kind': 'mentioned_connection', 'quote': note}]
        with patch.object(notes, 'interpret', side_effect=infer):
            notes.step(self.conn)
        self.assertEqual(notes.result(self.conn, self.pid)['state'], 'pending')
        self.assertEqual(notes.result(self.conn, self.pid)['facts'], [])

    def test_model_receives_context_and_returns_exact_sentence(self):
        context = mentions.context(self.conn, self.pid, self.note)
        with patch.object(notes.local_model, 'complete_json', return_value={'facts': [{'kind': 'mentioned_connection', 'sentence': 0}]}) as call:
            result = notes.interpret(self.note, context=context)
        self.assertEqual(result[0]['quote'], self.note)
        self.assertEqual(json.loads(call.call_args.args[1])['context'], context)
