"""LLM provider layer (stdlib only): the local OpenRouter proxy first, then OpenRouter direct with several rotating keys.

Keys: env OPENROUTER_API_KEYS (comma-separated) and data/openrouter.json {"keys": [...], "models": [...], "daily_limit": n};
the Settings page edits the file (add_key, remove_key, set_models) and the running pool picks it up at once.
Key values are never logged or returned; status() shows them as sk-…abcd with a stable id (key_id).
Failure handling per (provider, model): 429 / 402 / 5xx -> cooldown (Retry-After, else exponential backoff) and move on;
401 / 403 -> that key is disabled until restart or a passing test; transport errors -> short cooldown for the whole provider.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
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


def key_id(key):
    """Stable short id for a key: lets the UI name a key without ever seeing it."""
    return hashlib.sha256(key.encode()).hexdigest()[:10]


KEY_RX = re.compile(r'[A-Za-z0-9_\-]{16,200}')
MODEL_RX = re.compile(r'[A-Za-z0-9._\-]+/[A-Za-z0-9._\-:]+')
MODELS_MAX = 12


def read_config(path=None):
    try:
        cfg = json.loads(Path(path or CONFIG).read_text())
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def write_config(cfg, path=None):
    """Atomic write, readable by the owner only (the file holds API keys)."""
    path = Path(path or CONFIG)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


class Providers:
    """Provider 'proxy' (no key) first, then one OpenRouter provider per key. State (cooldowns, counters, errors) is keyed
    by the provider id ('proxy' or key_id), so keys can be added or removed while the server runs."""

    def __init__(self, keys=None, models=None, proxy=PROXY, daily_limit=FREE_DAILY, env_keys=()):
        self.lock = threading.Lock()
        self.proxy = proxy
        self.cool = {}        # (pid, model|'*') -> (until, strikes)
        self.disabled = set()  # pid (bad key)
        self.count = {}       # (pid, model) -> (day, n)
        self.errors = {}      # pid -> str
        self.rr = 0
        self.configure(keys, models, daily_limit, env_keys)

    def configure(self, keys, models=None, daily_limit=FREE_DAILY, env_keys=()):
        with self.lock:
            self.keys = [k for k in dict.fromkeys(keys or []) if k]
            self.env_keys = set(env_keys)
            self.models = tuple(models or MODELS)
            self.daily_limit = daily_limit
            live = {'proxy'} | {key_id(k) for k in self.keys}
            for d in (self.cool, self.count):
                for k in [k for k in d if k[0] not in live]:
                    del d[k]
            self.disabled &= live
            self.errors = {k: v for k, v in self.errors.items() if k in live}

    @classmethod
    def load(cls, path=None, proxy=PROXY):
        return cls(proxy=proxy, **settings(path))

    def reload(self, path=None):
        self.configure(**settings(path))

    def _providers(self):
        return [('proxy', self.proxy, None)] + [(key_id(k), OPENROUTER, k) for k in self.keys]

    def _usable(self, pid, key, model, now):
        if pid in self.disabled:
            return False
        for k in ((pid, model), (pid, '*')):
            if self.cool.get(k, (0, 0))[0] > now:
                return False
        if key and self.daily_limit:
            day, n = self.count.get((pid, model), ('', 0))
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

    def _bump(self, pid, model):
        day, n = self.count.get((pid, model), ('', 0))
        self.count[(pid, model)] = (_today(), (n if day == _today() else 0) + 1)

    def chat(self, messages, models=None, timeout=45, budget=90, max_tokens=400, json_mode=True):
        """-> (content, model). Raises Unavailable when no provider/model answered usefully within the budget."""
        deadline = time.monotonic() + budget
        last = 'no provider available'
        for model in models or self.models:
            for pid, url, key in self._order():
                left = deadline - time.monotonic()
                if left <= 1:
                    raise Unavailable('budget spent: ' + last)
                with self.lock:
                    if not self._usable(pid, key, model, time.time()):
                        continue
                    if key:
                        self._bump(pid, model)
                try:
                    content = _post(url, key, model, messages, min(timeout, left), max_tokens, json_mode)
                except _Http as e:
                    last = f'{model}: HTTP {e.code}'
                    with self.lock:
                        self.errors[pid] = last
                        if e.code in (401, 403) and key:
                            self.disabled.add(pid)
                        elif e.code in (402, 408, 429) or e.code >= 500:
                            self._backoff((pid, model), e.retry_after, BACKOFF_BASE, BACKOFF_CAP)
                    continue
                except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                    last = f'{model}: {type(e).__name__}'
                    with self.lock:
                        self.errors[pid] = last
                        self._backoff((pid, '*'), None, DOWN_BASE, DOWN_CAP)
                    continue
                except ValueError as e:   # bad / substituted reply: this model, not the provider
                    last = str(e)[:160]
                    with self.lock:
                        self.errors[pid] = last
                    continue
                with self.lock:
                    self.cool.pop((pid, model), None)
                    self.cool.pop((pid, '*'), None)
                return content, model
        raise Unavailable(last)

    def test(self, pid, timeout=20):
        """One tiny request with this key (or the proxy) on the first model. -> {passed, model, ms, error}.
        A key that answers is enabled again (a 401 had disabled it until restart)."""
        with self.lock:
            found = next(((u, k) for p, u, k in self._providers() if p == pid), None)
            model = self.models[0]
        if not found:
            raise LookupError('no such key')
        url, key = found
        t = time.monotonic()
        msgs = [{'role': 'user', 'content': 'Reply with the JSON object {"ok": true}.'}]
        try:
            _post(url, key, model, msgs, timeout, 20, True)
        except _Http as e:
            err = f'HTTP {e.code}' + {401: ' (key refused)', 402: ' (no credits)', 429: ' (rate limited)'}.get(e.code, '')
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            err = 'not reachable (' + type(e).__name__ + ')'
        except ValueError as e:
            err = str(e)[:160]
        else:
            err = None
        with self.lock:
            if key:
                self._bump(pid, model)
            if err is None:
                self.disabled.discard(pid)
                self.cool.pop((pid, model), None)
                self.cool.pop((pid, '*'), None)
            else:
                self.errors[pid] = f'{model}: {err}'
        return {'passed': err is None, 'model': model, 'ms': round((time.monotonic() - t) * 1000), 'error': err}

    def status(self):
        now = time.time()
        with self.lock:
            out = []
            for pid, url, key in self._providers():
                cools = {m: _iso(u) for (i, m), (u, _) in self.cool.items() if i == pid and u > now}
                today = {m: n for (i, m), (d, n) in self.count.items() if i == pid and d == _today()}
                out.append({'id': pid, 'name': 'proxy' if not key else 'openrouter', 'url': url, 'key': mask(key),
                            'source': None if not key else 'env' if key in self.env_keys else 'file',
                            'disabled': pid in self.disabled, 'cooldowns': cools, 'requests_today': today,
                            'last_error': self.errors.get(pid)})
            return {'providers': out, 'models': list(self.models), 'daily_limit': self.daily_limit}


def settings(path=None):
    """Keys from env OPENROUTER_API_KEYS and data/openrouter.json; models and daily limit from the file."""
    env = [k.strip() for k in os.environ.get('OPENROUTER_API_KEYS', '').split(',') if k.strip()]
    cfg = read_config(path)
    keys = env + [k.strip() for k in cfg.get('keys') or [] if isinstance(k, str) and k.strip()]
    models = cfg.get('models') if isinstance(cfg.get('models'), list) and cfg['models'] \
        and all(isinstance(m, str) for m in cfg['models']) else None
    limit = cfg['daily_limit'] if isinstance(cfg.get('daily_limit'), int) and not isinstance(cfg['daily_limit'], bool) else FREE_DAILY
    return {'keys': keys, 'models': models, 'daily_limit': limit, 'env_keys': env}


def add_key(key, path=None):
    key = key.strip() if isinstance(key, str) else ''
    if not KEY_RX.fullmatch(key):
        raise ValueError('that does not look like an API key')
    cfg = read_config(path)
    keys = [k for k in cfg.get('keys') or [] if isinstance(k, str)]
    if key in keys or key in settings(path)['env_keys']:
        raise ValueError('this key is already added')
    write_config(dict(cfg, keys=keys + [key]), path)
    get().reload(path)
    return key_id(key)


def remove_key(pid, path=None):
    cfg = read_config(path)
    keys = [k for k in cfg.get('keys') or [] if isinstance(k, str)]
    left = [k for k in keys if key_id(k) != pid]
    if len(left) == len(keys):
        if any(key_id(k) == pid for k in settings(path)['env_keys']):
            raise ValueError('this key comes from OPENROUTER_API_KEYS; remove it there')
        raise LookupError('no such key')
    write_config(dict(cfg, keys=left), path)
    get().reload(path)


def set_models(models=None, daily_limit=None, path=None):
    cfg = read_config(path)
    if models is not None:
        if not isinstance(models, list) or not 1 <= len(models) <= MODELS_MAX \
                or not all(isinstance(m, str) and MODEL_RX.fullmatch(m.strip()) for m in models):
            raise ValueError(f'models must be 1-{MODELS_MAX} ids like vendor/model:free')
        cfg['models'] = list(dict.fromkeys(m.strip() for m in models))
    if daily_limit is not None:
        if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or not 0 <= daily_limit <= 100_000:
            raise ValueError('daily_limit must be a whole number 0-100000')
        cfg['daily_limit'] = daily_limit
    write_config(cfg, path)
    get().reload(path)


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
