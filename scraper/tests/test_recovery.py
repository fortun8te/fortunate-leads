"""Crash/restart regressions for the durable scrape queue."""
import asyncio
import os
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from igscraper import mapper
from igscraper.config import ScraperConfig
from igscraper.scraper import ProfileScraper
from igscraper.store import Store
from igscrape_app import ui


def cfg(path, **overrides):
    return ScraperConfig(cache_path=str(path), job='recovery', health_interval=0,
                         rate_per_ip=0, max_retries=0, **overrides)


def item(target):
    return mapper.with_coverage({"inputUrl": target.input_url, "username": target.username,
                                 "biography": "bio for " + target.username}, "test")


class RecoveryStoreTests(unittest.TestCase):
    def test_old_job_schema_migrates_and_keeps_completed_result(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'old.sqlite'
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE job_items(job TEXT NOT NULL, idx INTEGER NOT NULL, raw TEXT NOT NULL, "
                       "status TEXT NOT NULL DEFAULT 'pending', item TEXT, updated REAL, PRIMARY KEY(job, idx))")
            db.execute("INSERT INTO job_items VALUES('recovery', 0, 'a', 'done', "
                       "'{\"username\":\"a\",\"biography\":\"\"}', 1)")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.job_undelivered_indices('recovery'), set())
            self.assertEqual(store.job_items('recovery')[0][3]['username'], 'a')
            store.close()

    def test_expired_final_errors_stay_final_even_when_retry_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 'cache.sqlite')
            store.job_init('recovery', ['missing', 'bad?input', 'transient'])
            for idx, code in enumerate(('not_found', 'invalid_input', 'rate_limited')):
                store.job_done('recovery', idx, {'inputUrl': str(idx), 'error': code})
            store.db.execute('UPDATE job_items SET updated=0')
            self.assertEqual(store.job_pending('recovery', retry_errors=False, max_age=1), [])
            self.assertEqual(store.job_pending('recovery', retry_errors=True, max_age=1), [(2, 'transient')])
            store.close()


class RecoveryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_saves_each_output_by_atomic_replace(self):
        with tempfile.TemporaryDirectory() as d:
            class FakeJob:
                partial_items = [{'inputUrl': 'a', 'username': 'a', 'biography': ''}]
                async def run(self):
                    return self.partial_items
            app = ui.App.__new__(ui.App)
            app.saved = []
            with patch.object(ui, 'RESULTS_DIR', Path(d)), \
                 patch('igscrape_app.runner.os.replace', wraps=os.replace) as replace:
                await app._run(FakeJob())
            self.assertEqual(replace.call_count, 2)
            self.assertEqual({Path(p).suffix for p in app.saved}, {'.json', '.csv'})
            self.assertTrue(all(Path(p).exists() for p in app.saved))

    async def test_failed_callback_replays_saved_result_without_refetch_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            config = cfg(Path(d) / 'cache.sqlite')
            async with ProfileScraper(config) as first:
                calls = []
                async def fetch(target, *_):
                    calls.append(target.username)
                    return item(target)
                first.scrape_target = fetch
                def broken(_):
                    raise RuntimeError('sink unavailable')
                with self.assertRaisesRegex(RuntimeError, 'sink unavailable'):
                    await first.run(['a'], on_item=broken, concurrency=1)
                self.assertEqual(calls, ['a'])
                self.assertEqual(first.store.job_undelivered_indices('recovery'), {0})

            received = []
            async with ProfileScraper(config) as second:
                async def must_not_fetch(*_):
                    raise AssertionError('replay must not fetch')
                second.scrape_target = must_not_fetch
                result = await second.run(['a'], on_item=received.append, concurrency=1)
                self.assertEqual(received, result)
                self.assertEqual(second.store.job_undelivered_indices('recovery'), set())
                self.assertEqual(second.last_run_counts['alreadyDone'], 0)
                self.assertEqual(second.last_run_counts['done'], 1)

            async with ProfileScraper(config) as third:
                third.scrape_target = must_not_fetch
                self.assertEqual((await third.run(['a']))[0]['biography'], 'bio for a')

    async def test_streaming_resume_skips_loading_completed_payloads(self):
        with tempfile.TemporaryDirectory() as d:
            config = cfg(Path(d) / 'cache.sqlite')
            async with ProfileScraper(config) as first:
                first.scrape_target = lambda target, *_: asyncio.sleep(0, result=item(target))
                await first.run(['a', 'b'], concurrency=1)
            seen = []
            with patch.object(Store, 'job_items', side_effect=AssertionError('bulk payload load')):
                async with ProfileScraper(config) as second:
                    second.scrape_target = lambda *_: asyncio.sleep(0, result=AssertionError('unexpected fetch'))
                    result = await second.run(['a', 'b'], on_item=seen.append, collect=False)
                    self.assertEqual(result, [])
                    self.assertEqual(seen, [])
                    self.assertEqual(second.last_run_counts['alreadyDone'], 2)
                    self.assertEqual(second.last_run_counts['done'], 2)

    async def test_large_streaming_replay_is_bounded_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            config = cfg(Path(d) / 'cache.sqlite')
            count = 320
            raws = [f'user{i:04d}' for i in range(count)]
            # About 20 MiB of saved JSON, far larger than one replay page.
            with Store(config.cache_path) as store:
                store.job_init('recovery', raws)
                for idx, raw in enumerate(raws):
                    store.job_done('recovery', idx, {
                        'username': raw, 'biography': 'x' * (64 * 1024),
                        '_scrape': {'bioStatus': 'observed', 'observedAt': '2026-01-01'},
                    }, delivered=False)

            requested = list(reversed(raws))
            first_seen = []
            async with ProfileScraper(config) as first:
                async def must_not_fetch(*_):
                    raise AssertionError('saved result must not be fetched again')
                first.scrape_target = must_not_fetch

                def fail_after_120(result):
                    if len(first_seen) == 120:
                        raise RuntimeError('sink interrupted')
                    first_seen.append(result['username'])

                tracemalloc.start()
                try:
                    with self.assertRaisesRegex(RuntimeError, 'sink interrupted'):
                        await first.run(requested, on_item=fail_after_120, collect=False)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 10 * 1024 * 1024,
                                'replay retained more than a bounded page of payloads')
                self.assertEqual(first_seen, requested[:120])
                self.assertEqual(len(first.store.job_undelivered_indices('recovery')), count - 120)

            second_seen = []
            async with ProfileScraper(config) as second:
                second.scrape_target = must_not_fetch
                self.assertEqual(await second.run(requested, on_item=lambda result:
                    second_seen.append(result['username']), collect=False), [])
                self.assertEqual(second.store.job_undelivered_indices('recovery'), set())
            self.assertEqual(first_seen + second_seen, requested)

    async def test_reordered_inputs_reorder_saved_results(self):
        with tempfile.TemporaryDirectory() as d:
            config = cfg(Path(d) / 'cache.sqlite')
            async with ProfileScraper(config) as first:
                first.scrape_target = lambda target, *_: asyncio.sleep(0, result=item(target))
                self.assertEqual([r['username'] for r in await first.run(['a', 'b'])], ['a', 'b'])
            async with ProfileScraper(config) as second:
                async def must_not_fetch(*_):
                    raise AssertionError('completed rows must be reused')
                second.scrape_target = must_not_fetch
                self.assertEqual([r['username'] for r in await second.run(['b', 'a'])], ['b', 'a'])


if __name__ == '__main__':
    unittest.main()
