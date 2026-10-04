"""Seeds: the Instagram profiles whose follower and following lists are collected.

One seed has up to two lists (followers, following). This module owns everything the Accounts page does with
them: reading pasted text, checking it against what is already tracked, adding it atomically and idempotently,
pausing, removing and undoing, and describing each seed in plain words. The state machine and the API are
documented in docs/ACCOUNTS_UX.md. Nothing here talks to Instagram or changes request pacing: it only decides
which lists exist, which are held back and which come first. Callers own the connection; every public write
function opens one write transaction and commits it, so a batch either lands whole or not at all.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import db
from storage.checkpoints import DIRECTIONS, wanted_directions

MAX_BATCH = 500
BATCH_KEEP = 40                     # remembered request ids (idempotency + undo)
BATCH_TTL = timedelta(days=1)
BATCH_KEY = 'seed_batches'
HOLD_OPERATOR, HOLD_REMOVED, HOLD_OFF = 'operator', 'removed', 'direction_off'

REASONS = {
    'empty': 'Nothing to add.',
    'post_link': 'This is a post or reel link, not a profile.',
    'not_instagram': 'This is not an Instagram link.',
    'too_long': 'Instagram names are 30 characters or fewer.',
    'not_valid': 'This is not an Instagram name.',
}
_SPLIT = re.compile(r'[\s,;]+')
_WRAP = '"\'()[]{}<>`'
_POST_PATH = re.compile(r'instagram\.com/(p|reel|reels|tv|stories|explore|direct|accounts)(/|$|\?|#)', re.I)


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec='microseconds')


def _chunks(values, size=400):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


# ---------- reading pasted text ----------

def split_tokens(text):
    """Pasted text (or a list of strings) to raw tokens. Commas, semicolons, spaces and newlines all separate."""
    pieces = text if isinstance(text, list) else [text]
    out = []
    for piece in pieces:
        if not isinstance(piece, str):
            out.append(piece)
            continue
        out.extend(token for token in (t.strip(_WRAP) for t in _SPLIT.split(piece)) if token)
    return out


def read_token(raw):
    """(handle, reason). Exactly one is set. Uses the same rules the collector queue uses."""
    if not isinstance(raw, str) or not raw.strip():
        return None, 'empty'
    handle = db.norm_handle(raw)
    if handle and '~' not in handle:
        return handle, None
    text = raw.strip()
    if _POST_PATH.search(text):
        return None, 'post_link'
    if re.match(r'[a-z][a-z0-9+.-]*://', text, re.I) and not re.search(r'instagram\.com|instagr\.am', text, re.I):
        return None, 'not_instagram'
    core = text.lstrip('@')
    if re.fullmatch(r'[A-Za-z0-9._]+', core) and len(core) > 30:
        return None, 'too_long'
    return None, 'not_valid'


def effective_directions(raw, discovered, existing):
    """Lists this seed asks for. Auto-discovered seeds only ever asked for the lists they already have."""
    if raw is None and discovered:
        return tuple(d for d in DIRECTIONS if d in existing) or ('following',)
    return wanted_directions(raw)


# ---------- what is already known ----------

def _lookup(conn, handles):
    """Saved facts for these lowercase handles: seeds, lists, aliases, people and our own accounts."""
    seeds, lists, aliases, people, found = {}, {}, {}, {}, set()
    handles = sorted(set(handles))
    for part in _chunks(handles):
        marks = ','.join('?' * len(part))
        for r in conn.execute(f'SELECT handle,ig_id,is_me,added_at,directions,removed_at FROM seeds WHERE handle IN ({marks})', part):
            seeds[r['handle'].lower()] = r
        for r in conn.execute(f'SELECT seed,direction,state,hold,received,cursor,error FROM lists WHERE seed IN ({marks})', part):
            lists[(r['seed'].lower(), r['direction'])] = r
        for r in conn.execute(f'SELECT old_handle,new_handle,renamed_at FROM seed_aliases WHERE old_handle IN ({marks})', part):
            aliases[r['old_handle'].lower()] = r
        for r in conn.execute(f'SELECT id,handle,ig_id,name,pic_file,is_private,followers,following FROM people WHERE handle IN ({marks})', part):
            people[r['handle'].lower()] = r
        found.update(r[0].lower() for r in conn.execute(
            f"SELECT handle FROM collection_discovery WHERE state='queued' AND handle IN ({marks})", part))
    ours = {r[0].lower() for r in conn.execute('SELECT handle FROM accounts WHERE handle IS NOT NULL')} | \
           {r[0].lower() for r in conn.execute('SELECT handle FROM seeds WHERE is_me=1')}
    return seeds, lists, aliases, people, ours, found


def _resolve_alias(conn, handle, aliases, seeds, people):
    """The current handle for a name the seed used to have, or None. A name someone else holds now is not an alias."""
    if handle in seeds or handle in people or handle not in aliases:
        return None
    current = aliases[handle]['new_handle']
    for _ in range(5):
        step = conn.execute('SELECT new_handle FROM seed_aliases WHERE old_handle=?', (current,)).fetchone()
        if not step:
            break
        current = step[0]
    row = conn.execute('SELECT 1 FROM seeds WHERE handle=?', (current,)).fetchone()
    return current.lower() if row else None


def classify(conn, raw_tokens, directions):
    """One verdict per pasted token, in order. Never writes.

    state: new | extend | exists | restore | invalid | duplicate
    add:   lists this paste would add (extend/restore/new)
    """
    dirs = [d for d in DIRECTIONS if d in directions]
    parsed = [(raw, *read_token(raw)) for raw in raw_tokens]
    seeds, lists, aliases, people, ours, found = _lookup(conn, [h for _, h, _ in parsed if h])
    # A renamed seed's current handle needs its own lookup before it can be classified.
    renamed = {}
    for _, handle, _ in parsed:
        if handle:
            current = _resolve_alias(conn, handle, aliases, seeds, people)
            if current:
                renamed[handle] = current
    if renamed:
        s2, l2, _, p2, _, f2 = _lookup(conn, renamed.values())
        seeds.update(s2), lists.update(l2), people.update(p2), found.update(f2)
    out, seen = [], set()
    for raw, handle, reason in parsed:
        item = {'input': raw if isinstance(raw, str) else None, 'handle': handle, 'state': 'invalid', 'reason': reason,
                'message': REASONS.get(reason), 'add': [], 'warn': [], 'renamed_from': None, 'person': None,
                'identity': 'pending', 'have': {}}
        if handle is None:
            out.append(item)
            continue
        if handle in renamed:
            item['renamed_from'] = handle
            handle = renamed[handle]
            item['handle'] = handle
        if handle in seen:
            item.update(state='duplicate', reason='duplicate', message='Already in this paste.')
            out.append(item)
            continue
        seen.add(handle)
        person = people.get(handle)
        if person:
            item['person'] = {'name': person['name'], 'followers': person['followers'], 'following': person['following'],
                              'pic': f"/img/{person['id']}" if person['pic_file'] else None}
            item['identity'] = 'known' if person['ig_id'] else 'pending'
            if person['is_private']:
                item['warn'].append('private')
        if handle in ours:
            item['warn'].append('yours')
        seed = seeds.get(handle)
        if seed is None:
            item.update(state='new', reason=None, message=None, add=list(dirs))
        else:
            if seed['ig_id']:
                item['identity'] = 'known'
            effective = effective_directions(seed['directions'], handle in found, {d for d in DIRECTIONS if (handle, d) in lists})
            item['have'] = {d: (lists[(handle, d)]['state'] if (handle, d) in lists else None) for d in effective}
            missing = [d for d in dirs if d not in effective]
            if seed['removed_at']:
                item.update(state='restore', reason=None, message='Removed earlier. Adding it brings it back.', add=missing)
            elif missing:
                item.update(state='extend', reason=None, message=None, add=missing)
            else:
                done = all(lists.get((handle, d)) is not None and lists[(handle, d)]['state'] == 'done' for d in effective)
                held = any(lists.get((handle, d)) is not None and lists[(handle, d)]['hold'] for d in effective)
                item.update(state='exists', reason='collected' if done else 'paused' if held else 'tracked',
                            message='Already collected.' if done else 'Already added, paused.' if held else 'Already added.')
        out.append(item)
    return out


def summarize(items):
    counts = {'ready': 0, 'already': 0, 'duplicate': 0, 'invalid': 0, 'renamed': 0, 'private': 0, 'restore': 0}
    for it in items:
        if it['state'] in ('new', 'extend', 'restore'):
            counts['ready'] += 1
        elif it['state'] == 'exists':
            counts['already'] += 1
        elif it['state'] in ('duplicate', 'invalid'):
            counts[it['state']] += 1
        counts['renamed'] += bool(it['renamed_from'])
        counts['private'] += 'private' in it['warn']
        counts['restore'] += it['state'] == 'restore'
    return counts


def check(conn, text, directions):
    items = classify(conn, split_tokens(text), directions)
    return {'items': items, 'summary': summarize(items)}


# ---------- list-level moves ----------

def _live_lease(conn, seed, direction, now):
    return conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND lower(seed)=lower(?) AND direction=? AND state='leased' "
                        'AND leased_until>? LIMIT 1', (seed, direction, _iso(now))).fetchone() is not None


def pause_list(conn, seed, direction, reason=HOLD_OPERATOR, now=None):
    """Hold one list. A request already on its way finishes first; the pause lands between pages."""
    now = now or _now()
    row = conn.execute('SELECT state,hold FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
    if row is None or row['state'] == 'done':
        return 'noop'
    if row['hold'] and not (row['hold'] == HOLD_REMOVED and reason == HOLD_OPERATOR):
        return 'noop'
    conn.execute('UPDATE lists SET hold=? WHERE seed=? AND direction=?', (reason, seed, direction))
    leased = _live_lease(conn, seed, direction, now)
    conn.execute("UPDATE jobs SET state='cancelled',lane=NULL,lease_token=NULL,leased_until=NULL WHERE kind='list' "
                 "AND lower(seed)=lower(?) AND direction=? AND (state='queued' OR (state='leased' AND leased_until<=?))",
                 (seed, direction, _iso(now)))
    if row['state'] in ('queued', 'running') and not leased:
        conn.execute("UPDATE lists SET state='paused',lane=NULL,updated_at=? WHERE seed=? AND direction=?",
                     (db.now(), seed, direction))
    return 'pausing' if leased else 'ok'


def resume_list(conn, seed, direction, only_reason=None):
    """Lift a hold and queue the list again from its saved cursor."""
    row = conn.execute('SELECT state,hold FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
    if row is None:
        return 'noop'
    if not row['hold'] or (only_reason and row['hold'] != only_reason):
        return 'noop'
    conn.execute('UPDATE lists SET hold=NULL WHERE seed=? AND direction=?', (seed, direction))
    if row['state'] in ('paused', 'queued', 'running'):
        live = conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND lower(seed)=lower(?) AND direction=? "
                            "AND state IN ('queued','leased') LIMIT 1", (seed, direction)).fetchone()
        if not live:
            conn.execute("UPDATE lists SET state='queued',updated_at=? WHERE seed=? AND direction=?", (db.now(), seed, direction))
            job_id = conn.execute('INSERT INTO jobs(kind,seed,direction,priority,created_at) VALUES(?,?,?,?,?)',
                                  ('list', seed, direction, 0, db.now())).lastrowid
            db.start_list_run(conn, job_id, seed, direction)
    return 'ok'


def _pristine(conn, seed, direction):
    """True when the list never fetched anything: safe to delete instead of keep."""
    row = conn.execute('SELECT received,cursor FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
    if row is None:
        return True
    if row['received'] or row['cursor']:
        return False
    fetched = conn.execute("SELECT 1 FROM jobs j JOIN pages p ON p.job_id=j.id WHERE j.kind='list' AND lower(j.seed)=lower(?) "
                           "AND j.direction=? LIMIT 1", (seed, direction)).fetchone()
    in_flight = conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND lower(seed)=lower(?) AND direction=? AND state='leased'",
                             (seed, direction)).fetchone()
    return not fetched and not in_flight


def _purge_list(conn, seed, direction):
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE kind='list' AND lower(seed)=lower(?) AND direction=?", (seed, direction))]
    for part in _chunks(ids):
        marks = ','.join('?' * len(part))
        for table in ('list_runs', 'list_page_requests', 'list_members'):
            conn.execute(f'DELETE FROM {table} WHERE job_id IN ({marks})', part)
        conn.execute(f'DELETE FROM jobs WHERE id IN ({marks})', part)
    conn.execute('DELETE FROM lists WHERE seed=? AND direction=?', (seed, direction))


def _drop_seed_if_empty(conn, seed):
    left = conn.execute('SELECT 1 FROM lists WHERE seed=? LIMIT 1', (seed,)).fetchone()
    has_edges = conn.execute('SELECT 1 FROM edges WHERE seed=? LIMIT 1', (seed,)).fetchone()
    row = conn.execute('SELECT is_me FROM seeds WHERE handle=?', (seed,)).fetchone()
    if not left and not has_edges and row and not row['is_me']:
        conn.execute('DELETE FROM seeds WHERE handle=?', (seed,))
        return True
    return False


# ---------- seed-level moves ----------

def _directions_json(dirs):
    return json.dumps([d for d in DIRECTIONS if d in dirs])


def _effective_for(conn, handle, raw):
    discovered = conn.execute("SELECT 1 FROM collection_discovery WHERE state='queued' AND handle=?", (handle,)).fetchone()
    existing = {r[0] for r in conn.execute('SELECT direction FROM lists WHERE seed=?', (handle,))}
    return effective_directions(raw, bool(discovered), existing)


def _cached_identity(conn, handle):
    row = conn.execute('SELECT ig_id FROM people WHERE handle=?', (handle,)).fetchone()
    return row[0] if row and row[0] else None


def _require_write(conn):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')


def _batches(conn):
    saved = db.get_setting(conn, BATCH_KEY) or {}
    cutoff = _iso(_now() - BATCH_TTL)
    return {k: v for k, v in saved.items() if isinstance(v, dict) and v.get('at', '') >= cutoff}


def _save_batch(conn, request_id, record):
    saved = _batches(conn)
    saved[request_id] = record
    for key in sorted(saved, key=lambda k: saved[k]['at'])[:-BATCH_KEEP]:
        del saved[key]
    db.set_setting(conn, BATCH_KEY, saved)


def add_batch(conn, raw_items, directions, request_id=None):
    """Add pasted profiles. Atomic, and a repeated request_id returns the first answer without touching anything.

    result per item: added | extended | restored | exists | invalid | duplicate | failed
    """
    if not isinstance(raw_items, (list, str)):
        raise ValueError('handles must be text or a list of text')
    dirs = [d for d in DIRECTIONS if isinstance(directions, list) and d in directions]
    if not dirs:
        raise ValueError('Choose followers, following or both.')
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', request_id)):
        raise ValueError('request_id must be 8 to 64 letters, digits, dashes or underscores')
    tokens = split_tokens(raw_items)
    if len(tokens) > MAX_BATCH:
        raise ValueError(f'Add up to {MAX_BATCH} profiles at a time.')
    _require_write(conn)
    try:
        previous = _batches(conn).get(request_id) if request_id else None
        if previous and previous.get('response'):
            conn.rollback()
            return dict(previous['response'], replayed=True)
        items = classify(conn, tokens, dirs)
        stamp, created = db.now(), []
        for item in items:
            item['result'] = item['state'] if item['state'] in ('invalid', 'duplicate', 'exists') else None
            if item['result']:
                continue
            handle = item['handle']
            conn.execute('SAVEPOINT item')
            try:
                item['result'] = _apply_add(conn, item, dirs, stamp, created)
                conn.execute('RELEASE item')
            except ValueError as exc:
                conn.execute('ROLLBACK TO item')
                conn.execute('RELEASE item')
                item.update(result='failed', message=str(exc)[:200], add=[])
        response = {'request_id': request_id, 'items': [_public(i) for i in items], 'summary': summarize(items),
                    'added': sum(i['result'] in ('added', 'extended', 'restored') for i in items)}
        if request_id:
            _save_batch(conn, request_id, {'at': _iso(_now()), 'created': created, 'response': response})
        conn.commit()
        return response
    except BaseException:
        conn.rollback()
        raise


def _public(item):
    keep = ('input', 'handle', 'state', 'result', 'reason', 'message', 'add', 'warn', 'renamed_from', 'person', 'identity')
    return {k: item[k] for k in keep}


def _apply_add(conn, item, dirs, stamp, created):
    handle, add = item['handle'], item['add']
    if item['state'] == 'new':
        conn.execute('INSERT OR IGNORE INTO seeds(handle,ig_id,added_at,directions) VALUES(?,?,?,?)',
                     (handle, _cached_identity(conn, handle), stamp, _directions_json(dirs)))
        for direction in dirs:
            db.queue_list(conn, handle, direction)
        created.append({'handle': handle, 'kind': 'new', 'directions': list(dirs)})
        return 'added'
    seed = conn.execute('SELECT directions,ig_id FROM seeds WHERE handle=?', (handle,)).fetchone()
    effective = _effective_for(conn, handle, seed['directions'])
    union = [d for d in DIRECTIONS if d in effective or d in add]
    conn.execute('UPDATE seeds SET directions=?,removed_at=NULL WHERE handle=?', (_directions_json(union), handle))
    if seed['ig_id'] is None and _cached_identity(conn, handle):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (_cached_identity(conn, handle), handle))
    if item['state'] == 'restore':
        for direction in DIRECTIONS:
            resume_list(conn, handle, direction, only_reason=HOLD_REMOVED)
    for direction in add:
        resume_list(conn, handle, direction, only_reason=HOLD_OFF)
        db.queue_list(conn, handle, direction)
    created.append({'handle': handle, 'kind': item['state'], 'directions': list(add), 'was': list(effective)})
    return 'restored' if item['state'] == 'restore' else 'extended'


def undo_batch(conn, request_id):
    """Take back one add: delete what it created if nothing was fetched yet, otherwise pause or re-remove it."""
    _require_write(conn)
    try:
        record = _batches(conn).get(request_id)
        if not record:
            raise LookupError('That add can no longer be undone.')
        if record.get('undone'):
            conn.rollback()
            return dict(record['undone'], replayed=True)
        results = []
        for entry in record['created']:
            handle = entry['handle']
            if not conn.execute('SELECT 1 FROM seeds WHERE handle=?', (handle,)).fetchone():
                results.append({'handle': handle, 'result': 'gone'})
                continue
            if entry['kind'] == 'restore':
                remove_seed(conn, handle)
                results.append({'handle': handle, 'result': 'removed'})
                continue
            dropped, kept = [], []
            for direction in entry['directions']:
                if _pristine(conn, handle, direction):
                    _purge_list(conn, handle, direction)
                    dropped.append(direction)
                else:
                    pause_list(conn, handle, direction)
                    kept.append(direction)
            if entry['kind'] == 'extend':
                conn.execute('UPDATE seeds SET directions=? WHERE handle=?',
                             (_directions_json([d for d in entry['was'] if d not in dropped] or entry['was']), handle))
            elif not kept:
                _drop_seed_if_empty(conn, handle)
            results.append({'handle': handle, 'result': 'removed' if dropped and not kept else 'paused' if kept else 'gone'})
        out = {'request_id': request_id, 'items': results}
        record['undone'] = out
        saved = _batches(conn)
        saved[request_id] = record
        db.set_setting(conn, BATCH_KEY, saved)
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise


def remove_seed(conn, handle):
    """Stop collecting and hide the seed. Everything already saved stays; adding it again brings it back."""
    conn.execute('UPDATE seeds SET removed_at=? WHERE handle=? AND removed_at IS NULL', (db.now(), handle))
    for direction in DIRECTIONS:
        pause_list(conn, handle, direction, reason=HOLD_REMOVED)
    return 'ok'


def restore_seed(conn, handle):
    conn.execute('UPDATE seeds SET removed_at=NULL WHERE handle=?', (handle,))
    for direction in DIRECTIONS:
        resume_list(conn, handle, direction, only_reason=HOLD_REMOVED)
    return 'ok'


def set_directions(conn, handle, dirs):
    seed = conn.execute('SELECT directions FROM seeds WHERE handle=?', (handle,)).fetchone()
    before = set(_effective_for(conn, handle, seed['directions']))
    after = {d for d in DIRECTIONS if d in dirs}
    if not after:
        raise ValueError('Keep at least one list.')
    if after == before and seed['directions'] is not None:
        return 'noop'
    conn.execute('UPDATE seeds SET directions=? WHERE handle=?', (_directions_json(after), handle))
    for direction in DIRECTIONS:
        if direction in after and direction not in before:
            resume_list(conn, handle, direction, only_reason=HOLD_OFF)
            db.queue_list(conn, handle, direction)
        elif direction in before and direction not in after:
            if _pristine(conn, handle, direction):
                _purge_list(conn, handle, direction)
            else:
                pause_list(conn, handle, direction, reason=HOLD_OFF)
    return 'ok'


def prioritize(conn, handle):
    """Move this seed's waiting lists to the front of the queue. Order only; pacing is untouched."""
    top = conn.execute("SELECT coalesce(max(priority),0) FROM jobs WHERE kind='list' AND state IN ('queued','leased')").fetchone()[0]
    moved = conn.execute("UPDATE jobs SET priority=? WHERE kind='list' AND state='queued' AND lower(seed)=lower(?)",
                         (top + 1, handle)).rowcount
    return 'ok' if moved else 'noop'


