import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import httpx
from igscraper import mapper
from igscraper.config import ScraperConfig
from igscraper.scraper import ProfileScraper
from igscraper.store import Store
from igscraper.output import to_csv, to_json
from igscrape_app.runner import Job, Progress


def transport(payload):
    return lambda _: httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


def cfg(**kw):
    return ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0, **kw)


class MappingTests(unittest.TestCase):
    def test_empty_and_missing_are_distinct(self):
        empty = mapper.with_coverage({'username': 'a', 'biography': ''}, 'test')
        missing = mapper.with_coverage({'username': 'a', 'biography': None}, 'test')
        self.assertNotIn('error', empty)
        self.assertEqual(empty['_scrape']['bioStatus'], 'empty')
        self.assertEqual(missing['error'], 'incomplete_profile')
        self.assertIn('_scrape/bioStatus', to_csv([empty, missing]))
        self.assertEqual(json.loads(to_json([empty]))[0]['biography'], '')

    def test_feed_item_owner_identity_preserved(self):
        item = mapper.map_fallback('https://www.instagram.com/a',
            {'items': [{'pk': 100, 'code': 'C', 'user': {'pk': 42, 'username': 'a'}}]}, None)
        self.assertEqual((item['username'], item['id']), ('a', '42'))

    def test_threads_only_bio_not_verified_instagram_bio(self):
        item = mapper.with_coverage({'biography': 'Threads bio'}, 'fallback', bio_observed=False)
        self.assertEqual(item['_scrape']['bioStatus'], 'unavailable')

    def test_metrics_errors_and_cache_cannot_inflate_fresh_bios(self):
        p = Progress(done=300, errors=100, cached=190, fresh_success=10, started=time.monotonic()-60)
        self.assertAlmostEqual(p.per_min, 10, delta=.1)
        self.assertGreater(p.as_dict()['processedPerMinute'], 290)

    def test_durable_identity_is_canonical_and_options_sensitive(self):
        a = Job(['a', '@b'], cfg(cache_path='unused.sqlite'))
        b = Job(['https://www.instagram.com/b', '@a'], cfg(cache_path='unused.sqlite'))
        c = Job(['a', 'b'], cfg(cache_path='unused.sqlite', include_about=True))
        self.assertEqual(a.cfg.job, b.cfg.job)
        self.assertNotEqual(a.cfg.job, c.cfg.job)


