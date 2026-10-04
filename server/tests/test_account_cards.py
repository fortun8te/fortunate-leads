"""Account cards: every state in plain words, one primary action, live updates that cost a 304 when nothing changed."""
import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base, db, server  # noqa: E402

import account_view  # noqa: E402
import accounts  # noqa: E402
import browser_startup  # noqa: E402
from backend.account_cards import AccountCardService  # noqa: E402
from backend.common import Bad  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat(timespec='microseconds')


class CardBase(Base):
    def lane(self, lane='lane-a', ig='101', handle='acct.a', **fields):
        self.call('/api/ext/heartbeat', {'lane_id': lane, 'version': '3.9.30', 'state': 'idle', 'account': {'ig_id': ig, 'handle': handle}})
        if fields:
            sets = ', '.join(f'{k}=?' for k in fields)
            self.conn.execute(f'UPDATE accounts SET {sets} WHERE lane_id=?', (*fields.values(), lane))
            self.conn.commit()

    def cards(self):
        return {c['lane_id']: c for c in self.call('/api/accounts/live')[1]['cards']}

    def card(self, lane='lane-a'):
        return self.cards()[lane]

    def setting(self, key, value):
        db.set_setting(self.conn, key, value)
        self.conn.commit()


class TheStates(CardBase):
    def test_a_healthy_idle_account_is_ready_and_offers_pause(self):
        self.lane()
        c = self.card()
        self.assertEqual((c['state'], c['headline'], c['action']['id'], c['kind']), ('ready', 'Ready', 'pause', 'collector'))
        self.assertEqual(c['avatar']['initial'], 'A')

    def test_collecting_names_what_it_is_reading(self):
        self.lane()
        self.call('/api/seeds/batch', {'text': 'brand', 'directions': ['following']})
        job = self.call('/api/ext/next?lane=lane-a&ig_id=101&handle=acct.a')[1]['job']
        self.assertEqual(job['seed'], 'brand')
        c = self.card()
        self.assertEqual((c['state'], c['headline'], c['detail']), ('collecting', 'Collecting', "Reading @brand's following list."))

    def test_the_main_account_is_personal_and_has_no_allowance(self):
        self.lane(handle='me.main', is_main=1)
        c = self.card()
        self.assertEqual((c['state'], c['kind'], c['allowance']), ('personal', 'main', None))
        self.assertEqual(c['action']['id'], 'inbox')
        self.assertNotIn('pause', json.dumps(c['action']))
        self.assertEqual(self.call('/api/accounts/live')[1]['main'], {'handle': 'me.main'})

    def test_offline_says_so_and_only_offers_to_open_chrome_when_a_profile_is_saved(self):
        self.lane(last_seen='2026-01-01T00:00:00+00:00')
        self.assertEqual((self.card()['state'], self.card()['action']), ('offline', None))
        with mock.patch.object(browser_startup, 'read_config', return_value=[{'lane_id': 'lane-a'}]):
            row = self.conn.execute('SELECT * FROM accounts WHERE lane_id=?', ('lane-a',)).fetchone()
            payload = account_view.build(self.conn, ROOT, True)
            self.assertEqual(payload['cards'][0]['action']['id'], 'connect')
            payload = account_view.build(self.conn, ROOT, False)
            self.assertIsNone(payload['cards'][0]['action'])      # a saved-data workspace cannot open Chrome

    def test_login_and_security_holds_need_the_person_and_offer_no_fake_button(self):
        self.lane(hold='login')
        c = self.card()
        self.assertEqual((c['state'], c['tone'], c['headline'], c['action']), ('needs_login', 'attention', 'Needs login', None))
        self.assertIn('Sign in to Instagram', c['detail'])
        self.conn.execute("UPDATE accounts SET hold='challenge'")
        self.conn.commit()
        self.assertEqual((self.card()['state'], self.card()['headline']), ('security_check', 'Needs a security check'))

    def test_an_instagram_warning_is_a_review_with_the_acknowledgement_as_its_one_action(self):
        self.lane()
        self.setting('instagram_scraping_warning', {'kind': 'scraping_warning', 'lane': 'lane-a', 'ig_id': '101', 'at': iso(),
                                                    'message': 'Collection stopped: Instagram showed a scraping warning.', 'review_ready': False})
        c = self.card()
        self.assertEqual((c['state'], c['headline'], c['action']['id'], c['action']['enabled']), ('review', 'Instagram asked for a review', 'acknowledge', False))
        self.assertIn('Close the Instagram warning page', c['detail'])
        warning = db.get_setting(self.conn, 'instagram_scraping_warning')
        self.setting('instagram_scraping_warning', dict(warning, review_ready=True))
        c = self.card()
        self.assertTrue(c['action']['enabled'])
        self.assertEqual(self.call('/api/accounts/live')[1]['workspace']['warning']['lane'], 'lane-a')

    def test_after_a_warning_only_the_chosen_accounts_collect_and_the_others_are_on_hold(self):
        self.lane()
        self.lane('lane-b', '102', 'acct.b')
        self.setting('instagram_collection_isolation', {'accounts': {'lane-a': '101'}, 'collection_stages': ['lists'], 'at': iso()})
        self.assertEqual((self.card('lane-a')['state'], self.card('lane-b')['state']), ('ready', 'held_back'))
        self.assertIsNone(self.card('lane-b')['action'])
        self.assertIn('Held back', self.card('lane-b')['detail'])

    def test_paused_offers_resume(self):
        self.lane(paused=1)
        c = self.card()
        self.assertEqual((c['state'], c['headline'], c['action']['id']), ('paused', 'Paused', 'resume'))

    def test_a_cooldown_counts_down_to_an_absolute_time_and_says_why(self):
        self.lane(list_cool_until=iso(240), profile_cool_until=iso(240))
        c = self.card()
        self.assertEqual((c['state'], c['headline']), ('waiting', 'Waiting {t}'))
        self.assertEqual(c['detail'], 'Instagram asked this account to slow down.')
        self.assertLess(abs((accounts.utc(c['until']) - datetime.now(timezone.utc)).total_seconds() - 240), 5)

    def test_only_follower_lists_resting_is_not_a_full_stop(self):
        self.lane(list_endpoint_until=iso(600))
        c = self.card()
        self.assertEqual(c['state'], 'waiting')
        self.assertIn('Following lists and bios continue', c['detail'])

    def test_a_daily_limit_names_the_reset_time(self):
        self.lane(today=json.dumps({'list': 200, 'profile': 10}), budget=json.dumps({'list': 200, 'profile': 100}), role='lists')
        c = self.card()
        self.assertEqual((c['state'], c['headline']), ('limit', 'Daily limit reached'))
        self.assertRegex(c['detail'], r'Resumes at \d\d:\d\d')
        self.assertEqual(c['allowance']['list'], {'used': 200, 'limit': 200})
        self.assertIsNone(c['allowance']['profile'])          # a lists-only account shows only what it does

    def test_one_dimension_used_up_is_not_a_stop_when_the_account_also_reads_the_other(self):
        self.lane(today=json.dumps({'list': 200, 'profile': 10}), budget=json.dumps({'list': 200, 'profile': 100}))
        self.assertEqual(self.card()['state'], 'ready')

    def test_a_workspace_wide_instagram_pause_is_shared_and_says_so(self):
        self.lane()
        self.setting('cooldown', iso(1800))
        c = self.card()
        self.assertEqual((c['state'], c['headline']), ('waiting', 'Waiting {t}'))
        self.assertIn('every account', c['detail'])
        self.assertIsNotNone(c['until'])

    def test_collection_stopped_reads_as_ready_not_as_a_fault(self):
        self.lane()
        self.setting('paused_lists', True)
        self.setting('paused_bios', True)
        c = self.card()
        self.assertEqual((c['state'], c['tone']), ('ready', 'quiet'))
        self.assertTrue(self.call('/api/accounts/live')[1]['workspace']['stopped'])

    def test_connecting_is_shown_while_chrome_is_being_opened(self):
        self.lane(last_seen='2026-01-01T00:00:00+00:00')
        self.setting('collection_startup', {'id': 'x', 'state': 'opening', 'message': 'Opening', 'accounts': [{'lane_id': 'lane-a', 'ig_id': '101'}],
                                            'requested_at': time.time(), 'deadline_at': time.time() + 90})
        c = self.card()
        self.assertEqual((c['state'], c['headline'], c['action']), ('connecting', 'Connecting', None))

    def test_allowance_has_a_default_and_marks_a_custom_limit(self):
        self.lane(budget=json.dumps({'list': 120}))
        b = self.card()['details']['budgets']
        self.assertEqual((b['list']['limit'], b['list']['custom'], b['profile']['custom']), (120, True, False))

    def test_recent_events_read_as_sentences(self):
        self.lane()
        self.call('/api/seeds/batch', {'text': 'brand', 'directions': ['following']})
        job = self.call('/api/ext/next?lane=lane-a&ig_id=101&handle=acct.a')[1]['job']
        self.page(job, [{'ig_id': '9', 'handle': 'x'}], cursor='c', lane_id='lane-a', account={'ig_id': '101', 'handle': 'acct.a'})
        self.conn.execute("INSERT INTO collector_events(at,lane,kind,outcome,reason) VALUES(?,?,?,?,?)", (iso(1), 'lane-a', 'list', 'rate_limit', 'http_429'))
        self.conn.commit()
        texts = [e['text'] for e in self.card()['details']['events']]
        self.assertEqual(texts[0], 'Instagram asked to slow down')
        self.assertIn("Saved 1 person from @brand's following list", texts)
        self.assertNotRegex(' '.join(texts), r'http|429|lane|lease|permit')