ACTIONS = ('pause', 'resume', 'remove', 'restore', 'next', 'directions')


def act(conn, action, handles, directions=None):
    """One action on many seeds, atomically. Returns a result per handle: ok | noop | missing."""
    if action not in ACTIONS:
        raise ValueError('action must be one of ' + ', '.join(ACTIONS))
    if not isinstance(handles, list) or not handles or len(handles) > MAX_BATCH:
        raise ValueError(f'Choose 1 to {MAX_BATCH} profiles.')
    if action == 'directions' and not (isinstance(directions, list) and directions
                                       and all(d in DIRECTIONS for d in directions)):
        raise ValueError('Choose followers, following or both.')
    _require_write(conn)
    try:
        results = []
        for raw in dict.fromkeys(h for h in handles if isinstance(h, str)):
            handle = db.norm_handle(raw)
            row = conn.execute('SELECT handle,removed_at FROM seeds WHERE handle=?', (handle,)).fetchone() if handle else None
            if row is None:
                results.append({'handle': handle or raw, 'result': 'missing'})
                continue
            handle = row['handle']
            if action == 'pause':
                outcomes = [pause_list(conn, handle, d) for d in DIRECTIONS]
                result = 'ok' if any(o in ('ok', 'pausing') for o in outcomes) else 'noop'
            elif action == 'resume':
                outcomes = [resume_list(conn, handle, d, only_reason=HOLD_OPERATOR) for d in DIRECTIONS]
                result = 'ok' if 'ok' in outcomes else 'noop'
            elif action == 'remove':
                result = 'noop' if row['removed_at'] else remove_seed(conn, handle)
            elif action == 'restore':
                result = restore_seed(conn, handle) if row['removed_at'] else 'noop'
            elif action == 'next':
                result = prioritize(conn, handle)
            else:
                result = set_directions(conn, handle, directions)
            results.append({'handle': handle, 'result': result})
        conn.commit()
        return {'items': results}
    except BaseException:
        conn.rollback()
        raise


