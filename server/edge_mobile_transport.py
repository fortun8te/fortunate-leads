"""Benchmark-only, one Session.send per page; never logs in or follows redirects."""
import hashlib
import json
import re
import time
from types import SimpleNamespace

from mobile_collector import ID, Stopped, session_identity

MAX_BYTES = 8_000_000


def device_fingerprint(client):
    fields = ('phone_id', 'uuid', 'android_device_id', 'advertising_id')
    identity = {key: getattr(client, key, None) for key in fields}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise Stopped('Saved device identity is incomplete')
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def session_fingerprint(client):
    authorization = client.authorization
    if not isinstance(authorization, str) or not authorization:
        raise Stopped('Saved session authorization is missing')
    return hashlib.sha256(authorization.encode()).hexdigest()


def construct_request(client, task, viewer_id):
    """Use pinned library's pure GraphQL builder, never its network convenience calls."""
    import requests
    if session_identity(client) != viewer_id or task.get('viewer_id') != viewer_id:
        raise Stopped('Benchmark session identity mismatch')
    target, direction = str(task.get('target_id') or ''), task.get('direction')
    route, count, cursor = task.get('route'), task.get('page_size'), task.get('cursor')
    if not ID.fullmatch(target) or direction not in ('following', 'followers'):
        raise Stopped('Invalid benchmark target')
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 4096):
        raise Stopped('Invalid benchmark cursor')
    headers = dict(client.base_headers)
    if client.authorization:
        headers['Authorization'] = client.authorization
    if route == 'mobile_rest':
        if type(count) is not int or count not in (25, 50, 100, 200):
            raise Stopped('Unsupported mobile REST page size')
        params = {'count': count, 'rank_token': client.rank_token}
        if direction == 'followers':
            params.update(search_surface='follow_list_page', query='', enable_groups='true')
        if cursor:
            params['max_id'] = cursor
        return requests.Request('GET', 'https://i.instagram.com/api/v1/friendships/' + target + '/' + direction + '/', headers=headers, params=params)
    if route != 'mobile_graphql' or count is not None:
        raise Stopped('Private GraphQL supports native page size only')
    from instagrapi.mixins.user import UserMixin
    from instagrapi.mixins.graphql import PRIVATE_GRAPHQL_QUERY_URL
    capture = SimpleNamespace(private_graphql_query_request=lambda **kwargs: kwargs)
    builder = getattr(UserMixin, 'private_graphql_' + direction + '_list')
    spec = builder(capture, target, client.rank_token, max_id=cursor, priority='u=3, i')
    data = {'method': 'post', 'pretty': 'false', 'format': 'json', 'server_timestamps': 'true',
            'locale': 'user', 'fb_api_req_friendly_name': spec['friendly_name'],
            'enable_canonical_naming': 'true', 'enable_canonical_variable_overrides': 'true',
            'enable_canonical_naming_ambiguous_type_prefixing': 'true',
            'variables': json.dumps(spec['variables'], separators=(',', ':')), 'client_doc_id': spec['client_doc_id']}
    headers.update({'X-FB-Friendly-Name': spec['friendly_name'], 'X-Root-Field-Name': spec['root_field_name'],
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Client-Doc-Id': spec['client_doc_id'], 'Priority': spec['priority']})
    headers.update(spec['extra_headers'])
    return requests.Request('POST', PRIVATE_GRAPHQL_QUERY_URL, headers=headers, data=data)


class PageWarning(Exception):
    def __init__(self, code, reason=None, diagnostics=None):
        self.code = code
        self.reason = reason or code
        self.diagnostics = diagnostics or {}


def _page_payload(payload, task):
    if not isinstance(payload, dict):
        return None
    if task['route'] != 'mobile_graphql':
        return payload
    data = payload.get('data') or payload
    if not isinstance(data, dict):
        return None
    root = 'xdt_api__v1__friendships__' + task['direction']
    matches = [value for key, value in data.items() if root in str(key) and isinstance(value, dict)]
    return matches[0] if len(matches) == 1 else None


def page_diagnostics(payload, task):
    """Only counts and fixed reason labels escape; no upstream messages or user text."""
    page = _page_payload(payload, task)
    raw_count = len(page['users']) if isinstance(page, dict) and isinstance(page.get('users'), list) else None
    flags = set()
    # Traverse error metadata, excluding user/profile content to avoid false positives.
    def inspect(value):
        if isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == 'users':
                    continue
                if key in ('challenge', 'checkpoint_url', 'two_factor_required') and item:
                    flags.add('challenge_required')
                if key in ('require_login', 'login_required') and item is True:
                    flags.add('login_required')
                if key == 'spam' and item is True:
                    flags.add('spam_flag')
                if key in ('message', 'error_type', 'error', 'errors'):
                    text = json.dumps(item).lower()
                    for token, flag in (('challenge', 'challenge_required'), ('checkpoint', 'challenge_required'),
                            ('two_factor', 'challenge_required'), ('login_required', 'login_required'),
                            ('feedback_required', 'feedback_required'), ('please wait', 'please_wait'),
                            ('sentry_block', 'sentry_block'), ('rate_limit', 'rate_limit_message'),
                            ('temporarily blocked', 'temporarily_blocked')):
                        if token in text:
                            flags.add(flag)
                if isinstance(item, (dict, list)):
                    inspect(item)
    inspect(payload)
    limited = isinstance(page, dict) and page.get('should_limit_list_of_followers') is True
    if limited:
        flags.add('list_cap_flag')
    return {'raw_returned_count': raw_count, 'returned_count': raw_count,
            'reason_flags': sorted(flags), 'target_limited': limited}


def parse_page(payload, task):
    diagnostics = page_diagnostics(payload, task)
    def fail(code, reason):
        raise PageWarning(code, reason, diagnostics)
    if not isinstance(payload, dict):
        fail('invalid_page', 'non_object_json')
    flags = set(diagnostics['reason_flags'])
    if 'challenge_required' in flags:
        fail('challenge', 'challenge_required')
    if 'login_required' in flags:
        fail('login', 'login_required')
    provider = flags & {'spam_flag', 'feedback_required', 'please_wait', 'sentry_block', 'rate_limit_message', 'temporarily_blocked'}
    if provider:
        fail('soft_block', sorted(provider)[0])
    if payload.get('errors') or payload.get('status') == 'fail':
        fail('invalid_page', 'upstream_error')
    page = _page_payload(payload, task)
    if page is None:
        fail('invalid_page', 'missing_or_ambiguous_graphql_root')
    if task['route'] != 'mobile_graphql' and page.get('status') != 'ok':
        fail('invalid_page', 'unexpected_status')
    if page.get('user_id') is not None and str(page['user_id']) != str(task['target_id']):
        fail('invalid_page', 'target_id_mismatch')
    if not isinstance(page.get('users'), list):
        fail('invalid_page', 'missing_users_array')
    rows = []
    for user in page['users']:
        if not isinstance(user, dict) or not ID.fullmatch(str(user.get('pk') or user.get('id') or '')) or not re.fullmatch(r'[a-zA-Z0-9._]{1,30}', str(user.get('username') or '')):
            fail('invalid_page', 'malformed_user')
        if user.get('pk') is not None and user.get('id') is not None and str(user['pk']) != str(user['id']):
            fail('invalid_page', 'user_id_mismatch')
        rows.append({'ig_id': str(user.get('pk') or user['id']), 'handle': user['username']})
    if 'should_limit_list_of_followers' in page and type(page['should_limit_list_of_followers']) is not bool:
        fail('pagination', 'invalid_list_cap_flag')
    limited = diagnostics['target_limited']
    if not limited and 'next_max_id' not in page and page.get('has_more') is not False:
        fail('pagination', 'missing_cursor')
    cursor = page.get('next_max_id')
    if cursor == '':
        cursor = None
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        fail('pagination', 'invalid_cursor')
    if cursor is not None and cursor == task.get('cursor'):
        fail('pagination', 'repeated_cursor')
    more = page.get('has_more')
    if more is not None and type(more) is not bool:
        fail('pagination', 'invalid_has_more')
    if more is True and not cursor or more is False and cursor or cursor and not rows:
        fail('pagination', 'contradictory_pagination')
    return dict(diagnostics, rows=rows, next_cursor=cursor, has_more=bool(cursor) if more is None else more,
                reported_has_more=more, status='target_cap' if limited else 'ok',
                terminal_warning='target_cap' if limited else None, failure_reason='list_cap_flag' if limited else None)


def one_request(client, task, viewer_id):
    import requests
    from requests.adapters import HTTPAdapter
    request = construct_request(client, task, viewer_id)
    fingerprint = device_fingerprint(client)
    session = client.private
    if not isinstance(session, requests.Session):
        raise Stopped('Benchmark requires the pinned requests transport')
    session.trust_env = False
    session.proxies.clear()
    # Fresh adapters exclude saved custom adapters and urllib3 automatic retries.
    session.mount('https://', HTTPAdapter(max_retries=0))
    session.mount('http://', HTTPAdapter(max_retries=0))
    prepared = session.prepare_request(request)
    prepared.hooks = {'response': []}  # Exclude auth/challenge response callbacks.
    result = {'rows': [], 'next_cursor': None, 'has_more': None, 'status': 'transport_uncertain',
              'terminal_warning': True, 'actual_http_requests': 0, 'http_status': 0,
              'transport_completed': False, 'requested_count': task.get('page_size'),
              'returned_count': None, 'raw_returned_count': None, 'reason_flags': [],
              'failure_reason': None, 'device_fingerprint': fingerprint}
    response = None
    started = time.monotonic()
    try:
        result['actual_http_requests'] = 1
        response = requests.Session.send(session, prepared, timeout=(5, 20), allow_redirects=False,
                                         stream=True, proxies={}, verify=True)
        result['http_status'] = response.status_code
        result['retry_after'] = response.headers.get('Retry-After')
        if response.status_code == 429:
            result['status'] = 'rate_limit'
        elif response.status_code in (401, 403):
            result['status'] = 'login'
        elif 300 <= response.status_code < 400:
            result['status'] = 'soft_block'
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            raw.extend(chunk)
            if time.monotonic() - started > 30:
                raise PageWarning('transport_uncertain')
            if len(raw) > MAX_BYTES:
                raise PageWarning('response_too_large')
        result['transport_completed'] = True
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = None
        result.update(page_diagnostics(payload, task))
        if response.status_code == 429:
            raise PageWarning('rate_limit', 'http_429')
        if response.status_code in (401, 403):
            raise PageWarning('login', 'http_auth')
        if 300 <= response.status_code < 400:
            raise PageWarning('soft_block', 'http_redirect')
        if response.status_code != 200:
            raise PageWarning('http_error', 'http_non_200')
        if payload is None:
            raise PageWarning('soft_block', 'non_json_response')
        result.update(parse_page(payload, task))
    except PageWarning as exc:
        result.update(exc.diagnostics)
        result['status'] = exc.code
        result['failure_reason'] = exc.reason
        result['terminal_warning'] = exc.reason
        result['reason_flags'] = sorted(set(result.get('reason_flags', [])) | {exc.reason})
    except Exception:
        # Read/connection failures retain the shared permit; no retry or fallback.
        if response is None:
            result['actual_http_requests'] = None
        if result['status'] not in ('rate_limit', 'login', 'soft_block'):
            result['status'] = 'transport_uncertain'
        result['transport_completed'] = False
        result['failure_reason'] = 'connection_failure' if response is None else 'response_read_failure'
        result['reason_flags'] = sorted(set(result['reason_flags']) | {result['failure_reason']})
    finally:
        result['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
        if response is not None:
            response.close()
    return result
