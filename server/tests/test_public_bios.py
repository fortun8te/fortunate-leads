import sys
import tempfile
import unittest
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import public_bios as p


class PublicBiosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'test.sqlite')
        if 'lease_token' not in {r[1] for r in self.conn.execute('PRAGMA table_info(jobs)')}:
            self.conn.execute('ALTER TABLE jobs ADD COLUMN lease_token TEXT')
        p.init(self.conn)
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True})
        self.pid = db.upsert_person(self.conn, {'handle': 'person', 'ig_id': '123', 'bio': 'known'})
        db.add_edge(self.conn, 'seed', self.pid, 'following')
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','person',?)", (db.now(),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def item(self, **extra):
        return {'id': '123', 'username': 'person', 'biography': 'new',
                '_scrape': {'bioStatus': 'observed', 'source': 'test', 'observedAt': db.now(), 'cacheHit': False}, **extra}

    def test_exclusive_and_late_result_rejected(self):
        job = p.lease(self.conn, 'public-a')
        self.assertIsNone(p.lease(self.conn, 'public-b'))
        p.release(self.conn, 'public-a')
        job2 = p.lease(self.conn, 'public-b')
        self.assertFalse(p.import_item(self.conn, job, self.item()))
        self.assertTrue(p.import_item(self.conn, job2, self.item()))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT bio FROM people').fetchone()[0], 'new')

    def test_missing_bio_keeps_known_and_retries(self):
        job = p.lease(self.conn, 'public-a')
        item = self.item(biography='', fullName='Updated', error='incomplete_profile')
        item['_scrape']['bioStatus'] = 'unavailable'
        self.assertFalse(p.import_item(self.conn, job, item))
        self.assertEqual(tuple(self.conn.execute('SELECT bio,name FROM people').fetchone()), ('known', 'Updated'))
        self.assertIsNone(p.lease(self.conn, 'public-a'))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'queued')

    def test_empty_observed_is_success_cache_not_rate(self):
        job = p.lease(self.conn, 'a')
        item = self.item(biography='')
        item['_scrape'].update(bioStatus='empty', cacheHit=True)
        self.assertTrue(p.import_item(self.conn, job, item))
        status = p.status(self.conn)
        self.assertEqual(status['cached'], 1)
        self.assertEqual(status['freshPerMinute'], 0)
        self.assertIsNone(status['etaSeconds'])
        self.assertEqual(self.conn.execute('SELECT bio FROM people').fetchone()[0], '')

    def test_pause_and_release(self):
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        self.assertIsNone(p.lease(self.conn, 'a'))
        db.set_setting(self.conn, 'paused_bios', False)
        self.conn.commit()
        p.lease(self.conn, 'a')
        p.release(self.conn, 'a')
        self.assertEqual(tuple(self.conn.execute('SELECT state,attempts,lease_token FROM jobs').fetchone()), ('queued', 0, None))

    def test_wrong_identity_not_imported(self):
        job = p.lease(self.conn, 'a')
        self.assertFalse(p.import_item(self.conn, job, self.item(id='456', username='other')))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 1)

    def test_missing_scope_bounded_and_no_duplicate_or_error_requeue(self):
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True, 'scope': 'missing'})
        for i in range(300):
            db.upsert_person(self.conn, {'handle': 'lead' + str(i)})
        no = db.upsert_person(self.conn, {'handle': 'skip_no'})
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'no')", (no,))
        self.conn.execute("INSERT INTO seeds(handle,added_at) VALUES('seedonly',?)", (db.now(),))
        self.conn.execute("INSERT INTO jobs(kind,handle,state) VALUES('profile','lead0','error')")
        self.conn.commit()
        self.assertEqual(p.plan(self.conn), 255)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE handle='seedonly'").fetchone()[0], 1)
        self.assertEqual(p.plan(self.conn), 0)
        self.assertGreater(p.outstanding(self.conn), 256)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE handle='lead0'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE handle='skip_no'").fetchone()[0], 0)
        self.conn.execute("UPDATE jobs SET state='done' WHERE state='queued'")
        self.conn.commit()
        p.plan(self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE handle='seedonly'").fetchone()[0], 1)

    def test_default_scope_does_not_expand(self):
        db.upsert_person(self.conn, {'handle': 'unqueued'})
        self.conn.commit()
        self.assertEqual(p.plan(self.conn), 0)

    def test_cooldown_survives_restart_and_hides_credentials(self):
        calls = []
        eg = SimpleNamespace(spec=SimpleNamespace(kind='proxy', url='socks5://user:secret@local:99'),
                             name='lane', parked_until=time.monotonic() + 300,
                             retired_until=0, strikes=2, rate_strikes=1,
                             limiter=SimpleNamespace(on_block=lambda: calls.append(True)))
        pool = SimpleNamespace(egresses=[eg])
        p.save_cooldowns(self.conn, pool)
        saved = db.get_setting(self.conn, 'public_bios_cooldowns')
        self.assertNotIn('secret', str(saved))
        eg.parked_until = 0
        eg.strikes = 0
        p.restore_cooldowns(self.conn, pool)
        self.assertGreater(eg.parked_until - time.monotonic(), 290)
        self.assertEqual(eg.strikes, 2)
        self.assertEqual(calls, [True])

    def test_rotated_tor_cooldown_restored_to_fresh_spec(self):
        def egress(url, rotation):
            return SimpleNamespace(spec=SimpleNamespace(kind='proxy', url=url, name='tor-slot-1'),
                                   name='tor-slot-1' + rotation, parked_until=time.monotonic() + 600,
                                   retired_until=0, strikes=3, rate_strikes=2,
                                   limiter=SimpleNamespace(on_block=lambda: None))
        rotated = egress('socks5://random-rotated:x@local:9150', '#99')
        p.save_cooldowns(self.conn, SimpleNamespace(egresses=[rotated]))
        fresh = egress('socks5://initial:x@local:9150', '')
        fresh.parked_until = 0
        p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[fresh]))
        self.assertGreater(fresh.parked_until - time.monotonic(), 590)

    def test_cooldowns_survive_reorder_credentials_and_config_removal(self):
        def lane(name, url, wait=0):
            return SimpleNamespace(spec=SimpleNamespace(kind='proxy', name=name, url=url),
                                   parked_until=1000 + wait, retired_until=0, strikes=2,
                                   rate_strikes=1, limiter=SimpleNamespace(mode='adaptive', rpm=5,
                                                                           on_block=lambda: None))
        a = lane('a', 'socks5://old:secret@one:9000', 7200)
        b = lane('b', 'socks5://old:secret@two:9000', 120)
        with patch.object(p.time, 'time', return_value=100000), patch.object(p.time, 'monotonic', return_value=1000):
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[a, b]))
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[b]))
            a2 = lane('a', 'socks5://new:password@one:9000')
            b2 = lane('b', 'socks5://new:password@two:9000')
            a2.parked_until = b2.parked_until = 0
            a2.limiter.rpm = b2.limiter.rpm = 20
            p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[b2, a2]))
            self.assertEqual(a2.parked_until, 8200)
            self.assertEqual(b2.parked_until, 1120)
            self.assertEqual(a2.limiter.rpm, 5)
        self.assertNotIn('secret', str(db.get_setting(self.conn, 'public_bios_cooldowns')))

    def test_rename_uses_unique_endpoint_and_does_not_cross_shared_endpoints(self):
        def lane(name, wait=0):
            return SimpleNamespace(spec=SimpleNamespace(kind='proxy', name=name,
                                   url='socks5://user:secret@same:9000'),
                                   parked_until=1000 + wait, retired_until=0, strikes=0,
                                   rate_strikes=0, limiter=SimpleNamespace(mode='adaptive', rpm=10,
                                                                           on_block=lambda: None))
        with patch.object(p.time, 'time', return_value=100000), patch.object(p.time, 'monotonic', return_value=1000):
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[lane('old', 600)]))
            renamed = lane('new')
            renamed.parked_until = 0
            p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[renamed]))
            self.assertEqual(renamed.parked_until, 1600)
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[lane('first', 300), lane('second', 600)]))
            ambiguous = lane('other')
            ambiguous.parked_until = 0
            p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[ambiguous]))
            self.assertEqual(ambiguous.parked_until, 0)

    def test_expired_wait_keeps_adaptive_rate_and_clock_skew_keeps_deadline(self):
        def lane(wait=0):
            return SimpleNamespace(spec=SimpleNamespace(kind='proxy', name='lane', url='http://u:p@host:99'),
                                   parked_until=1000 + wait, retired_until=0, strikes=2,
                                   rate_strikes=3, limiter=SimpleNamespace(mode='adaptive', rpm=4,
                                                                           on_block=lambda: None))
        with patch.object(p.time, 'time', return_value=100000), patch.object(p.time, 'monotonic', return_value=1000):
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[lane(600)]))
        for wall, expected in ((99900, 500), (200000, 500)):
            fresh = lane()
            fresh.parked_until = 0
            fresh.limiter.rpm = 20
            with patch.object(p.time, 'time', return_value=wall), patch.object(p.time, 'monotonic', return_value=1100):
                p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[fresh]))
            self.assertEqual(fresh.parked_until, 1100 + expected)
            self.assertEqual((fresh.strikes, fresh.rate_strikes, fresh.limiter.rpm), (2, 3, 4))
        with patch.object(p.time, 'time', return_value=100700), patch.object(p.time, 'monotonic', return_value=1700):
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[lane()]))
            fresh = lane()
            fresh.parked_until = 0
            fresh.limiter.rpm = 20
            p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[fresh]))
            self.assertEqual(fresh.parked_until, 0)
            self.assertEqual(fresh.limiter.rpm, 4)

    def test_unknown_legacy_key_keeps_server_deadline(self):
        eg = SimpleNamespace(spec=SimpleNamespace(kind='proxy', name='new', url='http://u:p@host:99'),
                             parked_until=0, retired_until=0, strikes=0, rate_strikes=0,
                             limiter=SimpleNamespace(mode='adaptive', rpm=20, on_block=lambda: None))
        db.set_setting(self.conn, 'public_bios_cooldowns', {'old-worker-hash': {'until': 100500}})
        self.conn.commit()
        with patch.object(p.time, 'time', return_value=100000), patch.object(p.time, 'monotonic', return_value=1000):
            p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[eg]))
            self.assertEqual(eg.parked_until, 1500)
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[eg]))
        self.assertIn('old-worker-hash', db.get_setting(self.conn, 'public_bios_cooldowns'))

    def test_retirement_wait_survives_restart(self):
        eg = SimpleNamespace(spec=SimpleNamespace(kind='proxy', name='retired', url='http://host:99'),
                             parked_until=0, retired_until=1600, strikes=0, rate_strikes=0,
                             limiter=SimpleNamespace(mode='adaptive', rpm=10, on_block=lambda: None))
        with patch.object(p.time, 'time', return_value=100000), patch.object(p.time, 'monotonic', return_value=1000):
            p.save_cooldowns(self.conn, SimpleNamespace(egresses=[eg]))
        fresh = SimpleNamespace(spec=eg.spec, parked_until=0, retired_until=0, strikes=0,
                                rate_strikes=0, limiter=eg.limiter)
        with patch.object(p.time, 'time', return_value=100100), patch.object(p.time, 'monotonic', return_value=1100):
            p.restore_cooldowns(self.conn, SimpleNamespace(egresses=[fresh]))
        self.assertEqual(fresh.parked_until, 1600)

    def test_environment_failures_never_exhaust_handle_attempts(self):
        for code in ['no_proxies_left', 'rate_limited', 'login_required', 'upstream_error', 'network_error', 'rate_limited']:
            job = p.lease(self.conn, 'public-a')
            self.assertIsNotNone(job)
            p.import_item(self.conn, job, {'error': code})
            row = self.conn.execute('SELECT state,attempts FROM jobs').fetchone()
            self.assertEqual(tuple(row), ('queued', 0))
            self.conn.execute('UPDATE public_bio_retries SET next_at=NULL')
            self.conn.commit()
        self.assertEqual(p.status(self.conn)['deferred'], 6)
        self.assertEqual(self.conn.execute('SELECT failures FROM public_bio_retries').fetchone()[0], 0)

    def test_old_cache_does_not_regress_newer_profile(self):
        fresh_at = db.now()
        self.conn.execute("UPDATE people SET bio_at=?,name='Newest',followers=999 WHERE id=?", (fresh_at, self.pid))
        self.conn.commit()
        job = p.lease(self.conn, 'a')
        item = self.item(fullName='Old', followersCount=1)
        item['_scrape'].update(cacheHit=True, observedAt='2020-01-01T00:00:00+00:00')
        self.assertTrue(p.import_item(self.conn, job, item))
        self.assertEqual(tuple(self.conn.execute('SELECT bio,bio_at,name,followers FROM people').fetchone()),
                         ('known', fresh_at, 'Newest', 999))

    def test_terminal_jobs_not_in_eta_remaining(self):
        db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True, 'scope': 'missing'})
        self.conn.execute("UPDATE jobs SET state='error'")
        self.conn.commit()
        status = p.status(self.conn)
        self.assertEqual(status['remaining'], 0)
        self.assertEqual(status['terminalErrors'], 1)

    def test_eta_uses_sustained_fresh_saves_only(self):
        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        def record(seconds_ago, outcome='success', cached=False):
            at = (now - timedelta(seconds=seconds_ago)).isoformat()
            self.conn.execute('''INSERT INTO public_bio_results
              (job_id,handle,at,outcome,cache_hit) VALUES(?,?,?,?,?)''',
              (1, 'sample', at, outcome, int(cached)))
        def status(healthy=2):
            db.set_setting(self.conn, 'public_bios_heartbeat',
                           {'epoch': now.timestamp(), 'healthyEgresses': healthy, 'egresses': 2})
            self.conn.commit()
            with patch.object(p.db, 'utc_now', return_value=now), patch.object(p.time, 'time', return_value=now.timestamp()):
                return p.status(self.conn)

        for age in (285, 230, 180, 120, 20):
            record(age)
        record(2, 'error')
        record(3, 'deferred')
        record(4, cached=True)
        record(301)  # Outside the five minute window.
        first = status()
        self.assertEqual((first['freshPerMinute'], first['freshFiveMinutes'], first['etaRatePerMinute']), (1, 5, 1))
        self.assertEqual(first['etaSeconds'], 60)
        self.assertEqual((first['errors'], first['deferred'], first['cached']), (1, 1, 1))
        self.assertIsNone(status(healthy=0)['etaSeconds'])
        self.conn.execute('DELETE FROM public_bio_results WHERE at>=?', ((now - timedelta(seconds=60)).isoformat(),))
        self.conn.commit()
        self.assertEqual(status()['freshPerMinute'], 0)
        self.assertIsNone(status()['etaSeconds'])

    def test_eta_waits_for_history_and_respects_queue_scope(self):
        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        db.set_setting(self.conn, 'public_bios_heartbeat', {'epoch': now.timestamp(), 'healthyEgresses': 1})
        for seconds_ago in (8, 7, 6, 5, 4, 3, 2, 1):
            self.conn.execute('''INSERT INTO public_bio_results
              (job_id,handle,at,outcome,cache_hit) VALUES(1,'sample',?,'success',0)''',
              ((now - timedelta(seconds=seconds_ago)).isoformat(),))
        db.upsert_person(self.conn, {'handle': 'not_queued'})
        self.conn.commit()
        with patch.object(p.db, 'utc_now', return_value=now), patch.object(p.time, 'time', return_value=now.timestamp()):
            queued = p.status(self.conn)
            self.assertEqual(queued['remaining'], 1)
            self.assertEqual(queued['freshPerMinute'], 8)
            self.assertIsNone(queued['etaSeconds'])
            db.set_setting(self.conn, 'public_bios', {**p.DEFAULTS, 'on': True, 'scope': 'missing'})
            missing = p.status(self.conn)
            self.assertEqual(missing['remaining'], 2)
            self.assertIsNone(missing['etaSeconds'])

    def test_config_validation(self):
        for body in ({'concurrency': 100}, {'concurrency': True}, {'egress_file': '/tmp/secret'}, {'on': 'yes'}):
            with self.assertRaises(ValueError):
                p.configure(self.conn, body)

    def test_prior_attempts_cannot_strand_public_job(self):
        self.conn.execute('UPDATE jobs SET attempts=4')
        self.conn.commit()
        job = p.lease(self.conn, 'a')
        p.import_item(self.conn, job, self.item(error='incomplete_profile'))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'error')
        self.assertEqual(p.outstanding(self.conn), 0)

    def test_exhausted_queued_jobs_become_terminal(self):
        self.conn.execute('UPDATE jobs SET attempts=5')
        self.conn.commit()
        self.assertIsNone(p.lease(self.conn, 'a'))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'error')

if __name__ == '__main__':
    unittest.main()
