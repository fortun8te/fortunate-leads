"""Current fit presentation agrees across rows, facets and tag filters."""
import tempfile
import unittest
from pathlib import Path
from test_server import db, server


class TagProjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'tags.sqlite'))
        server.deepscout.ensure(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, name, fit, tier='hot', status=None):
        pid = db.upsert_person(self.conn, {'handle': name, 'bio': 'A business'})
        self.conn.execute('INSERT INTO verdicts(person_id,content_fit,tier,role,model) VALUES(?,?,?,?,?)',
                          (pid, fit, tier, 'buyer', 'rules'))
        if status:
            self.conn.execute('INSERT INTO marks(person_id,status) VALUES(?,?)', (pid,status))
        return pid

    def names(self, pid):
        return {t['tag'] for t in server.api_person(self.conn, {}, {}, pid)['tags']}

    def test_thresholds_and_filtered_facets_agree_without_saved_tags(self):
        ids = [self.person('person'+str(i), fit) for i,fit in enumerate([None,44,45,69,70,90])]
        unread = self.person('unread',90,'unread')
        self.assertFalse(self.names(unread) & {'Fit: strong','Fit: good'})
        for tag, expected in [('Fit: good',ids[2:4]), ('Fit: strong',ids[4:])]:
            rows = server.api_leads(self.conn, {'tags':[tag]}, {})['rows']
            self.assertEqual({r['id'] for r in rows},set(expected))
            self.assertTrue(all(tag in {t['tag'] for t in r['tags']} for r in rows))
            facet = next(t for t in server.tag_facets(self.conn,{}) if t['tag']==tag)
            self.assertEqual((facet['count'],facet['total']),(2,2))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tags').fetchone()[0],0)
        self.assertNotIn('AI: Top fit',self.names(ids[-1]))

    def test_revised_fit_replaces_stale_label_and_does_not_double_count(self):
        pid = self.person('changed',80)
        self.conn.execute("INSERT INTO tags VALUES(?,'Fit: strong','signal','auto')",(pid,))
        facet = next(t for t in server.tag_facets(self.conn,{}) if t['tag']=='Fit: strong')
        self.assertEqual(facet['total'],1)
        self.conn.execute('UPDATE verdicts SET content_fit=50 WHERE person_id=?',(pid,))
        self.assertIn('Fit: good',self.names(pid))
        self.assertNotIn('Fit: strong',self.names(pid))
        self.assertEqual(server.api_leads(self.conn,{'tags':['Fit: strong']},{})['total'],0)
        self.assertEqual(server.api_leads(self.conn,{'not':['Fit: good']},{})['total'],0)
        self.assertEqual(server.api_leads(self.conn,{'any':['Fit: strong,Fit: good']},{})['total'],1)

    def test_clients_rejections_and_manual_client_not_acquisition_prospects(self):
        ids = [self.person('owner'+str(i),90,status=status) for i,status in enumerate(['client','no',None])]
        self.conn.execute("INSERT INTO tags VALUES(?,'Client','signal','manual')",(ids[-1],))
        for pid in ids:
            self.assertFalse(self.names(pid) & {'Fit: strong','Fit: good'})
        self.assertEqual(server.api_leads(self.conn,{'tags':['Fit: strong'],'status':['all']},{})['total'],0)

    def test_top_fit_requires_saved_external_judgment_and_current_eligible_verdict(self):
        pid = self.person('external',80)
        self.conn.execute("INSERT INTO tags VALUES(?,'AI: Top fit','ai','auto')",(pid,))
        self.assertIn('AI: Top fit',self.names(pid))
        self.conn.execute('UPDATE verdicts SET content_fit=50 WHERE person_id=?',(pid,))
        self.assertNotIn('AI: Top fit',self.names(pid))
        self.assertEqual(server.api_leads(self.conn,{'tags':['AI: Top fit']},{})['total'],0)


if __name__ == '__main__':
    unittest.main()
