"""Photo audit on a tiny generated store: states, refetch buckets, duplicates, and parity with the server's rule."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS.parent / 'server'))
import db  # noqa: E402

spec = importlib.util.spec_from_file_location('photo_audit', OPS / 'photo_audit.py')
photo_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(photo_audit)

JPEG = b'\xff\xd8\xff\xe0' + b'a' * 60 + b'\xff\xd9'
JPEG2 = b'\xff\xd8\xff\xe0' + b'b' * 60 + b'\xff\xd9'
WEBP_BODY = b'WEBPVP8 ' + b'c' * 20
WEBP = b'RIFF' + len(WEBP_BODY).to_bytes(4, 'little') + WEBP_BODY
PNG = b'\x89PNG\r\n\x1a\n' + b'd' * 20 + b'IEND' + b'\x00' * 4


def url(expires):
    return f'https://scontent.cdninstagram.com/v/pic.jpg?stp=x&oe={expires:X}&_nc_sid=1'


class PhotoAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pfp = self.root / 'pfp'
        self.pfp.mkdir()
        self.db = self.root / 'leads.sqlite'
        conn = db.init(str(self.db))
        future, past = int(time.time()) + 86400, int(time.time()) - 86400
        rows = [  # id, pic_file, pic_url, file bytes
            (1, '1.jpg', url(future), JPEG),            # healthy
            (2, '2.jpg', url(future), JPEG[:-5]),        # damaged: truncated
            (3, '3.jpg', url(future), None),             # database says photo, file is gone
            (4, '', url(future), None),                  # no photo, address still good
            (5, '', url(past), None),                    # no photo, address expired
            (6, '', None, None),                         # no photo, no address
            (7, '7.jpg', None, WEBP),                    # healthy webp
            (8, '8.jpg', None, JPEG),                    # healthy, same bytes as 1
            (9, '', None, PNG),                          # file but the database does not link it
        ]
        for pid, pic_file, pic_url, data in rows:
            conn.execute('INSERT INTO people(id,handle,pic_file,pic_url,first_seen,updated_at) VALUES(?,?,?,?,?,?)',
                         (pid, f'p{pid}', pic_file, pic_url, '2026-01-01', '2026-01-01'))
            if data is not None:
                (self.pfp / f'{pid}.jpg').write_bytes(data)
        conn.commit()
        conn.close()
        (self.pfp / '.4.abcd.tmp').write_bytes(b'partial')
        (self.pfp / '999.jpg').write_bytes(JPEG2)       # a file for a person that does not exist

    def test_states_refetch_buckets_and_orphans(self):
        report = photo_audit.audit(self.db, self.pfp)
        self.assertEqual(report['states'], {'ok': 3, 'damaged_file': 1, 'missing_file': 1,
                                            'file_without_database_link': 1, 'no_photo_with_address': 2,
                                            'no_photo_no_address': 2})
        self.assertEqual(report['refetch'], {'fetchable': 1, 'expired': 1, 'no_url': 2, 'already_retrying': 0})
        self.assertEqual(report['damaged_on_disk'], 1)
        self.assertEqual(report['temp_files']['count'], 1)
        self.assertEqual(report['files_for_unknown_people']['count'], 1)
        self.assertEqual(report['formats'], {'jpeg': 6, 'webp': 1, 'png': 1})
        self.assertEqual(report['_ids']['fetchable'], [4])
        self.assertEqual(report['_ids']['expired'], [5])
        self.assertEqual(report['_ids']['missing_file'], [3])

    def test_duplicates_need_hashing_and_count_the_extra_copies(self):
        self.assertIsInstance(photo_audit.audit(self.db, self.pfp)['duplicates'], str)
        report = photo_audit.audit(self.db, self.pfp, hash_files=True)
        self.assertEqual(report['duplicates']['groups'], 1)
        self.assertEqual(report['duplicates']['extra_files'], 1)
        self.assertEqual(report['duplicates']['extra_bytes'], len(JPEG))

    def test_audit_changes_nothing(self):
        before = sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.pfp.iterdir())
        db_before = self.db.read_bytes()
        photo_audit.audit(self.db, self.pfp, hash_files=True)
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.pfp.iterdir()), before)
        self.assertEqual(self.db.read_bytes(), db_before)

    def test_expiry_reading(self):
        self.assertEqual(photo_audit.url_expiry(url(0x6A2B3C4D)), 0x6A2B3C4D)
        self.assertIsNone(photo_audit.url_expiry('https://x.test/a.jpg'))
        self.assertIsNone(photo_audit.url_expiry('https://x.test/a.jpg?oe=zzz'))

    def test_validity_rule_matches_the_servers(self):
        from backend.photos import PhotoService
        service = PhotoService(None)
        samples = {'jpeg': JPEG, 'jpeg-cut': JPEG[:-1], 'webp': WEBP, 'webp-cut': WEBP[:-1], 'png': PNG,
                   'png-cut': PNG[:-4], 'tiny': b'\xff\xd8\xff', 'empty': b'', 'junk': b'x' * 50,
                   'huge': b'\xff\xd8\xff' + b'z' * (3 * 1024 * 1024) + b'\xff\xd9'}
        for name, data in samples.items():
            path = self.root / f'sample-{name}'
            path.write_bytes(data)
            self.assertEqual(photo_audit.valid_pic_file(path), service.valid_pic_file(path), name)


if __name__ == '__main__':
    unittest.main()
