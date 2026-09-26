"""Service control tests. Nothing is installed or launched on the host."""
import io
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import laya_service as service  # noqa: E402


class Response:
    def __init__(self, body):
        self.body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        return self.body.read(size)


class ServiceTest(unittest.TestCase):
    def test_preflight_requires_cached_checkpoint_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / '.venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.touch()
            snapshot = root / 'cache' / 'hub' / 'models--convaiinnovations--laya' / 'snapshots' / 'rev' / 'multilingual'
            needed = ('rl_agent_config.json', 'model.safetensors', 'tokenizer/tokenizer.json', 'encoder/config.json')
            for name in needed:
                target = snapshot / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            cfg = {'cache': root / 'cache', 'model': 'convaiinnovations/laya:multilingual'}
            with patch.object(service, 'HERE', root), patch.object(service.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0, stdout='0.3.20\n')):
                service.preflight(cfg)
                (snapshot / 'model.safetensors').unlink()
                with self.assertRaisesRegex(RuntimeError, 'Cached checkpoint files'):
                    service.preflight(cfg)

    def test_plist_is_offline_and_matches_client(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
        import laya
        spec = service.service_spec()
        env = spec['EnvironmentVariables']
        self.assertEqual(env['LAYA_MODEL'], laya.MODEL)
        self.assertEqual(env['LAYA_DEPLOYMENT_VERSION'], laya.DEPLOYMENT_VERSION)
        self.assertEqual(env['LAYA_PORT'], str(laya.PORT))
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        self.assertEqual(spec['ProgramArguments'][1], str(service.HERE / 'laya_server.py'))
        self.assertEqual(spec['ProgramArguments'][3], '18742')
        self.assertNotIn('--allow-download', spec['ProgramArguments'])

    def test_explicit_cpu_device_is_saved_and_verified(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(service.os.environ, {'LAYA_DEVICE': 'cpu'}), \
                patch.object(service, 'PLIST', Path(directory) / 'laya.plist'), \
                patch.object(service, 'LOG', Path(directory) / 'laya.log'), patch.object(service, 'preflight'):
            self.assertEqual(service.install(), 'installed (not started)')
            spec = plistlib.loads(service.PLIST.read_bytes())
            self.assertEqual(spec['EnvironmentVariables']['LAYA_DEVICE'], 'cpu')
            cfg = service.installed_config()
            self.assertEqual(cfg['device'], 'cpu')
            healthy = {'ok': True, 'model': cfg['model'], 'deployment_version': cfg['deployment_version']}
            self.assertTrue(service.matches(cfg, dict(healthy, device='cpu')))
            self.assertFalse(service.matches(cfg, dict(healthy, device='mps')))

    def test_install_writes_plist_without_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(service, 'PLIST', path / 'laya.plist'), \
                    patch.object(service, 'LOG', path / 'logs' / 'laya.log'), \
                    patch.object(service, 'preflight'), patch.object(service, 'launch') as launch:
                self.assertEqual(service.install(), 'installed (not started)')
                spec = plistlib.loads(service.PLIST.read_bytes())
                self.assertEqual(spec['Label'], service.LABEL)
                self.assertEqual(service.installed_config()['model'], service.laya_server.DEFAULT_MODEL)
                self.assertEqual(service.install(), 'already installed')
                launch.assert_not_called()

                spec['EnvironmentVariables']['HF_HUB_OFFLINE'] = '0'
                service.PLIST.write_bytes(plistlib.dumps(spec))
                with self.assertRaisesRegex(RuntimeError, 'offline service'):
                    service.installed_config()

    def test_legacy_plist_can_be_stopped_then_replaced(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(service, 'PLIST', Path(directory) / 'laya.plist'), \
                patch.object(service, 'LOG', Path(directory) / 'laya.log'), patch.object(service, 'preflight'), \
                patch.object(service, 'loaded', side_effect=[True, False]), \
                patch.object(service, 'launch') as launch:
            legacy = {'Label': service.LABEL, 'ProgramArguments': [
                str(service.HERE / '.venv' / 'bin' / 'python'), str(service.HERE / 'laya_server.py')]}
            service.PLIST.write_bytes(plistlib.dumps(legacy))
            self.assertEqual(service.stop(), 'stopped')
            launch.assert_called_once_with('bootout', service.target())
            self.assertEqual(service.install(replace=True), 'installed (not started)')
            self.assertEqual(service.installed_config()['model'], service.laya_server.DEFAULT_MODEL)

    def test_unrelated_plist_cannot_be_stopped_or_replaced(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(service, 'PLIST', Path(directory) / 'laya.plist'), \
                patch.object(service, 'LOG', Path(directory) / 'laya.log'), patch.object(service, 'preflight'), \
                patch.object(service, 'loaded', return_value=True), patch.object(service, 'launch') as launch:
            unrelated = {'Label': service.LABEL, 'ProgramArguments': ['/other/python', '/other/laya_server.py']}
            original = plistlib.dumps(unrelated)
            service.PLIST.write_bytes(original)
            with self.assertRaises(RuntimeError):
                service.stop()
            with self.assertRaises(RuntimeError):
                service.install(replace=True)
            self.assertEqual(service.PLIST.read_bytes(), original)
            launch.assert_not_called()

    def test_health_requires_matching_model_and_deployment(self):
        cfg = {'port': 18742, 'model': 'repo:multilingual', 'deployment_version': 'v1'}
        response = Response(b'{"ok":true,"model":"repo:multilingual","deployment_version":"v1"}')
        with patch.object(service.urllib.request.OpenerDirector, 'open', return_value=response):
            self.assertTrue(service.probe(cfg))
        response = Response(b'{"ok":true,"model":"other","deployment_version":"v1"}')
        with patch.object(service.urllib.request.OpenerDirector, 'open', return_value=response):
            self.assertFalse(service.probe(cfg))
        response = Response(b'{"ok":true,"model":"repo:multilingual","deployment_version":"old"}')
        with patch.object(service.urllib.request.OpenerDirector, 'open', return_value=response):
            self.assertFalse(service.probe(cfg))

    def test_start_uses_launchctl_then_waits_for_health(self):
        cfg = {'port': 18742, 'model': 'repo:multilingual', 'deployment_version': 'v1', 'cache': Path('/tmp')}
        with patch.object(service, 'installed_config', return_value=cfg), patch.object(service, 'preflight'), \
                patch.object(service, 'loaded', return_value=False), \
                patch.object(service, 'health_response', return_value=None), \
                patch.object(service, 'probe', return_value=True), \
                patch.object(service, 'launch') as launch:
            self.assertIn('healthy', service.start(timeout=1))
            launch.assert_called_once_with('bootstrap', 'gui/%d' % service.os.getuid(), str(service.PLIST))

    def test_start_rejects_wrong_model_without_launching(self):
        cfg = {'port': 18742, 'model': 'repo:multilingual', 'deployment_version': 'v1', 'cache': Path('/tmp')}
        with patch.object(service, 'installed_config', return_value=cfg), patch.object(service, 'preflight'), \
                patch.object(service, 'loaded', return_value=False), \
                patch.object(service, 'health_response', return_value={'ok': True, 'model': 'wrong',
                                                                       'deployment_version': 'v1'}), \
                patch.object(service, 'launch') as launch:
            with self.assertRaisesRegex(RuntimeError, 'different model'):
                service.start(timeout=1)
            launch.assert_not_called()

    def test_start_rejects_wrong_device_without_launching(self):
        cfg = {'port': 18742, 'model': 'repo:multilingual', 'deployment_version': 'v1',
               'cache': Path('/tmp'), 'device': 'cpu'}
        status = {'ok': True, 'model': 'repo:multilingual', 'deployment_version': 'v1', 'device': 'mps'}
        with patch.object(service, 'installed_config', return_value=cfg), patch.object(service, 'preflight'), \
                patch.object(service, 'loaded', return_value=False), \
                patch.object(service, 'health_response', return_value=status), \
                patch.object(service, 'launch') as launch:
            with self.assertRaisesRegex(RuntimeError, 'device'):
                service.start(timeout=1)
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
