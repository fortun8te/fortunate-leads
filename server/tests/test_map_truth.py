"""Map evidence and sampling contracts, using only an isolated database."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import server  # noqa: E402


class MapTruthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'map.sqlite')
        self.old_db = server.CFG['db']
        server.CFG['db'] = self.path
        server.clear_caches()
        self.conn = db.init(self.path)

    def tearDown(self):
        self.conn.close()
        server.CFG['db'] = self.old_db
        server.clear_caches()
        self.tmp.cleanup()

    def person(self, handle):
        return db.upsert_person(self.conn, {'handle': handle})

    def test_current_counts_overlap_and_history_are_separate(self):
        both = self.person('both')
        stale = self.person('stale')
        db.add_edge(self.conn, 'alpha', both, 'followers')
        db.add_edge(self.conn, 'alpha', both, 'following')
        db.add_edge(self.conn, 'beta', both, 'followers')
        db.add_edge(self.conn, 'gamma', both, 'following', observed=False)
        db.add_edge(self.conn, 'alpha', stale, 'followers')
        self.conn.execute('UPDATE edge_evidence SET active=0 WHERE person_id=?', (stale,))
        self.conn.execute('UPDATE edge_evidence SET active=0 WHERE seed=? AND person_id=? AND direction=?',
                          ('alpha', both, 'following'))
        self.conn.commit()

        graph = server.api_map(self.conn, {'scope': ['all']}, None)
        lead = next(n for n in graph['nodes'] if n['id'] == f'p:{both}')
        seeds = {n['label']: n for n in graph['nodes'] if n['kind'] == 'seed'}
        self.assertEqual((graph['total'], lead['degree'], lead['seeds']), (1, 2, ['alpha', 'beta']))
        self.assertFalse(any(n['id'] == f'p:{stale}' for n in graph['nodes']))
        self.assertEqual(seeds['alpha']['degree'], 1)  # two directions still mean one person
        self.assertEqual(graph['seed_links'], [{'source': 's:alpha', 'target': 's:beta', 'shared': 1}])
        states = {(l['source'], l['direction']): l['state'] for l in graph['links'] if l['target'] == f'p:{both}'}
        self.assertEqual(states, {('s:alpha', 'followers'): 'observed', ('s:alpha', 'following'): 'absent',
                                  ('s:beta', 'followers'): 'observed', ('s:gamma', 'following'): 'unverified'})
        self.assertEqual(server.map_graph(self.conn, {'seed': ['gamma']})['total'], 0)
        self.assertEqual(server.map_graph(self.conn, {'seed': ['beta']})['total'], 1)

    def test_total_reports_matching_people_beyond_display_limit(self):
        for i in range(12):
            db.add_edge(self.conn, 'alpha', self.person(f'person{i}'), 'followers')
        self.conn.commit()
        graph = server.map_graph(self.conn, {'scope': ['all'], 'limit': ['10']})
        self.assertEqual(graph['total'], 12)
        self.assertEqual(graph['limit'], 10)
        self.assertEqual(len([n for n in graph['nodes'] if n['kind'] == 'lead']), 10)

    def test_historical_source_identity_is_not_duplicated_as_a_lead(self):
        source = self.person('retired_source')
        person = self.person('person')
        db.add_edge(self.conn, 'retired_source', person, 'followers', observed=False)
        db.add_edge(self.conn, 'alpha', source, 'followers')
        self.conn.commit()
        graph = server.map_graph(self.conn, {})
        self.assertEqual(graph['total'], 0)
        self.assertEqual([n['id'] for n in graph['nodes'] if n.get('pid') == source], ['s:retired_source'])
        self.assertEqual(graph['links'][0]['target'], 's:retired_source')

    def test_api_50000_people_stays_bounded_and_counts_distinct_current_connections(self):
        count = 50000
        stamp = '2026-09-01T00:00:00Z'
        self.conn.executemany('INSERT INTO people(id,handle,name,followers,first_seen,updated_at) VALUES(?,?,?,?,?,?)',
                              ((i, f'person{i:05d}', f'Person {i}', i * 11, stamp, stamp) for i in range(1, count + 1)))
        self.conn.executemany('INSERT INTO seeds(handle,added_at) VALUES(?,?)',
                              ((f'source{i:02d}', stamp) for i in range(20)))
        self.conn.executemany('INSERT INTO verdicts(person_id,score,tier,content_fit,reason) VALUES(?,?,?,?,?)',
                              ((i, i % 101, 'maybe', i % 101, 'Synthetic fixture') for i in range(1, count + 1)))
        self.conn.executemany('INSERT INTO tags(person_id,tag,grp,source) VALUES(?,?,?,?)',
                              ((i, 'Fixture', 'signal', 'manual') for i in range(10, count + 1, 10)))
        self.conn.executemany('INSERT INTO marks(person_id,status,note) VALUES(?,?,?)',
                              ((i, 'interested', 'Synthetic note') for i in range(17, count + 1, 17)))
        edges, evidence = [], []
        for i in range(1, count + 1):
            current = [(f'source{i % 20:02d}', 'followers')]
            if i % 3 == 0:
                current.append((f'source{(i + 1) % 20:02d}', 'followers'))
            if i % 5 == 0:
                current.append((f'source{i % 20:02d}', 'following'))
            for seed, direction in current:
                edges.append((seed, i, direction, stamp))
                evidence.append((seed, i, direction, 1, stamp, stamp))
            if i % 7 == 0:
                edges.append(('historical', i, 'followers', stamp))
            if i % 13 == 0:
                edges.append(('absent', i, 'following', stamp))
                evidence.append(('absent', i, 'following', 0, stamp, stamp))
        self.conn.executemany('INSERT INTO edges VALUES(?,?,?,?)', edges)
        self.conn.executemany('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?)', evidence)
        self.conn.commit()
        started = time.monotonic()
        graph = server.api_map(self.conn, {'scope': ['all'], 'limit': ['50000']}, None)
        elapsed = time.monotonic() - started
        leads = [n for n in graph['nodes'] if n['kind'] == 'lead']
        self.assertEqual((graph['total'], graph['limit'], len(leads)), (count, 10000, 10000))
        self.assertEqual(len({n['id'] for n in graph['nodes']}), len(graph['nodes']))
        self.assertEqual({l['state'] for l in graph['links']}, {'observed', 'absent', 'unverified'})
        by_id = {n['id']: n for n in graph['nodes']}
        for link in graph['links']:
            self.assertIn(link['source'], by_id)
            self.assertIn(link['target'], by_id)
        for n in leads:
            self.assertEqual(n['degree'], 2 if int(n['id'][2:]) % 3 == 0 else 1)
        for n in graph['nodes']:
            if n['kind'] == 'seed':
                expected = self.conn.execute('SELECT count(DISTINCT person_id) FROM current_edges WHERE seed=?',
                                             (n['label'],)).fetchone()[0]
                self.assertEqual(n['degree'], expected)
        filtered = server.api_map(self.conn, {'seed': ['source00'], 'limit': ['10']}, None)
        expected = self.conn.execute("SELECT count(DISTINCT person_id) FROM current_edges WHERE seed='source00'").fetchone()[0]
        self.assertEqual(filtered['total'], expected)
        self.assertEqual(len([n for n in filtered['nodes'] if n['kind'] == 'lead']), 10)
        print(f'\nMap fixture: {count} people, {len(edges)} historical edges, {len(leads)} displayed; uncached API {elapsed:.3f}s')


if __name__ == '__main__':
    unittest.main()
