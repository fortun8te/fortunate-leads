"""Invalidation storage ownership; writes remain in the caller transaction."""



def mark_network_dirty(conn, person_ids):
    """Durable local rerank work. Caller commits; consumers acknowledge exact revisions."""
    ids = sorted(set(person_ids))
    if not ids:
        return None
    # The write takes SQLite's writer lock before reading the shared sequence.
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('network_change_id','0')")
    conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='network_change_id'")
    revision = int(conn.execute("SELECT value FROM settings WHERE key='network_change_id'").fetchone()[0])
    conn.executemany('INSERT INTO network_dirty VALUES(?,?) ON CONFLICT(person_id) DO UPDATE SET change_id=excluded.change_id',
                     ((pid, revision) for pid in ids))
    return revision


def dirty_seed_members(conn, *handles):
    handles = tuple(dict.fromkeys(h for h in handles if h))
    if not handles:
        return
    slots = ','.join('?' * len(handles))
    # One revision and one indexed, set-based write per page. In particular,
    # avoid copying the entire growing list into Python and executemany on
    # every cursor. A later page must refresh revisions even for queued peers.
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('network_change_id','0')")
    conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='network_change_id'")
    revision = int(conn.execute("SELECT value FROM settings WHERE key='network_change_id'").fetchone()[0])
    conn.execute(f'''INSERT INTO network_dirty(person_id,change_id)
        SELECT person_id,? FROM (
          SELECT person_id FROM edges WHERE seed IN ({slots})
          UNION SELECT id FROM people WHERE handle IN ({slots})
        ) WHERE true
        ON CONFLICT(person_id) DO UPDATE SET change_id=excluded.change_id''',
        (revision, *handles, *handles))
