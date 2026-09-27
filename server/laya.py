"""Optional client for the local Laya decision sidecar (sidecar/laya_server.py on 127.0.0.1:18742). Stdlib only.

Laya is ONE soft ranking signal: it never gates anyone and never gives a verdict. Its scores are uncalibrated ranking hints and never create factual tags.
Down or slow -> skipped silently (health is checked with a 3 s timeout and cached for 60 s).
"""
from __future__ import annotations

import json
import hashlib
from http.client import HTTPException
import math
import os
import threading
import errno
import resource_budget
import urllib.error
import time
import urllib.request

PORT = int(os.environ.get('LAYA_PORT', '18742'))
if not 1 <= PORT <= 65535:
    raise ValueError('LAYA_PORT must be between 1 and 65535')
URL = f'http://127.0.0.1:{PORT}'
MODEL = os.environ.get('LAYA_MODEL', 'convaiinnovations/laya:multilingual')
DEPLOYMENT_VERSION = os.environ.get('LAYA_DEPLOYMENT_VERSION', 'laya-0.3.20-checkpoint-1')
PIPELINE_VERSION = 'laya-soft-signal-v2'
HEALTH_TIMEOUT = 3
HEALTH_TTL = 60
DECIDE_TIMEOUT = 60
BATCH = 64
QUESTIONS = (
    {'key': 'dtc_founder', 'q': 'Is this person a founder, owner or decision-maker of a direct-to-consumer brand that sells its own physical products?'},
    {'key': 'brand_account', 'q': 'Is this the official account of a brand or company rather than a personal account?'},
    {'key': 'creator', 'q': 'Is this person mainly a content creator, influencer or UGC creator who makes content for other brands?'},
    {'key': 'service_provider', 'q': 'Is this an agency, freelancer, consultant or other service provider selling services rather than products?'},
    {'key': 'netherlands', 'q': 'Is this person or business based in the Netherlands?'},
)
# answer key -> tag it may confirm (the rule/LLM tag must agree) ; sure = p at which Laya alone may tag
TAG_OF = {'dtc_founder': 'Founder', 'brand_account': 'Brand', 'creator': 'Creator', 'service_provider': 'Agency', 'netherlands': 'NL'}
SURE = {'creator': 0.9, 'brand_account': 0.9, 'netherlands': 0.9}
FIT_W = {'dtc_founder': 45, 'brand_account': 25, 'netherlands': 10, 'creator': -30, 'service_provider': -25}

_health = {'at': None, 'ok': False, 'model': None, 'deployment_version': None}
_lock = threading.Lock()
_probe_lock = threading.Lock()
_activity = {'unknown': False, 'checked_at': float('-inf')}
_activity_probe_lock = threading.Lock()


def _open(req, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)


def available(now=None):
    explicit_now = now is not None
    now = time.monotonic() if not explicit_now else now
    with _lock:
        if _health['at'] is not None and 0 <= now - _health['at'] < HEALTH_TTL:
            return _health['ok']
    # Only one caller probes an expired sidecar. The short state lock stays free
    # so status reads never wait for the network timeout.
    with _probe_lock:
        now = time.monotonic() if not explicit_now else now
        with _lock:
            if _health['at'] is not None and 0 <= now - _health['at'] < HEALTH_TTL:
                return _health['ok']
        ok = False
        data = {}
        try:
            with _open(urllib.request.Request(URL + '/health'), HEALTH_TIMEOUT) as r:
                data = json.loads(r.read(100_000))
                ok = (isinstance(data, dict) and data.get('ok') is True and data.get('model') == MODEL
                      and data.get('deployment_version') == DEPLOYMENT_VERSION)
        except (OSError, ValueError, HTTPException):
            ok = False
        with _lock:
            _health.update(at=time.monotonic() if not explicit_now else now, ok=ok,
                           model=data.get('model') if ok else None,
                           deployment_version=data.get('deployment_version') if ok else None)
            if ok and data.get('busy') is True:
                # A prior app process may have left a request running in the sidecar.
                _activity.update(unknown=True, checked_at=float('-inf'))
                resource_budget.set_uncertain('laya', True)
        return ok


def last_known():
    """Last health result without probing (status endpoints must never wait on the sidecar)."""
    with _lock:
        return _health['ok'] if _health['at'] is not None and 0 <= time.monotonic() - _health['at'] < HEALTH_TTL else None


def reset():
    with _probe_lock, _lock:
        _health.update(at=None, ok=False, model=None, deployment_version=None)


_signature = {}


def cache_signature():
    """Version the answers by checkpoint deployment, question text, and local scoring policy."""
    key = (PIPELINE_VERSION, MODEL, DEPLOYMENT_VERSION, id(QUESTIONS), tuple(sorted(FIT_W.items())))
    if key not in _signature:
        _signature.clear()
        _signature[key] = _cache_signature()
    return _signature[key]


