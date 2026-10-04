#!/usr/bin/env python3
"""Restore test for every backup type. Read-only on the real data folder.

    python3 ops/restore_check.py                       # every backup under ./data, full checks
    python3 ops/restore_check.py --data /path/to/data --serve --json report.json
    python3 ops/restore_check.py --path some-backup.sqlite --path snapshot-bundle/

What it does for each backup (the originals are only read with `cp -c`, never opened):
  1. restore it into a scratch folder with the same code a real restore uses (verify, place, verify)
  2. full SQLite integrity_check on the restored copy, required tables, evidence row counts,
     logical orphan checks, journal mode and sidecars
  3. open it with the current server code (db.init, the upgrade every server start runs) on that copy
  4. with --serve, start the real server on the restored copy of the newest backup of each kind
     (--no-workers, so no Instagram, photo or model traffic) and read /api/counts and /api/leads
Backup types: daily (ops/backup.sh, the 03:30 LaunchAgent), checkpoint (named copies),
legacy-dated and legacy-root (older naming), workspace bundles (ops/backup-workspace.py).
Stray sidecars (*.partial-shm and similar) are listed, not restored. Exit status 1 on any FAIL.
Nothing is deleted; the scratch folder is removed at the end unless --keep-scratch is given.
"""
import argparse
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'server'))
import dbsafe  # noqa: E402

KINDS = (
    ('daily', re.compile(r'leads-\d{8}-\d{6}-\d+-\d+\.sqlite$')),
    ('legacy-dated', re.compile(r'leads-\d{8}-\d{4}\.sqlite$')),
    ('legacy-root', re.compile(r'leads\.backup-.+\.sqlite$')),
)
SIDECAR = re.compile(r'(.+\.sqlite)(-wal|-shm|-journal|\.partial-shm|\.partial)$')


def classify(name):
    for kind, pattern in KINDS:
        if pattern.fullmatch(name):
            return kind
    return 'checkpoint' if name.endswith('.sqlite') else None


def discover(data):
    """Every backup candidate under a data folder, sorted by kind then name. Stray sidecars are 'ignored'."""
    data = Path(data)
    found, ignored = [], []
    roots = [data / 'backups', data]
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if entry.is_symlink():
                ignored.append({'path': str(entry), 'why': 'symlink'})
            elif entry.is_file():
                if root == data and not entry.name.startswith('leads.backup-'):
                    continue                       # the live database and other files are not backups
                match = SIDECAR.fullmatch(entry.name)
                if match:
                    if not (root / match.group(1)).exists():
                        ignored.append({'path': str(entry), 'why': 'stray sidecar, its database is gone'})
                    continue                       # a sidecar beside its database is listed with that backup
                kind = classify(entry.name)
                if kind:
                    found.append({'path': str(entry), 'kind': kind, 'bytes': entry.stat().st_size,
                                  'mtime': entry.stat().st_mtime,
                                  'sidecars': [p.name for p in dbsafe.sidecars(entry)]})
            elif entry.is_dir() and (entry / 'manifest.json').is_file():
                found.append({'path': str(entry), 'kind': 'bundle', 'bytes': 0, 'mtime': entry.stat().st_mtime,
                              'sidecars': []})
    order = {'daily': 0, 'checkpoint': 1, 'legacy-dated': 2, 'legacy-root': 3, 'bundle': 4}
    return sorted(found, key=lambda c: (order[c['kind']], c['path'])), ignored


def expected_schema():
    """Tables and columns a database has after the current server code has opened it."""
    import db
    with tempfile.TemporaryDirectory(prefix='fl-schema-') as tmp:
        conn = db.init(str(Path(tmp) / 'fresh.sqlite'))
        try:
            return dbsafe.column_map(conn)
        finally:
            conn.close()


def timed(function, *args, **kwargs):
    started = time.monotonic()
    value = function(*args, **kwargs)
    return value, round(time.monotonic() - started, 2)


