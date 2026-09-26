"""Identity changes must not transfer historical evidence to a new account."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from connection_graph import compare


class IdentityIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = db.init(str(Path(self.tmp.name) / 'identity.sqlite'))

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def person(self, handle, ig_id=None):
        return db.upsert_person(self.c, dict(handle=handle, ig_id=ig_id))

    def seed(self, handle, ig_id=None):
        self.c.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', (handle, ig_id))

    def profile(self, handle):
        return self.c.execute('SELECT * FROM people WHERE handle=?', (handle,)).fetchone()

    def seed_id(self, handle):
        return self.c.execute('SELECT ig_id FROM seeds WHERE handle=?', (handle,)).fetchone()[0]

    def test_blank_ids_never_erase_identity_and_reused_handle_keeps_old_history(self):
        old = self.person('connector', '300')
        for blank in ('', ' ', '\t\n', None):
            self.assertEqual(self.person('connector', blank), old)
            self.assertEqual(self.profile('connector')['ig_id'], '300')
        self.c.execute("INSERT INTO tags VALUES(?,'Already know them','signal','manual')", (old,))
        db.add_edge(self.c, 'alice', old, 'following')
        new = self.person('connector', '999')
        self.assertNotEqual(old, new)
        self.assertEqual(self.profile('connector~' + str(old))['ig_id'], '300')
        self.assertEqual(self.c.execute('SELECT person_id FROM edges').fetchone()[0], old)
        self.assertEqual(self.c.execute('SELECT person_id FROM tags').fetchone()[0], old)

    def test_blank_new_ids_and_page_keys_use_handles(self):
        for index, blank in enumerate(('', ' \t\n', None)):
            self.person('unknown' + str(index), blank)
            self.assertIsNone(self.profile('unknown' + str(index))['ig_id'])
        key = lambda handle, identity: db.list_page_key('brand', 'followers',
                                                       [dict(handle=handle, ig_id=identity)], None)
        self.assertEqual(key('alice', ''), key('alice', None))
        self.assertEqual(key('alice', ' \t'), key('alice', None))
        self.assertNotEqual(key('alice', ''), key('bob', ''))
        self.assertEqual(key('alice', ' 123 '), key('renamed', '123'))

    def test_null_or_blank_seed_preserved_before_handle_reuse(self):
        bob = self.person('bob', '200')
        self.seed('bob', '200')
        for index, missing in enumerate((None, '', ' \t\n')):
            handle = 'alice' + str(index)
            old = self.person(handle, 'old' + str(index))
            self.seed(handle, missing)
            db.add_edge(self.c, handle, bob, 'following')
            self.person(handle, 'new' + str(index))
            self.assertEqual(self.seed_id(handle), 'old' + str(index))
            self.assertEqual(self.profile(handle + '~' + str(old))['ig_id'], 'old' + str(index))
            with self.assertRaises(ValueError):
                compare(self.c, handle, 'bob')

    def test_rename_preserves_departing_seed_and_both_sides_of_handle_collision(self):
        moving = self.person('oldname', '100')
        displaced = self.person('newname', '200')
        self.seed('oldname')
        self.seed('newname', '')
        self.assertEqual(self.person('newname', '100'), moving)
        self.assertEqual(self.seed_id('oldname'), '100')
        self.assertEqual(self.seed_id('newname'), '200')
        self.assertEqual(self.profile('newname~' + str(displaced))['ig_id'], '200')
        self.seed('thirdname')
        self.assertEqual(self.person('thirdname', '100'), moving)
        # A previously established conflicting seed identity is never overwritten.
        self.assertEqual(self.seed_id('newname'), '200')

    def test_rename_to_unused_handle_preserves_seed(self):
        old = self.person('before', '100')
        self.seed('before')
        self.assertEqual(self.person('after', '100'), old)
        self.assertEqual(self.seed_id('before'), '100')

    def test_provisional_person_cannot_override_contradictory_seed_evidence(self):
        old = self.person('alias')
        self.seed('alias', '100')
        db.add_edge(self.c, 'brand', old, 'followers')
        self.person('real', '999')
        for incoming in ('999', '888'):
            with self.assertRaisesRegex(ValueError, 'identity conflicts'):
                self.person('alias', incoming)
            self.assertEqual(self.profile('alias')['id'], old)
            self.assertIsNone(self.profile('alias')['ig_id'])
            self.assertEqual(self.seed_id('alias'), '100')
            self.assertEqual(self.profile('real')['ig_id'], '999')

    def test_list_member_reuse_cannot_transfer_seed_history(self):
        import server
        self.person('alice', '100')
        bob = self.person('bob', '200')
        self.seed('alice')
        self.seed('bob', '200')
        db.add_edge(self.c, 'alice', bob, 'following')
        self.c.commit()
        server.ext_list_page(self.c, {}, dict(seed='collector', direction='followers',
                                             users=[dict(handle='alice', ig_id='999')]))
        self.assertEqual(self.seed_id('alice'), '100')
        self.assertEqual(self.profile('alice')['ig_id'], '999')
        with self.assertRaises(ValueError):
            compare(self.c, 'alice', 'bob')

    def test_list_member_blank_id_preserves_known_identity(self):
        import server
        old = self.person('alice', '100')
        self.c.commit()
        for index, blank in enumerate(('', '\t ')):
            server.ext_list_page(self.c, {}, dict(seed='collector', direction='followers',
                                                 next_cursor=str(index), users=[dict(handle='alice', ig_id=blank)]))
            self.assertEqual(self.profile('alice')['id'], old)
            self.assertEqual(self.profile('alice')['ig_id'], '100')

    def test_profile_blank_id_preserves_known_identity(self):
        import server
        old = self.person('alice', '100')
        self.c.commit()
        for blank in ('', ' \t'):
            server.ext_profile(self.c, {}, dict(profile=dict(handle='alice', ig_id=blank)))
            self.assertEqual(self.profile('alice')['id'], old)
            self.assertEqual(self.profile('alice')['ig_id'], '100')

    def merged_dates(self, left, right):
        self.c.execute('DELETE FROM edge_observations')
        self.c.execute('DELETE FROM edges')
        self.c.execute('DELETE FROM people')
        keep = self.person('original', '100')
        drop = self.person('alias')
        for pid, stamp in ((keep, left), (drop, right)):
            self.c.execute('INSERT INTO edges VALUES(?,?,?,?)', ('brand', pid, 'followers', stamp))
            db.observe_edge(self.c, 'brand', pid, 'followers', 'same', 1, stamp if stamp is not None else '')
        self.assertEqual(self.person('alias', '100'), keep)
        self.assertEqual(self.c.execute('SELECT count(*) FROM people').fetchone()[0], 1)
        return (self.c.execute('SELECT first_seen FROM edges').fetchone()[0],
                self.c.execute('SELECT observed_at FROM edge_observations').fetchone()[0])

    def test_merge_dates_compare_instants_in_both_orders(self):
        early = '2020-01-01T02:00:00+02:00'
        later = '2020-01-01T01:00:00+00:00'
        for pair in ((early, later), (later, early)):
            self.assertEqual(self.merged_dates(*pair), ('2020-01-01T00:00:00+00:00',) * 2)

    def test_merge_prefers_valid_date_over_unknown_source_dates(self):
        valid = '2020-01-01T00:00:00+00:00'
        for unknown in (None, '', 'nonsense', '2019-01-01', '2019-01-01T00:00:00',
                        '9999-01-01T00:00:00+00:00', '0001-01-01T00:00:00+23:00'):
            for pair in ((unknown, valid), (valid, unknown)):
                with self.subTest(pair=pair):
                    self.assertEqual(self.merged_dates(*pair), (valid, valid))

    def test_merge_preserves_unknown_legacy_literals_without_inventing_dates(self):
        self.assertEqual(self.merged_dates('2020-01-02', '2020-01-01'), ('2020-01-01',) * 2)
        self.assertEqual(self.merged_dates(None, 'unknown'), ('unknown',) * 2)
        self.assertEqual(self.merged_dates('unknown', None), ('unknown',) * 2)
        self.assertEqual(self.merged_dates(None, None), (None, ''))


if __name__ == '__main__':
    unittest.main()
