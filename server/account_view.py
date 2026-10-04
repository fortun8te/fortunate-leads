"""One calm card per Instagram account, in plain words.

Pure view-model code over data the server already has; it never changes anything and never talks to Instagram.
The card state machine (first match wins) is documented in docs/ACCOUNTS_UX.md:

    disconnected > connecting > personal > review > security_check > needs_login > offline > trouble
    > held_back > paused > waiting (shared pause) > ready (collection stopped) > limit > waiting (this account)
    > collecting > ready

Times are sent as absolute ISO strings (`until`) so the page can count down without asking again.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import accounts
import browser_startup
import control
import db

EVENTS_PER_ACCOUNT = 5
RECENT_SETUP = timedelta(minutes=10)

TONE = {'collecting': 'ok', 'waiting': 'wait', 'limit': 'wait', 'trouble': 'wait', 'connecting': 'wait',
        'ready': 'quiet', 'paused': 'quiet', 'offline': 'quiet', 'held_back': 'quiet', 'personal': 'quiet',
        'disconnected': 'quiet', 'needs_login': 'attention', 'security_check': 'attention', 'review': 'attention'}
READS = {'lists': 'Lists only', 'bios': 'Bios only', 'both': 'Lists and bios'}


def _clock(value):
    return value.astimezone().strftime('%H:%M')


def _midnight(now):
    local = now.astimezone()
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _later(value, now):
    try:
        return bool(value) and accounts.utc(value) > now
    except (ValueError, AttributeError, TypeError):
        return False


def event_text(row):
    """One recent collector event as a short past-tense sentence."""
    outcome, reason = row['outcome'], row['reason']
    seed = row['seed']
    if outcome == 'page':
        saved = row['saved_entries'] if row['saved_entries'] is not None else row['returned_count']
        where = f"@{seed}'s {row['direction']} list" if seed else 'a list'
        return f"Saved {saved:,} {'person' if saved == 1 else 'people'} from {where}" if saved else f'Read a page of {where}, nothing new'
    if outcome == 'profile':
        return 'Read a bio' + (f" for @{row['handle']}" if row['handle'] else '')
    if outcome == 'rate_limit':
        return 'Instagram asked to slow down'
    if outcome == 'private':
        return 'A list was private'
    if outcome == 'not_found':
        return "A profile wasn't found"
    if reason == 'list_html_home_redirect':
        return 'Instagram sent its home page instead of a list'
    if reason == 'no_profile_data':
        return 'Instagram returned no profile data'
    return 'Instagram gave an unexpected answer'


def recent_events(conn, lane, limit=EVENTS_PER_ACCOUNT):
    rows = conn.execute(
        'SELECT e.at,e.kind,e.direction,e.outcome,e.reason,e.returned_count,e.saved_entries,j.seed,j.handle '
        'FROM collector_events e LEFT JOIN jobs j ON j.id=e.job_id WHERE e.lane=? ORDER BY e.at DESC LIMIT ?', (lane, limit))
    return [{'at': r['at'], 'text': event_text(r)} for r in rows]


def avatars(conn, handles):
    out = {}
    handles = [h for h in {h.lower() for h in handles if h}]
    for i in range(0, len(handles), 400):
        part = handles[i:i + 400]
        for r in conn.execute(f"SELECT handle,id,pic_file,name FROM people WHERE handle IN ({','.join('?' * len(part))})", part):
            out[r['handle'].lower()] = {'pic': f"/img/{r['id']}" if r['pic_file'] else None, 'name': r['name']}
    return out


def workspace(conn, snapshot, now):
    """Facts that are about the whole workspace, not one account."""
    stages = [s for s in snapshot['stages'] if s['id'] in ('lists', 'bios')]
    stopped = len(stages) == 2 and all(s['paused'] for s in stages)
    shared = next((s['wait'] for s in stages if s.get('wait') and s['wait'].get('scope') == 'workspace'), None)
    warning = db.get_setting(conn, 'instagram_scraping_warning') or None
    isolation = db.get_setting(conn, 'instagram_collection_isolation') or None
    startup = browser_startup.snapshot(conn)
    return {'stopped': stopped, 'stopping': bool(snapshot.get('stopping')), 'shared_wait': shared,
            'warning': ({'lane': warning.get('lane'), 'message': warning.get('message'), 'review_ready': bool(warning.get('review_ready')),
                         'more': len(warning.get('pending') or {})} if warning else None),
            'isolation': list(isolation['accounts']) if isolation else None,
            'scope': snapshot.get('collection_scope'),
            'startup': ({'state': startup['state'], 'message': startup.get('message'),
                         'lanes': [a['lane_id'] for a in startup.get('accounts') or []]} if startup else None)}


def card(conn, a, row, c, ws, now, *, saved_profile, host_ok, picture):
    """Everything one account card shows. `a` is accounts.out, `c` is control.account_out for the same lane."""
    role = row['role'] if row['role'] in accounts.ROLES else 'both'
    reads_lists, reads_bios = role in ('lists', 'both'), role in ('bios', 'both')
    warning = accounts.warning_for(conn, row)
    connecting = bool(ws['startup'] and ws['startup']['state'] in browser_startup._PENDING and a['lane_id'] in ws['startup']['lanes'])
    midnight = _midnight(now)
    spent = {k: a['today'].get(k, 0) for k in ('list', 'profile')}
    limit = {k: (a['budget'].get(k) or 0) for k in ('list', 'profile')}
    out_of = [k for k, wanted in (('list', reads_lists), ('profile', reads_bios)) if wanted and limit[k] and spent[k] >= limit[k]]
    exhausted = bool(out_of) and len(out_of) == sum([reads_lists, reads_bios])
    cooling = [v for v in ((a['cool'] or {}).get('list') if reads_lists else None,
                           (a['cool'] or {}).get('profile') if reads_bios else None, a['cooldown_until']) if _later(v, now)]
    route_wait = a['list_endpoint_until'] if _later(a['list_endpoint_until'], now) else None
    wait = max(cooling, key=accounts.utc) if cooling else None
    state, headline, detail, until, action = 'ready', 'Ready', 'Waiting for work.', None, None

    if a['disconnected']:
        state, headline, detail = 'disconnected', 'Disconnected', 'Not used for collecting. Its Chrome profile can stay open.'
        action = {'id': 'reconnect', 'label': 'Reconnect'}
    elif connecting:
        state, headline, detail = 'connecting', 'Connecting', 'Opening its Chrome profile. This can take up to 90 seconds.'
    elif a['collection_protected']:
        state, headline, detail = 'personal', 'Personal account', 'Never used for collecting.'
        action = {'id': 'inbox', 'label': 'Open Instagram'}
    elif warning:
        ready = bool(warning.get('review_ready'))
        state, headline = 'review', 'Instagram asked for a review'
        detail = ('Check this account in its Chrome profile, then confirm here. It stays stopped until you do.' if ready
                  else 'Close the Instagram warning page in this Chrome profile, then confirm here.')
        action = {'id': 'acknowledge', 'label': "I've reviewed it", 'enabled': ready,
                  'hint': None if ready else 'Waiting for the extension to see the warning page closed.'}
    elif a['hold'] == 'challenge':
        state, headline, detail = 'security_check', 'Needs a security check', 'Complete Instagram’s check in this Chrome profile.'
    elif a['hold'] == 'login' or (a['online'] and not a['ig_id']):
        state, headline, detail = 'needs_login', 'Needs login', 'Sign in to Instagram in this Chrome profile.'
    elif not a['online']:
        state, headline = 'offline', 'Offline'
        detail = "Its Chrome profile isn't open." + (' It is paused too.' if a['paused'] else '')
        if saved_profile and host_ok:
            action = {'id': 'connect', 'label': 'Open Chrome profile'}
    elif a.get('state') == 'network_wait' or a['status'] == 'connection_error':
        state, headline, detail = 'trouble', 'Connection trouble', 'Retrying safely. Progress is saved.'
        action = {'id': 'pause', 'label': 'Pause'}
    elif ws['isolation'] is not None and a['lane_id'] not in ws['isolation']:
        state, headline = 'held_back', 'On hold'
        detail = 'Held back after an Instagram warning. Only the accounts you chose can collect.'
    elif a['paused']:
        state, headline, detail = 'paused', 'Paused', 'Resume when you want it to collect again.'
        action = {'id': 'resume', 'label': 'Resume'}
    elif ws['shared_wait']:
        until = ws['shared_wait'].get('until')
        state, headline, detail = 'waiting', 'Waiting {t}', 'Instagram asked every account to pause. It resumes by itself.'
        action = {'id': 'pause', 'label': 'Pause'}
    elif ws['stopped']:
        state, headline, detail = 'ready', 'Ready', 'Collection is stopped. Start it from the top of the page.'
        action = {'id': 'pause', 'label': 'Pause'}
    elif exhausted:
        until = _iso(midnight)
        state, headline = 'limit', 'Daily limit reached'
        detail = f'Resumes at {_clock(midnight)}, when the day resets. Progress is saved.'
        action = {'id': 'pause', 'label': 'Pause'}
    elif wait or route_wait:
        until = wait or route_wait
        state, headline = 'waiting', 'Waiting {t}'
        detail = ('Instagram asked this account to slow down.' if wait else 'Follower lists are resting. Following lists and bios continue.')
        action = {'id': 'pause', 'label': 'Pause'}
    elif c.get('state') == 'running' or a['job']:
        job = a['job'] or {}
        what = (f"@{job['seed']}'s {job['direction']} list" if job.get('kind') == 'list' else
                f"the bio of @{job['handle']}" if job.get('handle') else 'Instagram')
        state, headline, detail = 'collecting', 'Collecting', f'Reading {what}.'
        action = {'id': 'pause', 'label': 'Pause'}
    else:
        action = {'id': 'pause', 'label': 'Pause'}
        if c.get('wait') and c['wait'].get('seconds') is not None and c['wait']['seconds'] > 60:
            state, headline, detail = 'waiting', 'Waiting {t}', 'Pacing itself between requests.'
            until = _iso(now + timedelta(seconds=c['wait']['seconds']))

    handle = a['handle']
    person = picture.get((handle or '').lower(), {})
    budgets = {
        'list': {'limit': limit['list'] or None, 'default': (db.get_setting(conn, 'budget') or {}).get('list') or None,
                 'custom': bool(a['budget_custom'] and (accounts.jload(row['budget'], {}) or {}).get('list') is not None)},
        'profile': {'limit': limit['profile'] or None, 'default': (db.get_setting(conn, 'budget') or {}).get('profile') or None,
                    'custom': bool(a['budget_custom'] and (accounts.jload(row['budget'], {}) or {}).get('profile') is not None)},
    }
    return {
        'lane_id': a['lane_id'], 'ig_id': a['ig_id'], 'handle': handle, 'label': a['label'], 'name': a['name'],
        'avatar': {'pic': person.get('pic'), 'initial': ((handle or a['label'] or '?')[:1]).upper()},
        'kind': 'main' if a['is_main'] or a['collection_protected'] else 'collector',
        'state': state, 'tone': TONE[state], 'headline': headline, 'detail': detail, 'until': until,
        'action': action, 'online': a['online'], 'paused': a['paused'], 'disconnected': a['disconnected'],
        'first_seen': a['first_seen'], 'last_seen': a['last_seen'], 'version': a['version'],
        'allowance': None if a['collection_protected'] else {
            'reads': role, 'list': {'used': spent['list'], 'limit': limit['list'] or None} if reads_lists else None,
            'profile': {'used': spent['profile'], 'limit': limit['profile'] or None} if reads_bios else None,
            'resets_at': _iso(midnight)},
        'details': {'reads': role, 'reads_label': READS[role], 'budgets': budgets, 'last_slowed': a['last_limit'],
                    'hour': a['hour'], 'events': recent_events(conn, a['lane_id']), 'can_forget': a['disconnected'],
                    'saved_profile': saved_profile, 'pace': None},
    }


def _iso(value):
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else value


def build(conn, config_root, host_ok, now=None, setup=None):
    """The payload behind GET /api/accounts/live."""
    now = now or datetime.now(timezone.utc)
    rows = {r['lane_id']: r for r in accounts.rows(conn)}
    listing = [accounts.out(conn, r, now, include_lists=False) for r in rows.values()]
    snapshot = control.snapshot(conn)
    ws = workspace(conn, snapshot, now)
    by_lane = {c['lane_id']: c for c in snapshot['accounts']}
    try:
        profiles = {p['lane_id'] for p in browser_startup.read_config(config_root)}
    except (OSError, ValueError):
        profiles = set()
    pics = avatars(conn, [a['handle'] for a in listing])
    cards = [card(conn, a, rows[a['lane_id']], by_lane.get(a['lane_id'], {}), ws, now,
                  saved_profile=a['lane_id'] in profiles, host_ok=host_ok, picture=pics) for a in listing]
    # Collectors first in the order they were added, then the personal account, then disconnected ones.
    cards.sort(key=lambda x: (x['disconnected'], x['kind'] == 'main', x['first_seen'] or '', x['lane_id']))
    shown = [x for x in cards if not x['disconnected']]
    summary = {'total': len(shown), 'collecting': sum(x['state'] == 'collecting' for x in shown),
               'attention': sum(x['tone'] == 'attention' for x in shown),
               'online': sum(bool(x['online']) for x in shown if x['kind'] == 'collector'),
               'collectors': sum(x['kind'] == 'collector' for x in shown)}
    payload = {'cards': cards, 'summary': summary, 'workspace': ws,
               'main': next(({'handle': x['handle']} for x in cards if x['kind'] == 'main'), None)}
    if setup:
        payload['setup'] = setup
    payload['revision'] = hashlib.sha1(json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()[:16]
    return payload
