"""Profile picture cache behavior, with no Instagram or CDN requests."""
import os
import urllib.error
import urllib.request
from unittest import mock

from test_server import Base, db, server


JPEG = b'\xff\xd8\xff\xe0a-photo\xff\xd9'


class PictureTest(Base):
    def setUp(self):
        super().setUp()
        server._pfp_check_id = 0

    def test_collection_stop_preserves_pending_picture_without_network(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/photo.jpg'})
        for settings in ({'paused': True}, {'paused_lists': True, 'paused_bios': True},
                         {'cooldown': '2099-01-01T00:00:00Z'}, {'cooldown': 'broken'}, {'cooldown': False}):
            with self.subTest(settings=settings):
                for key in ('paused', 'paused_lists', 'paused_bios'):
                    db.set_setting(self.conn, key, False)
                db.set_setting(self.conn, 'cooldown', None)
                for key, value in settings.items():
                    db.set_setting(self.conn, key, value)
                self.conn.commit()
                with mock.patch.object(server, 'fetch_pic') as fetch:
                    self.assertFalse(server.pfp_step(self.conn))
                    fetch.assert_not_called()
                self.assertIsNone(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0])
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        with mock.patch.object(server, 'fetch_pic', return_value=JPEG) as fetch:
            self.assertTrue(server.pfp_step(self.conn))
            fetch.assert_called_once()

    def test_changed_url_keeps_photo_and_requests_refresh_but_same_url_does_not(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        self.assertEqual(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0], f'{pid}.jpg')
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/new.jpg'})
        self.assertEqual(tuple(self.conn.execute('SELECT pic_file,pic_refresh FROM people WHERE id=?', (pid,)).fetchone()), (f'{pid}.jpg', 1))
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', ('', pid))
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/latest.jpg'})
        self.assertIsNone(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0])

    def test_hold_repairs_orphaned_files_without_requests(self):
        server.pfp_dir().mkdir()
        ids = []
        for index, prior in enumerate((None, '', None, 'missing.jpg')):
            pid = db.upsert_person(self.conn, {'handle': f'person{index}', 'pic_url': 'https://a.cdninstagram.com/photo.jpg'})
            self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (prior, pid))
            ids.append(pid)
        for pid in ids[:2]:
            (server.pfp_dir() / f'{pid}.jpg').write_bytes(JPEG)
        (server.pfp_dir() / f'{ids[2]}.jpg').write_bytes(b'corrupt')
        db.set_setting(self.conn, 'cooldown', '2099-01-01T00:00:00Z')
        self.conn.commit()
        with mock.patch.object(server, 'fetch_pic') as fetch:
            self.assertTrue(server.pfp_step(self.conn))
            fetch.assert_not_called()
        self.assertEqual([r[0] for r in self.conn.execute('SELECT pic_file FROM people ORDER BY id')],
                         [None, '', None, None])

    def test_rotating_url_keeps_valid_photo_during_hold_and_failed_refresh(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        server.pfp_dir().mkdir()
        (server.pfp_dir() / f'{pid}.jpg').write_bytes(JPEG)
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/new.jpg'})
        self.conn.commit()
        with mock.patch.object(server.meta_network, 'blocked', return_value=True), mock.patch.object(server, 'fetch_pic') as fetch:
            server.pfp_step(self.conn)
            fetch.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0], f'{pid}.jpg')
        with mock.patch.object(server.meta_network, 'blocked', return_value=False), mock.patch.object(server, 'fetch_pic', return_value=None) as fetch:
            self.assertTrue(server.pfp_step(self.conn))
            fetch.assert_called_once_with('https://a.cdninstagram.com/new.jpg')
            self.assertFalse(server.pfp_step(self.conn))
        self.assertEqual(tuple(self.conn.execute('SELECT pic_file,pic_refresh FROM people WHERE id=?', (pid,)).fetchone()), (f'{pid}.jpg', 1))
        self.assertEqual((server.pfp_dir() / f'{pid}.jpg').read_bytes(), JPEG)

    def test_refresh_does_not_clear_a_newer_url_update(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        self.conn.commit()
        def fetch(_):
            db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/new.jpg'})
            return JPEG
        with mock.patch.object(server.meta_network, 'blocked', return_value=False), mock.patch.object(server, 'fetch_pic', side_effect=fetch):
            server.pfp_step(self.conn)
        self.assertIsNone(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT pic_url FROM people WHERE id=?', (pid,)).fetchone()[0], 'https://a.cdninstagram.com/new.jpg')
        self.assertEqual(self.conn.execute('SELECT pic_refresh FROM people WHERE id=?', (pid,)).fetchone()[0], 1)

    def test_local_recovery_is_bounded_and_resumable(self):
        server.pfp_dir().mkdir()
        for index in range(3):
            pid = db.upsert_person(self.conn, {'handle': f'person{index}'})
            (server.pfp_dir() / f'{pid}.jpg').write_bytes(b'corrupt')
            self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        self.conn.commit()
        with mock.patch.object(server, 'fetch_pic') as fetch:
            self.assertEqual(server.repair_pfp_cache(self.conn, limit=2), 2)
            self.assertEqual(server._pfp_check_id, 2)
            self.assertEqual(server.repair_pfp_cache(self.conn, limit=2), 1)
            self.assertEqual(server.repair_pfp_cache(self.conn, limit=2), 0)
            self.assertEqual(server._pfp_check_id, 0)
            fetch.assert_not_called()

    def test_rotating_url_refreshes_cached_photo_when_network_is_available(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        server.pfp_dir().mkdir()
        (server.pfp_dir() / f'{pid}.jpg').write_bytes(JPEG)
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/new.jpg'})
        replacement = JPEG[:-2] + b'new photo' + JPEG[-2:]
        self.conn.commit()
        with mock.patch.object(server.meta_network, 'blocked', return_value=False), mock.patch.object(server, 'fetch_pic', return_value=replacement):
            self.assertTrue(server.pfp_step(self.conn))
        self.assertEqual((server.pfp_dir() / f'{pid}.jpg').read_bytes(), replacement)
        self.assertEqual(tuple(self.conn.execute('SELECT pic_file,pic_refresh FROM people WHERE id=?', (pid,)).fetchone()), (f'{pid}.jpg', 0))

    def test_missing_file_is_repaired_and_replaced_atomically(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/photo.jpg'})
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        self.conn.commit()
        real_replace = os.replace
        seen = []

        def check_replace(source, target):
            seen.append((os.path.exists(source), os.path.exists(target)))
            real_replace(source, target)

        with mock.patch.object(server, 'fetch_pic', return_value=JPEG) as fetch, \
                mock.patch.object(server.os, 'replace', side_effect=check_replace):
            self.assertTrue(server.pfp_step(self.conn))
        fetch.assert_called_once_with('https://a.cdninstagram.com/photo.jpg')
        self.assertEqual(seen, [(True, False)])
        self.assertEqual((server.pfp_dir() / f'{pid}.jpg').read_bytes(), JPEG)
        self.assertEqual(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0], f'{pid}.jpg')

    def test_invalid_download_and_corrupt_file_are_not_served(self):
        self.assertFalse(server.valid_pic(b'\xff\xd8\xfftruncated'))
        self.assertFalse(server.valid_pic(b'\x89PNG\r\n\x1a\ntruncated'))
        self.assertFalse(server.valid_pic(b'RIFF\x00\x00\x00\x00WEBPtruncated'))
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/photo.jpg'})
        self.conn.commit()
        with mock.patch.object(server, 'fetch_pic', return_value=None):
            self.assertTrue(server.pfp_step(self.conn))
        self.assertEqual(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0], '')
        server.pfp_dir().mkdir()
        (server.pfp_dir() / f'{pid}.jpg').write_bytes(b'broken')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{server.CFG['port']}/img/{pid}")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_valid_image_is_revalidated_by_browser(self):
        server.pfp_dir().mkdir()
        pid = db.upsert_person(self.conn, {'handle': 'alice'})
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        self.conn.commit()
        (server.pfp_dir() / f'{pid}.jpg').write_bytes(JPEG)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.CFG['port']}/img/{pid}") as response:
            self.assertEqual(response.read(), JPEG)
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
            self.assertEqual(response.headers['Content-Type'], 'image/jpeg')

        self.conn.execute('UPDATE people SET pic_file=NULL WHERE id=?', (pid,))
        self.conn.commit()
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{server.CFG['port']}/img/{pid}")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_ownership_connection_tracks_database_replacement(self):
        import sqlite3
        from pathlib import Path
        cache = server.PictureOwnership()
        path = Path(server.CFG['db']).parent / 'ownership.sqlite'
        replacement = path.with_name('replacement.sqlite')
        try:
            for target, filename in ((path, '1.jpg'), (replacement, None)):
                conn = sqlite3.connect(target)
                try:
                    conn.execute('CREATE TABLE people(id INTEGER PRIMARY KEY, pic_file TEXT)')
                    conn.execute('INSERT INTO people VALUES(1, ?)', (filename,))
                    conn.commit()
                finally:
                    conn.close()
            self.assertTrue(cache.owns(path, '1'))
            os.replace(replacement, path)
            self.assertFalse(cache.owns(path, '1'))
        finally:
            cache.close()

    def test_corrupt_cached_file_is_replaced(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/photo.jpg'})
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        self.conn.commit()
        server.pfp_dir().mkdir()
        (server.pfp_dir() / f'{pid}.jpg').write_bytes(b'broken')
        with mock.patch.object(server, 'fetch_pic', return_value=JPEG) as fetch:
            self.assertTrue(server.pfp_step(self.conn))
        fetch.assert_called_once()
        self.assertEqual((server.pfp_dir() / f'{pid}.jpg').read_bytes(), JPEG)

    def test_fetch_rejects_invalid_hosts_and_nonstandard_ports_without_network(self):
        for url in ('https://evilcdninstagram.com/p.jpg', 'https://cdninstagram.com.evil.test/p.jpg',
                    'https://x.cdninstagram.com:444/p.jpg', 'https://user@x.cdninstagram.com/p.jpg',
                    'http://x.cdninstagram.com/p.jpg', 'https://x.cdninstagram.com:bad/p.jpg'):
            with self.subTest(url=url), mock.patch.object(server.PIC_OPENER, 'open') as open_:
                self.assertIsNone(server.fetch_pic(url))
                open_.assert_not_called()
