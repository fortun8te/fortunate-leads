#!/usr/bin/env python3
"""Preview saved-content work and quarantine research on an offline copy only."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import enrichment_plan
import evidence_cleanup


def _connect(path, write=False):
    path = Path(path).expanduser().resolve(strict=True)
    if write:
        live_paths = (Path.home() / 'Projects/Fortunate-Leads/app/data/leads.sqlite',
                      Path.home() / 'fortunate-leads/data/leads.sqlite')
        if any(path == live.resolve() for live in live_paths):
            raise ValueError('Apply and restore require an offline database copy.')
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    if not write:
        conn.execute('PRAGMA query_only=ON')
    return conn


def _save(path, value):
    target = Path(path).expanduser()
    # Manifests contain no prose or notes, but keep them private too.
    with target.open('x') as stream:
        target.chmod(0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('plan', 'preview', 'restore-preview', 'quarantine', 'restore', 'copy'))
    parser.add_argument('--db', required=True)
    parser.add_argument('--out')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--manifest')
    parser.add_argument('--review-hash')
    parser.add_argument('--source')
    args = parser.parse_args()
    if args.operation == 'copy':
        if not args.source or Path(args.db).expanduser().exists():
            parser.error('Copy needs a source and a new database path.')
        source = _connect(args.source)
        target_path = Path(args.db).expanduser()
        target_path.touch(mode=0o600, exist_ok=False)
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        print(json.dumps({'copied': True, 'database': str(target_path.resolve())}))
        return
    write = args.operation in ('quarantine', 'restore')
    conn = _connect(args.db, write=write)
    try:
        if args.operation == 'plan':
            result = enrichment_plan.next_batch(conn, limit=args.limit or 64)
        elif args.operation == 'preview':
            result = evidence_cleanup.preview(conn, limit=args.limit or 1000)
        elif args.operation == 'restore-preview':
            result = evidence_cleanup.restore_preview(conn, limit=args.limit or 1000)
        else:
            if not args.manifest or not args.review_hash:
                parser.error('Apply needs the exact saved manifest and its reviewed hash.')
            manifest = json.loads(Path(args.manifest).read_text())
            with conn:
                function = evidence_cleanup.quarantine if args.operation == 'quarantine' else evidence_cleanup.restore
                result = function(conn, manifest, args.review_hash)
        if args.out:
            _save(args.out, result)
            print(json.dumps({'saved': str(Path(args.out).resolve()),
                              'count': result.get('count', result.get('inspected')),
                              'review_hash': result.get('review_hash', result.get('manifest_hash'))}))
        else:
            print(json.dumps(result, indent=2))
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
