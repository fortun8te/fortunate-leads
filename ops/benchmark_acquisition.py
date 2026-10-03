#!/usr/bin/env python3
"""Measure local saved-list intake with a synthetic graph; never Instagram speed.

Creates an isolated temporary database and JSONL fixture, checks exact edge
counts and replay idempotency, then removes the fixture unless --keep is set.
"""
import argparse
import gc
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import acquisition
import db
import list_import


def benchmark(edges=100000, keep=False):
    if type(edges) is not int or not 1 <= edges <= 1000000:
        raise ValueError('Use between 1 and 1,000,000 synthetic connections.')
    if shutil.disk_usage(tempfile.gettempdir()).free < 3 * 1024 ** 3:
        raise ValueError('At least 3 GiB temporary free space is required.')
    folder = Path(tempfile.mkdtemp(prefix='fortunate-graph-benchmark-'))
    path, source = folder / 'graph.sqlite', folder / 'saved-lists.jsonl'
    conn = None
    try:
        conn = db.init(path)
        # Reuse 1,000 member identities across distinct source accounts. This
        # exercises actual graph integration and degree changes, not only inserts.
        members = [{'ig_id': str(2000000 + i), 'handle': 'fixture_member_' + str(i)} for i in range(1000)]
        with source.open('w') as out:
            for start in range(0, edges, 1000):
                owner = start // 1000
                out.write(json.dumps({'source_owner_id': str(1000000 + owner),
                    'source_owner_handle': 'fixture_owner_' + str(owner), 'direction': 'following',
                    'observed_at': '2026-01-01T00:00:00+00:00',
                    'members': members[:min(1000, edges - start)]}, separators=(',', ':')) + '\n')
        t = time.perf_counter()
        preview = list_import.preview(conn, source)
        preview_seconds = time.perf_counter() - t
        t = time.perf_counter()
        imported = list_import.apply(conn, source, preview['sha256'])
        import_seconds = time.perf_counter() - t
        totals = acquisition.stats(conn)
        mapped = conn.execute('SELECT count(*) FROM current_edges').fetchone()[0]
        if totals['canonical_edges'] != edges or mapped != edges:
            raise AssertionError('Canonical and visible graph counts must equal the fixture.')
        before = conn.total_changes
        t = time.perf_counter()
        replay = list_import.apply(conn, source, preview['sha256'])
        replay_seconds = time.perf_counter() - t
        if not replay['replayed'] or conn.total_changes != before:
            raise AssertionError('Replay must make no changes.')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return {'fixture': 'synthetic_saved_lists', 'instagram_requests': 0,
                'measures': 'local_import_only_not_live_collection', 'connections': edges,
                'profile_identities': totals['nodes'], 'mapped_connections': mapped,
                'preview_seconds': round(preview_seconds, 3), 'import_seconds': round(import_seconds, 3),
                'imported_connections_per_second': round(edges / import_seconds, 1),
                'replay_seconds': round(replay_seconds, 3), 'replay_writes': conn.total_changes - before,
                'database_bytes': path.stat().st_size, 'source_bytes': source.stat().st_size,
                'temporary_directory': str(folder) if keep else None}
    finally:
        if conn:
            conn.close()
        if not keep:
            gc.collect()
            shutil.rmtree(folder)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edges', type=int, default=100000)
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()
    print(json.dumps(benchmark(args.edges, args.keep), indent=2))
