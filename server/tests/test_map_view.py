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

    def test_default_audiences_fill_compact_grid_and_preserve_membership(self):
        with MV.reader(self.path, 'closeness') as store:
            ctx = ML.load_ctx(store.conn)
            groups = ctx.groups('closeness')
            self.assertEqual(len(ctx.plan['comm']), 12)
            self.assertEqual(len(groups), 14)
            for i, group in enumerate(groups):
                for other in groups[i+1:]:
                    distance = ((group['x']-other['x'])**2+(group['y']-other['y'])**2)**0.5
                    self.assertGreater(distance, group['r'] + other['r'])
            self.assertLess(((groups[0]['x']-.5)**2+(groups[0]['y']-.5)**2)**0.5, .2)
            feats = ML.features_for(self.conn, ctx, [self.fixture['first']+3])
            feature = next(iter(feats.values()))
            row = ML.layout_row('closeness', feature, ctx)
            source_handles = [s for s in feature[6] if s != ctx.owner and s in ctx.src and ctx.src[s][1] >= 0]
            if source_handles:
                smallest = min(source_handles, key=lambda s:(ctx.plan['src'][s][0],s))
                self.assertEqual(row[4], ctx.src[smallest][1])
            centre = groups[row[4]]
            distance = ((ML.coord(row[1])-centre['x'])**2+(ML.coord(row[2])-centre['y'])**2)**0.5
            self.assertLessEqual(distance, centre['r'] + 1e-6)
            self.assertEqual(row[7], int(round(ML.closeness(feature,ctx)*1000)))

    def test_equal_rank_world_uses_index_and_returns_exact_ties(self):
        path = Path(self.temp.name)/'equal-rank.sqlite'
        conn = ML.open_store(path,create=True)
        try:
            rows = [(i,ML.to_micro(.5),ML.to_micro(.5),.5,0,0,None,0,0) for i in range(1,10001)]
            ML.insert_rows(conn,rows)
            ML.build_agg(conn)
            store = MV.Store(conn,path,'closeness')
            ranked = MV.top_ranked(store,(0.,0.,1.,1.),1,20,1.)
            self.assertEqual([r[0] for r in ranked],list(range(1,21)))
            self.assertEqual(MV.top_ranked(store,(0.,0.,1.,1.),1<<5,20,1.),[])
        finally:
            conn.close()

    def test_reader_pool_is_bounded_across_old_layout_files(self):
        MV.close_pool()
        class FakeConnection:
            def __init__(self):self.closed=False
            def close(self):self.closed=True
        connections=[]
        for index in range(30):
            path=Path(self.temp.name)/f'old-{index}.sqlite';path.touch()
            conn=FakeConnection();connections.append(conn);MV._give(path,conn)
        self.assertLessEqual(sum(len(v) for v in MV._POOL.values()),MV._POOL_MAX)
        self.assertGreaterEqual(sum(c.closed for c in connections),22)
        MV.close_pool()
