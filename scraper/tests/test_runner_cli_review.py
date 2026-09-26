"""Offline input, streamed export and interrupted-run CLI regressions."""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from igscrape_app import cli
from igscrape_app.runner import Job, write_outputs
from igscraper.cli import main as advanced
from igscraper.config import ScraperConfig
from igscraper.inputs import validate_inputs
from igscraper.scraper import ProfileScraper
from igscraper.store import Store


def config(path=None):
    return ScraperConfig(cache_path=str(path) if path else None, health_interval=0,
                         concurrency=1, rate_per_ip=0, max_retries=0)


def result(target):
    return {'inputUrl': target.input_url, 'username': target.username,
            'biography': 'synthetic bio', '_scrape': {'bioStatus': 'observed'}}


class InputCliTests(unittest.TestCase):
    def test_strict_types_and_alias_dedup(self):
        self.assertEqual(validate_inputs(['NASA', '@nasa', 'https://instagram.com/nasa/', 'natgeo']),
                         ['NASA', 'natgeo'])
        for bad in ([None], [True], [123], [{}], [''], ['https://instagram.com/p/post'], 'nasa'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_inputs(bad)

    def test_output_collision_rejected_before_config_or_network(self):
        with patch('igscrape_app.runner.build_config', side_effect=AssertionError('must not start')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.run_cli(['nasa', '-o', 'same.json', '--csv', './same.json']), 2)
            self.assertIn('different file paths', err.getvalue())

    def test_bad_json_contract_is_friendly(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'input.json'
            for payload in ([], {'usernames': 'nasa'}, {'usernames': [True]},
                            {'usernames': ['nasa'], 'includeAboutSection': 'false'}):
                path.write_text(json.dumps(payload))
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(advanced(['--input', str(path)]), 2)
                    self.assertIn('error:', err.getvalue())

    def test_bad_numeric_options_rejected_before_config(self):
        for options in (['-c', '0'], ['--target-rate', 'nan'], ['--target-rate', '-1']):
            with patch('igscrape_app.runner.build_config', side_effect=AssertionError('must not start')), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run_cli(['nasa'] + options), 2)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_then_resume_streams_every_saved_item_in_requested_order(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config(Path(d) / 'cache.sqlite')
            pending = asyncio.Event()
            calls = []
            async def fetch(_scraper, target, *_):
                calls.append(target.username)
                if target.username == 'b':
                    pending.set()
                    await asyncio.Event().wait()
                return result(target)
            first = Job(['a', 'b', 'c'], cfg, retain_items=False)
            with patch.object(ProfileScraper, 'scrape_target', fetch):
                task = asyncio.create_task(first.run())
                await asyncio.wait_for(pending.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(first.progress.state, 'stopped')
            self.assertEqual(first._partial, [])
            with first.output_source() as rows:
                self.assertEqual([r['username'] for r in rows()], ['a'])
            async def finish(_scraper, target, *_):
                self.assertNotEqual(target.username, 'a', 'completed profile fetched twice')
                return result(target)
            second = Job(['c', 'b', 'a'], cfg, retain_items=False)
            with patch.object(ProfileScraper, 'scrape_target', finish):
                self.assertEqual(await second.run(), [])
            with second.output_source() as rows:
                self.assertEqual([r['username'] for r in rows()], ['c', 'b', 'a'])
                write_outputs(rows, str(Path(d) / 'out.json'), str(Path(d) / 'out.csv'))
            self.assertEqual(len(json.loads((Path(d) / 'out.json').read_text())), 3)
            self.assertEqual(second.progress.already_done, 1)
            self.assertEqual(second._partial, [])

    async def test_callback_cancel_is_not_acknowledged_until_successful_replay(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config(Path(d) / 'cache.sqlite')
            cfg.job = 'callback'
            entered = asyncio.Event()
            async def sink(_):
                entered.set()
                await asyncio.Event().wait()
            async with ProfileScraper(cfg) as scraper:
                scraper.scrape_target = lambda target, *_: asyncio.sleep(0, result=result(target))
                task = asyncio.create_task(scraper.run(['a'], on_item=sink, collect=False))
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(scraper.store.job_undelivered_indices(cfg.job), {0})
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            seen = []
            async with ProfileScraper(cfg) as scraper:
                async def no_fetch(*_):
                    raise AssertionError('must replay')
                scraper.scrape_target = no_fetch
                await scraper.run(['a'], on_item=seen.append, collect=False)
                self.assertEqual(scraper.store.job_undelivered_indices(cfg.job), set())
            self.assertEqual(len(seen), 1)

    async def test_no_cache_spools_payloads_and_replays_export(self):
        job = Job(['a', 'b'], config(), retain_items=False)
        async def fetch(_scraper, target, *_):
            return result(target)
        try:
            with patch.object(ProfileScraper, 'scrape_target', fetch):
                self.assertEqual(await job.run(), [])
            self.assertEqual(job._partial, [])
            with job.output_source() as rows:
                self.assertEqual(list(rows()), list(rows()))
                self.assertEqual(len(list(rows())), 2)
        finally:
            job._spool.close()

    async def test_export_holds_snapshot_across_passes(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config(Path(d) / 'cache.sqlite')
            job = Job(['a'], cfg, retain_items=False)
            with Store(cfg.cache_path) as store:
                store.job_init(job.cfg.job, ['a'])
                store.job_done(job.cfg.job, 0, {'username': 'a', 'biography': 'before'})
                with job.output_source() as rows:
                    self.assertEqual(next(rows())['biography'], 'before')
                    store.job_done(job.cfg.job, 0, {'username': 'a', 'biography': 'after'})
                    self.assertEqual(next(rows())['biography'], 'before')


if __name__ == '__main__':
    unittest.main()
