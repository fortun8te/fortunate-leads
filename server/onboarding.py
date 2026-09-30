"""First-run checklist and the one-handle start flow, in plain words.

Pure functions over data the server already has (accounts listing, LLM status, settings, backup folder), so the
same checks can be unit tested without a browser. Nothing here touches Instagram or changes pacing."""
import os
import subprocess
import sys
import time
from pathlib import Path

BACKUP_LABEL = 'com.fortunate.leads.backup'
BACKUP_STALE_HOURS = 36
OPTIONAL = ('ai', 'backups')          # never block "ready"; can be skipped
FIX_ROUTES = {'extension': '#/accounts', 'instagram': '#/accounts', 'ai': '#/settings', 'backups': None}


def newest_backup(db_path):
    """(path, age_hours) of the newest leads-*.sqlite next to the database, or None."""
    folder = Path(db_path).resolve().parent / 'backups'
    try:
        files = [e for e in os.scandir(folder) if e.name.startswith('leads-') and e.name.endswith('.sqlite') and e.is_file()]
    except OSError:
        return None
    if not files:
        return None
    best = max(files, key=lambda e: e.stat().st_mtime)
    return best.path, (time.time() - best.stat().st_mtime) / 3600


def backup_agent_loaded():
    """True/False on macOS, None when it cannot be known (other platform, no launchctl)."""
    if sys.platform != 'darwin':
        return None
    try:
        done = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{BACKUP_LABEL}'],
                              capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode == 0


def _step(id_, title, status, detail, action=None, items=None):
    return {'id': id_, 'title': title, 'status': status, 'detail': detail, 'action': action, 'items': items or [],
            'optional': id_ in OPTIONAL}


def _profile_items(accts):
    out = []
    for a in accts:
        if a.get('hold') == 'login':
            state, text = 'bad', 'Logged out of Instagram'
        elif a.get('hold') == 'challenge':
            state, text = 'bad', 'Instagram security check waiting'
        elif not a.get('online'):
            state, text = 'wait', 'Not connected right now'
        elif not a.get('ig_id'):
            state, text = 'wait', 'Connected, not logged in to Instagram'
        else:
            state, text = 'ok', 'Connected'
        out.append({'lane_id': a['lane_id'], 'name': a.get('name') or a['lane_id'], 'state': state, 'text': text})
    return out


def checks(accts, llm_status, backup, agent, skipped=(), server_info=None):
    """The ordered checklist. status: ok | todo | warn. Optional steps skipped by the user count as ok."""
    steps = [_step('server', 'Server', 'ok', 'Running' + (f" · v{server_info['extension_version']}" if server_info and server_info.get('extension_version') else ''))]

    items = _profile_items(accts)
    online = [a for a in accts if a.get('online')]
    if not accts:
        steps.append(_step('extension', 'Chrome extension', 'todo',
                           'No Chrome profile has connected yet. Load the extension once per profile.',
                           {'label': 'Connect a profile', 'route': FIX_ROUTES['extension'], 'kind': 'wizard'}))
    elif not online:
        steps.append(_step('extension', 'Chrome extension', 'todo',
                           'No Chrome profile is connected right now. Open Chrome with the extension loaded.',
                           {'label': 'Show profiles', 'route': FIX_ROUTES['extension']}, items))
    else:
        n = len(online)
        steps.append(_step('extension', 'Chrome extension', 'ok',
                           f"{n} Chrome profile{'s' if n != 1 else ''} connected", None, items))

    logged = [a for a in online if a.get('ig_id') and not a.get('hold')]
    holds = [a for a in accts if a.get('hold')]
    if logged:
        names = ', '.join(a['name'] for a in logged[:3])
        steps.append(_step('instagram', 'Instagram login', 'ok', f'Logged in: {names}'))
    elif holds:
        why = 'logged out' if holds[0].get('hold') == 'login' else 'waiting on a security check'
        steps.append(_step('instagram', 'Instagram login', 'todo',
                           f"{holds[0]['name']} is {why}. Open that Chrome profile and fix it there.",
                           {'label': 'See account', 'route': FIX_ROUTES['instagram']}))
    elif online:
        steps.append(_step('instagram', 'Instagram login', 'todo',
                           'Log in to instagram.com in a connected Chrome profile and keep the tab open.',
                           {'label': 'Open Instagram', 'href': 'https://www.instagram.com/'}))
    else:
        steps.append(_step('instagram', 'Instagram login', 'todo', 'Connect a Chrome profile first, then log in to Instagram in it.'))

    keys = [p for p in (llm_status or {}).get('providers') or [] if p.get('key') and not p.get('disabled')]
    if keys:
        steps.append(_step('ai', 'AI checking', 'ok', f"{len(keys)} OpenRouter key{'s' if len(keys) != 1 else ''} added"))
    else:
        steps.append(_step('ai', 'AI checking', 'ok' if 'ai' in skipped else 'todo',
                           'Optional. Leads are ranked by rules without a key. Add a free key for smarter checks.',
                           {'label': 'Add a key', 'route': FIX_ROUTES['ai']}))

    if backup and backup[1] <= BACKUP_STALE_HOURS:
        steps.append(_step('backups', 'Backups', 'ok', f'Last backup {int(backup[1])} h ago' if backup[1] >= 1 else 'Last backup under an hour ago'))
    elif backup:
        steps.append(_step('backups', 'Backups', 'ok' if 'backups' in skipped else 'warn',
                           f'Newest backup is {int(backup[1] // 24)} days old.' if backup[1] >= 48 else f'Newest backup is {int(backup[1])} h old.',
                           None if agent else {'label': 'Run ops/install.sh', 'kind': 'copy', 'copy': 'ops/install.sh'}))
    else:
        steps.append(_step('backups', 'Backups', 'ok' if 'backups' in skipped else 'warn',
                           'Daily backup is on; the first one runs at 03:30.' if agent else 'No backup yet. ops/install.sh turns on a daily one.',
                           None if agent else {'label': 'Copy install command', 'kind': 'copy', 'copy': 'ops/install.sh'}))
    return steps


