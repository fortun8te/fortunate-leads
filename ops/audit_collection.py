#!/usr/bin/env python3
"""Audit saved list pages for lost or double-saved pages; optionally clean stale leases and duplicate profile jobs.

  python3 ops/audit_collection.py --db data/leads.sqlite            read-only audit (opens the file read-only)
  python3 ops/audit_collection.py --db copy.sqlite --cleanup        report what cleanup would do (dry-run)
  python3 ops/audit_collection.py --db copy.sqlite --cleanup --apply  do it (requeues expired leases, cancels duplicate pending profile jobs)

Never run --apply against the live database while the server is running.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import collection_audit  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--cleanup', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply and not args.cleanup:
        ap.error('--apply needs --cleanup')
    uri = f'file:{Path(args.db).resolve()}?mode={"rw" if args.apply else "ro"}'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    report = {k: {'count': v['count'], 'sample': v['sample'][:5]} for k, v in collection_audit.audit(conn).items()}
    out = {'audit': report}
    if args.cleanup:
        out['cleanup'] = collection_audit.cleanup(conn, apply=args.apply)
    print(json.dumps(out, indent=1, default=str))
    bad = [k for k in ('lost_pages', 'member_count_drift', 'received_drift', 'double_counted_events', 'orphan_requests', 'orphan_edges', 'duplicate_people') if report[k]['count']]
    print('PROBLEMS: ' + (', '.join(bad) if bad else 'none'), file=sys.stderr)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
