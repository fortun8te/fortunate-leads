#!/usr/bin/env python3
"""Conservative, on-demand storage cleanup. Dry-run unless --apply is supplied.

    python3 ops/storage_cleanup.py --data data                 # what is removable under the default policy
    python3 ops/storage_cleanup.py --data data --inventory     # every entry in the data folder, classed
    python3 ops/storage_cleanup.py --data data --inventory --include-legacy-backups \
        --include-old-checkpoints --checkpoint-days 7 --include-disposable

By default it preserves all lead data, profile photos, model files, bio caches and named backups.
It keeps the latest three generated backups plus the newest backup on seven dates. Only explicitly
named synthetic benchmark files can be removed from /tmp. Extra tiers are opt-in, one flag each, and
each is listed before anything is removed:

  --include-legacy-backups   leads.backup-*.sqlite in data/ and HHMM-named leads-YYYYMMDD-HHMM.sqlite
  --include-old-checkpoints  named checkpoints older than --checkpoint-days (default 14)
  --include-disposable       benchmark output, test logs, scratch, the archived Tor folder, pickles
  --include-regenerable      leads.sqlite.map and leads.sqlite.universe (the app rebuilds them; stop it first)

--apply refuses to remove any backup unless the newest generated backup passes a full SQLite
integrity check, and refuses if any listed file is open or changed since it was listed.
Compression (--compress-plan) never removes anything: it reports what gzip would save.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

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
    if any(e['reason'] in BACKUP_REASONS[1:] for e in entries):
        if not keep:
            raise RuntimeError('No retained generated backup to rely on; nothing deleted')
        healthy_backup_full(keep[-1])
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
    # Tidy only folders this removal emptied, and only inside a known disposable or regenerable tree.
    own = set(DISPOSABLE_DIRS) | set(REGENERABLE_DIRS)
    tidy = set()
    for entry, path in zip(entries, paths):
        if entry['reason'] in ('disposable', 'regenerable cache'):
            chain = []
            for parent in path.parents:
                chain.append(parent)
                if parent.name in own:
                    tidy.update(chain)
                    break
    for folder in sorted(tidy, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass


LEGACY_DATED = re.compile(r'leads-\d{8}-\d{4}\.sqlite$')
LEGACY_ROOT = re.compile(r'leads\.backup-.+\.sqlite$')
SIDECAR = re.compile(r'(.+\.sqlite)(-wal|-shm|-journal|\.partial-shm|\.partial)$')
DISPOSABLE_FILES = re.compile(
    r'(benchmark-[\w.-]+\.(json|log|txt)|bio-live-probe\.json|astra_[\w.-]+\.json|broad_[\w.-]+\.pkl|'
    r'test-[\w.-]+\.log|server-integration-run[\w.-]*\.log|server\.log|scraper-egresses\.yaml)$')
DISPOSABLE_DIRS = ('benchmarks', 'scratch', 'tor')
REGENERABLE_DIRS = ('leads.sqlite.map', 'leads.sqlite.universe')
BACKUP_REASONS = ('redundant generated backup', 'legacy backup', 'old checkpoint', 'sidecar of a removed checkpoint',
                  'original of a verified gzip copy')

# Why each class exists. 'irreplaceable' and 'secret' are never listed for removal by any flag.
CLASSES = {
    'irreplaceable': 'Cannot be rebuilt. This script never lists it.',
    'secret': 'Keys and bindings. Never listed; keep out of shared folders.',
    'slow-to-rebuild': 'Rebuildable only by new Instagram reads at the safe pace (weeks). Compress, do not delete.',
    'backup': 'A copy of the lead database. The policy below decides what is redundant.',
    'regenerable': 'The app rebuilds it. Stop the app before removing.',
    'disposable': 'Test output, old logs, benchmark leftovers. Nothing reads them.',
    'state': 'Small bookkeeping files the app or installer reads. Keep.',
    'notes': 'Hand-off notes. Keep.',
    'unknown': 'Not recognised. Review by hand.',
}
KNOWN = {
    'leads.sqlite': ('irreplaceable', 'The lead database.'),
    'leads.sqlite-wal': ('irreplaceable', 'Write-ahead log of the lead database; belongs with it.'),
    'leads.sqlite-shm': ('irreplaceable', 'Shared memory index of the lead database.'),
    'llm_usage.sqlite': ('irreplaceable', 'AI usage ledger.'),
    'pfp': ('slow-to-rebuild', 'Profile photos. The server refetches missing ones from saved addresses; expired addresses cannot be.'),
    'public-bios-cache.sqlite': ('slow-to-rebuild', 'Raw bio responses from 26 Sep 2026. Nothing in this repository reads it. Compresses about 5x.'),
    'public-bios-cache.sqlite-wal': ('slow-to-rebuild', 'Belongs with the bio cache.'),
    'public-bios-cache.sqlite-shm': ('slow-to-rebuild', 'Belongs with the bio cache.'),
    'public-bios-worker.lock': ('state', 'Empty lock file of the retired bio worker.'),
    'reviews': ('irreplaceable', 'Hand-entered review files.'),
    'labels': ('irreplaceable', 'Hand-entered labels.'),
    'reports': ('irreplaceable', 'Generated reports kept as evidence.'),
    'openrouter.json': ('secret', 'OpenRouter keys.'),
    'openrouter.state.json': ('secret', 'OpenRouter key state.'),
    'browser-startup.json': ('secret', 'Chrome profile to account bindings.'),
    '.server-source.sha256': ('state', 'Installer fingerprint: decides whether the server restarts.'),
    'scraper-venv': ('state', 'Python environment the LaunchAgent runs (data/scraper-venv/bin/python). Do not remove.'),
    'WORK_CHECKPOINT.md': ('notes', 'Work notes.'), 'agent-plan.json': ('notes', 'Work notes.'),
    'ASTRA_HANDOFF.md': ('notes', 'Work notes.'),
    'backups': ('backup', 'Generated and named database backups.'),
    'leads.sqlite.map': ('regenerable', 'Prepared map layout caches.'),
    'leads.sqlite.universe': ('regenerable', 'Prepared map snapshot (not selected by the current UI).'),
    'benchmarks': ('disposable', 'Collection and map benchmark output.'),
    'scratch': ('disposable', 'Scratch files from September.'),
    'tor': ('disposable', 'Archived Tor collector files. docs/PRODUCT.md rules Tor and proxies out; confirm, then remove.'),
}


def tree_size(path):
    """(files, apparent bytes, allocated bytes) of a file or directory, without following symlinks."""
    path = Path(path)
    if path.is_symlink() or path.is_file():
        info = path.lstat()
        return 1, info.st_size, info.st_blocks * 512
    files = size = blocks = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            files += 1
            size += info.st_size
            blocks += info.st_blocks * 512
    return files, size, blocks


def classify_entry(name):
    if name in KNOWN:
        return KNOWN[name]
    if LEGACY_ROOT.fullmatch(name) or LEGACY_DATED.fullmatch(name):
        return 'backup', 'Older-format backup of the lead database.'
    if DISPOSABLE_FILES.fullmatch(name):
        return 'disposable', 'Generated test or benchmark output.'
    return 'unknown', CLASSES['unknown']


def backup_kind(name):
    if BACKUP.fullmatch(name):
        return 'daily'
    if LEGACY_DATED.fullmatch(name):
        return 'legacy-dated'
    if LEGACY_ROOT.fullmatch(name):
        return 'legacy-root'
    if SIDECAR.fullmatch(name):
        return 'sidecar'
    return 'checkpoint' if name.endswith('.sqlite') else 'other'


def inventory(data, *, walk_photos=True, now=None):
    """Every entry of the data folder with size, class and note, plus the backups one by one. Read only."""
    data, now = Path(data), now or time.time()
    rows = []
    for entry in sorted(data.iterdir(), key=lambda p: p.name):
        is_dir = entry.is_dir() and not entry.is_symlink()
        klass, note = classify_entry(entry.name)
        if is_dir and entry.name == 'pfp' and not walk_photos:
            files, size, blocks = 0, 0, 0
            note += ' (not measured: --fast)'
        else:
            files, size, blocks = tree_size(entry)
        rows.append({'name': entry.name + ('/' if is_dir else ''), 'class': klass, 'note': note, 'files': files,
                     'bytes': size, 'allocated': blocks})
    backups = []
    root = data / 'backups'
    if root.is_dir():
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if entry.is_file() and not entry.is_symlink():
                info = entry.lstat()
                backups.append({'name': entry.name, 'kind': backup_kind(entry.name), 'bytes': info.st_size,
                                'allocated': info.st_blocks * 512, 'age_days': round((now - info.st_mtime) / 86400, 1)})
    return rows, backups


def _entry(path, reason):
    stat = path.stat()
    return {'path': str(path), 'bytes': stat.st_size, 'reason': reason,
            'identity': [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]}


def _files_of(path):
    """Files under a directory, or None if the tree contains a symlink (then nothing in it is listed)."""
    out = []
    for root, dirs, names in os.walk(path, followlinks=False):
        for name in dirs + names:
            if os.path.islink(os.path.join(root, name)):
                return None
        out.extend(Path(root, name) for name in names)
    return out


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def gzip_matches(original, twin):
    """Whether twin (a .gz file) decompresses to exactly the bytes of original."""
    import gzip
    digest = hashlib.sha256()
    try:
        with gzip.open(twin, 'rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                digest.update(block)
    except (OSError, EOFError):
        return False
    return digest.hexdigest() == sha256_of(original)


def tier_plan(data, include, *, checkpoint_days=14, now=None):
    """Extra removal tiers, each opt-in. Returns entries shaped like plan()'s."""
    data, now = Path(data), now or time.time()
    entries = []
    backups_dir = data / 'backups'
    if 'legacy' in include:
        for folder in (data, backups_dir):
            if folder.is_dir():
                for path in sorted(folder.iterdir()):
                    if regular(path) and (LEGACY_ROOT.fullmatch(path.name) or LEGACY_DATED.fullmatch(path.name)) \
                            and not any(Path(str(path) + s).exists() for s in ('-wal', '-journal')):
                        entries.append(_entry(path, 'legacy backup'))
    if 'checkpoints' in include and backups_dir.is_dir():
        generated = sorted((p for p in backups_dir.glob('*.sqlite') if BACKUP.fullmatch(p.name) and regular(p)),
                           key=lambda p: p.stat().st_mtime)
        named = sorted((p for p in backups_dir.glob('*.sqlite')
                        if backup_kind(p.name) == 'checkpoint' and regular(p)), key=lambda p: p.stat().st_mtime)
        newest_generated = generated[-1].stat().st_mtime if generated else None
        for path in named[:-1]:                                  # the newest checkpoint is always kept
            age_days = (now - path.stat().st_mtime) / 86400
            if age_days < checkpoint_days or newest_generated is None or newest_generated <= path.stat().st_mtime:
                continue                                          # needs a newer generated backup behind it
            wal, shm, journal = (Path(str(path) + s) for s in ('-wal', '-shm', '-journal'))
            if journal.exists() or (wal.exists() and (not regular(wal) or wal.stat().st_size > 0)):
                continue                                          # unreplayed changes: leave it for a person
            entries.append(_entry(path, 'old checkpoint'))
            for side in (wal, shm):
                if regular(side):
                    entries.append(_entry(side, 'sidecar of a removed checkpoint'))
    if 'disposable' in include:
        for path in sorted(data.iterdir()):
            if path.is_symlink():
                continue
            if path.is_file() and DISPOSABLE_FILES.fullmatch(path.name):
                entries.append(_entry(path, 'disposable'))
            elif path.is_dir() and path.name in DISPOSABLE_DIRS:
                entries.extend(_entry(f, 'disposable') for f in (_files_of(path) or []))
        if backups_dir.is_dir():
            for path in sorted(backups_dir.iterdir()):
                match = SIDECAR.fullmatch(path.name)
                if regular(path) and match and not (backups_dir / match.group(1)).exists():
                    entries.append(_entry(path, 'disposable'))     # stray sidecar whose database is gone
    if 'regenerable' in include:
        for name in REGENERABLE_DIRS:
            path = data / name
            if path.is_dir() and not path.is_symlink():
                entries.extend(_entry(f, 'regenerable cache') for f in (_files_of(path) or []))
    if 'gzip-twins' in include:
        for folder in (data, backups_dir):
            if folder.is_dir():
                for twin in sorted(folder.glob('*.gz')):
                    original = twin.with_suffix('')
                    if regular(original) and regular(twin) and gzip_matches(original, twin):
                        entries.append(_entry(original, 'original of a verified gzip copy'))
    return entries


