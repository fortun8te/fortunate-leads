#!/usr/bin/env python3
"""Preview or import saved Instagram JSONL lists into an offline local database.

Preview: python3 ops/import_lists.py --db /path/offline.sqlite --file saved.jsonl
Apply:   add --apply --sha256 <preview sha256>. No network requests are made.
The production database is never a default. Work on a stopped/offline copy.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import list_import


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sha256', help='Exact SHA256 returned by preview')
    parser.add_argument('--batch-size', type=int, default=100)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error('--db must name an existing initialized offline database')
    production = Path('/Users/michael/Projects/Fortunate-Leads/app/data').resolve()
    target = args.db.resolve()
    if args.apply and (target.is_relative_to(production) or target.is_relative_to(Path('/Users/michael/fortunate-leads/data').resolve())):
        parser.error('Use an offline database copy; production imports are not supported by this command')
    if args.apply and not args.sha256:
        parser.error('--apply requires --sha256 from preview')
    conn = sqlite3.connect(target.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if args.apply:
            # Existing database schema only. Do not run startup migrations or collectors.
            result = list_import.apply(conn, args.file, args.sha256, args.batch_size)
        else:
            result = list_import.preview(conn, args.file)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
