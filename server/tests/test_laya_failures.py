"""Optional-service failures must not escape or make health appear successful."""
import io
import json
import sys
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import laya


def response(body):
    return io.BytesIO(json.dumps(body).encode())


class LayaFailureTest(unittest.TestCase):
    def setUp(self):
        laya.reset()
        self.addCleanup(laya.reset)

    def test_initial_probe_and_cache_work_at_clock_zero(self):
        self.assertIsNone(laya.last_known())
        with patch.object(laya, '_open', side_effect=lambda *a: response({'ok': True})) as call:
            self.assertTrue(laya.available(now=0))
            self.assertIs(laya.last_known(), True)
            self.assertTrue(laya.available(now=laya.HEALTH_TTL - 1))
            self.assertEqual(call.call_count, 1)
            self.assertTrue(laya.available(now=laya.HEALTH_TTL))
            self.assertEqual(call.call_count, 2)
            laya.reset()
            self.assertIsNone(laya.last_known())
            self.assertTrue(laya.available(now=1))
            self.assertEqual(call.call_count, 3)

    def test_health_requires_explicit_boolean_true(self):
        invalid = [None, [], True, {}, {'ok': False}, {'ok': None}, {'ok': 0}, {'ok': 1}, {'ok': 'true'}]
        for body in invalid:
            with self.subTest(body=body):
                laya.reset()
                with patch.object(laya, '_open', side_effect=lambda *a: response(body)) as call:
                    self.assertFalse(laya.available(now=0))
                    self.assertIs(laya.last_known(), False)
                    self.assertFalse(laya.available(now=1))
                    self.assertEqual(call.call_count, 1)

    def test_malformed_envelopes_back_off_without_raising(self):
        invalid = [None, [], 1, 'bad', {}, {'results': None}, {'results': False},
                   {'results': 1}, {'results': 'bad'}, {'results': {}}]
        for body in invalid:
            with self.subTest(body=body):
                laya.reset()
                with patch.object(laya, '_open', return_value=response(body)) as call, \
                        patch.object(laya.time, 'monotonic', return_value=0):
                    self.assertEqual(laya.decide([{'id': 1}]), {})
                    self.assertIs(laya.last_known(), False)
                    self.assertFalse(laya.available(now=1))
                    self.assertEqual(call.call_count, 1)

    def test_failed_probe_retries_after_ttl(self):
        with patch.object(laya, '_open', side_effect=[response({}), response({'ok': True})]) as call:
            self.assertFalse(laya.available(now=0))
            self.assertFalse(laya.available(now=laya.HEALTH_TTL - 1))
            self.assertEqual(call.call_count, 1)
            self.assertTrue(laya.available(now=laya.HEALTH_TTL))
            self.assertEqual(call.call_count, 2)

    def test_invalid_json_response_is_skipped(self):
        with patch.object(laya, '_open', side_effect=lambda *a: io.BytesIO(b'{broken')), \
                patch.object(laya.time, 'monotonic', return_value=0):
            self.assertFalse(laya.available())
            laya.reset()
            self.assertEqual(laya.decide([{'id': 1}]), {})
            self.assertIs(laya.last_known(), False)

    def test_transport_errors_are_skipped(self):
        for error in (OSError('offline'), IncompleteRead(b'partial', 100)):
            with self.subTest(error=type(error).__name__):
                laya.reset()
                with patch.object(laya, '_open', side_effect=error), \
                        patch.object(laya.time, 'monotonic', return_value=0):
                    self.assertFalse(laya.available())
                    laya.reset()
                    self.assertEqual(laya.decide([{'id': 1}]), {})
                    self.assertIs(laya.last_known(), False)

    def test_later_failed_batch_preserves_earlier_answers_and_stops(self):
        good = {'results': [{'id': '1', 'answers': {'creator': {'p': .2}}}]}
        with patch.object(laya, 'BATCH', 1), \
                patch.object(laya, '_open', side_effect=[response(good), response({'results': 1})]) as call:
            self.assertEqual(laya.decide([{'id': 1}, {'id': 2}, {'id': 3}]), {1: {'creator': .2}})
            self.assertEqual(call.call_count, 2)
            self.assertIs(laya.last_known(), False)

    def test_non_finite_id_does_not_discard_valid_rows(self):
        body = {'results': [{'id': float('inf'), 'answers': {}},
                            {'id': '2', 'answers': {'creator': {'p': .2}}}]}
        with patch.object(laya, '_open', return_value=response(body)):
            self.assertEqual(laya.decide([{'id': 2}]), {2: {'creator': .2}})


if __name__ == '__main__':
    unittest.main()
