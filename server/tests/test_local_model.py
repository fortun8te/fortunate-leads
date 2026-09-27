import json
from contextlib import nullcontext
import sys
import threading
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_model as model


class LocalModelTest(unittest.TestCase):
    def setUp(self):
        model._retry_at = 0
        mocked = patch.object(model.resource_budget, 'lease', return_value=nullcontext())
        mocked.start()
        self.addCleanup(mocked.stop)
        activity = patch.object(model, '_record_activity')
        activity.start()
        self.addCleanup(activity.stop)

    def replies(self, result=None, reason='stop', tokens=50):
        return [{'status': 'ok'}, {'data': [{'id': model.MODEL}]}, {'prompt': 'rendered'},
                {'tokens': [1] * tokens}, {'choices': [{'finish_reason': reason,
                'message': {'content': json.dumps(result or {'facts': []})}}]}]

    def test_fixed_local_request_and_bounded_structured_result(self):
        with patch.object(model, '_request', side_effect=self.replies()) as call:
            self.assertEqual(model.complete_json('sys', 'note', {'type': 'object'}, 600), {'facts': []})
        path, body = call.call_args.args
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(body['model'], model.MODEL)
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(body['max_tokens'], 600)
        self.assertEqual(call.call_args.kwargs['timeout'], 45)
        self.assertNotIn('reasoning_budget_tokens', body)

    def test_profile_reasoning_budget_reserves_answer_room_without_more_context(self):
        with patch.object(model, '_request', side_effect=self.replies()) as call:
            model.complete_json('sys', 'bio', {'type': 'object'}, 700, reasoning_budget_tokens=200)
        body = call.call_args.args[1]
        self.assertEqual(body['reasoning_budget_tokens'], 200)
        self.assertEqual(body['max_tokens'], 700)

    def test_invalid_reasoning_budget_never_calls_model(self):
        for budget in (-1, 700, True, 1.5):
            with self.subTest(budget=budget), patch.object(model, '_request') as call:
                with self.assertRaisesRegex(ValueError, 'reasoning budget'):
                    model.complete_json('sys', 'bio', {}, 700, reasoning_budget_tokens=budget)
                call.assert_not_called()

    def test_single_inference_lane_never_queues_duplicate_call(self):
        model._lock.acquire()
        try:
            with patch.object(model, '_request') as call:
                with self.assertRaises(model.Busy):
                    model.complete_json('s', 'u', {})
                call.assert_not_called()
        finally:
            model._lock.release()

    def test_no_truncated_completion_accepted_and_slot_released(self):
        with patch.object(model, '_request', side_effect=self.replies(reason='length')):
            with self.assertRaisesRegex(ValueError, 'truncated'):
                model.complete_json('s', 'u', {})
        self.assertFalse(model._lock.locked())

    def test_exact_token_budget_blocks_inference_before_context_truncation(self):
        with patch.object(model, '_request', side_effect=self.replies(tokens=3900)) as call:
            with self.assertRaisesRegex(ValueError, 'context budget'):
                model.complete_json('s', 'u', {})
            self.assertEqual(call.call_count, 4)

    def test_failure_backoff_does_not_spin_or_fallback(self):
        with patch.object(model, '_request', side_effect=model.Unavailable('offline')) as call:
            with self.assertRaises(model.Unavailable):
                model.complete_json('s', 'u', {})
            with self.assertRaises(model.Busy):
                model.complete_json('s', 'u', {})
            self.assertEqual(call.call_count, 1)

    def test_wrong_model_rejected(self):
        with patch.object(model, '_request', side_effect=[{'status': 'ok'}, {'data': [{'id': 'different-model'}]}]) as call:
            with self.assertRaises(model.Unavailable):
                model.complete_json('s', 'u', {})
            self.assertEqual(call.call_count, 2)

    def test_pressure_stops_only_through_owned_service_helper(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(model, 'SERVICE_ROOT', Path(directory)):
            (Path(directory) / 'pid').write_text('123')
            with patch.object(model.resource_budget, 'state', return_value={'recovering': True}), patch.object(model.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(model.maintain_service(), {'stopped': True, 'reason': 'memory_or_heat'})
                self.assertEqual(run.call_args.args[0][-1], 'stop')
                self.assertTrue(run.call_args.args[0][1].endswith('ops/k2-service.py'))

    def test_health_poll_does_not_extend_idle_and_recent_work_prevents_stop(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(model, 'SERVICE_ROOT', Path(directory)):
            root = Path(directory)
            (root / 'pid').write_text('123')
            (root / 'last_activity').write_text('1000')
            with patch.object(model.time, 'time', return_value=1400), patch.object(model.resource_budget, 'state', return_value={}), patch.object(model.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(model.maintain_service()['reason'], 'idle')
                self.assertEqual((root / 'last_activity').read_text(), '1000')
                (root / 'last_activity').write_text('1390')
                self.assertFalse(model.maintain_service()['stopped'])
                self.assertEqual(run.call_count, 1)

    def test_resource_retry_deadline_survives_adapter(self):
        with patch.object(model.resource_budget, 'lease', side_effect=model.resource_budget.Deferred('rest', 3.25)), patch.object(model, '_request') as call:
            with self.assertRaises(model.Busy) as deferred:
                model.complete_json('s', 'u', {})
            self.assertEqual(deferred.exception.retry_after, 3.25)
            call.assert_not_called()

    def test_transport_backoff_reports_remaining_time_separately(self):
        with patch.object(model.time, 'monotonic', return_value=100):
            model._retry_at = 120
            with self.assertRaises(model.Busy) as deferred:
                model.complete_json('s', 'u', {})
            self.assertEqual(deferred.exception.retry_after, 20)

    def test_redirects_rejected(self):
        with self.assertRaises(model.Unavailable):
            model.NoRedirect().redirect_request(None, None, None, None, None, None)


if __name__ == '__main__':
    unittest.main()
