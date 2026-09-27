import tempfile
import unittest
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import collection_suggestions as suggestions


class CollectionSuggestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'suggestions.sqlite'))
        suggestions.ensure(self.conn)

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

    def account(self, **fields):
        now = datetime.now(timezone.utc)
        self.conn.execute("INSERT OR REPLACE INTO accounts(lane_id,handle,ig_id,last_seen,role,version) "
                          "VALUES('test','collector','900',?,'both','3.9.16')", (now.isoformat(),))
        for field, value in fields.items():
            self.conn.execute(f'UPDATE accounts SET {field}=? WHERE lane_id=?', (value, 'test'))
        return self.conn.execute("SELECT * FROM accounts WHERE lane_id='test'").fetchone()

    def test_owner_context_and_manual_fit_enter_without_inventing_relationships(self):
        self.person('strong_saved_fit')
        pid = self.person('owner_client', None)
        self.conn.execute('INSERT INTO owner_context VALUES(?,?,?,?)', (pid, '["client","worked_with"]', None, db.now()))
        pid = self.person('manual_fit', None)
        self.conn.execute("INSERT INTO tags VALUES(?,'Exceptional fit','signal','manual')", (pid,))
        rows = self.rows()
        self.assertEqual([r['handle'] for r in rows], ['owner_client','manual_fit','strong_saved_fit'])
        self.assertIn('your client',rows[0]['reason'])
        self.assertIn('recorded by you',rows[1]['reason'])
        for tag in ('Blocked', 'Not a fit'):
            self.conn.execute('INSERT OR REPLACE INTO tags VALUES(?,?,?,?)',(pid,tag,'signal','manual'))
            self.assertNotIn('manual_fit', [r['handle'] for r in self.rows()])

    def test_recent_owner_feedback_is_inside_bounded_pool(self):
        for i in range(8):
            pid = self.person('old_client'+str(i),None,'client')
            self.conn.execute('UPDATE marks SET updated_at=? WHERE person_id=?', ('2020-01-01',pid))
        pid = self.person('new_client',None,'client')
        self.conn.execute('UPDATE marks SET updated_at=? WHERE person_id=?',(db.now(),pid))
        with mock.patch.object(suggestions, 'OWNER_POOL', 2):
            self.assertIn('new_client', [r['handle'] for r in self.rows()])

    def test_failed_prior_direction_never_retried_and_hide_survives_rename(self):
        self.person('failed')
        for direction in ('followers','following'):
            self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list','FAILED',?,'error')", (direction,))
        pid = self.person('hidden',ig_id='101')
        suggestions.hide(self.conn,'@hidden')
        self.conn.execute("UPDATE people SET handle='renamed' WHERE id=?",(pid,))
        self.assertEqual(self.rows(), [])
        self.assertEqual(suggestions.suggest(self.conn)['history'][0]['state'],'hidden')

    def test_auto_default_one_target_normal_queue_and_durable_attempt(self):
        self.person('best',90)
        self.person('second',80)
        row = self.account()
        self.assertTrue(suggestions.enabled(self.conn))
        picked = suggestions.queue_when_idle(self.conn,row)
        self.assertEqual(picked['handle'], 'best')
        jobs = self.conn.execute('SELECT kind,seed,priority FROM jobs').fetchall()
        self.assertEqual([(j['kind'],j['seed'],j['priority']) for j in jobs], [('list','best',-10)]*2)
        self.assertIsNone(suggestions.queue_when_idle(self.conn,row))
        self.conn.execute("UPDATE jobs SET state='error'")
        self.conn.execute("DELETE FROM lists")
        self.conn.execute("DELETE FROM jobs")
        picked = suggestions.queue_when_idle(self.conn,row)
        self.assertEqual(picked['handle'],'second')
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collection_discovery').fetchone()[0],2)
        self.assertEqual(suggestions.suggest(self.conn)['added_today'],2)

    def test_manual_work_even_delayed_or_on_other_lane_wins(self):
        self.person('best')
        row=self.account()
        self.conn.execute("INSERT INTO jobs(kind,handle,state,retry_not_before) VALUES('profile','manual','queued','2099-01-01')")
        self.assertIsNone(suggestions.queue_when_idle(self.conn,row))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collection_discovery').fetchone()[0],0)

    def test_pauses_off_shared_hold_and_account_guards_do_not_populate(self):
        self.person('best')
        row=self.account()
        until=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        for key,value in [('auto_discover',False),('paused',True),('paused_lists',True),('cooldown',until),('cooldown','invalid')]:
            with self.subTest(setting=key,value=value):
                db.set_setting(self.conn,key,value)
                self.assertIsNone(suggestions.queue_when_idle(self.conn,row))
                self.conn.execute('DELETE FROM settings WHERE key=?',(key,))
        for fields in ({'paused':1},{'hold':'login'},{'list_cool_until':until},
                       {'list_endpoint_until':until},{'last_seen':'2020-01-01T00:00:00+00:00'},
                       {'role':'bios'},{'budget':'{"list":1}', 'today':'{"list":1}'}):
            with self.subTest(account=fields):
                row=self.account(**fields)
                self.assertIsNone(suggestions.queue_when_idle(self.conn,row))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collection_discovery').fetchone()[0],0)

    def test_unknown_fit_follow_evidence_does_not_create_trust(self):
        pid=self.person('follow_only',None)
        self.conn.execute('INSERT INTO map_person_degree(person_id,degree) VALUES(?,99)',(pid,))
        self.assertEqual(self.rows(),[])


if __name__=='__main__': unittest.main()
