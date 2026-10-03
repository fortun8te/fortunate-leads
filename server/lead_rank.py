"""Exact, transactional ordering for the ordinary lead list.

Existing databases are prepared explicitly; installing the schema never scans
profiles. Readers fall back to the original query until preparation completes.
"""
import argparse
import sqlite3

ORDERS = {
    'fit': "CASE WHEN tier='unread' THEN 1 ELSE 0 END, (content_fit IS NULL), content_fit DESC, degree DESC, (score IS NULL), score DESC, person_id",
    'score': '(score IS NULL), score DESC, followers DESC, person_id',
    'connected': 'degree DESC, (followers IS NULL), followers DESC, person_id',
}

# Connected ordering is an optional upgrade for existing prepared databases.
# Missing it must not disable the established score and fit projections.
BASE_SORTS = ('fit', 'score')
FIT_CONDITIONS = {
    'strong': 'content_fit>=70',
    'good': 'content_fit>=45 AND content_fit<70',
    'weak': 'content_fit<45',
    'unread': 'content_fit IS NULL',
}


def ensure(conn):
    installed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lead_rank'").fetchone()
    if installed and (conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'lead_rank_%'").fetchone()[0] != 15
                      or conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('lead_rank','lead_rank_totals','lead_rank_facets')").fetchone()[0] != 3):
        conn.execute("UPDATE settings SET value='false' WHERE key='lead_rank_ready'")
    conn.execute('CREATE TABLE IF NOT EXISTS lead_rank(person_id INTEGER PRIMARY KEY, hidden INTEGER NOT NULL, '
                 'tier TEXT, content_fit REAL, degree INTEGER NOT NULL, score REAL, followers INTEGER, status TEXT, has_bio INTEGER NOT NULL DEFAULT 0)')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(lead_rank)')}
    if 'status' not in columns:
        conn.execute('ALTER TABLE lead_rank ADD COLUMN status TEXT')
        conn.execute('ALTER TABLE lead_rank ADD COLUMN has_bio INTEGER NOT NULL DEFAULT 0')
        conn.execute("UPDATE settings SET value='false' WHERE key='lead_rank_ready'")
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'lead_rank_%'").fetchall():
            conn.execute(f'DROP TRIGGER {name}')
    conn.execute('CREATE TABLE IF NOT EXISTS lead_rank_facets(tier TEXT NOT NULL,status TEXT NOT NULL, '
                 'has_bio INTEGER NOT NULL,n INTEGER NOT NULL,PRIMARY KEY(tier,status,has_bio))')
    conn.execute('CREATE TABLE IF NOT EXISTS lead_rank_totals(hidden INTEGER PRIMARY KEY, n INTEGER NOT NULL)')
    for hidden in (0, 1):
        conn.execute('INSERT OR IGNORE INTO lead_rank_totals VALUES(?,0)', (hidden,))
    if not installed:
        empty = conn.execute('SELECT 1 FROM people LIMIT 1').fetchone() is None
        conn.execute("INSERT OR REPLACE INTO settings VALUES('lead_rank_ready',?)", ('true' if empty else 'false',))
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='lead_rank'")}
    for sort, order in ORDERS.items():
        name = 'lead_rank_' + sort
        if name not in indexes:
            if conn.execute('SELECT 1 FROM lead_rank LIMIT 1').fetchone():
                if sort in BASE_SORTS:
                    conn.execute("UPDATE settings SET value='false' WHERE key='lead_rank_ready'")
            else:
                conn.execute(f'CREATE INDEX {name} ON lead_rank(hidden,{order})')
    refresh = ("INSERT INTO lead_rank SELECT p.id, EXISTS(SELECT 1 FROM marks m WHERE m.person_id=p.id AND m.status='no'), "
               "v.tier,v.content_fit,coalesce(d.degree,0),v.score,p.followers,m.status,coalesce(p.bio,'')!='' FROM people p "
               'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id LEFT JOIN map_person_degree d ON d.person_id=p.id WHERE p.id={pid} '
               'ON CONFLICT(person_id) DO UPDATE SET hidden=excluded.hidden,tier=excluded.tier,'
               'content_fit=excluded.content_fit,degree=excluded.degree,score=excluded.score,followers=excluded.followers,status=excluded.status,has_bio=excluded.has_bio;')
    for table, column, fields in (('people', 'id', ('id', 'followers', 'bio')), ('verdicts', 'person_id', ('person_id', 'tier', 'content_fit', 'score')),
                                  ('marks', 'person_id', ('person_id', 'status')), ('map_person_degree', 'person_id', ('person_id', 'degree'))):
        for op in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'lead_rank_sync_{table}_{op.lower()}'
            when = ' WHEN ' + ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields) if op == 'UPDATE' else ''
            refs = ('OLD', 'NEW') if op == 'UPDATE' else (('OLD',) if op == 'DELETE' else ('NEW',))
            body = ''
            for ref in refs:
                pid = f'{ref}.{column}'
                changed_identity = f' AND OLD.{column} IS NOT NEW.{column}' if op == 'UPDATE' and ref == 'OLD' else ''
                if table == 'people' and ref == 'OLD':
                    body += f'DELETE FROM lead_rank WHERE person_id={pid}{changed_identity};'
                query = refresh.format(pid=pid)
                if changed_identity:
                    query = query.replace(f'WHERE p.id={pid} ', f'WHERE p.id={pid}{changed_identity} ')
                body += query
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {op} ON {table}{when} BEGIN {body} END')
    increase = ("INSERT INTO lead_rank_facets VALUES(coalesce(NEW.tier,'unread'),coalesce(NEW.status,''),NEW.has_bio,1) "
                'ON CONFLICT(tier,status,has_bio) DO UPDATE SET n=n+1;')
    decrease = ("UPDATE lead_rank_facets SET n=n-1 WHERE tier=coalesce(OLD.tier,'unread') "
                "AND status=coalesce(OLD.status,'') AND has_bio=OLD.has_bio;")
    conn.execute('CREATE TRIGGER IF NOT EXISTS lead_rank_count_insert AFTER INSERT ON lead_rank BEGIN '
                 'UPDATE lead_rank_totals SET n=n+1 WHERE hidden=NEW.hidden; ' + increase + ' END')
    conn.execute('CREATE TRIGGER IF NOT EXISTS lead_rank_count_delete AFTER DELETE ON lead_rank BEGIN '
                 'UPDATE lead_rank_totals SET n=n-1 WHERE hidden=OLD.hidden; ' + decrease + ' END')
    conn.execute('CREATE TRIGGER IF NOT EXISTS lead_rank_count_update AFTER UPDATE ON lead_rank '
                 'WHEN OLD.hidden IS NOT NEW.hidden OR OLD.tier IS NOT NEW.tier OR OLD.status IS NOT NEW.status '
                 'OR OLD.has_bio IS NOT NEW.has_bio BEGIN '
                 'UPDATE lead_rank_totals SET n=n-1 WHERE hidden=OLD.hidden; '
                 'UPDATE lead_rank_totals SET n=n+1 WHERE hidden=NEW.hidden; ' + decrease + increase + ' END')


