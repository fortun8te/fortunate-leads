"""Offline coverage for the optional K2 LAN connection."""
import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import k2_connection
import local_model
import server


class ConnectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = patch.dict(server.CFG, {'db': str(Path(self.temp.name) / 'leads.sqlite')})
        config.start()
        self.addCleanup(config.stop)
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'connection.json'
        mock = patch.object(k2_connection, 'CONFIG_PATH', path)
        mock.start()
        self.addCleanup(mock.stop)
        for name, value in (('_remote_ready_endpoint', None), ('_activity_unknown', False)):
            state = patch.object(local_model, name, value)
            state.start()
            self.addCleanup(state.stop)

    def test_default_is_fixed_local_and_status_does_not_probe_remote(self):
        self.assertEqual(k2_connection.read(), k2_connection.DEFAULT)
        with patch.object(local_model, 'ready', return_value=True) as ready:
            self.assertEqual(local_model.status()['location'], 'this_mac')
            ready.assert_called_once()
        k2_connection.save({'location': 'other_pc', 'host': '192.168.1.5', 'port': 8888})
        with patch.object(local_model, '_request') as request, patch.object(local_model.resource_budget, 'state') as budget:
            result = local_model.status()
        self.assertFalse(result['ready'])
        self.assertEqual(result['resources'], {})
        request.assert_not_called()
        budget.assert_not_called()

    def test_only_rfc1918_ipv4_peer_and_port_are_accepted(self):
        for host in ('example.com', '127.0.0.1', '169.254.1.2', '8.8.8.8', '::1',
                     '192.168.1.1/path', '192.168.1.1@evil.com'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                k2_connection.save({'location': 'other_pc', 'host': host, 'port': 8888})
        for port in (0, 65536, True, '8888'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                k2_connection.save({'location': 'other_pc', 'host': '10.0.0.5', 'port': port})
        self.assertEqual(k2_connection.save({'location': 'other_pc', 'host': '172.16.1.1', 'port': 8888})['host'], '172.16.1.1')
        self.assertEqual(k2_connection.CONFIG_PATH.stat().st_mode & 0o777, 0o600)

    def test_api_save_does_not_enable_or_connect_and_test_is_explicit(self):
        body = {'location': 'other_pc', 'host': '10.3.4.5', 'port': 8080}
        with patch.object(server.get_application().local_services, 'schedule') as schedule, patch.object(local_model, '_request') as request:
            self.assertEqual(server.api_k2_connection(None, {}, body), dict(body, has_api_key=False))
            schedule.assert_not_called()
            request.assert_not_called()
        self.assertEqual(server.api_k2_connection(None, {}, {}), dict(body, has_api_key=False))
        with patch.object(local_model, 'test_connection', return_value={'ok': False, 'message': 'offline'}) as probe:
            self.assertFalse(server.api_k2_connection_test(None, {}, {})['ok'])
            probe.assert_called_once()
        with self.assertRaises(server.Bad):
            server.api_k2_connection_test(None, {}, body)

    def test_probe_verifies_exact_model_and_required_helpers_without_inference(self):
        k2_connection.save({'location': 'other_pc', 'host': '10.2.3.4', 'port': 8888})
        results = [{'data': [{'id': local_model.MODEL}]}, {'prompt': 'test'}, {'tokens': [1, 2]}]
        with patch.object(local_model, '_request', side_effect=results) as request:
            self.assertTrue(local_model.test_connection()['ok'])
        self.assertEqual([call.args[0] for call in request.call_args_list], ['/v1/models', '/apply-template', '/tokenize'])
        self.assertTrue(local_model.status()['ready'])
        with patch.object(local_model, '_request', side_effect=[{'data': [{'id': 'wrong'}]}]) as request:
            self.assertFalse(local_model.test_connection()['ok'])
            self.assertEqual(request.call_count, 1)

    def test_saved_key_is_private_and_transport_uses_only_private_endpoint(self):
        public = k2_connection.save({'location': 'other_pc', 'host': '10.2.3.4',
                                     'port': 8888, 'api_key': 'sk-unsloth-secret'})
        self.assertNotIn('api_key', public)
        self.assertNotIn('api_key', k2_connection.read())
        readback = server.api_k2_connection(None, {}, {})
        self.assertTrue(readback['has_api_key'])
        self.assertNotIn('api_key', readback)
        self.assertEqual(k2_connection.read_secret()['api_key'], 'sk-unsloth-secret')
        opener = unittest.mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"data": []}'
        with patch.object(local_model.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(local_model._request('/v1/models'), {'data': []})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://10.2.3.4:8888/v1/models')
        self.assertEqual(request.get_header('Authorization'), 'Bearer sk-unsloth-secret')
        self.assertTrue(any(isinstance(handler, local_model.NoRedirect) for handler in build.call_args.args))
        k2_connection.save({'location': 'other_pc', 'host': '10.2.3.5', 'port': 8888})
        self.assertEqual(k2_connection.read_secret()['api_key'], '')

    def test_redirect_is_rejected_and_remote_no_local_service_stop(self):
        k2_connection.save({'location': 'other_pc', 'host': '10.2.3.4', 'port': 8888})
        with self.assertRaises(local_model.Unavailable):
            local_model.NoRedirect().redirect_request(None, None, None, None, None, None)
        with patch.object(local_model.subprocess, 'run') as run:
            self.assertFalse(local_model.maintain_service()['stopped'])
            run.assert_not_called()

    def test_connection_change_rejected_during_work_or_unknown_activity(self):
        body = {'location': 'other_pc', 'host': '10.2.3.4', 'port': 8888}
        with patch.object(server.engine_controls, 'has_active', return_value=True):
            with self.assertRaises(server.Bad):
                server.api_k2_connection(None, {}, body)
        local_model._activity_unknown = True
        with patch.object(server.engine_controls, 'has_active', return_value=False):
            with self.assertRaises(server.Bad):
                server.api_k2_connection(None, {}, body)
        self.assertEqual(k2_connection.read(), k2_connection.DEFAULT)

    def test_uncertain_activity_only_clears_on_proven_idle_slots(self):
        k2_connection.save({'location': 'other_pc', 'host': '10.2.3.4', 'port': 8888})
        local_model._activity_unknown = True
        with patch.object(local_model, '_request', return_value=[{'is_processing': True}]):
            self.assertTrue(local_model.status()['activity_unknown'])
        with patch.object(local_model, '_request', return_value=[{'is_processing': False}]):
            self.assertFalse(local_model.status()['activity_unknown'])

    def test_unknown_remote_request_blocks_next_inference(self):
        k2_connection.save({'location': 'other_pc', 'host': '10.2.3.4', 'port': 8888})
        local_model._activity_unknown = True
        with patch.object(local_model, '_request', return_value=[{'is_processing': True}]) as request:
            with self.assertRaises(local_model.Busy):
                local_model.complete_json('system', 'user', {'type': 'object'})
        self.assertEqual([call.args[0] for call in request.call_args_list], ['/slots'])


if __name__ == '__main__':
    unittest.main()
