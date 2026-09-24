"""LLM provider layer (stdlib only): the local OpenRouter proxy first, then OpenRouter direct with several rotating keys.

Keys: env OPENROUTER_API_KEYS (comma-separated) or data/openrouter.json {"keys": [...], "models": [...], "daily_limit": n}.
Key values are never logged or returned; status() shows them as sk-…abcd.
Failure handling per (provider, model): 429 / 402 / 5xx -> cooldown (Retry-After, else exponential backoff) and move on;
401 / 403 -> that key is disabled until restart; transport errors -> short cooldown for the whole provider.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROXY = 'http://127.0.0.1:18741/api/v1/chat/completions'
OPENROUTER = 'https://openrouter.ai/api/v1/chat/completions'
MODELS = ('z-ai/glm-5.2:free', 'google/gemma-4-31b-it:free', 'nvidia/nemotron-3-super-120b-a12b:free')
CONFIG = Path(__file__).resolve().parent.parent / 'data' / 'openrouter.json'
REPLY_MAX = 1_000_000
BACKOFF_BASE, BACKOFF_CAP = 30, 3600       # s, per (provider, model) after 429 / 402 / 5xx without Retry-After
DOWN_BASE, DOWN_CAP = 10, 300              # s, whole provider after a transport error (proxy not running)
FREE_DAILY = 1000                          # requests per key and free model per UTC day (OpenRouter free tier with credits)
HEADERS = {'Content-Type': 'application/json', 'HTTP-Referer': 'http://127.0.0.1:8777', 'X-Title': 'Fortunate Leads'}


class Unavailable(Exception):
    pass


def mask(key):
    return 'sk-…' + key[-4:] if key else None


class Providers:
    def __init__(self, keys=None, models=None, proxy=PROXY, daily_limit=FREE_DAILY):
        self.lock = threading.Lock()
        self.proxy = proxy
        self.keys = [k for k in dict.fromkeys(keys or []) if k]
        self.models = tuple(models or MODELS)
        self.daily_limit = daily_limit
        self.cool = {}        # (provider idx, model|'*') -> (until, strikes)
        self.disabled = set()  # provider idx (bad key)
        self.count = {}       # (provider idx, model) -> (day, n)
        self.errors = {}      # provider idx -> str
        self.rr = 0

    @classmethod
    def load(cls, path=None, proxy=PROXY):
        keys, models, limit = [], None, FREE_DAILY
        env = os.environ.get('OPENROUTER_API_KEYS', '')
        keys += [k.strip() for k in env.split(',') if k.strip()]
        try:
            cfg = json.loads(Path(path or CONFIG).read_text())
            if isinstance(cfg, dict):
                keys += [k.strip() for k in cfg.get('keys') or [] if isinstance(k, str) and k.strip()]
                if isinstance(cfg.get('models'), list) and all(isinstance(m, str) for m in cfg['models']) and cfg['models']:
                    models = cfg['models']
                if isinstance(cfg.get('daily_limit'), int):
                    limit = cfg['daily_limit']
        except (OSError, ValueError):
            pass
        return cls(keys, models, proxy, limit)

    # providers: index 0 = proxy (no key), 1.. = OpenRouter keys
    def _providers(self):
        return [(0, self.proxy, None)] + [(i + 1, OPENROUTER, k) for i, k in enumerate(self.keys)]

    def _usable(self, idx, model, now):
        if idx in self.disabled:
            return False
        for k in ((idx, model), (idx, '*')):
            if self.cool.get(k, (0, 0))[0] > now:
                return False
        if idx and self.daily_limit:
            day, n = self.count.get((idx, model), ('', 0))
            if day == _today() and n >= self.daily_limit:
                return False
        return True

    def _order(self):
        """Proxy first, then the keys starting at a rotating offset so parallel workers spread across keys."""
        with self.lock:
            ps = self._providers()
            keys = ps[1:]
            if keys:
                off = self.rr % len(keys)
                self.rr += 1
                keys = keys[off:] + keys[:off]
            return ps[:1] + keys

    def _backoff(self, key, retry_after, base, cap):
        _, strikes = self.cool.get(key, (0, 0))
        wait = retry_after if retry_after else min(cap, base * 2 ** strikes)
        self.cool[key] = (time.time() + wait, strikes + 1)

    def chat(self, messages, models=None, timeout=45, budget=90, max_tokens=400, json_mode=True):
        """-> (content, model). Raises Unavailable when no provider/model answered usefully within the budget."""
        deadline = time.monotonic() + budget
        last = 'no provider available'
        for model in models or self.models:
            for idx, url, key in self._order():
                left = deadline - time.monotonic()
                if left <= 1:
                    raise Unavailable('budget spent: ' + last)
                with self.lock:
                    if not self._usable(idx, model, time.time()):
                        continue
                    if idx:
                        day, n = self.count.get((idx, model), ('', 0))
                        self.count[(idx, model)] = (_today(), (n if day == _today() else 0) + 1)
                try:
                    content = _post(url, key, model, messages, min(timeout, left), max_tokens, json_mode)
                except _Http as e:
                    last = f'{model}: HTTP {e.code}'
                    with self.lock:
                        self.errors[idx] = last
                        if e.code in (401, 403) and key:
                            self.disabled.add(idx)
                        elif e.code in (402, 408, 429) or e.code >= 500:
                            self._backoff((idx, model), e.retry_after, BACKOFF_BASE, BACKOFF_CAP)
                    continue
                except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                    last = f'{model}: {type(e).__name__}'
                    with self.lock:
                        self.errors[idx] = last
                        self._backoff((idx, '*'), None, DOWN_BASE, DOWN_CAP)
                    continue
                except ValueError as e:   # bad / substituted reply: this model, not the provider
                    last = str(e)[:160]
                    with self.lock:
                        self.errors[idx] = last
                    continue
                with self.lock:
                    self.cool.pop((idx, model), None)
                    self.cool.pop((idx, '*'), None)
                return content, model
        raise Unavailable(last)

    def status(self):
        now = time.time()
        with self.lock:
            out = []
            for idx, url, key in self._providers():
                cools = {m: _iso(u) for (i, m), (u, _) in self.cool.items() if i == idx and u > now}
                today = {m: n for (i, m), (d, n) in self.count.items() if i == idx and d == _today()}
                out.append({'name': 'proxy' if not idx else 'openrouter', 'url': url, 'key': mask(key),
                            'disabled': idx in self.disabled, 'cooldowns': cools, 'requests_today': today,
                            'last_error': self.errors.get(idx)})
            return {'providers': out, 'models': list(self.models), 'daily_limit': self.daily_limit}


class _Http(Exception):
    def __init__(self, code, retry_after):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


def _retry_after(v):
    try:
        return max(1.0, min(float(v), 24 * 3600)) if v else None
    except ValueError:
        return None


def _post(url, key, model, messages, timeout, max_tokens, json_mode):
    body = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.1,
            'reasoning': {'effort': 'low', 'exclude': True}}
    if json_mode:
        body['response_format'] = {'type': 'json_object'}
    headers = dict(HEADERS, **({'Authorization': 'Bearer ' + key} if key else {}))
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}) if not key else urllib.request.BaseHandler()).open(
                req, timeout=timeout) as r:
            data = json.loads(r.read(REPLY_MAX))
    except urllib.error.HTTPError as exc:
        code, ra = exc.code, _retry_after(exc.headers.get('Retry-After') if exc.headers else None)
        exc.close()
        raise _Http(code, ra) from None
    if not isinstance(data, dict):
        raise ValueError(f'{model}: reply is not an object')
    if data.get('error'):
        err = data['error']
        code = err.get('code') if isinstance(err, dict) else None
        if isinstance(code, int) and code in (401, 402, 403, 429) or isinstance(code, int) and code >= 500:
            raise _Http(code, None)
        raise ValueError(f'{model}: ' + str(err)[:160])
    returned = str(data.get('model') or model)
    if returned.split(':')[0] != model.split(':')[0]:
        raise ValueError(f'{model}: provider substituted {returned}')
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f'{model}: empty reply')
    return content


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds')


PROVIDERS = [None]   # the process-wide instance, created on first use


def get():
    if PROVIDERS[0] is None:
        PROVIDERS[0] = Providers.load()
    return PROVIDERS[0]
