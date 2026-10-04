"""Pace control: how long an account waits between requests it is already allowed to make.

The numbers, bounds and presets live in extension/lib/pace-limits.js (read through pace_limits.py). This module stores
the choice (global, or one account), hands each extension its effective pace in the heartbeat and lease replies, and
explains the result in plain words. It never touches cooldowns, holds, stage pauses, budgets' enforcement, the request
permit gate or main-account protection: those run on their own and a pace value cannot reach them.

Storage (settings):
  pace          {"v": 1, "global": {preset, values, set_at}, "accounts": {lane_id: {ig_id, preset, values, set_at}}}
  pace_applied  {lane_id: {id, at, since}}   what each extension last said it is running
"""
from datetime import datetime, timedelta, timezone
import re

import accounts
import db
import pace_limits as limits

MIN_WINDOW_S = 11 * 60          # the extension's rolling window (fixed, not a pace value)
DELIVERY = 'Reaches each extension within about 30 seconds and applies to its next request.'
FIXED = (
    'Instagram limits always win: after a limit the account rests 10 minutes, doubling per hit up to 6 hours, '
    'and at least 2 hours after three hits in a day.',
    'A limit, security check or scraping warning on any account stops all collection until it is reviewed.',
    'Paused lists or bios, paused accounts, daily limits and the main account protection are not affected.',
    'Only one Instagram request runs at a time across the workspace.',
)
SAVE_EVERY_S = 60               # heartbeats repeat the same id; write it at most once a minute unless it changes
_ID = re.compile(r'^[a-z0-9.|-]{1,120}$')


def utcnow():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat(timespec='seconds').replace('+00:00', 'Z')


# ---------- storage ----------

def _config(conn):
    raw = db.get_setting(conn, 'pace', None)
    if not isinstance(raw, dict) or raw.get('v') != 1:
        return {'v': 1, 'global': None, 'accounts': {}}
    return {'v': 1, 'global': raw.get('global') if isinstance(raw.get('global'), dict) else None,
            'accounts': raw.get('accounts') if isinstance(raw.get('accounts'), dict) else {}}


def _entry_values(entry):
    """Stored values, clamped again against the bounds in force now (a tightened bound applies at once)."""
    if not isinstance(entry, dict) or not isinstance(entry.get('values'), dict):
        return None
    return limits.clamp(entry['values'])[0]


def label_for(values):
    """normal / slow / fast when the values equal a preset under today's bounds, else custom."""
    for name in ('normal', 'slow', 'fast'):
        if name in limits.LIMITS['presets'] and limits.preset(name)[0] == values:
            return name
    return 'custom'


def resolve(conn, row, cfg=None):
    """The pace one account runs: its own override (tied to its Instagram identity), else the global one, else the defaults."""
    cfg = cfg or _config(conn)
    own = cfg['accounts'].get(row['lane_id']) if row is not None else None
    ignored = False
    if own is not None and row is not None and own.get('ig_id') and own.get('ig_id') == row['ig_id']:
        values = _entry_values(own)
        if values is not None:
            return _out(values, 'account', own.get('set_at'))
    elif own is not None:
        ignored = True   # set for another Instagram identity: does not follow the Chrome profile
    values = _entry_values(cfg['global'])
    out = _out(values, 'global', (cfg['global'] or {}).get('set_at')) if values is not None else _out(limits.defaults(), 'global', None)
    out['override_ignored'] = ignored
    return out


def _out(values, scope, set_at):
    return {'values': values, 'id': limits.ident(values), 'preset': label_for(values), 'scope': scope, 'set_at': set_at}


def wire(conn, row):
    """What ext_state() sends an extension. The extension clamps it again with its own copy of the bounds."""
    p = resolve(conn, row)
    return {'v': 1, 'preset': p['preset'], 'scope': p['scope'], 'values': p['values'], 'id': p['id']}


def note_applied(conn, lane, report):
    """The extension says which pace it runs. Stored so the readout can show whether a change has arrived."""
    if not isinstance(report, dict) or not isinstance(report.get('id'), str) or not _ID.match(report['id']):
        return
    state = db.get_setting(conn, 'pace_applied', None)
    state = state if isinstance(state, dict) else {}
    old, now = state.get(lane), utcnow()
    since = report.get('since') if isinstance(report.get('since'), str) and len(report['since']) <= 40 else None
    if old and old.get('id') == report['id']:
        try:
            if (now - datetime.fromisoformat(old['at'].replace('Z', '+00:00'))).total_seconds() < SAVE_EVERY_S:
                return
        except (KeyError, ValueError, AttributeError):
            pass
    state[lane] = {'id': report['id'], 'at': iso(now), 'since': since}
    if len(state) > 64:   # lanes that no longer exist
        known = {r['lane_id'] for r in accounts.rows(conn)}
        state = {k: v for k, v in state.items() if k in known}
    db.set_setting(conn, 'pace_applied', state)


