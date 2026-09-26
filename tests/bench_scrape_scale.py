"""Synthetic, read-only timing of the scraping hot paths (no Instagram or live DB).

PYTHONPATH=server python3 tests/bench_scrape_scale.py --edges 250000 --members 100000
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ['FL_NO_ORSLOT'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import server  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--edges', type=int, default=250_000)
    ap.add_argument('--members', type=int, default=100_000)
    args = ap.parse_args()
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE people(id INTEGER PRIMARY KEY, handle TEXT, followers INT);
        CREATE TABLE marks(person_id INT PRIMARY KEY, status TEXT);
        CREATE TABLE edges(seed TEXT, person_id INT, direction TEXT, first_seen TEXT,
          PRIMARY KEY(seed,person_id,direction));
        CREATE INDEX edges_person_seed ON edges(person_id,seed);
        CREATE TABLE edge_evidence(seed TEXT, person_id INT, direction TEXT, active INT,
          observed_at TEXT, checked_at TEXT, PRIMARY KEY(seed,person_id,direction));
        CREATE INDEX edge_evidence_person ON edge_evidence(person_id,seed);
        CREATE VIEW current_edges AS SELECT e.seed,e.person_id,e.direction,e.first_seen,v.observed_at
          FROM edges e JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id
          AND v.direction=e.direction WHERE v.active=1;
        CREATE TABLE list_members(job_id INT, person_id INT, observed_at TEXT,
          PRIMARY KEY(job_id,person_id));
        CREATE TABLE list_runs(job_id INTEGER PRIMARY KEY, first_page_seen INT DEFAULT 0,
          total INT, total_source TEXT DEFAULT 'unknown', member_count INT NOT NULL DEFAULT 0);
        CREATE TRIGGER list_members_count_insert AFTER INSERT ON list_members BEGIN
          UPDATE list_runs SET member_count=member_count+1 WHERE job_id=NEW.job_id; END;
        CREATE TRIGGER list_members_count_delete AFTER DELETE ON list_members BEGIN
          UPDATE list_runs SET member_count=member_count-1 WHERE job_id=OLD.job_id; END;
    ''')
    people = max(20_000, args.edges // 5)
    seeds = 2_000
    c.executemany('INSERT INTO people(id,handle,followers) VALUES(?,?,?)',
                  ((i, f'p{i}', 100) for i in range(1, people + 1)))
    c.executemany('INSERT INTO marks(person_id,status) VALUES(?,?)',
                  ((i, 'interested' if i % 4 else 'no') for i in range(1, people + 1, 100)))
    rows = ((f's{((i // 5) * 17 + (i % 5) * 313) % seeds}', i // 5 + 1, 'followers', '')
            for i in range(args.edges))
    # Materialize once so both tables have precisely the same observations.
    edges = list(rows)
    c.executemany('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)', edges)
    c.executemany('INSERT OR IGNORE INTO edge_evidence VALUES(?,?,?,1,?,?)',
                  ((s, p, d, '', '') for s, p, d, _ in edges))
    c.execute('INSERT INTO list_runs(job_id) VALUES(1)')
    c.executemany('INSERT INTO list_members VALUES(1,?,?)',
                  ((i, '') for i in range(1, args.members + 1)))
    c.commit()
    ids = list(range(500, 525))
    start = time.perf_counter()
    result = server.network_context(c, ids, me='')
    network_ms = (time.perf_counter() - start) * 1_000
    start = time.perf_counter()
    members = {r[0] for r in c.execute('SELECT person_id FROM list_members WHERE job_id=1')}
    received = c.execute('SELECT count(*) FROM list_members WHERE job_id=1').fetchone()[0]
    member_ms = (time.perf_counter() - start) * 1_000
    start = time.perf_counter()
    tracked = c.execute('SELECT member_count FROM list_runs WHERE job_id=1').fetchone()[0]
    counter_ms = (time.perf_counter() - start) * 1_000
    print(f'edges={c.execute("SELECT count(*) FROM edges").fetchone()[0]} '
          f'members={args.members} profiles={len(result)} network_ms={network_ms:.2f} '
          f'member_set_and_count_ms={member_ms:.2f} member_set_size={len(members)} received={received} '
          f'tracked_count_ms={counter_ms:.3f} tracked={tracked}')


if __name__ == '__main__':
    main()