def compress_plan(data, *, daily_min_days=2, now=None, level=6, chunk=4 * 1024 * 1024, chunks=4):
    """Reversible savings: what gzip would save, from a sampled compression ratio. Nothing is written.

    Candidates: dailies older than `daily_min_days`, every named checkpoint and older-format backup,
    and the raw bio cache. Never the live database or anything else in the data folder.
    """
    import zlib
    data, now = Path(data), now or time.time()
    candidates = []
    backups_dir = data / 'backups'
    if backups_dir.is_dir():
        for path in sorted(backups_dir.iterdir()):
            if not regular(path) or path.name.endswith('.gz') or Path(str(path) + '.gz').exists():
                continue
            kind = backup_kind(path.name)
            age = (now - path.stat().st_mtime) / 86400
            if kind in ('checkpoint', 'legacy-dated') or (kind == 'daily' and age >= daily_min_days):
                candidates.append(path)
    for path in sorted(data.glob('leads.backup-*.sqlite')) + [data / 'public-bios-cache.sqlite']:
        if regular(path) and not Path(str(path) + '.gz').exists():
            candidates.append(path)
    rows = []
    for path in candidates:
        size = path.stat().st_size
        raw = packed = 0
        with open(path, 'rb') as stream:
            for i in range(chunks):
                offset = max(size - chunk, 0) * i // max(chunks - 1, 1)
                stream.seek(offset - offset % 4096)
                block = stream.read(chunk)
                raw += len(block)
                packed += len(zlib.compress(block, level))
        ratio = packed / raw if raw else 1.0
        rows.append({'path': str(path), 'bytes': size, 'ratio': round(ratio, 3), 'saves_bytes': int(size * (1 - ratio))})
    return rows