# ---------- the plain-words model ----------

def _mean(pair):
    return (pair[0] + pair[1]) / 2


def _n(x):
    x = round(float(x), 1)
    return str(int(x)) if x == int(x) else str(x)


def _rng(pair, unit=''):
    return f'{_n(pair[0])} to {_n(pair[1])}{unit}'


BINDS = {'gap': 'The wait between requests sets this pace.',
         'gap_and_breaks': 'The wait between requests and the breaks set this pace.',
         'window': 'The 11 minute cap sets this pace.',
         'daily_limit': 'The daily limit sets this pace.'}


def _local_midnight(now):
    local = now.astimezone()
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def model(values, budget=None, used=None, now=None):
    """Expected requests per hour from the numbers alone, and what limits them. Instagram's answer time and waiting for
    other work come on top, so real numbers are lower; the readout shows both. With `used` (requests made today) and
    `now` it also says when each daily limit would be reached, so a limit never arrives as a surprise."""
    gap, spacing = _mean(values['list_gap']), _mean(values['spacing'])
    cycle = max(gap, spacing)
    breaks = _mean(values['break_len']) / _mean(values['break_every'])
    gap_rate = 3600 / cycle
    list_rate = 3600 / (cycle + breaks)
    bio_rate = 3600 / max(_mean(values['profile_gap']), spacing)
    window_cap = values['window_max'] * 3600 / MIN_WINDOW_S
    both = list_rate + bio_rate
    all_rate = min(both, window_cap)
    binds = 'window' if both > window_cap else 'gap'
    if breaks / cycle > 0.12 and binds == 'gap':
        binds = 'gap_and_breaks'
    out = {'list_per_hour': round(list_rate), 'bio_per_hour': round(bio_rate), 'all_per_hour': round(all_rate),
           'window_per_hour': round(window_cap), 'gap_only_per_hour': round(gap_rate), 'binds': binds}
    budget, used = budget or {}, used or {}
    out['daily_limit'] = {}
    for kind, rate in (('list', list_rate), ('profile', bio_rate)):
        cap = budget.get(kind) or 0
        if cap > 0 and rate > 0:
            left = max(0, cap - (used.get(kind) or 0))
            hours = left / rate
            info = {'limit': cap, 'left': left, 'hours': round(hours, 1)}
            if now is not None:
                at = now + timedelta(hours=hours)
                info['reached_at'] = iso(at)
                info['before_midnight'] = at < _local_midnight(now)
            else:
                info['before_midnight'] = hours < 24
            out['daily_limit'][kind] = info
            if kind == 'list' and info['before_midnight']:   # the headline is the pages pace; a bio limit has its own sentence
                out['binds'] = 'daily_limit'
    out['text'] = BINDS[out['binds']]
    return out


def why(values, expected, observed=None):
    """Sentences for the UI, in the order a person reads them. The binding sentence is always among them."""
    lines = [
        f"Pages: one every {_rng(values['list_gap'], ' seconds')}, with a {_rng(values['break_len'], ' second')} break every "
        f"{_rng(values['break_every'], ' pages')}. About {expected['list_per_hour']} an hour.",
        f"Bios: one every {_rng(values['profile_gap'], ' seconds')}, when bio reading is on. About {expected['bio_per_hour']} an hour.",
        f"At most {values['window_max']} requests in any 11 minutes, about {expected['window_per_hour']} an hour.",
    ]
    for kind, word in (('list', 'pages'), ('profile', 'bios')):
        info = (expected.get('daily_limit') or {}).get(kind)
        if not info:
            lines.append(f'No daily cap on {word}.')
            continue
        if info['before_midnight']:
            when = ''
            if info.get('reached_at'):
                when = ', around ' + accounts.utc(info['reached_at']).astimezone().strftime('%H:%M')
            lines.append(f"Daily limit of {info['limit']:,} {word}: {info['left']:,} left today, reached in about {_n(info['hours'])} hours{when} at this pace.")
        else:
            lines.append(f"Daily limit of {info['limit']:,} {word}: {info['left']:,} left today, not reached before midnight at this pace.")
    lines.append(BINDS[expected['binds']])
    if observed and isinstance(observed.get('pages_hour'), (int, float)) and observed['pages_hour'] > 0 and expected['list_per_hour']:
        pct = round(100 * observed['pages_hour'] / expected['list_per_hour'])
        lines.append(f"Last hour: {round(observed['pages_hour'])} pages saved, {pct}% of this pace. "
                     'The rest is Instagram response time and waiting for other work.')
    return lines