def ready(conn, sort=None):
    if sort is not None and sort not in ORDERS:
        return False
    prepared = bool(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('lead_rank','lead_rank_totals','lead_rank_facets')").fetchone()[0] == 3
                and conn.execute("SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN ('lead_rank_fit','lead_rank_score')").fetchone()[0] == 2
                and conn.execute("SELECT 1 FROM settings WHERE key='lead_rank_ready' AND value='true'").fetchone()
                and conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'lead_rank_%'").fetchone()[0] == 15
                and conn.execute("SELECT 1 FROM settings WHERE key='map_person_degree_v1' AND value='true'").fetchone()
                and conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_degree_%'").fetchone()[0] == 6)
    return prepared and (sort != 'connected' or bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='lead_rank_connected'").fetchone()))


def prepare(conn):
    """Publish a complete projection atomically, preserving caller transactions."""
    conn.execute('SAVEPOINT lead_rank_prepare')
    try:
        _prepare(conn)
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT lead_rank_prepare')
        conn.execute('RELEASE SAVEPOINT lead_rank_prepare')
        raise
    conn.execute('RELEASE SAVEPOINT lead_rank_prepare')


def _prepare(conn):
    ensure(conn)
    conn.execute("UPDATE settings SET value='false' WHERE key='lead_rank_ready'")
    for operation in ('insert', 'delete', 'update'):
        conn.execute(f'DROP TRIGGER IF EXISTS lead_rank_count_{operation}')
    conn.execute('DELETE FROM lead_rank')
    conn.execute('DELETE FROM lead_rank_facets')
    for sort in ORDERS:
        conn.execute(f'DROP INDEX IF EXISTS lead_rank_{sort}')
    conn.execute("INSERT INTO lead_rank SELECT p.id, EXISTS(SELECT 1 FROM marks m WHERE m.person_id=p.id AND m.status='no'), "
                 "v.tier,v.content_fit,coalesce(d.degree,0),v.score,p.followers,m.status,coalesce(p.bio,'')!='' FROM people p "
                 'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id LEFT JOIN map_person_degree d ON d.person_id=p.id')
    for sort, order in ORDERS.items():
        conn.execute(f'CREATE INDEX lead_rank_{sort} ON lead_rank(hidden,{order})')
    conn.execute('UPDATE lead_rank_totals SET n=(SELECT count(*) FROM lead_rank WHERE hidden=lead_rank_totals.hidden)')
    conn.execute("INSERT INTO lead_rank_facets SELECT coalesce(tier,'unread'),coalesce(status,''),has_bio,count(*) FROM lead_rank GROUP BY 1,2,3")
    ensure(conn)
    conn.execute("UPDATE settings SET value='true' WHERE key='lead_rank_ready'")


def owner_ids(conn):
    return [row[0] for row in conn.execute("SELECT p.id FROM people p WHERE p.handle='fortun8te' COLLATE NOCASE "
                                         'OR p.handle IN (SELECT handle FROM seeds WHERE is_me=1)')]


def counts(conn, statuses):
    if not ready(conn):
        return None
    facets = {(tier, status, bio): n for tier, status, bio, n in conn.execute('SELECT * FROM lead_rank_facets')}
    total = sum(facets.values())
    with_bio = sum(n for (_, _, bio), n in facets.items() if bio)
    for pid in owner_ids(conn):
        row = conn.execute("SELECT coalesce(tier,'unread'),coalesce(status,''),has_bio FROM lead_rank WHERE person_id=?", (pid,)).fetchone()
        if row:
            key = tuple(row)
            facets[key] -= 1
    out = dict.fromkeys(('hot', 'warm', 'cold', 'unread', *statuses, 'none', 'open'), 0)
    for (tier, status, bio), n in facets.items():
        if n <= 0:
            continue
        if status != 'no':
            out[tier] = out.get(tier, 0) + n
            out['open'] += n
        out[status or 'none'] = out.get(status or 'none', 0) + n
    out.update(total=total, with_bio=with_bio)
    return out


def page(conn, sort, limit, offset, *, min_lists=0, fits=()):
    """Exact open-list ordering with optional degree and content-fit predicates.

    Callers still hydrate only these IDs through the ordinary profile query.
    A missing connected index falls back until explicit offline preparation.
    """
    if (sort not in ORDERS or type(min_lists) is not int or min_lists < 0
            or any(fit not in FIT_CONDITIONS for fit in fits) or not ready(conn, sort)):
        return None
    # Preserve the original SQL NOT IN semantics even for a legacy null owner.
    if conn.execute('SELECT 1 FROM seeds WHERE is_me=1 AND handle IS NULL LIMIT 1').fetchone():
        return 0, []
    # Owner handles are few. Exclude them without counting ten million profiles.
    excluded = owner_ids(conn)
    clause = 'hidden=0'
    args = []
    if min_lists:
        clause += ' AND degree>=?'
        args.append(min_lists)
    if fits:
        clause += ' AND (' + ' OR '.join('(' + FIT_CONDITIONS[fit] + ')' for fit in dict.fromkeys(fits)) + ')'
    if excluded:
        clause += ' AND person_id NOT IN (' + ','.join('?' for _ in excluded) + ')'
        args.extend(excluded)
    if min_lists or fits:
        total = conn.execute(f'SELECT count(*) FROM lead_rank WHERE {clause}', args).fetchone()[0]
    else:
        total = conn.execute('SELECT n FROM lead_rank_totals WHERE hidden=0').fetchone()[0]
        if excluded:
            total -= conn.execute('SELECT count(*) FROM lead_rank WHERE hidden=0 AND person_id IN (' +
                                  ','.join('?' for _ in excluded) + ')', excluded).fetchone()[0]
    ids = [row[0] for row in conn.execute(f'SELECT person_id FROM lead_rank WHERE {clause} '
                                        f'ORDER BY {ORDERS[sort]} LIMIT ? OFFSET ?', args + [limit, offset])]
    return total, ids


def page_for_query(conn, sort, limit, offset, query):
    """Use only after the shared filter/sort/pagination validation has run.

    Reject every unimplemented query key, including empty-valued filters, so
    the ordinary query remains authoritative for status, tags and other views.
    Parsing matches the shared first-value comma and whole-number semantics.
    """
    if set(query) - {'sort', 'limit', 'offset', 'fit', 'min_lists'}:
        return None
    fits = [value.strip() for value in query.get('fit', [''])[0].split(',') if value.strip()]
    try:
        min_lists = int(query.get('min_lists', [''])[0].strip() or '0')
    except ValueError:
        return None
    if not 0 <= min_lists < 2 ** 63:
        return None
    return page(conn, sort, limit, offset, min_lists=min_lists, fits=fits)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Prepare exact indexed lead ordering on a stopped database copy.')
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.db) as connection:
        connection.execute('PRAGMA temp_store=FILE')
        prepare(connection)
