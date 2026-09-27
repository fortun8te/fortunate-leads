"""Durable local review work and non-AI fallbacks for mode changes.

Schema setup belongs at startup. Queue seeding walks primary keys in bounded
batches; normal reads never scan all profiles. Caller owns every transaction.
"""
import json
import time

import db


_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS local_queue(
        person_id INTEGER PRIMARY KEY, retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT, revision INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 0)''',
    'CREATE INDEX IF NOT EXISTS local_queue_ready ON local_queue(retry_at,person_id)',
    '''CREATE TABLE IF NOT EXISTS local_reviews(
        person_id INTEGER PRIMARY KEY, input_hash TEXT NOT NULL, prompt TEXT,
        model TEXT, model_version TEXT, status TEXT NOT NULL, verdict TEXT,
        escalation_reason TEXT, updated_at TEXT NOT NULL, private_context_hash TEXT)''',
    'CREATE INDEX IF NOT EXISTS local_reviews_status ON local_reviews(status,person_id)',
    '''CREATE TABLE IF NOT EXISTS rule_assessments(
        person_id INTEGER PRIMARY KEY, profile_updated_at TEXT, input_hash TEXT,
        prefilter INTEGER, score INTEGER, tier TEXT, role TEXT, reason TEXT,
        content_fit INTEGER, tags TEXT NOT NULL DEFAULT '[]')''',
    '''CREATE TABLE IF NOT EXISTS processing_rule_queue(person_id INTEGER PRIMARY KEY)''',
    '''CREATE TABLE IF NOT EXISTS processing_ai_history(
        id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, model TEXT NOT NULL,
        input_hash TEXT NOT NULL, verdict_updated_at TEXT NOT NULL,
        verdict TEXT NOT NULL, tags TEXT NOT NULL, archived_at TEXT NOT NULL,
        UNIQUE(person_id,model,input_hash,verdict_updated_at))''',
    'CREATE INDEX IF NOT EXISTS verdicts_model_person ON verdicts(model,person_id)',
)


def _queue_sql(pid, priority=0):
    return f'''INSERT INTO local_queue(person_id,priority)
        SELECT id,{priority} FROM people WHERE id={pid} AND trim(coalesce(bio,''))!=''
        ON CONFLICT(person_id) DO UPDATE SET retry_at=0,last_error=NULL,revision=revision+1,
            priority=max(local_queue.priority,excluded.priority);'''


def ensure(conn):
    """Idempotent startup schema. Does not commit a surrounding migration."""
    for sql in _SCHEMA:
        conn.execute(sql)
    if 'failure_reason' not in {r[1] for r in conn.execute('PRAGMA table_info(local_reviews)')}:
        conn.execute('ALTER TABLE local_reviews ADD COLUMN failure_reason TEXT')
    if 'priority' not in {r[1] for r in conn.execute('PRAGMA table_info(local_queue)')}:
        conn.execute('ALTER TABLE local_queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
    conn.execute('CREATE INDEX IF NOT EXISTS local_queue_priority_ready ON local_queue(priority,retry_at,person_id)')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS processing_people_insert AFTER INSERT ON people
        BEGIN {_queue_sql('NEW.id')} END''')
    columns = ('handle','name','bio','website','category','followers','following','posts',
               'is_private','is_verified','is_business')
    changed = ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in columns)
    old_trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='processing_people_update'").fetchone()
    if old_trigger and 'DELETE FROM local_reviews' not in old_trigger[0]:
        # Replace the previous insert-only trigger once during schema setup.
        conn.execute('DROP TRIGGER processing_people_update')
        db.set_setting(conn, 'local_empty_cleanup_complete', False)
        db.set_setting(conn, 'local_empty_cleanup_cursor', 0)
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS processing_people_update
        AFTER UPDATE OF {','.join(columns)} ON people WHEN {changed}
        BEGIN
        DELETE FROM local_queue WHERE person_id=NEW.id AND trim(coalesce(NEW.bio,''))='';
        DELETE FROM local_reviews WHERE person_id=NEW.id AND trim(coalesce(NEW.bio,''))='';
        {_queue_sql('NEW.id')} END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS processing_people_delete AFTER DELETE ON people
        BEGIN DELETE FROM local_queue WHERE person_id=OLD.id;
        DELETE FROM local_reviews WHERE person_id=OLD.id;
        DELETE FROM rule_assessments WHERE person_id=OLD.id;
        DELETE FROM processing_rule_queue WHERE person_id=OLD.id; END''')
    for table in ('marks', 'owner_context', 'tags', 'owner_note_reads'):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        for event, ref in (('INSERT','NEW'), ('UPDATE','NEW'), ('DELETE','OLD')):
            name = f'processing_{table}_{event.lower()}'
            previous = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            if previous and 'priority' not in previous[0]:
                conn.execute(f'DROP TRIGGER {name}')
            condition = ''
            if event == 'UPDATE' and table in ('marks', 'owner_context'):
                columns = ('status', 'note') if table == 'marks' else ('relationships', 'familiarity')
                condition = ' WHEN ' + ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in columns)
            if table == 'tags':
                condition = (" WHEN (OLD.source='manual' OR NEW.source='manual') AND (OLD.tag IS NOT NEW.tag OR OLD.grp IS NOT NEW.grp OR OLD.source IS NOT NEW.source)" if event == 'UPDATE'
                             else f" WHEN {ref}.source='manual'")
            elif table == 'owner_note_reads':
                # Running/retry state bookkeeping must not create extra profile work.
                condition = (" WHEN NEW.state='ready' AND (OLD.state IS NOT NEW.state OR OLD.facts IS NOT NEW.facts)"
                             if event == 'UPDATE' else f" WHEN {ref}.state='ready'")
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS processing_{table}_{event.lower()}
                AFTER {event} ON {table}{condition} BEGIN {_queue_sql(ref + '.person_id', priority=1)} END''')