class TheLiveEndpoint(CardBase):
    def get_raw(self, path, etag=None):
        port = server.CFG['port']
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
        req.add_header('Host', f'127.0.0.1:{port}')
        if etag:
            req.add_header('If-None-Match', etag)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get('ETag'), json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get('ETag'), None

    def test_nothing_changed_means_a_304_and_a_change_means_a_new_revision(self):
        self.lane(last_seen=iso(-10))
        status, etag, body = self.get_raw('/api/accounts/live')
        self.assertEqual(status, 200)
        self.assertEqual(etag, '"' + body['revision'] + '"')
        self.assertEqual(self.get_raw('/api/accounts/live', etag)[0], 304)
        self.call('/api/accounts/lane-a', {'paused': True})
        status, newer, _ = self.get_raw('/api/accounts/live', etag)
        self.assertEqual((status, newer != etag), (200, True))

    def test_setup_details_come_only_when_asked_for(self):
        self.assertNotIn('setup', self.call('/api/accounts/live')[1])
        setup = self.call('/api/accounts/live?setup=1')[1]['setup']
        self.assertTrue(setup['extension_path'].endswith('/extension'))
        self.assertEqual(set(setup), {'extension_path', 'extension_id', 'extension_version', 'server'})

    def test_collectors_come_first_then_the_personal_account_then_disconnected_ones(self):
        self.lane('lane-m', '1', 'me.main', is_main=1, first_seen='2026-01-01T00:00:00+00:00')
        self.lane('lane-a', '101', 'acct.a', first_seen='2026-02-01T00:00:00+00:00')
        self.lane('lane-b', '102', 'acct.b', first_seen='2026-03-01T00:00:00+00:00')
        self.call('/api/accounts/lane-a/disconnect', {})
        order = [c['lane_id'] for c in self.call('/api/accounts/live')[1]['cards']]
        self.assertEqual(order, ['lane-b', 'lane-m', 'lane-a'])
        summary = self.call('/api/accounts/live')[1]['summary']
        self.assertEqual((summary['total'], summary['collectors']), (2, 1))     # a disconnected account is not counted

    def test_alerts_and_onboarding_ignore_a_disconnected_account(self):
        self.lane(last_seen='2026-01-01T00:00:00+00:00')
        self.assertTrue(self.call('/api/scraper/status')[1]['alerts'])
        self.call('/api/accounts/lane-a/disconnect', {})
        self.assertEqual(self.call('/api/scraper/status')[1]['alerts'], [])
        steps = {s['id']: s for s in self.call('/api/onboarding')[1]['steps']}
        self.assertEqual(steps['extension']['status'], 'todo')


