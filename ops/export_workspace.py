#!/usr/bin/env python3
"""Export a complete, consistent, read-only snapshot of the leads workspace.

JSON keeps SQLite nulls and numbers intact. Optional CSV files are for spreadsheet
review; text that could be interpreted as a spreadsheet formula is escaped.

Usage:
    python3 ops/export_workspace.py --db data/workspace.sqlite --output snapshot.json
    python3 ops/export_workspace.py --db data/workspace.sqlite --bundle-dir exports/run-001

Bundles contain workspace.json, one CSV per included dataset, and manifest.json
with row counts and SHA-256 checksums. Choose a new bundle directory each time.
CSV blank numeric cells mean unknown; 0 is a known zero. JSON retains exact nulls.
The export contains private people and review data. Keep it in a private location.
Optional datasets absent from older databases are omitted, not reported as empty.

"""

import argparse
import csv
import json
import hashlib
import shutil
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


CONNECTION_COLUMNS = (
    'seed', 'seed_ig_id', 'person_id', 'person_ig_id', 'person_handle',
    'direction', 'first_seen', 'evidence_state', 'active', 'observed_at', 'checked_at',
)
CONNECTION_QUERY = """
    SELECT e.seed, s.ig_id AS seed_ig_id, e.person_id,
           p.ig_id AS person_ig_id, p.handle AS person_handle,
           e.direction, e.first_seen,
           CASE WHEN v.active=1 THEN 'observed'
                WHEN v.active=0 THEN 'absent'
                ELSE 'unverified' END AS evidence_state,
           v.active, v.observed_at, v.checked_at
    FROM edges e
    LEFT JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id
                             AND v.direction=e.direction
    LEFT JOIN seeds s ON s.handle=e.seed
    LEFT JOIN people p ON p.id=e.person_id
    ORDER BY e.seed COLLATE NOCASE, e.direction, e.person_id
"""


# Explicit allowlist: account state, provider settings, and lease credentials
# are not part of a people/connections export.
TABLES = (
    ('profiles', 'people', 'id'), ('seeds', 'seeds', 'handle COLLATE NOCASE'),
    ('coverage', 'lists', 'seed COLLATE NOCASE, direction'),
    ('edge_evidence', 'edge_evidence', 'seed COLLATE NOCASE, direction, person_id'),
    ('list_runs', 'list_runs', 'job_id'),
    ('list_page_requests', 'list_page_requests', 'job_id, requested_cursor'),
    ('list_members', 'list_members', 'job_id, person_id'),
    ('pages', 'pages', 'job_id, cursor'),
    ('tags', 'tags', 'person_id, tag'), ('marks', 'marks', 'person_id'),
    ('verdicts', 'verdicts', 'person_id'), ('laya', 'laya', 'person_id'),
    ('site_reads', 'site_reads', 'person_id'),
    ('site_evidence', 'site_evidence', 'person_id, tag'),
    ('tag_rules', 'tag_rules', 'id'), ('saved_views', 'saved_views', 'id'),
)
JOB_COLUMNS = ('id', 'kind', 'seed', 'direction', 'handle', 'state',
               'attempts', 'created_at')


def _datasets(conn, found):
    datasets = []
    for key, table, order in TABLES:
        if table in found:
            datasets.append((key, _columns(conn, table), f'SELECT * FROM {table} ORDER BY {order}'))
    datasets.append(('connections', CONNECTION_COLUMNS, CONNECTION_QUERY))
    if 'jobs' in found:
        columns = [c for c in JOB_COLUMNS if c in _columns(conn, 'jobs')]
        datasets.append(('jobs', columns, 'SELECT ' + ','.join(columns) + ' FROM jobs ORDER BY id'))
    return datasets


def _columns(conn, table):
    return [row['name'] for row in conn.execute(f'PRAGMA table_info({table})')]


def _safe_cell(value):
    if value is None:
        return ''
    if isinstance(value, str):
        # Excel and Sheets may treat text beginning with these characters as a
        # formula, even when quoted by the CSV writer.
        if value.lstrip('\ufeff \t\r\n\v\f').startswith(('=', '+', '-', '@')) or value[:1] in ('\t', '\r', '\n'):
            return "'" + value
    return value


