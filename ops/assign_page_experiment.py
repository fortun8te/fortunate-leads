"""Create fresh trials: followers 25/50, or pinned-viewer following 50/100/200.

Dry run by default. No Instagram request is made by this script. Existing jobs,
cursors, pacing, and account budgets are left alone.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db  # noqa: E402


def candidates(conn, count, minimum, maximum, min_fit):
    pairs, span = count // 2, maximum - minimum + 1
    chosen = []
    for pair in range(pairs):
        low = minimum + span * pair // pairs
        high = minimum + span * (pair + 1) // pairs - 1
        rows = conn.execute(
            "SELECT p.handle,p.followers FROM people p JOIN verdicts v ON v.person_id=p.id "
            "LEFT JOIN marks m ON m.person_id=p.id "
            "WHERE p.followers BETWEEN ? AND ? AND v.prefilter>=? AND p.is_private=0 "
            "AND p.ig_id IS NOT NULL AND instr(p.handle,'~')=0 AND coalesce(m.status,'')!='no' "
            "AND NOT EXISTS(SELECT 1 FROM seeds s WHERE s.handle=p.handle) "
            "AND NOT EXISTS(SELECT 1 FROM edges e WHERE e.seed=p.handle) "
            "AND EXISTS(SELECT 1 FROM current_edges e WHERE e.person_id=p.id) "
            "ORDER BY p.followers,p.id LIMIT 2", (low, high, min_fit)).fetchall()
        if len(rows) != 2:
            return []
        chosen.extend(rows)
    return chosen


def current_extension(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    for (version,) in conn.execute('SELECT version FROM accounts WHERE last_seen>=?', (cutoff,)):
        parts = str(version or '').split('.')
        if all(part.isdigit() for part in parts) and tuple(int(part) for part in (parts + ['0', '0'])[:3]) >= (3, 9, 12):
            return True
    return False


def following_trial(conn, targets, viewer_id, priority=280, apply=False):
    """One fresh target per arm. Target/cursor/viewer never changes within an arm."""
    targets = [db.norm_handle(target) for target in targets]
    if len(targets) != 3 or len(set(targets)) != 3 or not all(targets):
        raise ValueError('following trial needs three distinct new targets, in 50/100/200 order')
    if not isinstance(viewer_id, str) or not viewer_id.isdigit():
        raise ValueError('following trial needs --viewer-ig-id')
    viewers = conn.execute('SELECT * FROM accounts WHERE ig_id=?', (viewer_id,)).fetchall()
    if not viewers or any(row['is_main'] for row in viewers):
        raise ValueError('viewer must be a known alternate account')
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    compatible = False
    for row in viewers:
        parts = str(row['version'] or '').split('.')
        compatible |= bool(len(parts) == 3 and all(p.isdigit() for p in parts) and
                           tuple(map(int, parts)) >= (3, 9, 21) and (row['last_seen'] or '') >= cutoff)
    if apply and not compatible:
        raise ValueError('pinned alternate needs extension 3.9.21 online before creating trial jobs')
    plan = []
    for target, size in zip(targets, (50, 100, 200)):
        person = conn.execute('SELECT ig_id,is_private,following FROM people WHERE handle=?', (target,)).fetchone()
        if not person or not person['ig_id'] or person['is_private'] != 0:
            raise ValueError(f'{target} needs a known public profile with stable ID')
        if any(conn.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (target,)).fetchone()
               for table, column in [('seeds','handle'),('lists','seed'),('jobs','seed'),('edges','seed')]):
            raise ValueError(f'{target} already has collection history; use a fresh target')
        plan.append({'target':target,'target_ig_id':person['ig_id'],'following':person['following'],
                     'requested_count':size,'viewer_ig_id':viewer_id,'cursor':None})
    if apply:
        ids=[]
        for arm in plan:
            db.queue_list(conn, arm['target'], 'following', priority=priority, page_size=arm['requested_count'],
                          experiment_viewer_ig_id=viewer_id)
            ids.append(conn.execute("SELECT id FROM jobs WHERE seed=? AND direction='following' ORDER BY id DESC LIMIT 1", (arm['target'],)).fetchone()[0])
        db.set_setting(conn,'page_experiment_latest',{'created_at':db.now(),'job_ids':ids,
                       'direction':'following','viewer_ig_id':viewer_id,'sizes':[50,100,200]})
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--count', type=int, default=4)
    parser.add_argument('--min-followers', type=int, default=500)
    parser.add_argument('--max-followers', type=int, default=1200)
    parser.add_argument('--min-fit', type=int, default=50)
    parser.add_argument('--priority', type=int, default=280)
    parser.add_argument('--direction', choices=['followers','following'], default='followers')
    parser.add_argument('--targets', help='Three fresh handles, comma separated, for following 50/100/200')
    parser.add_argument('--viewer-ig-id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.count < 2 or args.count % 2 or args.count > 12 or args.min_followers >= args.max_followers:
        parser.error('use an even --count from 2 to 12 and an increasing follower range')
    if not args.db.is_file():
        parser.error('database does not exist')
    conn = sqlite3.connect(str(args.db)) if args.apply else sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if args.direction == 'following':
            if args.apply:
                conn.execute('BEGIN IMMEDIATE')
            plan = following_trial(conn, (args.targets or '').split(','), args.viewer_ig_id, args.priority, args.apply)
            print(json.dumps({'mode':'applied' if args.apply else 'dry_run','arms':plan}, indent=2))
            if args.apply:
                conn.commit()
            return
        if args.apply:
            conn.execute('BEGIN IMMEDIATE')
            if not current_extension(conn):
                print('No updated extension seen in the last 10 minutes; no jobs created.')
                return
        rows = candidates(conn, args.count, args.min_followers, args.max_followers, args.min_fit)
        if len(rows) != args.count:
            print('Not enough matched, new public network profiles; no jobs created.')
            return
        job_ids = []
        for index, row in enumerate(rows):
            # Adjacent follower counts form pairs; reverse every other pair.
            size = (25, 50)[(index % 2) ^ ((index // 2) % 2)]
            print(f"@{row['handle']} approx {row['followers']:,} followers → request {size}")
            if args.apply:
                if not db.queue_list(conn, row['handle'], 'followers', priority=args.priority, page_size=size):
                    raise RuntimeError('candidate changed while creating the experiment')
                job_ids.append(conn.execute(
                    "SELECT id FROM jobs WHERE kind='list' AND seed=? AND direction='followers' "
                    'ORDER BY id DESC LIMIT 1', (row['handle'],)).fetchone()[0])
        if args.apply:
            db.set_setting(conn, 'page_experiment_latest', {
                'created_at': db.now(), 'job_ids': job_ids})
            conn.commit()
            print('Created new follower jobs. Existing account pacing is unchanged.')
        else:
            print('Dry run; pass --apply after the updated extensions are online.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
