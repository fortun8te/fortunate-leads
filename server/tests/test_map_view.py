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

    def test_all_modes_share_exact_positions_communities_and_owner(self):
        reference = None
        groups = None
        for mode in ML.MODES:
            with MV.reader(self.path, mode) as store:
                positions = store.conn.execute('SELECT person_id,mx,my,cluster FROM mp ORDER BY person_id').fetchall()
                if reference is None:
                    reference, groups = positions, store.groups()
                self.assertEqual(positions, reference)
                self.assertEqual(store.groups(), groups)
            result = self.view({'mode':[mode], 'scope':['all'], 'status':['all'], 'overview':['1'], 'budget':['80']})
            self.assertEqual(result['world']['layout'], 'network_disk')
            self.assertAlmostEqual(result['world']['me']['x'], .5, places=6)
            self.assertAlmostEqual(result['world']['me']['y'], .5, places=6)
            self.assertEqual(sum(b['count'] for b in result['clusters']), result['hidden'])
            self.assertLessEqual(len(result['nodes']), 80)
            for node in result['nodes']:
                degree = self.conn.execute('SELECT degree FROM map_person_degree WHERE person_id=?', (node['id'],)).fetchone()
                self.assertEqual(node['source_count'], degree[0] if degree else 0)

    def test_partial_mode_build_reuses_shared_geometry_plan(self):
        path = Path(self.temp.name) / 'shared-plan.sqlite'
        fixture = make(path, people=50)
        ML.build(path, modes=('closeness',))
        conn = db.connect(path); self.addCleanup(conn.close)
        with MV.reader(path, 'closeness') as store:
            original = store.meta('plan')
        # A new audience changes a newly computed angular allocation. Preparing
        # another mode alone must retain existing anchors instead of relocating.
        pid = fixture['first']
        db.add_edge(conn, 'new_audience', pid, 'followers'); conn.commit()
        self.assertIn('new_audience', ML.make_plan(conn)['src'])
        ML.build(path, modes=('fit',), fresh=True)
        with MV.reader(path, 'fit') as store:
            self.assertEqual(store.meta('plan'), original)

    def test_disconnected_saved_people_and_owner_are_included(self):
        path = Path(self.temp.name) / 'disconnected.sqlite'
        conn = db.init(str(path))
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO seeds(handle,added_at,is_me) VALUES('owner','2026-01-01',1)")
        conn.executemany("INSERT INTO people(id,handle,name,first_seen,updated_at) VALUES(?,?,?,'2026-01-01','2026-01-01')",
                         [(1, 'owner', 'Owner'), (2, 'saved', 'Saved lead'), (3, 'unreviewed', 'Unreviewed')])
        conn.execute("INSERT INTO marks(person_id,status) VALUES(2,'interested')")
        conn.commit()
        self.assertEqual(ML.make_plan(conn)['people'], 3)
        ML.build(path, modes=('closeness',))
        result = json.loads(MV.view(conn, path, {'scope':['all']}, cache=False).body)
        self.assertEqual(result['total'], 3)
        self.assertEqual({n['id'] for n in result['nodes']}, {1, 2, 3})
        self.assertEqual(result['world']['me']['id'], 1)
        self.assertEqual(next(n for n in result['nodes'] if n['id'] == 2)['status'], 'interested')

    def test_invalid_parameters(self):
        for q in ({'mode':['bad']}, {'x0':['nan']}, {'budget':['abc']}, {'min_fit':['nan']}, {'q':['x']}):
            with self.assertRaises(ValueError): self.view(q)

    def test_missing_layout_does_not_start_build(self):
        path = Path(self.temp.name) / 'other.sqlite'
        result = json.loads(MV.view(self.conn, path, {}).body)
        self.assertFalse(result['ready'])
        self.assertFalse(ML.map_dir(path).exists())

    def test_bulk_follower_hydration_preserves_unknown_zero_and_cached_portraits(self):
        path = Path(self.temp.name) / 'followers.sqlite'
        conn = db.init(str(path)); self.addCleanup(conn.close)
        conn.execute("INSERT INTO seeds(handle,is_me) VALUES('owner',1)")
        for pid, handle, followers, pic in ((1,'owner',None,'owner.jpg'),(2,'zero',0,None),
                                           (3,'unknown',None,None),(4,'large',123456,'large.jpg')):
            conn.execute("INSERT INTO people(id,handle,name,followers,pic_file,first_seen,updated_at) VALUES(?,?,?,?,?,'now','now')",
                         (pid,handle,handle.title(),followers,pic))
        conn.commit(); ML.build(path)
        expected = {1:None,2:0,3:None,4:123456}
        for mode in ML.MODES:
            result = json.loads(MV.view(conn,path,{'scope':['all'],'mode':[mode]},cache=False).body)
            self.assertEqual({node['id']:node['followers'] for node in result['nodes']}, expected)
            self.assertEqual(result['world']['me']['pic'], '/img/1')
            self.assertIsNone(result['world']['me']['followers'])
        for handle, value in (('zero',0),('unknown',None),('large',123456)):
            found = MV.search(conn,path,{'q':[handle]})['results'][0]
            self.assertEqual(found['followers'], value)
            self.assertEqual(found['name'], handle.title())
            self.assertEqual(found['pic'], '/img/4' if handle == 'large' else None)
        queries=[]
        conn.set_trace_callback(queries.append)
        try:
            hydrated = MV._people(conn, list(range(1,1802)))
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(hydrated),4)
        # 900-ID chunks: follower/name/photo/count hydration never queries per row.
        self.assertEqual(len(queries),3)
        self.assertTrue(all('p.followers' in query and 'WHERE p.id IN' in query for query in queries))

    def test_old_layout_is_unprepared_and_old_build_plan_is_not_resumed(self):
        path = Path(self.temp.name) / 'old-schema.sqlite'
        make(path, people=50)
        ML.build(path, modes=('closeness',))
        conn = db.connect(path); self.addCleanup(conn.close)
        store = ML.open_store(ML.mode_path(path, 'closeness'))
        plan = ML.meta_get(store, 'plan')
        ML.meta_set(store, 'schema', ML.SCHEMA_VERSION - 1)
        store.close()
        MV.close_pool()
        self.assertFalse(json.loads(MV.view(conn,path,{},cache=False).body)['ready'])
        self.assertFalse(ML.status(path)['modes']['closeness']['ready'])
        self.assertEqual(ML.apply_ids(path,[self.fixture['first']],main=conn), {})
        plan.pop('network_disk', None)
        plan['network_centres'] = [[.5,.5,.1]]
        ML._write_json(ML.map_dir(path) / 'plan.json', {'state':'building','schema':ML.SCHEMA_VERSION-1,
                       'modes':['closeness'],'plan':plan})
        ML.build(path,modes=('closeness',))
        with MV.reader(path,'closeness') as current:
            self.assertTrue(ML.load_ctx(current.conn).network_disk)

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
        positions = []
        for mode in ML.MODES:
            with MV.reader(self.path, mode) as store:
                ctx = ML.load_ctx(store.conn)
                feature = ML.features_for(self.conn, ctx, [pid])[pid]
                actual = store.conn.execute('SELECT * FROM mp WHERE person_id=?',(pid,)).fetchone()
                self.assertEqual(tuple(actual), tuple(ML.layout_row(mode,feature,ctx)))
                positions.append((actual[1], actual[2], actual[4]))
        self.assertEqual(len(set(positions)), 1)

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

    def test_network_disk_preserves_recorded_evidence_and_owner_origin(self):
        with MV.reader(self.path, 'closeness') as store:
            ctx = ML.load_ctx(store.conn)
            groups = ctx.groups('closeness')
        self.assertGreater(len(groups), 5)
        self.assertTrue(ctx.network_disk)
        self.assertTrue(all(((g['x']-.5)**2+(g['y']-.5)**2)**.5 < .47 for g in groups))
        owner = ML.features_for(self.conn, ctx, [ctx.owner_id])[ctx.owner_id]
        row = ML.layout_row('closeness', owner, ctx)
        self.assertAlmostEqual(ML.coord(row[1]), .5, places=6)
        self.assertAlmostEqual(ML.coord(row[2]), .5, places=6)
        self.assertEqual(row[4], (ctx.k_other + 1) * 4)
        feature = ML.features_for(self.conn, ctx, [self.fixture['first']])[self.fixture['first']]
        row = ML.layout_row('closeness', feature, ctx)
        self.assertEqual(row[4] % 4, ML.network_group(feature,ctx))
        self.assertEqual(row[7], int(round(ML.closeness(feature,ctx)*1000)))
        category = ML.network_group(feature, ctx)
        radius = ((ML.coord(row[1])-.5)**2+(ML.coord(row[2])-.5)**2)**.5
        low, high = ML.NETWORK_RADII[category]
        self.assertGreaterEqual(radius, low-1e-6)
        self.assertLessEqual(radius, high+1e-6)

    def test_overview_keeps_exact_counts_with_bounded_spatial_sampling(self):
        result = self.view({'scope': ['all'], 'budget': ['80'], 'overview': ['1']})
        self.assertEqual(result['world']['layout'], 'network_disk')
        self.assertLessEqual(len(result['nodes']), 80)
        self.assertEqual(len({n['id'] for n in result['nodes']}), len(result['nodes']))
        self.assertEqual(sum(b['count'] for b in result['clusters']), result['hidden'])
        self.assertIn(result['world']['me']['id'], {n['id'] for n in result['nodes']})
        self.assertTrue(all((n['x']-.5)**2+(n['y']-.5)**2 <= .47**2+1e-6 for n in result['nodes']))

    def test_follow_direction_uses_observation_and_explicit_absence_only(self):
        path = Path(self.temp.name) / 'directions.sqlite'
        conn = db.init(str(path)); self.addCleanup(conn.close)
        conn.execute("INSERT INTO seeds(handle,is_me) VALUES('owner',1)")
        for pid in range(1, 9):
            conn.execute("INSERT INTO people(id,handle,pic_file,first_seen,updated_at) VALUES(?,?,?,'now','now')",
                         (pid, 'owner' if pid == 1 else f'person{pid}', 'saved.jpg' if pid in (1, 2) else None))
        for pid, direction in ((2,'following'),(3,'followers'),(4,'following'),(4,'followers'),(5,'following'),(7,'following'),(7,'followers')):
            db.add_edge(conn, 'owner', pid, direction)
        # Explicit negative observations stand for the collector's proven complete
        # snapshot; legacy edges and never-collected profiles remain unknown.
        conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='owner' AND direction='following' AND person_id IN(5,7)")
        db.add_edge(conn, 'owner', 6, 'following', observed=False)
        conn.execute("UPDATE people SET name='Distinct owner follow' WHERE id=2")
        conn.commit(); ML.build(path, modes=('closeness',))
        expected = {'following': {2,4}, 'followers': {3,4,7}, 'mutual': {4},
                    'not_following': {5,7}, 'unknown': {1,3,6,8}, 'all': set(range(1,9))}
        for follow, ids in expected.items():
            result = json.loads(MV.view(conn,path,{'scope':['all'],'follow':[follow]},cache=False).body)
            self.assertEqual({n['id'] for n in result['nodes']}, ids, follow)
            self.assertEqual(result['total'], len(ids))
            self.assertEqual(result['world']['me']['pic'], '/img/1')
            for node in result['nodes']:
                if node['id'] in (5,7): self.assertEqual(node['following_evidence'], 'absent')
                if node['id'] in (1,3,6,8): self.assertEqual(node['following_evidence'], 'unknown')
                if node['id'] == 2: self.assertEqual(node['pic'], '/img/2')
                if node['id'] == 4: self.assertEqual(node['source_count'], 1)
        found = MV.search(conn, path, {'q':['Distinct owner follow']})['results']
        self.assertEqual([person['id'] for person in found], [2])
        self.assertEqual(found[0]['pic'], '/img/2')
        self.assertTrue(found[0]['followed'])
        # A later positive observation wins over the dated negative. Source case
        # differences and an inactive incoming direction cannot invert that fact.
        db.add_edge(conn, 'OWNER', 5, 'following', ts='2099-01-01T00:00:00+00:00')
        conn.execute("INSERT INTO edges VALUES('owner',2,'followers','now')")
        conn.execute("INSERT INTO edge_evidence VALUES('owner',2,'followers',0,NULL,'now')")
        conn.commit(); ML.apply_dirty(path, main=conn)
        result = json.loads(MV.view(conn,path,{'scope':['all'],'follow':['following']},cache=False).body)
        self.assertEqual({n['id'] for n in result['nodes']}, {2,4,5})
        self.assertTrue(all(n['following_evidence'] == 'observed' for n in result['nodes']))
        absent = json.loads(MV.view(conn,path,{'scope':['all'],'follow':['not_following']},cache=False).body)
        self.assertEqual({n['id'] for n in absent['nodes']}, {7})
        with self.assertRaises(ValueError): MV.Query({'follow':['unsupported']})

    def test_network_group_never_promotes_an_interested_source_to_a_known_relationship(self):
        plan = {'owner': 'owner', 'owner_id': 1, 'src': {'cold': [10,1,0,0]}, 'comm': ['cold'],
                'seed_list': ['cold'], 'network_disk': True, 'network_sectors': [[0,3.14],[3.14,3.14]],
                'known_sources': []}
        ctx = ML.Ctx(plan)
        feature = (2, 1, None, None, 0, 0, ('cold',), 0, 0, 0)
        self.assertEqual(ML.network_group(feature,ctx), 3)
        direct = (*feature[:5], 2, *feature[6:])
        self.assertEqual(ML.network_group(direct,ctx), 0)


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
