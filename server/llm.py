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
import ssl
import threading
import tempfile
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

PROXY = 'http://127.0.0.1:18741/api/v1/chat/completions'
OPENROUTER = 'https://openrouter.ai/api/v1/chat/completions'
MODELS = ('z-ai/glm-5.2:free', 'google/gemma-4-31b-it:free', 'nvidia/nemotron-3-super-120b-a12b:free')
CONFIG = Path(__file__).resolve().parent.parent / 'data' / 'openrouter.json'
REPLY_MAX = 1_000_000
BACKOFF_BASE, BACKOFF_CAP = 30, 3600       # s, per (provider, model) after 429 / 402 / 5xx without Retry-After
DOWN_BASE, DOWN_CAP = 10, 300              # s, whole provider after a transport error (proxy not running)
FREE_DAILY = 1000                          # conservative local ceiling per key across all free models, per UTC day
MODELS_URL = 'https://openrouter.ai/api/v1/models'
KEY_URL = 'https://openrouter.ai/api/v1/key'
MODELS_EVERY = 24 * 3600                   # s between refreshes of the free / stealth model list


def _ssl():
    """python.org / Homebrew Pythons ship without a CA bundle: every https call to OpenRouter failed with
    CERTIFICATE_VERIFY_FAILED and showed as 'not reachable'. Use the system bundle (or certifi when installed)."""
    for f in ('/etc/ssl/cert.pem', '/opt/homebrew/etc/ca-certificates/cert.pem'):
        if Path(f).is_file():
            return ssl.create_default_context(cafile=f)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL = _ssl()


