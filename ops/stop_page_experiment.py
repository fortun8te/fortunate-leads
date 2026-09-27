"""Stop the latest page-size trial, preserving collected links and partial coverage.

Dry run by default. With --apply, queued or leased trial jobs are parked. A
result already in flight becomes stale; ordinary scraping remains available.
"""
import argparse
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error('database does not exist')
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    try:
        if args.apply:
            conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT value FROM settings WHERE key='page_experiment_latest'").fetchone()
        cohort = json.loads(row['value']) if row else {}
        ids = cohort.get('job_ids', []) if isinstance(cohort, dict) else []
        ids = [job_id for job_id in ids if type(job_id) is int and job_id > 0]
        if not ids:
            print('No page-size trial is recorded.')
            return
        marks = ','.join('?' for _ in ids)
        jobs = conn.execute(f"SELECT id,seed,direction,state,page_size FROM jobs WHERE id IN ({marks}) "
                            "AND kind='list' ORDER BY id", ids).fetchall()
        active = [job for job in jobs if job['state'] in ('queued', 'leased')]
        for job in jobs:
            print(f"Job {job['id']} @{job['seed']}: {job['state']}, request {job['page_size']}")
        if args.apply:
            for job in active:
                conn.execute("UPDATE jobs SET state='error',leased_until=NULL,lane=NULL,lease_token=NULL,"
                             'retry_not_before=NULL WHERE id=?', (job['id'],))
                conn.execute("UPDATE lists SET state='partial',"
                             "error='Page-size trial stopped; saved links are partial.' "
                             'WHERE seed=? AND direction=? AND state!=\'done\'', (job['seed'],job['direction']))
            conn.commit()
            print(f'Stopped {len(active)} active trial jobs; saved links remain.')
        else:
            print(f'Dry run; --apply would stop {len(active)} active trial jobs.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
