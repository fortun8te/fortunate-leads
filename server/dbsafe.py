"""Database safety helpers shared by the backup, restore, upgrade, health and update tools.

Stdlib only, Python 3.9+. Nothing in here opens the live database for writing on its own:
callers pass a clone, a backup or a database that is already stopped. Reads of a live database
use the SQLite backup API (one consistent snapshot, safe beside the running server).
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

# Tables the server cannot start without. Same set as ops/backup.sh and ops/backup-workspace.py.
REQUIRED_TABLES = ('people', 'seeds', 'lists', 'edges', 'accounts', 'settings')

# The least a file must hold to count as a Fortunate Leads backup that can be restored. Older backups
# predate the accounts and settings tables; the server adds those when it opens the restored file.
CORE_TABLES = ('people', 'seeds', 'lists', 'edges')

# User-entered or collected evidence. A schema upgrade or a restore may add rows; it must never lose
# these. (Derived tables such as lead_rank, laya_queue or map_* are rebuilt and are not compared.)
EVIDENCE_TABLES = ('people', 'seeds', 'lists', 'edges', 'marks', 'owner_context', 'accounts', 'jobs',
                   'list_members', 'verdicts', 'laya', 'followups', 'activity', 'saved_views', 'tag_rules')

# Child rows that point at people(id). The schema declares no foreign keys, so these are checked here.
ORPHAN_CHECKS = (
    ('edges_without_person', 'edges', 'person_id'),
    ('tags_without_person', 'tags', 'person_id'),
    ('marks_without_person', 'marks', 'person_id'),
    ('verdicts_without_person', 'verdicts', 'person_id'),
    ('owner_context_without_person', 'owner_context', 'person_id'),
    ('list_members_without_person', 'list_members', 'person_id'),
    ('edge_evidence_without_person', 'edge_evidence', 'person_id'),
    ('laya_without_person', 'laya', 'person_id'),
)


class RestoreError(RuntimeError):
    """A backup could not be verified or put in place; the original files are back where they were."""


def human_bytes(value):
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024


def free_bytes(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def open_database(path, *, readonly=False, timeout=30.0):
    """A plain connection. `readonly` adds query_only so a check can never write by accident."""
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
    if readonly:
        conn.execute('PRAGMA query_only=ON')
    return conn


def table_names(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def row_counts(conn, tables=None):
    present = set(table_names(conn))
    out = {}
    for table in (tables if tables is not None else sorted(present)):
        if table in present:
            out[table] = conn.execute('SELECT count(*) FROM "%s"' % table.replace('"', '""')).fetchone()[0]
    return out


def schema_signature(conn):
    """name -> (type, whitespace-normalised SQL) for every table, index, trigger and view."""
    rows = conn.execute("SELECT type, name, coalesce(sql,'') FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    return {name: (kind, re.sub(r'\s+', ' ', sql).strip()) for kind, name, sql in rows}


def column_map(conn):
    return {t: [r[1] for r in conn.execute('PRAGMA table_info("%s")' % t.replace('"', '""'))]
            for t in table_names(conn)}


def schema_diff(before, after):
    """Compare two schema_signature dicts: what an upgrade created, dropped or rewrote."""
    created = sorted(n for n in after if n not in before)
    dropped = sorted(n for n in before if n not in after)
    changed = sorted(n for n in after if n in before and after[n] != before[n])
    return {'created': [{'name': n, 'type': after[n][0]} for n in created],
            'dropped': [{'name': n, 'type': before[n][0]} for n in dropped],
            'changed': [{'name': n, 'type': after[n][0]} for n in changed]}


def column_diff(before, after):
    added = {t: [c for c in cols if c not in before.get(t, [])] for t, cols in after.items() if t in before}
    return {t: cols for t, cols in added.items() if cols}


def settings_keys(conn):
    if 'settings' not in set(table_names(conn)):
        return set()
    return {r[0] for r in conn.execute('SELECT key FROM settings')}


def check_database(conn, *, full=True):
    """SQLite's own check. Returns the problem lines, or [] when the database is sound."""
    pragma = 'integrity_check' if full else 'quick_check'
    rows = [r[0] for r in conn.execute('PRAGMA ' + pragma)]
    return [] if rows == ['ok'] else rows[:20]


