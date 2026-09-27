import tempfile
import unittest
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import collection_suggestions as suggestions
import discovery_policy


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
                      {'rank_pool':2000,'owner_pool':500,'limit':6,'following_only':False}))
        self.assertIn('verdicts_score',plan)
        self.assertIn('verdicts_prefilter',plan)
        self.assertNotIn('SCAN edges',plan)
        self.assertNotIn('SCAN edge_evidence',plan)

    def account(self, **fields):
        now = datetime.now(timezone.utc)
        self.conn.execute("INSERT OR REPLACE INTO accounts(lane_id,handle,ig_id,last_seen,role,version) "
                          "VALUES('test','collector','900',?,'both','3.9.17')", (now.isoformat(),))
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
        self.person('third',70)
        row = self.account()
        self.assertTrue(suggestions.enabled(self.conn))
        picked = suggestions.queue_when_idle(self.conn,row)
        self.assertEqual(picked['handle'], 'best')
        jobs = self.conn.execute('SELECT kind,seed,priority FROM jobs').fetchall()
        self.assertEqual([(j['kind'],j['seed'],j['priority']) for j in jobs], [('list','best',-10)])
        self.assertEqual(picked['directions'], ['following'])
        self.assertIsNone(suggestions.queue_when_idle(self.conn,row))
        self.conn.execute("UPDATE jobs SET state='error'")
        self.conn.execute("DELETE FROM lists")
        self.conn.execute("DELETE FROM jobs")
        picked = suggestions.queue_when_idle(self.conn,row)
        self.assertEqual(picked['handle'],'second')
        self.conn.execute("DELETE FROM lists")
        self.conn.execute("DELETE FROM jobs")
        picked = suggestions.queue_when_idle(self.conn,row)
        self.assertEqual(picked['handle'], 'third')
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collection_discovery').fetchone()[0],3)
        self.assertEqual(suggestions.suggest(self.conn)['added_today'],3)

    def test_manual_lists_win_but_profile_backlog_does_not_block_discovery(self):
        self.person('best')
        row=self.account()
        self.conn.execute("INSERT INTO jobs(kind,handle,state,retry_not_before) VALUES('profile','manual','queued','2099-01-01')")
        self.assertEqual(suggestions.queue_when_idle(self.conn,row)['handle'],'best')
        self.conn.execute("DELETE FROM jobs WHERE kind='list'")
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,state,retry_not_before) "
                          "VALUES('list','manual_seed','followers','queued','2099-01-01')")
        self.assertIsNone(suggestions.queue_when_idle(self.conn,row))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collection_discovery').fetchone()[0],1)

    def test_pending_limit_bounds_delayed_work_and_replenishes_after_completion(self):
        for i in range(4):
            self.person('fresh' + str(i))
        row = self.account()
        for expected in ('fresh0', 'fresh1'):
            self.assertEqual(suggestions.queue_when_idle(self.conn, row)['handle'], expected)
            self.conn.execute("UPDATE jobs SET retry_not_before='2099-01-01' WHERE state='queued'")
        self.assertEqual(suggestions._pending_auto(self.conn), 2)
        self.assertIsNone(suggestions.queue_when_idle(self.conn, row))
        self.conn.execute("UPDATE jobs SET state='done' WHERE seed='fresh0'")
        self.assertEqual(suggestions.queue_when_idle(self.conn, row)['handle'], 'fresh2')
        self.assertEqual(suggestions._pending_auto(self.conn), 2)

    def test_renamed_automatic_target_still_counts_toward_pending_bound(self):
        pid = self.person('fresh')
        row = self.account()
        suggestions.queue_when_idle(self.conn, row)
        self.conn.execute("UPDATE people SET handle='renamed' WHERE id=?", (pid,))
        self.conn.execute("UPDATE jobs SET seed='renamed' WHERE seed='fresh'")
        self.assertEqual(suggestions._pending_auto(self.conn), 1)

    def test_equal_fit_prefers_less_observed_source_overlap_without_raising_fit(self):
        repeated = self.person('repeated')
        fresh = self.person('fresh')
        self.conn.execute('INSERT INTO map_person_degree(person_id,degree) VALUES(?,20)', (repeated,))
        self.conn.execute('INSERT INTO map_person_degree(person_id,degree) VALUES(?,1)', (fresh,))
        self.assertEqual([r['handle'] for r in self.rows()], ['fresh', 'repeated'])
        self.assertEqual([r['business_fit'] for r in self.rows()], [80, 80])

    def test_automatic_discovery_does_not_add_untouched_followers_or_refresh_following(self):
        self.person('followers_only', 95, following=0)
        self.person('following_already_read', 90)
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('following_already_read','following','done')")
        self.person('fresh', 70)
        picked = suggestions.queue_when_idle(self.conn, self.account())
        self.assertEqual((picked['handle'], picked['directions']), ('fresh', ['following']))

    def test_future_retry_does_not_starve_fresh_discovery(self):
        self.person('fresh')
        row = self.account()
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,state,retry_not_before) "
                          "VALUES('list','manual_seed','followers','queued','2099-01-01')")
        self.assertEqual(suggestions.queue_when_idle(self.conn, row)['handle'], 'fresh')
        pending = self.conn.execute("SELECT state,retry_not_before FROM jobs WHERE seed='manual_seed'").fetchone()
        self.assertEqual(tuple(pending), ('queued', '2099-01-01'))

    def test_ready_manual_list_and_active_other_lane_take_precedence(self):
        self.person('fresh')
        row = self.account()
        db.queue_list(self.conn, 'manual_seed', 'following', priority=100)
        self.assertIsNone(suggestions.queue_when_idle(self.conn, row))
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.conn.execute("UPDATE jobs SET state='leased',lane='other',leased_until=?", (until,))
        self.assertIsNone(suggestions.queue_when_idle(self.conn, row))

    def test_follower_route_wait_allows_only_unrelated_following_discovery(self):
        self.person('followers_only', 95, following=0)
        self.person('fresh')
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        row = self.account(list_endpoint_until=until)
        picked = suggestions.queue_when_idle(self.conn, row)
        self.assertEqual(picked['handle'], 'fresh')
        self.assertEqual(picked['directions'], ['following'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT list_endpoint_until FROM accounts').fetchone()[0], until)

    def test_route_filter_precedes_suggestion_limit(self):
        for i in range(21):
            self.person('followers_only' + str(i), 95, following=0)
        self.person('fresh', 70)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        picked = suggestions.queue_when_idle(self.conn, self.account(list_endpoint_until=until))
        self.assertEqual((picked['handle'], picked['directions']), ('fresh', ['following']))

    def test_auto_discovery_never_uses_reserved_main(self):
        self.person('fresh')
        row = self.account(is_main=1)
        self.conn.execute("INSERT INTO accounts(lane_id,handle,ig_id,role) VALUES('alt','alt','901','both')")
        db.set_setting(self.conn, 'main_list_share', 0)
        self.assertIsNone(suggestions.queue_when_idle(self.conn, row))

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
                       {'last_seen':'2020-01-01T00:00:00+00:00'},
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

    def test_completion_policy_is_explicit_and_does_not_change_manual_preview(self):
        self.person('large', 90, following=6000)
        self.person('small', 80, following=90)
        self.assertEqual(suggestions.policy(self.conn), 'saved_fit')
        self.assertEqual(suggestions.suggest(self.conn, 1, following_only=True, automatic=True)['suggestions'][0]['handle'], 'large')
        suggestions.set_policy(self.conn, 'bounded_completion')
        self.assertEqual(self.rows()[0]['handle'], 'large')
        self.assertEqual(suggestions.suggest(self.conn, 1, following_only=True, automatic=True)['suggestions'][0]['handle'], 'small')
        with self.assertRaises(ValueError):
            suggestions.set_policy(self.conn, 'fastest')

    def test_completion_policy_preserves_owner_evidence_and_strong_fit_band(self):
        self.person('small_good', 50, following=10)
        self.person('large_strong', 90, following=6000)
        self.person('client', None, 'client', following=7000)
        suggestions.set_policy(self.conn, 'bounded_completion')
        self.assertEqual(suggestions.queue_when_idle(self.conn, self.account())['handle'], 'client')
        self.conn.execute("UPDATE jobs SET state='done'")
        self.assertEqual(suggestions.queue_when_idle(self.conn, self.account())['handle'], 'large_strong')

    def test_fourth_admission_advances_large_or_unknown_original_target(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                self.conn.execute('DELETE FROM collection_discovery')
                self.conn.execute('DELETE FROM jobs')
                self.conn.execute('DELETE FROM lists')
                self.conn.execute('DELETE FROM people')
                self.person('original', 95, following=None if unknown else 7000)
                for i in range(5):
                    self.person('small' + str(i), 80, following=10)
                suggestions.set_policy(self.conn, 'bounded_completion')
                row = self.account()
                selected = []
                for _ in range(4):
                    selected.append(suggestions.queue_when_idle(self.conn, row)['handle'])
                    self.conn.execute("UPDATE jobs SET state='done'")
                self.assertEqual(selected, ['small0', 'small1', 'small2', 'original'])

    def test_completion_ranking_is_read_only_and_keeps_partial_excluded(self):
        self.person('partial', 99, following=2)
        self.conn.execute("INSERT INTO lists(seed,direction,state,cursor,received,total) VALUES('partial','following','partial','keep',1,2)")
        self.person('fresh', 80, following=20)
        suggestions.set_policy(self.conn, 'bounded_completion')
        before = self.conn.total_changes
        rows = suggestions.suggest(self.conn, 1, following_only=True, automatic=True)['suggestions']
        self.assertEqual(rows[0]['handle'], 'fresh')
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(tuple(self.conn.execute("SELECT state,cursor,received FROM lists WHERE seed='partial'").fetchone()), ('partial','keep',1))

    def test_request_estimate_keeps_unknown_and_counts_target_lookup(self):
        self.assertIsNone(discovery_policy.estimated_requests(None))
        self.assertEqual(discovery_policy.estimated_requests(1), 2)
        self.assertEqual(discovery_policy.estimated_requests(90), 3)
        self.assertEqual(discovery_policy.estimated_requests(91), 4)


if __name__=='__main__': unittest.main()
