"""View-mode cohorts: /api/map/view?cohort=1&lens=<id> (map_lenses.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import map_layout as ML  # noqa: E402
import map_lenses  # noqa: E402
import map_view as MV  # noqa: E402

TS = '2026-01-01T00:00:00+00:00'


def person(conn, handle, followers=None, tier=None, score=None):
    pid = db.upsert_person(conn, {'handle': handle, 'name': handle.title(), 'followers': followers})
    if tier:
        conn.execute('INSERT INTO verdicts(person_id,score,tier,content_fit) VALUES(?,?,?,NULL)', (pid, score, tier))
    return pid


class LensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'map.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        self.addCleanup(MV.close_pool)
        self.addCleanup(map_lenses.clear_cache)
        c = self.conn
        c.execute("INSERT INTO seeds(handle,is_me) VALUES('fortun8te',1)")
        self.owner = person(c, 'fortun8te')
        self.a = person(c, 'amy', 5000, 'warm', 60)          # mutual follow, a collected source
        self.b = person(c, 'ben', 2_000_000, 'hot', 85)      # owner follows, a collected source
        self.cc = person(c, 'cal', 900)                      # follows the owner, not collected
        self.x = person(c, 'xena', 150_000, 'hot', 80)       # in two connections' lists
        self.y = person(c, 'yuri', 800, 'warm', 55)          # in one connection's list
        self.z = person(c, 'zed', None, 'hot', 75)           # in no list of a connection, unknown followers
        self.q = person(c, 'quin', 40)                       # only seen by a stranger
        for handle, direction, pid in (('fortun8te', 'followers', self.a), ('fortun8te', 'following', self.a),
                                       ('fortun8te', 'following', self.b), ('fortun8te', 'followers', self.cc)):
            db.add_edge(c, handle, pid, direction)
        for handle, pid in (('amy', self.x), ('amy', self.y), ('ben', self.x)):
            db.add_edge(c, handle, pid, 'following')
        self.stranger = person(c, 'stranger', 10)
        for pid in (self.x, self.z):
            db.add_edge(c, 'stranger', pid, 'following')
        db.add_edge(c, 'stranger', self.q, 'following')
        c.commit()
        ML.build(self.path)

    def page(self, lens, **extra):
        q = {'mode': ['closeness'], 'cohort': ['1'], 'lens': [lens], 'scope': ['all'], 'budget': ['50']}
        q.update({k: [v] for k, v in extra.items()})
        return json.loads(MV.view(self.conn, self.path, q, cache=False).body)

    def ids(self, lens, **extra):
        return [n['id'] for n in self.page(lens, **extra)['nodes']]

    def test_reach_lists_only_people_with_a_saved_count_largest_first(self):
        result = self.page('reach')
        self.assertEqual(self.ids('reach'), [self.b, self.x, self.a, self.cc, self.y, self.q, self.stranger])
        self.assertEqual(result['total'], 7)
        self.assertEqual(result['lens']['id'], 'reach')
        self.assertNotIn(self.z, self.ids('reach'), 'unknown follower counts never rank')
        self.assertNotIn(self.owner, self.ids('reach'))

    def test_warm_paths_exclude_direct_connections_and_count_links(self):
        result = self.page('warm')
        self.assertEqual([(n['id'], n['links']) for n in result['nodes']], [(self.x, 2), (self.y, 1)])
        self.assertTrue(all(not (n['followed'] or n['follows_me']) for n in result['nodes']))
        self.assertEqual(result['total'], 2)

    def test_a_link_needs_a_connection_not_just_any_collected_source(self):
        self.assertNotIn(self.q, self.ids('warm'), 'seen only by a stranger the owner is not linked to')
        self.assertNotIn(self.z, self.ids('warm'))

    def test_shared_audiences_need_two_or_more_sources(self):
        self.assertEqual(self.ids('shared'), [self.x])
        node = self.page('shared')['nodes'][0]
        self.assertGreaterEqual(node['source_count'], 2)

    def test_promising_leads_are_hot_or_warm_under_the_follower_ceiling_nearest_first(self):
        result = self.page('leads')
        ids = [n['id'] for n in result['nodes']]
        self.assertEqual(set(ids), {self.a, self.y, self.z})
        self.assertEqual(ids[0], self.a, 'a mutual follow comes first')
        self.assertNotIn(self.b, ids, 'above 100,000 followers')
        self.assertNotIn(self.x, ids)
        by_id = {n['id']: n for n in result['nodes']}
        self.assertEqual(by_id[self.a]['priority'], 60)
        self.assertIsNone(by_id[self.z]['followers'], 'unknown counts stay unknown and are allowed')

    def test_filters_apply_inside_a_lens(self):
        self.assertEqual(self.ids('leads', follow='mutual'), [self.a])
        self.assertEqual(self.page('leads', follow='mutual')['total'], 1)
        self.assertEqual(self.ids('reach', follow='followers'), [self.a, self.cc])

    def test_every_lens_keeps_the_default_page_shape(self):
        default = json.loads(MV.view(self.conn, self.path, {'mode': ['closeness'], 'cohort': ['1'], 'scope': ['all']}, cache=False).body)
        for lens in map_lenses.LENSES:
            result = self.page(lens)
            self.assertEqual(set(default) - {'lens'}, set(result) - {'lens'}, lens)
            self.assertTrue(result['cohort'] and result['ready'])
            self.assertEqual(result['world']['me']['id'], self.owner)
            self.assertTrue(all('x' in n and 'y' in n and 'followers' in n for n in result['nodes']))

    def test_unknown_lens_and_text_search_are_refused(self):
        with self.assertRaises(ValueError):
            MV.Query({'cohort': ['1'], 'lens': ['nonsense']})
        with self.assertRaises(ValueError):
            MV.Query({'cohort': ['1'], 'lens': ['reach'], 'q': ['amy']})
        with self.assertRaises(ValueError):
            MV.Query({'lens': ['reach']})

    def test_lenses_are_not_confused_by_the_response_cache(self):
        q = {'mode': ['closeness'], 'cohort': ['1'], 'scope': ['all'], 'budget': ['50']}
        bodies = {lens: MV.view(self.conn, self.path, dict(q, lens=[lens]), cache=True).body for lens in ('reach', 'warm', 'reach')}
        self.assertNotEqual(bodies['reach'], bodies['warm'])
        self.assertEqual(MV.view(self.conn, self.path, dict(q, lens=['reach']), cache=True).body, bodies['reach'])

    def test_compact_pages_carry_links_and_priority_columns(self):
        q = {'mode': ['closeness'], 'cohort': ['1'], 'lens': ['leads'], 'compact': ['1'], 'scope': ['all'], 'budget': ['50']}
        result = json.loads(MV.view(self.conn, self.path, q, cache=False).body)
        self.assertEqual(result['columns'][-2:], ['links', 'priority'])
        by_id = {row[0]: row for row in result['rows']}
        self.assertEqual(by_id[self.a][-1], 60)
        self.assertEqual(len(result['columns']), len(result['rows'][0]))


class LensPagingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'paging.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        self.addCleanup(MV.close_pool)
        self.addCleanup(map_lenses.clear_cache)
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('fortun8te',1)")
        person(self.conn, 'fortun8te')
        self.followers = {}
        for i in range(130):
            pid = person(self.conn, f'user{i:03}', 1000 + (i * 7) % 130)   # distinct counts, shuffled ids
            self.followers[pid] = 1000 + (i * 7) % 130
        self.conn.commit()
        ML.build(self.path)

    def get(self, **extra):
        q = {'mode': ['closeness'], 'cohort': ['1'], 'lens': ['reach'], 'scope': ['all'], 'budget': ['50']}
        q.update({k: [v] for k, v in extra.items()})
        return json.loads(MV.view(self.conn, self.path, q, cache=False).body)

    def test_pages_cover_everyone_once_in_rank_order(self):
        seen, after, rounds = [], None, 0
        while True:
            result = self.get(after=after) if after else self.get()
            seen.extend(n['followers'] for n in result['nodes'])
            after = result['next_cursor']
            rounds += 1
            if not after:
                break
        self.assertEqual(rounds, 3)
        self.assertEqual(len(seen), 130)
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertEqual(len(set(seen)), 130)

    def test_a_cursor_from_another_filter_set_restarts_instead_of_skipping(self):
        first = self.get()
        again = self.get(after=first['next_cursor'], scope='leads')
        self.assertTrue(again['cohort_reset'])
        self.assertIn('lens:reach:', first['next_cursor'])

    def test_last_page_has_no_cursor_and_total_is_exact(self):
        last = self.get(after=self.get(after=self.get()['next_cursor'])['next_cursor'])
        self.assertIsNone(last['next_cursor'])
        self.assertEqual(last['total'], 130)
        self.assertEqual(len(last['nodes']), 30)


if __name__ == '__main__':
    unittest.main()
