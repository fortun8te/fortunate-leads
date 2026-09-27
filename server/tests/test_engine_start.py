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
        with patch.object(engine_start, 'start', return_value={'ok': True, 'profiles_opened': 3}) as start:
            self.assertEqual(server.api_engine_start(FakeConn(), {}, {}), {'ok': True, 'profiles_opened': 3})
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
                return SimpleNamespace(returncode=0, stdout='ENGINE_STARTED:3\n', stderr='')

            self.assertEqual(engine_start.start(repo, 3, run), {'ok': True, 'profiles_opened': 3})
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

    def test_direct_launcher_hold_check_fails_closed_without_opening_chrome(self):
        script = (Path(__file__).resolve().parents[2] / 'ops/start-all.command').read_text()
        function = script[script.index('check_collection_hold() {'):script.index('check_collection_hold allow-missing || exit 1')]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite'
            with sqlite3.connect(database) as conn:
                conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
                conn.execute('INSERT INTO settings VALUES(?,?)', ('cooldown', json.dumps('2099-01-01T00:00:00Z')))
            command = function + '\ncheck_collection_hold'
            env = dict(__import__('os').environ, FL_DB=str(database), FL_PYTHON=sys.executable)
            blocked = subprocess.run(['bash', '-c', command], env=env, capture_output=True, text=True)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn('shared safety hold', blocked.stderr)
            with sqlite3.connect(database) as conn:
                conn.execute("UPDATE settings SET value=?", (json.dumps('2000-01-01T00:00:00Z'),))
            self.assertEqual(subprocess.run(['bash', '-c', command], env=env, capture_output=True).returncode, 0)
            env['FL_DB'] = str(Path(directory) / 'missing.sqlite')
            self.assertNotEqual(subprocess.run(['bash', '-c', command], env=env, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(['bash', '-c', function + '\ncheck_collection_hold allow-missing'], env=env, capture_output=True).returncode, 0)
        self.assertIn('check_collection_hold || exit 1\n  open', script)

    def test_app_account_count_must_match_configured_profile_count(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            ops = repo / 'ops'
            ops.mkdir()
            (ops / 'startup_profiles.json').write_text(
                '{"chrome_profiles":["Michael","BOT","BOT2"]}', encoding='utf-8')
            with self.assertRaisesRegex(engine_start.EngineStartError, 'app has 22 accounts'):
                engine_start.start(repo, 22, run=lambda *args, **kwargs: self.fail('must fail before launch'))


if __name__ == '__main__':
    unittest.main()
