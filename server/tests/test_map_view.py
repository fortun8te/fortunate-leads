"""Viewport results agree with exhaustive layout rows, without live data."""
import json
import random
import tempfile
import unittest
from pathlib import Path
from map_view_fixture import make
import db
import map_layout as ML
import map_view as MV


class MapViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / 'map.sqlite'
        cls.fixture = make(cls.path, people=3000)
        ML.build(cls.path)

    @classmethod
    def tearDownClass(cls):
        MV.close_pool()
        cls.temp.cleanup()

    def setUp(self):
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    def view(self, q):
        return json.loads(MV.view(self.conn, self.path, q, cache=False).body)

    def test_viewport_matches_exhaustive_population_and_top_rank(self):
        rng = random.Random(13)
        for mode in ML.MODES:
            with MV.reader(self.path, mode) as store:
                rows = store.conn.execute('SELECT * FROM mp').fetchall()
            for attempt in range(12):
                q = {'mode': [mode], 'scope': ['all'], 'budget': ['50']}
                if attempt:
                    x, y = rng.random() * .7, rng.random() * .7
                    q.update(x0=[str(x)], y0=[str(y)], x1=[str(x+.3)], y1=[str(y+.3)])
                query = MV.Query(q)
                result = self.view(q)
                rect = result['viewport']; x0,y0,x1,y1 = [rect[k] for k in ('x0','y0','x1','y1')]
                expected = [r for r in rows if (query.mask >> r[5]) & 1 and x0 <= ML.coord(r[1]) < x1 and y0 <= ML.coord(r[2]) < y1]
                expected.sort(key=lambda r: (-r[3], r[0]))
                self.assertEqual(result['total'], len(expected), (mode, attempt))
                self.assertEqual([n['id'] for n in result['nodes']], [r[0] for r in expected[:50]], (mode, attempt))
                self.assertEqual(sum(b['count'] for b in result['clusters']), result['hidden'])
                self.assertLessEqual(len(result['clusters']), MV.BUBBLE_MAX)

    def test_invalid_parameters(self):
        for q in ({'mode':['bad']}, {'x0':['nan']}, {'budget':['abc']}, {'min_fit':['nan']}, {'q':['x']}):
            with self.assertRaises(ValueError): self.view(q)

    def test_missing_layout_does_not_start_build(self):
        path = Path(self.temp.name) / 'other.sqlite'
        result = json.loads(MV.view(self.conn, path, {}).body)
        self.assertFalse(result['ready'])
        self.assertFalse(ML.map_dir(path).exists())

    def test_edges_have_correct_handles_and_positions(self):
        pid = self.fixture['source_ids']['src01']
        result = MV.edges(self.conn, {'ids':[str(pid)]}, self.path)
        for edge in result['edges']:
            if edge['kind'] == 'follow':
                self.assertTrue(edge['from_handle'])
                self.assertTrue(edge['to_handle'])
                if edge['direction'] == 'followers': self.assertEqual(edge['to_handle'], edge['seed'])
                else: self.assertEqual(edge['from_handle'], edge['seed'])
        self.assertLessEqual(len(result['edges']), 2000)
        self.assertTrue(result['nodes'])

    def test_name_edit_invalidates_view_and_search(self):
        pid = self.fixture['first']
        before = MV.view(self.conn, self.path, {'scope':['all']})
        self.conn.execute('UPDATE people SET name=? WHERE id=?', ('Distinctive test name', pid));self.conn.commit()
        after = MV.view(self.conn, self.path, {'scope':['all']})
        self.assertNotEqual(before.etag, after.etag)
        self.assertGreater(json.loads(after.body)['layout']['pending'], 0)
        ML.apply_dirty(self.path, main=self.conn)
        self.assertEqual(ML.pending(self.conn), 0)

    def test_incremental_updates_agree_with_pure_layout(self):
        pid = self.fixture['first']+1
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'client') ON CONFLICT(person_id) DO UPDATE SET status='client'", (pid,))
        self.conn.commit()
        ML.apply_dirty(self.path, main=self.conn)
        for mode in ML.MODES:
            with MV.reader(self.path, mode) as store:
                ctx = ML.load_ctx(store.conn)
                feature = ML.features_for(self.conn, ctx, [pid])[pid]
                actual = store.conn.execute('SELECT * FROM mp WHERE person_id=?',(pid,)).fetchone()
                self.assertEqual(tuple(actual), tuple(ML.layout_row(mode,feature,ctx)))

    def test_http_routes_and_conditional_response(self):
        import http.client
        import threading
        import server
        from http.server import ThreadingHTTPServer
        old = dict(server.CFG)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.CFG.update(db=str(self.path), port=httpd.server_address[1])
        thread = threading.Thread(target=httpd.serve_forever, daemon=True);thread.start()
        try:
            ML.apply_dirty(self.path, main=self.conn)
            client = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1])
            client.request('GET', '/api/map/view?scope=all')
            first = client.getresponse();self.assertEqual(first.status, 200)
            self.assertTrue(json.loads(first.read())['ready'])
            etag = first.getheader('ETag');self.assertTrue(etag)
            client.request('GET', '/api/map/view?scope=all', headers={'If-None-Match':etag})
            second = client.getresponse();self.assertEqual(second.status, 304);self.assertEqual(second.read(), b'')
            for path, key in (('/api/map/search?q=user', 'results'), ('/api/map/edges?ids=1','edges')):
                client.request('GET',path);response=client.getresponse()
                self.assertEqual(response.status,200);self.assertIn(key,json.loads(response.read()))
            client.close()
        finally:
            httpd.shutdown();httpd.server_close();thread.join();server.CFG.clear();server.CFG.update(old)

    def test_own_source_deleted_leaves_no_ghost(self):
        pid = self.fixture['source_ids']['src00']
        self.conn.execute('DELETE FROM people WHERE id=?',(pid,));self.conn.commit()
        ML.apply_dirty(self.path,main=self.conn)
        for mode in ML.MODES:
            with MV.reader(self.path,mode) as store:
                self.assertIsNone(store.conn.execute('SELECT 1 FROM mp WHERE person_id=?',(pid,)).fetchone())
