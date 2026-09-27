"""Collection results must belong to their lease and prove current coverage."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server
import control
import accounts


class CollectionIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'test.sqlite')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def job(self, seed='seed', lane='lane-a'):
        db.queue_list(self.conn, seed, 'following')
        self.conn.commit()
        return server.ext_next(self.conn, {'lane': [lane]}, {'version': '3.9.15'})['job']

    def page(self, job, handles, **extra):
        body = dict(job_id=job['id'], seed=job['seed'], direction=job['direction'],
                    lease_token=job['lease_token'], requested_cursor=job['cursor'],
                    users=[{'handle': h} for h in handles], done=True, total_source='current_run')
        body.update(extra)
        return server.ext_list_page(self.conn, {'lane': ['lane-a']}, body)

    def test_capped_list_is_partial_and_keeps_edges(self):
        job = self.job()
        self.page(job, ['alice', 'bob'], limited=True, total=100)
        row = self.conn.execute('SELECT * FROM lists').fetchone()
        self.assertEqual((row['state'], row['received']), ('partial', 2))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 2)

    def test_benchmark_counts_new_links_once_per_saved_page(self):
        job = self.job()
        self.page(job, ['alice', 'bob'], done=False, next_cursor='next', total=3,
                  requested_count=25, http_status=200)
        job = server.ext_next(self.conn, {'lane': ['lane-a']}, {'version': '3.9.15'})['job']
        self.page(job, ['alice', 'carol'], total=3, requested_count=25, http_status=200)
        rows = self.conn.execute('SELECT returned_count,new_links,requested_count,http_status '
                                 'FROM collector_events ORDER BY id').fetchall()
        self.assertEqual([tuple(row) for row in rows], [(2, 2, 25, 200), (2, 1, 25, 200)])

    def test_error_event_is_durable_and_replayed_outbox_is_idempotent(self):
        job = self.job()
        body = {'job_id': job['id'], 'lease_token': job['lease_token'], 'kind': 'list',
                'direction': 'following', 'event_id': 'one-response', 'code': 'rate_limit',
                'reason': 'http_429', 'http_status': 429}
        server.ext_error(self.conn, {'lane': ['lane-a']}, body)
        self.assertTrue(server.ext_error(self.conn, {'lane': ['lane-a']}, body)['duplicate'])
        row = self.conn.execute('SELECT kind,direction,outcome,reason,http_status FROM collector_events').fetchone()
        self.assertEqual(tuple(row), ('list', 'following', 'rate_limit', 'http_429', 429))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collector_events').fetchone()[0], 1)

    def test_experiment_page_size_survives_cursor_and_account_handoff(self):
        db.queue_list(self.conn, 'experiment', 'followers', page_size=50)
        job_id = self.conn.execute("SELECT id FROM jobs WHERE seed='experiment'").fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'new seed'):
            db.queue_list(self.conn, 'experiment', 'followers', page_size=25)
        self.conn.commit()
        old = server.ext_next(self.conn, {'lane': ['lane-old'], 'version': ['3.9.11']}, {})
        self.assertIsNone(old['job'])
        db.queue_list(self.conn, 'ordinary', 'followers')
        self.conn.commit()
        old = server.ext_next(self.conn, {'lane': ['lane-old'], 'version': ['3.9.11']}, {})
        self.assertIsNone(old['job'])
        self.assertTrue(old['upgrade_required'])
        job = server.ext_next(self.conn, {'lane': ['lane-a'], 'version': ['3.9.15']}, {})['job']
        self.assertEqual((job['id'], job['page_size']), (job_id, 50))
        body = dict(job_id=job_id, seed='experiment', direction='followers', lease_token=job['lease_token'],
                    requested_cursor=None, next_cursor='second', done=False, total=2, total_source='current_run',
                    users=[{'handle': 'alice'}], requested_count=25)
        with self.assertRaisesRegex(server.Bad, 'page size changed'):
            server.ext_list_page(self.conn, {'lane': ['lane-a']}, body)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pages').fetchone()[0], 0)
        body['requested_count'] = 50
        server.ext_list_page(self.conn, {'lane': ['lane-a']}, body)
        self.conn.execute("UPDATE lists SET lane=NULL WHERE seed='experiment'")
        self.conn.commit()
        handed = server.ext_next(self.conn, {'lane': ['lane-b'], 'version': ['3.9.15']}, {})['job']
        self.assertEqual((handed['id'], handed['page_size'], handed['cursor']), (job_id, 50, 'second'))

    def test_page_promising_more_cannot_remove_existing_follower(self):
        db.queue_list(self.conn, 'seed', 'followers')
        self.conn.commit()
        first = server.ext_next(self.conn, {'lane': ['lane-a']}, {'version': '3.9.15'})['job']
        server.ext_list_page(self.conn, {'lane': ['lane-a']}, dict(
            job_id=first['id'], seed='seed', direction='followers', lease_token=first['lease_token'],
            requested_cursor=None, users=[{'handle': 'alice'}], done=True, has_more=False,
            total=1, total_source='current_run'))
        self.assertTrue(db.queue_list(self.conn, 'seed', 'followers', refresh=True))
        self.conn.commit()
        second = server.ext_next(self.conn, {'lane': ['lane-a']}, {'version': '3.9.15'})['job']
        with self.assertRaisesRegex(server.Bad, 'promises more'):
            server.ext_list_page(self.conn, {'lane': ['lane-a']}, dict(
                job_id=second['id'], seed='seed', direction='followers', lease_token=second['lease_token'],
                requested_cursor=None, users=[], done=True, has_more=True,
                total=0, total_source='current_run'))
        self.assertEqual(self.conn.execute("SELECT active FROM edge_evidence WHERE seed='seed'").fetchone()[0], 1)
        self.assertNotEqual(self.conn.execute("SELECT state FROM lists WHERE seed='seed'").fetchone()[0], 'done')

    def test_new_run_counts_observed_members_not_historical_edges(self):
        first = self.job()
        self.page(first, ['alice', 'bob'], total=3)
        second = self.job()
        self.assertNotEqual(first['id'], second['id'])
        self.page(second, ['carol'], total=1)
        row = self.conn.execute('SELECT * FROM lists').fetchone()
        self.assertEqual((row['state'], row['received']), ('done', 1))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 3)

    def test_explicit_refresh_starts_completed_list_once_and_preserves_history(self):
        first = self.job()
        self.page(first, ['alice'], total=1)
        self.assertFalse(db.queue_list(self.conn, 'seed', 'following'))
        self.assertTrue(db.queue_list(self.conn, 'seed', 'following', refresh=True))
        self.conn.commit()
        second = server.ext_next(self.conn, {'lane': ['lane-a']}, {'version': '3.9.15'})['job']
        self.assertNotEqual(first['id'], second['id'])
        self.page(second, ['bob'], done=False, next_cursor='c2', total=3)
        self.assertFalse(db.queue_list(self.conn, 'seed', 'following', refresh=True))
        row = self.conn.execute('SELECT cursor,received FROM lists').fetchone()
        self.assertEqual(tuple(row), ('c2', 1))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM list_members WHERE job_id=?', (first['id'],)).fetchone()[0], 1)

    def test_stale_token_cannot_change_current_list(self):
        job = self.job()
        self.conn.execute("UPDATE jobs SET lease_token='new-token', lane='lane-b' WHERE id=?", (job['id'],))
        self.conn.commit()
        self.assertTrue(self.page(job, ['wrong'])['stale'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT lease_token FROM jobs').fetchone()[0], 'new-token')

    def test_missing_bio_does_not_erase_existing_observation(self):
        pid = db.upsert_person(self.conn, {'handle': 'alice', 'bio': 'Actual bio', 'bio_at': '2026-01-01'})
        self.conn.commit()
        server.ext_profile(self.conn, {}, {'profile': {'handle': 'alice', 'name': 'Alice'}})
        row = self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        self.assertEqual((row['bio'], row['bio_at']), ('Actual bio', '2026-01-01'))
        server.ext_profile(self.conn, {}, {'profile': {'handle': 'alice', 'bio': ''}})
        self.assertEqual(self.conn.execute('SELECT bio FROM people WHERE id=?', (pid,)).fetchone()[0], '')

    def test_ai_pause_stops_background_rules_and_laya(self):
        control.stop_all(self.conn)
        self.conn.commit()
        with patch.object(server, 'qualify_batch') as rules, patch.object(server.laya, 'available') as laya:
            self.assertFalse(server.background_qualify(self.conn))
            self.assertFalse(server.laya_step(self.conn))
            rules.assert_not_called()
            laya.assert_not_called()

    def test_stalled_page_keeps_new_members_and_marks_partial(self):
        job = self.job()
        self.page(job, ['alice'], done=False, next_cursor='same', total=10)
        job = server.ext_next(self.conn, {'lane': ['lane-a']}, {'version': '3.9.15'})['job']
        result = self.page(job, ['bob'], done=False, next_cursor='same', total=10)
        self.assertTrue(result['stalled'])
        row = self.conn.execute('SELECT state,received,error FROM lists').fetchone()
        self.assertEqual(tuple(row[:2]), ('partial', 2))
        self.assertIn('cursor', row['error'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'error')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 2)

    def test_missing_token_cannot_import_a_page(self):
        job = self.job()
        self.assertTrue(self.page(job, ['wrong'], lease_token=None)['stale'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_invalid_profile_handle_never_creates_blank_identity(self):
        with self.assertRaises(server.Bad):
            server.ext_profile(self.conn, {}, {'profile': {'handle': 'https://evil.test/?next=instagram.com/alice', 'bio': 'wrong'}})
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_stale_login_error_cannot_release_new_owner(self):
        job = self.job()
        self.conn.execute("UPDATE jobs SET lease_token='new-token',lane='lane-b' WHERE id=?", (job['id'],))
        self.conn.commit()
        result = server.ext_error(self.conn, {'lane': ['lane-a']},
                                  dict(job_id=job['id'],lease_token=job['lease_token'],code='login_required'))
        self.assertTrue(result['stale'])
        self.assertEqual(tuple(self.conn.execute('SELECT state,lane,lease_token FROM jobs').fetchone()),
                         ('leased', 'lane-b', 'new-token'))

    def test_new_list_account_keeps_main_accounts_in_flight_page(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('me',1)")
        db.queue_list(self.conn, 'seed', 'following')
        self.conn.commit()
        main = {'lane': ['lane-a'], 'handle': ['me'], 'ig_id': ['101']}
        other = {'lane': ['lane-b'], 'handle': ['other'], 'ig_id': ['102']}
        first = server.ext_next(self.conn, main, {'version': '3.9.15'})['job']
        self.assertEqual(first['seed'], 'seed')

        # A second account changes who gets future list work. The page already
        # in flight must still be accepted under the original lease token.
        self.assertIsNone(server.ext_next(self.conn, other, {'version': '3.9.15'})['job'])
        lease = self.conn.execute('SELECT state,lane,lease_token FROM jobs WHERE id=?', (first['id'],)).fetchone()
        self.assertEqual(tuple(lease), ('leased', 'lane-a', first['lease_token']))
        body = dict(job_id=first['id'], lease_token=first['lease_token'], seed='seed', direction='following',
                    requested_cursor=None, next_cursor='next', done=False, total=2, total_source='current_run',
                    users=[{'handle': 'alice'}, {'handle': 'bob'}])
        self.assertEqual(server.ext_list_page(self.conn, main, body)['received'], 2)
        resumed = server.ext_next(self.conn, other, {'version': '3.9.15'})['job']
        self.assertEqual((resumed['id'], resumed['cursor'], resumed['received']), (first['id'], 'next', 2))

    def test_role_change_keeps_in_flight_list_but_cooldown_hands_it_off(self):
        db.queue_list(self.conn, 'seed', 'following')
        self.conn.commit()
        first = server.ext_next(self.conn, {'lane': ['lane-a']}, {'version': '3.9.15'})['job']
        server.ext_next(self.conn, {'lane': ['lane-b']}, {'version': '3.9.15'})

        self.conn.execute("UPDATE accounts SET role='bios' WHERE lane_id='lane-a'")
        accounts.release(self.conn, datetime.now(timezone.utc))
        self.assertEqual(self.conn.execute('SELECT lease_token FROM jobs WHERE id=?', (first['id'],)).fetchone()[0],
                         first['lease_token'])

        until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        self.conn.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?', (until, 'lane-a'))
        accounts.release(self.conn, datetime.now(timezone.utc))
        self.conn.commit()
        handed = server.ext_next(self.conn, {'lane': ['lane-b']}, {'version': '3.9.15'})['job']
        self.assertEqual(handed['id'], first['id'])
        self.assertNotEqual(handed['lease_token'], first['lease_token'])

    def test_valid_profile_token_cannot_import_another_identity(self):
        db.upsert_person(self.conn, {'handle':'alice','ig_id':'123'})
        self.conn.execute("INSERT INTO jobs(kind,handle,state,lane,lease_token) VALUES('profile','alice','leased','lane-a','token')")
        job_id = self.conn.execute('SELECT id FROM jobs').fetchone()[0]
        self.conn.commit()
        with self.assertRaises(server.Bad):
            server.ext_profile(self.conn, {'lane':['lane-a']},
                               {'job_id':job_id,'lease_token':'token','profile':{'handle':'bob','ig_id':'456','bio':'wrong'}})
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
