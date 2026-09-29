"""One-command installer: dry run changes nothing, flags are validated, start-all falls back to it."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

OPS = Path(__file__).resolve().parent


def run(script, *args, home=None):
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=home or tmp, PYTHON=sys.executable)
        return subprocess.run(['bash', str(OPS / script), *args], env=env, text=True, capture_output=True, timeout=30), tmp


@unittest.skipUnless(sys.platform == 'darwin', 'the installer is macOS only')
class InstallTests(unittest.TestCase):
    def test_dry_run_lists_every_step_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            result, _ = run('install.sh', '--dry-run', home=home)
            self.assertEqual(result.returncode, 0, result.stderr)
            for text in ('[1/4]', '[2/4]', '[3/4]', '[4/4]', 'would run: ops/install-launchagent.sh --with-backup',
                         'would run: ops/doctor.sh --quick', 'would open: http://127.0.0.1:8777/'):
                self.assertIn(text, result.stdout)
            self.assertEqual(list(Path(home).iterdir()), [])   # no LaunchAgents, logs or data written

    def test_unknown_option_is_rejected_and_help_prints(self):
        result, _ = run('install.sh', '--nope')
        self.assertEqual(result.returncode, 2)
        result, _ = run('install.sh', '--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('ops/install.sh', result.stdout)

    def test_installer_is_idempotent_by_delegation(self):
        text = (OPS / 'install.sh').read_text()
        self.assertIn('install-launchagent.sh" --with-backup', text)   # unchanged agents are left alone there
        self.assertIn('doctor.sh" --quick', text)
        self.assertNotIn('rm -', text)
        self.assertNotIn('kill', text)

    def test_start_all_installs_on_first_run_and_keeps_its_safety_contract(self):
        text = (OPS / 'start-all.command').read_text()
        self.assertIn('exec "$FL_OPS/install.sh"', text)
        self.assertIn('ENGINE_STARTED:0', text)
        for forbidden in ('instagram.com', 'Google Chrome', '/api/control', 'start_all'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
