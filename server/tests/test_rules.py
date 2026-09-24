import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402
import rules  # noqa: E402


def person(**kw):
    return {k: kw.get(k) for k in ('id', 'handle', 'name', 'bio', 'website', 'category')}


def hit(field, match, **kw):
    return rules.matches({'field': field, 'match': match}, person(**kw))


class Matching(unittest.TestCase):
    def test_keywords_are_whole_words_in_text_fields(self):
        self.assertTrue(hit('bio', 'dtc, skin care', bio='Scaling a DTC brand'))
        self.assertTrue(hit('bio', 'skin care', bio='clean  Skin\ncare for all'))  # spaces match any whitespace
        self.assertFalse(hit('bio', 'dtc', bio='dtcx brand'))
        self.assertFalse(hit('bio', 'shop', bio='workshop leader'))
        self.assertTrue(hit('bio', 'vegan*', bio='Vegans welcome'))
        self.assertTrue(hit('name', '@glow', name='Sara | @glow'))
        self.assertTrue(hit('category', 'health/beauty', category='Health/beauty'))

    def test_substring_for_handle_and_website(self):
        self.assertTrue(hit('handle', 'shop', handle='glowshop'))
        self.assertTrue(hit('website', 'myshopify', website='https://glow.myshopify.com'))
        self.assertFalse(hit('website', 'myshopify', website=None))

    def test_any_field_and_regex(self):
        self.assertTrue(hit('any', 'roasters', website='https://northroasters.com'))
        self.assertTrue(hit('any', '/^the\\w+co$/', handle='thecandleco'))
        self.assertFalse(hit('bio', '/^the\\w+co$/', handle='thecandleco'))
        self.assertTrue(hit('bio', '/(?:founder|ceo) @\\w+/', bio='CEO @hydrate'))

    def test_scan_is_capped(self):
        self.assertFalse(hit('bio', 'needle', bio='x' * rules.TEXT_MAX + ' needle'))
        self.assertTrue(hit('bio', 'needle', bio='needle ' + 'x' * 500))

    def test_rejections(self):
        for field, match in (('email', 'x'), ('bio', ''), ('bio', ' , , '), ('bio', '/[unclosed/'), ('bio', '/(a+)+b/'),
                             ('bio', '/(?:ab|cd){3,}/'), ('bio', '/(?P<x>a)(?P=x)/'), ('bio', '/a*b*c*/'), ('bio', 'a*b*'),
                             ('bio', '/' + 'x' * (rules.MAX_REGEX + 1) + '/'), ('bio', ','.join(f'w{i}' for i in range(rules.MAX_WORDS + 1)))):
            with self.subTest(match=match[:40]):
                self.assertRaises(ValueError, rules.compile_match, field, match)
        for match in ('/shop(ify)?/', '/(?:https?://)?www\\./', '/founder.*(?:skincare|beauty)/', '/\\bdtc\\b/', '/(?=.*founder).*/'):
            rules.compile_match('bio', match)


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'r.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def tags(self, pid):
        return sorted((r[0], r[1]) for r in self.conn.execute('SELECT tag, source FROM tags WHERE person_id=?', (pid,)))

    def test_sync_adds_removes_and_leaves_other_sources(self):
        a = db.upsert_person(self.conn, {'handle': 'a', 'bio': 'coffee roasters'})
        b = db.upsert_person(self.conn, {'handle': 'b', 'bio': 'tea'})
        self.conn.execute("INSERT INTO tags VALUES(?, 'Coffee', 'niche', 'manual')", (b,))
        self.conn.execute("INSERT INTO tag_rules(tag, grp, field, match) VALUES('Coffee', 'niche', 'bio', 'coffee, espresso')")
        self.conn.execute("INSERT INTO tag_rules(tag, grp, field, match) VALUES('Broken', 'signal', 'bio', '/(a+)+/')")  # skipped
        rules.sync(self.conn, [a, b, None])
        self.assertEqual(self.tags(a), [('Coffee', 'rule')])
        self.assertEqual(self.tags(b), [('Coffee', 'manual')])
        self.conn.execute("UPDATE people SET bio='tea now' WHERE id=?", (a,))
        self.conn.execute("UPDATE people SET bio='espresso' WHERE id=?", (b,))
        rules.sync(self.conn, [a, b])
        self.assertEqual(self.tags(a), [])
        self.assertEqual(self.tags(b), [('Coffee', 'manual')])  # manual wins, nothing duplicated
        self.assertEqual(len(rules.load(self.conn)), 1)
        self.assertEqual(rules.sync(self.conn, []), 0)


if __name__ == '__main__':
    unittest.main()