# ---------- describing seeds in plain words ----------

LIST_LABEL = {'collecting': 'Collecting', 'pausing': 'Pausing', 'waiting': 'Waiting', 'paused': 'Paused',
              'partial': 'Partly collected', 'capped': 'Capped by Instagram', 'private': 'Private', 'error': "Couldn't read",
              'unverified': 'Needs review', 'done': 'Done', 'none': 'Not queued yet', 'off': 'Off'}
SEED_ORDER = ('collecting', 'pausing', 'waiting', 'error', 'private', 'unverified', 'paused', 'partial', 'capped', 'done', 'none')
ATTENTION = ('error', 'private', 'unverified')


def _list_status(wanted, coverage, hold, leased):
    if not wanted:
        return 'off'
    if coverage is None:
        return 'paused' if hold else 'none'
    if hold:
        return 'pausing' if leased else 'paused'
    completion = coverage['completion']
    if completion == 'collecting':
        return 'collecting'
    if completion == 'waiting':
        return 'waiting'
    if completion == 'complete':
        return 'done'
    if completion == 'unverified':
        return 'unverified'
    if completion == 'partial':
        return 'capped' if str(coverage.get('error') or '').startswith('Instagram limited this list;') else 'partial'
    if coverage['state'] == 'private':
        return 'private'
    if coverage['state'] == 'paused':
        return 'paused'
    return 'error'


