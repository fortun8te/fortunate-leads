"""Recorded owner follows outrank audience overlap without claiming personal closeness."""
import math
import json
import tempfile
import unittest
from pathlib import Path
import db
import map_layout as ML
import map_view as MV


class OwnerDistanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'map.sqlite'))
        self.addCleanup(self.conn.close)
        self.owner = db.upsert_person(self.conn, {'handle':'fortun8te'})
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('fortun8te',1)")
        self.conn.commit()
        self.ctx = ML.Ctx(ML.make_plan(self.conn))

    def feature(self, pid, me=0, status=0, score=0, fit=None, seeds=(), fam=0, rel=0, src=0):
        return (pid,len(seeds),score,fit,status,me,seeds,src,fam,rel)

    def radius(self, feature):
        row = ML.layout_row('closeness', feature, self.ctx)
        return math.hypot(ML.coord(row[1])-.5, ML.coord(row[2])-.5)

    def test_recorded_follow_tiers_have_disjoint_distance_and_rank(self):
        tiers = [lambda p:self.feature(p,me=3), lambda p:self.feature(p,me=2),
                 lambda p:self.feature(p,me=1),lambda p:self.feature(p,fam=3,rel=1),
                 lambda p:self.feature(p,score=100,fit=100,status=5,src=1,seeds=tuple(f's{i}' for i in range(20)))]
        bands=[]
        for make in tiers:
            features=[make(p) for p in range(100,200)]
            bands.append(([self.radius(f) for f in features],[ML.layout_row('closeness',f,self.ctx)[3] for f in features]))
        for inner,outer in zip(bands,bands[1:]):
            self.assertLess(max(inner[0]),min(outer[0]))
            self.assertGreater(min(inner[1]),max(outer[1]))

    def test_status_and_source_do_not_claim_direct_owner_link(self):
        for status in (3,4,5):
            self.assertNotEqual(ML.network_group(self.feature(100,status=status),self.ctx),0)

    def test_reverse_source_observations_are_owner_follow_evidence(self):
        friend = db.upsert_person(self.conn, {'handle':'friend'})
        db.add_edge(self.conn,'friend',self.owner,'following')
        self.conn.commit()
        features=ML.features_for(self.conn,self.ctx,[friend])
        self.assertEqual(features[friend][5]&3,2)
        db.add_edge(self.conn,'friend',self.owner,'followers')
        self.conn.commit()
        self.assertEqual(ML.features_for(self.conn,self.ctx,[friend])[friend][5]&3,3)
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='friend'")
        self.conn.commit()
        self.assertEqual(ML.features_for(self.conn,self.ctx,[friend])[friend][5]&3,0)

    def test_default_owner_view_includes_people_without_lead_verdict(self):
        self.assertEqual(MV.Query({}).scope,'all')
        self.assertEqual(MV.Query({'mode':['fit']}).scope,'leads')

    def test_default_cohort_retains_not_a_fit_mutual_owner_connection(self):
        friend = db.upsert_person(self.conn, {'handle':'friend'})
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'no')", (friend,))
        db.add_edge(self.conn,'friend',self.owner,'following')
        db.add_edge(self.conn,'friend',self.owner,'followers')
        self.conn.commit()
        path = self.conn.execute('PRAGMA database_list').fetchone()[2]
        ML.build(path,modes=('closeness',))
        result = json.loads(MV.view(self.conn,path,{'cohort':['1']},cache=False).body)
        node = next(n for n in result['nodes'] if n['id']==friend)
        self.assertTrue(node['followed'])
        self.assertTrue(node['follows_me'])
        self.assertEqual(node['connection_kind'],'mutual')
        self.assertEqual(result['filters']['scope'],'all')
        compact = json.loads(MV.view(self.conn,path,{'cohort':['1'],'compact':['1']},cache=False).body)
        row = next(r for r in compact['rows'] if r[0]==friend)
        self.assertEqual(row[12],0)
        self.assertEqual(compact['columns'][12],'connectionTier')

    def test_reverse_source_updates_refresh_the_source_layout_on_owner_dirty(self):
        friend = db.upsert_person(self.conn, {'handle':'friend'})
        self.conn.commit()
        path = self.conn.execute('PRAGMA database_list').fetchone()[2]
        ML.build(path,modes=('closeness',))
        db.add_edge(self.conn,'friend',self.owner,'following')
        self.conn.commit()
        ML.apply_ids(path,[self.owner],main=self.conn)
        with MV.reader(path,'closeness') as store:
            row = store.conn.execute('SELECT cls FROM mp WHERE person_id=?',(friend,)).fetchone()
            self.assertEqual(row[0]//64&3,2)
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='friend'")
        self.conn.commit()
        ML.apply_ids(path,[self.owner],main=self.conn)
        with MV.reader(path,'closeness') as store:
            row = store.conn.execute('SELECT cls FROM mp WHERE person_id=?',(friend,)).fetchone()
            self.assertEqual(row[0]//64&3,0)

    def test_background_arrows_are_recorded_directional_edges_only(self):
        friend = db.upsert_person(self.conn, {'handle':'friend'})
        outsider = db.upsert_person(self.conn, {'handle':'outsider'})
        db.add_edge(self.conn,'friend',self.owner,'following')
        db.add_edge(self.conn,'fortun8te',friend,'followers')
        db.add_edge(self.conn,'fortun8te',outsider,'following')
        db.add_edge(self.conn,'audience',friend,'followers')
        self.conn.commit()
        edges,truncated = MV.recorded_connections(self.conn,[friend],self.owner)
        self.assertFalse(truncated)
        self.assertEqual([(e['source'],e['target'],e['kind']) for e in edges],[(friend,self.owner,'follow')])
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='friend' OR person_id=?",(friend,))
        self.conn.commit()
        self.assertEqual(MV.recorded_connections(self.conn,[friend],self.owner)[0],[])

    def test_background_arrows_stay_bounded_for_large_visible_cohort(self):
        ids=[]
        for i in range(420):
            pid=db.upsert_person(self.conn,{'handle':f'friend{i}'})
            ids.append(pid)
            db.add_edge(self.conn,'fortun8te',pid,'following')
            db.add_edge(self.conn,'fortun8te',pid,'followers')
        self.conn.commit()
        edges,truncated=MV.recorded_connections(self.conn,ids,self.owner)
        self.assertEqual(len(edges),500)
        self.assertTrue(truncated)
        self.assertTrue(all(e['source'] in {*ids,self.owner} and e['target'] in {*ids,self.owner} for e in edges))
