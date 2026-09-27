import sys
import sqlite3
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine_start
import server


class FakeConn:
    @staticmethod
    def execute(_query, *_args):
        return SimpleNamespace(fetchone=lambda: None) if 'settings' in _query else FakeConn()

    @staticmethod
    def fetchone():
        return (3,)


class EngineStartTest(unittest.TestCase):
    def test_ui_endpoint_uses_the_local_engine_launcher(self):
        with patch.object(engine_start, 'start', return_value={'ok': True, 'local_services_started': True, 'profiles_opened': 0}) as start:
            self.assertEqual(server.api_engine_start(FakeConn(), {}, {}), {'ok': True, 'local_services_started': True, 'profiles_opened': 0})
            start.assert_called_once_with(server.ROOT, 3)

    def test_ui_endpoint_returns_a_clear_startup_error(self):
        with patch.object(engine_start, 'start', side_effect=engine_start.EngineStartError('Chrome profile missing')):
            with self.assertRaisesRegex(server.Bad, 'Chrome profile missing'):
                server.api_engine_start(FakeConn(), {}, {})

    def test_start_runs_only_the_owned_checkout_script_in_app_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            ops = repo / 'ops'
            ops.mkdir()
            (ops / 'startup_profiles.json').write_text(
                '{"chrome_profiles":["Michael","BOT","BOT2"]}', encoding='utf-8')
            launcher = ops / 'start-all.command'
            launcher.write_text('#!/bin/bash\n', encoding='utf-8')
            launcher.chmod(0o700)
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout='ENGINE_STARTED:0\n', stderr='')

            self.assertEqual(engine_start.start(repo, 3, run), {'ok': True, 'local_services_started': True, 'profiles_opened': 0})
            self.assertEqual(calls[0][0], [str(launcher.resolve()), '--from-app'])
            self.assertEqual(calls[0][1]['cwd'], str(repo.resolve()))
            self.assertEqual(calls[0][1]['timeout'], 210)

    def test_missing_confirmation_and_launcher_failure_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            ops = repo / 'ops'
            ops.mkdir()
            (ops / 'startup_profiles.json').write_text(
                '{"chrome_profiles":["Michael","BOT","BOT2"]}', encoding='utf-8')
            launcher = ops / 'start-all.command'
            launcher.write_text('#!/bin/bash\n', encoding='utf-8')
            launcher.chmod(0o700)
            run = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='Started\n', stderr='')
            with self.assertRaisesRegex(engine_start.EngineStartError, 'without confirming'):
                engine_start.start(repo, 3, run)
            run = lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout='', stderr='Laya is unavailable\n')
            with self.assertRaisesRegex(engine_start.EngineStartError, 'Laya is unavailable'):
                engine_start.start(repo, 3, run)

    def test_local_startup_never_opens_instagram_or_resumes_stages(self):
        script = (Path(__file__).resolve().parents[2] / 'ops/start-all.command').read_text()
        self.assertNotIn('instagram.com', script)
        self.assertNotIn('Google Chrome', script)
        self.assertNotIn('/api/control', script)
        self.assertNotIn('start_all', script)
        self.assertIn('ENGINE_STARTED:0', script)

    def test_local_startup_does_not_require_matching_account_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'ops').mkdir()
            launcher = repo / 'ops/start-all.command'
            launcher.write_text('#!/bin/bash\n')
            launcher.chmod(0o700)
            run = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='ENGINE_STARTED:0\n', stderr='')
            self.assertTrue(engine_start.start(repo, 22, run)['local_services_started'])


if __name__ == '__main__':
    unittest.main()

class EngineReconcileTest(unittest.TestCase):
    def setUp(self):
        import processing_modes
        self.modes = processing_modes
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.modes.set_mode(self.conn, 'RLAI')
        self.conn.commit()
        self.addCleanup(self.conn.close)
        import local_model
        remote = patch.object(local_model, 'is_remote', return_value=False)
        self.remote = remote.start()
        self.addCleanup(remote.stop)
        import resource_budget
        from contextlib import nullcontext
        budget = patch.object(resource_budget, 'lease', side_effect=lambda *a, **kw: nullcontext())
        budget.start()
        self.addCleanup(budget.stop)

    def test_independent_start_stop_and_remote_k2_never_starts_local(self):
        self.modes.set_engine(self.conn, 'k2', False)
        run = unittest.mock.Mock(return_value=SimpleNamespace(returncode=0, stderr=''))
        engine_start.reconcile_models('/test/repo', self.conn, run)
        self.assertEqual([c.args[0][2] for c in run.call_args_list], ['start', 'stop'])
        run.reset_mock()
        self.modes.set_engine(self.conn, 'k2', True)
        self.modes.set_engine(self.conn, 'laya', False)
        engine_start.reconcile_models('/test/repo', self.conn, run)
        self.assertEqual([c.args[0][2] for c in run.call_args_list], ['stop', 'start'])
        run.reset_mock()
        self.remote.return_value = True
        engine_start.reconcile_models('/test/repo', self.conn, run)
        self.assertEqual([c.args[0][2] for c in run.call_args_list], ['stop', 'stop'])

    def test_pause_during_slow_start_reconciles_back_to_stopped(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[2] == 'start':
                self.modes.set_paused(self.conn, True)
            return SimpleNamespace(returncode=0, stderr='')
        engine_start.reconcile_models('/test/repo', self.conn, run)
        self.assertEqual([c[2] for c in commands], ['start', 'stop', 'stop'])

    def test_one_failed_engine_does_not_prevent_other_engine_stop(self):
        self.modes.set_engine(self.conn, 'k2', False)
        run = unittest.mock.Mock(side_effect=[SimpleNamespace(returncode=1, stderr='cannot start'),
                                             SimpleNamespace(returncode=0, stderr='')])
        with self.assertRaisesRegex(engine_start.EngineStartError, 'LAYA: cannot start'):
            engine_start.reconcile_models('/test/repo', self.conn, run)
        self.assertEqual(run.call_args_list[-1].args[0][2], 'stop')
