import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('k2_service', Path(__file__).resolve().parents[2] / 'ops/k2-service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class K2ServiceTest(unittest.TestCase):
    def test_install_cannot_accept_a_different_model(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / service.WEIGHT).write_bytes(b'wrong weights')
            with self.assertRaisesRegex(RuntimeError, 'verified model'):
                service.install(source)

    def test_manifest_verifies_files_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'ROOT', Path(directory)):
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / service.WEIGHT).write_bytes(b'model')
            (root / 'bin/llama-server').write_bytes(b'binary')
            digest = hashlib.sha256(b'model').hexdigest()
            manifest = {'model_sha256': digest, 'runtime_revision': service.REVISION,
                'hashes': {service.WEIGHT: digest, 'bin/llama-server': hashlib.sha256(b'binary').hexdigest()}}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            with patch.object(service.local_model, 'MODEL_DIGEST', digest):
                service.verify()
                (root / 'bin/llama-server').write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                    service.verify()

    def test_never_kills_a_reused_pid_or_other_installation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'ROOT', Path(directory)):
            (Path(directory) / 'pid').write_text('123')
            with patch.object(service.subprocess, 'check_output', return_value='/elsewhere/llama-server --port 11436'), patch.object(service.os, 'kill') as kill:
                service.stop()
                kill.assert_not_called()

    def test_correct_process_ownership_is_required(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'ROOT', Path(directory)):
            (Path(directory) / 'pid').write_text('123')
            command = str(Path(directory) / 'bin/llama-server') + ' -m weights --port 11436 --ctx-size 4096'
            with patch.object(service.subprocess, 'check_output', return_value=command):
                self.assertEqual(service.owned_pid(), 123)

    def test_no_context_shift_or_external_host_in_start_configuration(self):
        source = Path(service.__file__).read_text()
        self.assertIn("'--host', '127.0.0.1'", source)
        self.assertIn("'--ctx-size', '4096', '--parallel', '1'", source)
        self.assertIn("'--no-context-shift'", source)


if __name__ == '__main__':
    unittest.main()
