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

    def test_changed_url_invalidates_success_and_failure_but_same_url_does_not(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f'{pid}.jpg', pid))
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/old.jpg'})
        self.assertEqual(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0], f'{pid}.jpg')
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/new.jpg'})
        self.assertIsNone(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0])
        self.conn.execute('UPDATE people SET pic_file=? WHERE id=?', ('', pid))
        db.upsert_person(self.conn, {'handle': 'alice', 'pic_url': 'https://a.cdninstagram.com/latest.jpg'})
        self.assertIsNone(self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,)).fetchone()[0])

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
        (server.pfp_dir() / '42.jpg').write_bytes(JPEG)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.CFG['port']}/img/42") as response:
            self.assertEqual(response.read(), JPEG)
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
            self.assertEqual(response.headers['Content-Type'], 'image/jpeg')

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
