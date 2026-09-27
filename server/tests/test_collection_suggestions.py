import tempfile
import unittest
from pathlib import Path
from test_server import db, server
import collection_suggestions as suggestions


class CollectionSuggestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'suggestions.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, name, fit=80, status=None, **fields):
        pid = db.upsert_person(self.conn, {'handle': name, 'followers':2500, 'following':100, **fields})
        self.conn.execute('INSERT INTO verdicts(person_id,content_fit,score,prefilter,tier) VALUES(?,?,?,?,?)',
                          (pid, fit, fit, fit, 'hot' if fit is not None else 'unread'))
        if status:
            self.conn.execute('INSERT INTO marks(person_id,status) VALUES(?,?)', (pid,status))
        return pid

    def rows(self):
        return suggestions.suggest(self.conn)['suggestions']

    def test_saved_fit_and_owner_evidence_rank_without_any_network_or_writes(self):
        self.person('good_fit',50)
        self.person('strong_fit',80)
        self.person('known_client',None,'client')
        before = self.conn.total_changes
        rows = self.rows()
        self.assertEqual([r['handle'] for r in rows],['known_client','strong_fit','good_fit'])
        self.assertEqual(rows[0]['directions'],['followers','following'])
        self.assertIn('your client',rows[0]['reason'])
        self.assertIn('2,500 followers on saved profile',rows[1]['reason'])
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_excludes_self_collectors_private_rejected_unread_and_weak(self):
        self.person('fortun8te')
        self.person('myself'); self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('myself',1)")
        self.person('collector',ig_id='55'); self.conn.execute("INSERT INTO accounts(lane_id,handle,ig_id) VALUES('bot','collector','55')")
        self.person('case_collector')
        self.conn.execute("INSERT INTO accounts(lane_id,handle) VALUES('casebot','CASE_COLLECTOR')")
        self.person('renamed_collector',ig_id='56')
        self.conn.execute("INSERT INTO account_identity_state(lane_id,ig_id,day) VALUES('old','56','2026-09-27')")
        self.person('private',is_private=1)
        self.person('rejected',90,'no')
        self.person('weak',40)
        self.person('unread',None)
        self.assertEqual(self.rows(),[])

    def test_only_uncollected_directions_and_never_requeues_partial_lists(self):
        self.person('one_side')
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('one_side','following','partial')")
        self.person('queued')
        for direction in ('followers','following'):
            self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list','queued',?,'queued')",(direction,))
        self.person('complete')
        for direction in ('followers','following'):
            self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('complete',?,'done')",(direction,))
        self.assertEqual([(r['handle'],r['directions']) for r in self.rows()],[('one_side',['followers'])])

    def test_zero_lists_not_offered_unknown_counts_not_invented(self):
        self.person('empty',followers=0,following=0)
        self.person('unknown',followers=None,following=None)
        rows=self.rows()
        self.assertEqual([r['handle'] for r in rows],['unknown'])
        self.assertNotIn('followers',rows[0]['reason'])
        self.assertIsNone(rows[0]['followers'])

    def test_explicit_owner_no_overrides_legacy_client_tag(self):
        pid=self.person('legacy',None)
        self.conn.execute("INSERT INTO tags VALUES(?,'Client','signal','manual')",(pid,))
        self.assertEqual(self.rows()[0]['handle'],'legacy')
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'no')",(pid,))
        self.assertEqual(self.rows(),[])

    def test_bound_and_indexed_candidate_selection(self):
        for i in range(30): self.person('fit'+str(i))
        self.assertEqual(len(suggestions.suggest(self.conn,100)['suggestions']),20)
        plan=' '.join(str(tuple(r)) for r in self.conn.execute('EXPLAIN QUERY PLAN '+suggestions.CANDIDATES,
                      {'rank_pool':2000,'owner_pool':500,'limit':6}))
        self.assertIn('verdicts_score',plan)
        self.assertIn('verdicts_prefilter',plan)
        self.assertNotIn('SCAN edges',plan)
        self.assertNotIn('SCAN edge_evidence',plan)


if __name__=='__main__': unittest.main()
