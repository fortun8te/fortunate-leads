"""Bulk stage on Michael's SuperGrok plan: Grok 4.7 over xAI's Responses API, signed in through Hermes.

Hermes owns the SuperGrok login (xai-oauth) and refreshes it; this module asks Hermes for a current token and
calls the API itself, so there is no agent loop, no per-call process and no agent system prompt:
- the rubric is a fixed prefix sent with x-grok-conv-id, so xAI serves it from its prompt cache after the first call
- people go in as a compact table and come back as short coded rows under a strict JSON schema
- reasoning effort 'low' (the lowest Grok 4.7 accepts)
Every call's tokens are appended to data/grok_usage.jsonl for the 7-day usage view."""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERMES = Path.home() / '.hermes-me'
PYTHON = HERMES / 'hermes-agent' / 'venv' / 'bin' / 'python'
MODEL = 'grok-4.7'
EFFORT = 'low'
BATCH = 80          # people per call; the fixed prefix is cached, so larger batches mostly add output
TIMEOUT = 300
LOG = Path(__file__).resolve().parent.parent / 'data' / 'grok_usage.jsonl'
CREDS_TTL = 20 * 60
_CREDS_JS = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
             "from hermes_cli.auth_xai import resolve_xai_oauth_runtime_credentials as r;"
             "c=r();print(json.dumps({'base_url':c['base_url'],'api_key':c['api_key']}))")
_lock = threading.Lock()
_creds = {'at': 0, 'value': None}


class Failed(Exception):
    pass


def available():
    return not os.environ.get('FL_NO_ORSLOT') and PYTHON.exists()


def creds(force=False):
    """{'base_url', 'api_key'} from Hermes' auth store (Hermes refreshes it when it is close to expiry)."""
    with _lock:
        if not force and _creds['value'] and time.time() - _creds['at'] < CREDS_TTL:
            return _creds['value']
        try:
            out = subprocess.run([str(PYTHON), '-c', _CREDS_JS, str(HERMES / 'hermes-agent')], capture_output=True, text=True,
                                 timeout=40, stdin=subprocess.DEVNULL, env=dict(os.environ, HERMES_HOME=str(HERMES)))
            value = json.loads(out.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            raise Failed('no SuperGrok login in Hermes (run: hermesme auth, xAI)') from None
        if not value.get('api_key'):
            raise Failed('no SuperGrok login in Hermes (run: hermesme auth, xAI)')
        _creds.update(at=time.time(), value=value)
        return value


def call(system, user, schema, cache_key, timeout=TIMEOUT):
    """One Responses API call -> (parsed JSON, usage dict). Raises Failed."""
    body = {'model': MODEL, 'reasoning': {'effort': EFFORT}, 'store': False,
            'input': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'text': {'format': {'type': 'json_schema', 'name': 'rows', 'schema': schema, 'strict': True}}}
    for attempt in (0, 1):
        c = creds(force=attempt == 1)
        req = urllib.request.Request(c['base_url'].rstrip('/') + '/responses', data=json.dumps(body).encode(), method='POST',
                                     headers={'Authorization': 'Bearer ' + c['api_key'], 'Content-Type': 'application/json',
                                              'x-grok-conv-id': cache_key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            raise Failed(f'HTTP {e.code}: {e.read()[:200]!r}') from None
        except (OSError, ValueError) as e:
            raise Failed(str(e)[:200]) from None
    text = ''.join(part.get('text', '') for item in data.get('output') or [] if item.get('type') == 'message'
                   for part in item.get('content') or [] if part.get('type') == 'output_text')
    u = data.get('usage') or {}
    usage = {'at': time.time(), 'in': u.get('input_tokens') or 0, 'out': u.get('output_tokens') or 0,
             'cached': (u.get('input_tokens_details') or {}).get('cached_tokens') or 0,
             'reasoning': (u.get('output_tokens_details') or {}).get('reasoning_tokens') or 0}
    _log(usage)
    try:
        return json.loads(text), usage
    except ValueError:
        raise Failed('reply was not JSON') from None


def _log(rec):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with _lock, LOG.open('a') as f:
            f.write(json.dumps(rec) + '\n')
    except OSError:
        pass


def usage(days=7):
    """{'calls', 'tokens_in', 'tokens_cached', 'tokens_out', 'today'} over the last `days`."""
    since, midnight = time.time() - days * 86400, time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    out = {'calls': 0, 'tokens_in': 0, 'tokens_cached': 0, 'tokens_out': 0, 'today': 0}
    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('at', 0) >= since:
            out['calls'] += 1
            out['tokens_in'] += r.get('in', 0)
            out['tokens_cached'] += r.get('cached', 0)
            out['tokens_out'] += r.get('out', 0)
            out['today'] += r.get('at', 0) >= midnight
    return out