def _temporary_path(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{destination.name}.', suffix='.tmp', dir=destination.parent)
    return fd, Path(name)


def _write_json(conn, path, exported_at, counts, datasets):
    fd, temporary = _temporary_path(path)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as output:
            output.write('{\n  "format": "fortunate-leads-workspace-v2",\n')
            output.write('  "exported_at": ' + json.dumps(exported_at) + ',\n')
            output.write('  "counts": ' + json.dumps(counts, sort_keys=True) + ',\n')
            for position, (key, columns, query) in enumerate(datasets):
                output.write(f'  "{key}": [')
                rows_written = 0
                for index, row in enumerate(conn.execute(query)):
                    output.write(',\n    ' if index else '\n    ')
                    json.dump(dict(row), output, ensure_ascii=False, allow_nan=False)
                    rows_written += 1
                # Keep an empty array compact and a nonempty array readable.
                output.write('\n  ]' if rows_written else ']')
                output.write(',\n' if position != len(datasets) - 1 else '\n')
            output.write('}\n')
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_csv(conn, path, columns, query):
    fd, temporary = _temporary_path(path)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            writer.writerow([_safe_cell(column) for column in columns])
            for row in conn.execute(query):
                writer.writerow([_safe_cell(row[column]) for column in columns])
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def export_workspace(database, output=None, csv_dir=None, *, bundle_dir=None):
    """Stream one read transaction to atomic JSON or a new atomic bundle directory.

    A bundle is immutable. Pick a fresh destination for each snapshot, so readers
    can never observe CSV files from different generations. Arbitrary independent
    output paths cannot be atomically published together and are rejected.
    """
    if csv_dir is not None:
        raise ValueError('Use bundle_dir (CLI --bundle-dir) for consistent JSON and CSV output')
    if (output is None) == (bundle_dir is None):
        raise ValueError('Provide exactly one of output or bundle_dir')
    database = Path(database).expanduser().resolve(strict=True)
    bundle = Path(bundle_dir).expanduser().absolute() if bundle_dir is not None else None
    destination = bundle if bundle is not None else Path(output).expanduser().absolute()
    for protected in (database, *(Path(str(database) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        if destination.resolve() == protected or (destination.exists() and protected.exists()
                                                  and os.path.samefile(destination, protected)):
            raise ValueError('output must not be the database or its SQLite companion files')
    if bundle is not None and (bundle.exists() or bundle.is_symlink()):
        raise FileExistsError('Bundle destination already exists; choose a new directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{bundle.name}.', dir=bundle.parent)) if bundle else None
    output = staging / 'workspace.json' if staging else destination
    conn = None
    pending = []
    try:
        conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('BEGIN')
        required = {'people', 'seeds', 'lists', 'edges', 'edge_evidence'}
        found = {row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required - found
        if missing:
            raise ValueError('database lacks required tables: ' + ', '.join(sorted(missing)))
        datasets = _datasets(conn, found)
        counts = {key: conn.execute('SELECT count(*) FROM (' + query.rsplit('ORDER BY', 1)[0] + ')').fetchone()[0]
                  for key, _, query in datasets}
        exported_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        pending.append((_write_json(conn, output, exported_at, counts, datasets), output))
        if staging:
            for key, columns, query in datasets:
                target = staging / (key + '.csv')
                pending.append((_write_csv(conn, target, columns, query), target))
        conn.rollback()
        for temporary, target in pending:
            os.replace(temporary, target)
        if staging:
            files = {}
            for _, target in pending:
                digest = hashlib.sha256()
                with target.open('rb') as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
                files[target.name] = {'sha256': digest.hexdigest(), 'bytes': target.stat().st_size}
            manifest = staging / 'manifest.json'
            with manifest.open('w', encoding='utf-8') as handle:
                json.dump({'format': 'fortunate-leads-bundle-v1', 'exported_at': exported_at,
                           'counts': counts, 'files': files}, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            # Existing nonempty bundles cannot be replaced by rename. Recheck
            # also protects an empty directory created while the export ran.
            if bundle.exists() or bundle.is_symlink():
                raise FileExistsError('Bundle destination already exists; choose a new directory')
            os.rename(staging, bundle)
        return counts
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
        if staging and staging.exists():
            shutil.rmtree(staging)
        if conn is not None:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Existing workspace SQLite database')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--output', type=Path, help='Atomically replace this JSON file')
    target.add_argument('--bundle-dir', type=Path,
                        help='Publish JSON, all CSV files and checksums together in a NEW directory')
    args = parser.parse_args()
    counts = export_workspace(args.db, args.output, bundle_dir=args.bundle_dir)
    print(f"Exported {counts['profiles']} profiles and {counts['connections']} connections to {args.bundle_dir or args.output}")


if __name__ == '__main__':
    main()
