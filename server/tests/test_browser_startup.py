import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_startup
import db


class BrowserStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.conn = db.init(str(self.root / 'data/leads.sqlite'))
        self.addCleanup(self.conn.close)
        self.profiles = [{'directory': 'Profile ' + str(i + 2), 'lane_id': 'lane' + str(i), 'ig_id': str(i + 100)} for i in range(3)]
        for profile in self.profiles:
            self.conn.execute('INSERT INTO accounts(lane_id,ig_id) VALUES (?,?)', (profile['lane_id'], profile['ig_id']))
        db.set_setting(self.conn, 'paused', False)
        db.set_setting(self.conn, 'paused_lists', False)
        db.set_setting(self.conn, 'paused_bios', False)
        self.conn.commit()
        self.config = self.root / 'data/browser-startup.json'
        self.write_config()
        self.now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.launch = Mock(return_value=Mock(wait=Mock(return_value=0)))

    def write_config(self, enabled=True):
        self.config.write_text(json.dumps({'enabled': enabled, 'profiles': self.profiles}))

    def run_launch(self, **kwargs):
        return browser_startup.launch_saved(self.root, self.conn, popen=self.launch, now=kwargs.get('now', self.now))

    def test_three_explicit_profiles_reuse_persistent_sessions_without_foreground_tabs(self):
        self.assertEqual(self.run_launch()['requested'], 3)
        self.assertEqual([call.args[0][1] for call in self.launch.call_args_list],
                         ['--profile-directory=Profile 2', '--profile-directory=Profile 3', '--profile-directory=Profile 4'])
        for call in self.launch.call_args_list:
            self.assertEqual(call.args[0][-1], '--no-startup-window')
            self.assertEqual(len(call.args[0]), 3)
        self.assertFalse(db.get_setting(self.conn, 'paused'))

    def test_main_and_duplicate_main_identity_never_launch_for_collection(self):
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='lane0'")
        self.conn.execute("UPDATE accounts SET ig_id='100' WHERE lane_id='lane1'")
        self.profiles[1]['ig_id'] = '100'
        self.write_config()
        self.conn.commit()
        self.assertEqual(self.run_launch()['requested'], 1)
        self.assertEqual(self.launch.call_args.args[0][1], '--profile-directory=Profile 4')

    def test_no_config_or_disabled_means_no_launch(self):
        self.write_config(False)
        self.assertEqual(self.run_launch()['requested'], 0)
        self.config.unlink()
        self.assertEqual(self.run_launch()['requested'], 0)
        self.launch.assert_not_called()

    def test_stop_and_paused_lanes_are_respected(self):
        db.set_setting(self.conn, 'paused', True)
        self.conn.commit()
        self.assertEqual(self.run_launch()['requested'], 0)
        db.set_setting(self.conn, 'paused', False)
        self.conn.execute("UPDATE accounts SET paused=1 WHERE lane_id='lane1'")
        self.conn.commit()
        self.assertEqual(self.run_launch()['requested'], 2)

    def test_recheck_stop_between_profiles(self):
        def pause_after_first(*args, **kwargs):
            db.set_setting(self.conn, 'paused', True)
            self.conn.commit()
            return Mock(wait=Mock(return_value=0))
        self.launch.side_effect = pause_after_first
        self.assertEqual(self.run_launch()['requested'], 1)

    def test_changed_identity_and_non_chrome_account_are_not_opened(self):
        self.conn.execute("UPDATE accounts SET ig_id='999' WHERE lane_id='lane1'")
        self.conn.execute("UPDATE accounts SET collection_backend='mobile' WHERE lane_id='lane2'")
        self.conn.commit()
        self.assertEqual(self.run_launch()['requested'], 1)

    def test_stage_scope_and_duplicate_startup_are_bounded(self):
        self.conn.execute("UPDATE accounts SET role='bios' WHERE lane_id='lane1'")
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        self.assertEqual(self.run_launch()['requested'], 2)
        self.assertEqual(self.run_launch()['requested'], 0)
        self.assertEqual(self.run_launch(now=self.now + timedelta(seconds=61))['requested'], 2)

    def test_invalid_and_duplicate_profile_configuration_fails_before_launch(self):
        for bad in ('../Default', '--user-data-dir=other', 'Profile 0'):
            self.profiles[0]['directory'] = bad
            self.write_config()
            with self.assertRaises(ValueError):
                self.run_launch()
        self.profiles[0]['directory'] = self.profiles[1]['directory']
        self.write_config()
        with self.assertRaises(ValueError):
            self.run_launch()
        self.launch.assert_not_called()

    def test_isolated_preview_does_not_open_machine_apps(self):
        preview = db.init(str(self.root / 'preview.sqlite'))
        self.addCleanup(preview.close)
        with patch.object(browser_startup.threading, 'Thread') as thread:
            self.assertFalse(browser_startup.schedule(self.root, preview))
            thread.assert_not_called()

