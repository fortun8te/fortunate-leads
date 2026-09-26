"""Rollback-only write cost probe on an explicitly named synthetic fixture.

python3 tests/bench_map_writes.py /private/tmp/fl-map-scale-5m.sqlite
Never point this at an application database. All changes use SAVEPOINT rollback.
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db  # noqa: E402


def probe(conn, kind, enabled):
    conn.execute('SAVEPOINT map_write_probe')
    try:
        if not enabled:
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_%'").fetchall():
                conn.execute(f'DROP TRIGGER {row[0]}')
        ids = range(1, 501)
        stamp = '2026-07-01T00:00:00+00:00'
        t = time.perf_counter()
        if kind == 'new_members':
            conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                             (('benchwrite', pid, 'followers', stamp) for pid in ids))
            conn.executemany('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?)',
                             (('benchwrite', pid, 'followers', 1, stamp, stamp) for pid in ids))
        elif kind == 'active_flip':
            conn.executemany("UPDATE edge_evidence SET active=0 WHERE seed='seed01' AND person_id=?", ((pid,) for pid in ids))
        else:
            conn.executemany("UPDATE edge_evidence SET observed_at=? WHERE seed='seed01' AND person_id=?",
                             ((stamp, pid) for pid in ids))
        return round((time.perf_counter() - t) * 1000, 2)
    finally:
        conn.execute('ROLLBACK TO map_write_probe')
        conn.execute('RELEASE map_write_probe')


if __name__ == '__main__':
    path = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else None
    if path is None or not path.is_file() or 'fl-map-scale-' not in path.name:
        raise SystemExit('Pass an existing synthetic fl-map-scale-*.sqlite file')
    conn = db.connect(str(path))
    report = {'fixture': str(path), 'edges': conn.execute('SELECT count(*) FROM edges').fetchone()[0]}
    for kind in ('new_members', 'active_flip', 'timestamp_only'):
        samples = {str(enabled): [] for enabled in (True, False)}
        for i in range(4):
            for enabled in ((True, False) if i % 2 == 0 else (False, True)):
                samples[str(enabled)].append(probe(conn, kind, enabled))
        report[kind] = {key: {'runs_ms': values, 'median_ms': statistics.median(values)}
                        for key, values in samples.items()}
    print(json.dumps(report, indent=2))
    conn.close()
