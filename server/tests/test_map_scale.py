"""Dense map keeps identities exact and bounds the point payload."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import map_scale  # noqa: E402
import server  # noqa: E402


class DenseMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'map.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_exact_people_memberships_filters_and_overlapping_buckets(self):
        a = db.upsert_person(self.conn, {'handle': 'alice', 'followers': 1200})
        b = db.upsert_person(self.conn, {'handle': 'bob', 'followers': 200})
        c = db.upsert_person(self.conn, {'handle': 'carol', 'followers': 5000})
        db.add_edge(self.conn, 'alpha', a, 'followers')
        db.add_edge(self.conn, 'beta', a, 'following')
        db.add_edge(self.conn, 'alpha', b, 'followers')
        db.add_edge(self.conn, 'alpha', c, 'followers', observed=False)
        self.conn.execute('UPDATE seeds SET is_me=0')
        self.conn.execute("INSERT OR IGNORE INTO seeds(handle,is_me) VALUES('alpha',1)")
        self.conn.execute("UPDATE seeds SET is_me=1 WHERE handle='alpha'")
        self.conn.execute("INSERT INTO tags(person_id,tag,grp,source) VALUES(?,?,?,?)", (a, 'Founder', 'role', 'manual'))
        self.conn.commit()
        q = {'scope': ['all'], 'followers_min': ['1000']}
        where, args = server.lead_filter(q)
        graph = map_scale.graph(self.conn, q, where, args, 7, 100000)
        leads = [n for n in graph['nodes'] if n['kind'] == 'lead']
        self.assertEqual((graph['total'], len(leads), graph['limit']), (1, 1, 100000))
        self.assertEqual((leads[0]['id'], set(leads[0]['seeds'])), ('p:' + str(a), {'alpha', 'beta'}))
        self.assertEqual((leads[0]['owner_relationship'], leads[0]['relationship_owner']), ('follows', 'alpha'))
        self.assertEqual(graph['links'], [])
        self.assertFalse(any(n['id'] == 'p:' + str(c) for n in graph['nodes']))
        summary = map_scale.overview(self.conn, 7)
        self.assertEqual(summary['total'], 2)
        self.assertEqual({row['seed']: row['count'] for row in summary['buckets']}, {'alpha': 2, 'beta': 1})
        self.assertTrue(summary['overlapping_lists'])
        tagged = {'scope': ['all'], 'tags': ['Founder']}
        where, args = server.lead_filter(tagged)
        self.assertEqual(map_scale.graph(self.conn, tagged, where, args, 7, 100000)['total'], 1)
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,?)", (b, 'no'))
        self.conn.commit()
        where, args = server.lead_filter({'scope': ['all']})
        self.assertEqual(map_scale.graph(self.conn, {'scope': ['all']}, where, args, 8, 100000)['total'], 1)

    def test_limit_is_bounded_and_ranking_is_deterministic(self):
        for i in range(15):
            pid = db.upsert_person(self.conn, {'handle': f'person{i}'})
            db.add_edge(self.conn, 'alpha', pid, 'followers')
        self.conn.commit()
        q = {'scope': ['all']}
        where, args = server.lead_filter(q)
        graph = map_scale.graph(self.conn, q, where, args, 9, 10)
        self.assertEqual(graph['total'], 15)
        self.assertEqual([n['id'] for n in graph['nodes'] if n['kind'] == 'lead'],
                         [f'p:{i}' for i in range(1, 11)])
        routed = server.api_map(self.conn, {'scope': ['all'], 'limit': ['100000']}, None)
        self.assertEqual((routed['total'], routed['limit'], routed['dense']), (15, 100000, True))
        self.assertEqual(len([n for n in routed['nodes'] if n['kind'] == 'lead']), 15)


if __name__ == '__main__':
    unittest.main()
