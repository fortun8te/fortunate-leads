import tempfile
import unittest
from pathlib import Path
from test_server import db, server
import owner_relationships

class OwnerRelationships(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.conn=db.init(str(Path(self.tmp.name)/'test.sqlite'))
        self.ids={name:db.upsert_person(self.conn,{'handle':name}) for name in ['follower','following','both','historical','elsewhere']}
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('fortun8te',1)")
        for name,seed,directions,active in [('follower','fortun8te',['followers'],1),('following','fortun8te',['following'],1),('both','fortun8te',['followers','following'],1),('historical','fortun8te',['followers'],0),('elsewhere','another',['followers'],1)]:
            for direction in directions:
                args=(seed,self.ids[name],direction)
                self.conn.execute('INSERT INTO edges VALUES(?,?,?,?)',(*args,'2026-09-01'))
                self.conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?)',(*args,active,'2026-09-02','2026-09-03'))
        server.set_status(self.conn,[self.ids['follower']],status='no')
        self.conn.commit()
    def tearDown(self):
        self.conn.close();self.tmp.cleanup()
    def test_directions_are_independent_and_absence_or_other_lists_never_imply_follow(self):
        facts=owner_relationships.facts(self.conn,self.ids.values())
        self.assertEqual([facts[self.ids[n]]['owner_relationship'] for n in self.ids],['follows','followed','mutual',None,None])
        self.assertEqual(facts[self.ids['follower']]['relationship_evidence'],[{'seed':'fortun8te','direction':'followers','observed_at':'2026-09-02'}])
    def test_filter_includes_unread_and_rejected_when_all_requested_without_followback(self):
        out=server.api_leads(self.conn,{'relationship':['follows'],'status':['all']},{})
        self.assertEqual({r['id'] for r in out['rows']},{self.ids['follower'],self.ids['both']})
        follower=next(r for r in out['rows'] if r['id']==self.ids['follower'])
        self.assertEqual(follower['owner_relationship'],'follows')
        self.assertEqual(next(r for r in out['rows'] if r['id']==self.ids['both'])['tier'],'unread')
        self.assertEqual(follower['status'],'no')
        out=server.api_leads(self.conn,{'relationship':['follows'],'status':['no']},{})
        self.assertEqual([r['id'] for r in out['rows']],[self.ids['follower']])
    def test_mutual_requires_both_and_bad_filter_is_rejected(self):
        out=server.api_leads(self.conn,{'relationship':['mutual'],'status':['all']},{})
        self.assertEqual([r['id'] for r in out['rows']],[self.ids['both']])
        with self.assertRaises(server.Bad): server.lead_filter({'relationship':['friend']})
