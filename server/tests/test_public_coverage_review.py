"""Importer-to-canonical-DB coverage using only temporary local databases."""
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import public_bios


class PublicCoverageReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'coverage.sqlite')
        public_bios.init(self.conn)
        db.set_setting(self.conn, 'public_bios', {**public_bios.DEFAULTS, 'on': True})
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','person',?)", (db.now(),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def item(self, **changes):
        result = {'id': '123', 'username': 'person', 'biography': 'A bio',
                  '_scrape': {'bioStatus': 'observed', 'observedAt': db.now(),
                              'cacheHit': False, 'source': 'test'}}
        result.update(changes)
        return result

    def import_once(self, item):
        job = public_bios.lease(self.conn, 'test')
        self.assertIsNotNone(job)
        return public_bios.import_item(self.conn, job, item)

    def test_private_and_observed_empty_are_saved(self):
        item = self.item(private=True, biography='')
        item['_scrape']['bioStatus'] = 'empty'
        self.assertTrue(self.import_once(item))
        row = self.conn.execute('SELECT ig_id,handle,is_private,bio,bio_at FROM people').fetchone()
        self.assertEqual(tuple(row)[:4], ('123', 'person', 1, ''))
        self.assertIsNotNone(row['bio_at'])
        self.assertEqual(public_bios.status(self.conn)['successes'], 1)

    def test_unavailable_bio_keeps_header_but_is_not_a_success(self):
        item = self.item(fullName='Known header', biography=None)
        item['_scrape']['bioStatus'] = 'unavailable'
        self.assertFalse(self.import_once(item))
        row = self.conn.execute('SELECT ig_id,name,bio_at FROM people').fetchone()
        self.assertEqual(tuple(row), ('123', 'Known header', None))
        self.assertEqual(public_bios.status(self.conn)['successes'], 0)

    def test_missing_id_cannot_report_success_without_a_saved_person(self):
        self.assertFalse(self.import_once(self.item(id=None)))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)
        self.assertEqual(public_bios.status(self.conn)['successes'], 0)

    def test_same_handle_wrong_stable_id_cannot_displace_existing_person(self):
        db.upsert_person(self.conn, {'handle': 'person', 'ig_id': '123', 'bio': 'Known', 'bio_at': db.now()})
        self.conn.commit()
        self.assertFalse(self.import_once(self.item(id='456')))
        self.assertEqual(tuple(self.conn.execute('SELECT ig_id,handle,bio FROM people').fetchone()),
                         ('123', 'person', 'Known'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 1)

    def test_known_stable_id_can_follow_rename(self):
        db.upsert_person(self.conn, {'handle': 'person', 'ig_id': '123'})
        self.conn.commit()
        self.assertTrue(self.import_once(self.item(username='renamed')))
        self.assertEqual(tuple(self.conn.execute('SELECT ig_id,handle,bio FROM people').fetchone()),
                         ('123', 'renamed', 'A bio'))

    def test_rename_without_known_id_is_rejected(self):
        self.assertFalse(self.import_once(self.item(username='unrelated')))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_invalid_fields_do_not_poison_canonical_row(self):
        item = self.item(fullName={'bad': 'name'}, private='false', followersCount='100',
                         profilePicUrl=['bad'], postsCount=-1, verified=False)
        self.assertTrue(self.import_once(item))
        row = self.conn.execute('SELECT name,is_private,followers,pic_url,posts,is_verified,bio FROM people').fetchone()
        self.assertEqual(tuple(row), (None, None, None, None, None, 0, 'A bio'))

    def test_invalid_observation_time_cannot_be_marked_success(self):
        for bad in (None, 'invalid', (db.utc_now() + timedelta(days=2)).isoformat()):
            with self.subTest(bad=bad):
                item = self.item()
                item['_scrape']['observedAt'] = bad
                self.assertFalse(self.import_once(item))
                self.assertEqual(public_bios.status(self.conn)['successes'], 0)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)
                self.conn.execute('UPDATE public_bio_retries SET next_at=NULL')
                self.conn.commit()

    def test_old_cache_does_not_create_new_identity(self):
        item = self.item()
        item['_scrape'].update(cacheHit=True, observedAt=(db.utc_now() - timedelta(days=2)).isoformat())
        self.assertFalse(self.import_once(item))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
