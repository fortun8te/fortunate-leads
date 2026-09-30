"""Prepared exact default tag facets; filtered requests retain their SQL relation.

Rows retain each projection branch and nullable group. Small totals are updated
in the same transaction as source edits. Owner exclusion is applied at read time.
Installing the schema never backfills an existing database.
"""
import argparse
import sqlite3

import tag_projection

TABLES = ('tag_facet_rows', 'tag_facet_totals')
INDEX = 'tag_facet_person'
# Bump when projection semantics or sync rules change; existing copies then
# require explicit preparation before the fast path is used again.
SCHEMA = '1'
TRIGGERS = tuple(f'tag_facet_sync_{table}_{op}' for table in
                 ('tags', 'verdicts', 'marks', 'owner_context', 'people')
                 for op in ('insert', 'update', 'delete')) + ('tag_facet_count_insert', 'tag_facet_count_delete')


def _installed(conn, kind, names):
    return conn.execute('SELECT count(*) FROM sqlite_master WHERE type=? AND name IN (' +
                        ','.join('?' for _ in names) + ')', [kind, *names]).fetchone()[0] == len(names)


def _rows(predicate=None):
    branches = []
    for branch, relation in enumerate(tag_projection.parts()):
        # quote distinguishes NULL from text and escapes separators inside values.
        key = f"{branch}||':'||quote(t.tag)||':'||quote(t.source)||':'||quote(t.grp)"
        eligible = ("EXISTS(SELECT 1 FROM people p LEFT JOIN marks m ON m.person_id=p.id "
                    "WHERE p.id=t.person_id AND coalesce(m.status,'')!='no')")
        branches.append(f'SELECT t.person_id,{branch},t.tag,t.source,t.grp,{key},{eligible} '
                        f'FROM ({relation}) t' + (f' WHERE t.person_id IS {predicate}' if predicate else ''))
    return ' UNION ALL '.join(branches)


def ensure(conn):
    installed = _installed(conn, 'table', TABLES)
    version = conn.execute("SELECT value FROM settings WHERE key='tag_facets_schema'").fetchone()
    if installed and (not version or version[0] != SCHEMA):
        for name in TRIGGERS:
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        conn.execute("UPDATE settings SET value='false' WHERE key='tag_facets_ready'")
    if installed and (not _installed(conn, 'trigger', TRIGGERS) or not _installed(conn, 'index', (INDEX,))):
        conn.execute("UPDATE settings SET value='false' WHERE key='tag_facets_ready'")
    conn.execute('CREATE TABLE IF NOT EXISTS tag_facet_rows(person_id INTEGER,branch INTEGER NOT NULL, '
                 'tag TEXT,source TEXT,grp TEXT,facet_key TEXT NOT NULL,eligible INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS tag_facet_totals(facet_key TEXT PRIMARY KEY,branch INTEGER NOT NULL, '
                 'tag TEXT,source TEXT,grp TEXT,n INTEGER NOT NULL,n_open INTEGER NOT NULL)')
    if not installed:
        empty = all(conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is None
                    for table in ('people', 'tags', 'verdicts', 'owner_context'))
        conn.execute("INSERT OR REPLACE INTO settings VALUES('tag_facets_ready',?)", ('true' if empty else 'false',))
    conn.execute("INSERT OR REPLACE INTO settings VALUES('tag_facets_schema',?)", (SCHEMA,))
    if not _installed(conn, 'index', (INDEX,)) and conn.execute('SELECT 1 FROM tag_facet_rows LIMIT 1').fetchone() is None:
        conn.execute(f'CREATE INDEX {INDEX} ON tag_facet_rows(person_id)')
    for table, column, fields in (
            ('tags', 'person_id', ('person_id', 'tag', 'source', 'grp')),
            ('verdicts', 'person_id', ('person_id', 'tier', 'content_fit', 'role')),
            ('marks', 'person_id', ('person_id', 'status')),
            ('owner_context', 'person_id', ('person_id', 'relationships')),
            ('people', 'id', ('id',))):
        for op in ('insert', 'update', 'delete'):
            refs = ('OLD', 'NEW') if op == 'update' else (('OLD',) if op == 'delete' else ('NEW',))
            # Unprepared copies require an explicit full preparation. Avoid
            # accumulating partial rows or scanning after a dropped person index.
            conditions = ["EXISTS(SELECT 1 FROM settings WHERE key='tag_facets_ready' AND value='true')",
                          f"EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='{INDEX}')"]
            if op == 'update':
                conditions.append('(' + ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields) + ')')
            when = ' WHEN ' + ' AND '.join(conditions)
            body = ''
            for ref in refs:
                pid = f'{ref}.{column}'
                body += f'DELETE FROM tag_facet_rows WHERE person_id IS {pid};'
                body += 'INSERT INTO tag_facet_rows ' + _rows(pid) + ';'
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS tag_facet_sync_{table}_{op} AFTER {op.upper()} ON {table}{when} BEGIN {body} END')
    conn.execute('CREATE TRIGGER IF NOT EXISTS tag_facet_count_insert AFTER INSERT ON tag_facet_rows BEGIN '
                 'INSERT INTO tag_facet_totals VALUES(NEW.facet_key,NEW.branch,NEW.tag,NEW.source,NEW.grp,1,NEW.eligible) '
                 'ON CONFLICT(facet_key) DO UPDATE SET n=n+1,n_open=n_open+NEW.eligible; END')
    conn.execute('CREATE TRIGGER IF NOT EXISTS tag_facet_count_delete AFTER DELETE ON tag_facet_rows BEGIN '
                 'UPDATE tag_facet_totals SET n=n-1,n_open=n_open-OLD.eligible WHERE facet_key=OLD.facet_key; '
                 'DELETE FROM tag_facet_totals WHERE facet_key=OLD.facet_key AND n=0; END')


