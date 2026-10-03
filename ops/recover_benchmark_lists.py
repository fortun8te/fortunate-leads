#!/usr/bin/env python3
"""Export previously captured benchmark list observations, without Instagram calls.

Sources and optional identity database are read-only. Only confirmed saved
observations are exported, never benchmark baseline rows or invented handles.
The output is input for import_lists.py, not a claim of current/full coverage.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def _open(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('BEGIN')
    return conn


def recover(sources, output, identity_db=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new output file; existing captures are never overwritten.')
    identities = _open(identity_db) if identity_db else None
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.recovered-', dir=output.parent)
    counts = {'pages': 0, 'observations': 0, 'members_with_known_handles': 0,
              'unresolved_member_observations': 0, 'skipped_unconfirmed': 0}
    try:
        with os.fdopen(fd, 'w') as target:
            for source in sources:
                with closing(_open(source)) as conn:
                    for row in conn.execute('SELECT r.request_id,r.started_at,r.finished_at,r.result,'
                        't.target_id,t.target_handle,t.direction,t.route,t.warmup '
                        'FROM requests r JOIN tasks t USING(task_id) ORDER BY r.started_at,r.request_id'):
                        observations = conn.execute('SELECT src,dst FROM observations WHERE request_id=? '
                                                    'ORDER BY src,dst', (row['request_id'],)).fetchall()
                        if not observations:
                            continue
                        result = json.loads(row['result'] or '{}')
                        if (result.get('actual_http_requests') != 1 or result.get('transport_completed') is not True
                                or row['finished_at'] is None):
                            counts['skipped_unconfirmed'] += 1
                            continue
                        members = []
                        for observed in observations:
                            src, dst = str(observed['src']), str(observed['dst'])
                            if ((row['direction'] == 'following' and src != str(row['target_id'])) or
                                    (row['direction'] == 'followers' and dst != str(row['target_id']))):
                                raise ValueError('Captured pair does not match its source list.')
                            member_id = dst if row['direction'] == 'following' else src
                            member = {'ig_id': member_id}
                            known = identities.execute('SELECT handle FROM people WHERE ig_id=?',
                                                       (member_id,)).fetchone() if identities else None
                            if known and '~' not in known['handle']:
                                member['handle'] = known['handle']
                                counts['members_with_known_handles'] += 1
                            else:
                                counts['unresolved_member_observations'] += 1
                            members.append(member)
                        owner = identities.execute('SELECT handle FROM people WHERE ig_id=?',
                                                    (str(row['target_id']),)).fetchone() if identities else None
                        owner_handle = owner['handle'] if owner else row['target_handle']
                        # Split only for the local file's bounded transaction format.
                        # Each chunk retains the original request ID and capture time.
                        for start in range(0, len(members), 2000):
                            page = {'source_owner_id': str(row['target_id']),
                                    'direction': row['direction'],
                                    'observed_at': datetime.fromtimestamp(row['finished_at'], timezone.utc).isoformat(),
                                    'members': members[start:start + 2000],
                                    'source_context': {'route': row['route'], 'run_id': row['request_id'],
                                                       'warmup': bool(row['warmup']),
                                                       'stop_outcome': result.get('terminal_warning') or result.get('status', 'unknown')}}
                            if owner_handle and '~' not in owner_handle:
                                page['source_owner_handle'] = owner_handle
                            target.write(json.dumps(page, separators=(',', ':')) + '\n')
                            counts['pages'] += 1
                        counts['observations'] += len(members)
            target.flush()
            os.fsync(target.fileno())
        # Exclusive link avoids a race overwriting another capture at this path.
        os.link(temporary, output)
        return dict(counts, output=str(output), source='historical_benchmark_observations',
                    new_instagram_requests=0, coverage='observed_connections_only')
    finally:
        if identities:
            identities.close()
        os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--identity-db', type=Path)
    args = parser.parse_args()
    print(json.dumps(recover(args.source, args.output, args.identity_db), indent=2))
