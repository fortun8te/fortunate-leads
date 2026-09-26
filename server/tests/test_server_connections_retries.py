"""Exercise observation dates through the extension's real HTTP ingestion path.

Fixtures are temporary and fictional. Dates identify server receipts, not dates on
which Instagram relationships began. Only the clock is replaced; page processing,
job transitions, storage, and the connection query all use production code.
"""

import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from test_server import Base, db


T0 = '2026-01-01T10:00:00.000000+00:00'
T1 = '2026-01-02T10:00:00.000000+00:00'
T2 = '2026-01-03T10:00:00.000000+00:00'
T3 = '2026-01-04T10:00:00.000000+00:00'
T4 = '2026-01-05T10:00:00.000000+00:00'


class ConnectionPageRetryTests(Base):
    def queue(self, seed='fictional_source', direction='following'):
        status, body = self.call('/api/scraper/seeds', {
            'handles': [seed], 'directions': [direction],
        })
        self.assertEqual((status, body['queued']), (200, 1))
        status, body = self.call('/api/ext/next?kinds=list')
        self.assertEqual(status, 200)
        job = body['job']
        self.assertIsNotNone(job)
        self.assertEqual((job['seed'], job['direction']), (seed, direction))
        return job

    @staticmethod
    def user(ig_id, handle=None):
        return {'ig_id': str(ig_id), 'handle': handle or f'fictional_member_{ig_id}'}

    @staticmethod
    def payload(job, users, cursor=None, done=False, source_id='900', total=3):
        return {'job_id': job['id'], 'seed': job['seed'], 'direction': job['direction'],
                'ig_id': source_id, 'users': users, 'next_cursor': cursor,
                'done': done, 'total': total}

    def deliver(self, body, when):
        with patch.object(db, 'now', return_value=when):
            status, result = self.call('/api/ext/list-page', body)
        self.assertEqual(status, 200, result)
        return result

    def observations(self):
        return [tuple(row) for row in self.conn.execute(
            'SELECT seed,direction,seed_ig_id,person_ig_id,first_seen,last_seen '
            'FROM edge_observations ORDER BY seed,direction,person_ig_id')]

    def dates(self):
        return {row['person_ig_id']: (row['first_seen'], row['last_seen'])
                for row in self.conn.execute('SELECT * FROM edge_observations')}

    def listing(self, seed='fictional_source', direction='following'):
        return dict(self.conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?',
                                      (seed, direction)).fetchone())

    def graph(self, source='900', target='101'):
        status, result = self.call('/api/connections?' + urlencode({
            'from': f'ig:{source}', 'to': f'ig:{target}', 'mode': 'following',
        }))
        self.assertEqual(status, 200, result)
        return result

    def assert_one_relationship(self, source='900', target='101', evidence_count=1):
        result = self.graph(source, target)
        self.assertEqual(len(result['edges']), 1)
        self.assertEqual(len(result['paths']), 1)
        edge = result['edges'][0]
        self.assertEqual((edge['from'], edge['to']), (f'ig:{source}', f'ig:{target}'))
        self.assertEqual(len(edge['evidence']), evidence_count)
        return edge

    def test_repeated_members_refresh_only_on_unseen_pages_and_replays_cannot_rewind(self):
        job = self.queue()
        alice, bob, carol = map(self.user, ('101', '102', '103'))
        first = self.payload(job, [alice, alice, bob], cursor='page-2')
        second = self.payload(job, [carol, alice, carol], cursor='page-3')
        self.assertEqual(self.deliver(first, T0), {'ok': True, 'received': 2})
        self.assertEqual(self.deliver(second, T1), {'ok': True, 'received': 3})
        expected = {'101': (T0, T1), '102': (T0, T0), '103': (T1, T1)}
        self.assertEqual(self.dates(), expected)
        saved_list = self.listing()

        # Replay newer then older then newer. Receipt order cannot change dates,
        # cursor, coverage, or unique relationship counts for acknowledged pages.
        for body, when in ((second, T2), (first, T3), (second, T4)):
            self.assertEqual(self.deliver(body, when),
                             {'ok': True, 'received': 3, 'duplicate': True})
            self.assertEqual(self.dates(), expected)
            self.assertEqual(self.listing(), saved_list)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pages').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 3)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 3)
        evidence = self.assert_one_relationship()['evidence'][0]
        self.assertEqual((evidence['first_seen'], evidence['last_seen']), (T0, T1))

    def test_terminal_replay_and_late_previous_page_preserve_completed_observations(self):
        job = self.queue()
        first = self.payload(job, [self.user('101')], cursor='page-2')
        final = self.payload(job, [self.user('101'), self.user('102')], done=True)
        self.deliver(first, T0)
        self.deliver(final, T1)
        expected_observations, expected_list = self.observations(), self.listing()
        self.assertEqual(expected_list['state'], 'done')
        for body in (final, first, dict(final, next_cursor='')):
            self.assertTrue(self.deliver(body, T4)['duplicate'])
            self.assertEqual(self.observations(), expected_observations)
            self.assertEqual(self.listing(), expected_list)
        self.assertEqual(self.dates(), {'101': (T0, T1), '102': (T1, T1)})
        self.assert_one_relationship()

    def test_empty_terminal_page_does_not_freshen_members_on_previous_pages(self):
        job = self.queue()
        self.deliver(self.payload(job, [self.user('101')], cursor='page-2'), T0)
        final = self.payload(job, [], done=True, total=1)
        self.assertEqual(self.deliver(final, T2), {'ok': True, 'received': 1})
        self.assertEqual(self.listing()['updated_at'], T2)
        self.assertEqual(self.dates(), {'101': (T0, T0)})
        self.assertTrue(self.deliver(final, T4)['duplicate'])
        evidence = self.assert_one_relationship()['evidence'][0]
        self.assertEqual((evidence['first_seen'], evidence['last_seen']), (T0, T0))

    def test_requeued_failed_job_preserves_cursor_and_distinguishes_new_receipts(self):
        job = self.queue()
        original = self.payload(job, [self.user('101')], cursor='page-2')
        self.deliver(original, T0)
        # Five real lease/failure cycles park the old job. Requeue through the
        # public endpoint creates a new job while preserving its list cursor.
        for _ in range(5):
            leased = self.call('/api/ext/next?kinds=list')[1]['job']
            self.assertEqual(leased['id'], job['id'])
            status, _ = self.call('/api/ext/error', {
                'job_id': job['id'], 'code': 'other', 'message': 'fictional transient failure',
            })
            self.assertEqual(status, 200)
        self.assertEqual(self.listing()['state'], 'error')
        with patch.object(db, 'now', return_value=T1):
            resumed = self.queue()
        self.assertNotEqual(resumed['id'], job['id'])
        self.assertEqual((resumed['cursor'], resumed['received']), ('page-2', 1))
        self.assertEqual(self.dates(), {'101': (T0, T0)})
        checkpoint = self.listing()
        self.assertTrue(self.deliver(original, T2)['duplicate'])
        self.assertEqual(self.listing(), checkpoint)
        self.assertEqual(self.dates(), {'101': (T0, T0)})

        fresh = self.payload(resumed, [self.user('101'), self.user('102')], cursor='page-3')
        self.assertEqual(self.deliver(fresh, T3), {'ok': True, 'received': 2})
        self.assertEqual(self.dates(), {'101': (T0, T3), '102': (T3, T3)})
        self.assertTrue(self.deliver(fresh, T4)['duplicate'])
        self.assertTrue(self.deliver(original, T4)['duplicate'])
        self.assertEqual(self.listing()['cursor'], 'page-3')
        self.assertEqual(self.dates(), {'101': (T0, T3), '102': (T3, T3)})
        self.assert_one_relationship()

    def test_reopened_short_list_accepts_same_cursor_under_new_job_without_duplicates(self):
        job = self.queue()
        first = self.payload(job, [self.user('101')], cursor='page-2', total=100)
        self.deliver(first, T0)
        self.deliver(self.payload(job, [], done=True, total=100), T1)
        with patch.object(db, 'now', return_value=T2):
            repaired = db.repair_lists(self.conn)
            self.conn.commit()
        self.assertEqual(repaired['reopened'], 1)
        resumed = dict(self.conn.execute(
            "SELECT * FROM jobs WHERE seed='fictional_source' AND direction='following' "
            "AND state='queued' ORDER BY id DESC LIMIT 1").fetchone())
        self.assertNotEqual(resumed['id'], job['id'])
        self.assertIsNone(self.listing()['cursor'])
        self.assertEqual(self.dates(), {'101': (T0, T0)})
        self.assertTrue(self.deliver(first, T2)['duplicate'])
        fresh = self.payload(resumed, [self.user('101')], cursor='page-2', total=100)
        self.assertEqual(self.deliver(fresh, T3), {'ok': True, 'received': 1})
        self.assertEqual(self.dates(), {'101': (T0, T3)})
        self.assertTrue(self.deliver(fresh, T4)['duplicate'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 1)
        self.assert_one_relationship()

    def test_independent_source_lists_corroborate_one_follow_and_keep_separate_dates(self):
        first_job = self.queue('fictional_alpha')
        first = self.payload(first_job, [self.user('101', 'fictional_beta')],
                             done=True, source_id='900', total=1)
        self.deliver(first, T0)
        second_job = self.queue('fictional_beta', 'followers')
        second = self.payload(second_job, [self.user('900', 'fictional_alpha')],
                              cursor='page-2', source_id='101', total=1)
        self.deliver(second, T1)
        third = self.payload(second_job, [self.user('900', 'fictional_alpha')],
                             done=True, source_id='101', total=1)
        self.deliver(third, T2)
        self.assertTrue(self.deliver(first, T4)['duplicate'])
        self.assertTrue(self.deliver(second, T4)['duplicate'])
        edge = self.assert_one_relationship(evidence_count=2)
        self.assertEqual({(item['seed'], item['direction']):
                          (item['first_seen'], item['last_seen']) for item in edge['evidence']}, {
            ('fictional_alpha', 'following'): (T0, T0),
            ('fictional_beta', 'followers'): (T1, T2),
        })
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 2)

    def test_stale_job_delivery_cannot_create_memberships_without_evidence(self):
        job = self.queue()
        self.deliver(self.payload(job, [self.user('101')], done=True, total=1), T0)
        saved_list, saved_observations = self.listing(), self.observations()
        # Another lane can deliver a result after the job has ended. If the
        # server classifies it as duplicate, it must leave memberships alone.
        late = self.payload(job, [self.user('102')], cursor='unseen-late-page', total=2)
        result = self.deliver(late, T1)
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['received'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 1)
        self.assertEqual(self.observations(), saved_observations)
        self.assertEqual(self.listing(), saved_list)
        self.assert_one_relationship()

    def test_explicit_unknown_job_cannot_bypass_retry_protection(self):
        job = self.queue()
        first = self.payload(job, [self.user('101')], cursor='page-2')
        self.deliver(first, T0)
        saved_list, saved_observations = self.listing(), self.observations()
        # This is an explicitly job-scoped result, not a jobless manual import.
        # Treating a missing job as a fresh import would renew an old receipt.
        unknown_job_payload = dict(first, job_id=job['id'] + 10000)
        with patch.object(db, 'now', return_value=T4):
            status, result = self.call('/api/ext/list-page', unknown_job_payload)
        self.assertIn(status, (400, 404), result)
        self.assertEqual(self.observations(), saved_observations)
        self.assertEqual(self.listing(), saved_list)


if __name__ == '__main__':
    unittest.main()