def logical_orphans(conn):
    """Child rows whose person is gone. Cheap: each check is one indexed anti-join."""
    present = set(table_names(conn))
    if 'people' not in present:
        return {}
    found = {}
    for name, table, column in ORPHAN_CHECKS:
        if table in present:
            found[name] = conn.execute(
                f'SELECT count(*) FROM "{table}" c WHERE c."{column}" IS NOT NULL AND NOT EXISTS '
                f'(SELECT 1 FROM people p WHERE p.id=c."{column}")').fetchone()[0]
    return found


def evidence_counts(conn):
    return row_counts(conn, EVIDENCE_TABLES)


def evidence_lost(before, after):
    """Evidence tables whose row count fell. A table missing after is a loss too."""
    return {t: (n, after.get(t)) for t, n in before.items() if after.get(t) is None or after[t] < n}


def sidecars(path):
    path = str(path)
    return [Path(path + s) for s in ('-wal', '-shm', '-journal') if os.path.lexists(path + s)]


def database_in_use(paths):
    """Whether any process has one of these files open. Unknown (no lsof) counts as in use."""
    paths = [str(p) for p in paths if os.path.lexists(str(p))]
    if not paths:
        return False
    lsof = '/usr/sbin/lsof' if os.path.exists('/usr/sbin/lsof') else shutil.which('lsof')
    if not lsof:
        return True
    result = subprocess.run([lsof, '-t', '--', *paths], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        return True
    return bool(result.stdout.strip())


def clone_file(source, target, *, allow_copy=False):
    """APFS copy-on-write clone: instant and takes no space until either side changes.

    Returns 'clone' or 'copy'. A plain copy is only made with allow_copy and enough free space
    (the file plus 2 GB), so a scratch run can never fill the disk.
    """
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(str(target))
    if os.uname().sysname == 'Darwin':
        if subprocess.run(['/bin/cp', '-c', str(source), str(target)], capture_output=True).returncode == 0:
            return 'clone'
        target.unlink(missing_ok=True)
    if not allow_copy:
        raise OSError(f'cannot clone {source} (needs APFS on one volume); pass allow_copy to make a full copy')
    size = source.stat().st_size
    if free_bytes(target.parent) < size + 2 * 1024 ** 3:
        raise OSError(f'not enough free space for a full copy of {source} ({human_bytes(size)})')
    shutil.copyfile(source, target)
    return 'copy'


def clone_database(source, target, *, allow_copy=False):
    """Clone a database file that nothing is writing, plus a non-empty WAL beside it.

    Only valid for a backup or a stopped database. For a live one use backup_database.
    """
    method = clone_file(source, target, allow_copy=allow_copy)
    wal = Path(str(source) + '-wal')
    if wal.exists() and wal.stat().st_size > 0:
        clone_file(wal, str(target) + '-wal', allow_copy=allow_copy)
    return method


def backup_database(source, target, *, timeout=60.0):
    """Consistent snapshot of a database that may be live (SQLite backup API), single-file result.

    Mirrors ops/backup.sh: the copy is switched to DELETE journal mode so it has no sidecars,
    then verified. Returns {'seconds', 'bytes', 'counts'}.
    """
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(str(target))
    started = time.monotonic()
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=timeout)) as src:
            src.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
            with closing(sqlite3.connect(str(target))) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise sqlite3.DatabaseError('backup is not a single-file database')
                problems = check_database(dst, full=False)
                if problems:
                    raise sqlite3.DatabaseError('backup failed its check: ' + '; '.join(problems[:3]))
                counts = row_counts(dst, EVIDENCE_TABLES)
    except BaseException:
        for leftover in (target, *sidecars(target)):
            Path(leftover).unlink(missing_ok=True)
        raise
    for extra in sidecars(target):
        extra.unlink(missing_ok=True)
    os.chmod(target, 0o600)
    fsync_path(target)
    return {'seconds': round(time.monotonic() - started, 2), 'bytes': target.stat().st_size, 'counts': counts}