# ---------- reads ----------

def _safety_now(conn, accts, now):
    out = []
    shared = db.get_setting(conn, 'cooldown', None)
    try:
        until = accounts.utc(shared) if shared else None
    except (TypeError, ValueError):
        until = None
    if shared and (until is None or until > now):
        out.append({'lane_id': None, 'text': 'All collection is waiting after an Instagram response' +
                    (f' until {until.astimezone():%H:%M}.' if until else '.')})
    if db.get_setting(conn, 'instagram_scraping_warning', None):
        out.append({'lane_id': None, 'text': 'Instagram showed a scraping warning. Collection stays stopped until it is reviewed.'})
    for a in accts:
        if a['hold']:
            out.append({'lane_id': a['lane_id'], 'text': f"{a['name']}: Instagram needs a login or security check."})
        elif a['cooldown_until']:
            out.append({'lane_id': a['lane_id'], 'text': f"{a['name']} is resting after an Instagram limit."})
        if a['collection_protected']:
            out.append({'lane_id': a['lane_id'], 'text': f"{a['name']} is your main account and is never used for collection."})
    return out


def _account_view(conn, a, row, cfg, applied, now):
    p = resolve(conn, row, cfg)
    exp = model(p['values'], a['budget'], a['today'], now)
    rec = (applied or {}).get(a['lane_id'])
    seen = None
    if rec:
        try:
            seen = accounts.utc(rec['at'])
        except (KeyError, ValueError, TypeError):
            seen = None
    return {'lane_id': a['lane_id'], 'name': a['name'], 'handle': a['handle'], 'ig_id': a['ig_id'], 'online': a['online'],
            'collection_protected': a['collection_protected'], 'version': a['version'],
            'scope': p['scope'], 'preset': p['preset'], 'values': p['values'], 'id': p['id'], 'set_at': p['set_at'],
            'override_ignored': p.get('override_ignored', False),
            'applied': ({'id': rec['id'], 'matches': rec['id'] == p['id'], 'seen_at': rec['at'], 'since': rec.get('since'),
                         'age_s': round((now - seen).total_seconds()) if seen else None} if rec else None),
            'budget': dict(a['budget'], used_today=a['today']),
            'expected': exp,
            'observed': {k: (a['rate'] or {}).get(k) for k in ('pages_hour', 'requests_hour', 'bios_hour', 'hits_24h')},
            'why': why(p['values'], exp, a['rate'])}


def _preset_list():
    out = []
    for name, spec in limits.LIMITS['presets'].items():
        values, notes = limits.preset(name)
        out.append({'id': name, 'label': spec['label'], 'values': values, 'clamped_fields': [n['field'] for n in notes]})
    return out


def _scope_view(conn, row, cfg):
    """The pace of one scope: an account's effective pace, or the global setting."""
    if row is not None:
        return resolve(conn, row, cfg)
    return _out(_entry_values(cfg['global']) or limits.defaults(), 'global', (cfg['global'] or {}).get('set_at'))


def snapshot(conn, lane=None):
    now = utcnow()
    cfg = _config(conn)
    applied = db.get_setting(conn, 'pace_applied', None)
    applied = applied if isinstance(applied, dict) else {}
    accts = accounts.listing(conn, now, include_lists=False)
    rows = {r['lane_id']: r for r in accounts.rows(conn)}
    glob = _scope_view(conn, None, cfg)
    return {'v': 1,
            'limits': {'status': limits.LIMITS['status'], 'note': limits.LIMITS['note'], 'fields': limits.FIELDS,
                       'budget_max': limits.budget_max(), 'budget_default': limits.budget_default()},
            'presets': _preset_list(), 'defaults': limits.defaults(),
            'global': {'preset': glob['preset'], 'values': glob['values'], 'id': glob['id'], 'set_at': glob['set_at'],
                       'expected': model(glob['values'], db.get_setting(conn, 'budget'))},
            'accounts': [_account_view(conn, a, rows[a['lane_id']], cfg, applied, now) for a in accts if lane in (None, a['lane_id'])],
            'safety': {'fixed': list(FIXED), 'now': _safety_now(conn, accts, now)},
            'delivery': DELIVERY, 'at': iso(now)}


def get(conn, q, b):
    return snapshot(conn, (q.get('lane') or [None])[0] if isinstance(q, dict) else None)


# ---------- writes ----------

def _int_budget(kind, value):
    cap = limits.budget_max()[kind]
    word = 'list pages' if kind == 'list' else 'bio reads'
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= cap:
        raise ValueError(f'Daily {word} must be a whole number from 0 to {cap:,}. 0 means no daily cap.')
    return value


