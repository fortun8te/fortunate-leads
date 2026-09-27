"""Offline integration of real list leases, callbacks, repairs and edge evidence."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import server


class ListProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'lists.sqlite')
        self.q = {'lane': ['test-lane'], 'kinds': ['list'], 'version': ['3.8.0']}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def start(self, direction='following', refresh=False):
        db.queue_list(self.conn, 'seed', direction, refresh=refresh)
        self.conn.commit()
        return self.next()

    def next(self):
        return server.ext_next(self.conn, self.q, {})['job']

    def page(self, job, handles=(), **extra):
        body = dict(job_id=job['id'], lease_token=job['lease_token'], seed=job['seed'],
                    direction=job['direction'], requested_cursor=job['cursor'],
                    users=[{'handle': h} for h in handles], done=True,
                    total=len(handles), total_source='current_run')
        body.update(extra)
        return server.ext_list_page(self.conn, self.q, body)

    def row(self, direction='following'):
        return self.conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?', ('seed', direction)).fetchone()

    def active(self, direction='following'):
        return {r[0] for r in self.conn.execute('SELECT p.handle FROM current_edges e JOIN people p ON p.id=e.person_id WHERE seed=? AND direction=?', ('seed', direction))}

    def test_both_directions_only_complete_after_all_pages(self):
        for direction in ('following', 'followers'):
            job = self.start(direction)
            self.page(job, ['alice'], done=False, next_cursor='second', total=2)
            self.assertEqual(self.row(direction)['state'], 'running')
            self.page(self.next(), ['bob'], total=None, total_source='cached')
            row = self.row(direction)
            self.assertEqual((row['state'], row['received'], row['total']), ('done', 2, 2))
            self.assertTrue(db.list_run_complete(self.conn, job['id']))

    def test_cached_total_cannot_certify_or_remove_old_edges(self):
        self.page(self.start(), ['old'])
        self.page(self.start(refresh=True), ['new'], total=1, total_source='cached')
        self.assertEqual(self.row()['state'], 'partial')
        self.assertEqual(self.active(), {'old', 'new'})

    def test_unknown_count_and_profile_fallback_cannot_certify(self):
        db.upsert_person(self.conn, {'handle': 'seed', 'following': 1})
        self.conn.commit()
        self.page(self.start(), ['alice'], total=None, total_source='unknown')
        self.assertEqual((self.row()['state'], self.row()['total']), ('partial', 1))

    def test_cached_later_count_does_not_override_current_run(self):
        job = self.start()
        self.page(job, ['alice'], done=False, next_cursor='b', total=2)
        self.page(self.next(), ['bob'], total=999, total_source='cached')
        self.assertEqual((self.row()['state'], self.row()['total']), ('done', 2))
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 0)

    def test_short_fresh_total_cannot_be_masked_by_lower_cached_count(self):
        self.page(self.start(), ['alice'], done=False, next_cursor='b', total=3)
        self.page(self.next(), ['bob'], total=2, total_source='cached')
        self.assertEqual((self.row()['state'], self.row()['received']), ('partial', 2))
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE kind='list'").fetchone()[0], 'partial')
        self.assertEqual(db.repair_lists(self.conn, dry=True)['requeued'], 0)

    def test_instagram_cap_finishes_attempt_without_claiming_full_list(self):
        job = self.start('followers')
        self.page(job, ['alice', 'bob'], limited=True, total=100)
        self.assertEqual((self.row('followers')['state'], self.row('followers')['received'],
                          self.row('followers')['total']), ('partial', 2, 100))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'partial')
        self.assertIsNone(self.next())
        self.assertEqual(db.repair_lists(self.conn, dry=True)['requeued'], 0)

    def test_partial_repair_waits_and_retries_in_small_bounded_batches(self):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        for i in range(4):
            seed = f'partial{i}'
            db.queue_list(self.conn, seed, 'followers')
            job = self.conn.execute('SELECT id FROM jobs WHERE seed=?', (seed,)).fetchone()[0]
            db.upsert_person(self.conn, {'handle': f'member{i}'})
            self.conn.execute("UPDATE jobs SET state='partial' WHERE id=?", (job,))
            self.conn.execute("UPDATE lists SET state='partial',received=1,total=100,run_job_id=?,"
                              "error='Instagram ended the list early; coverage is partial.',updated_at=?,lane='old-lane' "
                              "WHERE seed=? AND direction='followers'", (job, old, seed))
        self.conn.commit()
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 2)
        db.repair_lists(self.conn)
        self.conn.commit()
        retried = self.conn.execute("SELECT count(*) FROM lists WHERE seed LIKE 'partial%' AND direction='followers' "
                                         "AND state='queued'").fetchone()[0]
        self.assertEqual(retried, 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE kind='list' AND seed LIKE 'partial%' "
                                           "AND direction='followers' AND state='queued'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lists WHERE seed LIKE 'partial%' AND direction='followers' "
                                           "AND state='queued' AND lane IS NULL").fetchone()[0], 2)
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 2)

    def test_partial_retry_budget_and_cooldown_preserve_positive_evidence(self):
        first = self.start('followers')
        self.page(first, ['alice'], total=100)
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 0)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        for attempt in range(db.REOPEN_MAX):
            self.conn.execute("UPDATE lists SET updated_at=? WHERE seed='seed' AND direction='followers'", (old,))
            self.conn.commit()
            self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 1)
            db.repair_lists(self.conn)
            self.conn.commit()
            self.assertEqual(self.active('followers'), {'alice'})
            self.assertEqual(self.row('followers')['received'], 0)
            job = self.next()
            if job['direction'] != 'followers':
                self.page(job, [], total=0)
                job = self.next()
            self.page(job, ['alice'], total=100)
        self.conn.execute("UPDATE lists SET updated_at=? WHERE seed='seed' AND direction='followers'", (old,))
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 0)
        self.assertEqual(self.active('followers'), {'alice'})

    def test_explicit_instagram_cap_remains_parked_after_cooldown(self):
        first = self.start('followers')
        self.page(first, ['alice'], limited=True, total=1000000)
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.conn.execute("UPDATE lists SET updated_at=? WHERE seed='seed' AND direction='followers'", (old,))
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 0)
        self.assertEqual(self.row('followers')['state'], 'partial')
        self.assertEqual(self.active('followers'), {'alice'})

    def test_repair_resumes_terminal_partial_with_saved_cursor(self):
        first = self.start('followers')
        self.page(first, ['alice'], done=False, next_cursor='next', total=2)
        job = self.next()
        server.ext_error(self.conn, self.q, dict(job_id=job['id'], lease_token=job['lease_token'],
                                                 code='other', message='Temporary failure'))
        self.conn.execute("UPDATE jobs SET state='partial' WHERE id=?", (job['id'],))
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.conn.execute("UPDATE lists SET state='partial',updated_at=? WHERE seed='seed' AND direction='followers'", (old,))
        self.conn.commit()
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 1)
        db.repair_lists(self.conn)
        self.conn.commit()
        resumed = self.next()
        if resumed['direction'] != 'followers':
            self.page(resumed, [], total=0)
            resumed = self.next()
        self.assertEqual((resumed['cursor'], resumed['received']), ('next', 1))
        self.page(resumed, ['bob'], total=None, total_source='cached')
        self.assertEqual(self.row('followers')['state'], 'done')
        self.assertEqual(self.active('followers'), {'alice', 'bob'})

    def test_cursor_cycle_saves_last_page_and_stops(self):
        self.page(self.start(), ['alice'], done=False, next_cursor='a', total=4)
        self.page(self.next(), ['bob'], done=False, next_cursor='b', total=4)
        result = self.page(self.next(), ['carol'], done=False, next_cursor='a', total=4)
        self.assertTrue(result['stalled'])
        self.assertEqual((self.row()['state'], self.row()['cursor'], self.row()['received']), ('partial', 'b', 3))
        self.assertEqual(self.active(), {'alice', 'bob', 'carol'})
        self.assertIsNone(self.next())

    def test_stalled_cursor_keeps_each_page_observation(self):
        self.page(self.start(), ['alice'], done=False, next_cursor='a', total=2)
        self.page(self.next(), ['alice', 'bob'], done=False, next_cursor='a', total=2)
        observations = self.conn.execute(
            "SELECT count(*) FROM edge_observations o JOIN people p ON p.id=o.person_id "
            "WHERE p.handle='alice' AND o.seed='seed' AND o.direction='following'").fetchone()[0]
        self.assertEqual(observations, 2)
        self.assertEqual((self.row()['state'], self.row()['received']), ('partial', 2))
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_duplicate_callback_never_double_counts(self):
        job = self.start()
        self.page(job, ['alice'], done=False, next_cursor='a', total=2)
        self.assertTrue(self.page(job, ['other'], done=False, next_cursor='a', total=2)['duplicate'])
        self.assertEqual(self.active(), {'alice'})

    def test_rate_error_does_not_erase_first_page_proof(self):
        first = self.start()
        self.page(first, ['alice'], done=False, next_cursor='a', total=2)
        job = self.next()
        server.ext_error(self.conn, self.q, dict(job_id=job['id'], lease_token=job['lease_token'], code='other', message='Temporary failure'))
        self.assertEqual(self.conn.execute('SELECT first_page_seen FROM list_runs WHERE job_id=?', (job['id'],)).fetchone()[0], 1)
        self.conn.execute('UPDATE jobs SET retry_not_before=NULL WHERE id=?', (job['id'],))
        self.conn.commit()  # simulate the target retry delay elapsing
        self.page(self.next(), ['bob'], total=None, total_source='cached')
        self.assertEqual(self.row()['state'], 'done')
        self.assertIsNone(self.row()['error'])

    def test_repair_clones_tracked_prefix_without_counting_history(self):
        job = self.start()
        self.page(job, ['alice'], done=False, next_cursor='a', total=2)
        self.conn.execute("UPDATE jobs SET state='error' WHERE id=?", (job['id'],))
        self.conn.execute("UPDATE lists SET state='error' WHERE direction='following'")
        db.repair_lists(self.conn)
        self.conn.commit()
        resumed = self.next()
        # repair also adds the other direction; choose the resumed job explicitly by priority.
        if resumed['direction'] != 'following':
            self.page(resumed, [], total=0)
            resumed = self.next()
        self.assertNotEqual(resumed['id'], job['id'])
        self.assertEqual((resumed['cursor'], resumed['received']), ('a', 1))
        self.page(resumed, ['bob'], total=None, total_source='cached')
        self.assertEqual((self.row()['state'], self.row()['received']), ('done', 2))

    def test_legacy_prefix_cannot_certify_even_if_error_is_cleared(self):
        job = self.start()
        self.conn.execute("UPDATE lists SET cursor='a',received=20,error=NULL WHERE direction='following'")
        self.conn.commit()
        job['cursor'] = 'a'
        self.page(job, ['alice'], total=1)
        self.assertEqual(self.row()['state'], 'partial')
        self.assertIn('first-page', self.row()['error'])

    def test_missing_request_tracking_cannot_certify(self):
        job = self.start()
        server.ext_list_page(self.conn, self.q, dict(job_id=job['id'], lease_token=job['lease_token'],
            seed='seed', direction='following', users=[{'handle': 'alice'}], done=True, total=1, total_source='current_run'))
        self.assertEqual(self.row()['state'], 'partial')

    def test_missing_end_confirmation_and_remaining_cursor_are_partial(self):
        job = self.start()
        self.page(job, ['alice'], done=False)
        self.assertEqual(self.row()['state'], 'partial')
        job = self.start()
        self.page(job, ['bob'], next_cursor='a')
        self.assertEqual(self.row()['state'], 'partial')

    def test_complete_refresh_retires_absence_but_keeps_history(self):
        self.page(self.start(), ['old'])
        self.page(self.start(refresh=True), ['new'])
        self.assertEqual(self.active(), {'new'})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 2)

    def test_zero_current_count_is_valid_complete_empty_list(self):
        self.page(self.start(), ['old'])
        self.page(self.start(refresh=True), [], total=0)
        self.assertEqual(self.row()['state'], 'done')
        self.assertEqual(self.active(), set())

    def test_known_old_extension_cannot_lease(self):
        db.queue_list(self.conn, 'seed', 'following')
        self.conn.commit()
        result = server.ext_next(self.conn, dict(self.q, version=['3.7.0']), {})
        self.assertIsNone(result['job'])
        self.assertTrue(result['upgrade_required'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'queued')

    def test_terminal_error_after_pages_remains_partial(self):
        self.q['ig_id'] = ['99']
        self.page(self.start(), ['alice'], done=False, next_cursor='a', total=2)
        job = self.next()
        server.ext_error(self.conn, self.q, dict(job_id=job['id'], lease_token=job['lease_token'], code='private',
                                                 reason='profile_private_wall'))
        self.assertEqual((self.row()['state'], self.row()['received'], self.row()['cursor']), ('partial', 1, 'a'))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'partial')
        self.assertEqual(self.active(), {'alice'})
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.conn.execute("UPDATE lists SET updated_at=? WHERE seed='seed' AND direction='following'", (old,))
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 0)

    def test_opening_old_db_corrects_only_current_partial_job(self):
        current = self.start('followers')
        self.page(current, ['alice'], total=100)
        self.conn.execute("UPDATE jobs SET state='done' WHERE id=?", (current['id'],))
        old = self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list','seed','followers','done')").lastrowid
        self.conn.commit()
        self.conn.close()
        self.conn = db.init(Path(self.tmp.name) / 'lists.sqlite')
        states = dict(self.conn.execute('SELECT id,state FROM jobs WHERE id IN (?,?)', (current['id'], old)))
        self.assertEqual(states, {current['id']: 'partial', old: 'done'})
        self.assertEqual((self.row('followers')['state'], self.row('followers')['received']), ('partial', 1))

    def test_repair_reopens_unverified_done_even_with_matching_display_count(self):
        job = self.start()
        self.conn.execute("UPDATE jobs SET state='done' WHERE id=?", (job['id'],))
        self.conn.execute("UPDATE lists SET state='done',received=1,total=1 WHERE direction='following'")
        self.assertEqual(db.repair_lists(self.conn, dry=True)['reopened'], 1)
        db.repair_lists(self.conn)
        self.assertEqual((self.row()['state'], self.row()['received']), ('queued', 0))

    def test_fractional_count_cannot_prove_an_empty_snapshot(self):
        self.page(self.start(), ['old'])
        self.page(self.start(refresh=True), [], total=0.5)
        self.assertEqual(self.row()['state'], 'partial')
        self.assertEqual(self.active(), {'old'})

    def test_unreadable_member_prevents_complete_proof(self):
        self.page(self.start(), ['old'])
        self.page(self.start(refresh=True), [], total=1,
                  users=[{'handle': 'new'}, {'handle': ''}])
        self.assertEqual(self.row()['state'], 'partial')
        self.assertEqual(self.active(), {'old', 'new'})

    def test_malformed_protocol_is_rejected_before_import(self):
        job = self.start()
        for extra in ({'users': {}}, {'next_cursor': 7}, {'done': 'false'}):
            with self.assertRaises(server.Bad):
                self.page(job, ['alice'], **extra)
            self.conn.rollback()
            self.assertEqual(self.active(), set())


if __name__ == '__main__':
    unittest.main()
