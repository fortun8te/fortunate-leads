"""Health checks must run even when the monotonic clock starts near zero."""
import io
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import laya


HEALTHY = ('{"ok": true, "model": "%s", "deployment_version": "%s"}' % (laya.MODEL, laya.DEPLOYMENT_VERSION)).encode()


class HealthCacheTest(unittest.TestCase):
    def setUp(self):
        clock = patch.object(laya.time, 'monotonic', return_value=0.5)   # last_known() reads the clock
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        laya.reset()

    def test_initial_probe_at_zero_and_cached_success(self):
        laya.reset()
        self.assertIsNone(laya.last_known())
        with patch.object(laya, '_open', side_effect=lambda *a: io.BytesIO(HEALTHY)) as request:
            self.assertTrue(laya.available(now=0))
            self.assertTrue(laya.last_known())
            self.assertTrue(laya.available(now=59))
            self.assertEqual(request.call_count, 1)
            self.assertTrue(laya.available(now=60))
            self.assertEqual(request.call_count, 2)

    def test_failure_is_cached_after_initial_probe(self):
        laya.reset()
        with patch.object(laya, '_open', side_effect=OSError('offline')) as request:
            self.assertFalse(laya.available(now=0.05))
            self.assertFalse(laya.available(now=1))
            self.assertFalse(laya.last_known())
            self.assertEqual(request.call_count, 1)
            laya.reset()
            self.assertIsNone(laya.last_known())
            self.assertFalse(laya.available(now=2))
            self.assertEqual(request.call_count, 2)

    def test_concurrent_health_checks_share_one_probe(self):
        laya.reset()
        start = threading.Barrier(8)

        def check():
            start.wait()
            return laya.available(now=0)

        def slow_response(*_):
            time.sleep(0.04)
            return io.BytesIO(HEALTHY)

        with patch.object(laya, '_open', side_effect=slow_response) as request:
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(lambda _: check(), range(8))), [True] * 8)
            self.assertEqual(request.call_count, 1)
