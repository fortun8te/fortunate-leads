"""One bounded, local-only K2 inference lane shared by notes and profile checks."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import resource_budget

MODEL = 'k2-horizon-3.7B-q4km'
MODEL_DIGEST = '07773aed93890d4f5d08b421430000d5c8d73cefc74fbdc32e67d820f23207b2'
URL = 'http://127.0.0.1:11436'
SERVICE_ROOT = Path.home() / 'Library/Application Support/Fortunate Leads/k2'
_lock = threading.Lock()
_retry_at = 0.0


class Unavailable(RuntimeError):
    pass


class Busy(Unavailable):
    def __init__(self, message, retry_after=1):
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Unavailable('Local model redirected the request')


def _request(path, payload=None, timeout=3):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(URL + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError('Local model response too large')
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise Unavailable('K2 is unavailable. Start local models in Accounts.') from exc


def ready():
    try:
        health = _request('/health')
        if not isinstance(health, dict) or health.get('status') != 'ok':
            return False
        models = _request('/v1/models').get('data', [])
        return any(isinstance(m, dict) and m.get('id') == MODEL for m in models)
    except (Unavailable, ValueError, TypeError, AttributeError):
        return False


def status():
    return {'model': MODEL, 'ready': ready(), 'busy': _lock.locked(),
            'retry_in': max(0, round(_retry_at - time.monotonic())),
            'resources': resource_budget.state()}


def _record_activity():
    try:
        if SERVICE_ROOT.is_dir():
            (SERVICE_ROOT / 'last_activity').write_text(str(time.time()))
    except OSError:
        pass


def maintain_service(paused=False):
    """Cheap scheduler hook: stop our K2 on pause/pressure or after 5 idle minutes.

    Does not start models. Health polling does not extend the idle timer.
    Returns a reason when a stop was requested; surfaces errors to the scheduler.
    """
    if not (SERVICE_ROOT / 'pid').exists():
        return {'stopped': False, 'reason': ''}
    budget = resource_budget.state()
    reason = 'paused' if paused else ('memory_or_heat' if budget.get('recovering') or budget.get('error') else '')
    if not reason:
        try:
            idle = max(0, time.time() - float((SERVICE_ROOT / 'last_activity').read_text()))
        except (OSError, ValueError):
            idle = 0
        if idle >= 300 and not _lock.locked():
            reason = 'idle'
    if not reason:
        return {'stopped': False, 'reason': ''}
    idle_lock = reason == 'idle'
    if idle_lock and not _lock.acquire(blocking=False):
        return {'stopped': False, 'reason': ''}
    try:
        if idle_lock:
            # An inference may have finished after the first timestamp read.
            try:
                if time.time() - float((SERVICE_ROOT / 'last_activity').read_text()) < 300:
                    return {'stopped': False, 'reason': ''}
            except (OSError, ValueError):
                return {'stopped': False, 'reason': ''}
        script = Path(__file__).resolve().parents[1] / 'ops/k2-service.py'
        result = subprocess.run([sys.executable, str(script), 'stop'], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise Unavailable((result.stderr or 'Could not stop the local K2 service').strip()[:200])
        return {'stopped': True, 'reason': reason}
    finally:
        if idle_lock:
            _lock.release()


def complete_json(system, user, schema, max_tokens=900, timeout=45):
    try:
        with resource_budget.lease('k2'):
            _record_activity()
            try:
                return _complete_json(system, user, schema, max_tokens, timeout)
            finally:
                _record_activity()
    except resource_budget.Deferred as exc:
        raise Busy(str(exc), exc.retry_after) from exc


def _complete_json(system, user, schema, max_tokens=900, timeout=45):
    """Return JSON, leaving domain validation to the caller. No hidden retries."""
    global _retry_at
    if not isinstance(system, str) or not isinstance(user, str) or len(system) + len(user) > 14000:
        raise ValueError('Local input is too large')
    if not isinstance(schema, dict) or not 1 <= max_tokens <= 1600 or not 1 <= timeout <= 90:
        raise ValueError('Invalid local inference limits')
    if time.monotonic() < _retry_at:
        raise Busy('K2 is recovering; this check will retry later', _retry_at - time.monotonic())
    if not _lock.acquire(blocking=False):
        raise Busy('K2 is checking another profile or note')
    try:
        if not ready():
            raise Unavailable('The verified K2 model is not ready')
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        rendered = _request('/apply-template', {'messages': messages})
        if not isinstance(rendered, dict) or not isinstance(rendered.get('prompt'), str):
            raise ValueError('Invalid local prompt template')
        tokenized = _request('/tokenize', {'content': rendered['prompt'], 'add_special': True})
        tokens = tokenized.get('tokens') if isinstance(tokenized, dict) else None
        if not isinstance(tokens, list) or len(tokens) + max_tokens + 64 > 4096:
            raise ValueError('Local input exceeds the model context budget')
        response = _request('/v1/chat/completions', {
            'model': MODEL, 'messages': messages,
            'temperature': 0, 'max_tokens': max_tokens, 'stream': False,
            'reasoning_effort': 'low',
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'local_result', 'strict': True, 'schema': schema}}}, timeout=timeout)
        choices = response.get('choices') if isinstance(response, dict) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError('Invalid local completion')
        choice = choices[0]
        if choice.get('finish_reason') != 'stop':
            raise ValueError('Local completion was truncated')
        message = choice.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ValueError('Missing local JSON response')
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError('Local result must be an object')
        return result
    except Unavailable:
        _retry_at = time.monotonic() + 30
        raise
    finally:
        _lock.release()
