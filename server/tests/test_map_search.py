import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connection_graph


class MapSearchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE people(id INTEGER PRIMARY KEY,handle TEXT UNIQUE,name TEXT,bio TEXT)')
        self.conn.executemany('INSERT INTO people(handle,name,bio) VALUES(?,?,?)', [
            ('x_noah', 'Unrelated', ''), ('noah_extra', 'Other', ''),
            ('noah', 'Noah Main', ''), ('someone', 'Noah', ''),
            ('unrelated', 'Unrelated', 'Noah'), ('noahxextra', 'Other', ''),
        ])

    def tearDown(self):
        self.conn.close()

    def search(self, text):
        spec = connection_graph.map_search(text)
        return [r[0] for r in self.conn.execute(
            f"SELECT handle FROM people p WHERE {spec['where']} ORDER BY {spec['rank']},handle LIMIT 100",
            (*spec['args'], *spec['rank_args']))]

    def test_identity_rank_excludes_bio_mentions(self):
        self.assertEqual(self.search('noah'), ['noah', 'noah_extra', 'noahxextra', 'someone', 'x_noah'])

    def test_handle_and_profile_url_normalize(self):
        for query in (' @NOAH ', 'https://www.instagram.com/noah/?igsh=123', 'instagram.com/noah/'):
            self.assertEqual(self.search(query)[0], 'noah')

    def test_literal_wildcards(self):
        self.assertEqual(self.search('noah_'), ['noah_extra'])
        self.assertEqual(self.search('%'), [])
        self.assertEqual(self.search("' OR 1=1 --"), [])

    def test_invalid_urls(self):
        for query in ('https://instagram.com/p/123', 'https://example.com/noah', 'a' * 513):
            with self.assertRaises(ValueError):
                connection_graph.map_search(query)

    def test_empty_search_is_overview(self):
        self.assertIsNone(connection_graph.map_search(''))
        self.assertIsNone(connection_graph.map_search(' @ '))


class MapSearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        os.environ.setdefault('FL_NO_ORSLOT', '1')
        import db
        import server
        self.db, self.server = db, server
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'map.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, name=None):
        return self.db.upsert_person(self.conn, {'handle': handle, 'name': name})

    def test_search_reaches_outside_display_and_unconnected_hidden_people(self):
        for i in range(20):
            self.db.add_edge(self.conn, 'source', self.person(f'visible{i}'), 'followers')
        pid = self.person('exact_name', 'Exact Person')
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'no')", (pid,))
        self.conn.commit()
        before = self.server.map_graph(self.conn, {'limit': ['10']})
        self.assertNotIn(f'p:{pid}', [n['id'] for n in before['nodes']])
        for text in ('@EXACT_NAME', 'https://instagram.com/exact_name/', 'Exact Person'):
            graph = self.server.map_graph(self.conn, {'q': [text], 'limit': ['3000']})
            node = next(n for n in graph['nodes'] if n['id'] == f'p:{pid}')
            self.assertEqual(node['degree'], 0)
            self.assertEqual(graph['total'], 1)
            self.assertEqual(graph['limit'], 100)
            self.assertFalse(any(l['target'] == node['id'] for l in graph['links']))

    def test_exact_rank_precedes_higher_ranked_prefix_and_seed_name_retained(self):
        exact = self.person('alex')
        self.person('alex_prefix')
        seed = self.person('source', 'Friendly Name')
        self.conn.execute("INSERT INTO seeds(handle) VALUES('source')")
        self.conn.commit()
        graph = self.server.map_graph(self.conn, {'q': ['alex']})
        self.assertEqual([n['id'] for n in graph['nodes'] if n['kind'] == 'lead'][0], f'p:{exact}')
        self.assertEqual(next(n for n in graph['nodes'] if n.get('pid') == seed)['name'], 'Friendly Name')

    def test_search_still_respects_explicit_filters(self):
        self.person('alex')
        self.conn.commit()
        graph = self.server.map_graph(self.conn, {'q': ['alex'], 'followers_min': ['999']})
        self.assertEqual(graph['total'], 0)
