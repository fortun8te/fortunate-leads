"""Stream saved list observations locally, never collector state or complete coverage.

The source is copied to a private temporary snapshot before any writes. Its SHA256
must match a preview. Checkpoints and page evidence are bound to bytes, not paths.
Only stable IDs, handles, direction and explicit observation times are consumed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import sqlite3
import tempfile

import db

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_BATCH = 1000


class ImportError(ValueError):
    pass


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS local_list_imports(
        sha256 TEXT PRIMARY KEY, source_name TEXT NOT NULL, byte_size INTEGER NOT NULL,
        last_line INTEGER NOT NULL DEFAULT 0, byte_offset INTEGER NOT NULL DEFAULT 0,
        members INTEGER NOT NULL DEFAULT 0, new_canonical_edges INTEGER NOT NULL DEFAULT 0,
        completed INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS local_list_import_unresolved(
        sha256 TEXT NOT NULL,ig_id TEXT NOT NULL,PRIMARY KEY(sha256,ig_id))''')


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ImportError('Account ID must be a positive numeric stable ID')
    value = str(value)
    if not re.fullmatch(r'[1-9][0-9]{0,29}', value):
        raise ImportError('Account ID must be a positive numeric stable ID')
    return value


def _handle(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', value):
        raise ImportError('Handle must be a plain Instagram handle')
    normalized = db.norm_handle(value)
    if not normalized:
        raise ImportError('Invalid Instagram handle')
    return normalized


def _identity(value):
    clean = {'ig_id': _id(value.get('ig_id'))}
    if value.get('handle') is not None:
        clean['handle'] = _handle(value['handle'])
    return clean


def _date(value):
    if not isinstance(value, str):
        raise ImportError('observed_at must include a timezone')
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ImportError('Invalid observed_at') from exc
    if date.tzinfo is None:
        raise ImportError('observed_at must include a timezone')
    date = date.astimezone(timezone.utc)
    if date > datetime.now(timezone.utc):
        raise ImportError('observed_at cannot be in the future')
    return date.isoformat()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportError('Duplicate JSON field: ' + key)
        result[key] = value
    return result


def _page(raw, line):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_keys)
        if not isinstance(value, dict):
            raise ImportError('A page must be a JSON object')
        if value.get('direction') not in ('followers', 'following'):
            raise ImportError('direction must be followers or following')
        members = value.get('members')
        if not isinstance(members, list) or len(members) > MAX_MEMBERS:
            raise ImportError(f'members must be a list of at most {MAX_MEMBERS} accounts')
        clean = []
        for member in members:
            if not isinstance(member, dict):
                raise ImportError('A member must be an object')
            clean.append(_identity(member))
        context = value.get('source_context') or {}
        if not isinstance(context, dict):
            raise ImportError('source_context must be an object')
        context = {key: val for key, val in context.items() if key in ('route', 'run_id', 'warmup', 'stop_outcome')
                   and (type(val) in (bool, int) or isinstance(val, str) and len(val) <= 500)}
        return {'owner': _identity({'ig_id': value.get('source_owner_id'), 'handle': value.get('source_owner_handle')}),
                'direction': value['direction'], 'observed_at': _date(value.get('observed_at')), 'members': clean,
                'source_context': context}
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError, OverflowError) as exc:
        raise ImportError(f'Line {line}: {exc}') from exc


@contextmanager
def _snapshot(path, expected_sha256=None):
    path = Path(path)
    if expected_sha256 is not None and not re.fullmatch(r'[a-f0-9]{64}', expected_sha256):
        raise ImportError('A preview SHA256 is required')
    with path.open('rb') as source, tempfile.TemporaryFile() as snapshot:
        descriptor = source.fileno()
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ImportError('Source must be a regular file')
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(1024 * 1024):
            snapshot.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ImportError('Source changed while making its snapshot; preview it again')
        sha = digest.hexdigest()
        if expected_sha256 is not None and sha != expected_sha256:
            raise ImportError('Source differs from the preview SHA256; no rows were imported')
        snapshot.seek(0)
        yield snapshot, sha, size, path.name


def _rows(snapshot, start_line=0):
    line = start_line
    while raw := snapshot.readline(MAX_LINE_BYTES + 1):
        line += 1
        if len(raw) > MAX_LINE_BYTES:
            raise ImportError(f'Line {line}: exceeds {MAX_LINE_BYTES} bytes')
        if not raw.strip():
            raise ImportError(f'Line {line}: blank pages are not supported')
        yield line, snapshot.tell(), _page(raw, line)


def _check_identity(conn, identity):
    """Historical aliases never displace another identity or rename live work."""
    handle, ig_id = identity.get('handle'), identity['ig_id']
    person = conn.execute('SELECT ig_id FROM people WHERE handle=?', (handle,)).fetchone()
    seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (handle,)).fetchone()
    for row in (person, seed):
        if row and db.normalize_ig_id(row['ig_id']) not in (None, ig_id):
            raise ImportError(f'Handle @{handle} belongs to another saved account')
    current = conn.execute('SELECT id,handle FROM people WHERE ig_id=?', (ig_id,)).fetchone()
    if current and '~' in current['handle']:
        raise ImportError(f'Account {ig_id} has a retired handle; resolve its identity first')
    if current:
        canonical_seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (current['handle'],)).fetchone()
        if canonical_seed and db.normalize_ig_id(canonical_seed['ig_id']) not in (None, ig_id):
            raise ImportError(f'Account {ig_id} conflicts with its saved source identity')
    return current


def _check_page(conn, page, registry=None):
    seen = {}
    for identity in [page['owner'], *page['members']]:
        _check_identity(conn, identity)
        if not identity.get('handle'):
            continue
        prior = seen.setdefault(identity['handle'], identity['ig_id'])
        if prior != identity['ig_id']:
            raise ImportError('Page assigns one handle to different account IDs')
        if registry is not None:
            row = registry.execute('SELECT ig_id FROM identities WHERE handle=?', (identity['handle'],)).fetchone()
            if row and row[0] != identity['ig_id']:
                raise ImportError('File assigns one handle to different account IDs')
            registry.execute('INSERT OR IGNORE INTO identities VALUES(?,?)', (identity['handle'], identity['ig_id']))


def _validate_snapshot(conn, snapshot):
    """Validate the exact immutable bytes completely before any import writes."""
    snapshot.seek(0)
    with tempfile.TemporaryDirectory() as directory:
        registry = sqlite3.connect(str(Path(directory) / 'identities.sqlite'))
        try:
            registry.execute('CREATE TABLE identities(handle TEXT PRIMARY KEY,ig_id TEXT NOT NULL)')
            rows = members = 0
            for line, _, page in _rows(snapshot):
                try:
                    _check_page(conn, page, registry)
                except ImportError as exc:
                    raise ImportError(f'Line {line}: {exc}') from exc
                rows += 1
                members += len(page['members'])
            return rows, members
        finally:
            registry.close()
            snapshot.seek(0)


def preview(conn, path):
    """Validate every row read-only; use a disk-backed registry to bound RAM."""
    with _snapshot(path) as (snapshot, sha, size, name):
        rows, members = _validate_snapshot(conn, snapshot)
        return {'sha256': sha, 'source_name': name, 'byte_size': size, 'pages': rows,
                'members': members, 'mode': 'preview', 'coverage': 'observed_connections_only'}


def _person(conn, identity, observed_at):
    current = _check_identity(conn, identity)
    # Do not let an old alias rename seeds, leases or a newer profile.
    return current['id'] if current else db.upsert_person(conn, identity, observed_at) if identity.get('handle') else None


def apply(conn, path, expected_sha256, batch_size=100):
    """Commit bounded chunks. Failed chunks roll back with their checkpoint.

    Caller must supply an initialized offline database and no open transaction.
    File snapshots can be renamed or relocated; identical bytes resume by digest.
    """
    if conn.in_transaction:
        raise ImportError('Import requires its own transaction')
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH:
        raise ImportError(f'batch_size must be between 1 and {MAX_BATCH}')
    if not expected_sha256:
        raise ImportError('A preview SHA256 is required')
    import acquisition
    with _snapshot(path, expected_sha256) as (snapshot, sha, size, name):
        # A caller can compute a digest without previewing. Validate all rows
        # and cross-row identities before even schema/checkpoint writes.
        _validate_snapshot(conn, snapshot)
        ensure(conn)
        acquisition.ensure(conn)
        conn.commit()
        saved = conn.execute('SELECT * FROM local_list_imports WHERE sha256=?', (sha,)).fetchone()
        if saved and saved['completed']:
            return _result(conn, saved, True)
        offset, line = (saved['byte_offset'], saved['last_line']) if saved else (0, 0)
        snapshot.seek(offset)
        batch = []
        batch_members = 0
        def commit_batch(rows, completed=False):
            try:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT * FROM local_list_imports WHERE sha256=?', (sha,)).fetchone()
                if (current['last_line'] if current else 0) != line:
                    raise ImportError('Another importer advanced this file; resume it again')
                conn.execute('INSERT OR IGNORE INTO local_list_imports(sha256,source_name,byte_size) VALUES(?,?,?)', (sha, name, size))
                added_members = added_edges = 0
                for row_number, row_offset, page in rows:
                    _check_page(conn, page)
                    acquisition.prime_existing(conn, seed_ig_id=page['owner']['ig_id'],
                        direction=page['direction'], members=page['members'])
                    owner_pid = _person(conn, page['owner'], page['observed_at'])
                    owner_row = conn.execute('SELECT handle FROM people WHERE id=?', (owner_pid,)).fetchone()
                    owner = owner_row['handle'] if owner_row else None
                    if owner:
                        conn.execute('INSERT OR IGNORE INTO seeds(handle,ig_id,added_at) VALUES(?,?,?)',
                                     (owner, page['owner']['ig_id'], page['observed_at']))
                    else:
                        conn.execute('INSERT OR IGNORE INTO local_list_import_unresolved VALUES(?,?)', (sha, page['owner']['ig_id']))
                    event_id = f'local-import:{sha}:{row_number}'
                    changed = set()
                    flipped = set()
                    for member in page['members']:
                        if member['ig_id'] == page['owner']['ig_id']:
                            continue
                        pid = _person(conn, member, page['observed_at'])
                        if pid is None:
                            conn.execute('INSERT OR IGNORE INTO local_list_import_unresolved VALUES(?,?)', (sha, member['ig_id']))
                        if pid is None or owner is None:
                            continue
                        db.add_edge(conn, owner, pid, page['direction'], page['observed_at'], observed=True, flipped=flipped)
                        changed.add(pid)
                        db.observe_edge(conn, owner, pid, page['direction'], event_id, None, page['observed_at'])
                    if flipped:
                        db.dirty_seed_members(conn, *flipped)
                    if changed:
                        db.mark_network_dirty(conn, changed | {owner_pid})
                    metrics = acquisition.record_page(conn, event_id=event_id, source_kind='local_import',
                        seed_ig_id=page['owner']['ig_id'], direction=page['direction'], members=page['members'],
                        observed_at=page['observed_at'], source_ref=json.dumps(
                            {'sha256': sha, 'filename': saved['source_name'] if saved else name, 'row': row_number, **page['source_context']}, sort_keys=True))
                    added_members += len(page['members'])
                    added_edges += metrics['new_canonical_edges']
                last_line, last_offset = (rows[-1][0], rows[-1][1]) if rows else (line, offset)
                conn.execute('UPDATE local_list_imports SET last_line=?,byte_offset=?,members=members+?, '
                             'new_canonical_edges=new_canonical_edges+?,completed=? WHERE sha256=?',
                             (last_line, last_offset, added_members, added_edges, int(completed), sha))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        for row in _rows(snapshot, line):
            batch.append(row)
            batch_members += len(row[2]['members'])
            if len(batch) == batch_size or batch_members >= 10000:
                commit_batch(batch)
                line, offset = batch[-1][:2]
                batch.clear()
                batch_members = 0
        commit_batch(batch, completed=True)
        return _result(conn, conn.execute('SELECT * FROM local_list_imports WHERE sha256=?', (sha,)).fetchone(), False)


def _result(conn, row, replayed):
    unresolved = conn.execute('SELECT count(*) FROM local_list_import_unresolved WHERE sha256=?', (row['sha256'],)).fetchone()[0]
    return dict(row, mode='apply', replayed=replayed, coverage='observed_connections_only', staged_unresolved_ids=unresolved)
