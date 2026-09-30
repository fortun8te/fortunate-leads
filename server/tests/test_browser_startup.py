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


if __name__ == '__main__':
    unittest.main()