class StoreTests(unittest.TestCase):
    def test_partial_cache_rejected_empty_accepted_and_job_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp)/'cache.sqlite')
            s.cache_put({'username': 'a', 'biography': None})
            self.assertIsNone(s.cache_get('u:a', 100))
            s.cache_put({'username': 'a', 'biography': ''})
            self.assertEqual(s.cache_get('u:a', 100)['biography'], '')
            s.job_init('x', ['a', '@a'])
            s.job_init('x', ['https://www.instagram.com/a', 'b'])
            self.assertEqual(len(s.job_items('x')), 2)
            s.job_done('x', 0, {'username': 'a', 'biography': '', 'error': 'incomplete_profile'})
            self.assertEqual(len(s.job_pending('x')), 2)
            s.close()

    def test_stale_done_is_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp)/'cache.sqlite')
            s.job_init('x', ['a'])
            s.job_done('x', 0, mapper.with_coverage({'username': 'a', 'biography': ''}, 'test'))
            self.assertEqual(s.job_pending('x', max_age=3600), [])
            s.db.execute('UPDATE job_items SET updated=0')
            self.assertEqual(s.job_pending('x', max_age=3600), [(0, 'a')])
            s.close()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_cache_without_coverage_is_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'data': {'user': {'id': 1, 'username': 'a', 'biography': 'new'}}}
            async with ProfileScraper(cfg(cache_path=str(Path(tmp)/'cache.sqlite')), transport_factory=transport(payload)) as s:
                s.store.cache_put({'username': 'a', 'biography': 'old unverified'})
                item = (await s.run(['a']))[0]
                self.assertEqual(item['biography'], 'new')
                self.assertFalse(item['_scrape']['cacheHit'])

    async def test_feed_only_fallback_is_partial_and_not_cached(self):
        def handler(request):
            if '/feed/' in request.url.path:
                return httpx.Response(200, json={'user': {'pk': 42, 'username': 'a'}, 'items': []})
            if 'web_profile_info' in request.url.path:
                return httpx.Response(400, json={'status': 'fail', 'message': 'schema has been deleted'})
            return httpx.Response(200, headers={'content-type': 'text/html'}, text='<html></html>')
        with tempfile.TemporaryDirectory() as tmp:
            async with ProfileScraper(cfg(cache_path=str(Path(tmp)/'cache.sqlite')), transport_factory=lambda _: httpx.MockTransport(handler)) as s:
                item = (await s.run(['a']))[0]
                self.assertEqual(item['id'], '42')
                self.assertEqual(item['error'], 'incomplete_profile')
                self.assertIsNone(s.store.cache_get('u:a', 3600))
                self.assertEqual(s.last_run_counts['freshSuccess'], 0)

    async def test_failure_envelope_is_retryable_not_not_found(self):
        async with ProfileScraper(cfg(), transport_factory=transport({'status': 'fail', 'message': 'Please wait'})) as s:
            items = await s.run(['a'])
            self.assertNotEqual(items[0]['error'], 'not_found')
            self.assertEqual(s.last_run_counts['freshSuccess'], 0)

    async def test_wrong_identity_rejected(self):
        async with ProfileScraper(cfg(), transport_factory=transport({'data': {'user': {'id': 1, 'username': 'b', 'biography': ''}}})) as s:
            self.assertIn('error', (await s.run(['a']))[0])

    async def test_private_empty_bio_success_and_second_run_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'data': {'user': {'id': 1, 'username': 'a', 'biography': '', 'is_private': True}}}
            async with ProfileScraper(cfg(cache_path=str(Path(tmp)/'cache.sqlite')), transport_factory=transport(payload)) as s:
                first = (await s.run(['a']))[0]
                self.assertTrue(first['private'])
                self.assertNotIn('error', first)
                second = (await s.run(['a']))[0]
                self.assertTrue(second['_scrape']['cacheHit'])
                self.assertEqual(s.last_run_counts['freshSuccess'], 0)
                self.assertEqual(s.last_run_counts['cached'], 1)

    async def test_callback_error_cancels_sibling_workers(self):
        finished = []
        async with ProfileScraper(cfg()) as s:
            async def fake(target, *_):
                if target.username == 'b':
                    await asyncio.sleep(.05)
                finished.append(target.username)
                return {'username': target.username, 'biography': '', 'inputUrl': target.input_url}
            s.scrape_target = fake
            def bad_callback(_):
                raise ValueError('sink failed')
            with self.assertRaises(ValueError):
                await s.run(['a', 'b'], bad_callback, concurrency=2)
            await asyncio.sleep(.08)
            self.assertEqual(finished, ['a'])

    async def test_restart_retains_finished_and_retries_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = cfg(cache_path=str(Path(tmp)/'cache.sqlite'))
            def handler(req):
                name = req.url.params['username']
                return httpx.Response(200, json={'data': {'user': {'id': name, 'username': name, 'biography': ''}}})
            with patch('igscrape_app.runner.mock.enabled', return_value=True), patch('igscrape_app.runner.mock.transport_factory', return_value=lambda _: httpx.MockTransport(handler)):
                first = Job(['a', 'b'], conf)
                await first.run()
                store = Store(conf.cache_path)
                store.job_done(first.cfg.job, 1, {'inputUrl': 'https://www.instagram.com/b', 'username': 'b',
                                               'error': 'incomplete_profile', 'biography': None})
                store.db.execute("DELETE FROM profiles WHERE key='u:b'")
                store.close()
                second = Job(['@a', '@b'], conf)
                await second.run()
                self.assertEqual(second.progress.done, 2)
                self.assertEqual(second.progress.already_done, 1)
                self.assertEqual(second.progress.fresh_success, 1)
                self.assertEqual(len(second.items), 2)

if __name__ == '__main__':
    unittest.main()
