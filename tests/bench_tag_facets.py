"""Explicit preparation and independent legacy-query comparison on a DB copy."""
import argparse
import hashlib
import json
import resource
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server' / 'tests'))
import tag_facets
import server
from test_tag_facets_scale import legacy_facets


def main():
    parser = argparse.ArgumentParser(description='Measure prepared tag facets on an explicit stopped COPY only.')
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.db.exists() or shutil.disk_usage(args.db.parent).free < 8 * 1024**3:
        parser.error('An existing copy and at least 8 GiB free are required')
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA temp_store=FILE')
    conn.execute('PRAGMA cache_size=-16384')
    conn.execute('PRAGMA mmap_size=0')
    try:
        result = {'people': conn.execute('SELECT count(*) FROM people').fetchone()[0]}
        start = time.perf_counter()
        expected = legacy_facets(conn, {})
        result['legacy_seconds'] = time.perf_counter() - start
        start = time.perf_counter()
        with conn:
            tag_facets.prepare(conn)
        result['prepare_seconds'] = time.perf_counter() - start
        result['projection_rows'] = conn.execute('SELECT count(*) FROM tag_facet_rows').fetchone()[0]
        result['summary_rows'] = conn.execute('SELECT count(*) FROM tag_facet_totals').fetchone()[0]
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            actual = server.tag_facets(conn, {})
            samples.append((time.perf_counter() - start) * 1000)
            assert actual == expected, 'Prepared facets differ from original exact SQL'
        result['samples_ms'] = samples
        result['facet_count'] = len(actual)
        result['facet_digest_sha256'] = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
        result['oracle_equal'] = True
        # Roll back ingestion samples; leave the copied fixture unchanged.
        # Both samples retain all other application triggers.
        ingest = {}
        for prepared in (True, False):
            conn.execute('SAVEPOINT ingest_sample')
            try:
                conn.execute("UPDATE settings SET value=? WHERE key='tag_facets_ready'", ('true' if prepared else 'false',))
                high = conn.execute('SELECT coalesce(max(id),0) FROM people').fetchone()[0]
                start = time.perf_counter()
                for n in range(100):
                    pid = high + n + 1
                    conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?, 'now','now')", (pid, f'facet_benchmark_{pid}'))
                    conn.execute("INSERT INTO verdicts(person_id,tier,content_fit,role) VALUES(?,'hot',80,'buyer')", (pid,))
                    conn.execute("INSERT INTO tags VALUES(?,'Benchmark label','signal','manual')", (pid,))
                    conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'interested')", (pid,))
                ingest['prepared' if prepared else 'unprepared'] = (time.perf_counter() - start) * 1000
                if prepared:
                    assert tag_facets.default(conn) == legacy_facets(conn, {})
            finally:
                conn.execute('ROLLBACK TO SAVEPOINT ingest_sample')
                conn.execute('RELEASE SAVEPOINT ingest_sample')
        result['ingest_100_profiles_ms'] = ingest
        result['person_refresh_query_plan'] = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + tag_facets._rows('123'))]
        result['module_sha256'] = hashlib.sha256(Path(tag_facets.__file__).read_bytes()).hexdigest()
        result['peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
        result['database_bytes'] = args.db.stat().st_size
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2), flush=True)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
