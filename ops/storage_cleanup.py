#!/usr/bin/env python3
"""Conservative, on-demand storage cleanup. Dry-run unless --apply is supplied.

Preserves all lead data, profile photos, model files, bio caches and named backups.
Keeps the latest three generated backups plus the newest backup on seven dates.
Only explicitly named synthetic benchmark files can be removed from /tmp.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import subprocess

BACKUP = re.compile(r'leads-(\d{8})-\d{6}-\d+-\d+\.sqlite$')
BENCHMARKS = ('fl-map-backend-100k.sqlite', 'fl-map-backend-1m.sqlite', 'fl-map-backend-5m.sqlite')


def regular(path):
    return path.is_file() and not path.is_symlink()


def retained_backups(paths):
    paths = sorted(paths, key=lambda p: p.name, reverse=True)
    keep = set(paths[:3])
    dates = set()
    for path in paths:
        day = BACKUP.fullmatch(path.name).group(1)
        if day not in dates and len(dates) < 7:
            dates.add(day)
            keep.add(path)
    return keep


def open_files(paths):
    if not paths:
        return False
    result = subprocess.run(['/usr/sbin/lsof', '-t', '--', *map(str, paths)], capture_output=True, text=True)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError('Cannot verify that cleanup files are unused: ' + result.stderr.strip())
    return bool(result.stdout.strip())


def healthy_backup(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Newest retained backup failed its integrity check')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'people', 'edges', 'tags'} <= tables:
            raise RuntimeError('Newest retained backup is missing lead tables')


def plan(data, include_benchmarks=False, temp=Path('/tmp')):
    backups = [p for p in (data / 'backups').glob('*.sqlite') if BACKUP.fullmatch(p.name) and regular(p)]
    keep = retained_backups(backups)
    remove = []
    for path in backups:
        # Preserve backup files with sidecars; they might be active databases.
        if path not in keep and not any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            remove.append((path, 'redundant generated backup'))
    if include_benchmarks:
        for name in BENCHMARKS:
            base = temp / name
            group = [Path(str(base) + suffix) for suffix in ('', '-wal', '-shm')]
            if any(p.is_symlink() for p in group):
                continue
            existing = [p for p in group if regular(p)]
            if existing and not open_files(existing):
                remove.extend((p, 'synthetic map benchmark') for p in existing)
    entries = []
    for path, reason in sorted(remove):
        stat = path.stat()
        entries.append({'path': str(path), 'bytes': stat.st_size, 'reason': reason,
                        'identity': [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]})
    return sorted(keep), entries


def apply(keep, entries):
    if any(e['reason'] == 'redundant generated backup' for e in entries):
        if not keep:
            raise RuntimeError('No retained backup')
        healthy_backup(keep[-1])
    paths = [Path(e['path']) for e in entries]
    if open_files(paths):
        raise RuntimeError('Cleanup file is in use; nothing deleted')
    for entry, path in zip(entries, paths):
        stat = path.lstat()
        identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        if not regular(path) or identity != entry['identity']:
            raise RuntimeError('Cleanup file changed; nothing deleted: ' + str(path))
    for path in paths:
        path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--include-benchmarks', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    keep, entries = plan(args.data.resolve(), args.include_benchmarks)
    if args.apply:
        apply(keep, entries)
    print(json.dumps({'applied': args.apply, 'reclaim_bytes': sum(e['bytes'] for e in entries),
                      'retained_generated_backups': list(map(str, keep)), 'files': entries,
                      'untouched': 'Lead database, photos, public bio cache, models and named backups'}, indent=2))


if __name__ == '__main__':
    main()
