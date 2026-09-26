"""SQLite store regression checks using only disposable local databases."""
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from igscraper.store import Store
import igscraper.store as store_module
from igscraper import mapper
from igscraper.config import ScraperConfig
from igscraper.scraper import ProfileScraper


class StoreConcurrencyTests(unittest.TestCase):
    def test_same_store_serializes_transactions_across_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'cache.sqlite')
            first_inside = threading.Event()
            release_first = threading.Event()
            errors = []
            original = store_module.target_key

            def paused_key(raw):
                if raw == 'first':
                    first_inside.set()
                    if not release_first.wait(5):
                        raise TimeoutError('first writer was never released')
                return original(raw)

            def run(raw):
                try:
                    store.job_init('shared', [raw])
                except BaseException as error:
                    errors.append(error)

            with patch.object(store_module, 'target_key', side_effect=paused_key):
                first = threading.Thread(target=run, args=('first',))
                second = threading.Thread(target=run, args=('second',))
                first.start()
                self.assertTrue(first_inside.wait(5))
                second.start()
                second.join(.2)
                release_first.set()
                first.join(5)
                second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([raw for _, raw, *_ in store.job_items('shared')], ['first', 'second'])
            store.close()

    def test_failed_cache_write_rolls_back_both_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'cache.sqlite')
            store.db.execute("CREATE TRIGGER reject_id BEFORE INSERT ON profiles WHEN NEW.key LIKE 'id:%' "
                             "BEGIN SELECT RAISE(ABORT, 'blocked alias'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'blocked alias'):
                store.cache_put({'username': 'alice', 'id': '1', 'biography': 'bio'})
            self.assertIsNone(store.cache_get('u:alice', 100))
            store.db.execute('DROP TRIGGER reject_id')
            store.cache_put({'username': 'bob', 'id': '2', 'biography': 'bio'})
            self.assertIsNotNone(store.cache_get('id:2', 100))
            store.close()

    def test_two_stores_open_legacy_schema_without_migration_race(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite'
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE job_items(job TEXT NOT NULL, idx INTEGER NOT NULL, raw TEXT NOT NULL, "
                       "status TEXT NOT NULL DEFAULT 'pending', item TEXT, updated REAL, PRIMARY KEY(job, idx))")
            db.commit()
            db.close()
            start = threading.Barrier(2)
            errors = []
            stores = []
            def open_store():
                try:
                    start.wait(5)
                    stores.append(Store(path))
                except BaseException as error:
                    errors.append(error)
            threads = [threading.Thread(target=open_store) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            for store in stores:
                store.close()
            self.assertEqual(errors, [])
            self.assertEqual(len(stores), 2)

    def test_named_job_guard_released_after_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite'
            store = Store(path)
            child = subprocess.Popen([sys.executable, '-c',
                'import sys,time; from igscraper.store import Store; '
                's=Store(sys.argv[1]); '
                'with_guard=s.job_guard("shared"); with_guard.__enter__(); '
                'print("READY",flush=True); time.sleep(30)', str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'READY', child.stderr.read() if child.poll() is not None else '')
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    with store.job_guard('shared'):
                        pass
                with store.job_guard('other'):
                    pass
            finally:
                child.kill()
                child.communicate(timeout=5)
            with store.job_guard('shared'):
                pass
            store.close()

    def test_two_store_objects_append_same_job_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite'
            stores = [Store(path), Store(path)]
            start = threading.Barrier(2)
            errors = []
            def append(store, raws):
                try:
                    start.wait(5)
                    store.job_init('shared', raws)
                except BaseException as error:
                    errors.append(error)
            threads = [threading.Thread(target=append, args=(stores[0], ['a', 'b'])),
                       threading.Thread(target=append, args=(stores[1], ['@a', 'c']))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(len(stores[0].job_items('shared')), 3)
            for store in stores:
                store.close()

    def test_future_cache_and_job_timestamps_are_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'cache.sqlite')
            item = mapper.with_coverage({'username': 'a', 'biography': 'bio'}, 'test')
            with patch.object(store_module.time, 'time', return_value=2000):
                store.cache_put(item)
                store.job_init('shared', ['a'])
                store.job_done('shared', 0, item)
            with patch.object(store_module.time, 'time', return_value=1000):
                self.assertIsNone(store.cache_get('u:a', 100))
                self.assertEqual(store.job_pending('shared', max_age=100), [(0, 'a')])
            store.job_init('shared', ['missing', 'transient'])
            with patch.object(store_module.time, 'time', return_value=2000):
                store.job_done('shared', 1, {'error': 'not_found'})
                store.job_done('shared', 2, {'error': 'rate_limited'})
            with patch.object(store_module.time, 'time', return_value=1000):
                self.assertEqual(store.job_pending('shared', retry_errors=False, max_age=100), [(0, 'a')])
                self.assertEqual(store.job_pending('shared', retry_errors=True, max_age=100),
                                 [(0, 'a'), (2, 'transient')])
            store.close()


class NamedJobRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_runner_releases_named_job_guard(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            cfg = ScraperConfig(cache_path=str(Path(directory) / 'cache.sqlite'), job='shared',
                                health_interval=0, rate_per_ip=0, max_retries=0)
            entered = asyncio.Event()
            async with ProfileScraper(cfg) as first, ProfileScraper(cfg) as second:
                async def stalled(*_):
                    entered.set()
                    await asyncio.Event().wait()
                first.scrape_target = stalled
                task = asyncio.create_task(first.run(['a'], collect=False, concurrency=1))
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                second.scrape_target = lambda target, *_: asyncio.sleep(0, result=mapper.with_coverage(
                    {'username': target.username, 'biography': 'bio'}, 'test'))
                self.assertEqual(await second.run(['a'], collect=False, concurrency=1), [])
                self.assertEqual(second.store.job_summary('shared'), {'done': 1})

    async def test_future_callback_failure_is_replayed_after_restart(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            cfg = ScraperConfig(cache_path=str(Path(directory) / 'cache.sqlite'), job='shared',
                                health_interval=0, rate_per_ip=0, max_retries=0)
            async with ProfileScraper(cfg) as first:
                first.scrape_target = lambda target, *_: asyncio.sleep(0, result=mapper.with_coverage(
                    {'username': target.username, 'biography': 'bio'}, 'test'))
                async def fail():
                    raise RuntimeError('sink failed')
                with self.assertRaisesRegex(RuntimeError, 'sink failed'):
                    await first.run(['a'], on_item=lambda _item: asyncio.create_task(fail()))
                self.assertEqual(first.store.job_undelivered_indices('shared'), {0})
            received = []
            async with ProfileScraper(cfg) as second:
                async def unexpected_fetch(*_):
                    raise AssertionError('saved item should replay')
                second.scrape_target = unexpected_fetch
                await second.run(['a'], on_item=received.append)
                self.assertEqual(len(received), 1)
                self.assertEqual(second.store.job_undelivered_indices('shared'), set())

    async def test_second_runner_cannot_fetch_same_named_job(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            cfg = ScraperConfig(cache_path=str(Path(directory) / 'cache.sqlite'), job='shared',
                                health_interval=0, rate_per_ip=0, max_retries=0)
            first_started = asyncio.Event()
            release = asyncio.Event()
            async with ProfileScraper(cfg) as first, ProfileScraper(cfg) as second:
                calls = []
                async def fetch(target, *_):
                    calls.append(target.username)
                    first_started.set()
                    await release.wait()
                    return mapper.with_coverage({'username': target.username, 'biography': 'bio'}, 'test')
                first.scrape_target = fetch
                second.scrape_target = fetch
                task = asyncio.create_task(first.run(['a'], collect=False, concurrency=1))
                try:
                    await asyncio.wait_for(first_started.wait(), 5)
                    with self.assertRaisesRegex(RuntimeError, 'already running'):
                        await second.run(['a'], collect=False, concurrency=1)
                finally:
                    release.set()
                    await task
                self.assertEqual(calls, ['a'])
                self.assertEqual(await second.run(['a'], collect=False, concurrency=1), [])


if __name__ == '__main__':
    unittest.main()