def fsync_path(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_backup(path, *, full=False):
    """Open a backup the safe way (on a private clone, never the original) and report on it."""
    import tempfile
    path = Path(path)
    report = {'path': str(path), 'ok': False, 'problems': [], 'bytes': path.stat().st_size if path.exists() else 0}
    if not path.is_file():
        report['problems'].append('missing')
        return report
    # Always the system temp directory (same APFS volume, so the clone is instant): never write beside the original.
    with tempfile.TemporaryDirectory(prefix='fl-verify-') as tmp:
        work = Path(tmp) / 'check.sqlite'
        try:
            clone_database(path, work, allow_copy=True)
            with closing(open_database(work, readonly=True)) as conn:
                report['journal_mode'] = conn.execute('PRAGMA journal_mode').fetchone()[0]
                report['problems'] += check_database(conn, full=full)
                tables = set(table_names(conn))
                missing = [t for t in CORE_TABLES if t not in tables]
                if missing:
                    report['problems'].append('missing tables: ' + ', '.join(missing))
                else:
                    report['counts'] = evidence_counts(conn)
        except (sqlite3.Error, OSError) as exc:
            report['problems'].append(str(exc))
    report['ok'] = not report['problems']
    return report


def restore_database(backup, target, aside_dir, *, check_open=True, stamp=None):
    """Put a verified backup in place of `target`. Never deletes: the old files move to `aside_dir`.

    Steps: verify the backup on a clone, refuse if any process has the target open, move the
    target and its sidecars aside (same volume, instant), clone the backup into place with mode
    600, verify the result (check plus evidence counts equal to the backup), and on ANY failure
    move the originals back. Returns {'aside', 'counts', 'seconds'}.
    """
    started = time.monotonic()
    backup, target, aside_dir = Path(backup), Path(target), Path(aside_dir)
    report = verify_backup(backup)
    if not report['ok']:
        raise RestoreError('backup failed verification: ' + '; '.join(report['problems']))
    group = [target, *(Path(str(target) + s) for s in ('-wal', '-shm', '-journal'))]
    existing = [p for p in group if os.path.lexists(p)]
    if check_open and database_in_use(existing):
        raise RestoreError('the database is open in another process; stop the server and both LaunchAgents first')
    stamp = stamp or time.strftime('%Y%m%d-%H%M%S')
    aside = aside_dir / f'restore-aside-{stamp}-{os.getpid()}'
    staged = target.with_name(target.name + '.restoring')
    moved, placed = [], []
    try:
        if existing:
            aside.mkdir(parents=True, mode=0o700)
            for item in existing:
                os.rename(item, aside / item.name)
                moved.append(item)
        for leftover in (staged, *sidecars(staged)):
            Path(leftover).unlink(missing_ok=True)
        clone_database(backup, staged, allow_copy=True)
        os.chmod(staged, 0o600)
        with closing(open_database(staged)) as conn:
            problems = check_database(conn, full=False)
            if problems:
                raise RestoreError('restored file failed its check: ' + '; '.join(problems[:3]))
            counts = evidence_counts(conn)
        if counts != report['counts']:
            raise RestoreError('restored row counts differ from the backup')
        for extra in sidecars(staged):
            if extra.name.endswith('-wal') and extra.stat().st_size > 0:
                continue                      # unreplayed WAL belongs with its database
            extra.unlink()
        os.replace(staged, target)
        placed.append(target)
        for extra in sidecars(staged):
            os.replace(extra, str(target) + extra.name[len(staged.name):])
            placed.append(Path(str(target) + extra.name[len(staged.name):]))
    except BaseException:
        for leftover in (staged, *sidecars(staged)):
            Path(leftover).unlink(missing_ok=True)
        for item in placed:
            Path(item).unlink(missing_ok=True)
        for item in moved:
            os.rename(aside / item.name, item)
        raise
    return {'aside': str(aside) if moved else None, 'counts': counts,
            'seconds': round(time.monotonic() - started, 2)}


def sha256_file(path, block=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(block), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with open(tmp, 'w', encoding='utf-8') as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
