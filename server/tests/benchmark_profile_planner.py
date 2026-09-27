"""Offline planner query benchmark; e.g. python3 server/tests/benchmark_profile_planner.py 100000."""
import sqlite3
import statistics
import sys
import time


def build(n):
    c = sqlite3.connect(':memory:')
    c.executescript('''
        CREATE TABLE people(id INTEGER PRIMARY KEY,handle TEXT UNIQUE,bio_at TEXT,is_private INT);
        CREATE TABLE verdicts(person_id INTEGER PRIMARY KEY,prefilter INT);
        CREATE TABLE edges(seed TEXT,person_id INT,direction TEXT,PRIMARY KEY(seed,person_id,direction));
        CREATE TABLE edge_evidence(seed TEXT,person_id INT,direction TEXT,active INT,
            PRIMARY KEY(seed,person_id,direction));
        CREATE VIEW current_edges AS SELECT e.seed,e.person_id FROM edges e JOIN edge_evidence v
            ON v.seed=e.seed AND v.person_id=e.person_id AND v.direction=e.direction WHERE v.active=1;
        CREATE TABLE map_person_degree(person_id INTEGER PRIMARY KEY,degree INT);
        CREATE TABLE jobs(kind TEXT,handle TEXT);
        CREATE TABLE seeds(handle TEXT PRIMARY KEY);
        CREATE TABLE marks(person_id INTEGER PRIMARY KEY,status TEXT);
        CREATE INDEX edge_evidence_person ON edge_evidence(person_id,seed);
        CREATE INDEX edges_person_seed ON edges(person_id,seed);
        CREATE INDEX jobs_handle ON jobs(handle);
    ''')
    c.executemany('INSERT INTO people VALUES(?,?,NULL,0)', ((i, f'p{i:07d}') for i in range(n)))
    c.executemany('INSERT INTO verdicts VALUES(?,?)', ((i, (i * 37) % 101) for i in range(n)))
    # Every fourth person has zero sources; every fifth nonzero person has a
    # second direction to one source (which must not increase the degree).
    edges = ((f's{k}', i, direction) for i in range(n) for k in range(i % 4)
             for direction in (('followers', 'following') if i % 5 == 0 else ('followers',)))
    c.executemany('INSERT INTO edges VALUES(?,?,?)', edges)
    c.execute('INSERT INTO edge_evidence SELECT seed,person_id,direction,1 FROM edges')
    c.execute('INSERT INTO map_person_degree SELECT person_id,count(DISTINCT seed) FROM current_edges GROUP BY person_id')
    c.executemany("INSERT INTO jobs VALUES('profile',?)", ((f'p{i:07d}',) for i in range(0, n, 97)))
    c.executemany("INSERT INTO marks VALUES(?,'no')", ((i,) for i in range(0, n, 113)))
    c.commit()
    return c


def query(degree, early):
    join = (('JOIN' if early else 'LEFT JOIN') + ' map_person_degree d ON d.person_id=p.id') if degree else ''
    expr = ('d.degree' if early else 'coalesce(d.degree,0)') if degree else (
        '(SELECT count(DISTINCT e.seed) FROM current_edges e WHERE e.person_id=p.id)')
    return f'''SELECT * FROM (SELECT p.handle,v.prefilter,{expr} AS n
        FROM people p JOIN verdicts v ON v.person_id=p.id {join}
        WHERE p.bio_at IS NULL AND coalesce(p.is_private,0)=0 AND v.prefilter>=?
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)
          AND p.handle NOT IN (SELECT handle FROM seeds) AND instr(p.handle,'~')=0
          AND p.id NOT IN (SELECT person_id FROM marks WHERE status='no')) WHERE n>=?
        ORDER BY prefilter DESC,n DESC,handle LIMIT ?'''


def run(n):
    c = build(n)
    for ranked in (False, True):
        if ranked:
            c.execute('CREATE INDEX verdicts_prefilter ON verdicts(prefilter DESC,person_id)')
        for early in (False, True):
            results = {}
            for label, degree in (('correlated', False), ('summary', True)):
                sql = query(degree, early)
                args = (25, 2 if early else 0, 200)
                samples = []
                for _ in range(4):
                    start = time.perf_counter()
                    rows = c.execute(sql, args).fetchall()
                    samples.append((time.perf_counter() - start) * 1000)
                results[label] = rows
                print(f'{n:,} people, rank_index={ranked}, early={early}, {label}: '
                      f'median {statistics.median(samples):.1f} ms '
                      f'(runs: {", ".join(f"{x:.1f}" for x in samples)})')
            assert results['correlated'] == results['summary'], 'planner selection changed'
    c.close()


if __name__ == '__main__':
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 100_000)
