import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import laya
import resource_budget


class LayaActivityTest(unittest.TestCase):
    def setUp(self):
        activity = patch.dict(laya._activity, {'unknown': False, 'checked_at': float('-inf')})
        activity.start()
        self.addCleanup(activity.stop)
        self.budget = resource_budget.Governor(lambda: {
            'memory_free_percent': 60, 'thermal_limited': False, 'error': None})
        budget = patch.object(laya.resource_budget, 'set_uncertain', self.budget.set_uncertain)
        budget.start()
        self.addCleanup(budget.stop)

    def timeout(self):
        with patch.object(laya, '_open', side_effect=TimeoutError('request timeout')):
            self.assertEqual(laya.decide([{'id': 1, 'bio': 'Founder'}]), {})
        self.assertFalse(self.budget.state()['allowed'])

    def probe(self, payload):
        laya._activity['checked_at'] = float('-inf')
        with patch.object(laya, '_open', return_value=io.BytesIO(json.dumps(payload).encode())):
            return laya.runtime_status()

    def test_timeout_holds_budget_until_matching_sidecar_reports_idle(self):
        self.timeout()
        health = {'ok': True, 'model': laya.MODEL, 'deployment_version': laya.DEPLOYMENT_VERSION}
        for extra in ({}, {'busy': True}, {'busy': False, 'model': 'wrong-model'}):
            self.assertTrue(self.probe(dict(health, **extra))['activity_unknown'])
            self.assertFalse(self.budget.state()['allowed'])
        self.assertFalse(self.probe(dict(health, busy=False))['busy'])
        self.assertTrue(self.budget.state()['allowed'])

    def test_confirmed_closed_port_releases_hold_but_timeout_does_not(self):
        self.timeout()
        with patch.object(laya, '_open', side_effect=TimeoutError()):
            self.assertTrue(laya.runtime_status()['busy'])
        laya._activity['checked_at'] = float('-inf')
        with patch.object(laya, '_open', side_effect=ConnectionRefusedError()):
            self.assertFalse(laya.runtime_status()['busy'])
        self.assertTrue(self.budget.state()['allowed'])
