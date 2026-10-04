#!/usr/bin/env python3
"""Schema upgrade with a dry run and a verified rollback.

There is no separate migration step: db.init() upgrades the schema every time a server opens the
database (new tables and columns, one-time backfills, derived-table rebuilds). This tool runs that
same step off to the side first, and, when asked, behind a verified backup it can restore.

    python3 server/schema_upgrade.py --db data/leads.sqlite                    # dry run (default)
    python3 server/schema_upgrade.py --db data/leads.sqlite --apply --backup-dir data/backups
    python3 server/schema_upgrade.py --db data/leads.sqlite --rollback BACKUP --aside data/recovery

Dry run: takes a consistent snapshot of the database (SQLite backup API, safe beside the running
server), runs db.init on the snapshot only, and prints what an upgrade would create, add, rewrite
or backfill, how long it took and how much disk it needed. The real database is never written.

--apply: refuses unless the server and both LaunchAgents are stopped. Writes a pre-upgrade backup,
verifies it row for row, runs db.init, then checks integrity and that no evidence table lost a row.
If anything fails it restores the backup (the failed database is kept aside, nothing is deleted)
and exits non-zero. --rollback restores a named backup the same way.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import dbsafe  # noqa: E402


class UpgradeFailed(RuntimeError):
    def __init__(self, message, *, rolled_back, report=None):
        super().__init__(message)
        self.rolled_back = rolled_back
        self.report = report or {}


def _snapshot(source, scratch, fast_clone):
    target = Path(scratch) / 'leads.sqlite'
    if fast_clone:
        dbsafe.clone_database(source, target, allow_copy=True)
    else:
        dbsafe.backup_database(source, target)
    return target


def dry_run(source, *, scratch=None, fast_clone=False):
    """What db.init would change in `source`, measured on a private snapshot of it."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    owns_scratch = scratch is None
    scratch = Path(scratch or tempfile.mkdtemp(prefix='fl-upgrade-dry-'))
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        started = time.monotonic()
        copy = _snapshot(source, scratch, fast_clone)
        snapshot_s = round(time.monotonic() - started, 2)
        with closing(dbsafe.open_database(copy, readonly=True)) as conn:
            tables_before = set(dbsafe.table_names(conn))
            required_missing = [t for t in dbsafe.REQUIRED_TABLES if t not in tables_before]
            if required_missing:
                raise ValueError('not a Fortunate Leads database; missing tables: ' + ', '.join(required_missing))
            sig_before, cols_before = dbsafe.schema_signature(conn), dbsafe.column_map(conn)
            counts_before, settings_before = dbsafe.row_counts(conn), dbsafe.settings_keys(conn)
            pages_before = conn.execute('PRAGMA page_count').fetchone()[0]
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        size_before = copy.stat().st_size
        started = time.monotonic()
        upgraded = db.init(str(copy))
        upgraded.close()
        init_s = round(time.monotonic() - started, 2)
        with closing(dbsafe.open_database(copy, readonly=True)) as conn:
            sig_after, cols_after = dbsafe.schema_signature(conn), dbsafe.column_map(conn)
            counts_after, settings_after = dbsafe.row_counts(conn), dbsafe.settings_keys(conn)
            problems = dbsafe.check_database(conn, full=False)
            pages_after = conn.execute('PRAGMA page_count').fetchone()[0]
        wal = Path(str(copy) + '-wal')
        grown = max(pages_after - pages_before, 0) * page_size + (wal.stat().st_size if wal.exists() else 0)
        changed_rows = {t: {'before': counts_before.get(t), 'after': n} for t, n in counts_after.items()
                        if counts_before.get(t) != n}
        diff = dbsafe.schema_diff(sig_before, sig_after)
        added_columns = dbsafe.column_diff(cols_before, cols_after)
        lost = dbsafe.evidence_lost({t: n for t, n in counts_before.items() if t in dbsafe.EVIDENCE_TABLES},
                                    counts_after)
        report = {
            'database': str(source), 'source_bytes': source.stat().st_size, 'snapshot_s': snapshot_s,
            'upgrade_s': init_s,
            'extra_disk_bytes': grown,
            'would_change': bool(diff['created'] or diff['changed'] or diff['dropped'] or added_columns
                                 or changed_rows or (settings_after - settings_before)),
            'schema': diff, 'columns_added': added_columns,
            'settings_added': sorted(settings_after - settings_before),
            'rows_changed': changed_rows,
            'evidence_rows_lost': {t: list(v) for t, v in lost.items()},
            'integrity_after': problems or 'ok',
            'size_before_bytes': size_before,
        }
        report['safe'] = not lost and not problems
        return report
    finally:
        if owns_scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def apply_upgrade(path, backup_dir, *, check_open=True, inject_failure=False, aside=None):
    """Backup, upgrade, verify; restore the backup if anything goes wrong. Returns a report."""
    path, backup_dir = Path(path), Path(backup_dir)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    group = [path, *(Path(str(path) + s) for s in ('-wal', '-shm', '-journal'))]
    if check_open and dbsafe.database_in_use(group):
        raise UpgradeFailed('the database is open in another process; stop the server and both LaunchAgents first',
                            rolled_back=False)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = backup_dir / f'pre-upgrade-{stamp}-{os.getpid()}.sqlite'
    started = time.monotonic()
    info = dbsafe.backup_database(path, backup)
    verification = dbsafe.verify_backup(backup)
    if not verification['ok'] or verification.get('counts') != info['counts']:
        raise UpgradeFailed('the pre-upgrade backup did not verify; nothing was changed', rolled_back=False,
                            report={'backup': str(backup), 'problems': verification['problems']})
    with closing(dbsafe.open_database(path, readonly=True)) as conn:
        before = dbsafe.evidence_counts(conn)
        sig_before = dbsafe.schema_signature(conn)
    if before != info['counts']:
        raise UpgradeFailed('database changed while it was being backed up; stop every writer and retry',
                            rolled_back=False, report={'backup': str(backup)})
    try:
        upgraded = db.init(str(path))
        upgraded.close()
        if inject_failure:
            raise RuntimeError('injected failure after the upgrade (test hook)')
        with closing(dbsafe.open_database(path, readonly=True)) as conn:
            problems = dbsafe.check_database(conn, full=False)
            after = dbsafe.evidence_counts(conn)
            sig_after = dbsafe.schema_signature(conn)
        if problems:
            raise RuntimeError('integrity check failed after the upgrade: ' + '; '.join(problems[:3]))
        lost = dbsafe.evidence_lost(before, after)
        if lost:
            raise RuntimeError(f'the upgrade lost rows: {lost}')
    except BaseException as exc:
        aside_dir = Path(aside) if aside else backup_dir / 'failed-upgrades'
        try:
            restored = dbsafe.restore_database(backup, path, aside_dir, check_open=False)
        except Exception as restore_exc:  # noqa: BLE001 - the operator needs both failures
            raise UpgradeFailed(f'upgrade failed ({exc}) and the automatic restore failed too ({restore_exc}); '
                                f'the verified backup is {backup}', rolled_back=False,
                                report={'backup': str(backup)}) from exc
        if restored['counts'] != before:
            raise UpgradeFailed(f'upgrade failed ({exc}); restore finished but counts differ from before; '
                                f'backup {backup}', rolled_back=False, report={'backup': str(backup)}) from exc
        raise UpgradeFailed(f'upgrade failed ({exc}); the database was restored from {backup}',
                            rolled_back=True,
                            report={'backup': str(backup), 'failed_copy': restored['aside']}) from exc
    return {'backup': str(backup), 'seconds': round(time.monotonic() - started, 2),
            'schema': dbsafe.schema_diff(sig_before, sig_after), 'evidence_counts': after}


