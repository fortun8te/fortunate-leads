"""Timing of the hot read paths on a synthetic database (default 100k people).

    python3 tests/bench.py [--people 100000] [--db /tmp/bench.sqlite] [--runs 5]

Builds the DB once (kept at --db, rebuilt when --people changes), then prints the median ms of each
endpoint over --runs calls. Cached endpoints are shown cold (cache cleared) and warm.
"""
import argparse
import random
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db  # noqa: E402
import server  # noqa: E402

NICHES = ['Skincare', 'Apparel', 'Coffee', 'Supplements', 'Baby', 'Pets', 'Jewelry', 'Home', 'Fitness', 'Beauty']
ROLES = ['Founder', 'Brand', 'Creator', 'Agency', 'Personal', 'Coach']


def build(path, n, seeds=60):
    rnd = random.Random(7)
    Path(path).unlink(missing_ok=True)
    conn = db.init(path)
    ts = db.now()
    # Respect identity allocation even when /tmp/pfp contains older test art.
    first_id = conn.execute('SELECT value+1 FROM person_id_sequence WHERE singleton=1').fetchone()[0]
    names = [f'seed{i}' for i in range(seeds)]
    conn.executemany('INSERT INTO seeds(handle, added_at, is_me) VALUES(?,?,?)', [(s, ts, int(i == 0)) for i, s in enumerate(names)])
    people, edges, tags, verdicts, marks = [], [], [], [], []
    for pid in range(first_id, first_id + n):
        bio = f'founder of brand {pid} skincare shop' if rnd.random() < 0.3 else None
        people.append((pid, str(10 ** 9 + pid), f'user{pid}', f'User {pid}', bio, rnd.randint(10, 500000),
                       ts if bio else None, ts, ts))
        for s in rnd.sample(names, min(seeds, 1 + int(rnd.expovariate(1.4)))):
            edges.append((s, pid, rnd.choice(('followers', 'following')), ts))
        for t in rnd.sample(NICHES, 3) + [rnd.choice(ROLES), 'US' if rnd.random() < .5 else 'NL']:
            tags.append((pid, t, 'niche' if t in NICHES else 'role' if t in ROLES else 'signal', 'auto'))
        tier = rnd.choice(('hot', 'warm', 'cold', 'unread'))
        verdicts.append((pid, rnd.randint(0, 100), rnd.randint(0, 100), rnd.randint(0, 100), tier, 'rules', ts))
        if rnd.random() < 0.02:
            marks.append((pid, rnd.choice(('good', 'maybe', 'no', 'client')), ts))
    conn.executemany('INSERT INTO people(id, ig_id, handle, name, bio, followers, bio_at, first_seen, updated_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?)', people)
    conn.executemany('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)', edges)
    # The lead list counts current evidence, not discovery history alone.
    conn.executemany('INSERT OR IGNORE INTO edge_evidence(seed,person_id,direction,active,observed_at,checked_at) '
                     'VALUES(?,?,?,1,?,?)', ((s, p, d, ts, ts) for s, p, d, _ in edges))
    conn.executemany('INSERT OR IGNORE INTO tags VALUES(?,?,?,?)', tags)
    conn.executemany('INSERT INTO verdicts(person_id, prefilter, score, content_fit, tier, model, updated_at) '
                     'VALUES(?,?,?,?,?,?,?)', verdicts)
    conn.executemany('INSERT INTO marks(person_id, status, updated_at) VALUES(?,?,?)', marks)
    db.set_setting(conn, 'bench_people', n)
    db.set_setting(conn, 'bench_fixture_v2', True)
    db.set_setting(conn, 'qualify', True)
    conn.commit()
    conn.execute('ANALYZE')
    conn.close()
    return len(edges), len(tags)


def timed(fn, runs, before=None):
    out = []
    for _ in range(runs):
        if before:
            before()
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return statistics.median(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--people', type=int, default=100000)
    ap.add_argument('--db', default='/tmp/fl-bench.sqlite')
    ap.add_argument('--runs', type=int, default=5)
    a = ap.parse_args()
    fresh = not Path(a.db).exists()
    if not fresh:
        c = db.connect(a.db)
        try:
            fresh = db.get_setting(c, 'bench_people') != a.people or not db.get_setting(c, 'bench_fixture_v2', False)
        except Exception:
            fresh = True
        c.close()
    if fresh:
        t = time.perf_counter()
        e, tg = build(a.db, a.people)
        print(f'built {a.people} people, {e} edges, {tg} tags in {time.perf_counter() - t:.1f} s')
    db.init(a.db).close()   # migrations/indexes of the code under test
    server.CFG['db'] = a.db
    conn = db.connect(a.db)
    q = parse_qs
    clear = getattr(server, 'clear_caches', lambda: server.SEED_LINKS.__setitem__(0, None))
    pids = [r[0] for r in conn.execute('SELECT id FROM people ORDER BY random() LIMIT 200')]
    cases = [
        ('leads sort=score', lambda: server.api_leads(conn, q('limit=50'), {})),
        ('leads sort=connected', lambda: server.api_leads(conn, q('sort=connected&limit=50'), {})),
        ('leads min_lists=2', lambda: server.api_leads(conn, q('min_lists=2&limit=50'), {})),
        ('leads fit=strong', lambda: server.api_leads(conn, q('fit=strong&limit=50'), {})),
        ('tags unfiltered (cold)', lambda: server.api_tags(conn, q(''), {}), clear),
        ('tags unfiltered (warm)', lambda: server.api_tags(conn, q(''), {})),
        ('tags tags=Coffee (cold)', lambda: server.api_tags(conn, q('tags=Coffee'), {}), clear),
        ('map limit=400 (cold)', lambda: server.api_map(conn, q(''), {}), clear),
        ('map limit=400 (warm)', lambda: server.api_map(conn, q(''), {})),
        ('seed_links (cold)', lambda: server.seed_links(conn), clear),
        ('data_rev', lambda: server.data_rev(conn)),
        ('network_context 200', lambda: server.network_context(conn, pids)),
        ('edges_of x200', lambda: [server.edges_of(conn, p) for p in pids]),
        ('soak', lambda: server.soak(conn, server.datetime.now(server.timezone.utc))),
        ('plan_profiles query', lambda: (conn.execute('DELETE FROM jobs'), server.plan_profiles(conn))),
    ]
    print(f'{"case":28} {"median ms":>10}')
    for name, fn, *pre in cases:
        print(f'{name:28} {timed(fn, a.runs, pre[0] if pre else None):10.1f}')
    conn.close()


if __name__ == '__main__':
    main()
