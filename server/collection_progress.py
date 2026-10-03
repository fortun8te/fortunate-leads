"""Current list-queue progress and a cautious, observed ETA for its known part."""
from datetime import datetime, timedelta, timezone
import math

import accounts
import control
import db

WINDOW = timedelta(minutes=30)
MIN_SPAN = 300
MIN_PAGES = 6
MAX_EVENTS = 2000


def summary(conn, lists, accts, now=None):
    now = now or datetime.now(timezone.utc)
    if db.get_setting(conn, 'follower_lists') is False:
        lists = [row for row in lists if row.get('direction') == 'following']
    pending = [row for row in lists if row['completion'] in ('waiting', 'collecting')]
    known = [row for row in pending if row.get('expected_source') == 'current_run'
             and isinstance(row.get('saved_current_run'), int) and row.get('run_job_id') is not None]
    left = sum(max(0, row['expected'] - row['saved_current_run']) for row in known)
    out = {'finished': sum(row['completion'] == 'complete' for row in lists),
           'pending': len(pending), 'partial': sum(row['completion'] == 'partial' for row in lists),
           'limited': sum(row['completion'] == 'partial' and str(row.get('error') or '').startswith('Instagram limited this list;') for row in lists),
           'needs_review': sum(row['completion'] in ('blocked', 'unverified') for row in lists),
           'total': len(lists), 'known_entries_left': left, 'unknown_lists': len(pending) - len(known),
           'open_ended': db.get_setting(conn, 'auto_discover', True) is True, 'eta': None,
           'status': 'warming_up', 'message': 'Waiting for saved pages'}
    def waiting(status, message):
        return dict(out, status=status, message=message)
    if control.stage_paused(conn, 'lists'):
        return waiting('paused', 'Lists paused · progress saved')
    shared = control.shared_collection_wait(conn, now)
    if shared:
        return waiting('waiting', 'Waiting for Instagram')
    if not pending:
        return waiting('idle', 'Current queue finished' if lists and not out['partial'] and not out['needs_review']
                       else 'No lists waiting')
    rows = {row['lane_id']: row for row in accounts.rows(conn)}
    # ETA uses the same main-account reservation as list assignment. An idle
    # protected main cannot stand in for an offline or daily-capped alternate.
    assigned = [account for account in accts if (account['role'] or 'both') in ('lists', 'both')
                and account['lane_id'] in rows
                and not accounts.collection_protected(conn, rows[account['lane_id']])]
    online = [account for account in assigned if account['online'] and not account['paused']]
    usable = [account for account in online if not account['hold'] and account.get('status') != 'connection_error']
    ready = [account for account in usable
             if not accounts.list_wait_until(rows[account['lane_id']], now)
             and accounts.identity_owner(conn, rows[account['lane_id']], now)
             and not accounts.identity_cooling(conn, rows[account['lane_id']], 'list', now)]
    if not ready:
        if usable:
            return waiting('waiting', 'Waiting for Instagram')
        if any(account['hold'] == 'challenge' for account in online):
            return waiting('waiting', 'Complete the Instagram security check')
        if any(account.get('status') == 'connection_error' for account in online):
            return waiting('waiting', 'Instagram connection trouble · retrying safely')
        if any(account['hold'] == 'login' for account in online):
            return waiting('waiting', 'Sign in to Instagram')
        return waiting('waiting', 'Connected accounts are paused' if assigned and all(account['paused'] for account in assigned)
                       else 'Instagram accounts are offline')
    usable = ready
    budget_ready = [account for account in usable
                    if (not account['budget'].get('list')
                        or account['today'].get('list', 0) < account['budget']['list'])
                    and accounts.list_budget_left(conn, rows[account['lane_id']], now)]
    if not budget_ready:
        return waiting('waiting', 'Daily limits reached · resumes tomorrow')
    if out['unknown_lists'] == len(pending):
        return waiting('unknown_totals', 'List sizes will appear as they are read')
    if not left:
        return waiting('confirming', 'Confirming list completion')
    lane_ids = {account['lane_id'] for account in budget_ready}
    job_ids = {row['run_job_id'] for row in known}
    events = conn.execute("""SELECT e.at,e.lane,e.job_id,e.saved_entries FROM collector_events e
        WHERE e.at>=? AND e.kind='list' AND e.outcome='page' AND e.saved_entries IS NOT NULL
        ORDER BY e.at LIMIT ?""", (accounts.iso(now - WINDOW), MAX_EVENTS + 1)).fetchall()
    if len(events) > MAX_EVENTS:
        return waiting('warming_up', 'Measuring current pace')
    events = [event for event in events if event['lane'] in lane_ids and event['job_id'] in job_ids]
    if len(events) < MIN_PAGES:
        return out
    first, latest = accounts.utc(events[0]['at']), accounts.utc(events[-1]['at'])
    span = (now - first).total_seconds()
    if span < MIN_SPAN or now - latest > timedelta(minutes=5):
        return waiting('warming_up', 'Waiting for more saved pages')
    # Use wall-clock time (including waits), distinct current-run additions, and
    # only lanes still available. Old attempts and repeated people add no speed.
    added = sum(max(0, event['saved_entries']) for event in events[1:])
    if not added:
        return waiting('waiting', 'No new entries in recent pages')
    per_second = added / span
    for account in budget_ready:
        lane_events = [event for event in events if event['lane'] == account['lane_id']]
        if not lane_events:
            continue
        if len(lane_events) < 2:
            return waiting('warming_up', 'Measuring the connected accounts')
        daily = account['budget'].get('list')
        if daily:
            lane_added = sum(max(0, event['saved_entries']) for event in lane_events)
            per_page = lane_added / len(lane_events)
            requests_left = max(0, daily - account['today'].get('list', 0))
            if left * lane_added / (added + events[0]['saved_entries']) > requests_left * per_page:
                return waiting('waiting', 'Daily limits extend this queue into another day')
    # A planning range, not a statistical guarantee: allow substantial variation
    # around the observed pace. Never show a finish time for unknown/future work.
    low = max(1, math.ceil(left / (per_second * 1.25) / 60))
    high = max(low + 1, math.ceil(left / (per_second * 0.65) / 60))
    out.update(status='estimated', message='Based on recent saved pages',
               eta={'low_minutes': low, 'high_minutes': high,
                    'scope': 'known_lists' if out['unknown_lists'] else 'current_queue'})
    return out
