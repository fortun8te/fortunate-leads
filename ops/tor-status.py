#!/usr/bin/env python3
"""Manage only this checkout's Tor process. No Instagram requests or remote probes."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
_home = Path(os.environ.get('FL_TOR_DIR', str(ROOT / 'data' / 'tor'))).expanduser()
HOME = Path(os.path.abspath(_home if _home.is_absolute() else ROOT / _home))
RESTART_COOLDOWN = 60
try:
    PORT = int(os.environ.get('FL_TOR_PORT', '9152'))
except ValueError:
    PORT = -1  # main() reports the same concise error as for an out-of-range port.


def ps(pid, field):
    r = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', field + '='], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def owned():
    try:
        if (HOME / 'process.json').is_symlink() or (HOME / 'torrc').is_symlink():
            return None
        record = json.loads((HOME / 'process.json').read_text())
        pid = int(record['pid'])
        if pid <= 1 or ps(pid, 'lstart') != record['started']:
            return None
        # The recorded command alone is insufficient if the managed config was
        # replaced after launch (or FL_TOR_PORT changed between invocations).
        if (HOME / 'torrc').read_text() != torrc():
            return None
        # ps prints argv joined by spaces, without shell quoting. Compare the
        # complete command we launched so paths containing spaces still match.
        if ps(pid, 'command') != record['binary'] + ' -f ' + str(HOME / 'torrc'):
            return None
        return record
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def unmanaged_processes():
    """Detect lost records before touching endpoints, even before SOCKS binds."""
    result = subprocess.run(['ps', '-ww', '-axo', 'pid=,command='], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Could not check for an existing managed Tor process')
    suffix = ' -f ' + str(HOME / 'torrc')
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2
            and parts[0].isdigit() and parts[1].endswith(suffix)]


def unowned_status():
    result = {'state': 'stopped', 'ready': False, 'port': PORT, 'owned': False}
    record_path = HOME / 'process.json'
    try:
        regular_file(record_path)
        if record_path.exists():
            record = json.loads(record_path.read_text())
            pid = int(record['pid'])
            if pid > 1 and ps(pid, 'lstart'):
                result.update(state='conflict', detail='Recorded process is alive but ownership does not match')
        if unmanaged_processes():
            result.update(state='conflict', detail='Managed configuration has a live process without verified ownership')
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        result.update(state='unknown', detail='Process ownership could not be verified')
    return result


def validate_home():
    if HOME.is_symlink() or HOME in (Path('/'), Path.home(), ROOT, ROOT / 'data'):
        raise RuntimeError('FL_TOR_DIR must be a dedicated directory, not a symlink or shared parent')
    if any(ord(char) < 32 for char in str(HOME)):
        raise RuntimeError('FL_TOR_DIR must not contain control characters')


def restart_guard():
    path = HOME / 'last-launch.json'
    regular_file(path)
    if path.exists():
        try:
            last = json.loads(path.read_text())['time']
            if isinstance(last, bool) or not isinstance(last, (float, int)) or not 0 <= last < float('inf'):
                raise ValueError('Invalid timestamp')
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Invalid launch timestamp; inspect it before starting Tor') from exc
        remaining = RESTART_COOLDOWN - (time.time() - last)
        if remaining > 0:
            raise RuntimeError('Tor restart cooldown active; wait %d seconds. Scraper server cooldowns still apply.' % (remaining + 1))


def discard_child(child):
    """A failed launch must not leave an unrecorded process behind."""
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def control(command):
    cookie = (HOME / 'control.authcookie').read_bytes()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(HOME / 'control.sock'))
        stream = sock.makefile('rwb')
        def query(cmd):
            stream.write((cmd + '\r\n').encode())
            stream.flush()
            lines = []
            for _ in range(100):
                line = stream.readline(16384).decode('utf-8', errors='replace').rstrip()
                if not line:
                    raise RuntimeError('Tor control connection closed')
                lines.append(line)
                if re.match(r'^\d{3} ', line):
                    if not line.startswith('250 '):
                        raise RuntimeError('Tor control request failed')
                    return '\n'.join(lines)
            raise RuntimeError('Tor control reply too long')
        query('AUTHENTICATE ' + cookie.hex())
        return query(command)


def socks_ready():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1) as sock:
            sock.sendall(b'\x05\x01\x00')
            return sock.recv(2) == b'\x05\x00'
    except OSError:
        return False


def status():
    record = owned()
    if not record:
        return unowned_status()
    result = {'state': 'starting', 'ready': False, 'port': PORT, 'owned': True, 'pid': record['pid']}
    try:
        identity = control('GETINFO process/pid')
        if not re.search(r'^250[- ]process/pid=' + str(record['pid']) + r'$', identity, re.MULTILINE):
            result.update(state='conflict', detail='Control endpoint does not match the owned process')
            return result
        reply = control('GETINFO status/bootstrap-phase')
        m = re.search(r'PROGRESS=(\d+)', reply)
        result['bootstrapPercent'] = int(m.group(1)) if m else None
        result['ready'] = result['bootstrapPercent'] == 100 and socks_ready() and owned() == record
        result['state'] = 'ready' if result['ready'] else 'starting'
    except (OSError, RuntimeError) as exc:
        result['detail'] = 'Control socket not ready: ' + type(exc).__name__
    return result


def find_tor():
    requested = os.environ.get('TOR_BIN')
    candidates = [requested] if requested else [shutil.which('tor'), str(Path.home() / 'homebrew/bin/tor'),
        '/opt/homebrew/bin/tor', '/usr/local/bin/tor', '/usr/bin/tor']
    for binary in candidates:
        if binary and Path(binary).is_file() and os.access(binary, os.X_OK):
            return str(Path(binary).resolve())
    raise RuntimeError('Tor is not installed. Set TOR_BIN or install Tor first.')


def torrc():
    def quote(path):
        return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"'
    return '\n'.join([
        'DataDirectory ' + quote(HOME / 'state'),
        'SocksPort 127.0.0.1:%d IsolateSOCKSAuth IPv6Traffic PreferIPv6' % PORT,
        'ControlSocket ' + quote(HOME / 'control.sock'),
        'CookieAuthentication 1', 'CookieAuthFile ' + quote(HOME / 'control.authcookie'),
        'RunAsDaemon 0', 'MaxCircuitDirtiness 600',
        'Log notice stdout', '',
    ])


def regular_file(path):
    """Refuse links and special files at paths the manager writes or removes."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise RuntimeError('Managed Tor path is not a regular file: ' + str(path))


