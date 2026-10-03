"""Read-only, bounded selection of saved-content work. Never calls a model.

The worker can drain current cached outcomes before waiting for inference. The
manifest contains identities and hashes only, never profile or note text. It is
advisory until the worker rechecks its queue revision and content hash under the
same write transaction used by the existing processing worker.
"""
import hashlib
import json
import time

import local_qualification
import owner
import owner_notes
import processing_state
import qualify

VERSION = 'saved-content-v1'
MAX_BATCH = 100


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _person(conn, person_id):
    row = conn.execute('SELECT * FROM people WHERE id=?', (person_id,)).fetchone()
    if not row:
        return None
    person = dict(row)
    # Raw notes never enter this packet. The separate local-only context reader
    # validates its own note snapshot before producing an unconfirmed hint.
    mark = conn.execute('SELECT status FROM marks WHERE person_id=?', (person_id,)).fetchone()
    person['status'] = mark[0] if mark else None
    person['manual_tags'] = [r[0] for r in conn.execute(
        "SELECT tag FROM tags WHERE person_id=? AND source='manual' ORDER BY tag", (person_id,))]
    owner.hydrate(conn, [person])
    return person


def _protected(conn, person):
    return (person['handle'].casefold() == 'fortun8te' or '~' in person['handle'] or
            bool(conn.execute('SELECT 1 FROM seeds WHERE is_me=1 AND handle=?',
                              (person['handle'],)).fetchone()))


def reusable_result(result, person, note_context=None):
    """Current identity plus current evidence policy, before trusting old cache."""
    if not local_qualification.current_result(result, person, note_context):
        return False
    verdict = result.get('verdict')
    if result.get('status') == 'unverified' and verdict is None:
        return True
    if result.get('status') not in ('complete', 'needs_research') or not isinstance(verdict, dict):
        return False
    # Persisted local verdicts contain clamped integer values. A content hash
    # does not authenticate these answer fields, so validate them independently.
    if any(type(verdict.get(key)) is not int or not 0 <= verdict[key] <= 100
           for key in ('content_fit', 'fit', 'score')):
        return False
    if verdict.get('role') == 'unclear' and verdict['content_fit'] > 45:
        return False
    if verdict.get('tier') != qualify._tier(verdict['score'], bool(str(person.get('bio') or '').strip())):
        return False
    evidence = verdict.get('evidence')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 2:
        return False
    quotes = []
    for quote in evidence:
        if not isinstance(quote, str):
            return False
        if quote.startswith('Your tag: "') and quote.endswith('"'):
            quote = quote[len('Your tag: "'):-1]
        quotes.append(quote)
    safe = local_qualification._saved_person(person)
    value = {'role': verdict.get('role'), 'fit': verdict.get('content_fit'), 'evidence': quotes}
    if len(qualify._evidence_sources(value, safe)) != len(quotes):
        return False
    # An empty existing-tag set gives the complete set this validator may emit;
    # the original worker may have omitted tags already supplied by rules/owner.
    checked = qualify._verdict(value, safe, [],
                               'local:' + result['model'], result.get('prompt'))
    if not checked or any(checked[key] != verdict.get(key) for key in
                          ('role', 'reason', 'content_fit', 'fit', 'model', 'prompt', 'evidence', 'brand_handle')):
        return False
    # The local schema can emit only role/fit/evidence. Extra commercial claims
    # are an older or corrupt cache entry, and need a fresh validated review.
    if set(verdict) - (set(checked) | {'local_model_version', 'local_input_hash'}):
        return False
    if (verdict.get('local_model_version') != result.get('model_version') or
            verdict.get('local_input_hash') != result.get('input_hash')):
        return False
    tags = verdict.get('tags')
    if not isinstance(tags, list):
        return False
    allowed_tags = set(checked.get('tags') or [])
    # Manual tags are loaded separately by the worker. Every tag inside this
    # cached verdict is generated, irrespective of its claimed group name.
    return all(isinstance(tag, (tuple, list)) and len(tag) == 2 and
               all(isinstance(part, str) for part in tag) and tuple(tag) in allowed_tags
               for tag in tags)


