"""Offline Laya queue selection benchmark; temporary synthetic database only.

Run: PYTHONPATH=server python3 server/tests/benchmark_laya_queue.py
"""
import os
import sqlite3
import tempfile
import time

import db
import server


N = 120_000
REPEATS = 30


def main():
    with tempfile.TemporaryDirectory() as directory:
        conn = db.init(os.path.join(directory, 'synthetic.sqlite'))
        conn.executemany(
            "INSERT INTO people(id,handle,bio,first_seen,updated_at) VALUES(?,?,?,?,?)",
            ((i, f'p{i}', 'bio' if i % 3 else None, 't', 't') for i in range(1, N + 1)))
        conn.executemany(
            'INSERT INTO verdicts(person_id,prefilter) VALUES(?,?)',
            ((i, i % 101) for i in range(1, N + 1)))
        conn.create_function('laya_hash', 6, server.laya_hash, deterministic=True)
        cached = []
        for row in conn.execute('SELECT id,handle,name,bio,category,website,followers FROM people WHERE id%5 != 0'):
            cached.append((row['id'], server.laya_hash(*(row[k] for k in
                ('handle','name','bio','category','website','followers'))), '{}', 50, 't'))
        conn.executemany('INSERT INTO laya VALUES(?,?,?,?,?)', cached)
        # This is the one-time migration/version rebuild, excluded from steady-state timing.
        rebuild_start = time.perf_counter()
        conn.execute('DELETE FROM laya_queue')
        conn.execute("""INSERT INTO laya_queue
            SELECT p.id,coalesce(p.bio,'')='',v.prefilter FROM people p
            LEFT JOIN laya l ON l.person_id=p.id LEFT JOIN verdicts v ON v.person_id=p.id
            WHERE l.person_id IS NULL OR l.input_hash IS NOT
              laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers)""")
        conn.commit()
        rebuild_ms = (time.perf_counter() - rebuild_start) * 1000
        old = """SELECT p.id FROM people p LEFT JOIN laya l ON l.person_id=p.id
            LEFT JOIN verdicts v ON v.person_id=p.id
            WHERE l.person_id IS NULL OR l.input_hash IS NOT
              laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers)
            ORDER BY coalesce(p.bio,'')='',v.prefilter DESC,p.id LIMIT 64"""
        new = """SELECT p.id FROM laya_queue q JOIN people p ON p.id=q.person_id
            ORDER BY q.bio_blank,q.prefilter DESC,q.person_id LIMIT 64"""
        def measure(query):
            start = time.perf_counter()
            result = None
            for _ in range(REPEATS):
                result = [r[0] for r in conn.execute(query)]
            return (time.perf_counter() - start) * 1000 / REPEATS, result
        old_ms, old_ids = measure(old)
        new_ms, new_ids = measure(new)
        assert old_ids == new_ids
        print(f'{N:,} people; {N//5:,} unscored; {REPEATS} selections; same first 64 IDs')
        print(f'old {old_ms:.3f} ms/selection; queued {new_ms:.3f} ms/selection; {old_ms/new_ms:.1f}x')
        print(f'one-time queue rebuild {rebuild_ms:.1f} ms')
        print([tuple(r) for r in conn.execute('EXPLAIN QUERY PLAN ' + new)])
        conn.close()


if __name__ == '__main__':
    main()