def enqueue(conn, person_id):
    conn.execute(_queue_sql('?'), (person_id,))


def cleanup_empty_step(conn, limit=500):
    """Remove legacy phantom work in bounded primary-key windows, once.

    Walk both queues and completed reviews so an old empty-bio review without a
    queue entry cannot remain counted as reviewed. New edits use the trigger.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise ValueError('limit must be a whole number between 1 and 5000')
    if db.get_setting(conn, 'local_empty_cleanup_complete', False):
        return 0
    cursor = db.get_setting(conn, 'local_empty_cleanup_cursor', 0) or 0
    ids = set()
    for table in ('local_queue', 'local_reviews'):
        ids.update(r[0] for r in conn.execute(
            f'SELECT person_id FROM {table} WHERE person_id>? ORDER BY person_id LIMIT ?', (cursor,limit)))
    ids = sorted(ids)[:limit]
    if not ids:
        db.set_setting(conn, 'local_empty_cleanup_complete', True)
        return 0
    # Same bounded ID window for both tables; no full profile scan or giant IN list.
    high = ids[-1]
    for table in ('local_queue', 'local_reviews'):
        conn.execute(f"""DELETE FROM {table} WHERE person_id>? AND person_id<=?
            AND NOT EXISTS(SELECT 1 FROM people p WHERE p.id={table}.person_id
                AND trim(coalesce(p.bio,''))!='')""", (cursor,high))
    db.set_setting(conn, 'local_empty_cleanup_cursor', high)
    if len(ids) < limit:
        db.set_setting(conn, 'local_empty_cleanup_complete', True)
    return len(ids)


def seed_step(conn, limit=500, policy=None):
    """Queue existing bios once, walking bounded primary-key windows."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise ValueError('limit must be a whole number between 1 and 5000')
    cleanup_empty_step(conn, limit)
    if policy is not None and policy != db.get_setting(conn, 'local_queue_seed_policy'):
        if not isinstance(policy, str) or not policy:
            raise ValueError('policy must be a nonempty string')
        db.set_setting(conn, 'local_queue_seed_policy', policy)
        db.set_setting(conn, 'local_queue_seed_cursor', 0)
        db.set_setting(conn, 'local_queue_seed_complete', False)
        db.set_setting(conn, 'local_queue_seed_all', True)
    cursor = db.get_setting(conn, 'local_queue_seed_cursor', 0) or 0
    if db.get_setting(conn, 'local_queue_seed_complete', False):
        return 0
    rows = conn.execute('SELECT id,bio,updated_at FROM people WHERE id>? ORDER BY id LIMIT ?',
                        (cursor, limit)).fetchall()
    if not rows:
        db.set_setting(conn, 'local_queue_seed_complete', True)
        return 0
    ids = [r[0] for r in rows]
    lo, hi = ids[0], ids[-1]
    # Existing work/retry state must not be reset by migration.
    refresh_all = bool(db.get_setting(conn, 'local_queue_seed_all', False))
    conn.execute('''INSERT OR IGNORE INTO local_queue(person_id)
        SELECT id FROM people WHERE id BETWEEN ? AND ? AND trim(coalesce(bio,''))!=''
        AND (? OR NOT EXISTS(SELECT 1 FROM local_reviews l WHERE l.person_id=people.id))''', (lo, hi, refresh_all))
    # Legacy prefilter can contain Laya, so never claim it as a rules-only value.
    # Recompute tags rather than trusting legacy auto tags with mixed provenance.
    conn.execute('''INSERT OR IGNORE INTO rule_assessments
        (person_id,profile_updated_at,input_hash,score,tier,role,reason,content_fit)
        SELECT v.person_id,p.updated_at,v.input_hash,v.score,v.tier,v.role,v.reason,v.content_fit
        FROM verdicts v JOIN people p ON p.id=v.person_id
        WHERE p.id BETWEEN ? AND ? AND v.model='rules' AND v.updated_at=p.updated_at''', (lo, hi))
    db.set_setting(conn, 'local_queue_seed_cursor', hi)
    if len(rows) < limit:
        db.set_setting(conn, 'local_queue_seed_complete', True)
    return len(rows)