def rollback(backup, path, aside, *, check_open=True):
    """Restore `backup` over `path`; the current files move to `aside`."""
    return dbsafe.restore_database(backup, path, aside, check_open=check_open)


def _print_dry_run(report):
    def line(label, value):
        print(f'{label:<26}{value}')
    line('database', f"{report['database']} ({dbsafe.human_bytes(report['source_bytes'])})")
    line('snapshot time', f"{report['snapshot_s']}s")
    line('upgrade time', f"{report['upgrade_s']}s (this is how long the server's start-up upgrade takes)")
    line('extra disk needed', dbsafe.human_bytes(report['extra_disk_bytes']))
    line('would change anything', 'yes' if report['would_change'] else 'no: already at the current schema')
    for kind in ('created', 'changed', 'dropped'):
        items = report['schema'][kind]
        for type_ in ('table', 'view'):
            for item in [i for i in items if i['type'] == type_]:
                line(f'  {kind} {type_}', item['name'])
        for type_ in ('index', 'trigger'):
            names = [i['name'] for i in items if i['type'] == type_]
            if names:
                line(f'  {kind} {type_}' + ('es' if type_ == 'index' else 's'), f'{len(names)} ({", ".join(names[:3])}{", ..." if len(names) > 3 else ""})')
    for table, cols in report['columns_added'].items():
        line('  columns added', f'{table}: {", ".join(cols)}')
    for key in report['settings_added']:
        line('  setting added', key)
    for table, counts in report['rows_changed'].items():
        line('  rows changed', f"{table}: {counts['before']} -> {counts['after']}")
    line('evidence rows lost', report['evidence_rows_lost'] or 'none')
    line('integrity after', report['integrity_after'])
    line('verdict', 'safe to apply' if report['safe'] else 'NOT safe: do not apply')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=Path(__file__).resolve().parents[1] / 'data' / 'leads.sqlite')
    parser.add_argument('--apply', action='store_true', help='upgrade for real (server must be stopped)')
    parser.add_argument('--backup-dir', type=Path, help='where the pre-upgrade backup goes (required with --apply)')
    parser.add_argument('--rollback', type=Path, metavar='BACKUP', help='restore this backup over --db')
    parser.add_argument('--aside', type=Path, help='where replaced files go on rollback (default: next to the backup)')
    parser.add_argument('--fast-clone', action='store_true',
                        help='dry run on an APFS clone instead of a backup-API snapshot (only for a stopped database)')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--inject-failure', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            aside = args.aside or args.rollback.parent / 'recovery'
            result = rollback(args.rollback, args.db, aside)
            print(json.dumps(result, indent=2))
            return 0
        if args.apply:
            if not args.backup_dir:
                parser.error('--apply needs --backup-dir')
            result = apply_upgrade(args.db, args.backup_dir, aside=args.aside, inject_failure=args.inject_failure)
            print(json.dumps(result, indent=2))
            return 0
        report = dry_run(args.db, fast_clone=args.fast_clone)
    except UpgradeFailed as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        if exc.report:
            print(json.dumps(exc.report, indent=2), file=sys.stderr)
        return 3 if exc.rolled_back else 2
    except (dbsafe.RestoreError, ValueError, OSError) as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_dry_run(report)
    return 0 if report['safe'] else 1


if __name__ == '__main__':
    sys.exit(main())