def _opener(direct):
    """direct: the local proxy (no system proxy settings in between)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}) if direct else urllib.request.BaseHandler(),
                                       urllib.request.HTTPSHandler(context=SSL))


def _is_spent(code, text):
    """429 / 402 that means 'this key used up its free requests for today' rather than 'slow down'."""
    t = (text or '').lower()
    return code == 402 or code == 429 and ('per-day' in t or 'per day' in t or 'daily' in t or 'free-models-per' in t)


def _next_midnight():
    d = datetime.now(timezone.utc)
    return (d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400)


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
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)



class Providers:
    """Provider 'proxy' (no key) first, then one OpenRouter provider per key. State (cooldowns, counters, errors) is keyed
    by the provider id ('proxy' or key_id), so keys can be added or removed while the server runs."""

    def __init__(self, keys=None, models=None, proxy=PROXY, daily_limit=FREE_DAILY, env_keys=(), state_path=None):
        self.lock = threading.Lock()
        self.proxy = proxy
        self.cool = {}        # (pid, model|'*') -> (until, strikes)
        self.disabled = set()  # pid (bad key)
        self.count = {}       # (pid, model) -> (day, n)
        self.errors = {}      # pid -> str
        self.spent = {}       # pid -> epoch s: this key's free daily requests are used up until then
        self.checked = {}     # pid -> (epoch s, state) from the last test
        self.rr = 0
        self.state_path = Path(state_path) if state_path else None
        self._load_state()
        self.configure(keys, models, daily_limit, env_keys)

    def _load_state(self):
        """Restore only local usage metadata. The sidecar never contains API keys."""
        if not self.state_path:
            return
        try:
            state = json.loads(self.state_path.read_text())
            if state.get('day') == _today():
                self.count = {(pid, model): (_today(), n)
                              for pid, models in state.get('counts', {}).items()
                              for model, n in models.items()
                              if isinstance(pid, str) and isinstance(model, str)
                              and isinstance(n, int) and not isinstance(n, bool) and n >= 0}
            self.spent = {pid: until for pid, until in state.get('spent', {}).items()
                          if isinstance(pid, str) and isinstance(until, (int, float))
                          and math.isfinite(until) and until > time.time()}
            self.cool = {(pid, model): (until, strikes)
                         for pid, models in state.get('cooldowns', {}).items()
                         for model, (until, strikes) in models.items()
                         if isinstance(pid, str) and isinstance(model, str)
                         and isinstance(until, (int, float)) and math.isfinite(until)
                         and until > time.time() and isinstance(strikes, int) and 0 <= strikes <= 64}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_state(self):
        if not self.state_path:
            return
        today = _today()
        counts = {}
        for (pid, model), (day, n) in self.count.items():
            if day == today and pid != 'proxy':
                counts.setdefault(pid, {})[model] = n
        cooldowns = {}
        for (pid, model), (until, strikes) in self.cool.items():
            if until > time.time():
                cooldowns.setdefault(pid, {})[model] = [until, strikes]
        state = {'day': today, 'counts': counts, 'cooldowns': cooldowns,
                 'spent': {pid: until for pid, until in self.spent.items() if until > time.time()}}
        write_config(state, self.state_path)

    def configure(self, keys, models=None, daily_limit=FREE_DAILY, env_keys=()):
        with self.lock:
            self.keys = [k for k in dict.fromkeys(keys or []) if k]
            self.env_keys = set(env_keys)
            self.models = tuple(m for m in (models or MODELS) if isinstance(m, str) and MODEL_RX.fullmatch(m) and m.endswith(':free')) or MODELS
            self.daily_limit = max(1, min(FREE_DAILY, daily_limit))
            live = {'proxy'} | {key_id(k) for k in self.keys}
            # Keep usage and backoff when a key is temporarily removed and later re-added.
            self.disabled &= live
            self.checked = {k: v for k, v in self.checked.items() if k in live}
            self.errors = {k: v for k, v in self.errors.items() if k in live}

    @classmethod
    def load(cls, path=None, proxy=PROXY):
        config_path = Path(path or CONFIG)
        return cls(proxy=proxy, state_path=config_path.with_name(config_path.stem + '.state.json'), **settings(path))

    def reload(self, path=None):
        self.configure(**settings(path))

    def _providers(self):
        return [('proxy', self.proxy, None)] + [(key_id(k), OPENROUTER, k) for k in self.keys]

    def _usable(self, pid, key, model, now):
        if pid in self.disabled or self.spent.get(pid, 0) > now:
            return False
        for k in ((pid, model), (pid, '*')):
            if self.cool.get(k, (0, 0))[0] > now:
                return False
        if key and self.daily_limit:
            n = sum(n for (p, _), (day, n) in self.count.items() if p == pid and day == _today())
            if n >= self.daily_limit:
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
        wait = retry_after if retry_after else min(cap, base * 2 ** min(strikes, 16))
        self.cool[key] = (time.time() + wait, min(strikes + 1, 64))
        self._save_state()

    def _bump(self, pid, model):
        day, n = self.count.get((pid, model), ('', 0))
        self.count[(pid, model)] = (_today(), (n if day == _today() else 0) + 1)
        self._save_state()

    def chat(self, messages, models=None, timeout=45, budget=90, max_tokens=400, json_mode=True):
        """-> (content, model). Raises Unavailable when no provider/model answered usefully within the budget."""
        deadline = time.monotonic() + budget
        last = 'no provider available'
        selected = tuple(models or self.models)
        if not all(isinstance(m, str) and MODEL_RX.fullmatch(m) and m.endswith(':free') for m in selected):
            raise ValueError('only :free models may be requested')
        for model in selected:
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
                        elif key and _is_spent(e.code, e.text):
                            self.spent[pid] = e.reset or _next_midnight()
                            self._save_state()
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
                    last = f'{model}: invalid provider reply'
                    with self.lock:
                        self.errors[pid] = last
                    continue
                with self.lock:
                    # A concurrent failure may have established a new cooldown while this call ran.
                    if not any(self.cool.get(k, (0, 0))[0] > time.time()
                               for k in ((pid, model), (pid, '*'))):
                        self.errors.pop(pid, None)
                    self.checked[pid] = (time.time(), 'ok')
                return content, model
        raise Unavailable(last)

    def test(self, pid, timeout=20):
        """One tiny request with this key (or the proxy) on the first usable model -> {passed, state, model, ms, error}.
        state: ok | spent (free daily requests used up; back at `until`) | broken (key refused) | unreachable | error.
        A key that answers is enabled again (a 401 had disabled it until restart)."""
        with self.lock:
            found = next(((u, k) for p, u, k in self._providers() if p == pid), None)
            models = list(self.models)
        if not found:
            raise LookupError('no such key')
        url, key = found
        t = time.monotonic()
        msgs = [{'role': 'user', 'content': 'Reply with the JSON object {"ok": true}.'}]
        err, state, until, model = None, 'ok', None, models[0]
        for model in models[:3]:   # a model that is down right now says nothing about the key
            with self.lock:
                if any(self.cool.get(k, (0, 0))[0] > time.time() for k in ((pid, model), (pid, '*'))):
                    err, state = 'provider is cooling down', 'error'
                    continue
            if key:
                with self.lock:
                    used = sum(n for (p, _), (day, n) in self.count.items()
                               if p == pid and day == _today())
                    if self.spent.get(pid, 0) > time.time() or self.daily_limit and used >= self.daily_limit:
                        err, state = 'local daily request limit reached', 'spent'
                        until = self.spent.get(pid) or _next_midnight()
                        break
                    if any(self.cool.get(k, (0, 0))[0] > time.time() for k in ((pid, model), (pid, '*'))):
                        err, state = 'provider is cooling down', 'error'
                        continue
                    self._bump(pid, model)
            try:
                _post(url, key, model, msgs, timeout, 20, True)
            except _Http as e:
                if e.code in (401, 403):
                    err, state = f'HTTP {e.code} (key refused)', 'broken' if key else 'error'
                    break
                if key and _is_spent(e.code, e.text):
                    until = e.reset or _next_midnight()
                    err, state = 'free requests for today used up', 'spent'
                    break
                if e.code in (402, 408, 429) or e.code >= 500:
                    with self.lock:
                        self._backoff((pid, model), e.retry_after, BACKOFF_BASE, BACKOFF_CAP)
                err, state = f'HTTP {e.code}' + (' (rate limited)' if e.code == 429 else
                                                 ' (proxy could not reach OpenRouter)' if not key and e.code == 502 else ''), 'error'
                continue
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                with self.lock:
                    self._backoff((pid, '*'), None, DOWN_BASE, DOWN_CAP)
                err, state = 'not reachable (' + type(e).__name__ + ')', 'unreachable'
                break
            except ValueError as e:
                err, state = 'invalid provider reply', 'error'
                continue
            err, state = None, 'ok'
            break
        with self.lock:
            self.checked[pid] = (time.time(), state)
            if state == 'ok':
                self.disabled.discard(pid)
                if self.spent.get(pid, 0) <= time.time():
                    self.spent.pop(pid, None)
                if not any(self.cool.get(k, (0, 0))[0] > time.time()
                           for k in ((pid, model), (pid, '*'))):
                    self.errors.pop(pid, None)
            else:
                self.errors[pid] = f'{model}: {err}'
                if state == 'spent':
                    self.spent[pid] = until
                    self._save_state()
                elif state == 'broken' and key:
                    self.disabled.add(pid)
        return {'passed': state == 'ok', 'state': state, 'model': model, 'ms': round((time.monotonic() - t) * 1000),
                'error': err, 'until': _iso(until) if until else None}

    def state_of(self, pid, key, now):
        """Honest one-word status for the UI: ok | spent | broken | cooling | unreachable | error | untested."""
        if pid in self.disabled:
            return 'broken'
        if self.spent.get(pid, 0) > now:
            return 'spent'
        if key and self.daily_limit and sum(n for (p, _), (day, n) in self.count.items()
                                            if p == pid and day == _today()) >= self.daily_limit:
            return 'spent'
        if any(u > now for (i, _), (u, _) in self.cool.items() if i == pid):
            err = self.errors.get(pid) or ''
            return 'unreachable' if not any(u > now for (i, m), (u, _) in self.cool.items() if i == pid and m != '*') \
                and 'HTTP' not in err else 'cooling'
        chk = self.checked.get(pid)
        if chk:
            return chk[1] if chk[1] in ('ok', 'error', 'unreachable') else 'ok'
        return 'error' if self.errors.get(pid) else 'untested'

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
                            'last_error': self.errors.get(pid), 'state': self.state_of(pid, key, now),
                            'spent_until': _iso(self.spent[pid]) if self.spent.get(pid, 0) > now else None,
                            'checked_at': _iso(self.checked[pid][0]) if pid in self.checked else None})
            return {'providers': out, 'models': list(self.models), 'daily_limit': self.daily_limit}


ORSLOT = Path.home() / '.config' / 'openrouter' / 'slots'


def orslot_keys():
    """Every key in the orslot pool (~/.config/openrouter/slots/*), read fresh so new slots show up on reload."""
    try:
        files = sorted(ORSLOT.iterdir())
    except OSError:
        return []
    out = []
    for f in files:
        try:
            k = f.read_text().strip()
        except OSError:
            continue
        if KEY_RX.fullmatch(k) and k not in out:
            out.append(k)
    return out


def settings(path=None):
    """Keys from env OPENROUTER_API_KEYS and data/openrouter.json; models and daily limit from the file."""
    env = [k.strip() for k in os.environ.get('OPENROUTER_API_KEYS', '').split(',') if k.strip()]
    if path is None and not os.environ.get('FL_NO_ORSLOT'):
        env += [k for k in orslot_keys() if k not in env]
    cfg = read_config(path)
    keys = env + [k.strip() for k in cfg.get('keys') or [] if isinstance(k, str) and k.strip()]
    models = cfg.get('models') if isinstance(cfg.get('models'), list) and cfg['models'] \
        and all(isinstance(m, str) and MODEL_RX.fullmatch(m) and m.endswith(':free') for m in cfg['models']) else None
    models = model_order(models, cfg.get('auto_models'))
    limit = cfg['daily_limit'] if isinstance(cfg.get('daily_limit'), int) and not isinstance(cfg['daily_limit'], bool) else FREE_DAILY
    limit = max(1, min(FREE_DAILY, limit))
    return {'keys': keys, 'models': models, 'daily_limit': limit, 'env_keys': env}


# ---------- free and stealth models (refreshed daily from the public model list; no key needed) ----------

def is_free(m):
    # A zero price in a stale catalog is not a durable free-tier guarantee.
    return str(m.get('id', '')).endswith(':free')


def is_stealth(m):
    """OpenRouter lists cloaked pre-release models under the `stealth/` namespace ('anonymous model' in the text)."""
    mid, text = str(m.get('id', '')), (str(m.get('description') or '') + ' ' + str(m.get('name') or '')).lower()
    return mid.startswith('stealth/') or 'stealth' in text or 'cloaked' in text or 'anonymous' in text and 'model' in text


def usable(m):
    arch = m.get('architecture') or {}
    outs, ins = arch.get('output_modalities') or ['text'], arch.get('input_modalities') or ['text']
    mid = str(m.get('id', ''))
    return 'text' in outs and outs == ['text'] and 'text' in ins and MODEL_RX.fullmatch(mid) is not None \
        and not mid.startswith('openrouter/') and 'safety' not in mid and 'guard' not in mid


def pick_models(data):
    """-> {'stealth': [...], 'free': [...]} newest first, from the /models JSON."""
    rows = [m for m in (data.get('data') if isinstance(data, dict) else None) or [] if isinstance(m, dict)]
    rows = sorted((m for m in rows if is_free(m) and usable(m)), key=lambda m: -(m.get('created') or 0))
    return {'stealth': [m['id'] for m in rows if is_stealth(m)], 'free': [m['id'] for m in rows if not is_stealth(m)]}


def model_order(chosen, auto):
    """Stealth models first (auto-added), then the chosen list (or the built-in fallback). Models that vanished from
    the live free list are dropped, but the built-in fallback always stays so there is a working order."""
    auto = auto if isinstance(auto, dict) else {}
    stealth = [m for m in auto.get('stealth') or [] if isinstance(m, str) and MODEL_RX.fullmatch(m) and m.endswith(':free')]
    live = set(stealth) | {m for m in auto.get('free') or [] if isinstance(m, str) and m.endswith(':free')}
    base = [m for m in (chosen or MODELS) if isinstance(m, str) and m.endswith(':free')]
    if live:
        base = [m for m in base if m in live] or [m for m in MODELS if m in live] or list(MODELS)
    return tuple(list(dict.fromkeys(stealth[:3] + base))[:MODELS_MAX])


def refresh_models(path=None, force=False, fetch=None):
    """Fetch the public model list at most once a day; store the free / stealth ids in data/openrouter.json
    ('auto_models') and reload the pool. Returns the stored record. Never raises on network trouble."""
    cfg = read_config(path)
    cur = cfg.get('auto_models') if isinstance(cfg.get('auto_models'), dict) else {}
    if not force and time.time() - (cur.get('checked') or 0) < MODELS_EVERY:
        return cur
    try:
        if fetch:
            data = fetch()
        else:
            req = urllib.request.Request(MODELS_URL, headers={'User-Agent': 'fortunate-leads', 'Accept': 'application/json'})
            with _opener(False).open(req, timeout=20) as r:
                data = json.loads(r.read(20_000_000))
        picked = pick_models(data)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
        rec = dict(cur, checked=time.time(), error=type(e).__name__)
    else:
        new = [m for m in picked['stealth'] if m not in (cur.get('stealth') or [])]
        rec = dict(picked, checked=time.time(), at=_iso(time.time()), error=None, new_stealth=new or cur.get('new_stealth') or [])
    cfg = read_config(path)   # re-read: keys may have changed meanwhile
    cfg['auto_models'] = rec
    write_config(cfg, path)
    if PROVIDERS[0] is not None:
        get().reload(path)
    return rec


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
                or not all(isinstance(m, str) and MODEL_RX.fullmatch(m.strip()) and m.strip().endswith(':free') for m in models):
            raise ValueError(f'models must be 1-{MODELS_MAX} free ids like vendor/model:free')
        cfg['models'] = list(dict.fromkeys(m.strip() for m in models))
    if daily_limit is not None:
        if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or not 1 <= daily_limit <= FREE_DAILY:
            raise ValueError(f'daily_limit must be a whole number 1-{FREE_DAILY}')
        cfg['daily_limit'] = daily_limit
    write_config(cfg, path)
    get().reload(path)


class _Http(Exception):
    def __init__(self, code, retry_after, text='', reset=None):
        super().__init__(code)
        self.code, self.retry_after, self.text, self.reset = code, retry_after, text or '', reset


def _reset(v):
    """X-RateLimit-Reset: epoch ms (OpenRouter) or s."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    x = x / 1000 if x > 1e11 else x
    return x if time.time() < x < time.time() + 2 * 86400 else None


def _retry_after(v):
    try:
        seconds = float(v)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(v).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(1.0, min(seconds, 24 * 3600))


def _post(url, key, model, messages, timeout, max_tokens, json_mode):
    if not isinstance(model, str) or not MODEL_RX.fullmatch(model) or not model.endswith(':free'):
        raise ValueError('only :free models may be requested')
    body = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.1,
            'reasoning': {'effort': 'low', 'exclude': True}}
    if json_mode:
        body['response_format'] = {'type': 'json_object'}
    headers = dict(HEADERS, **({'Authorization': 'Bearer ' + key} if key else {}))
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        with _opener(not key).open(req, timeout=timeout) as r:
            data = json.loads(r.read(REPLY_MAX))
    except urllib.error.HTTPError as exc:
        h = exc.headers
        code, ra = exc.code, _retry_after(h.get('Retry-After') if h else None)
        try:
            text = exc.read(4000).decode('utf-8', 'replace')
        except OSError:
            text = ''
        exc.close()
        raise _Http(code, ra, _error_text(text, key), _reset(h.get('X-RateLimit-Reset') if h else None)) from None
    if not isinstance(data, dict):
        raise ValueError(f'{model}: reply is not an object')
    if data.get('error'):
        err = data['error']
        code = err.get('code') if isinstance(err, dict) else None
        if isinstance(code, int) and code in (401, 402, 403, 429) or isinstance(code, int) and code >= 500:
            raise _Http(code, None, _error_text(json.dumps(data), key))
        raise ValueError(f'{model}: provider returned an error')
    returned = data.get('model')
    if returned != model:
        raise ValueError(f'{model}: provider did not confirm the requested model')
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f'{model}: empty reply')
    return content


def _error_text(text, key=None):
    """Extract diagnostic text with authentication values removed."""
    text = str(text or '')
    if key:
        text = text.replace(key, '[redacted]')
    text = re.sub(r'(?i)bearer\s+[^\s\"\']+', 'Bearer [redacted]', text)
    text = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', text)
    try:
        d = json.loads(text)
        e = d.get('error') if isinstance(d, dict) else None
        msg = e.get('message') if isinstance(e, dict) else e
        return str(msg or '')[:300]
    except ValueError:
        return (text or '')[:300]


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds')


PROVIDERS_LOCK = threading.Lock()
PROVIDERS = [None]   # the process-wide instance, created on first use


def get():
    with PROVIDERS_LOCK:
        if PROVIDERS[0] is None:
            PROVIDERS[0] = Providers.load()
        return PROVIDERS[0]