class ExplicitConnectionTests(unittest.TestCase):
    write_config = BrowserStartupTests.write_config

    def setUp(self):
        BrowserStartupTests.setUp(self)
        self.selected = [{'lane_id': p['lane_id'], 'ig_id': p['ig_id']} for p in self.profiles[:2]]
        self.command = {'action': 'resume_selected_accounts', 'accounts': self.selected}
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {p['lane_id']: p['ig_id'] for p in self.selected}})
        db.set_setting(self.conn, 'instagram_scraping_warning', {'kind':'scraping_warning','lane':'lane2','ig_id':'102','at':'original','message':'Review warning'})
        db.set_setting(self.conn, 'budget', {'list':200,'profile':100})
        db.set_setting(self.conn, 'paused_lists', True)
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.execute("UPDATE accounts SET hold='challenge',paused=1 WHERE lane_id='lane2'")
        self.conn.execute("UPDATE accounts SET version='3.9.30',state='idle'")
        self.conn.commit()

    def begin(self, command=None):
        with patch.object(browser_startup, 'schedule', return_value=True):
            self.assertTrue(browser_startup.begin(self.root, self.conn, command or self.command))
        return db.get_setting(self.conn, 'collection_startup')

    def fresh_beats(self, stamp):
        self.conn.execute("UPDATE accounts SET last_seen=? WHERE lane_id IN ('lane0','lane1')",
                          (datetime.fromtimestamp(stamp, timezone.utc).isoformat(),))
        self.conn.commit()

    def test_closed_selected_profiles_open_before_resume_and_need_fresh_identity_beats(self):
        intent = self.begin()
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        clock = [intent['requested_at']]
        def beat(_):
            clock[0] += 1
            self.fresh_beats(clock[0])
        browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:clock[0],sleep=beat)
        self.assertEqual(self.launch.call_count,2)
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'started')
        self.assertFalse(db.get_setting(self.conn,'paused_lists'))
        self.assertEqual(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='lane2'").fetchone()[0],'challenge')
        self.assertEqual(db.get_setting(self.conn,'budget'),{'list':200,'profile':100})

    def test_connect_does_not_change_collection_intent(self):
        intent=self.begin({'action':'connect_accounts'})
        self.fresh_beats(intent['requested_at'] + 1)
        browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:intent['requested_at']+2)
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'connected')
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertEqual(self.launch.call_count,2)

    def test_stop_cancels_pending_resume(self):
        intent=self.begin()
        browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:intent['requested_at'],
                                    sleep=lambda _:browser_startup.cancel(self.conn))
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'cancelled')
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_launch_is_not_readiness_and_timeout_preserves_pause(self):
        intent=self.begin(); clock=[intent['requested_at']]
        browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:clock[0],sleep=lambda _:clock.__setitem__(0,clock[0]+31))
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'failed')
        self.assertEqual(self.launch.call_count,2)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_wrong_identity_and_new_warning_cancel_start(self):
        for change in ('identity','warning'):
            with self.subTest(change=change):
                intent=self.begin(); clock=[intent['requested_at']]
                def changed(_):
                    clock[0]+=1
                    self.fresh_beats(clock[0])
                    if change=='identity':
                        self.conn.execute("UPDATE accounts SET ig_id='999' WHERE lane_id='lane0'")
                    else:
                        warning=db.get_setting(self.conn,'instagram_scraping_warning');warning['at']='new-warning'
                        db.set_setting(self.conn,'instagram_scraping_warning',warning)
                    self.conn.commit()
                browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:clock[0],sleep=changed)
                self.assertEqual(browser_startup.snapshot(self.conn)['state'],'failed')
                self.assertTrue(db.get_setting(self.conn,'paused_lists'))
                self.conn.execute("UPDATE accounts SET ig_id='100',last_seen=NULL WHERE lane_id='lane0'")
                self.conn.commit()

    def test_failed_dispatch_is_saved_and_does_not_claim_connected(self):
        self.begin()
        self.launch.side_effect=OSError('Chrome unavailable')
        browser_startup.run_pending(self.root,self.conn,popen=self.launch)
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'failed')
        self.assertEqual(db.get_setting(self.conn,'browser_startup:lane0')['state'],'failed')
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_fresh_online_selected_resume_needs_no_profile_config_or_launch(self):
        self.config.unlink()
        self.fresh_beats(datetime.now(timezone.utc).timestamp())
        self.assertFalse(browser_startup.begin(self.root,self.conn,self.command,only_if_offline=True))
        self.launch.assert_not_called()


    def test_stop_between_resume_commit_and_startup_confirmation_wins(self):
        intent=self.begin();self.fresh_beats(intent['requested_at']+1)
        original=browser_startup.control.apply
        def resume_then_stop(conn, body):
            original(conn,body)
            browser_startup.cancel(conn)
            original(conn,{'action':'pause','stage':'collection'})
        with patch.object(browser_startup.control,'apply',side_effect=resume_then_stop):
            browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:intent['requested_at']+2)
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'cancelled')
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_quick_stop_then_start_hands_new_intent_to_next_worker(self):
        first=self.begin();tasks=[]
        def thread(*args,**kwargs):
            tasks.append(kwargs['target'])
            return Mock(start=Mock())
        def replace_intent(repo,conn):
            browser_startup.cancel(conn)
            self.assertTrue(browser_startup.begin(repo,conn,self.command))
        with patch.object(browser_startup.sys,'platform','darwin'), patch.object(browser_startup.threading,'Thread',side_effect=thread), patch.object(browser_startup,'run_pending',side_effect=replace_intent):
            self.assertTrue(browser_startup.schedule(self.root,self.conn))
            self.assertEqual(len(tasks),1)
            tasks[0]()
            self.assertEqual(len(tasks),2)
            self.assertNotEqual(db.get_setting(self.conn,'collection_startup')['id'],first['id'])
            with patch.object(browser_startup,'run_pending') as next_worker:
                tasks[1]()
                self.assertEqual(next_worker.call_count,1)
        self.assertFalse(browser_startup._START_LOCK.locked())

    def test_start_during_connect_upgrades_same_intent_without_duplicate_dispatch(self):
        first=self.begin({'action':'connect_accounts'})
        with patch.object(browser_startup,'schedule',return_value=True):
            self.assertTrue(browser_startup.begin(self.root,self.conn,self.command,only_if_offline=True))
        upgraded=db.get_setting(self.conn,'collection_startup')
        self.assertEqual(upgraded['id'],first['id'])
        self.assertEqual(upgraded['command'],self.command)
        self.fresh_beats(first['requested_at']+1)
        browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:first['requested_at']+2)
        self.assertEqual(self.launch.call_count,2)
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'started')


    def test_heartbeat_after_deadline_cannot_restart_collection(self):
        intent=self.begin();clock=[intent['requested_at']]
        def late_beat(_):
            clock[0]=intent['deadline_at']+1
            self.fresh_beats(clock[0])
        browser_startup.run_pending(self.root,self.conn,popen=self.launch,clock=lambda:clock[0],sleep=late_beat)
        self.assertEqual(browser_startup.snapshot(self.conn)['state'],'failed')
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))


if __name__ == '__main__':
    unittest.main()