class OpeningAChromeProfile(CardBase):
    def service(self, host=True):
        config = SimpleNamespace(root=ROOT, host_operations_allowed=host, __getitem__=lambda s, k: 8777)
        class Config(SimpleNamespace):
            def __getitem__(self, key):
                return 8777
        return AccountCardService(Config(root=ROOT, host_operations_allowed=host))

    def test_connect_asks_for_exactly_this_account_and_nothing_else(self):
        self.lane(last_seen='2026-01-01T00:00:00+00:00')
        with mock.patch.object(browser_startup, 'begin', return_value=True) as begin:
            self.service().api_connect(self.conn, {}, {}, 'lane-a')
        body = begin.call_args.args[2]
        self.assertEqual(body, {'action': 'connect_accounts', 'accounts': [{'lane_id': 'lane-a', 'ig_id': '101'}]})

    def test_connect_refuses_in_a_saved_data_workspace_and_for_unknown_or_signed_out_accounts(self):
        self.lane(last_seen='2026-01-01T00:00:00+00:00')
        with self.assertRaisesRegex(Bad, 'saved-data'):
            self.service(host=False).api_connect(self.conn, {}, {}, 'lane-a')
        with self.assertRaises(Bad):
            self.service().api_connect(self.conn, {}, {}, 'nobody')
        self.conn.execute("UPDATE accounts SET ig_id=NULL")
        self.conn.commit()
        with self.assertRaisesRegex(Bad, 'sign in'):
            self.service().api_connect(self.conn, {}, {}, 'lane-a')

    def test_connecting_one_profile_can_never_reach_a_profile_the_warning_selection_excludes(self):
        self.lane()
        self.setting('instagram_collection_isolation', {'accounts': {'lane-b': '102'}, 'collection_stages': ['lists'], 'at': iso()})
        profiles = [{'directory': 'Profile 1', 'lane_id': 'lane-a', 'ig_id': '101'}, {'directory': 'Profile 2', 'lane_id': 'lane-b', 'ig_id': '102'}]
        with mock.patch.object(browser_startup, 'read_config', return_value=profiles):
            with self.assertRaisesRegex(ValueError, 'previously selected'):
                browser_startup._selected(ROOT, self.conn, {'action': 'connect_accounts', 'accounts': [{'lane_id': 'lane-a', 'ig_id': '101'}]})


if __name__ == '__main__':
    unittest.main()
