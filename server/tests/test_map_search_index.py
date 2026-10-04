"""Ranked, indexed map search (map_search.py): folding, ranking, typos, bios, sync, API shape."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import map_layout as ML  # noqa: E402
import map_search as MS  # noqa: E402
import map_view as MV  # noqa: E402


class FoldingTest(unittest.TestCase):
    def test_fold_removes_case_accents_and_fancy_letters(self):
        self.assertEqual(MS.fold('André Ølsen ZÉ'), 'andre olsen ze')
        self.assertEqual(MS.fold('𝗗𝗮𝗻𝗶𝗲𝗹 ℬ𝓸𝓭𝓲𝓷𝒆'), 'daniel bodine')
        self.assertEqual(MS.fold('Straße'), 'strasse')

    def test_parse_reduces_handles_links_and_separators(self):
        for typed in ('@Cerave', ' cerave ', 'https://www.instagram.com/cerave/?igsh=1', 'instagram.com/cerave'):
            self.assertEqual(MS.parse(typed).compact, 'cerave', typed)
        q = MS.parse('Andre.Creates')
        self.assertEqual((q.text, q.tokens, q.compact, q.key), ('andre.creates', ['andre', 'creates'], 'andrecreates', 'andre creates'))
        self.assertFalse(MS.parse('  @ '))
        self.assertFalse(MS.parse(''))

    def test_posts_other_sites_and_huge_input_are_refused(self):
        for typed in ('https://instagram.com/p/abc123', 'https://example.com/noah', 'a' * 513):
            with self.assertRaises(ValueError):
                MS.parse(typed)

    def test_distance_counts_swaps_as_one_edit(self):
        self.assertEqual(MS.distance('cerave', 'cearve', 1), 1)
        self.assertEqual(MS.distance('cerave', 'cerrave', 1), 1)
        self.assertEqual(MS.distance('cerave', 'cerxve', 1), 1)
        self.assertEqual(MS.distance('chambers', 'chamners', 2), 1)
        self.assertGreater(MS.distance('cerave', 'tarot', 2), 2)


def people(conn, rows):
    ids = {}
    for handle, name, bio, followers in rows:
        ids[handle] = db.upsert_person(conn, {'handle': handle, 'name': name, 'bio': bio, 'followers': followers})
    conn.commit()
    return ids


class SearchBase(unittest.TestCase):
    rows = []
    layout = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'search.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        self.addCleanup(MV.close_pool)
        self.addCleanup(MS.forget)
        MS.forget()
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('fortun8te',1)")
        self.ids = people(self.conn, [('fortun8te', 'Michael', None, None), *self.rows])
        if self.layout:
            ML.build(self.path)

    def search(self, text, **extra):
        q = {'q': [text]}
        q.update({k: [str(v)] for k, v in extra.items()})
        return MS.search(self.conn, self.path, q)

    def handles(self, text, **extra):
        return [r['handle'] for r in self.search(text, **extra)['results']]


class RankingTest(SearchBase):
    rows = [
        ('noah', 'Somebody Else', None, 10),            # exact handle
        ('noahbrands', 'Noah Brands', None, 500),        # handle prefix and exact name
        ('xnoahx', 'Unrelated', None, 900),              # contains in the handle
        ('someone', 'Noah Smith', None, 20),             # name prefix
        ('person', 'Big Noah Fan', None, 30),            # whole word inside the name
        ('noahsark', 'Ark', None, 40),                   # handle prefix
        ('bioonly', 'Writer', 'I help brands. Ask Noah about skincare', 50),
        ('noha', 'Nope', None, 60),                      # one letter off
    ]

    def test_tiers_come_in_order(self):
        order = self.handles('noah')
        self.assertEqual(order[0], 'noah')
        self.assertLess(order.index('noahbrands'), order.index('someone'))
        self.assertLess(order.index('noahsark'), order.index('someone'))
        self.assertLess(order.index('someone'), order.index('person'))
        self.assertLess(order.index('person'), order.index('xnoahx'))
        self.assertLess(order.index('xnoahx'), order.index('bioonly'))
        self.assertNotIn('noha', order, 'close spellings only appear when nothing matches better')

    def test_every_result_says_how_it_matched(self):
        found = {r['handle']: r['match'] for r in self.search('noah')['results']}
        self.assertEqual(found['noah'], {'field': 'handle', 'kind': 'exact'})
        self.assertEqual(found['noahsark'], {'field': 'handle', 'kind': 'prefix'})
        self.assertEqual(found['someone'], {'field': 'name', 'kind': 'prefix'})
        self.assertEqual(found['person'], {'field': 'name', 'kind': 'word'})
        self.assertEqual(found['xnoahx'], {'field': 'handle', 'kind': 'contains'})
        self.assertEqual(found['bioonly'], {'field': 'bio', 'kind': 'words'})

    def test_close_spelling_is_the_last_resort_and_says_so(self):
        found = {r['handle']: r['match'] for r in self.search('noaah')['results']}
        self.assertEqual(found['noah'], {'field': 'name', 'kind': 'close'})
        self.assertIn('noah', self.handles('noaah'))

    def test_bio_hits_carry_a_snippet_around_the_words(self):
        hit = next(r for r in self.search('skincare')['results'] if r['handle'] == 'bioonly')
        self.assertIn('skincare', hit['snippet'].lower())

    def test_followers_break_ties_when_closeness_is_equal(self):
        # noahbrands (500 followers) and noahsark (40) are both handle prefixes; the bigger account first.
        order = self.handles('noah')
        self.assertLess(order.index('noahbrands'), order.index('noahsark'))


class IdentityMatchingTest(SearchBase):
    rows = [
        ('andreelmenhorst', 'André Elmenhorst', 'oslo, norway', 102030),
        ('_zemoreira_', 'Zé Moreira', 'braga', 1144),
        ('andre.creates', 'André Otoo', 'Helping coaches print', 194),
        ('chambersofhealth', 'Matthew Chambers', 'Supplements for joints', 17480),
        ('cerave', 'CeraVe', 'Developed with dermatologists. #1 dermatologist recommended skincare brand', 1574045),
        ('matthews_cafe', 'Matthews Café', None, 90),
        ('wxnter', 'Wxnter❄️', None, 269),
    ]

    def test_plain_letters_find_accented_names_and_the_reverse(self):
        self.assertEqual(self.handles('andre elmenhorst'), ['andreelmenhorst'])
        self.assertEqual(self.handles('ze moreira'), ['_zemoreira_'])
        self.assertIn('andreelmenhorst', self.handles('André'))
        self.assertIn('matthews_cafe', self.handles('matthews cafe'))
        self.assertIn('matthews_cafe', self.handles('CAFÉ'))

    def test_word_order_and_partial_words_do_not_matter(self):
        self.assertEqual(self.handles('chambers matthew')[0], 'chambersofhealth')
        self.assertEqual(self.handles('matt cham')[0], 'chambersofhealth')

    def test_dots_underscores_and_spaces_are_interchangeable(self):
        for typed in ('andre creates', 'andre.creates', 'andrecreates', '@Andre_Creates'):
            self.assertEqual(self.handles(typed)[0], 'andre.creates', typed)
        self.assertEqual(self.handles('zemoreira')[0], '_zemoreira_')

    def test_fragments_of_a_handle_are_found(self):
        self.assertEqual(self.handles('rsofhea')[0], 'chambersofhealth')
        self.assertEqual(self.handles('eates')[0], 'andre.creates')

    def test_emoji_in_a_name_do_not_block_the_word(self):
        self.assertEqual(self.handles('wxnter')[0], 'wxnter')

    def test_typos_are_forgiven(self):
        self.assertEqual(self.handles('cerrave')[0], 'cerave')       # extra letter
        self.assertEqual(self.handles('cerve')[0], 'cerave')         # missing letter
        self.assertEqual(self.handles('cearve')[0], 'cerave')        # swapped letters
        self.assertEqual(self.handles('matthew chamners')[0], 'chambersofhealth')
        self.assertEqual(self.handles('elmenhrst'), ['andreelmenhorst'])

    def test_unrelated_text_matches_nothing(self):
        self.assertEqual(self.handles('qqqqzzzz'), [])
        self.assertEqual(self.handles('%'), [])
        self.assertEqual(self.handles("' OR 1=1 --"), [])
        self.assertEqual(self.handles('"'), [])

    def test_bio_words_find_people_when_names_do_not(self):
        self.assertEqual(self.handles('dermatologist skincare')[0], 'cerave')
        self.assertEqual(self.handles('norway')[0], 'andreelmenhorst')

    def test_one_letter_queries_return_the_best_prefix_matches(self):
        result = self.handles('c')
        self.assertIn('cerave', result)
        self.assertIn('chambersofhealth', result)

    def test_empty_and_blank_queries_return_nothing(self):
        self.assertEqual(self.search('')['results'], [])
        self.assertEqual(self.search(' @ ')['results'], [])

    def test_limit_is_respected(self):
        for i in range(40):
            db.upsert_person(self.conn, {'handle': f'sam{i:02}', 'name': f'Sam {i}'})
        self.conn.commit()
        self.assertEqual(len(self.search('sam')['results']), MS.LIMIT_DEFAULT)
        self.assertEqual(len(self.search('sam', limit=5)['results']), 5)
        self.assertEqual(len(self.search('sam', limit=100)['results']), MS.LIMIT_MAX)


class ClosenessTieBreakTest(SearchBase):
    layout = True
    rows = [('sam_stranger', 'Sam', None, 90000), ('sam_follows_me', 'Sam', None, 100),
            ('sam_mutual', 'Sam', None, 10), ('sam_i_follow', 'Sam', None, 50)]

    def setUp(self):
        super().setUp()
        c = self.conn
        db.add_edge(c, 'fortun8te', self.ids['sam_mutual'], 'followers')
        db.add_edge(c, 'fortun8te', self.ids['sam_mutual'], 'following')
        db.add_edge(c, 'fortun8te', self.ids['sam_follows_me'], 'followers')
        db.add_edge(c, 'fortun8te', self.ids['sam_i_follow'], 'following')
        c.commit()
        ML.build(self.path)

    def test_closer_people_come_first_even_with_fewer_followers(self):
        result = self.search('sam')['results']
        self.assertEqual([r['handle'] for r in result], ['sam_mutual', 'sam_follows_me', 'sam_i_follow', 'sam_stranger'])
        self.assertEqual([r['relation'] for r in result], ['mutual', 'follows_you', 'you_follow', None])

    def test_result_rows_carry_what_the_map_needs(self):
        row = self.search('sam_mutual')['results'][0]
        for key in ('id', 'handle', 'name', 'pic', 'followers', 'source_count', 'relation', 'followed', 'follows_me',
                    'following_evidence', 'fit', 'status', 'lead', 'closeness', 'pending', 'positions', 'match'):
            self.assertIn(key, row)
        self.assertEqual(row['followers'], 10)
        self.assertIn('closeness', row['positions'])
        self.assertFalse(row['pending'])
        self.assertTrue(row['followed'] and row['follows_me'])

    def test_owner_is_findable_and_marked(self):
        row = self.search('fortun8te')['results'][0]
        self.assertEqual(row['relation'], 'you')


class KeepingUpTest(SearchBase):
    rows = [('alpha', 'Alpha One', 'first bio', 5)]

    def test_new_people_edits_and_removals_show_up(self):
        self.assertEqual(self.handles('alpha'), ['alpha'])
        index = MS.index_for(self.path)
        time.sleep(0.01)
        db.upsert_person(self.conn, {'handle': 'brandnew', 'name': 'Shiny Newcomer'})
        self.conn.execute("UPDATE people SET name='Renamed Person', bio='now sells candles', updated_at=? WHERE handle='alpha'", (db.now(),))
        self.conn.commit()
        index.last_sync = 0
        self.assertEqual(self.handles('shiny newcomer'), ['brandnew'])
        self.assertEqual(self.handles('renamed'), ['alpha'])
        self.assertEqual(self.handles('candles'), ['alpha'])
        self.assertEqual(self.handles('one'), [], 'the old name is gone from the index')
        self.assertEqual(self.handles('first bio'), [])

    def test_deleted_people_never_appear_and_trigger_a_rebuild(self):
        self.assertEqual(self.handles('alpha'), ['alpha'])
        self.conn.execute("DELETE FROM people WHERE handle='alpha'")
        self.conn.commit()
        self.assertEqual(self.handles('alpha'), [])
        index = MS.index_for(self.path)
        self.assertTrue(index.reconcile(self.conn))
        self.assertEqual(self.handles('alpha'), [])

    def test_index_is_a_separate_file_and_the_main_database_is_untouched(self):
        self.search('alpha')
        index = MS.index_for(self.path)
        self.assertTrue(index.path.exists())
        self.assertNotEqual(str(index.path), self.path)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any(name.startswith(('f_tri', 'f_word', 'f_bio', 'entry')) for name in tables))


class FallbackWithoutFts5Test(SearchBase):
    rows = RankingTest.rows + IdentityMatchingTest.rows

    def setUp(self):
        self.addCleanup(setattr, MS, 'FTS5', MS.FTS5)
        MS.FTS5 = False
        super().setUp()

    def test_plain_tables_still_rank_and_fold(self):
        self.assertFalse(MS.index_for(self.path).fts)
        self.assertEqual(self.handles('noah')[0], 'noah')
        self.assertEqual(self.handles('andre elmenhorst')[0], 'andreelmenhorst')
        self.assertEqual(self.handles('chambers matthew')[0], 'chambersofhealth')
        self.assertEqual(self.handles('rsofhea')[0], 'chambersofhealth')
        self.assertEqual(self.handles('norway')[0], 'andreelmenhorst')
        self.assertEqual(self.handles('andre creates')[0], 'andre.creates')

    def test_sync_works_without_fts5(self):
        self.search('noah')
        index = MS.index_for(self.path)
        db.upsert_person(self.conn, {'handle': 'latecomer', 'name': 'Late Comer'})
        self.conn.commit()
        index.last_sync = 0
        self.assertEqual(self.handles('late comer')[0], 'latecomer')


class ScaleTest(SearchBase):
    """Name search used to cover only the top of the layout; every saved person must be reachable."""

    def setUp(self):
        super().setUp()
        rows = [(f'h{i:05}x', f'Person Number{i:05} Sur{i % 97}', None, i % 1000) for i in range(6000)]
        self.conn.executemany('INSERT INTO people(handle,name,followers,first_seen,updated_at) VALUES(?,?,?,?,?)',
                              [(h, n, f, db.now(), db.now()) for h, n, _, f in rows])
        self.conn.commit()

    def test_every_person_is_found_by_exact_name_and_by_handle_fragment(self):
        for i in (0, 1, 2999, 5999):
            self.assertEqual(self.handles(f'Person Number{i:05} Sur{i % 97}')[0], f'h{i:05}x')
            self.assertEqual(self.handles(f'h{i:05}')[0], f'h{i:05}x')
            self.assertIn(f'h{i:05}x', self.handles(f'{i:05}x'))

    def test_queries_stay_fast_on_a_populated_index(self):
        self.search('warm up')
        queries = ['person number00042', 'numbr00042 persn', 'h0100', '0042x', 'sur5', 'zzz', 'p', 'h0012x person']
        began = time.process_time()
        for text in queries * 5:
            self.search(text)
        per_query = (time.process_time() - began) / (len(queries) * 5)
        self.assertLess(per_query, 0.05)


if __name__ == '__main__':
    unittest.main()
