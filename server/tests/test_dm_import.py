"""Synthetic Instagram downloads; no account or message archive is accessed."""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import dm_import


def selected(name, data):
    return {'name': name, 'data_base64': base64.b64encode(data).decode()}


def conversation(me='Michael', other='Alex Design', messages=None):
    messages = messages or [('Alex Design', 1750000000000), ('Michael', 1750000001000)]
    return json.dumps({'participants': [{'name': me}, {'name': other}],
                       'messages': [{'sender_name': sender, 'timestamp_ms': stamp, 'content': 'private words'}
                                    for sender, stamp in messages]}).encode()


def archive(*members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as z:
        for path, content in members:
            z.writestr(path, content)
    return [selected('instagram.zip', data.getvalue())]


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'test.sqlite'))
        self.addCleanup(self.conn.close)
        self.files = archive(('your_instagram_activity/messages/inbox/alex_123/message_1.json', conversation()))

    def test_preview_needs_owner_and_does_not_write(self):
        first = dm_import.preview(self.conn, {}, {'files': self.files})
        self.assertEqual(first['sender_candidates'], ['Alex Design', 'Michael'])
        self.assertEqual(first['threads'], [])
        found = dm_import.preview(self.conn, {}, {'files': self.files, 'owner_name': 'Michael'})
        self.assertEqual((found['outbound_threads'], found['threads'][0]['outbound'], found['threads'][0]['inbound']), (1, 1, 1))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_confirm_creates_contacted_event_without_raw_messages_and_is_idempotent(self):
        thread = dm_import.analyze(self.files, 'Michael')['threads'][0]
        body = {'files': self.files, 'owner_name': 'Michael', 'mappings': {thread['fingerprint']: '@alex.design'}}
        first = dm_import.confirm(self.conn, {}, body)
        self.assertEqual((first['new_people'], first['marked_contacted']), (1, 1))
        self.assertEqual(first['contacts'][0]['handle'], 'alex.design')
        pid = self.conn.execute('SELECT id FROM people WHERE handle=?', ('alex.design',)).fetchone()[0]
        self.assertEqual(self.conn.execute('SELECT status FROM marks WHERE person_id=?', (pid,)).fetchone()[0], 'contacted')
        events = self.conn.execute('SELECT kind,body,after_value FROM activity WHERE person_id=?', (pid,)).fetchall()
        self.assertEqual(len(events), 2)  # import evidence and status change
        self.assertNotIn('private words', str(events))
        self.assertEqual(json.loads(next(e['after_value'] for e in events if e['kind'] == 'dm_import'))['source'], 'instagram_export')
        dm_import.confirm(self.conn, {}, body)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM activity WHERE person_id=?', (pid,)).fetchone()[0], 2)

    def test_preserves_manual_relationship_stage_and_familiarity(self):
        pid = db.upsert_person(self.conn, {'handle': 'alex.design'})
        self.conn.execute("INSERT INTO marks VALUES(?,?,?,?)", (pid, 'talking', 'Owner note', db.now()))
        self.conn.execute("INSERT INTO owner_context VALUES(?,?,?,?)", (pid, '["friend"]', 'close', db.now()))
        self.conn.commit()
        thread = dm_import.analyze(self.files, 'Michael')['threads'][0]
        dm_import.confirm(self.conn, {}, {'files': self.files, 'owner_name': 'Michael',
                                          'mappings': {thread['fingerprint']: 'alex.design'}})
        self.assertEqual(tuple(self.conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()), ('talking', 'Owner note'))
        self.assertEqual(tuple(self.conn.execute('SELECT relationships,familiarity FROM owner_context WHERE person_id=?', (pid,)).fetchone()), ('["friend"]', 'close'))

    def test_inbound_only_cannot_mark_contacted(self):
        files = [selected('message_1.json', conversation(messages=[('Alex Design', 1750000000000)]))]
        thread = dm_import.analyze(files, 'Michael')['threads'][0]
        self.assertEqual(thread['outbound'], 0)
        with self.assertRaisesRegex(ValueError, 'Inbound-only'):
            dm_import.confirm(self.conn, {}, {'files': files, 'owner_name': 'Michael',
                                              'mappings': {thread['fingerprint']: 'alex.design'}})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_rejects_zip_traversal_and_expansion(self):
        bad = archive(('../messages/inbox/a/message_1.json', conversation()))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            dm_import.analyze(bad, 'Michael')
        large = archive(('messages/inbox/a/message_1.json', b'x' * (dm_import.MAX_JSON + 1)))
        with self.assertRaisesRegex(ValueError, 'too large'):
            dm_import.analyze(large, 'Michael')

    def test_skips_group_threads(self):
        group = json.dumps({'participants':[{'name':'Michael'}, {'name':'Alex'}, {'name':'Bea'}],
                            'messages':[{'sender_name':'Michael', 'timestamp_ms':1750000000000}]}).encode()
        result = dm_import.analyze(archive(('messages/inbox/group_1/message_1.json', group)), 'Michael')
        self.assertEqual((result['outbound_threads'], result['skipped_group_threads']), (0, 1))


if __name__ == '__main__':
    unittest.main()