def next_pending(conn, limit=1, now=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('limit must be a whole number between 1 and 100')
    # Two small indexed ranges avoid sorting the full bulk queue to find an owner edit.
    ready_at = time.time() if now is None else now
    rows = []
    for priority in (1, 0):
        rows.extend(conn.execute('''SELECT q.person_id,q.revision,q.retry_at,q.last_error
            FROM local_queue q JOIN people p ON p.id=q.person_id
            WHERE q.priority=? AND q.retry_at<=? AND trim(coalesce(p.bio,''))!=''
            ORDER BY q.retry_at,q.person_id LIMIT ?''', (priority, ready_at, limit-len(rows))).fetchall())
        if len(rows) == limit:
            break
    return rows


def queue_status(conn):
    # Indexed aggregate over queued work, not a join across millions of profiles.
    # Separate COUNT from MIN: combining them forces a row-by-row aggregate.
    # COUNT can use SQLite's b-tree count; earliest retry is one index entry.
    count = conn.execute('SELECT count(*) FROM local_queue').fetchone()[0]
    earliest = conn.execute('SELECT retry_at FROM local_queue ORDER BY retry_at LIMIT 1').fetchone()
    return {'pending': count, 'next_retry_at': earliest[0] if earliest else None,
            'seeding': not bool(db.get_setting(conn, 'local_queue_seed_complete', False)),
            'seed_cursor': db.get_setting(conn, 'local_queue_seed_cursor', 0) or 0}


def put_review(conn, person_id, result, revision=None, private_context_hash=None):
    """Save a validated review under the caller's content and mode write guards.

    Exact revision acknowledgment preserves a newer edit queued during inference.
    Returns False without writing when that queued revision changed.
    """
    if revision is not None:
        row = conn.execute('SELECT revision FROM local_queue WHERE person_id=?', (person_id,)).fetchone()
        if not row or row[0] != revision:
            return False
    conn.execute('''INSERT INTO local_reviews
        (person_id,input_hash,prompt,model,model_version,status,verdict,escalation_reason,updated_at,private_context_hash,failure_reason)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET
        input_hash=excluded.input_hash,prompt=excluded.prompt,model=excluded.model,
        model_version=excluded.model_version,status=excluded.status,verdict=excluded.verdict,
        escalation_reason=excluded.escalation_reason,updated_at=excluded.updated_at,
        private_context_hash=excluded.private_context_hash,failure_reason=excluded.failure_reason''',
        (person_id,result['input_hash'],result.get('prompt'),result.get('model'),result.get('model_version'),
         result['status'],json.dumps(result.get('verdict'),ensure_ascii=False),
         result.get('escalation_reason'),db.now(),private_context_hash,
         str(result['error'])[:240] if result.get('error') else None))
    conn.execute('DELETE FROM local_queue WHERE person_id=?', (person_id,))
    return True


def retry(conn, person_id, revision, error, delay=60):
    """Back off only the work version that failed; a newer profile stays ready."""
    conn.execute('''UPDATE local_queue SET retry_at=?,last_error=?
        WHERE person_id=? AND revision=?''', (time.time()+max(1,delay),str(error)[:240],person_id,revision))


def save_rules(conn, person, prefilter, verdict, auto_tags):
    """Store a complete rule-only fallback even when a stronger verdict remains active."""
    conn.execute('''INSERT INTO rule_assessments
        (person_id,profile_updated_at,input_hash,prefilter,score,tier,role,reason,content_fit,tags)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET
        profile_updated_at=excluded.profile_updated_at,input_hash=excluded.input_hash,
        prefilter=excluded.prefilter,score=excluded.score,tier=excluded.tier,role=excluded.role,
        reason=excluded.reason,content_fit=excluded.content_fit,tags=excluded.tags''',
        (person['id'],person.get('updated_at'),verdict.get('input_hash'),prefilter,
         verdict.get('score'),verdict.get('tier'),verdict.get('role'),verdict.get('reason'),
         verdict.get('content_fit'),json.dumps(list(auto_tags),ensure_ascii=False)))
    conn.execute('DELETE FROM processing_rule_queue WHERE person_id=?', (person['id'],))


def archive_verdict(conn, person_id):
    """Preserve an active AI result before replacing it. Safe to call repeatedly."""
    row = conn.execute("SELECT * FROM verdicts WHERE person_id=? AND model!='rules'", (person_id,)).fetchone()
    if not row:
        return False
    cols = [d[0] for d in conn.execute('SELECT * FROM verdicts LIMIT 0').description]
    verdict = dict(zip(cols, row))
    tags = list(conn.execute("SELECT tag,grp FROM tags WHERE person_id=? AND source='auto'", (person_id,)))
    conn.execute('''INSERT OR IGNORE INTO processing_ai_history
        (person_id,model,input_hash,verdict_updated_at,verdict,tags,archived_at) VALUES(?,?,?,?,?,?,?)''',
        (person_id,verdict['model'],verdict.get('input_hash') or '',verdict.get('updated_at') or '',
         json.dumps(verdict,ensure_ascii=False),json.dumps([list(r) for r in tags]),db.now()))
    return True


def invalidate_mode(conn, previous, mode):
    """Immediately remove disallowed active AI influence on a mode downgrade.

    Runs only on a mode transition, inside set_mode's transaction. Historical
    results and manual tags survive. Missing/stale rule fallbacks stay unscored
    until the bounded rule worker replaces them.
    """
    if previous == mode:
        return {'restored': 0, 'cleared': 0}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_assessments'").fetchone():
        # Tests/minimal databases can omit the app schema; production ensure runs at startup.
        return {'restored': 0, 'cleared': 0}
    disallowed = ("model!='rules'" if mode == 'R' else
                  "model!='rules' AND model NOT LIKE 'local:%'" if mode == 'RLAI' else '0')
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS mode_affected(person_id INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM mode_affected')
    conn.execute(f'INSERT INTO mode_affected SELECT person_id FROM verdicts WHERE {disallowed}')
    # Archive in SQL: one indexed tag lookup per affected profile, no per-profile
    # Python round trips and no entire-database materialization in memory.
    conn.execute("""INSERT OR IGNORE INTO processing_ai_history
        (person_id,model,input_hash,verdict_updated_at,verdict,tags,archived_at)
        SELECT v.person_id,v.model,coalesce(v.input_hash,''),coalesce(v.updated_at,''),
        json_object('person_id',v.person_id,'prefilter',v.prefilter,'score',v.score,
            'tier',v.tier,'role',v.role,'reason',v.reason,'model',v.model,
            'input_hash',v.input_hash,'updated_at',v.updated_at,'content_fit',v.content_fit),
        (SELECT json_group_array(json_array(t.tag,t.grp)) FROM tags t
            WHERE t.person_id=v.person_id AND t.source='auto'),?
        FROM mode_affected a JOIN verdicts v ON v.person_id=a.person_id""", (db.now(),))
    affected = conn.execute('SELECT count(*) FROM mode_affected').fetchone()[0]
    conn.execute("DELETE FROM tags WHERE source='auto' AND person_id IN (SELECT person_id FROM mode_affected)")
    conn.execute('DELETE FROM verdicts WHERE person_id IN (SELECT person_id FROM mode_affected)')
    conn.execute('''INSERT INTO verdicts(person_id,prefilter,score,tier,role,reason,model,input_hash,updated_at,content_fit)
        SELECT r.person_id,r.prefilter,r.score,r.tier,r.role,r.reason,'rules',r.input_hash,p.updated_at,r.content_fit
        FROM mode_affected a JOIN rule_assessments r ON r.person_id=a.person_id
        JOIN people p ON p.id=r.person_id WHERE r.profile_updated_at=p.updated_at''')
    restored = conn.execute('SELECT changes()').fetchone()[0]
    conn.execute('''INSERT OR IGNORE INTO tags(person_id,tag,grp,source)
        SELECT r.person_id,json_extract(j.value,'$[0]'),json_extract(j.value,'$[1]'),'auto'
        FROM mode_affected a JOIN rule_assessments r ON r.person_id=a.person_id
        JOIN people p ON p.id=r.person_id, json_each(r.tags) j
        WHERE r.profile_updated_at=p.updated_at''')
    conn.execute('''INSERT OR IGNORE INTO processing_rule_queue
        SELECT a.person_id FROM mode_affected a JOIN people p ON p.id=a.person_id''')
    if mode == 'R':
        # Even a rules verdict may carry an old Laya-influenced preliminary rank.
        conn.execute('''UPDATE verdicts SET prefilter=(
            SELECT r.prefilter FROM rule_assessments r JOIN people p ON p.id=r.person_id
            WHERE r.person_id=verdicts.person_id AND r.profile_updated_at=p.updated_at)''')
        conn.execute("INSERT OR IGNORE INTO processing_rule_queue SELECT person_id FROM verdicts WHERE prefilter IS NULL")
        conn.execute("UPDATE verdicts SET updated_at='' WHERE prefilter IS NULL")
    # Upgrades also need a bounded rules/Laya refresh, but model inference uses its
    # independent queue and content cache. Parent worker walks this cursor.
    db.set_setting(conn, 'processing_refresh_cursor', 0)
    db.set_setting(conn, 'processing_refresh_complete', False)
    # Keep two previous results per person/model. This history is recovery data,
    # not an unbounded second copy of every model response ever generated.
    conn.execute('''DELETE FROM processing_ai_history AS h
        WHERE person_id IN (SELECT person_id FROM mode_affected) AND id NOT IN (
            SELECT recent.id FROM processing_ai_history recent
            WHERE recent.person_id=h.person_id AND recent.model=h.model
            ORDER BY recent.id DESC LIMIT 2)''')
    conn.execute('DELETE FROM mode_affected')
    return {'restored': restored, 'cleared': affected-restored}