def _budget_of(conn, row):
    return accounts.budget_of(conn, row) if row is not None else db.get_setting(conn, 'budget')


def post(conn, q, b):
    scope = b.get('scope', 'global')
    if scope not in ('global', 'account'):
        raise ValueError('Choose all accounts or one account.')
    row = None
    if scope == 'account':
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (b.get('lane_id'),)).fetchone()
        if row is None:
            raise ValueError('That account is not connected.')
        if accounts.collection_protected(conn, row):
            raise ValueError("The main account isn't used for collection, so it has no pace.")
        if not row['ig_id']:
            raise ValueError("This account hasn't signed in to Instagram yet. Open Instagram in its Chrome profile first.")
    verbs = [k for k in ('preset', 'values', 'reset') if k in b]
    if len(verbs) > 1:
        raise ValueError('Choose one of preset, values or reset.')
    if not verbs and 'budget' not in b:
        raise ValueError('Nothing to change. Send a preset, values, reset or budget.')
    if 'values' in b and not isinstance(b['values'], dict):
        raise ValueError('Values must be an object, like {"list_gap": [9, 14]}.')
    unknown = [k for k in (b.get('values') or {}) if k not in limits.ORDER]
    if unknown:
        raise ValueError(f"Unknown pace setting: {unknown[0]}. Use {', '.join(limits.ORDER)}.")
    if 'budget' in b and (not isinstance(b['budget'], dict) or not b['budget'] or set(b['budget']) - {'list', 'profile'}):
        raise ValueError('Budget needs list and/or profile, like {"list": 400}.')
    if 'reset' in b and b['reset'] is not True:
        raise ValueError('Reset must be true.')
    if 'preset' in b and not isinstance(b['preset'], str):
        raise ValueError('Choose slow, normal or fast.')
    budget_new = {k: _int_budget(k, v) for k, v in (b.get('budget') or {}).items()}
    dry = b.get('dry_run') is True
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        cfg = _config(conn)
        before = _scope_view(conn, row, cfg)
        budget_before = _budget_of(conn, row)
        new = {'v': 1, 'global': cfg['global'], 'accounts': dict(cfg['accounts'])}
        notes, stamp = [], iso(utcnow())
        if verbs:
            if 'reset' in b:
                values = None
            elif 'preset' in b:
                values, notes = limits.preset(b['preset'])
            else:
                values, notes = limits.clamp(b['values'], base=before['values'], strict=True)
            if values is None:
                if row is not None:
                    new['accounts'].pop(row['lane_id'], None)
                else:
                    new['global'] = None
            else:
                entry = {'preset': b['preset'] if 'preset' in b else label_for(values), 'values': values, 'set_at': stamp}
                if row is not None:
                    new['accounts'][row['lane_id']] = dict(entry, ig_id=row['ig_id'])
                else:
                    new['global'] = entry
        after = _scope_view(conn, row, new)
        if dry:
            budget_after = dict(budget_before, **budget_new)
        else:
            if verbs:
                db.set_setting(conn, 'pace', new)
            if budget_new:
                if row is not None:
                    accounts.edit(conn, row['lane_id'], {'budget': budget_new})
                else:
                    db.set_setting(conn, 'budget', dict(budget_before, **budget_new))
            budget_after = _budget_of(conn, row)
        conn.rollback() if dry else conn.commit()
    except Exception:
        conn.rollback()
        raise
    now = utcnow()
    used = accounts.out(conn, row, now, include_lists=False)['today'] if row is not None else None
    targets = [r for r in accounts.rows(conn) if not accounts.collection_protected(conn, r) and
               (r['lane_id'] == row['lane_id'] if row is not None else r['lane_id'] not in new['accounts'])]
    changed = [f for f in limits.ORDER if after['values'][f] != before['values'][f]]
    if any(budget_after.get(k) != budget_before.get(k) for k in budget_new):
        changed.append('budget')
    return {'saved': not dry, 'scope': scope, 'lane_id': row['lane_id'] if row is not None else None,
            'before': {k: before[k] for k in ('preset', 'values', 'id')}, 'after': {k: after[k] for k in ('preset', 'values', 'id')},
            'changed': changed, 'clamped': notes, 'budget': {'before': budget_before, 'after': budget_after},
            'expected': {'before': model(before['values'], budget_before, used, now), 'after': model(after['values'], budget_after, used, now)},
            'affects': [{'lane_id': r['lane_id'], 'name': accounts.name_of(r),
                         'online': bool(r['last_seen']) and now - accounts.utc(r['last_seen']) < accounts.ONLINE_FOR} for r in targets],
            'delivery': DELIVERY}


def routes(_server):
    return [('GET', r'/api/pace', get), ('POST', r'/api/pace', post)]
