"""A small deterministic database for the map layout and view tests (not a test itself)."""
import os
import random
import sys
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

TS = '2026-01-01T00:00:00+00:00'


def make(path, people=3000, sources=30, seed=7, owner='fortun8te'):
    """Bulk-load with triggers off, then reopen so every summary and queue trigger exists again."""
    rng = random.Random(seed)
    conn = db.init(str(path))
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND "
                            "(name LIKE 'lead_rev_%' OR name LIKE 'map_%')").fetchall():
        conn.execute(f'DROP TRIGGER {row[0]}')
    conn.execute("DELETE FROM settings WHERE key LIKE 'map_%_v1'")
    names = [f'src{i:02}' for i in range(sources)]
    conn.execute('INSERT INTO seeds(handle,added_at,is_me) VALUES(?,?,1)', (owner, TS))
    conn.executemany('INSERT INTO seeds(handle,added_at) VALUES(?,?)', ((n, TS) for n in names))
    weights = [1.0 / (i + 1) ** 0.9 for i in range(sources)]
    src_ids = {}
    pid = 0
    people_rows, edge_rows = [], []
    for name in [owner, *names[: sources // 2]]:
        pid += 1
        src_ids[name] = pid
        people_rows.append((pid, name, name.title(), 1000 + pid, TS, TS))
    first = pid + 1
    for i in range(people):
        pid += 1
        people_rows.append((pid, f'user{i:05}', f'User {i}', rng.randrange(100, 200000), TS, TS))
        primary = rng.choices(range(sources), weights)[0]
        chosen = {primary}
        if rng.random() < 0.3:
            chosen.add((primary + rng.randrange(1, 4)) % sources)
        if rng.random() < 0.06:
            chosen.add(rng.randrange(sources))
        for s in chosen:
            edge_rows.append((names[s], pid, 'followers' if rng.random() < 0.5 else 'following'))
        if rng.random() < 0.05:
            edge_rows.append((owner, pid, 'followers' if rng.random() < 0.5 else 'following'))
        if rng.random() < 0.01:
            edge_rows.append((owner, pid, 'followers'))
            edge_rows.append((owner, pid, 'following'))
    conn.executemany('INSERT INTO people(id,handle,name,followers,first_seen,updated_at) VALUES(?,?,?,?,?,?)', people_rows)
    conn.executemany('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)', ((s, p, d, TS) for s, p, d in edge_rows))
    conn.executemany('INSERT OR IGNORE INTO edge_evidence VALUES(?,?,?,?,?,?)', ((s, p, d, 1, TS, TS) for s, p, d in edge_rows))
    fits = {}
    verdicts, marks, contexts = [], [], []
    for p in range(first, pid + 1):
        r = rng.random()
        if r < 0.35:
            fit = int(rng.random() ** 0.6 * 100) if rng.random() < 0.09 else None
            fits[p] = fit
            verdicts.append((p, int(rng.random() * 100), 'maybe', fit))
        r = rng.random()
        if r < 0.03:
            status = rng.choice(('interested', 'contacted', 'talking', 'spoke_before', 'client', 'no', 'no', 'contacted'))
            marks.append((p, status, TS))
        if rng.random() < 0.004:
            contexts.append((p, '["friend"]', rng.choice((None, 'briefly', 'know_them', 'close')), TS))
    conn.executemany('INSERT INTO verdicts(person_id,score,tier,content_fit) VALUES(?,?,?,?)', verdicts)
    conn.executemany('INSERT INTO marks(person_id,status,updated_at) VALUES(?,?,?)', marks)
    conn.executemany('INSERT INTO owner_context(person_id,relationships,familiarity,updated_at) VALUES(?,?,?,?)', contexts)
    # a source of Michael's that is a client, so closeness has something to follow
    conn.execute("INSERT INTO marks(person_id,status,updated_at) VALUES(?,?,?) ON CONFLICT(person_id) DO UPDATE SET status='client'",
                 (src_ids[names[1]], 'client', TS))
    conn.commit()
    conn.close()
    conn = db.init(str(path))   # recreates triggers and rebuilds every summary
    conn.close()
    return {'first': first, 'last': pid, 'sources': names, 'owner': owner, 'source_ids': src_ids}
