import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

WORK = Path('/Users/michael/Documents/Codex/2026-09-23/here-s-the-full-prompt-with-2/work')
OLD = WORK / 'mass_qualification' / 'research.sqlite'
BATCH = WORK / 'lead_workspace' / 'batches' / '2026-09-24-user-22.txt'
OUT = Path(__file__).resolve().parent.parent / 'data' / 'leads.sqlite'
ME = 'fortun8te'
FIELDS = {'display_name': 'name', 'profile_pic_url': 'pic_url', 'is_private': 'is_private', 'is_verified': 'is_verified',
          'biography': 'bio', 'website': 'website', 'followers_count': 'followers', 'business_category': 'category'}
COMPLETE = ('complete', 'claimed_complete_unverified')


def _migrate_into(old, conn, batch_path):
    ts = db.now()
    existing_lists = {(r['seed'].lower(), r['direction']) for r in conn.execute('SELECT seed,direction FROM lists')}

    ents = {r['id']: dict(r) for r in old.execute("SELECT id, handle, platform_id, created_at FROM entities WHERE platform='instagram'")}
    profile = {}
    for r in old.execute('SELECT entity_id, field, value_json, observed_at FROM profile_current'):
        if r['entity_id'] in ents and r['field'] in FIELDS:
            profile.setdefault(r['entity_id'], {})[FIELDS[r['field']]] = (json.loads(r['value_json']), r['observed_at'])

    collections = [r for r in old.execute('SELECT * FROM collections') if r['source_entity'] in ents]
    seed_ids = {r['source_entity'] for r in collections} | {i for i, e in ents.items() if e['handle'] == ME}
    batch = [db.norm_handle(h) for h in Path(batch_path).read_text().split() if h.strip()] if batch_path else []
    commanded = [db.norm_handle(json.loads(r[0]).get('handle')) for r in
                 old.execute("SELECT payload FROM exporter_commands WHERE type='collect' ORDER BY created_at")]

    pid = {}
    renamed = {}
    for eid, e in ents.items():
        p = {k: v for k, (v, _) in profile.get(eid, {}).items() if v not in (None, '')}
        p.update(handle=e['handle'], ig_id=e['platform_id'])
        if p.get('bio'):
            p['bio_at'] = profile[eid]['bio'][1]
        observed = max([at for _, at in profile.get(eid, {}).values() if at] + [e['created_at']])
        pid[eid] = db.upsert_person(conn, p, observed)
        conn.execute('UPDATE people SET first_seen=min(first_seen, ?) WHERE id=?', (e['created_at'], pid[eid]))
        current_handle = conn.execute('SELECT handle FROM people WHERE id=?', (pid[eid],)).fetchone()[0]
        if e['platform_id'] and current_handle != e['handle']:
            renamed[db.norm_handle(e['handle'])] = current_handle
        e['handle'] = current_handle

    commanded = [renamed.get(h, h) for h in commanded]
    batch = [renamed.get(h, h) for h in batch]
    for h in [ents[i]['handle'] for i in seed_ids] + commanded + batch:
        conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (h, ts))
    for i in seed_ids:
        if ents[i]['platform_id']:
            conn.execute('UPDATE seeds SET ig_id=coalesce(ig_id,?) WHERE handle=?', (ents[i]['platform_id'], ents[i]['handle']))
    conn.execute('UPDATE seeds SET is_me=1 WHERE handle=?', (renamed.get(ME, ME),))

    skipped = 0
    for src, dst, day in old.execute("SELECT src, dst, observed_date FROM edges WHERE relationship='follows'"):
        if src not in ents or dst not in ents or not ({src, dst} & seed_ids):
            skipped += 1
            continue
        if dst in seed_ids:  # src follows the seed
            db.add_edge(conn, ents[dst]['handle'], pid[src], 'followers', day, observed=False)
        if src in seed_ids:  # the seed follows dst
            db.add_edge(conn, ents[src]['handle'], pid[dst], 'following', day, observed=False)

    lists = {}
    for c in collections:
        key = (ents[c['source_entity']]['handle'], c['side'])
        lst = lists.setdefault(key, {'total': None, 'done': False, 'at': c['observed_date']})
        lst['total'] = max([t for t in (lst['total'], c['displayed_count']) if t], default=None)
        lst['done'] |= c['coverage'] in COMPLETE
        lst['at'] = max(lst['at'], c['observed_date'])
    for seed, direction, n in conn.execute('SELECT seed, direction, count(*) FROM edges GROUP BY 1, 2').fetchall():
        lst = lists.setdefault((seed, direction), {'total': None, 'done': seed == ME, 'at': ts})
        lst['received'] = n
    for (seed, direction), lst in lists.items():
        received, total = lst.get('received', 0), lst['total']
        done = lst['done'] or (total is not None and received >= total - max(2, total // 50))
        conn.execute('INSERT OR IGNORE INTO lists(seed, direction, state, received, total, updated_at) VALUES(?,?,?,?,?,?)',
                     (seed, direction, 'done' if done else 'paused', received, total, lst['at']))

    for i, h in enumerate(batch):
        for direction in ('followers', 'following'):
            if (h.lower(), direction) not in existing_lists:
                db.queue_list(conn, h, direction, priority=len(batch) - i)

    for r in old.execute('SELECT entity_id, relation, note, added_at FROM known_people'):
        if r['entity_id'] in pid:
            conn.execute('INSERT OR IGNORE INTO marks(person_id,status,note,updated_at) VALUES(?,?,?,?)', (pid[r['entity_id']], 'client' if r['relation'] == 'client' else 'known',
                                                                          f"{r['relation']}: {r['note'] or ''}".strip(': '), r['added_at']))
    db.migrate_statuses(conn)   # 'known' -> tag 'Already know them'
    conn.commit()

    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    summary = {'people': q('SELECT count(*) FROM people'), 'edges': q('SELECT count(*) FROM edges'),
               'seeds': q('SELECT count(*) FROM seeds'), 'bios': q("SELECT count(*) FROM people WHERE coalesce(bio,'')!=''"),
               'lists_done': q("SELECT count(*) FROM lists WHERE state='done'"),
               'lists_open': q("SELECT count(*) FROM lists WHERE state!='done'"),
               'list_jobs': q("SELECT count(*) FROM jobs WHERE kind='list' AND state='queued'"),
               'marks': q('SELECT count(*) FROM marks'), 'skipped_edges': skipped}
    return summary


def migrate(old_path, out_path, batch_path):
    """Build a complete import off to the side before exposing it as a database.

    An existing destination may be in use by the server. Never replace it from
    a stale snapshot: return the staged file for an explicit offline review.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, stage_name = tempfile.mkstemp(prefix=f'.{out_path.name}.import-', suffix='.sqlite', dir=out_path.parent)
    os.close(fd)
    stage = Path(stage_name)
    published = False
    try:
        existing = os.path.lexists(out_path)
        if existing:
            source = sqlite3.connect(f'{out_path.resolve().as_uri()}?mode=ro', uri=True)
            staged = sqlite3.connect(stage)
            try:
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {'people', 'seeds', 'lists', 'edges', 'accounts', 'settings'}
                if not required <= tables:
                    raise sqlite3.DatabaseError('destination is not a Fortunate Leads database')
                source.backup(staged)
            finally:
                staged.close()
                source.close()

        old = sqlite3.connect(f'{Path(old_path).resolve().as_uri()}?mode=ro', uri=True)
        try:
            old.row_factory = sqlite3.Row
            old.execute('BEGIN')  # Every legacy table comes from the same snapshot.
            conn = db.init(str(stage))
            try:
                summary = _migrate_into(old, conn, batch_path)
            finally:
                conn.close()
        finally:
            old.close()

        # Keep the deliverable in one file, including changes written in WAL.
        check = sqlite3.connect(stage)
        try:
            check.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if check.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise sqlite3.DatabaseError('staged import is not a standalone database')
            if check.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise sqlite3.DatabaseError('staged import failed integrity check')
        finally:
            check.close()

        if existing:
            summary['staged_path'] = str(stage)
        else:
            # A hard link creates the result only if no other process has done so.
            # Both paths are in the same directory, so this is atomic.
            try:
                os.link(stage, out_path)
            except FileExistsError:
                # A newly created destination belongs to another process. Keep
                # our completed import for review, just as for an existing DB.
                summary['staged_path'] = str(stage)
            else:
                stage.unlink()
        published = True
        return summary
    finally:
        if not published:
            stage.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(stage) + suffix).unlink(missing_ok=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', default=str(OLD))
    ap.add_argument('--out', default=str(OUT))
    ap.add_argument('--batch', default=str(BATCH))
    a = ap.parse_args()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    result = migrate(a.old, a.out, a.batch)
    for k, v in result.items():
        print(f'{k:>14}  {v}')
    if 'staged_path' in result:
        print('Import staged for review. The destination database was not changed.')
        print('Before replacing it, stop all database writers and back up the current database.')
        print('Changes made after this staged snapshot must be reconciled before replacement.')
    else:
        print(f'Import created: {a.out}')