def serve_check(db_path, port, python, timeout=90.0):
    """Start the real server on this database copy, read it, stop it. Never left running."""
    result = {'ok': False, 'port': port}
    home = tempfile.mkdtemp(prefix='fl-serve-home-')
    log = Path(db_path).with_name('server.log')
    started = time.monotonic()
    with open(log, 'wb') as out:
        proc = subprocess.Popen([python, str(REPO / 'server/server.py'), '--db', str(db_path),
                                 '--port', str(port), '--no-workers'],
                                stdout=out, stderr=subprocess.STDOUT, cwd=str(REPO),
                                env=dict(os.environ, HOME=home, PYTHONUNBUFFERED='1'))
    try:
        base = f'http://127.0.0.1:{port}'
        counts = None
        while time.monotonic() - started < timeout:
            if proc.poll() is not None:
                result['error'] = f'server exited with {proc.returncode}: ' + log.read_text(errors="replace")[-300:]
                return result
            try:
                with urllib.request.urlopen(base + '/api/counts', timeout=5) as response:
                    counts = json.load(response)
                break
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.25)
        if counts is None:
            result['error'] = f'no answer within {timeout:.0f}s'
            return result
        result['seconds_to_answer'] = round(time.monotonic() - started, 2)
        result['total'] = counts.get('total')
        with urllib.request.urlopen(base + '/api/leads?limit=1', timeout=15) as response:
            result['leads_status'] = response.status
        result['ok'] = isinstance(counts.get('total'), int) and result['leads_status'] == 200
        return result
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result['error'] = str(exc)
        return result
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        result['exit_code'] = proc.returncode
        shutil.rmtree(home, ignore_errors=True)


def check_database(candidate, scratch, expected, *, full, serve_port, python, allow_copy):
    """Restore one database backup into scratch and test the restored copy."""
    source = Path(candidate['path'])
    folder = scratch / re.sub(r'[^A-Za-z0-9._-]', '_', source.name)
    folder.mkdir(parents=True)
    target = folder / 'leads.sqlite'
    row = {'name': source.name, 'kind': candidate['kind'], 'bytes': candidate['bytes'],
           'sidecars_beside_original': candidate['sidecars'], 'problems': [], 'warnings': [], 'steps': {}}
    try:
        restored, row['steps']['restore_s'] = timed(dbsafe.restore_database, source, target, folder / 'aside',
                                                    check_open=False)
    except (dbsafe.RestoreError, OSError, sqlite3.Error) as exc:
        row['problems'].append(f'restore failed: {exc}')
        row['status'] = 'FAIL'
        return row
    row['restored_counts'] = restored['counts']
    with closing(dbsafe.open_database(target)) as conn:
        row['journal_mode'] = conn.execute('PRAGMA journal_mode').fetchone()[0]
        try:
            problems, row['steps']['integrity_s'] = timed(dbsafe.check_database, conn, full=full)
            row['problems'] += [f'integrity: {line}' for line in problems]
        except sqlite3.Error as exc:
            row['problems'].append(f'integrity: {exc}')
        row['counts'] = dbsafe.row_counts(conn, ('people', 'seeds', 'lists', 'edges', 'tags', 'marks', 'verdicts',
                                                 'jobs', 'owner_context', 'accounts', 'settings'))
        if not row['counts'].get('people'):
            row['problems'].append('no people rows')
        orphans, row['steps']['orphans_s'] = timed(dbsafe.logical_orphans, conn)
        row['orphans'] = {k: v for k, v in orphans.items() if v}
        columns = dbsafe.column_map(conn)
        missing_tables = sorted(t for t in expected if t not in columns)
        missing_columns = {t: [c for c in cols if c not in columns[t]] for t, cols in expected.items()
                           if t in columns and any(c not in columns[t] for c in cols)}
        row['needs_upgrade'] = {'tables': len(missing_tables), 'columns': sum(map(len, missing_columns.values()))}
        before = dbsafe.evidence_counts(conn)
    if candidate['kind'] == 'daily':
        if row['journal_mode'] != 'delete':
            row['warnings'].append(f'daily backup is in {row["journal_mode"]} mode, expected delete')
        if candidate['sidecars']:
            row['warnings'].append('sidecar files beside a daily backup: ' + ', '.join(candidate['sidecars']))
        if (source.stat().st_mode & 0o077):
            row['warnings'].append('file is readable by other users (expected mode 600)')
    # What every server start does to a database: db.init. Must open it and must not lose evidence.
    import db
    try:
        upgraded, row['steps']['upgrade_s'] = timed(db.init, str(target))
        upgraded.close()
        with closing(dbsafe.open_database(target)) as conn:
            lost = dbsafe.evidence_lost(before, dbsafe.evidence_counts(conn))
            if lost:
                row['problems'].append(f'upgrade lost rows: {lost}')
            after_problems = dbsafe.check_database(conn, full=False)
            row['problems'] += [f'after upgrade: {line}' for line in after_problems]
    except Exception as exc:  # noqa: BLE001 - any failure here means this backup cannot be restored and served
        row['problems'].append(f'upgrade: {type(exc).__name__}: {exc}')
    if serve_port and not row['problems']:
        row['server'] = serve_check(target, serve_port, python)
        if not row['server']['ok']:
            row['problems'].append('server: ' + str(row['server'].get('error') or 'did not answer'))
        elif row['server'].get('total') != row['counts'].get('people'):
            row['warnings'].append(f"server total {row['server'].get('total')} differs from people rows "
                                   f"{row['counts'].get('people')}")
    row['status'] = 'FAIL' if row['problems'] else 'WARN' if row['warnings'] or row['orphans'] else 'PASS'
    return row


