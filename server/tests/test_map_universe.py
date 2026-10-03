import json
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import threading
import urllib.request
from unittest import mock
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import map_universe as U


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        self.c = sqlite3.connect(self.path)
        self.c.executescript('''CREATE TABLE people(id INTEGER PRIMARY KEY,ig_id TEXT,handle TEXT,name TEXT,followers INTEGER);
          CREATE TABLE universe_edges(source_id INTEGER,target_id INTEGER,observed_at TEXT,provenance TEXT);
          CREATE TABLE current_edges(seed TEXT,person_id INTEGER,direction TEXT,observed_at TEXT);
          CREATE TABLE follow_export_imports(fingerprint TEXT,owner_handle TEXT,owner_person_id INTEGER);
          CREATE TABLE follow_export_members(fingerprint TEXT,direction TEXT,handle TEXT,source_timestamp TEXT,person_id INTEGER);
          INSERT INTO people VALUES(10,'100','me','Owner',100),(20,'200','one','One',1000),
            (30,'300','two','Two',10000),(40,'400','isolated','Isolated',NULL),(50,'500','inbound','Inbound',20);
          INSERT INTO universe_edges VALUES(10,20,'2026-09-29','captured_fixture'),
            (20,30,'2026-09-29','captured_fixture'),(20,30,'2026-09-29','captured_fixture'),
            (50,10,'2026-09-29','captured_fixture'),(30,9999,'2026-09-29','captured_fixture');
          INSERT INTO current_edges VALUES('two',40,'following','2026-09-29');
          INSERT INTO follow_export_imports VALUES('f','me',10);
          INSERT INTO follow_export_members VALUES('f','followers','isolated','2020-01-01',40);''')
        self.c.commit()
        self.c.close()

    def test_real_hops_directed_and_unknown(self):
        result = U.build(self.path,'me',tile_size=2)
        self.assertEqual(result['node_count'],5)
        self.assertEqual(result['edge_count'],3)
        self.assertEqual(result['reachable_count'],3)
        self.assertEqual(U.person(self.path,{'id':'30'})['person']['hop'],2)
        self.assertIsNone(U.person(self.path,{'id':'50'})['person']['hop'])
        self.assertIsNone(U.person(self.path,{'id':'40'})['person']['hop'])
        self.assertEqual(U.person(self.path,{'id':'10'})['person']['community'],10)
        self.assertEqual(U.person(self.path,{'id':'40'})['person']['community'],40)
        self.assertEqual(U.person(self.path,{'id':'20'})['person']['degree'],1)

    def test_binary_tiling_pagination_and_csr(self):
        result = U.build(self.path,10,tile_size=2)
        self.assertEqual(result['tile_count'],4)
        got = []
        q = {'version':result['version'],'budget':'2'}
        pages = 0
        while True:
            page = U.view(self.path,q)
            self.assertLessEqual(page['record_count'],2)
            for tile in page['tiles']:
                raw = U.tile(self.path,{'version':result['version'],'tile':str(tile['id'])})
                magic,n,stride,reserved = U.HEADER.unpack_from(raw.body)
                self.assertEqual((magic,stride,reserved),(b'MUV1',32,0))
                self.assertEqual(len(raw.body),16+n*32)
                for i in range(n):
                    record = U.RECORD.unpack_from(raw.body,16+i*32)
                    got.append(record[0])
                    self.assertGreater(record[3],0)
            pages += 1
            if page['next_cursor'] is None:
                break
            q['cursor'] = page['next_cursor']
        self.assertEqual(sorted(got),[10,20,30,40,50])
        self.assertEqual(pages,3)
        directory = U._snapshot(self.path)
        offsets = struct.unpack('<6Q',(directory/'offsets.u64').read_bytes())
        targets = struct.unpack('<3I',(directory/'targets.u32').read_bytes())
        self.assertEqual(offsets,(0,1,2,2,2,3))
        self.assertEqual(targets,(1,2,0))
        with self.assertRaises(ValueError):
            U.view(self.path,dict(q,x0='0'))
        with self.assertRaises(ValueError):
            U.tile(self.path,{'version':'../escape','tile':'0'})

    def test_explicit_traversal_and_historical_policy(self):
        U.build(self.path,'me','incoming')
        self.assertEqual(U.person(self.path,{'id':'50'})['person']['hop'],1)
        self.assertIsNone(U.person(self.path,{'id':'20'})['person']['hop'])
        U.build(self.path,'me','undirected','all')
        self.assertEqual(U.person(self.path,{'id':'50'})['person']['hop'],1)
        self.assertEqual(U.person(self.path,{'id':'40'})['person']['hop'],1)
        self.assertEqual(U.manifest(self.path)['provenance']['historical_export_handle'],1)

    def test_stable_positions_source_unchanged_and_immutable_versions(self):
        before = self.path.read_bytes()
        first = U.build(self.path,'me')
        p = U.person(self.path,{'id':'30'})['person']
        second = U.build(self.path,'me')
        self.assertNotEqual(first['version'],second['version'])
        self.assertEqual(p,U.person(self.path,{'id':'30'})['person'])
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(U.view(self.path,{'version':first['version']})['version'],first['version'])
        first_path = U._snapshot(self.path,first['version'])
        old = (first_path/'manifest.json').read_bytes()
        with self.assertRaises(ValueError):
            U.build(self.path,'missing')
        self.assertEqual(U.manifest(self.path)['version'],second['version'])
        self.assertEqual((first_path/'manifest.json').read_bytes(),old)

    def test_dense_radius_area_budget_and_search_locate(self):
        c=sqlite3.connect(self.path)
        c.executemany('INSERT INTO people VALUES(?,?,?,?,?)',((i,str(i),'dense_'+str(i),'Dense',i) for i in range(100,2100)))
        c.executemany('INSERT INTO universe_edges VALUES(?,?,?,?)',((10,i,'2026-09-29','captured_fixture') for i in range(100,2100)))
        c.commit();c.close()
        manifest = U.build(self.path,'me')
        with closing(sqlite3.connect(U._snapshot(self.path)/'index.sqlite')) as c:
            rows=c.execute('SELECT r,followers,x,y FROM nodes WHERE hop=1').fetchall()
        inner, outer = manifest['ring_bounds'][1]
        area=sum(math.pi*r*r for r,f,x,y in rows)
        self.assertLessEqual(area,math.pi*(outer**2-inner**2)*U.PORTRAIT_FILL+1e-6)
        self.assertGreater(outer, 600)
        self.assertTrue(all(r > 0 for r,f,x,y in rows))
        # The camera exposes a compact owner neighborhood even with thousands of direct follows.
        radius = manifest['initial_camera']['radius']
        visible = [(x,y,r) for r,f,x,y in rows if abs(x)<radius*1.6 and abs(y)<radius]
        self.assertTrue(350 < len(visible) < 950)
        # At an 800px-high landscape viewport, the center shows readable photos,
        # while the edges naturally become small gray bubbles.
        scale = 360/radius
        near = [r*scale for x,y,r in visible if math.hypot(x,y)<100]
        outer = [r*scale for x,y,r in visible if math.hypot(x,y)>350]
        self.assertGreater(min(near), 10)
        self.assertLess(max(outer), 10)
        self.assertGreater(max(near), max(outer)*1.5)
        # Golden-angle packing avoids severe collisions in that default neighborhood.
        severe = sum(1 for i,(x,y,r) in enumerate(visible) for xx,yy,rr in visible[i+1:]
                     if math.hypot(x-xx,y-yy)<.75*(r+rr))
        self.assertEqual(severe, 0)
        self.assertEqual(len(U.search(self.path,{'q':'dense_'})['results']),20)
        self.assertEqual([n['id'] for n in U.locate(self.path,{'ids':'10,20,99999'})['nodes']],[10,20])
        self.assertTrue(any(e['target']==20 for e in U.edges(self.path,{'ids':'10'})['edges']))
        with self.assertRaises(ValueError): U.locate(self.path,{'ids':','.join(['1']*201)})

    def test_large_second_hop_expands_without_shrinking_neighborhood(self):
        # Actual-data scale: adding 111,773 second-hop people must not squeeze
        # the 2,723 first-hop portraits into a microscopic disk.
        bands = U._bands([1, 2723, 111773, 0, 3000])
        self.assertGreater(bands[2][1], bands[1][1] * 5)
        self.assertEqual(bands[1], U._bands([1, 2723, 1, 0, 1])[1])
        self.assertTrue(all(bands[d][1] == bands[d+1][0] for d in range(1,len(bands)-1)))
        log_max = math.log1p(1000000)
        small = U._portrait_radius(1, 100, 0, log_max)
        large = U._portrait_radius(1, 100, 1000000, log_max)
        self.assertAlmostEqual(large / small, 1.15 / .85)
        self.assertGreater(small, U._portrait_radius(1, 1000, 0, log_max))
        self.assertGreater(small, U._portrait_radius(2, 100, 0, log_max))
        self.assertGreater(U._portrait_radius(2, 20000, 0, log_max), .15)

    def test_http_binary_mime_and_conditional_read(self):
        from backend.app import Application
        from backend.common import AppConfig
        from backend_http import Handler, Server

        result=U.build(self.path,'me')
        tile=U.manifest(self.path)['tiles'][0]
        application = Application(AppConfig(db=str(self.path), port=0, saved_data_only=True))
        self.addCleanup(application.close)
        http=Server(('127.0.0.1',0), Handler, application=application.http)
        port=http.server_address[1]
        application.bind_port(port)
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        try:
            url='http://127.0.0.1:'+str(port)+tile['url']
            with urllib.request.urlopen(url,timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(),'application/octet-stream')
                self.assertEqual(response.read()[:4],b'MUV1')
                etag=response.headers['ETag']
            request=urllib.request.Request(url,headers={'If-None-Match':etag})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request,timeout=5)
            self.assertEqual(caught.exception.code,304)
            caught.exception.close()
        finally:
            http.shutdown();http.server_close();thread.join(timeout=5)

    def test_abutting_hop_bands_and_source_change_hint(self):
        U.build(self.path,'me')
        self.assertFalse(U.manifest(self.path)['stale'])
        near=U.person(self.path,{'id':'20'})['person']
        far=U.person(self.path,{'id':'30'})['person']
        bands = U.manifest(self.path)['ring_bounds']
        self.assertTrue(bands[1][0]<=math.hypot(near['x'],near['y'])<bands[1][1])
        self.assertTrue(bands[2][0]<=math.hypot(far['x'],far['y'])<bands[2][1])
        self.assertEqual(bands[1][1],bands[2][0])
        self.assertEqual(bands[2][1],bands[3][0])
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('UPDATE people SET followers=777 WHERE id=20');c.commit()
        changed=U.manifest(self.path)
        self.assertTrue(changed['stale'])
        self.assertFalse(changed['auto_rebuild'])
        self.assertEqual(U.person(self.path,{'id':'20'})['person']['followers'],1000)
        U.build(self.path,'me')
        self.assertFalse(U.manifest(self.path)['stale'])
        self.assertEqual(U.person(self.path,{'id':'20'})['person']['followers'],777)

    def test_camera_and_missing_snapshot(self):
        self.assertFalse(U.manifest(self.path)['available'])
        U.build(self.path,'me')
        self.assertEqual(U.view(self.path,{'x0':'999999','x1':'1000000','y0':'999999','y1':'1000000'})['tiles'],[])
        with self.assertRaises(ValueError):
            U.view(self.path,{'x0':'nan'})


if __name__ == '__main__':
    unittest.main()
