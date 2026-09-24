"""Optional client for the local Laya decision sidecar (sidecar/laya_server.py on 127.0.0.1:18742). Stdlib only.

Laya is ONE soft ranking signal: it never gates anyone and never gives a verdict. It tags only on its own when very sure
(p >= 0.9 for creator / brand_account / netherlands); otherwise a Laya tag needs a rule or LLM tag that agrees.
Down or slow -> skipped silently (health is checked with a 3 s timeout and cached for 60 s).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

URL = 'http://127.0.0.1:18742'
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

_health = {'at': 0.0, 'ok': False}
_lock = threading.Lock()


def _open(req, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)


def available(now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        if now - _health['at'] < HEALTH_TTL:
            return _health['ok']
    ok = False
    try:
        with _open(urllib.request.Request(URL + '/health'), HEALTH_TIMEOUT) as r:
            data = json.loads(r.read(100_000))
            ok = isinstance(data, dict) and data.get('ok') is not False
    except (OSError, ValueError):
        ok = False
    with _lock:
        _health.update(at=now, ok=ok)
    return ok


def last_known():
    """Last health result without probing (status endpoints must never wait on the sidecar)."""
    with _lock:
        return _health['ok'] if _health['at'] else None


def reset():
    with _lock:
        _health.update(at=0.0, ok=False)


def person_text(p):
    rows = []
    for label, key in (('Handle', 'handle'), ('Name', 'name'), ('Category', 'category'), ('Bio', 'bio'), ('Website', 'website'),
                       ('Followers', 'followers')):
        v = p.get(key)
        if v not in (None, ''):
            rows.append(f"{label}: {'@' if key == 'handle' else ''}{str(v).strip()}")
    return '\n'.join(rows)


def decide(people):
    """people: [dict with id + profile fields] -> {id: {key: p}}. Missing ids = no answer. Raises nothing."""
    out = {}
    for i in range(0, len(people), BATCH):
        chunk = people[i:i + BATCH]
        body = {'items': [{'id': str(p['id']), 'text': person_text(p)} for p in chunk], 'questions': list(QUESTIONS)}
        req = urllib.request.Request(URL + '/decide', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'},
                                     method='POST')
        try:
            with _open(req, DECIDE_TIMEOUT) as r:
                data = json.loads(r.read(5_000_000))
        except (OSError, ValueError):
            with _lock:
                _health.update(at=time.monotonic(), ok=False)   # down or timed out: back off like a failed health check
            return out
        for res in (data.get('results') if isinstance(data, dict) else None) or []:
            if not isinstance(res, dict) or not isinstance(res.get('answers'), dict):
                continue
            ans = {}
            for q in QUESTIONS:
                a = res['answers'].get(q['key'])
                p = a.get('p') if isinstance(a, dict) else None
                if isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 1:
                    ans[q['key']] = float(p)
            try:
                out[int(res.get('id'))] = ans
            except (TypeError, ValueError):
                continue
    return out


def fit(ans):
    """0-100 soft signal (50 = no information)."""
    if not ans:
        return None
    s = 50 + sum(w * (ans[k] - 0.5) * 2 for k, w in FIT_W.items() if k in ans) / 2
    return int(max(0, min(100, round(s))))


def tags(ans, have):
    """Tags Laya may add: only when a rule/LLM tag agrees (then it is already there) or when it is very sure on its own."""
    return [TAG_OF[k] for k, t in SURE.items() if ans.get(k, 0) >= t and TAG_OF[k] not in have]
