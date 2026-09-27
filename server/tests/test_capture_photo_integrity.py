"""Offline regressions for delayed captures and work completing after another writer."""
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import db
import server

JPEG = b'\xff\xd8\xff\xe0a-photo\xff\xd9'


class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'leads.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        self.other = db.connect(self.path)
        self.addCleanup(self.other.close)
        self.config = patch.dict(server.CFG, {'db': self.path})
        self.config.start()
        self.addCleanup(self.config.stop)
        server._pfp_check_id = 0

    def person(self, **fields):
        pid = db.upsert_person(self.conn, dict(handle='alice', **fields))
        self.conn.commit()
        return pid

    def test_delayed_capture_does_not_replace_newer_bio_or_complete_queued_refresh(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'ig_id': '11', 'bio': 'fresh',
                             'bio_at': '2026-01-02T00:00:00+00:00'}, '2026-01-02T00:00:00+00:00')
        self.conn.execute("INSERT INTO jobs(kind,handle,state) VALUES('profile','alice','queued')")
        self.conn.commit()
        server.ext_profile(self.conn, {}, {'captured_at': '2026-01-01T20:00:00-02:00',
                           'profile': {'handle': 'alice', 'ig_id': '11', 'bio': 'old'}})
        self.assertEqual(self.conn.execute('SELECT bio FROM people WHERE id=?', (pid,)).fetchone()[0], 'fresh')
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'queued')

    def test_delayed_first_capture_does_not_acknowledge_later_refresh_request(self):
        self.person()
        self.conn.execute("INSERT INTO jobs(kind,handle,state,created_at) VALUES('profile','alice','queued',?)",
                          ('2026-01-02T00:00:00+00:00',))
        self.conn.commit()
        server.ext_profile(self.conn, {}, {'captured_at': '2026-01-01T00:00:00Z',
                           'profile': {'handle': 'alice', 'bio': 'Earlier read'}})
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'queued')

    def test_invalid_capture_is_rejected_and_sparse_capture_preserves_fields(self):
        self.person(bio='saved', website='https://example.com', is_private=True)
        for stamp in ('bad', '2099-01-01T00:00:00Z', '2026-01-01', 42, None):
            with self.assertRaises(server.Bad):
                server.ext_profile(self.conn, {}, {'captured_at': stamp, 'profile': {'handle': 'alice', 'bio': 'bad'}})
            self.conn.rollback()
        server.ext_profile(self.conn, {}, {'captured_at': db.now(), 'profile': {'handle': 'alice', 'name': 'Alice'}})
        self.assertEqual(tuple(self.conn.execute('SELECT bio,website,is_private FROM people').fetchone()),
                         ('saved', 'https://example.com', 1))
        server.ext_profile(self.conn, {}, {'captured_at': db.now(), 'profile': {'handle': 'alice', 'bio': '', 'website': None}})
        self.assertEqual(tuple(self.conn.execute('SELECT bio,website FROM people').fetchone()), ('', ''))

    def test_deleted_ids_are_never_reallocated_and_migration_reserves_old_photos(self):
        pid = self.person()
        self.conn.execute('DELETE FROM people WHERE id=?', (pid,))
        new = db.upsert_person(self.conn, {'handle': 'bob'})
        self.assertGreater(new, pid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,'eve','','')", (pid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('UPDATE people SET id=? WHERE id=?', (pid, new))
        self.conn.execute('INSERT INTO ai_scoring_events(person_id,scored_at) VALUES(300,?)', (db.now(),))
        self.conn.execute('DROP TRIGGER people_identity_allocation')
        self.conn.execute('DROP TABLE person_id_sequence')
        self.conn.commit()
        server.pfp_dir().mkdir()
        (server.pfp_dir() / '200.jpg').write_bytes(JPEG)
        migrated = db.init(self.path)
        try:
            self.assertGreater(db.upsert_person(migrated, {'handle': 'carol'}), 300)
            migrated.commit()
        finally:
            migrated.close()

    def test_photo_fetch_cannot_write_after_second_connection_replaces_person(self):
        pid = self.person(ig_id='11', pic_url='https://a.cdninstagram.com/a.jpg')
        def fetch(_):
            self.other.execute('DELETE FROM people WHERE id=?', (pid,))
            replacement = db.upsert_person(self.other, {'handle': 'bob', 'ig_id': '22', 'pic_url': 'https://a.cdninstagram.com/a.jpg'})
            self.other.commit()
            self.assertGreater(replacement, pid)
            return JPEG
        with patch.object(server.meta_network, 'blocked', return_value=False), patch.object(server, 'fetch_pic', side_effect=fetch):
            self.assertTrue(server.pfp_step(self.conn))
        self.assertFalse((server.pfp_dir() / f'{pid}.jpg').exists())
        self.assertIsNone(self.conn.execute('SELECT pic_file FROM people').fetchone()[0])

    def test_photo_retry_waits_then_succeeds(self):
        self.person(pic_url='https://a.cdninstagram.com/a.jpg')
        instant = db.utc_now()
        with patch.object(server.meta_network, 'blocked', return_value=False), patch.object(server, 'fetch_pic', return_value=None) as fetch, patch.object(db, 'utc_now', return_value=instant):
            self.assertTrue(server.pfp_step(self.conn))
            self.assertFalse(server.pfp_step(self.conn))
            fetch.assert_called_once()
        row = self.conn.execute('SELECT pic_attempts,pic_retry_at FROM people').fetchone()
        self.assertEqual(row['pic_attempts'], 1)
        self.assertEqual(row['pic_retry_at'], server.iso(instant + timedelta(seconds=60)))
        with patch.object(server.meta_network, 'blocked', return_value=False), patch.object(server, 'fetch_pic', return_value=JPEG), patch.object(db, 'now', return_value=server.iso(instant + timedelta(seconds=61))):
            self.assertTrue(server.pfp_step(self.conn))
        self.assertEqual(tuple(self.conn.execute('SELECT pic_attempts,pic_retry_at,pic_refresh FROM people').fetchone()), (0, None, 0))

    def test_cached_laya_ack_preserves_second_connection_profile_update(self):
        pid = self.person(bio='old')
        row = self.conn.execute('SELECT handle,name,bio,category,website,followers FROM people').fetchone()
        self.conn.execute('INSERT INTO laya VALUES(?,?,?,?,?)', (pid, server.laya_hash(*row), '{}', 0, db.now()))
        db.set_setting(self.conn, 'laya_queue_signature', server.laya.cache_signature())
        self.conn.commit()
        owner = self
        class RaceConnection:
            def __getattr__(self, name):
                return getattr(owner.conn, name)
            def executemany(self, query, args):
                if query.startswith('DELETE FROM laya_queue'):
                    owner.other.execute("UPDATE people SET bio='new' WHERE id=?", (pid,))
                    owner.other.commit()
                return owner.conn.executemany(query, args)
        with patch.object(server, 'laya_allowed', return_value=True), patch.object(server.laya, 'available', return_value=True), patch.object(server.laya, 'decide') as decide:
            self.assertFalse(server.laya_step(RaceConnection()))
            decide.assert_not_called()
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM laya_queue WHERE person_id=?', (pid,)).fetchone())


if __name__ == '__main__':
    unittest.main()
