"""Connection pages prioritize recorded network evidence without changing fit."""
import json
import tempfile
import unittest
from pathlib import Path
from map_view_fixture import make, TS
import db
import map_layout as ML
import map_view as MV


class MapEvidenceOrderTests(unittest.TestCase):
    def test_qualified_influencer_and_collection_source_do_not_outrank_owner_follow(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'map.sqlite'
            fixture=make(path,people=0,sources=4)
            conn=db.connect(path)
            try:
                conn.executemany('INSERT INTO people(id,handle,followers,first_seen,updated_at) VALUES(?,?,?,?,?)',[
                    (1001,'popular_without_connection',100000000,TS,TS),
                    (1002,'recorded_owner_follow',100,TS,TS),
                ])
                source=fixture['source_ids']['src00']
                conn.executemany('INSERT INTO verdicts(person_id,score,tier,content_fit) VALUES(?,?,?,?)',[
                    (1001,100,'strong',100),(source,100,'strong',100),(1002,0,'no',0),
                ])
                conn.execute('INSERT INTO edges VALUES(?,?,?,?)',(fixture['owner'],1002,'following',TS))
                conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?)',(fixture['owner'],1002,'following',1,TS,TS))
                conn.commit();ML.build(path,modes=('closeness',))
                result=json.loads(MV.view(conn,path,{'cohort':['1'],'scope':['all'],'budget':['1000']},cache=False).body)
                nodes=result['nodes'];order=[n['id'] for n in nodes]
                self.assertLess(order.index(1002),order.index(1001))
                self.assertLess(order.index(1002),order.index(source))
                direct=next(n for n in nodes if n['id']==1002)
                popular=next(n for n in nodes if n['id']==1001)
                self.assertTrue(direct['followed']);self.assertFalse(popular['followed'])
                self.assertGreater(direct['closeness'],popular['closeness'])
                self.assertEqual(popular['fit'],100)
                self.assertEqual(popular['followers'],100000000)
            finally:
                conn.close();MV.close_pool()
