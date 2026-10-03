"""Map schema storage ownership; writes remain in the caller transaction."""



def ensure_map_layout_dirty(conn):
    """Queue the people whose map layout inputs changed (server/map_layout.py drains it).

    Nothing is backfilled: a layout build reads current data, and only changes made after
    that need a queue. `claimed` lets the worker tell a change that landed while it was
    applying a batch (claimed back to 0 by the trigger) from one it already handled.
    """
    conn.execute('CREATE TABLE IF NOT EXISTS map_layout_dirty('
                 'person_id INTEGER PRIMARY KEY, claimed INTEGER NOT NULL DEFAULT 0)')
    verdict_columns = {r[1] for r in conn.execute('PRAGMA table_info(verdicts)')}
    specs = [('map_person_degree', 'degree', None), ('marks', 'marks', 'status'),
             ('verdicts', 'verdicts', 'content_fit,score' if 'content_fit' in verdict_columns else None),
             ('owner_context', 'owner', 'relationships,familiarity'), ('people', 'people', None)]
    for table, label, update_of in specs:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            if table == 'verdicts' and update_of is None and operation == 'UPDATE':
                continue
            name = f'map_layout_dirty_{label}_{operation.lower()}'
            if table == 'people' and operation == 'UPDATE':
                when = ' OF name,handle'
            elif operation == 'UPDATE' and update_of:
                when = f' OF {update_of}'
            else:
                when = ''
            ref = 'OLD' if operation == 'DELETE' else 'NEW'
            column = 'id' if table == 'people' else 'person_id'
            body = (f'INSERT INTO map_layout_dirty(person_id,claimed) VALUES({ref}.{column},0) '
                    'ON CONFLICT(person_id) DO UPDATE SET claimed=0;')
            if operation == 'UPDATE' and table != 'people':
                body += (f' INSERT INTO map_layout_dirty(person_id,claimed) VALUES(OLD.{column},0) '
                         'ON CONFLICT(person_id) DO UPDATE SET claimed=0;')
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation}{when} ON {table} BEGIN {body} END')


def init_map_membership_revision(conn):
    """Keep overlap caches valid across profile edits without rescanning all edges."""
    # Bump on installation/repair too: a rebuilt membership table may differ
    # from an older cache even when its restored revision value happens to match.
    conn.execute("INSERT INTO settings(key,value) VALUES('map_membership_rev','1') "
                 "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = 'map_overlap_rev_' + operation.lower()
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        when = ' WHEN OLD.person_id IS NOT NEW.person_id OR OLD.seed IS NOT NEW.seed' if operation == 'UPDATE' else ''
        conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON map_seed_member{when} BEGIN '
                     "UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='map_membership_rev'; END")