def _list_note(status, handle, coverage):
    if status == 'private':
        return f'Private. An account that follows @{handle} can read it.'
    if status == 'capped':
        return 'Instagram only shows part of this list.'
    if status in ('error', 'partial', 'unverified') and coverage:
        reason = coverage.get('completion_reason') or ''
        return reason if reason and len(reason) < 160 else ''
    if status == 'pausing':
        return 'Pausing after the current request.'
    return ''


def describe(conn, coverage_rows, *, now=None, scope='both'):
    """Every seed with its two lists, in stable order (newest first)."""
    now = now or _now()
    cov = {(r['seed'].lower(), r['direction']): r for r in coverage_rows}
    holds = {(r[0].lower(), r[1]): r[2] for r in conn.execute('SELECT seed,direction,hold FROM lists WHERE hold IS NOT NULL')}
    leased = {(r[0].lower(), r[1]) for r in conn.execute(
        "SELECT seed,direction FROM jobs WHERE kind='list' AND state='leased' AND leased_until>?", (_iso(now),))}
    aliases = {}
    for r in conn.execute('SELECT old_handle,new_handle,renamed_at FROM seed_aliases ORDER BY renamed_at'):
        aliases.setdefault(r['new_handle'].lower(), []).append({'from': r['old_handle'], 'at': r['renamed_at']})
    suggested = {r[0].lower() for r in conn.execute("SELECT handle FROM collection_discovery WHERE state='queued'")}
    seeds = []
    for r in conn.execute("SELECT s.handle,s.ig_id,s.is_me,s.added_at,s.directions,s.removed_at,p.id AS pid,p.name,p.pic_file,"
                          "p.is_private,p.followers,p.following,p.ig_id AS pig FROM seeds s LEFT JOIN people p ON p.handle=s.handle "
                          "WHERE instr(s.handle,'~')=0 ORDER BY coalesce(s.added_at,'') DESC, s.handle"):
        handle = r['handle']
        key = handle.lower()
        effective = effective_directions(r['directions'], key in suggested, {d for d in DIRECTIONS if (key, d) in cov})
        lists, statuses = {}, []
        saved = expected = 0
        known = True
        for direction in DIRECTIONS:
            wanted = direction in effective
            c = cov.get((key, direction))
            hold = holds.get((key, direction))
            status = _list_status(wanted, c, bool(hold), (key, direction) in leased)
            off_by_scope = wanted and scope == 'following' and direction == 'followers'
            entry = {'direction': direction, 'wanted': wanted and not off_by_scope, 'status': status,
                     'label': LIST_LABEL[status] if not off_by_scope else 'Off while following only',
                     'saved': c['saved_entries'] if c else 0, 'expected': c['expected'] if c else None,
                     'estimate': bool(c and c.get('expected_source') == 'estimate'),
                     'note': _list_note(status, handle, c)}
            if off_by_scope:
                entry['status'] = 'off'
            lists[direction] = entry
            if wanted and not off_by_scope:
                statuses.append(status)
                saved += entry['saved']
                if entry['expected'] is None:
                    known = False
                else:
                    expected += entry['expected']
        if r['removed_at']:
            primary = 'removed'
        else:
            primary = next((s for s in SEED_ORDER if s in statuses), 'done' if not statuses else 'none')
        seed_pct = min(100, round(saved / expected * 100)) if known and expected else None
        seeds.append({
            'handle': handle, 'name': r['name'], 'pic': f"/img/{r['pid']}" if r['pic_file'] else None,
            'followers': r['followers'], 'following': r['following'], 'private': bool(r['is_private']),
            'identity': 'known' if (r['ig_id'] or r['pig']) else 'pending', 'added_at': r['added_at'],
            'yours': bool(r['is_me']), 'source': 'suggested' if key in suggested else 'you',
            'status': primary, 'label': 'Removed' if primary == 'removed' else LIST_LABEL.get(primary, 'Done'),
            'attention': (not r['removed_at']) and any(s in ATTENTION for s in statuses),
            'lists': lists, 'saved': saved, 'expected': expected if known and expected else None, 'pct': seed_pct,
            'renamed_from': aliases.get(key, []),
        })
    return seeds