def _cache_signature():
    raw = json.dumps([PIPELINE_VERSION, MODEL, DEPLOYMENT_VERSION, QUESTIONS, FIT_W], ensure_ascii=False,
                     sort_keys=True, separators=(',', ':'))
    return 'laya:' + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _failed():
    with _lock:
        _health.update(at=time.monotonic(), ok=False, model=None, deployment_version=None)


def person_text(p):
    rows = []
    for label, key in (('Handle', 'handle'), ('Name', 'name'), ('Category', 'category'), ('Bio', 'bio'), ('Website', 'website'),
                       ('Followers', 'followers')):
        v = p.get(key)
        if v not in (None, ''):
            rows.append(f"{label}: {'@' if key == 'handle' else ''}{str(v).strip()}")
    return '\n'.join(rows)


def valid_answers(answers):
    return (isinstance(answers, dict) and set(answers) == {q['key'] for q in QUESTIONS}
            and all(isinstance(p, (int, float)) and not isinstance(p, bool)
                    and math.isfinite(p) and 0 <= p <= 1 for p in answers.values()))


def decide(people):
    """Return complete validated batches, or no answers if any batch is invalid."""
    out = {}
    try:
        ids = [str(p['id']) for p in people]
        if any(isinstance(p['id'], bool) or str(int(p['id'])) != str(p['id']) for p in people):
            return {}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    if len(set(ids)) != len(ids):
        return {}
    for i in range(0, len(people), BATCH):
        chunk = people[i:i + BATCH]
        expected = set(ids[i:i + BATCH])
        body = {'items': [{'id': str(p['id']), 'text': person_text(p)} for p in chunk], 'questions': list(QUESTIONS)}
        req = urllib.request.Request(URL + '/decide', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'},
                                     method='POST')
        try:
            with _open(req, DECIDE_TIMEOUT) as r:
                data = json.loads(r.read(5_000_000))
        except (OSError, HTTPException):
            with _lock:
                _activity.update(unknown=True, checked_at=float('-inf'))
            resource_budget.set_uncertain('laya', True)
            _failed()
            return {}
        except ValueError:
            _failed()
            return {}
        if (not isinstance(data, dict) or data.get('model') != MODEL
                or data.get('deployment_version') != DEPLOYMENT_VERSION
                or not isinstance(data.get('results'), list) or len(data['results']) != len(chunk)):
            _failed()
            return {}
        parsed = {}
        for res in data['results']:
            if (not isinstance(res, dict) or not isinstance(res.get('answers'), dict)
                    or str(res.get('id')) not in expected or str(res.get('id')) in parsed):
                _failed()
                return {}
            ans = {k: a.get('p') if isinstance(a, dict) else None for k, a in res['answers'].items()}
            if not valid_answers(ans):
                _failed()
                return {}
            parsed[str(res['id'])] = {k: float(p) for k, p in ans.items()}
        if set(parsed) != expected:
            _failed()
            return {}
        out.update({int(pid): ans for pid, ans in parsed.items()})
    return out


def fit(ans):
    """0-100 soft signal (50 = no information)."""
    if not valid_answers(ans):
        return None
    s = 50 + sum(w * (ans[k] - 0.5) * 2 for k, w in FIT_W.items() if k in ans) / 2
    return int(max(0, min(100, round(s))))


def tags(ans, have):
    """Uncalibrated model scores are not independent evidence for factual tags."""
    return []


def runtime_status():
    """After a transport failure, require sidecar idle proof before acknowledging stop."""
    with _lock:
        unknown = _activity['unknown']
        due = time.monotonic() - _activity['checked_at'] >= 2
    if unknown and due and _activity_probe_lock.acquire(blocking=False):
        try:
            idle = False
            try:
                with _open(urllib.request.Request(URL + '/health'), 0.5) as response:
                    data = json.loads(response.read(100_000))
                idle = (isinstance(data, dict) and data.get('ok') is True
                        and data.get('model') == MODEL
                        and data.get('deployment_version') == DEPLOYMENT_VERSION
                        and data.get('busy') is False)
            except (OSError, HTTPException) as exc:
                cause = getattr(exc, 'reason', exc)
                idle = isinstance(cause, ConnectionRefusedError) or getattr(cause, 'errno', None) == errno.ECONNREFUSED
            except (ValueError, TypeError):
                pass
            with _lock:
                _activity.update(unknown=not idle, checked_at=time.monotonic())
                unknown = _activity['unknown']
            resource_budget.set_uncertain('laya', unknown)
        finally:
            _activity_probe_lock.release()
    return {'ready': last_known(), 'busy': unknown, 'activity_unknown': unknown,
            'error': 'Waiting for Laya to confirm the request has stopped.' if unknown else ''}