def init_map_person_degree(conn):
    """Backfill once, then keep map degrees and source handles exact.

    The ready marker and trigger installation commit together with the backfill;
    a failed migration is retried from scratch at the next initialization.
    """
    columns = {r[1] for r in conn.execute('PRAGMA table_info(map_person_degree)')}
    if 'score' not in columns:
        conn.execute('ALTER TABLE map_person_degree ADD COLUMN score REAL')
    if 'hidden' not in columns:
        conn.execute('ALTER TABLE map_person_degree ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
    def complete(key, pattern, count):
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        installed = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                                 (pattern,)).fetchone()[0]
        return bool(row and row[0] == 'true' and installed == count)

    people_ready = complete('map_people_present_v1', 'map_people_%', 3)
    ready = complete('map_person_degree_v1', 'map_degree_%', 6) and people_ready
    if not ready:
        conn.execute('DELETE FROM map_person_degree')
        conn.execute('INSERT INTO map_person_degree(person_id,degree) '
                     'SELECT e.person_id,count(DISTINCT e.seed) FROM current_edges e '
                     'JOIN people p ON p.id=e.person_id '
                     'GROUP BY e.person_id HAVING count(DISTINCT e.seed)>0')
    rank_ready = complete('map_rank_v1', 'map_rank_%', 6)
    if not rank_ready or not ready:
        conn.execute('UPDATE map_person_degree SET '
                     'score=(SELECT score FROM verdicts WHERE person_id=map_person_degree.person_id), '
                     "hidden=EXISTS(SELECT 1 FROM marks WHERE person_id=map_person_degree.person_id AND status='no')")
    conn.execute('CREATE INDEX IF NOT EXISTS map_degree_rank ON map_person_degree(hidden,degree DESC,score DESC,person_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS map_score_rank ON map_person_degree(hidden,(score IS NULL),score DESC,degree DESC,person_id)')
    sources_ready = complete('map_source_handles_v1', 'map_sources_%', 6)
    if not sources_ready:
        conn.execute('DELETE FROM map_source_handles')
        conn.execute('INSERT INTO map_source_handles(handle,refs) '
                     'SELECT seed,count(*) FROM edges GROUP BY seed COLLATE NOCASE')
        conn.execute('INSERT INTO map_source_handles(handle,refs) SELECT handle,1 FROM seeds WHERE true '
                     'ON CONFLICT(handle) DO UPDATE SET refs=map_source_handles.refs+1')
    # Distinct current source membership is linear in the edge count. It keeps
    # source degrees exact without a source-wide rescan after each page.
    members_ready = complete('map_seed_member_v1', 'map_member_%', 6) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
    for name in ('map_seed_degree_insert', 'map_seed_degree_delete'):
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
    if not members_ready:
        conn.execute('DELETE FROM map_seed_member')
        conn.execute('DELETE FROM map_seed_degree')
        conn.execute('INSERT INTO map_seed_member(person_id,seed) '
                     'SELECT DISTINCT person_id,seed FROM current_edges')
        conn.execute('INSERT INTO map_seed_degree(seed,degree) '
                     'SELECT seed,count(*) FROM map_seed_member GROUP BY seed')
    conn.execute('CREATE TRIGGER map_seed_degree_insert AFTER INSERT ON map_seed_member BEGIN '
                 'INSERT INTO map_seed_degree(seed,degree) VALUES(NEW.seed,1) '
                 'ON CONFLICT(seed) DO UPDATE SET degree=degree+1; END')
    conn.execute('CREATE TRIGGER map_seed_degree_delete AFTER DELETE ON map_seed_member BEGIN '
                 'UPDATE map_seed_degree SET degree=degree-1 WHERE seed=OLD.seed; '
                 'DELETE FROM map_seed_degree WHERE seed=OLD.seed AND degree<=0; END')
    # Recreate on open, as revision triggers do. Old and new identities both
    # matter during merges, renames, and evidence rewrites.
    for table in ('edges', 'edge_evidence'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'map_degree_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            when = ''
            if operation == 'UPDATE':
                when = (' WHEN OLD.seed IS NOT NEW.seed OR OLD.person_id IS NOT NEW.person_id '
                        'OR OLD.direction IS NOT NEW.direction' +
                        (' OR OLD.active IS NOT NEW.active' if table == 'edge_evidence' else ''))
            refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
            body = []
            for ref in refs:
                pid = f'{ref}.person_id'
                body.append(f'DELETE FROM map_person_degree WHERE person_id={pid};')
                body.append('INSERT INTO map_person_degree(person_id,degree,score,hidden) '
                            f'SELECT {pid},count(DISTINCT seed),'
                            f'(SELECT score FROM verdicts WHERE person_id={pid}),'
                            f"EXISTS(SELECT 1 FROM marks WHERE person_id={pid} AND status='no') "
                            'FROM current_edges e JOIN people p ON p.id=e.person_id '
                            f'WHERE e.person_id={pid} HAVING count(DISTINCT e.seed)>0;')
            conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN {" ".join(body)} END')
            member_name = f'map_member_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {member_name}')
            member_body = []
            for ref in refs:
                pid, seed = f'{ref}.person_id', f'{ref}.seed'
                member_body.append(f'DELETE FROM map_seed_member WHERE person_id={pid} AND seed={seed};')
                member_body.append('INSERT INTO map_seed_member(person_id,seed) '
                                   f'SELECT {pid},{seed} WHERE EXISTS('
                                   'SELECT 1 FROM current_edges '
                                   f'WHERE person_id={pid} AND seed={seed});')
            conn.execute(f'CREATE TRIGGER {member_name} AFTER {operation} ON {table}{when} '
                         f'BEGIN {" ".join(member_body)} END')
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = f'map_people_{operation.lower()}'
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
        body = []
        for ref in refs:
            pid = f'{ref}.id'
            body.append(f'DELETE FROM map_person_degree WHERE person_id={pid};')
            body.append('INSERT INTO map_person_degree(person_id,degree,score,hidden) '
                        f'SELECT {pid},count(DISTINCT e.seed),'
                        f'(SELECT score FROM verdicts WHERE person_id={pid}),'
                        f"EXISTS(SELECT 1 FROM marks WHERE person_id={pid} AND status='no') "
                        'FROM current_edges e JOIN people p ON p.id=e.person_id '
                        f'WHERE e.person_id={pid} HAVING count(DISTINCT e.seed)>0;')
        when = ' WHEN OLD.id IS NOT NEW.id' if operation == 'UPDATE' else ''
        conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON people{when} BEGIN {" ".join(body)} END')
    for table, field, value in (('verdicts', 'score',
                                 '(SELECT score FROM verdicts WHERE person_id={pid})'),
                                ('marks', 'hidden',
                                 "EXISTS(SELECT 1 FROM marks WHERE person_id={pid} AND status='no')")):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'map_rank_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
            body = ' '.join(f'UPDATE map_person_degree SET {field}={value.format(pid=ref + ".person_id")} '
                            f'WHERE person_id={ref}.person_id;' for ref in refs)
            when = ''
            if operation == 'UPDATE':
                when = (f' WHEN OLD.person_id IS NOT NEW.person_id OR OLD.{field if table == "verdicts" else "status"} '
                        f'IS NOT NEW.{field if table == "verdicts" else "status"}')
            conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN {body} END')
    # A source remains excluded from lead dots if it is either an active seed
    # registry entry or appears in historical edges. Reference counts handle
    # rename/merge/delete without scanning all edge rows at map-read time.
    for table, column in (('edges', 'seed'), ('seeds', 'handle')):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'map_sources_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            if operation == 'INSERT':
                body = (f'INSERT INTO map_source_handles(handle,refs) VALUES(NEW.{column},1) '
                        'ON CONFLICT(handle) DO UPDATE SET refs=refs+1;')
                when = ''
            elif operation == 'DELETE':
                body = (f'UPDATE map_source_handles SET refs=refs-1 WHERE handle=OLD.{column}; '
                        f'DELETE FROM map_source_handles WHERE handle=OLD.{column} AND refs<=0;')
                when = ''
            else:
                body = (f'UPDATE map_source_handles SET refs=refs-1 WHERE handle=OLD.{column}; '
                        f'DELETE FROM map_source_handles WHERE handle=OLD.{column} AND refs<=0; '
                        f'INSERT INTO map_source_handles(handle,refs) VALUES(NEW.{column},1) '
                        'ON CONFLICT(handle) DO UPDATE SET refs=refs+1;')
                when = f' WHEN OLD.{column} IS NOT NEW.{column}'
            conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN {body} END')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_person_degree_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_rank_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_source_handles_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_people_present_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_seed_member_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
