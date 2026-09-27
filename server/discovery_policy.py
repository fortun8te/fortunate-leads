"""Request-cost ordering for fresh following networks. No coverage inference.

Saved counts only estimate work. They cannot prove completion or predict new
people. Keep every fourth admission in the original evidence order so unknown
and large networks retain opportunities within the bounded candidate pool.
"""
from math import ceil

POLICY_KEY = 'auto_discover_policy'
LEGACY = 'saved_fit'
COMPLETION = 'bounded_completion'
POLICIES = (LEGACY, COMPLETION)
CANDIDATE_POOL = 200
EXPLORATION_EVERY = 4
ESTIMATED_ROWS_PER_PAGE = 45


def estimated_requests(following):
    """One target lookup plus page calls, for ranking only; unknown stays unknown."""
    if following is None:
        return None
    return 1 + max(1, ceil(max(0, following) / ESTIMATED_ROWS_PER_PAGE))


def evidence_group(row):
    """Keep owner evidence ahead of fit, and strong saved fit ahead of good fit."""
    status = {'client': 0, 'talking': 1, 'interested': 2}.get(row['owner_status'], 3)
    relationship = row['relationships'] != '[]'
    manual = bool(row['manual_fit'])
    # Owner evidence can qualify profiles whose machine fit is absent or low.
    fit = 0 if status < 3 or relationship or manual else int((row['content_fit'] or 0) < 70)
    return status, not relationship, not manual, fit


def rank_for_completion(rows, admission_count):
    """Return a new ordering; never remove a target or change its requested scope."""
    rows = list(rows)
    if (admission_count + 1) % EXPLORATION_EVERY == 0:
        return rows
    def key(item):
        index, row = item
        requests = estimated_requests(row['following'])
        return evidence_group(row), requests is None, requests or 0, index
    return [row for _, row in sorted(enumerate(rows), key=key)]
