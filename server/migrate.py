import argparse
import json
import sqlite3
import sys
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


def migrate(old_path, out_path, batch_path):
    old = sqlite3.connect(f'file:{old_path}?mode=ro', uri=True)
    old.row_factory = sqlite3.Row
    conn = db.init(str(out_path))
    ts = db.now()

    ents = {r['id']: r for r in old.execute("SELECT id, handle, platform_id, created_at FROM entities WHERE platform='instagram'")}
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
    for eid, e in ents.items():
        p = {k: v for k, (v, _) in profile.get(eid, {}).items() if v not in (None, '')}
        p.update(handle=e['handle'], ig_id=e['platform_id'])
        if p.get('bio'):
            p['bio_at'] = profile[eid]['bio'][1]
        pid[eid] = db.upsert_person(conn, p, ts)
        conn.execute('UPDATE people SET first_seen=min(first_seen, ?) WHERE id=?', (e['created_at'], pid[eid]))

    for h in [ents[i]['handle'] for i in seed_ids] + commanded + batch:
        conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (h, ts))
    for i in seed_ids:
        if ents[i]['platform_id']:
            conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (ents[i]['platform_id'], ents[i]['handle']))
    conn.execute('UPDATE seeds SET is_me=1 WHERE handle=?', (ME,))

    skipped = 0
    for src, dst, day in old.execute("SELECT src, dst, observed_date FROM edges WHERE relationship='follows'"):
        if src not in ents or dst not in ents or not ({src, dst} & seed_ids):
            skipped += 1
            continue
        if dst in seed_ids:  # src follows the seed
            db.add_edge(conn, ents[dst]['handle'], pid[src], 'followers', day)
        if src in seed_ids:  # the seed follows dst
            db.add_edge(conn, ents[src]['handle'], pid[dst], 'following', day)

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
        conn.execute('INSERT OR REPLACE INTO lists(seed, direction, state, received, total, updated_at) VALUES(?,?,?,?,?,?)',
                     (seed, direction, 'done' if done else 'paused', received, total, lst['at']))

    for i, h in enumerate(batch):
        for direction in ('followers', 'following'):
            db.queue_list(conn, h, direction, priority=len(batch) - i)

    for r in old.execute('SELECT entity_id, relation, note, added_at FROM known_people'):
        if r['entity_id'] in pid:
            conn.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?,?)', (pid[r['entity_id']], 'client' if r['relation'] == 'client' else 'known',
                                                                          f"{r['relation']}: {r['note'] or ''}".strip(': '), r['added_at']))
    db.migrate_statuses(conn)   # 'known' -> tag 'Already know them'
    conn.commit()
    old.close()

    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    summary = {'people': q('SELECT count(*) FROM people'), 'edges': q('SELECT count(*) FROM edges'),
               'seeds': q('SELECT count(*) FROM seeds'), 'bios': q("SELECT count(*) FROM people WHERE coalesce(bio,'')!=''"),
               'lists_done': q("SELECT count(*) FROM lists WHERE state='done'"),
               'lists_open': q("SELECT count(*) FROM lists WHERE state!='done'"),
               'list_jobs': q("SELECT count(*) FROM jobs WHERE kind='list' AND state='queued'"),
               'marks': q('SELECT count(*) FROM marks'), 'skipped_edges': skipped}
    conn.close()
    return summary


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', default=str(OLD))
    ap.add_argument('--out', default=str(OUT))
    ap.add_argument('--batch', default=str(BATCH))
    a = ap.parse_args()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    for k, v in migrate(a.old, a.out, a.batch).items():
        print(f'{k:>14}  {v}')