def summarize(steps):
    blocking = [s for s in steps if not s['optional'] and s['status'] != 'ok']
    open_ = [s for s in steps if s['status'] != 'ok']
    return {'ready': not blocking, 'healthy': not open_, 'blocking': len(blocking), 'open': len(open_)}


def flow(conn, hold, controls=None):
    """Recent list activity and workspace totals, with actual collector state."""
    lists = [dict(r) for r in conn.execute(
        "SELECT l.seed, l.direction, l.received, l.total, l.state, l.error FROM lists l "
        "WHERE l.seed IN (SELECT handle FROM seeds) ORDER BY l.updated_at DESC LIMIT 12")]
    queued = conn.execute("SELECT seed, direction FROM jobs WHERE kind='list' AND state IN ('queued','leased') "
                          "ORDER BY id DESC LIMIT 12").fetchall()
    people = conn.execute('SELECT count(*) FROM people').fetchone()[0]
    bios = conn.execute('SELECT count(*) FROM people WHERE bio_at IS NOT NULL').fetchone()[0]
    ranked = conn.execute('SELECT count(*) FROM verdicts').fetchone()[0]
    jobs = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','leased')").fetchone()[0]
    if controls is None:
        import control
        controls = control.snapshot(conn)
    stages = {stage['id']: stage for stage in controls['stages']}
    collection = [stages[name] for name in ('lists', 'bios')]
    stopped = all(stage['paused'] for stage in collection)
    in_flight = controls.get('active', False)
    attention = controls.get('instagram_request_attention') or {}
    running = next((stage for stage in collection if stage['state'] == 'running'), None)
    waiting = next((stage for stage in collection if stage['state'] == 'waiting'), None)
    if not lists and not queued and not jobs:
        headline, state = 'Paste an Instagram account to begin.', 'idle'
    elif stopped and attention.get('message'):
        headline, state = attention['message'], 'wait'
    elif stopped and in_flight:
        headline, state = 'Stopping collection. Waiting for the current request to finish.', 'stopping'
    elif hold:
        headline, state = 'Instagram asked us to wait. Collection resumes on its own.', 'wait'
    elif stopped:
        headline, state = 'Collection is stopped. Continue where you left off.', 'paused'
    elif running:
        headline, state = running['now'], 'running'
    elif waiting:
        headline, state = waiting['now'], 'wait'
    elif queued or jobs:
        headline, state = 'Work is saved in the queue. Waiting for an account to pick it up.', 'wait'
    else:
        headline, state = 'Collection is ready. Your saved people are available in Leads.', 'done'
    return {'state': state, 'headline': headline, 'lists': lists, 'people': people, 'bios': bios, 'ranked': ranked,
            'jobs_left': jobs, 'stop_acknowledged': controls.get('stop_acknowledged', False), 'totals_scope': 'workspace'}


def instagram_setup(account, owner):
    """Observed main-account connection, separate from collection readiness."""
    account = account or {}
    hold = account.get('hold')
    state = ('missing' if not account else 'offline' if not account.get('online') else
             'security_check' if hold == 'challenge' else 'signed_out' if hold == 'login' or not account.get('ig_id') else 'connected')
    return {'handle': account.get('handle') or owner, 'lane_id': account.get('lane_id'),
            'ig_id': account.get('ig_id'), 'state': state, 'connected': state == 'connected',
            'paused': bool(account.get('paused')), 'home_url': 'https://www.instagram.com/',
            'inbox_url': 'https://www.instagram.com/direct/inbox/',
            'messages': {'mode': 'instagram', 'imported': False}}