FILTERS = ('all', 'collecting', 'waiting', 'paused', 'attention', 'done', 'removed')


def _matches(seed, name):
    if name == 'all':
        return seed['status'] != 'removed'
    if name == 'removed':
        return seed['status'] == 'removed'
    if seed['status'] == 'removed':
        return False
    if name == 'attention':
        return seed['attention']
    if name == 'collecting':
        return seed['status'] in ('collecting', 'pausing')
    if name == 'done':
        return seed['status'] in ('done', 'capped', 'partial')
    return seed['status'] == name


def counts(seeds):
    return {name: sum(_matches(s, name) for s in seeds) for name in FILTERS}


def listing(conn, coverage_rows, *, q='', status='all', sort='newest', limit=200, offset=0, scope='both'):
    """The payload behind GET /api/seeds. `revision` changes only when something a person could see changes."""
    if status not in FILTERS:
        raise ValueError('status must be one of ' + ', '.join(FILTERS))
    if sort not in ('newest', 'name'):
        raise ValueError('sort must be newest or name')
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    everything = describe(conn, coverage_rows, scope=scope)
    chosen = [s for s in everything if _matches(s, status)]
    needle = (q or '').strip().lstrip('@').lower()
    if needle:
        chosen = [s for s in chosen if needle in s['handle'].lower() or needle in (s['name'] or '').lower()
                  or any(needle in a['from'].lower() for a in s['renamed_from'])]
    if sort == 'name':
        chosen.sort(key=lambda s: s['handle'].lower())
    page = chosen[offset:offset + limit]
    payload = {'scope': scope, 'counts': counts(everything), 'total': len(chosen), 'offset': offset, 'items': page}
    payload['revision'] = hashlib.sha1(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    return payload

