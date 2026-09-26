"""Client for the Broad stage sidecar (sidecar/broad_server.py on 127.0.0.1:18742): Laya's multilingual encoder plus a head
trained on the Bulk stage's Grok verdicts. Stdlib only.

It answers one number per person: how likely Grok would call them a good lead (buyer, fit >= 60). It is a ranking signal for
who gets a Bulk (Grok) call next; it never gives a verdict and never creates tags.
Down or slow -> skipped silently (health is checked with a 3 s timeout and cached for 60 s).
"""
from __future__ import annotations

import json
from http.client import HTTPException
import math
import threading
import time
import urllib.request

URL = 'http://127.0.0.1:18742'
MODEL = 'broad'
HEALTH_TIMEOUT = 3
HEALTH_TTL = 60
DECIDE_TIMEOUT = 60
BATCH = 256
QUESTIONS = ({'key': 'broad'},)

_health = {'at': None, 'ok': False, 'model': None, 'deployment_version': None}
_lock = threading.Lock()


def _open(req, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)


def available(now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        if _health['at'] is not None and 0 <= now - _health['at'] < HEALTH_TTL:
            return _health['ok']
    ok = False
    data = {}
    try:
        with _open(urllib.request.Request(URL + '/health'), HEALTH_TIMEOUT) as r:
            data = json.loads(r.read(100_000))
            ok = (isinstance(data, dict) and data.get('ok') is True and data.get('model') == MODEL
                  and isinstance(data.get('deployment_version'), str) and bool(data['deployment_version']))
    except (OSError, ValueError, HTTPException):
        ok = False
    with _lock:
        _health.update(at=now, ok=ok, model=data.get('model') if ok else None,
                       deployment_version=data.get('deployment_version') if ok else None)
    return ok


def last_known():
    """Last health result without probing (status endpoints must never wait on the sidecar)."""
    with _lock:
        return _health['ok'] if _health['at'] is not None and 0 <= time.monotonic() - _health['at'] < HEALTH_TTL else None


def reset():
    with _lock:
        _health.update(at=None, ok=False, model=None, deployment_version=None)


def deployment_version():
    with _lock:
        return _health['deployment_version']


def cache_signature():
    """Stored answers are keyed by the trained head's version: a retrain re-scores everyone."""
    return 'broad:' + str(deployment_version() or 'none')


def _failed():
    with _lock:
        _health.update(at=time.monotonic(), ok=False, model=None, deployment_version=None)


def person(p):
    return {k: p.get(k) for k in ('handle', 'name', 'category', 'bio', 'followers')}


def valid_answers(answers):
    return (isinstance(answers, dict) and set(answers) == {'broad'} and isinstance(answers['broad'], (int, float))
            and not isinstance(answers['broad'], bool) and math.isfinite(answers['broad']) and 0 <= answers['broad'] <= 1)


def decide(people):
    """people: dicts with id, profile fields and 'rules' (the rules score) -> {id: {'broad': p}}; {} when anything is off."""
    out = {}
    try:
        ids = [str(p['id']) for p in people]
        if any(isinstance(p['id'], bool) or str(int(p['id'])) != str(p['id']) for p in people):
            return {}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    if len(set(ids)) != len(ids):
        return {}
    version = deployment_version()
    for i in range(0, len(people), BATCH):
        chunk = people[i:i + BATCH]
        expected = set(ids[i:i + BATCH])
        body = {'items': [{'id': str(p['id']), 'person': person(p), 'rules': p.get('rules') or 0} for p in chunk]}
        req = urllib.request.Request(URL + '/decide', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'},
                                     method='POST')
        try:
            with _open(req, DECIDE_TIMEOUT) as r:
                data = json.loads(r.read(5_000_000))
        except (OSError, ValueError, HTTPException):
            _failed()
            return {}
        if (not isinstance(data, dict) or data.get('model') != MODEL or data.get('deployment_version') != version
                or not isinstance(data.get('results'), list) or len(data['results']) != len(chunk)):
            _failed()   # includes a retrain mid-run: the next health check picks up the new version
            return {}
        parsed = {}
        for res in data['results']:
            ans = {'broad': res.get('p')} if isinstance(res, dict) else None
            if not isinstance(res, dict) or str(res.get('id')) not in expected or str(res.get('id')) in parsed or not valid_answers(ans):
                _failed()
                return {}
            parsed[str(res['id'])] = {'broad': float(ans['broad'])}
        if set(parsed) != expected:
            _failed()
            return {}
        out.update({int(pid): ans for pid, ans in parsed.items()})
    return out


def fit(ans):
    """0-100 ranking signal."""
    if not valid_answers(ans):
        return None
    return int(round(ans['broad'] * 100))


def tags(ans, have):
    """A ranking score is not evidence for factual tags."""
    return []
