"""Offline release checks. All fixtures stay in temporary directories."""
import os
import shutil
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

OPS = Path(__file__).resolve().parent


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.agents = self.root / 'agents'
        self.agents.mkdir()

    def shell(self, body):
        return subprocess.run(
            ['bash', '-eu', '-c',
             'source "$1"; unset PYTHON; FL_REPO="$2"; FL_AGENTS="$3"; ' + body,
             'test', str(OPS / 'lib.sh'), str(self.root), str(self.agents)],
            env=dict(os.environ, PYTHON=sys.executable), text=True, capture_output=True)

    def write(self, name, value='first\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def installed(self, python):
        (self.agents / 'com.fortunate.leads.plist').write_bytes(
            plistlib.dumps({'ProgramArguments': [python, str(self.root / 'server/server.py')]}))

    def bundled(self):
        path = self.root / 'data/scraper-venv/bin/python'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(sys.executable)
        return path

    def fingerprint(self):
        result = self.shell('source_fingerprint')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_runtime_and_dependency_changes_force_new_fingerprint(self):
        files = ['server/server.py', 'scraper/workspace_worker.py', 'scraper/app/igscraper/net.py',
                 'scraper/requirements.txt', 'data/scraper-venv/pyvenv.cfg',
                 'data/scraper-venv/lib/python3.12/site-packages/httpx-1.dist-info/METADATA',
                 'data/scraper-venv/lib/python3.12/site-packages/httpx-1.dist-info/RECORD']
        for name in files:
            self.write(name)
        previous = self.fingerprint()
        for name in files:
            with self.subTest(file=name):
                self.write(name, 'replacement\n')
                current = self.fingerprint()
                self.assertNotEqual(previous, current)
                previous = current
        (self.root / files[2]).unlink()
        self.assertNotEqual(previous, self.fingerprint())

    def test_private_state_and_tests_do_not_trigger_restart(self):
        self.write('server/server.py')
        previous = self.fingerprint()
        for name in ['data/leads.sqlite', 'scraper/egresses.yaml',
                     'server/tests/test_sample.py', 'scraper/app/igscraper/__pycache__/net.pyc']:
            self.write(name)
        self.assertEqual(previous, self.fingerprint())

    def test_existing_interpreter_wins_over_bundled(self):
        self.bundled()
        self.installed(sys.executable)
        result = self.shell('select_python')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), sys.executable)

    def test_new_install_prefers_bundled_interpreter(self):
        expected = self.bundled()
        result = self.shell('select_python')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(expected))

    def test_explicit_override_wins(self):
        self.installed('/missing/python')
        result = self.shell('PYTHON=/explicit/python; select_python')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '/explicit/python')

    def test_broken_existing_interpreter_fails_without_downgrade(self):
        self.bundled()
        self.installed('/missing/python')
        result = self.shell('FL_PYTHON="$(select_python)"; printf "%s\\n" "$FL_PYTHON"; python_ok')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '/missing/python')

    def test_malformed_existing_plist_fails_without_downgrade(self):
        self.bundled()
        (self.agents / 'com.fortunate.leads.plist').write_text('invalid')
        result = self.shell('select_python')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('set PYTHON explicitly', result.stderr)

    def test_agent_templates_use_private_umask(self):
        for name in ['com.fortunate.leads.plist', 'com.fortunate.leads.backup.plist']:
            with self.subTest(template=name):
                data = plistlib.loads((OPS / name).read_bytes())
                self.assertEqual(data['Umask'], 0o077)


    def fixture(self, foreign=False, loaded=True):
        repo = self.root / 'checkout'
        shutil.copytree(OPS, repo / 'ops', ignore=shutil.ignore_patterns('__pycache__'))
        (repo / 'server').mkdir()
        (repo / 'server/server.py').write_text('# mocked server\n')
        home = self.root / 'home'
        agents = home / 'Library/LaunchAgents'
        agents.mkdir(parents=True)
        config = {'Label': 'com.fortunate.leads', 'WorkingDirectory': str(repo),
                  'ProgramArguments': [sys.executable, str(repo / 'server/server.py')],
                  'PriorSetting': 'preserve me'}
        saved = agents / 'com.fortunate.leads.plist'
        saved.write_bytes(plistlib.dumps(config))
        original = saved.read_bytes()
        state = self.root / 'state.json'
        active = dict(config)
        if foreign:
            active['WorkingDirectory'] = '/another/checkout'
            active['ProgramArguments'][1] = '/another/checkout/server/server.py'
        state.write_text(json.dumps({active['Label']: active} if loaded else {}))
        binary = self.root / 'bin'
        binary.mkdir()
        launchctl = binary / 'launchctl'
        launchctl.write_text('#!' + sys.executable + '\n' + r"""
import json, os, pathlib, plistlib, sys
state = pathlib.Path(os.environ['MOCK_STATE'])
log = pathlib.Path(os.environ['MOCK_LOG'])
args = sys.argv[1:]
with log.open('a') as out: out.write(' '.join(args) + '\n')
services = json.loads(state.read_text())
active = services.get(args[1].split('/')[-1]) if len(args) > 1 else None
if args[0] == 'print':
    if active is None or args[1].split('/')[-1] != active['Label']: sys.exit(113)
    print('service = {\n working directory = ' + active['WorkingDirectory'])
    print(' arguments = {\n  ' + '\n  '.join(active['ProgramArguments']) + '\n }')
    print(' state = running\n pid = 424242\n}')
elif args[0] == 'bootout':
    services.pop(args[1].split('/')[-1], None)
    state.write_text(json.dumps(services))
elif args[0] == 'bootstrap':
    config = plistlib.loads(pathlib.Path(args[2]).read_bytes())
    if (os.environ.get('MOCK_FAIL') == 'bootstrap' or (os.environ.get('MOCK_FAIL') == 'backup' and config['Label'].endswith('.backup'))) and 'PriorSetting' not in config:
        print('mock bootstrap failure', file=sys.stderr); sys.exit(5)
    services[config['Label']] = config
    state.write_text(json.dumps(services))
""")
        launchctl.chmod(0o700)
        # Overrides are appended only to the disposable fixture. Every process
        # and network operation is mocked; launchctl itself uses realistic print.
        with (repo / 'ops/lib.sh').open('a') as out:
            out.write(r"""
is_macos() { return 0; }
port_pids() { return 0; }
agent_owns_port() { [ "${MOCK_FAIL:-}" != health ]; }
http_ok() { [ "${MOCK_FAIL:-}" != health ]; }
wait_http() { http_ok "$1"; }
plutil() { return 0; }
sleep() { return 0; }
kill() { echo 'UNEXPECTED KILL' >&2; return 99; }
""")
        env = dict(os.environ, HOME=str(home), PYTHON=sys.executable,
                   PATH=str(binary) + os.pathsep + os.environ['PATH'],
                   MOCK_STATE=str(state), MOCK_LOG=str(self.root / 'launchctl.log'))
        return repo, saved, original, state, env

    def run_fixture(self, repo, env, script='install-launchagent.sh', args=()):
        return subprocess.run(['bash', str(repo / 'ops' / script), *args],
                              env=env, text=True, capture_output=True, timeout=15)

    def test_bootstrap_and_health_failure_restore_prior_loaded_config(self):
        for failure in ('bootstrap', 'health'):
            with self.subTest(failure=failure):
                # Each subtest needs independent fixtures.
                with tempfile.TemporaryDirectory() as path:
                    old_root = self.root
                    self.root = Path(path).resolve()
                    try:
                        repo, saved, original, state, env = self.fixture()
                        env['MOCK_FAIL'] = failure
                        result = self.run_fixture(repo, env)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertEqual(saved.read_bytes(), original, result.stderr)
                        self.assertIn('configuration restored', result.stderr)
                        self.assertIn('bootstrap', Path(env['MOCK_LOG']).read_text())
                        self.assertEqual(json.loads(state.read_text())['com.fortunate.leads']['PriorSetting'], 'preserve me')
                        self.assertFalse((repo / 'data/.server-source.sha256').exists())
                    finally:
                        self.root = old_root

    def test_foreign_loaded_label_blocks_install_uninstall_and_doctor(self):
        repo, saved, original, state, env = self.fixture(foreign=True)
        before = state.read_text()
        for script, args in [('install-launchagent.sh', ()),
                             ('uninstall-launchagent.sh', ()), ('doctor.sh', ('--fix',))]:
            with self.subTest(script=script):
                result = self.run_fixture(repo, env, script, args)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(saved.read_bytes(), original)
                self.assertEqual(state.read_text(), before)
        log = Path(env['MOCK_LOG']).read_text()
        self.assertNotIn('bootout', log)
        self.assertNotIn('bootstrap', log)
        self.assertNotIn('kickstart', log)

    def test_failed_first_install_removes_new_plist(self):
        repo, saved, original, state, env = self.fixture(loaded=False)
        saved.unlink()
        env['MOCK_FAIL'] = 'health'
        result = self.run_fixture(repo, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(saved.exists(), result.stderr)
        self.assertEqual(json.loads(state.read_text()), {})

    def test_success_records_fingerprint_and_unchanged_install_does_not_reload(self):
        repo, saved, original, state, env = self.fixture()
        result = self.run_fixture(repo, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        fingerprint = repo / 'data/.server-source.sha256'
        self.assertEqual(fingerprint.stat().st_mode & 0o777, 0o600)
        Path(env['MOCK_LOG']).write_text('')
        result = self.run_fixture(repo, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('unchanged', result.stdout)
        self.assertNotIn('bootout', Path(env['MOCK_LOG']).read_text())


    def test_backup_bootstrap_failure_restores_both_prior_services(self):
        repo, saved, original, state, env = self.fixture()
        backup = {'Label': 'com.fortunate.leads.backup', 'WorkingDirectory': str(repo),
                  'ProgramArguments': ['/bin/bash', str(repo / 'ops/backup.sh')],
                  'PriorSetting': 'old backup'}
        backup_path = saved.with_name('com.fortunate.leads.backup.plist')
        backup_path.write_bytes(plistlib.dumps(backup))
        before_backup = backup_path.read_bytes()
        services = json.loads(state.read_text())
        services[backup['Label']] = backup
        state.write_text(json.dumps(services))
        env['MOCK_FAIL'] = 'backup'
        result = self.run_fixture(repo, env, args=('--with-backup',))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(saved.read_bytes(), original, result.stderr)
        self.assertEqual(backup_path.read_bytes(), before_backup, result.stderr)
        self.assertEqual(json.loads(state.read_text()), services)
        self.assertEqual(result.stderr.count('configuration restored'), 2)

    def test_process_matching_requires_python_in_command_position(self):
        body = r"""
ps() { printf '%s\n' "$(id -u)"; }
proc_cwd() { printf '%s\n' "$FL_REPO"; }
proc_cmd() { printf '%s\n' "$TEST_COMMAND"; }
TEST_COMMAND="$FL_PYTHON $FL_REPO/server/server.py"; owned_server_pid 42
TEST_COMMAND="$FL_PYTHON -u server/server.py"; owned_server_pid 42
TEST_COMMAND="echo $FL_REPO/server/server.py"; ! owned_server_pid 42
TEST_COMMAND="$FL_PYTHON other.py server/server.py"; ! owned_server_pid 42
TEST_COMMAND="$FL_PYTHON ${FL_REPO}suffix/server/server.py"; ! owned_server_pid 42
"""
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