def ready(conn):
    return (_installed(conn, 'table', TABLES) and _installed(conn, 'index', (INDEX,))
            and _installed(conn, 'trigger', TRIGGERS)
            and conn.execute("SELECT 1 FROM settings WHERE key='tag_facets_schema' AND value=?", (SCHEMA,)).fetchone() is not None
            and conn.execute("SELECT 1 FROM settings WHERE key='tag_facets_ready' AND value='true'").fetchone() is not None)


def prepare(conn):
    """Atomic explicit preparation, including on autocommit connections."""
    conn.execute('SAVEPOINT tag_facets_prepare')
    try:
        ensure(conn)
        conn.execute("UPDATE settings SET value='false' WHERE key='tag_facets_ready'")
        for op in ('insert', 'delete'):
            conn.execute(f'DROP TRIGGER IF EXISTS tag_facet_count_{op}')
        conn.execute('DELETE FROM tag_facet_rows')
        conn.execute('DELETE FROM tag_facet_totals')
        conn.execute(f'DROP INDEX IF EXISTS {INDEX}')
        conn.execute('INSERT INTO tag_facet_rows ' + _rows())
        conn.execute(f'CREATE INDEX {INDEX} ON tag_facet_rows(person_id)')
        conn.execute('INSERT INTO tag_facet_totals SELECT facet_key,branch,tag,source,grp,count(*),sum(eligible) '
                     'FROM tag_facet_rows GROUP BY facet_key')
        ensure(conn)
        conn.execute("UPDATE settings SET value='true' WHERE key='tag_facets_ready'")
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT tag_facets_prepare')
        conn.execute('RELEASE SAVEPOINT tag_facets_prepare')
        raise
    conn.execute('RELEASE SAVEPOINT tag_facets_prepare')


def default(conn):
    if not ready(conn):
        return None
    # Totals include dangling and owner rows. Counts exclude missing/no people;
    # the few current owner IDs are subtracted without rewriting the projection.
    import lead_rank
    excluded = lead_rank.owner_ids(conn)
    subtract = {}
    if excluded:
        for branch, tag, source, n in conn.execute(
                'SELECT branch,tag,source,sum(eligible) FROM tag_facet_rows WHERE person_id IN (' +
                ','.join('?' for _ in excluded) + ') GROUP BY branch,tag,source', excluded):
            subtract[branch, tag, source] = n
    null_owner = conn.execute('SELECT 1 FROM seeds WHERE is_me=1 AND handle IS NULL LIMIT 1').fetchone() is not None
    out = {}
    for branch, tag, source, grp, total, n in conn.execute(
            'SELECT branch,tag,source,min(grp),sum(n),sum(n_open) FROM tag_facet_totals '
            'GROUP BY branch,tag,source ORDER BY branch'):
        count = 0 if null_owner else n - subtract.get((branch, tag, source), 0)
        item = out.setdefault((tag, source), {'tag': tag, 'source': source, 'grp': grp, 'count': 0, 'total': 0})
        # The original loop overwrites the group with the last present branch.
        item['grp'] = grp
        item['count'] += count
        item['total'] += total
    return sorted(out.values(), key=lambda item: (-item['count'], -item['total'], item['tag'], item['source']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Prepare exact default tag facets on a stopped database copy.')
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.db) as conn:
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute('PRAGMA cache_size=-32768')
        prepare(conn)