def decision(conn, job):
    """Classify one queued revision using the same content identity as inference."""
    pid = job['person_id']
    person = _person(conn, pid)
    item = {'person_id': pid, 'revision': job['revision'], 'repair_attempt': job['repair_attempt'],
            'source_hash': None, 'source_state': 'missing', 'action': 'skip_missing'}
    if not person or _protected(conn, person):
        return dict(item, action='skip_protected' if person else 'skip_missing')
    if not str(person.get('bio') or '').strip():
        return dict(item, action='skip_empty', source_state='needs_profile_read')
    context = owner_notes.local_context(conn, pid)
    source_hash = local_qualification.input_hash(person, context)
    item.update(source_hash=source_hash, source_state='saved_profile', action='infer')
    # A note still awaiting its LOCAL reader must not be bypassed by a reusable
    # review. Notes keep first priority in local_processing_step.
    has_note = conn.execute("SELECT 1 FROM marks WHERE person_id=? AND trim(coalesce(note,''))!=''",
                            (pid,)).fetchone()
    if has_note and owner_notes.result(conn, pid)['state'] not in ('ready', 'disabled'):
        return dict(item, action='wait_local_note')
    row = conn.execute('SELECT * FROM local_reviews WHERE person_id=?', (pid,)).fetchone()
    if not row:
        return item
    result = dict(row)
    try:
        result['verdict'] = json.loads(result.get('verdict') or 'null')
    except (TypeError, ValueError):
        return dict(item, source_state='invalid_cached_result')
    if job['repair_attempt'] or not reusable_result(result, person, context):
        return dict(item, source_state='needs_fresh_review')
    # Match the worker's existing policy: a deterministic current failure is
    # terminal for this input; an owner retry has repair_attempt=1 and calls AI.
    if result['status'] == 'unverified' and result['verdict'] is None:
        return dict(item, action='reuse_terminal', source_state='current_cached_result')
    if result['status'] not in ('complete', 'needs_research') or not isinstance(result['verdict'], dict):
        return dict(item, source_state='invalid_cached_result')
    return dict(item, action='reuse', source_state='current_cached_result')


def next_batch(conn, limit=64, now=None):
    """Inspect at most limit indexed ready rows; no full-profile scan or writes."""
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise ValueError('Batch size must be between 1 and 100')
    ready_at = time.time() if now is None else now
    jobs = []
    # Include empty/deleted rows in the preview even though the inference queue
    # excludes them. Each range uses local_queue_rank_ready, bounded by limit.
    for priority in (1, 0):
        for band in (2, 1, 0):
            jobs.extend(conn.execute('''SELECT person_id,revision,repair_attempt FROM local_queue
                WHERE priority=? AND rank_band=? AND retry_at<=?
                ORDER BY retry_at,person_id LIMIT ?''',
                (priority, band, ready_at, limit-len(jobs))).fetchall())
            if len(jobs) == limit:
                break
        if len(jobs) == limit:
            break
    entries = [decision(conn, job) for job in jobs]
    counts = {name: sum(e['action'] == name for e in entries) for name in
              ('reuse', 'reuse_terminal', 'infer', 'skip_empty', 'skip_missing',
               'skip_protected', 'wait_local_note')}
    body = {'version': VERSION, 'inspected': len(entries), 'limit': limit,
            'entries': entries, 'counts': counts,
            'model_calls_needed': counts['infer'],
            'model_calls_avoided': counts['reuse'] + counts['reuse_terminal'],
            'scope': 'This ready batch only; no whole-workspace completion estimate.'}
    return dict(body, manifest_hash=_hash(body))


def next_job(conn, limit=64, now=None):
    """Worker hook: prefer cached outcomes within the same owner-priority class.

    Avoids an unavailable model blocking reusable results behind the first row.
    Queue ordering across owner edits versus bulk work remains unchanged. The
    caller keeps its existing mode/content/revision commit guards.
    """
    jobs = processing_state.next_pending(conn, limit, now=now)
    if not jobs:
        return None
    first_priority = conn.execute('SELECT priority FROM local_queue WHERE person_id=?',
                                  (jobs[0]['person_id'],)).fetchone()[0]
    for job in jobs:
        priority = conn.execute('SELECT priority FROM local_queue WHERE person_id=?',
                                (job['person_id'],)).fetchone()[0]
        if priority != first_priority:
            break
        if decision(conn, job)['action'] in ('reuse', 'reuse_terminal'):
            return job
    return jobs[0]


def current(conn, entry):
    """Recheck an advisory entry before dispatch; does not acquire a writer lock."""
    job = conn.execute('SELECT person_id,revision,repair_attempt FROM local_queue WHERE person_id=?',
                       (entry.get('person_id'),)).fetchone()
    if not job or job['revision'] != entry.get('revision'):
        return False
    latest = decision(conn, job)
    return all(latest[key] == entry.get(key) for key in ('source_hash', 'action', 'repair_attempt'))
