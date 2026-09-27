#!/usr/bin/env python3
"""Install the pinned existing K2 build, or manage its localhost process.

No downloads, provider calls, or Instagram operations. Install is explicit.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import fcntl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'server'))
import local_model
import resource_budget

ROOT = Path.home() / 'Library/Application Support/Fortunate Leads/k2'
REVISION = '42adf019f76013dac873b5b43950d54d5ab27216'
WEIGHT = 'K2-Horizon-4B-Q4_K_M.gguf'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def install(source):
    source = Path(source).resolve()
    model = source / WEIGHT
    if digest(model) != local_model.MODEL_DIGEST:
        raise RuntimeError('K2 weights do not match the verified model')
    runtime = source / 'llama.cpp'
    revision = subprocess.check_output(['git', '-C', str(runtime), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(runtime), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
    if revision != REVISION or dirty:
        raise RuntimeError('K2 runtime does not match the verified publisher build')
    if (ROOT / 'manifest.json').exists():
        verify()
        return
    if ROOT.exists() and any(ROOT.iterdir()):
        raise RuntimeError('An incomplete K2 installation exists; inspect it before installing')
    ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    target = ROOT / WEIGHT
    # clonefile through cp -c on macOS shares unchanged storage blocks.
    copied = subprocess.run(['/bin/cp', '-c', str(model), str(target)], capture_output=True).returncode == 0 if sys.platform == 'darwin' else False
    if not copied:
        shutil.copy2(model, target)
    files = [runtime / 'build/bin/llama-server'] + sorted((runtime / 'build/bin').glob('*.dylib'))
    (ROOT / 'bin').mkdir(exist_ok=True)
    for item in files:
        shutil.copy2(item, ROOT / 'bin' / item.name, follow_symlinks=False)
    relocate_runtime(ROOT / 'bin')
    version = subprocess.run([str(ROOT / 'bin/llama-server'), '--version'], check=True,
                             capture_output=True, text=True, timeout=45)
    if REVISION[:7] not in version.stdout + version.stderr:
        raise RuntimeError('The compiled K2 binary reports an unexpected revision')
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in ROOT.rglob('*') if p.is_file()}
    manifest = {'model': local_model.MODEL, 'model_sha256': local_model.MODEL_DIGEST,
                'runtime_revision': REVISION, 'hashes': hashes}
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2))


def relocate_runtime(directory):
    """Remove build-checkout rpaths and bundle non-system dylib dependencies."""
    if sys.platform != 'darwin':
        return
    processed = set()
    pending = [p for p in directory.iterdir() if p.is_file() and not p.is_symlink()]
    while pending:
        item = pending.pop()
        if item.name in processed:
            continue
        processed.add(item.name)
        details = subprocess.check_output(['/usr/bin/otool', '-L', str(item)], text=True)
        for line in details.splitlines()[1:]:
            dep = line.strip().split(' (', 1)[0]
            if not dep.startswith('/') or dep.startswith(('/System/', '/usr/lib/')):
                continue
            target = directory / Path(dep).name
            if not target.exists():
                shutil.copy2(dep, target)
                pending.append(target)
            subprocess.run(['/usr/bin/install_name_tool', '-change', dep,
                            '@loader_path/' + target.name, str(item)], check=True, capture_output=True)
        load = subprocess.check_output(['/usr/bin/otool', '-l', str(item)], text=True)
        rpaths = re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path ([^\n]+?) \(offset', load)
        for path in rpaths:
            if path != '@loader_path':
                subprocess.run(['/usr/bin/install_name_tool', '-delete_rpath', path, str(item)], check=True, capture_output=True)
        if '@loader_path' not in rpaths:
            subprocess.run(['/usr/bin/install_name_tool', '-add_rpath', '@loader_path', str(item)], check=True, capture_output=True)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(item)], check=True, capture_output=True)


def verify():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    if manifest.get('model_sha256') != local_model.MODEL_DIGEST or manifest.get('runtime_revision') != REVISION:
        raise RuntimeError('Unexpected local K2 installation')
    hashes = manifest.get('hashes', {})
    if hashes.get(WEIGHT) != local_model.MODEL_DIGEST or 'bin/llama-server' not in hashes:
        raise RuntimeError('Incomplete local K2 manifest')
    for name, expected in hashes.items():
        path = (ROOT / name).resolve()
        if ROOT.resolve() not in path.parents or digest(path) != expected:
            raise RuntimeError('Installed K2 file verification failed: ' + name)


def owned_pid():
    try:
        pid = int((ROOT / 'pid').read_text())
        command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        return pid if command.startswith(str(ROOT / 'bin/llama-server') + ' ') and '--port 11436' in command else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def stop():
    pid = owned_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not owned_pid():
                break
            time.sleep(.1)
        else:
            raise RuntimeError('K2 did not stop; no replacement was started')
    (ROOT / 'pid').unlink(missing_ok=True)


def start():
    if owned_pid() and local_model.ready():
        return
    try:
        with resource_budget.lease('k2_start', startup=True):
            _start()
    except resource_budget.Deferred as exc:
        raise RuntimeError(str(exc)) from exc


def _start():
    if owned_pid() and local_model.ready():
        return
    verify()
    if owned_pid():
        raise RuntimeError('K2 is starting or unhealthy; wait before retrying')
    # Never replace an unknown service, even one advertising the right model.
    import socket
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', 11436)) == 0:
            raise RuntimeError('Another service owns the K2 port')
    command = [str(ROOT / 'bin/llama-server'), '-m', str(ROOT / WEIGHT), '--host', '127.0.0.1',
        '--port', '11436', '--ctx-size', '4096', '--parallel', '1', '-ngl', '99', '--threads', '2',
        '--batch-size', '64', '--ubatch-size', '64', '--jinja', '--reasoning-effort', 'low',
        '--no-context-shift', '--no-webui', '--alias', local_model.MODEL]
    log_path = ROOT / 'server.log'
    if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
        log_path.replace(ROOT / 'server.previous.log')
    with log_path.open('ab') as log:
        process = subprocess.Popen(['/usr/bin/nice', '-n', '10'] + command, stdout=log, stderr=log,
                                   start_new_session=True, cwd=str(ROOT))
    (ROOT / 'pid').write_text(str(process.pid))
    (ROOT / 'last_activity').write_text(str(time.time()))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('K2 exited while loading; inspect its local log')
        if local_model.ready():
            return
        time.sleep(.5)
    stop()
    raise RuntimeError('K2 did not become ready within 60 seconds')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'start', 'stop', 'status'])
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps(local_model.status()))
        return
    ROOT.parent.mkdir(parents=True, exist_ok=True)
    with (ROOT.parent / 'k2-service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'install':
            if not args.source:
                parser.error('install requires --source with the existing verified build')
            install(args.source)
        else:
            globals()[args.action]()
    print('K2 ' + args.action + ' complete')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
