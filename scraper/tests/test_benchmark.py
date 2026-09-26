import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('scraper_benchmark', Path(__file__).resolve().parents[1]/'benchmark.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_counts_failures_partial_cache_separately(self):
        args = bench.parser().parse_args(['--profiles', '40', '--latency-ms', '1', '--seconds', '5', '--cache-fraction', '.1'])
        report = await bench.benchmark(args)
        self.assertFalse(report['networkVerified'])
        self.assertEqual(report['processed'], 40)
        self.assertEqual(report['cached'], 4)
        self.assertGreater(report['partial'], 0)
        self.assertGreater(report['errors'], 0)
        self.assertEqual(sum(report[k] for k in ['freshSuccess', 'cached', 'partial', 'errors']), 40)
        self.assertLess(report['freshBiosPerMinute'], report['processedPerMinute'])

    async def test_time_limit_returns_partial_without_running_on(self):
        args = bench.parser().parse_args(['--profiles', '20', '--latency-ms', '20', '--seconds', '.035', '--cache-fraction', '0'])
        report = await bench.benchmark(args)
        self.assertGreater(report['unfinished'], 0)
        self.assertLess(report['elapsedSeconds'], .2)

    def test_live_requires_explicit_handles_and_bounds(self):
        args = bench.parser().parse_args(['--live'])
        with self.assertRaisesRegex(ValueError, 'handles-file'):
            bench.validate(args)

    async def test_legacy_api_rate_excludes_cache_and_errors(self):
        from igscraper import api
        import time
        api._runs['test-metrics'] = {'id': 'test-metrics', 'status': 'succeeded', 'total': 3, 'errors': 1,
            'started': time.time()-60, 'finished': None, 'error': None,
            'items': {0: {'biography': '', '_scrape': {'bioStatus': 'empty', 'cacheHit': False}},
                      1: {'biography': 'a', '_scrape': {'bioStatus': 'observed', 'cacheHit': True}},
                      2: {'error': 'incomplete_profile'}}}
        try:
            result = await api.run_status('test-metrics')
            self.assertEqual(result['perMinute'], 1)
            self.assertEqual(result['processedPerMinute'], 3)
            self.assertEqual(result['cached'], 1)
            self.assertEqual(result['runPersistence'], 'memory_only')
        finally:
            api._runs.pop('test-metrics', None)

if __name__ == '__main__':
    unittest.main()
