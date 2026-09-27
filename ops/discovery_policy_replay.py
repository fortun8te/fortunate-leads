"""Read-only recorded-page replay; a counterfactual, never a live throughput test.

Compare original job order with bounded completion selection on the same saved
cohort, using recorded pages and distinct people unknown at the cohort start.
Profile size estimates come from the current snapshot, so this is retrospective.
Partial lists remain partial even after their last recorded page. Each started
target is charged one extra request for a lookup. Unknown future pages, errors,
wall-clock pacing and actual lookup reuse are not simulated.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from discovery_policy import rank_for_completion


def replay(conn, created_at, budgets=(50, 100, 200, 400)):
    rows = [dict(r) for r in conn.execute(
        "SELECT j.id,j.seed,j.state,p.following,0 AS manual_fit,80 AS content_fit,"
        "NULL AS owner_status,'[]' AS relationships FROM jobs j "
        "JOIN people p ON p.handle=j.seed COLLATE NOCASE "
        "WHERE j.kind='list' AND j.direction='following' AND j.created_at=? "
        "AND EXISTS(SELECT 1 FROM pages pg WHERE pg.job_id=j.id) ORDER BY j.id", (created_at,))]
    ids = {r['id'] for r in rows}
    pages = defaultdict(list)
    for row in conn.execute('SELECT job_id,at,users FROM pages ORDER BY at'):
        if row['job_id'] in ids:
            pages[row['job_id']].append(dict(row))
    if not rows:
        raise ValueError('No recorded following pages in cohort')
    start = min(p['at'] for ps in pages.values() for p in ps)
    novel = defaultdict(set)
    for row in conn.execute(
        'SELECT m.job_id,m.observed_at,m.person_id FROM list_members m JOIN people p ON p.id=m.person_id '
        'WHERE julianday(p.first_seen)>=julianday(?)', (start,)):
        if row['job_id'] in ids:
            novel[(row['job_id'], row['observed_at'])].add(row['person_id'])
    planned, pending = [], list(rows)
    while pending:
        selected = rank_for_completion(pending, len(planned))[0]
        planned.append(selected)
        pending.remove(selected)
    results = {}
    for name, ordered in [('original_job_order', rows), ('bounded_completion', planned)]:
        results[name] = []
        for budget in budgets:
            spent, page_count, complete, seen, touched = 0, 0, [], set(), []
            for target in ordered:
                if spent == budget:
                    break
                touched.append(target['seed'])
                spent += 1  # Conservative target-lookup allowance for both policies.
                recorded = pages[target['id']]
                used = recorded[:budget-spent]
                for page in used:
                    seen.update(novel[(target['id'], page['at'])])
                spent += len(used)
                page_count += len(used)
                if len(used) == len(recorded) and target['state'] == 'done':
                    complete.append(target['seed'])
            results[name].append({'modeled_request_budget': budget, 'requests_used': spent,
                                  'pages_used': page_count,
                                  'completed_lists': len(complete), 'new_distinct_people': len(seen),
                                  'started_targets': len(touched)})
    return {'cohort_created_at': created_at, 'cohort_first_page': start,
            'recorded_targets': len(rows), 'recorded_pages': sum(map(len, pages.values())),
            'limitations': __doc__.strip(), 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--cohort', default='2026-09-26T16:40:01.129730+00:00')
    args = parser.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        print(json.dumps(replay(conn, args.cohort), indent=2))