def load_backup_workspace():
    spec = importlib.util.spec_from_file_location('backup_workspace', REPO / 'ops/backup-workspace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_bundle(candidate, scratch, expected, *, serve_port, python):
    """Verify a workspace snapshot, restore it into a fresh folder, check the restored database and photos."""
    bundle = Path(candidate['path'])
    row = {'name': bundle.name, 'kind': 'bundle', 'problems': [], 'warnings': [], 'steps': {}}
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 'fortunate-leads-private-snapshot-v1':
        row['kind'] = 'export-bundle'
        row['warnings'].append('data export bundle (JSON/CSV): checked by ops/import_workspace.py, not restorable here')
        row['status'] = 'WARN'
        return row
    module = load_backup_workspace()
    try:
        info, row['steps']['verify_s'] = timed(module.verify_snapshot, bundle)
        row['bytes'] = info['bytes']
        fresh = scratch / ('bundle-' + re.sub(r'[^A-Za-z0-9._-]', '_', bundle.name))
        _, row['steps']['restore_s'] = timed(module.restore_snapshot, bundle, fresh)
    except (ValueError, OSError, sqlite3.Error) as exc:
        row['problems'].append(f'restore failed: {exc}')
        row['status'] = 'FAIL'
        return row
    photos = [r for r in manifest['files'] if r.startswith('data/pfp/')]
    present = sum(1 for r in photos if (fresh / r).is_file())
    row['photos'] = {'in_manifest': len(photos), 'restored': present}
    if present != len(photos):
        row['problems'].append(f'{len(photos) - present} photos missing after restore')
    sub = check_database({'path': str(fresh / 'data/leads.sqlite'), 'kind': 'bundle-database', 'bytes':
                          (fresh / 'data/leads.sqlite').stat().st_size, 'sidecars': []},
                         scratch / 'bundle-db', expected, full=True, serve_port=serve_port, python=python,
                         allow_copy=True)
    row['database'] = {k: sub[k] for k in ('counts', 'steps', 'problems', 'warnings', 'server', 'status')
                       if k in sub}
    row['problems'] += sub['problems']
    row['warnings'] += sub['warnings']
    row['status'] = 'FAIL' if row['problems'] else 'WARN' if row['warnings'] else 'PASS'
    return row


def summary_table(rows):
    lines = [f"{'backup':50} {'kind':13} {'size':>9} {'restore':>8} {'check':>7} {'upgrade':>8} {'server':>8}  verdict"]
    for r in rows:
        steps = r.get('steps', {})
        server = r.get('server') or (r.get('database') or {}).get('server') or {}
        cells = [r['name'][:50], r['kind'], dbsafe.human_bytes(r.get('bytes', 0)),
                 f"{steps.get('restore_s', '-')}s", f"{steps.get('integrity_s', '-')}s",
                 f"{steps.get('upgrade_s', '-')}s",
                 f"{server.get('seconds_to_answer', '-')}s" if server else 'skipped']
        lines.append(f'{cells[0]:50} {cells[1]:13} {cells[2]:>9} {cells[3]:>8} {cells[4]:>7} {cells[5]:>8} {cells[6]:>8}  '
                     f"{r.get('status', '?')}" + (' - ' + '; '.join((r['problems'] + r['warnings'])[:2]) if
                                                   r['problems'] or r['warnings'] else ''))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', type=Path, default=REPO / 'data', help='data folder to search (read only)')
    parser.add_argument('--path', type=Path, action='append', default=[], help='a specific backup file or bundle')
    parser.add_argument('--only', help='regex on the file name')
    parser.add_argument('--scratch', type=Path, help='work folder (same volume as the backups for instant clones)')
    parser.add_argument('--quick', action='store_true', help='quick_check instead of the full integrity_check')
    parser.add_argument('--serve', action='store_true',
                        help='also start the server on the restored copy of the newest backup of each kind')
    parser.add_argument('--port', type=int, default=9120, help='first port for --serve (use 9120-9129)')
    parser.add_argument('--python', default=sys.executable, help='interpreter for --serve')
    parser.add_argument('--allow-copy', action='store_true', help='allow full copies when cloning is impossible')
    parser.add_argument('--list', action='store_true', help='only list what would be tested')
    parser.add_argument('--keep-scratch', action='store_true')
    parser.add_argument('--json', type=Path, help='write the full report here')
    args = parser.parse_args(argv)

    candidates, ignored = discover(args.data)
    for extra in args.path:
        extra = extra.resolve()
        kind = 'bundle' if extra.is_dir() else classify(extra.name) or 'checkpoint'
        candidates.append({'path': str(extra), 'kind': kind, 'bytes': extra.stat().st_size if extra.is_file() else 0,
                           'mtime': extra.stat().st_mtime, 'sidecars': [p.name for p in dbsafe.sidecars(extra)]})
    if args.only:
        candidates = [c for c in candidates if re.search(args.only, Path(c['path']).name)]
    if not candidates:
        print('no backups found', file=sys.stderr)
        return 1
    if args.list:
        for c in candidates:
            print(f"{c['kind']:13} {dbsafe.human_bytes(c['bytes']):>9}  {Path(c['path']).name}"
                  + (f"  (+{', '.join(c['sidecars'])})" if c['sidecars'] else ''))
        for item in ignored:
            print(f"ignored       {Path(item['path']).name}: {item['why']}")
        return 0
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix='fl-restore-'))
    scratch.mkdir(parents=True, exist_ok=True)
    needed = max((c['bytes'] for c in candidates), default=0) * 2 + 2 * 1024 ** 3
    if dbsafe.free_bytes(scratch) < needed:
        print(f'not enough free space in {scratch} ({dbsafe.human_bytes(dbsafe.free_bytes(scratch))} free)',
              file=sys.stderr)
        return 1
    expected = expected_schema()
    newest = {}
    for candidate in candidates:
        if candidate['kind'] not in newest or candidate['mtime'] > newest[candidate['kind']]['mtime']:
            newest[candidate['kind']] = candidate
    to_serve = {c['path'] for c in newest.values()} if args.serve else set()
    rows, served = [], 0
    try:
        for candidate in candidates:
            port = args.port + served if candidate['path'] in to_serve else 0
            if candidate['kind'] == 'bundle':
                row = check_bundle(candidate, scratch, expected, serve_port=port, python=args.python)
            else:
                row = check_database(candidate, scratch, expected, full=not args.quick, serve_port=port,
                                     python=args.python, allow_copy=args.allow_copy)
            served += 1 if row.get('server') or (row.get('database') or {}).get('server') else 0
            rows.append(row)
            print(f"{row['status']:4} {row['kind']:13} {row['name']}", flush=True)
            shutil.rmtree(scratch / re.sub(r'[^A-Za-z0-9._-]', '_', row['name']), ignore_errors=True)
    finally:
        if not args.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    print()
    print(summary_table(rows))
    for item in ignored:
        print(f"ignored: {item['path']} ({item['why']})")
    totals = {s: sum(1 for r in rows if r.get('status') == s) for s in ('PASS', 'WARN', 'FAIL')}
    print(f"\n{totals['PASS']} pass, {totals['WARN']} warn, {totals['FAIL']} fail of {len(rows)}")
    if args.json:
        dbsafe.write_json_atomic(args.json, {'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'rows': rows,
                                             'ignored': ignored, 'totals': totals})
    return 1 if totals['FAIL'] else 0


if __name__ == '__main__':
    sys.exit(main())
