import os; os.environ.setdefault('FL_NO_ORSLOT', '1')
"""Get-started checklist and the one-handle start flow."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base  # noqa: E402

import onboarding  # noqa: E402


def acct(**kw):
    return dict({'lane_id': 'lane-a', 'name': '@a', 'online': True, 'ig_id': '1', 'hold': None}, **kw)


def by_id(steps):
    return {s['id']: s for s in steps}


class ChecksTest(unittest.TestCase):
    def run_checks(self, accts, providers=(), backup=(None), agent=True, skipped=()):
        return by_id(onboarding.checks(accts, {'providers': list(providers)}, backup, agent, set(skipped)))

    def test_fresh_mac_needs_extension_first_with_a_wizard_action(self):
        s = self.run_checks([])
        self.assertEqual(s['server']['status'], 'ok')
        self.assertEqual((s['extension']['status'], s['extension']['action']['kind']), ('todo', 'wizard'))
        self.assertEqual(s['instagram']['status'], 'todo')
        self.assertFalse(onboarding.summarize(list(s.values()))['ready'])

    def test_connected_and_logged_in_is_ready_even_without_keys_or_backups(self):
        s = self.run_checks([acct()])
        self.assertEqual((s['extension']['status'], s['instagram']['status']), ('ok', 'ok'))
        summary = onboarding.summarize(list(s.values()))
        self.assertTrue(summary['ready'])
        self.assertFalse(summary['healthy'])   # optional steps still open
        self.assertEqual(summary['blocking'], 0)

    def test_extension_reports_each_profile(self):
        s = self.run_checks([acct(), acct(lane_id='lane-b', name='@b', online=False)])
        self.assertEqual([i['state'] for i in s['extension']['items']], ['ok', 'wait'])
        self.assertEqual(s['extension']['status'], 'ok')

    def test_offline_profiles_only_is_a_todo(self):
        s = self.run_checks([acct(online=False)])
        self.assertEqual(s['extension']['status'], 'todo')

    def test_logged_out_profile_points_at_the_profile(self):
        s = self.run_checks([acct(hold='login', ig_id='1')])
        self.assertEqual(s['instagram']['status'], 'todo')
        self.assertIn('logged out', s['instagram']['detail'])

    def test_online_without_instagram_login(self):
        s = self.run_checks([acct(ig_id=None)])
        self.assertEqual(s['instagram']['action']['href'], 'https://www.instagram.com/')

    def test_keys_and_backups(self):
        s = self.run_checks([acct()], providers=[{'key': 'sk-or-x'}, {'key': 'y', 'disabled': True}],
                            backup=('/x/leads-1.sqlite', 3.2))
        self.assertEqual((s['ai']['status'], s['backups']['status']), ('ok', 'ok'))
        self.assertIn('1 OpenRouter key added', s['ai']['detail'])

    def test_stale_backup_warns_and_skip_clears_optional_steps(self):
        s = self.run_checks([acct()], backup=('/x/leads-1.sqlite', 60), agent=False)
        self.assertEqual(s['backups']['status'], 'warn')
        s = self.run_checks([acct()], backup=None, agent=False, skipped=('ai', 'backups'))
        summary = onboarding.summarize(list(s.values()))
        self.assertTrue(summary['healthy'])

    def test_newest_backup_reads_the_backup_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / 'leads.sqlite'
            self.assertIsNone(onboarding.newest_backup(db_path))
            (Path(d) / 'backups').mkdir()
            old = Path(d) / 'backups' / 'leads-old.sqlite'
            new = Path(d) / 'backups' / 'leads-new.sqlite'
            old.write_text('x'); new.write_text('x')
            os.utime(old, (time.time() - 7200 * 10, time.time() - 7200 * 10))
            path, age = onboarding.newest_backup(db_path)
            self.assertTrue(path.endswith('leads-new.sqlite'))
            self.assertLess(age, 1)


class OnboardingApiTest(Base):
    def test_endpoint_on_an_empty_workspace(self):
        code, body = self.call('/api/onboarding')
        self.assertEqual(code, 200)
        self.assertFalse(body['ready'])
        self.assertEqual([s['id'] for s in body['steps']], ['server', 'extension', 'instagram', 'ai', 'backups'])
        self.assertEqual(body['flow']['state'], 'idle')

    def test_skip_hides_an_optional_step_and_rejects_others(self):
        self.assertEqual(self.call('/api/onboarding', {'skip': 'extension'})[0], 400)
        code, body = self.call('/api/onboarding', {'skip': 'ai'})
        self.assertEqual(code, 200)
        self.assertEqual(by_id(body['steps'])['ai']['status'], 'ok')
        code, body = self.call('/api/onboarding', {'skip': 'ai', 'on': False})
        self.assertEqual(by_id(body['steps'])['ai']['status'], 'todo')

    def test_start_queues_both_lists_resumes_and_reports_progress_in_words(self):
        code, body = self.call('/api/start', {'handle': 'https://www.instagram.com/Some.Brand/'})
        self.assertEqual(code, 200)
        self.assertEqual((body['queued'], body['started']), (2, True))
        self.assertFalse(self.call('/api/scraper/status')[1]['paused'])
        flow = self.call('/api/onboarding')[1]['flow']
        self.assertEqual(flow['state'], 'wait')   # no Chrome profile connected yet
        self.assertIn('Chrome profile', flow['headline'])
        self.assertEqual(self.call('/api/start', {'handle': 'some.brand'})[1]['queued'], 0)   # idempotent

    def test_start_rejects_a_bad_handle_and_keeps_pacing_hold(self):
        self.assertEqual(self.call('/api/start', {'handle': 'not a handle!!'})[0], 400)
        from datetime import datetime, timedelta, timezone
        import db
        db.set_setting(self.conn, 'cooldown', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.conn.commit()
        body = self.call('/api/start', {'handle': 'held.brand'})[1]
        self.assertEqual((body['queued'], body['started']), (2, False))
        self.assertIn('safety hold', body['note'])
        self.assertEqual(self.call('/api/onboarding')[1]['flow']['state'], 'wait')


if __name__ == '__main__':
    unittest.main()
