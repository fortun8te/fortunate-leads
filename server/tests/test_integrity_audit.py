"""Offline regressions for identity merges and delayed profile responses."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import biofetch
import db
import external_queue
import owner_notes
import processing_state
import qual_api
import server
import websearch


class IntegrityAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'audit.sqlite'
        self.conn = db.init(self.path)
        self.now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, **fields):
        return db.upsert_person(self.conn, dict(handle=handle, **fields))

    def enable_graph(self):
        biofetch.save(self.conn, {'on': True, 'token': 'old-token', 'ig_user_id': '999'})
        self.conn.commit()

    def graph_reply(self, **fields):
        return (200, {'business_discovery': dict(username='acme', biography='Older Graph bio',
                                                name='Acme', **fields)}, {})

    def test_merge_moves_review_events_and_retires_deleted_id(self):
        keep, drop = self.person('survivor'), self.person('duplicate')
        processing_state.record_review_event(self.conn, drop, 'local:test', 'good', 80, 'Duplicate review')
        self.conn.execute('INSERT INTO ai_scoring_events(person_id,scored_at) VALUES(?,?)', (drop, db.now()))
        db.merge_people(self.conn, keep, drop)
        newcomer = self.person('unrelated')
        self.assertGreater(newcomer, drop)
        self.assertEqual(processing_state.recent_history(self.conn, person_id=newcomer), [])
        self.assertEqual(processing_state.recent_history(self.conn, person_id=drop), [])
        history = processing_state.recent_history(self.conn, person_id=keep)
        self.assertEqual([(row['handle'], row['reason']) for row in history], [('survivor', 'Duplicate review')])
        self.assertEqual(self.conn.execute('SELECT person_id FROM ai_scoring_events').fetchone()[0], keep)

    def test_merge_moves_archives_rewrites_identity_and_retains_bound(self):
        keep, drop = self.person('survivor'), self.person('duplicate')
        for pid, fingerprint in [(keep, 'same'), (drop, 'same'), (drop, 'second'), (drop, 'third')]:
            self.conn.execute('''INSERT INTO processing_ai_history
                (person_id,model,input_hash,verdict_updated_at,verdict,tags,archived_at)
                VALUES(?,'local:test',?,'2026-09-27',?,'[]','2026-09-27')''',
                (pid, fingerprint, json.dumps({'person_id': pid, 'reason': fingerprint})))
        db.merge_people(self.conn, keep, drop)
        rows = self.conn.execute('SELECT person_id,verdict FROM processing_ai_history ORDER BY id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['person_id'] == keep and json.loads(row['verdict'])['person_id'] == keep for row in rows))
        self.assertEqual([json.loads(row['verdict'])['reason'] for row in rows], ['second', 'third'])

    def test_merge_discards_dropped_identity_caches_and_attempts(self):
        keep, drop = self.person('survivor'), self.person('duplicate')
        external_queue.ensure(self.conn)
        owner_notes.ensure(self.conn)
        self.conn.execute(websearch.SCHEMA)
        token = external_queue.claim(self.conn, drop, 'old-input', now=100)
        self.conn.execute("INSERT INTO web_research VALUES(?,'old-input','[]',NULL,'2026-09-27')", (drop,))
        self.conn.execute("INSERT INTO owner_note_reads VALUES(?,'old-snapshot','ready','[]','2026-09-27',0)", (drop,))
        db.merge_people(self.conn, keep, drop)
        newcomer = self.person('unrelated')
        self.assertGreater(newcomer, drop)
        for table in ('external_attempts', 'web_research', 'owner_note_reads'):
            with self.subTest(table=table):
                self.assertIsNone(self.conn.execute(f'SELECT 1 FROM {table} WHERE person_id IN (?,?)', (drop, newcomer)).fetchone())
        self.assertFalse(external_queue.complete(self.conn, drop, 'old-input', {}, claim_token=token, now=110))

    def test_merge_keeps_content_hash_with_selected_website_evidence(self):
        keep, drop = self.person('survivor'), self.person('duplicate')
        qual_api._ensure(self.conn)
        for pid, stamp, digest in [(keep, '2026-09-25', 'old-hash'), (drop, '2026-09-27', 'new-hash')]:
            self.conn.execute('''INSERT INTO site_reads
                (person_id,url,title,summary,signals,at,content_hash) VALUES(?,?,'Title',?,'{}',?,?)''',
                (pid, 'https://example.com', digest, stamp, digest))
        db.merge_people(self.conn, keep, drop)
        row = self.conn.execute('SELECT summary,content_hash FROM site_reads WHERE person_id=?', (keep,)).fetchone()
        self.assertEqual(tuple(row), ('new-hash', 'new-hash'))

    def test_graph_response_does_not_overwrite_a_newer_extension_read(self):
        pid = self.person('acme')
        self.enable_graph()
        def fetch(_url):
            other = db.connect(self.path)
            try:
                db.upsert_person(other, {'handle': 'acme', 'bio': 'Fresh browser bio',
                                        'bio_at': db.now(), 'bio_src': 'extension'})
                other.commit()
            finally:
                other.close()
            return self.graph_reply()
        with patch.object(biofetch, 'FETCH', [fetch]):
            self.assertTrue(biofetch.step(self.conn, self.now))
        row = self.conn.execute('SELECT bio,bio_src,bd_at FROM people WHERE id=?', (pid,)).fetchone()
        self.assertEqual(tuple(row), ('Fresh browser bio', 'extension', None))
        self.assertEqual(biofetch.state(self.conn)['calls'], 1)

    def test_graph_response_does_not_follow_reassigned_handle(self):
        original = self.person('acme', ig_id='original')
        self.enable_graph()
        def fetch(_url):
            other = db.connect(self.path)
            try:
                db.upsert_person(other, {'handle': 'renamed', 'ig_id': 'original'})
                db.upsert_person(other, {'handle': 'acme', 'ig_id': 'new-owner'})
                other.commit()
            finally:
                other.close()
            return self.graph_reply()
        with patch.object(biofetch, 'FETCH', [fetch]):
            biofetch.step(self.conn, self.now)
        self.assertIsNone(self.conn.execute("SELECT bio FROM people WHERE handle='acme'").fetchone()[0])
        self.assertIsNone(self.conn.execute('SELECT bio FROM people WHERE id=?', (original,)).fetchone()[0])

    def test_graph_auth_error_does_not_replace_new_settings(self):
        self.person('acme')
        self.enable_graph()
        def fetch(_url):
            other = db.connect(self.path)
            try:
                biofetch.save(other, {'token': 'new-token', 'gap': 5})
                other.commit()
            finally:
                other.close()
            return (400, {'error': {'code': 190}}, {})
        with patch.object(biofetch, 'FETCH', [fetch]):
            biofetch.step(self.conn, self.now)
        settings = biofetch.settings(self.conn)
        self.assertEqual((settings['token'], settings['gap'], settings['on']), ('new-token', 5, True))

    def test_graph_mismatched_username_is_not_ingested(self):
        pid = self.person('acme')
        self.enable_graph()
        reply = (200, {'business_discovery': {'username': 'somebody_else', 'biography': 'Wrong person'}}, {})
        with patch.object(biofetch, 'FETCH', [lambda _url: reply]):
            biofetch.step(self.conn, self.now)
        self.assertIsNone(self.conn.execute('SELECT bio FROM people WHERE id=?', (pid,)).fetchone()[0])

    def test_usage_headers_tolerate_unexpected_json_types(self):
        for header in ('x-app-usage', 'x-business-use-case-usage'):
            for data in ('null', '[]', '7', '"text"', '{"999":[null,42,[]]}', '{"999":null}', '{}'):
                with self.subTest(header=header, data=data):
                    self.assertEqual(biofetch.usage_pct({header: data}), 0)
        self.assertEqual(biofetch.usage_pct({'x-app-usage': '{"call_count":80}'}), 80)
        self.assertEqual(biofetch.usage_pct({'x-business-use-case-usage': '{"999":[{"total_time":85}]}'}), 85)

    def test_complete_profile_can_remove_website_but_partial_cannot(self):
        pid = self.person('acme', website='https://old.example')
        self.conn.commit()
        server.ext_profile(self.conn, {}, {'profile': {'handle': 'acme', 'website': None}})
        self.assertEqual(self.conn.execute('SELECT website FROM people WHERE id=?', (pid,)).fetchone()[0], 'https://old.example')
        server.ext_profile(self.conn, {}, {'profile': {'handle': 'acme', 'bio': 'Complete bio', 'website': None}})
        self.assertEqual(self.conn.execute('SELECT website FROM people WHERE id=?', (pid,)).fetchone()[0], '')

    def test_omitted_or_unsafe_profile_website_does_not_erase_existing_link(self):
        pid = self.person('acme', website='https://old.example')
        self.conn.commit()
        for profile in ({'handle': 'acme', 'bio': 'Complete'},
                        {'handle': 'acme', 'bio': 'Complete', 'website': 'javascript:alert(1)'}):
            server.ext_profile(self.conn, {}, {'profile': profile})
            self.assertEqual(self.conn.execute('SELECT website FROM people WHERE id=?', (pid,)).fetchone()[0], 'https://old.example')


if __name__ == '__main__':
    unittest.main()