def start(wait):
    validate_home()
    if not owned():
        if unmanaged_processes():
            raise RuntimeError('Managed configuration already has a live process; no second process launched')
        # A live PID in a stale or altered record may still be our Tor. Never
        # replace its control socket/cookie or launch a second instance.
        record_path = HOME / 'process.json'
        regular_file(record_path)
        if record_path.exists():
            try:
                stale_pid = int(json.loads(record_path.read_text())['pid'])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError('Invalid process record; inspect it before starting Tor') from exc
            if stale_pid > 1 and ps(stale_pid, 'lstart'):
                raise RuntimeError('Recorded PID is still alive but ownership does not match; leaving it untouched')
        try:
            with socket.create_connection(('127.0.0.1', PORT), timeout=.5):
                raise RuntimeError('Port %d is already used by another process; leaving it untouched.' % PORT)
        except ConnectionRefusedError:
            pass
        restart_guard()
        binary = find_tor()
        rc = HOME / 'torrc'
        regular_file(rc)
        regular_file(HOME / 'launcher.log')
        desired = torrc()
        if rc.exists() and rc.read_text() != desired:
            raise RuntimeError('Existing torrc differs from the managed configuration; leaving it untouched: ' + str(rc))
        if (HOME / 'state').is_symlink():
            raise RuntimeError('Managed Tor configuration or state is a symlink; leaving it untouched')
        (HOME / 'state').mkdir(mode=0o700, exist_ok=True)
        (HOME / 'state').chmod(0o700)
        rc.write_text(desired)
        rc.chmod(0o600)
        # Never reuse stale control endpoints or cookies from a previous owned process.
        for name in ('control.sock', 'control.authcookie'):
            path = HOME / name
            if path.exists() or path.is_symlink():
                path.unlink()
        log_path = HOME / 'launcher.log'
        launch_path = HOME / 'last-launch.json'
        launch_path.write_text(json.dumps({'time': time.time()}))
        launch_path.chmod(0o600)
        with log_path.open('ab') as log:
            log_path.chmod(0o600)
            child = subprocess.Popen([binary, '-f', str(rc)], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
        try:
            started = ''
            for _ in range(20):
                if child.poll() is not None:
                    raise RuntimeError('Tor exited during startup; inspect the managed launcher.log')
                started = ps(child.pid, 'lstart')
                if started:
                    break
                time.sleep(.05)
            if not started:
                raise RuntimeError('Could not establish ownership of the new Tor process')
            record_path.write_text(json.dumps({'pid': child.pid, 'started': started, 'binary': binary}))
            record_path.chmod(0o600)
        except BaseException:
            if child.poll() is None:
                discard_child(child)
            raise
    deadline = time.monotonic() + wait
    while True:
        result = status()
        if result['ready'] or not result['owned'] or time.monotonic() >= deadline:
            print(json.dumps(result))
            return 0 if result['ready'] else 1
        time.sleep(.5)


def stop():
    record = owned()
    if not record:
        result = unowned_status()
        result.setdefault('detail', 'No owned Tor process to stop')
        print(json.dumps(result))
        return 0 if result['state'] == 'stopped' else 1
    # Revalidate immediately before signaling; never kill by name or by listening port.
    if owned() != record:
        raise RuntimeError('Process ownership changed; no signal sent')
    os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 10
    # Wait for the original PID to exit, even if config/record changes while
    # stopping. A failed ownership check is not proof the process exited.
    while ps(record['pid'], 'lstart') == record['started'] and time.monotonic() < deadline:
        time.sleep(.2)
    if ps(record['pid'], 'lstart') == record['started']:
        raise RuntimeError('Owned Tor is still stopping; no force-kill performed')
    record_path = HOME / 'process.json'
    if record_path.exists():
        regular_file(record_path)
        try:
            current = json.loads(record_path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError('Process record changed; leaving it untouched') from exc
        if current != record:
            raise RuntimeError('Process record changed; leaving it untouched')
    record_path.unlink(missing_ok=True)
    print(json.dumps({'state': 'stopped', 'pid': record['pid']}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'start', 'stop'], nargs='?', default='status')
    parser.add_argument('--wait', type=float, default=55)
    args = parser.parse_args()
    if not 1024 <= PORT <= 65535:
        raise RuntimeError('FL_TOR_PORT must be an unprivileged TCP port from 1024 to 65535')
    if not 0 <= args.wait < float('inf'):
        raise RuntimeError('--wait must be a finite nonnegative number')
    validate_home()
    if args.action == 'status':
        result = status()
        print(json.dumps(result))
        return 0 if result['ready'] else 1
    os.umask(0o077)
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    HOME.chmod(0o700)
    lock_path = HOME / 'manager.lock'
    regular_file(lock_path)
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return start(max(0,args.wait)) if args.action == 'start' else stop()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as exc:
        print(json.dumps({'state': 'error', 'error': str(exc)}), file=sys.stderr)
        sys.exit(1)