def healthy_backup_full(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        rows = [r[0] for r in db.execute('PRAGMA integrity_check')]
    finally:
        db.close()
    if rows != ['ok']:
        raise RuntimeError('Newest retained backup failed its full integrity check')


def human(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1000


def print_inventory(data, rows, backups, tiers, compress):
    total = sum(r['allocated'] for r in rows)
    print(f'data folder {data}: {human(total)} on disk\n')
    by_class = {}
    for r in rows:
        by_class.setdefault(r['class'], []).append(r)
    for klass in CLASSES:
        group = by_class.get(klass)
        if not group:
            continue
        print(f"{klass}  {human(sum(r['allocated'] for r in group))}  - {CLASSES[klass]}")
        for r in sorted(group, key=lambda r: -r['allocated']):
            if r['allocated'] >= 1_000_000 or klass == 'unknown':
                print(f"    {human(r['allocated']):>9}  {r['name']:<34} {r['note']}")
        small = [r for r in group if r['allocated'] < 1_000_000 and klass != 'unknown']
        if small:
            print(f"    {human(sum(r['allocated'] for r in small)):>9}  {len(small)} small entries")
    print('\nbackups:')
    for kind in ('daily', 'checkpoint', 'legacy-dated', 'legacy-root', 'sidecar'):
        group = [b for b in backups if b['kind'] == kind]
        if group:
            print(f"    {kind:<13}{len(group):>3} files  {human(sum(b['allocated'] for b in group)):>9}  "
                  f"oldest {max(b['age_days'] for b in group):.0f} days, newest {min(b['age_days'] for b in group):.0f} days")
    print('\nwhat each opt-in tier would remove (nothing is removed without --apply):')
    for label, flag, entries in tiers:
        print(f"    {human(sum(e['bytes'] for e in entries)):>9}  {len(entries):>5} files  {label:<46} {flag}")
    if compress:
        print('\nreversible, nothing removed (gzip -k keeps the original; remove it later with --include-gzipped-originals):')
        for row in compress:
            print(f"    saves {human(row['saves_bytes']):>9} of {human(row['bytes']):>9}  ({row['ratio']:.0%} of size)  {Path(row['path']).name}")
        print(f"    total saves {human(sum(r['saves_bytes'] for r in compress))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--include-benchmarks', action='store_true')
    parser.add_argument('--include-legacy-backups', action='store_true')
    parser.add_argument('--include-old-checkpoints', action='store_true')
    parser.add_argument('--checkpoint-days', type=float, default=14)
    parser.add_argument('--include-disposable', action='store_true')
    parser.add_argument('--include-regenerable', action='store_true')
    parser.add_argument('--include-gzipped-originals', action='store_true')
    parser.add_argument('--inventory', action='store_true', help='list the whole data folder by class and the savings per tier')
    parser.add_argument('--compress-plan', action='store_true', help='with --inventory: sample how much gzip would save')
    parser.add_argument('--fast', action='store_true', help='do not walk data/pfp (157k files)')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    data = args.data.resolve()
    keep, entries = plan(data, args.include_benchmarks)
    include = {name for flag, name in ((args.include_legacy_backups, 'legacy'), (args.include_old_checkpoints, 'checkpoints'),
                                       (args.include_disposable, 'disposable'), (args.include_regenerable, 'regenerable'),
                                       (args.include_gzipped_originals, 'gzip-twins')) if flag}
    entries = entries + tier_plan(data, include, checkpoint_days=args.checkpoint_days)
    if args.inventory:
        rows, backups = inventory(data, walk_photos=not args.fast)
        tiers = [('generated backups beyond retention (default)', '(on by default)', [e for e in entries if e['reason'] == 'redundant generated backup'])]
        for label, flag, name in (('older-format backups', '--include-legacy-backups', 'legacy'),
                                  (f'checkpoints older than {args.checkpoint_days:g} days', '--include-old-checkpoints', 'checkpoints'),
                                  ('disposable test and benchmark output', '--include-disposable', 'disposable'),
                                  ('regenerable map caches', '--include-regenerable', 'regenerable'),
                                  ('originals that have a verified .gz copy', '--include-gzipped-originals', 'gzip-twins')):
            tiers.append((label, flag, tier_plan(data, {name}, checkpoint_days=args.checkpoint_days)))
        compress = compress_plan(data) if args.compress_plan else []
        if args.json:
            print(json.dumps({'rows': rows, 'backups': backups, 'tiers': [{'label': l, 'flag': f, 'bytes': sum(e['bytes'] for e in es),
                                                                         'files': len(es)} for l, f, es in tiers],
                              'compress': compress}, indent=2))
        else:
            print_inventory(data, rows, backups, tiers, compress)
        return
    if args.apply:
        apply(keep, entries)
    print(json.dumps({'applied': args.apply, 'reclaim_bytes': sum(e['bytes'] for e in entries),
                      'retained_generated_backups': list(map(str, keep)), 'files': entries,
                      'untouched': 'Lead database, photos, public bio cache, models and named backups'
                      + ('' if include else ' (unless a tier flag is given)')}, indent=2))


if __name__ == '__main__':
    main()
