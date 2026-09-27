import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resource_budget as resources


class ResourceBudgetTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.sample = {'memory_free_percent': 60, 'thermal_limited': False, 'error': None}
        self.calls = 0
        def probe():
            self.calls += 1
            return dict(self.sample)
        self.probe = probe
        self.governor = resources.Governor(probe, lambda: self.now)

    def test_cached_readings_do_not_spawn_probe_per_request(self):
        self.governor.state()
        self.governor.state()
        self.assertEqual(self.calls, 1)
        self.now += 11
        self.governor.state()
        self.assertEqual(self.calls, 2)

    def test_memory_recovery_requires_headroom_and_stability(self):
        self.sample['memory_free_percent'] = 15
        self.assertFalse(self.governor.state()['allowed'])
        self.sample['memory_free_percent'] = 30
        self.now += 11
        self.assertFalse(self.governor.state()['allowed'])
        self.sample['memory_free_percent'] = 40
        self.now += 11
        self.assertFalse(self.governor.state()['allowed'])
        self.now += 20
        self.assertFalse(self.governor.state()['allowed'])
        self.now += 11
        self.assertTrue(self.governor.state()['allowed'])

    def test_thermal_pressure_and_unavailable_monitor_fail_closed(self):
        self.sample['thermal_limited'] = True
        with self.assertRaises(resources.Deferred):
            with self.governor.lease('k2'):
                self.fail('must not run')
        self.sample.update(thermal_limited=None, error='monitor unavailable')
        self.assertEqual(self.governor.state(force=True)['reason'], 'monitor unavailable')

    def test_start_requires_extra_headroom(self):
        self.sample['memory_free_percent'] = 30
        self.assertTrue(self.governor.state()['allowed'])
        with self.assertRaises(resources.Deferred):
            with self.governor.lease('start', startup=True):
                self.fail('must not start')

    def test_one_ai_call_and_equal_rest_even_after_failure(self):
        with self.governor.lease('notes'):
            self.assertTrue(self.governor.state()['busy'])
            with self.assertRaises(resources.Deferred):
                with self.governor.lease('laya'):
                    self.fail('concurrent inference')
            self.now += 5
        self.assertFalse(self.governor.state()['allowed'])
        self.now += 5
        self.assertTrue(self.governor.state()['allowed'])
        with self.assertRaises(ValueError):
            with self.governor.lease('laya'):
                raise ValueError('test failure')
        self.assertFalse(self.governor.state()['busy'])

    def test_file_lock_serializes_separate_governors(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'ai.lock'
            first = resources.Governor(self.probe, lambda: self.now, lock)
            second = resources.Governor(self.probe, lambda: self.now, lock)
            with first.lease('notes'):
                with self.assertRaises(resources.Deferred):
                    with second.lease('laya'):
                        self.fail('different governor ran concurrently')
            with self.assertRaises(resources.Deferred):
                with second.lease('laya'):
                    self.fail('cross-process duty cycle ignored')

    def test_low_memory_does_not_call_model_start(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('k2_test_service', Path(__file__).resolve().parents[2] / 'ops/k2-service.py')
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)
        self.sample['memory_free_percent'] = 10
        with patch.object(service, 'owned_pid', return_value=None), patch.object(service.resource_budget, 'lease', self.governor.lease), patch.object(service, '_start') as launch:
            with self.assertRaises(RuntimeError):
                service.start()
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
