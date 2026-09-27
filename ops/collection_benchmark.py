"""Read-only collection benchmark. Counts new directed links, not returned page rows.

python3 ops/collection_benchmark.py --db data/leads.sqlite --hours 24
python3 ops/collection_benchmark.py --db data/leads.sqlite --since 2026-09-27T10:00:00Z --json
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError('timestamp needs a timezone')
    return dt.astimezone(timezone.utc)


def report(conn, since, until):
    start, end = since.isoformat(), until.isoformat()
    hours = (until - since).total_seconds() / 3600
    if hours <= 0:
        raise ValueError('until must follow since')
    new_by_direction = dict(conn.execute(
        'SELECT direction,count(*) FROM edges WHERE first_seen>=? AND first_seen<? GROUP BY direction',
        (start, end)))
    hourly = [dict(row) for row in conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:00:00Z',first_seen) AS hour,direction,count(*) AS new_links "
        'FROM edges WHERE first_seen>=? AND first_seen<? GROUP BY hour,direction ORDER BY hour,direction',
        (start, end))]
    coverage = [dict(row) for row in conn.execute(
        'SELECT direction,state,count(*) AS lists,coalesce(sum(received),0) AS received, '
        'coalesce(sum(total),0) AS reported_total FROM lists GROUP BY direction,state ORDER BY direction,state')]
    has_events = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collector_events'").fetchone())
    pages = [dict(row) for row in conn.execute(
        'SELECT p.lane,coalesce(a.handle,p.lane) AS account,j.direction,count(*) AS pages, '
        'coalesce(sum(p.users),0) AS returned_users FROM pages p '
        'LEFT JOIN jobs j ON j.id=p.job_id LEFT JOIN accounts a ON a.lane_id=p.lane '
        'WHERE p.at>=? AND p.at<? GROUP BY p.lane,j.direction ORDER BY pages DESC', (start, end))]
    errors, page_sizes, experiment_cohort, experiment_window, experiment_accounts, first_event = [], [], [], [], [], None
    experiment_at = None
    collector_new = {}
    if has_events:
        collector_new = dict(conn.execute(
            "SELECT direction,coalesce(sum(new_links),0) FROM collector_events "
            "WHERE at>=? AND at<? AND outcome='page' GROUP BY direction", (start, end)))
        errors = [dict(row) for row in conn.execute(
            "SELECT e.lane,coalesce(a.handle,e.lane) AS account,e.kind,e.direction,e.outcome,e.reason,"
            'count(*) AS events FROM collector_events e LEFT JOIN accounts a ON a.lane_id=e.lane '
            "WHERE e.at>=? AND e.at<? AND e.outcome!='page' "
            'GROUP BY e.lane,e.kind,e.direction,e.outcome,e.reason ORDER BY events DESC', (start, end))]
        page_sizes = [dict(row) for row in conn.execute(
            "SELECT requested_count,count(*) AS pages,coalesce(sum(returned_count),0) AS returned_users,"
            "coalesce(sum(new_links),0) AS new_links FROM collector_events "
            "WHERE at>=? AND at<? AND outcome='page' AND direction='followers' "
            'GROUP BY requested_count ORDER BY requested_count', (start, end))]
        marker = conn.execute("SELECT value FROM settings WHERE key='collector_events_started_at'").fetchone()
        first_event = json.loads(marker[0]) if marker else conn.execute('SELECT min(at) FROM collector_events').fetchone()[0]
    cohort_row = conn.execute("SELECT value FROM settings WHERE key='page_experiment_latest'").fetchone()
    cohort = json.loads(cohort_row[0]) if cohort_row else {}
    job_ids = cohort.get('job_ids', []) if isinstance(cohort, dict) else []
    job_ids = [job_id for job_id in job_ids if type(job_id) is int and job_id > 0]
    experiment_at = cohort.get('created_at') if isinstance(cohort, dict) and job_ids else None
    if job_ids and 'page_size' in {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}:
        marks = ','.join('?' for _ in job_ids)
        experiment_cohort = [dict(row) for row in conn.execute(
            "SELECT j.page_size,count(DISTINCT j.id) AS assigned_jobs,"
            "count(DISTINCT CASE WHEN j.state='done' THEN j.id END) AS current_done_jobs,"
            "count(DISTINCT CASE WHEN j.state='partial' THEN j.id END) AS current_partial_jobs,"
            "count(DISTINCT CASE WHEN j.state='error' THEN j.id END) AS current_stopped_or_error_jobs "
            "FROM jobs j "
            f"WHERE j.id IN ({marks}) AND j.kind='list' AND j.direction='followers' AND j.page_size IN (25,50) "
            'GROUP BY j.page_size ORDER BY j.page_size', job_ids)]
        if has_events:
            experiment_window = [dict(row) for row in conn.execute(
                "SELECT j.page_size,sum(CASE WHEN e.outcome='page' THEN 1 ELSE 0 END) AS accepted_pages,"
                "coalesce(sum(e.returned_count),0) AS returned_users,coalesce(sum(e.new_links),0) AS new_links,"
                "sum(CASE WHEN e.outcome='rate_limit' THEN 1 ELSE 0 END) AS rate_limits,"
                "sum(CASE WHEN e.reason='list_html_home_redirect' THEN 1 ELSE 0 END) AS redirects "
                "FROM collector_events e JOIN jobs j ON j.id=e.job_id "
                f"WHERE e.at>=? AND e.at<? AND j.id IN ({marks}) "
                "AND j.kind='list' AND j.direction='followers' AND j.page_size IN (25,50) "
                'GROUP BY j.page_size ORDER BY j.page_size', (start, end, *job_ids))]
            experiment_accounts = [dict(row) for row in conn.execute(
                "SELECT j.page_size,e.lane,coalesce(a.handle,e.lane) AS account,"
                "sum(CASE WHEN e.outcome='page' THEN 1 ELSE 0 END) AS accepted_pages,"
                "coalesce(sum(e.returned_count),0) AS returned_users,coalesce(sum(e.new_links),0) AS new_links,"
                "sum(CASE WHEN e.outcome='rate_limit' THEN 1 ELSE 0 END) AS rate_limits,"
                "sum(CASE WHEN e.reason='list_html_home_redirect' THEN 1 ELSE 0 END) AS redirects "
                "FROM collector_events e JOIN jobs j ON j.id=e.job_id "
                "LEFT JOIN accounts a ON a.lane_id=e.lane "
                f"WHERE e.at>=? AND e.at<? AND j.id IN ({marks}) "
                "AND j.kind='list' AND j.direction='followers' AND j.page_size IN (25,50) "
                'GROUP BY j.page_size,e.lane ORDER BY j.page_size,e.lane', (start, end, *job_ids))]
    return {'since': start, 'until': end, 'hours': round(hours, 3),
            'new_links_all_sources': new_by_direction, 'collector_new_links': collector_new,
            'collector_history_covers_window': bool(first_event and first_event <= start),
            'new_followers_all_sources_per_hour': round(new_by_direction.get('followers', 0) / hours, 1),
            'collector_new_followers_per_hour': round(collector_new.get('followers', 0) / hours, 1),
            'hourly_new_links': hourly, 'pages_by_account': pages, 'current_coverage': coverage,
            'errors_by_account_and_type': errors, 'follower_page_sizes_all_jobs': page_sizes,
            'experiment_cohort_current': experiment_cohort, 'experiment_in_window': experiment_window,
            'experiment_by_account_in_window': experiment_accounts,
            'experiment_started_at': experiment_at,
            'error_history_starts': first_event}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--since', type=utc)
    parser.add_argument('--until', type=utc)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    until = args.until or datetime.now(timezone.utc)
    since = args.since or until - timedelta(hours=args.hours)
    conn = sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = report(conn, since, until)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(data, indent=2))
        return
    print(f"{data['since']} → {data['until']} ({data['hours']} h)")
    print(f"New follower links saved by collector: {data['collector_new_links'].get('followers', 0):,} "
          f"({data['collector_new_followers_per_hour']:,.1f}/h across this window)")
    if not data['collector_history_covers_window']:
        print(f"All-source follower link estimate: {data['new_links_all_sources'].get('followers', 0):,} "
              f"({data['new_followers_all_sources_per_hour']:,.1f}/h); collector event history does not cover this window.")
    print(f"New following links saved by collector: {data['collector_new_links'].get('following', 0):,}")
    print('Pages by account:', data['pages_by_account'])
    print('Current list coverage:', data['current_coverage'])
    print('Errors by account and request type:', data['errors_by_account_and_type'])
    print('Follower page sizes (all jobs in window):', data['follower_page_sizes_all_jobs'])
    print('25/50 cohort (current state):', data['experiment_cohort_current'])
    print('25/50 experiment (in window):', data['experiment_in_window'])
    print('25/50 by account (in window):', data['experiment_by_account_in_window'])
    if not data['error_history_starts']:
        print('Account error history starts after the collector-events update is deployed.')


if __name__ == '__main__':
    main()
