"""Offline checks for the checkout-owned Tor manager; never launches Tor."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).with_name('tor-status.py')
spec = importlib.util.spec_from_file_location('tor_runtime', SCRIPT)
tor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tor)


class TorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tor runtime spaces ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'managed tor'
        self.home.mkdir()
        self.old_home, self.old_port = tor.HOME, tor.PORT
        tor.HOME, tor.PORT = self.home, 9152
        self.addCleanup(self.restore_globals)
        self.process_scan = tor.unmanaged_processes
        scan = patch.object(tor, 'unmanaged_processes', return_value=[])
        scan.start()
        self.addCleanup(scan.stop)

    def restore_globals(self):
        tor.HOME, tor.PORT = self.old_home, self.old_port

    def test_owned_process_with_spaces_and_exact_command(self):
        binary = Path(self.temp.name) / 'tor binary'
        binary.touch()
        record = {'pid': 12345, 'started': 'Fri Sep 26 02:00:00 2026', 'binary': str(binary.resolve())}
        (self.home / 'process.json').write_text(json.dumps(record))
        (self.home / 'torrc').write_text(tor.torrc())
        command = f'{binary.resolve()} -f {self.home / "torrc"}'
        with patch.object(tor, 'ps', side_effect=lambda pid, field: record['started'] if field == 'lstart' else command):
            self.assertEqual(tor.owned(), record)
        with patch.object(tor, 'ps', side_effect=lambda pid, field: record['started'] if field == 'lstart' else command.replace('torrc', 'other-torrc')):
            self.assertIsNone(tor.owned())

    def test_live_stale_record_refuses_start_before_changing_endpoints(self):
        (self.home / 'process.json').write_text(json.dumps({'pid': os.getpid(), 'started': 'wrong', 'binary': '/wrong'}))
        cookie = self.home / 'control.authcookie'
        cookie.write_bytes(b'keep this')
        with patch.object(tor, 'ps', return_value='alive'), patch.object(tor, 'find_tor') as find_binary:
            with self.assertRaisesRegex(RuntimeError, 'still alive'):
                tor.start(0)
        find_binary.assert_not_called()
        self.assertEqual(cookie.read_bytes(), b'keep this')
        self.assertFalse((self.home / 'torrc').exists())

    def test_repeated_start_preserves_control_files(self):
        record = {'pid': 12345, 'started': 'same', 'binary': '/tor'}
        cookie = self.home / 'control.authcookie'
        cookie.write_bytes(b'keep this')
        with patch.object(tor, 'owned', return_value=record), patch.object(tor, 'status', return_value={'ready': True, 'owned': True}):
            self.assertEqual(tor.start(0), 0)
        self.assertEqual(cookie.read_bytes(), b'keep this')
        self.assertFalse((self.home / 'torrc').exists())

    def test_changed_config_invalidates_process_ownership(self):
        record = {'pid': 12345, 'started': 'same', 'binary': '/tor'}
        (self.home / 'process.json').write_text(json.dumps(record))
        (self.home / 'torrc').write_text('SocksPort 127.0.0.1:9999\n')
        with patch.object(tor, 'ps', side_effect=lambda pid, field: 'same' if field == 'lstart' else f'/tor -f {self.home / "torrc"}'):
            self.assertIsNone(tor.owned())
            with self.assertRaisesRegex(RuntimeError, 'still alive'):
                tor.start(0)

    def test_stale_invalid_config_is_preserved(self):
        (self.home / 'torrc').write_text('wrong config\n')
        (self.home / 'process.json').write_text(json.dumps({'pid': 12345, 'started': 'old', 'binary': '/tor'}))
        with patch.object(tor, 'ps', return_value=''), patch.object(tor, 'find_tor', return_value='/tor'), patch.object(tor.socket, 'create_connection', side_effect=ConnectionRefusedError):
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                tor.start(0)
        self.assertEqual((self.home / 'torrc').read_text(), 'wrong config\n')
        self.assertFalse((self.home / 'state').exists())

    def test_occupied_port_refuses_start_without_changing_files(self):
        cookie = self.home / 'control.authcookie'
        cookie.write_bytes(b'keep')
        with patch.object(tor.socket, 'create_connection'):
            with self.assertRaisesRegex(RuntimeError, 'already used'):
                tor.start(0)
        self.assertEqual(cookie.read_bytes(), b'keep')
        self.assertFalse((self.home / 'torrc').exists())

    def test_symlinked_managed_files_are_rejected(self):
        target = Path(self.temp.name) / 'outside'
        target.write_text('keep')
        (self.home / 'launcher.log').symlink_to(target)
        with patch.object(tor, 'find_tor', return_value='/tor'), patch.object(tor.socket, 'create_connection', side_effect=ConnectionRefusedError):
            with self.assertRaisesRegex(RuntimeError, 'not a regular file'):
                tor.start(0)
        self.assertEqual(target.read_text(), 'keep')

    def test_mock_launch_writes_private_files_and_ready_status(self):
        child = Mock(pid=12345)
        child.poll.return_value = None
        with patch.object(tor.socket, 'create_connection', side_effect=ConnectionRefusedError), \
             patch.object(tor, 'find_tor', return_value='/mock/tor'), \
             patch.object(tor.subprocess, 'Popen', return_value=child), \
             patch.object(tor, 'ps', return_value='same'), \
             patch.object(tor, 'status', return_value={'ready': True, 'owned': True}):
            self.assertEqual(tor.start(0), 0)
        self.assertEqual(json.loads((self.home / 'process.json').read_text())['pid'], 12345)
        for name in ('torrc', 'process.json', 'launcher.log'):
            self.assertEqual((self.home / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / 'state').stat().st_mode & 0o777, 0o700)

    def test_reused_pid_is_never_signaled(self):
        record = {'pid': 12345, 'started': 'old', 'binary': '/tor'}
        (self.home / 'process.json').write_text(json.dumps(record))
        (self.home / 'torrc').write_text(tor.torrc())
        with patch.object(tor, 'ps', return_value='new'), patch.object(tor.os, 'kill') as kill:
            self.assertEqual(tor.stop(), 1)
        kill.assert_not_called()
        self.assertTrue((self.home / 'process.json').exists())

    def test_bootstrap_and_socks_handshake_required(self):
        record = {'pid': 12345, 'started': 'same', 'binary': '/tor'}
        with patch.object(tor, 'owned', return_value=record), patch.object(tor, 'control', side_effect=lambda command: '250-process/pid=12345\n250 OK' if command == 'GETINFO process/pid' else '250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100\n250 OK'):
            with patch.object(tor, 'socks_ready', return_value=False):
                self.assertFalse(tor.status()['ready'])
            with patch.object(tor, 'socks_ready', return_value=True):
                self.assertTrue(tor.status()['ready'])

    def test_invalid_and_privileged_ports_rejected_without_creating_home(self):
        cases = ('0', '80', '65536', '-1')
        for port in cases:
            home = self.home / port
            result = subprocess.run([sys.executable, str(SCRIPT), 'start', '--wait', '0'],
                                    env={**os.environ, 'FL_TOR_DIR': str(home), 'FL_TOR_PORT': port},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, port)
            self.assertFalse(home.exists(), port)

    def test_manager_directory_and_lock_are_private(self):
        (self.home / 'manager.lock').touch(mode=0o666)
        (self.home / 'manager.lock').chmod(0o666)
        old_umask = os.umask(0o022)
        try:
            with patch.object(sys, 'argv', [str(SCRIPT), 'start', '--wait', '0']), patch.object(tor, 'start', return_value=0):
                self.assertEqual(tor.main(), 0)
            self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.home / 'manager.lock').stat().st_mode & 0o777, 0o600)
        finally:
            os.umask(old_umask)

    def test_lost_record_blocks_duplicate_before_any_file_changes(self):
        cookie = self.home / 'control.authcookie'
        cookie.write_bytes(b'keep')
        with patch.object(tor, 'unmanaged_processes', return_value=[12345]), patch.object(tor.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'already has a live process'):
                tor.start(0)
            self.assertEqual(tor.status()['state'], 'conflict')
        launch.assert_not_called()
        self.assertEqual(cookie.read_bytes(), b'keep')

    def test_process_scan_matches_complete_config_with_spaces(self):
        output = f'  123 /tor -f {self.home / "torrc"}\n456 /tor -f /other/torrc\n'
        with patch.object(tor.subprocess, 'run', return_value=Mock(returncode=0, stdout=output)):
            self.assertEqual(self.process_scan(), [123])

    def test_unavailable_process_scan_does_not_claim_stopped(self):
        with patch.object(tor, 'unmanaged_processes', side_effect=RuntimeError('ps failed')):
            self.assertEqual(tor.status()['state'], 'unknown')
            with self.assertRaises(RuntimeError):
                tor.start(0)

    def test_control_endpoint_must_belong_to_owned_pid(self):
        record = {'pid': 12345, 'started': 'same', 'binary': '/tor'}
        with patch.object(tor, 'owned', return_value=record), patch.object(tor, 'control', return_value='250-process/pid=999\n250 OK'), patch.object(tor, 'socks_ready') as handshake:
            self.assertEqual(tor.status()['state'], 'conflict')
        handshake.assert_not_called()

    def test_restart_cooldown_is_persisted_and_does_not_touch_endpoints(self):
        path = self.home / 'last-launch.json'
        path.write_text(json.dumps({'time': 1000}))
        with patch.object(tor.time, 'time', return_value=1030), patch.object(tor.socket, 'create_connection', side_effect=ConnectionRefusedError), patch.object(tor.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'restart cooldown active'):
                tor.start(0)
        launch.assert_not_called()
        with patch.object(tor.time, 'time', return_value=1061):
            tor.restart_guard()

    def test_invalid_cooldown_record_fails_closed(self):
        for value in [float('nan'), float('inf'), True, 'wrong']:
            (self.home / 'last-launch.json').write_text(json.dumps({'time': value}))
            with self.assertRaisesRegex(RuntimeError, 'Invalid launch timestamp'):
                tor.restart_guard()

    def test_unrecorded_child_is_reaped_after_ownership_probe_fails(self):
        child = Mock(pid=12345)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('mock', 5), 0]
        with patch.object(tor, 'unmanaged_processes', return_value=[]), patch.object(tor.socket, 'create_connection', side_effect=ConnectionRefusedError), patch.object(tor, 'find_tor', return_value='/mock/tor'), patch.object(tor.subprocess, 'Popen', return_value=child), patch.object(tor, 'ps', side_effect=OSError('ps failed')):
            with self.assertRaises(OSError):
                tor.start(0)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)
        self.assertFalse((self.home / 'process.json').exists())

    def test_symlink_home_is_rejected_before_chmod(self):
        target = Path(self.temp.name) / 'outside'
        target.mkdir(mode=0o755)
        link = Path(self.temp.name) / 'link'
        link.symlink_to(target, target_is_directory=True)
        tor.HOME = link
        with patch.object(sys, 'argv', [str(SCRIPT), 'start', '--wait', '0']):
            with self.assertRaisesRegex(RuntimeError, 'dedicated directory'):
                tor.main()
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_control_characters_in_home_are_rejected(self):
        tor.HOME = self.home / 'bad\nSocksPort 0'
        with self.assertRaisesRegex(RuntimeError, 'control characters'):
            tor.validate_home()



if __name__ == '__main__':
    unittest.main()
