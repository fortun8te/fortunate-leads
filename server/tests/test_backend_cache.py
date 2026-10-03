import concurrent.futures
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend_cache import CacheBusy, CacheStore


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / 'cache.sqlite')
        self.conn = sqlite3.connect(self.path)
        self.conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.conn.execute("INSERT INTO settings VALUES('lead_data_rev','0')")
        self.conn.commit()
        self.cache = CacheStore()

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def read(self, compute):
        return self.cache.cached(self.conn, 'view', {}, compute)

    def test_revision_and_query_are_separate(self):
        self.assertEqual(self.read(lambda: 1), 1)
        self.assertEqual(self.read(lambda: 2), 1)
        self.conn.execute("UPDATE settings SET value='1'")
        self.conn.commit()
        self.assertEqual(self.read(lambda: 2), 2)
        self.assertEqual(self.cache.cached(self.conn, 'view', {'q': ['x']}, lambda: 3), 3)
        self.assertFalse(self.conn.in_transaction)

    def test_rollback_never_publishes(self):
        self.conn.execute("UPDATE settings SET value='1'")
        self.assertEqual(self.read(lambda: 'uncommitted'), 'uncommitted')
        self.conn.rollback()
        self.conn.execute("UPDATE settings SET value='1'")
        self.conn.commit()
        self.assertEqual(self.read(lambda: 'committed'), 'committed')

    def test_concurrent_miss_computes_once(self):
        start = threading.Barrier(8)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def compute():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return {'people': 7}

        def reader():
            conn = sqlite3.connect(self.path)
            try:
                start.wait(timeout=2)
                return self.cache.cached(conn, 'view', {}, compute)
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(reader) for _ in range(8)]
            self.assertTrue(entered.wait(2))
            release.set()
            self.assertEqual([f.result(timeout=2) for f in futures], [{'people': 7}] * 8)
        self.assertEqual(len(calls), 1)

    def test_clear_during_compute_does_not_repopulate(self):
        def compute():
            self.cache.clear()
            return 'old'
        self.assertEqual(self.read(compute), 'old')
        self.assertFalse(self.cache.values)
        self.assertEqual(self.read(lambda: 'new'), 'new')

    def test_failure_releases_snapshot_and_pending_key(self):
        def fail():
            raise ValueError('failed')
        with self.assertRaisesRegex(ValueError, 'failed'):
            self.read(fail)
        self.assertFalse(self.conn.in_transaction)
        self.assertFalse(self.cache._pending)
        self.assertEqual(self.read(lambda: 'retry'), 'retry')

    def test_bound_and_recency(self):
        cache = CacheStore(max_entries=2)
        for key in ('a', 'b', 'a', 'c'):
            cache._get_or_compute(key, lambda: key)
        self.assertEqual(list(cache.values), ['a', 'c'])

    def test_wait_is_bounded_and_leader_can_finish(self):
        cache = CacheStore(wait_timeout=.01)
        entered, release = threading.Event(), threading.Event()
        def compute():
            entered.set()
            release.wait(2)
            return 'done'
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            leader = pool.submit(cache._get_or_compute, 'a', compute)
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaises(CacheBusy):
                    cache._get_or_compute('a', lambda: 'duplicate')
            finally:
                release.set()
            self.assertEqual(leader.result(timeout=1), 'done')
        self.assertEqual(cache._get_or_compute('a', lambda: 'duplicate'), 'done')

    def test_inflight_keys_are_bounded(self):
        cache = CacheStore(max_entries=1)
        with self.assertRaises(CacheBusy):
            cache._get_or_compute('a', lambda: cache._get_or_compute('b', lambda: 2))
        self.assertFalse(cache._pending)

    def test_memory_database_bypasses_cache(self):
        conn = sqlite3.connect(':memory:')
        try:
            self.assertEqual(self.cache.cached(conn, 'x', {}, lambda: 1), 1)
            self.assertEqual(self.cache.cached(conn, 'x', {}, lambda: 2), 2)
            self.assertFalse(self.cache.values)
        finally:
            conn.close()

    def test_seed_overlap_memory_databases_do_not_share_cached_identity(self):
        from backend.common import AppConfig
        from backend.maps import MapService
        service = MapService(AppConfig(self.path), self.cache)
        for names in (('alpha', 'beta'), ('different', 'other')):
            conn = sqlite3.connect(':memory:')
            try:
                conn.execute('CREATE TABLE settings(key TEXT,value TEXT)')
                conn.execute('CREATE TABLE current_edges(person_id INTEGER,seed TEXT)')
                conn.executemany('INSERT INTO current_edges VALUES(1,?)', [(name,) for name in names])
                conn.commit()
                result = service.seed_links(conn)
                self.assertEqual(result, [{'source': 's:' + names[0], 'target': 's:' + names[1], 'shared': 1}])
                self.assertIsNone(self.cache.seed_links[0])
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
