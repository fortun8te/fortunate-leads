"""Independent human relationship, familiarity and outreach facts stay coherent."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from test_server import db, server
import owner
import owner_notes
import qualify


class RelationshipContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.conn = db.init(self.path)
        self.pid = db.upsert_person(self.conn, {'handle': 'relation_case', 'bio': 'A small service studio', 'followers': 900000})
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def read(self):
        return server.api_person(self.conn, {}, {}, self.pid)

    def mark(self, **values):
        return server.api_mark(self.conn, {}, values, self.pid)

    def test_relationship_only_record_survives_restart_and_note_clear(self):
        self.mark(relationships=['friend'], familiarity='briefly')
        self.mark(note='')
        self.conn.close()
        self.conn = db.init(self.path)
        result = self.read()
        self.assertEqual(result['relationships'], ['friend'])
        self.assertEqual(result['familiarity'], 'briefly')
        self.assertIsNone(result['status'])
        self.assertIn('Friend', [t['tag'] for t in result['tags']])

    def test_client_stage_and_business_decision_are_independent(self):
        self.mark(relationships=['client'], status='spoke_before')
        result = self.read()
        self.assertEqual(result['relationships'], ['worked_with', 'client'])
        self.assertEqual(result['status'], 'spoke_before')
        self.assertIsNone(result['familiarity'])
        self.mark(status='no')
        self.assertEqual(self.read()['relationships'], ['worked_with', 'client'])
        self.assertEqual(server.feedback_example(self.conn, self.pid)['label'], 'no')
        self.assertIsNone(self.read()['owner_conflict'])

    def test_context_revision_conflict_does_not_overwrite_newer_edit(self):
        old = self.read()['mark_rev']
        self.mark(relationships=['friend'])
        with self.assertRaises(server.Conflict):
            self.mark(if_match=old, familiarity='close')
        self.assertIsNone(self.read()['familiarity'])

    def test_friend_is_not_positive_business_training_but_client_is(self):
        self.mark(relationships=['friend'], familiarity='close')
        self.assertIsNone(server.feedback_example(self.conn, self.pid))
        self.assertEqual(server.fewshot(self.conn), [])
        self.mark(relationships=['client'])
        self.assertEqual(server.feedback_example(self.conn, self.pid)['status'], 'client')
        self.assertEqual(server.fewshot(self.conn)[0]['person_id'], self.pid)

    def test_legacy_client_survives_stage_edit_and_can_be_explicitly_removed(self):
        self.mark(status='client', note='Saved history')
        self.mark(status='contacted')
        self.assertEqual(self.read()['relationships'], ['worked_with', 'client'])
        self.mark(relationships=[], familiarity=None)
        self.assertEqual(self.read()['relationships'], [])
        self.assertEqual(self.read()['status'], 'contacted')
        self.assertEqual(self.read()['note'], 'Saved history')

    def test_manual_relationship_tag_uses_canonical_context(self):
        server.api_tag_edit(self.conn, {}, {'add': ['Friend']}, self.pid)
        self.assertEqual(self.read()['relationships'], ['friend'])
        server.api_tag_edit(self.conn, {}, {'remove': ['Friend']}, self.pid)
        self.assertEqual(self.read()['relationships'], [])

    def test_relationship_display_filters_and_facets_agree(self):
        self.mark(relationships=['friend'])
        self.conn.execute("INSERT INTO tags VALUES(?,'Not reachable','signal','auto')", (self.pid,))
        self.conn.commit()
        self.assertNotIn('Not reachable', [t['tag'] for t in self.read()['tags']])
        self.assertEqual(server.api_leads(self.conn, {'tags': ['Not reachable'], 'status': ['all']}, {})['total'], 0)
        self.assertEqual(server.api_leads(self.conn, {'tags': ['Friend'], 'status': ['all']}, {})['total'], 1)
        self.assertEqual(next(t for t in server.tag_facets(self.conn, {}) if t['tag'] == 'Friend')['total'], 1)

    def test_legacy_and_new_clients_have_same_visible_filterable_tags(self):
        self.mark(status='client')
        self.assertIn('Client', [t['tag'] for t in self.read()['tags']])
        self.assertEqual(server.api_leads(self.conn, {'tags': ['Client'], 'status': ['all']}, {})['total'], 1)
        self.mark(status='spoke_before')
        self.assertIn('Client', [t['tag'] for t in self.read()['tags']])
        self.assertEqual(server.api_leads(self.conn, {'tags': ['Client'], 'status': ['all']}, {})['total'], 1)

    def test_fixed_relationship_taxonomy_cannot_be_globally_renamed_or_deleted(self):
        self.mark(relationships=['friend'])
        for label in owner.RELATIONSHIPS.values():
            with self.assertRaisesRegex(server.Bad, 'Edit relationships on the profile'):
                server.api_tag_delete(self.conn, {}, {'tag': label})
            with self.assertRaisesRegex(server.Bad, 'Edit relationships on the profile'):
                server.api_tag_rename(self.conn, {}, {'from': label, 'to': 'Other'})
            with self.assertRaisesRegex(server.Bad, 'Edit relationships on the profile'):
                server.api_tag_rename(self.conn, {}, {'from': 'Other', 'to': label})
        self.assertEqual(self.read()['relationships'], ['friend'])

    def test_owner_context_hash_and_private_prompt(self):
        person = dict(self.conn.execute('SELECT * FROM people WHERE id=?', (self.pid,)).fetchone())
        untouched = qualify.input_hash(person)
        person.update(relationships=[], familiarity=None)
        self.assertEqual(qualify.input_hash(person), untouched)
        person.update(relationships=['friend'], familiarity='briefly', note='SECRET PRIVATE STORY')
        self.assertNotEqual(qualify.input_hash(person), untouched)
        packet = qualify._packet(person, [], [])
        self.assertIn('Friend', packet)
        self.assertIn('Briefly', packet)
        self.assertNotIn('SECRET', packet)
        self.assertIn('not closeness', packet)

    def test_notes_require_explicit_facts_not_implied_closeness(self):
        for note, kind in [('We lived together for three weeks.', 'close'),
                           ('He is not my friend.', 'friend'),
                           ('We lived together for three weeks.', 'colleague'),
                           ('I hope we work together.', 'worked_with'),
                           ('We were talking last year.', 'in_conversation')]:
            self.assertEqual(owner_notes.validate({'facts': [{'kind': kind, 'quote': note}]}, note), [])
        note = 'He is a close friend.'
        facts = owner_notes.validate({'facts': [{'kind': 'friend', 'quote': note}, {'kind': 'close', 'quote': note}]}, note)
        self.assertEqual(owner_notes.actionable(facts[0])['relationships'], ['friend'])
        self.assertEqual(owner_notes.actionable(facts[1])['familiarity'], 'close')
