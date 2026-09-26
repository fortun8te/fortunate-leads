"""Offline identity-cache and crash recovery checks, using temporary SQLite files."""
import concurrent.futures
import json
import select
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from igscraper.store import Store


def profile(username, user_id, biography='bio'):
    return {'username': username, 'id': user_id, 'biography': biography,
            '_scrape': {'bioStatus': 'observed', 'observedAt': '2026-09-26'}}


class IdentityCacheTests(unittest.TestCase):
    def test_rename_retires_old_username_and_refreshes_both_aliases_ttl(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d) / 'cache.sqlite') as s:
            with patch('igscraper.store.time.time', return_value=100):
                s.cache_put(profile('Alice', '1'))
            with patch('igscraper.store.time.time', return_value=200):
                s.cache_put(profile('Bob', '1', 'new bio'))
                self.assertIsNone(s.cache_get('u:alice', 1000))
                self.assertEqual(s.cache_get('u:bob', 10), s.cache_get('id:1', 10))
            with patch('igscraper.store.time.time', return_value=211):
                self.assertIsNone(s.cache_get('u:bob', 10))
                self.assertIsNone(s.cache_get('id:1', 10))

    def test_reassigned_username_invalidates_previous_id_payload(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d) / 'cache.sqlite') as s:
            s.cache_put(profile('alice', '1'))
            s.cache_put(profile('alice', '2', 'different person'))
            self.assertIsNone(s.cache_get('id:1', 100))
            self.assertEqual(s.cache_get('u:alice', 100)['id'], '2')
            self.assertEqual(s.cache_get('id:2', 100)['username'], 'alice')

    def test_legacy_divergent_alias_is_not_deleted_by_rename(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d) / 'cache.sqlite') as s:
            s.cache_put(profile('alice', '1'))
            # Reproduce a pre-fix database with a newer independent handle owner.
            s.db.execute('UPDATE profiles SET item=? WHERE key=?',
                         (json.dumps(profile('alice', '2')), 'u:alice'))
            s.cache_put(profile('bob', '1'))
            self.assertEqual(s.cache_get('u:alice', 100)['id'], '2')
            self.assertEqual(s.cache_get('u:bob', 100)['id'], '1')

    def test_alias_retirement_rolls_back_when_new_write_fails(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d) / 'cache.sqlite') as s:
            s.cache_put(profile('alice', '1'))
            s.db.execute("CREATE TRIGGER reject_alias BEFORE INSERT ON profiles "
                         "WHEN NEW.key='u:bob' BEGIN SELECT RAISE(ABORT, 'blocked'); END")
            with self.assertRaisesRegex(Exception, 'blocked'):
                s.cache_put(profile('bob', '1'))
            self.assertEqual(s.cache_get('u:alice', 100)['id'], '1')
            self.assertEqual(s.cache_get('id:1', 100)['username'], 'alice')


class DurableJobTests(unittest.TestCase):
    def test_six_simultaneous_first_opens_and_deduped_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'fresh.sqlite'
            barrier = threading.Barrier(6)
            def append(index):
                barrier.wait(10)
                with Store(path) as s:
                    s.job_init('massive', ['@shared', 'https://instagram.com/SHARED', f'person{index}'])
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(append, range(6)))
            with Store(path) as s:
                self.assertEqual(len(s.job_order('massive')), 7)
                self.assertEqual(s.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_export_iterator_keeps_saved_errors_and_unacknowledged_results_in_order(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d) / 'cache.sqlite') as s:
            s.job_init('massive', ['a', 'b', 'c', 'd'])
            s.job_done('massive', 2, {'username': 'c', 'biography': 'saved'}, delivered=False)
            s.job_done('massive', 0, {'username': 'a', 'error': 'not_found'})
            expected = [{'username': 'a', 'error': 'not_found'}, {'username': 'c', 'biography': 'saved'}]
            self.assertEqual(list(s.job_iter_items('massive', batch_size=1)), expected)
            self.assertEqual(list(s.job_iter_items('massive')), expected)
            self.assertEqual(s.job_undelivered_indices('massive'), {2})
            self.assertEqual(list(s.job_iter_items('absent')), [])
            with self.assertRaises(ValueError):
                list(s.job_iter_items('massive', batch_size=0))

    def test_generator_failure_rolls_back_entire_append(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d) / 'cache.sqlite') as s:
            s.job_init('massive', ['existing'])
            def interrupted():
                yield 'new'
                raise RuntimeError('input interrupted')
            with self.assertRaisesRegex(RuntimeError, 'input interrupted'):
                s.job_init('massive', interrupted())
            self.assertEqual(s.job_order('massive'), [(0, 'existing')])
            s.job_init('massive', iter(['@existing', 'new', 'https://instagram.com/NEW']))
            self.assertEqual(s.job_order('massive'), [(0, 'existing'), (1, 'new')])

    def test_process_kill_preserves_saved_unacknowledged_result_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cache.sqlite'
            script = '''import sys, time
from igscraper.store import Store
s = Store(sys.argv[1])
with s.job_guard('massive'):
    s.job_init('massive', ['alice', 'bob'])
    s.job_done('massive', 0, {'username': 'alice', 'biography': 'saved'}, delivered=False)
    print('SAVED', flush=True)
    time.sleep(60)
'''
            child = subprocess.Popen([sys.executable, '-c', script, str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'child did not save')
                self.assertEqual(child.stdout.readline().strip(), 'SAVED')
                with Store(path) as s:
                    with self.assertRaisesRegex(RuntimeError, 'already running'):
                        with s.job_guard('massive'):
                            pass
            finally:
                child.kill()
                child.communicate(timeout=10)
            with Store(path) as s, s.job_guard('massive'):
                self.assertEqual(s.job_pending('massive'), [(1, 'bob')])
                self.assertEqual(list(s.job_undelivered('massive')), [(0, {'username': 'alice', 'biography': 'saved'})])
                s.job_ack('massive', 0)
                self.assertEqual(list(s.job_undelivered('massive')), [])

    def test_2500_result_replay_is_bounded_and_resumes_at_last_ack(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cache.sqlite'
            count, acknowledged = 2500, 731
            with Store(path) as s:
                s.job_init('massive', (f'user{i}' for i in range(count)))
                for i in range(count):
                    s.job_done('massive', i, profile(f'user{i}', str(i), 'x' * 16384), delivered=False)
                seen = []
                tracemalloc.start()
                try:
                    for idx, item in s.job_undelivered('massive'):
                        if len(seen) == acknowledged:
                            break  # interruption before callback acknowledgement
                        seen.append(idx)
                        s.job_ack('massive', idx)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 4 * 1024 * 1024)
                print(f'2500-result replay: peak Python allocation {peak / 1024 / 1024:.2f} MiB', flush=True)
            with Store(path) as s:
                for idx, _ in s.job_undelivered('massive'):
                    seen.append(idx)
                    s.job_ack('massive', idx)
                self.assertEqual(seen, list(range(count)))
                self.assertEqual(s.job_undelivered_indices('massive'), set())


if __name__ == '__main__':
    unittest.main()
