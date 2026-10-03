"""Experimental, pure page allocation. Scores are heuristics, never probabilities.

Only observations already paid for may enter this module. It makes no network
calls, changes no pacing, and does not authorize a held or ineligible account.
"""
import math

MAX_CANDIDATES = 64
EXPLORATION_INTERVAL = 4


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('observed metrics must be finite nonnegative numbers')
    return value


def choose(candidates, state=None):
    """Return one job from an already eligible, ordered window of at most 64.

    Positive manual_priority wins over automatic work; ties preserve input order.
    New targets get one measured page before exploitation. Every fourth decision
    selects the least recently admitted job, including untested jobs, to prevent
    starvation within the caller's bounded window. The caller rotates that window.
    """
    candidates = list(candidates)
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError('candidate window exceeds 64')
    if not candidates:
        return None
    state = state or {}
    metrics = state.get('jobs', {})
    ids = [str(c['job_id']) for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate job_id')
    priority = max(_number(c.get('manual_priority', 0)) for c in candidates)
    if priority > 0:
        chosen = next(c for c in candidates if c.get('manual_priority', 0) == priority)
        return dict(job_id=chosen['job_id'], reason='manual priority preserved', score=None, experimental=True)
    def observed(c):
        return metrics.get(str(c['job_id']), c.get('observed', {}))
    def score(c):
        m = observed(c)
        requests = _number(m.get('requests', 0))
        edges = _number(m.get('new_edges', 0))
        people = _number(m.get('new_people', 0))
        return (0.5 * edges + 0.5 * people) / max(1, requests)
    # Validate even when exploration, so malformed saved data cannot silently win.
    for c in candidates:
        score(c)
    exploration = (state.get('admissions', 0) + 1) % EXPLORATION_INTERVAL == 0
    untried = [c for c in candidates if observed(c).get('pages', 0) == 0]
    if exploration:
        chosen = min(candidates, key=lambda c: observed(c).get('last_admission', -1))
        reason = 'scheduled exploration of least recently tried list'
    elif untried and not any(observed(c).get('pages', 0) for c in candidates):
        chosen, reason = untried[0], 'initial one-page sample'
    else:
        tried = [c for c in candidates if observed(c).get('pages', 0)]
        chosen = max(tried or untried, key=score)
        reason = 'observed edge and new-person yield per request'
    return dict(job_id=chosen['job_id'], reason=reason, score=score(chosen), experimental=True)


def observe(state, job_id, *, requests, new_edges, new_people, is_page=True):
    """Return fresh JSON-serializable state after one consumed recorded event."""
    values = dict(requests=requests, new_edges=new_edges, new_people=new_people)
    for value in values.values():
        _number(value)
    state = state or {}
    jobs = {key: dict(value) for key, value in state.get('jobs', {}).items()}
    key = str(job_id)
    old = jobs.get(key, {})
    admissions = state.get('admissions', 0) + 1
    jobs[key] = dict(old, **{name: old.get(name, 0) + value for name, value in values.items()},
                     pages=old.get('pages', 0) + int(is_page), last_admission=admissions)
    return dict(admissions=admissions, jobs=jobs)
