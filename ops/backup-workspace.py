#!/usr/bin/env python3
"""Create or verify a private, manual snapshot of Fortunate Leads data.

    python3 ops/backup-workspace.py --repo . --output /private/path/snapshot-name
    python3 ops/backup-workspace.py --verify /private/path/snapshot-name

No scheduler is installed. The output directory must not already exist.
Credentials, browser profiles, model weights, old backups, and work files are
intentionally excluded. Keep the bundle private: lead and account data remain
sensitive even without provider secrets.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone


DATABASES = ('leads.sqlite', 'public-bios-cache.sqlite', 'llm_usage.sqlite')
DATA_DIRS = ('pfp', 'reviews', 'labels', 'reports')
DATA_FILES = ('WORK_CHECKPOINT.md', 'agent-plan.json')
CONFIG_FILES = ('ops/startup_profiles.json',)
REQUIRED_TABLES = {'people', 'seeds', 'lists', 'edges', 'accounts', 'settings'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def require_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Expected regular file without symlink: {path}')


def copy_file(source, target):
    require_file(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # APFS copy-on-write avoids another physical copy of the photo library.
    # Other filesystems and macOS versions safely fall back to ordinary copy.
    cloned = False
    if os.uname().sysname == 'Darwin':
        cloned = subprocess.run(['/bin/cp', '-c', str(source), str(target)],
                                capture_output=True).returncode == 0
    if not cloned:
        target.unlink(missing_ok=True)
        shutil.copyfile(source, target)
    target.chmod(0o600)


def copy_tree(source, target):
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f'Expected directory without symlink: {source}')
    target.mkdir(parents=True, mode=0o700)
    for root, dirs, files in os.walk(source, followlinks=False):
        rel = Path(root).relative_to(source)
        for name in dirs:
            child = Path(root) / name
            if child.is_symlink():
                raise ValueError(f'Symlink in data directory: {child}')
            (target / rel / name).mkdir(mode=0o700)
        for name in files:
            copy_file(Path(root) / name, target / rel / name)


def backup_database(source, target, primary=False):
    require_file(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=60)) as src:
        src.execute('PRAGMA busy_timeout=60000')
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise RuntimeError(f'Cannot make single-file SQLite copy: {source}')
            check = dst.execute('PRAGMA quick_check').fetchone()[0]
            if check != 'ok':
                raise RuntimeError(f'SQLite check failed: {source}: {check}')
            if primary:
                tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES <= tables:
                    raise RuntimeError(f'Primary database lacks tables: {sorted(REQUIRED_TABLES - tables)}')
    # SQLite may leave a harmless shared-memory file after closing a
    # DELETE-mode copy; it has no content needed by the published database.
    Path(str(target) + '-shm').unlink(missing_ok=True)
    if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-journal')):
        raise RuntimeError(f'SQLite copy has sidecars: {target}')
    target.chmod(0o600)


def inventory(bundle):
    files = {}
    for path in sorted(bundle.rglob('*')):
        if path.name == 'manifest.json':
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink in snapshot: {path}')
        if path.is_file():
            files[str(path.relative_to(bundle))] = {'bytes': path.stat().st_size,
                                                    'sha256': digest(path)}
    return files


def create_snapshot(repo, output):
    repo = Path(repo).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'Snapshot already exists: {output}')
    if output == repo or repo in output.parents:
        raise ValueError('Snapshot destination must be outside the repository')
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f'.{output.name}.', dir=output.parent))
    staging.chmod(0o700)
    try:
        for name in DATABASES:
            source = repo / 'data' / name
            if source.exists():
                backup_database(source, staging / 'data' / name, primary=name == 'leads.sqlite')
            elif name == 'leads.sqlite':
                raise FileNotFoundError(source)
        for name in DATA_DIRS:
            source = repo / 'data' / name
            if source.exists():
                copy_tree(source, staging / 'data' / name)
        for name in DATA_FILES:
            source = repo / 'data' / name
            if source.exists():
                copy_file(source, staging / 'data' / name)
        for name in CONFIG_FILES:
            source = repo / name
            if source.exists():
                copy_file(source, staging / name)
        files = inventory(staging)
        manifest = {'format': 'fortunate-leads-private-snapshot-v1',
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'files': files,
                    'excluded': ['provider secrets', 'browser profiles and cookies',
                                 'model weights', 'old backups', 'repo work files']}
        path = staging / 'manifest.json'
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        path.chmod(0o600)
        if output.exists() or output.is_symlink():
            raise FileExistsError(f'Snapshot already exists: {output}')
        os.rename(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_snapshot(bundle):
    bundle = Path(bundle).expanduser().resolve(strict=True)
    require_file(bundle / 'manifest.json')
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 'fortunate-leads-private-snapshot-v1':
        raise ValueError('Unknown snapshot format')
    actual = inventory(bundle)
    if actual != manifest['files']:
        raise ValueError('Snapshot file list, size, or checksum differs from manifest')
    if 'data/leads.sqlite' not in actual:
        raise ValueError('Snapshot lacks primary database')
    for name in DATABASES:
        path = bundle / 'data' / name
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError(f'Invalid SQLite copy: {name}')
                if name == 'leads.sqlite':
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not REQUIRED_TABLES <= tables:
                        raise ValueError('Snapshot primary database lacks required tables')
    return {'files': len(actual), 'bytes': sum(v['bytes'] for v in actual.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if bool(args.output) == bool(args.verify):
        parser.error('Provide exactly one of --output or --verify')
    os.umask(0o077)
    result = verify_snapshot(args.verify) if args.verify else create_snapshot(args.repo, args.output)
    print(json.dumps(result if args.verify else {'output': str(args.output),
                                                  'files': len(result['files'])}, indent=2))


if __name__ == '__main__':
    main()
