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
    def __init__(self, code):
        self.code = code


def parse_page(payload, task):
    """Strict validated form of instagrapi's _private_graphql_root/users parser."""
    if not isinstance(payload, dict):
        raise PageWarning('invalid_page')
    # Scan nested GraphQL errors too. Never return server strings or credentials.
    warning_text = json.dumps(payload).lower()
    if any(word in warning_text for word in ('challenge_required', 'checkpoint_required', 'two_factor_required')) or payload.get('challenge'):
        raise PageWarning('challenge')
    if any(word in warning_text for word in ('login_required', 'require_login')):
        raise PageWarning('login')
    if payload.get('spam') is True or any(word in warning_text for word in ('feedback_required', 'please wait', 'sentry_block', 'rate_limit', 'temporarily blocked')):
        raise PageWarning('soft_block')
    if payload.get('errors') or payload.get('status') == 'fail':
        raise PageWarning('invalid_page')
    if task['route'] == 'mobile_graphql':
        root_name = 'xdt_api__v1__friendships__' + task['direction']
        data = payload.get('data') or payload
        if not isinstance(data, dict):
            raise PageWarning('invalid_page')
        matches = [value for key, value in data.items() if root_name in str(key) and isinstance(value, dict)]
        if len(matches) != 1:
            raise PageWarning('invalid_page')
        payload = matches[0]
    elif payload.get('status') != 'ok':
        raise PageWarning('invalid_page')
    if payload.get('user_id') is not None and str(payload['user_id']) != str(task['target_id']):
        raise PageWarning('invalid_page')
    if not isinstance(payload.get('users'), list):
        raise PageWarning('invalid_page')
    rows = []
    for user in payload['users']:
        if not isinstance(user, dict) or not ID.fullmatch(str(user.get('pk') or user.get('id') or '')) or not re.fullmatch(r'[a-zA-Z0-9._]{1,30}', str(user.get('username') or '')):
            raise PageWarning('invalid_page')
        if user.get('pk') is not None and user.get('id') is not None and str(user['pk']) != str(user['id']):
            raise PageWarning('invalid_page')
        row = {'ig_id': str(user.get('pk') or user['id']), 'handle': user['username']}
        rows.append(row)
    if 'next_max_id' not in payload and payload.get('has_more') is not False:
        raise PageWarning('pagination')
    cursor = payload.get('next_max_id')
    if cursor == '':
        cursor = None
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096 or cursor == task.get('cursor')):
        raise PageWarning('pagination')
    more = payload.get('has_more')
    if more is not None and type(more) is not bool:
        raise PageWarning('pagination')
    if more is True and not cursor or more is False and cursor or cursor and not rows:
        raise PageWarning('pagination')
    if payload.get('should_limit_list_of_followers') is True:
        raise PageWarning('soft_block')
    if 'should_limit_list_of_followers' in payload and type(payload['should_limit_list_of_followers']) is not bool:
        raise PageWarning('pagination')
    # Maintained private GQL uses next_max_id as its continuation criterion.
    return {'rows': rows, 'next_cursor': cursor, 'has_more': bool(cursor) if more is None else more,
            'returned_count': len(rows)}


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
              'returned_count': 0, 'device_fingerprint': fingerprint}
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
        if response.status_code == 429:
            raise PageWarning('rate_limit')
        if response.status_code in (401, 403):
            raise PageWarning('login')
        if 300 <= response.status_code < 400:
            raise PageWarning('soft_block')
        if response.status_code != 200:
            raise PageWarning('http_error')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise PageWarning('soft_block') from None
        result.update(parse_page(payload, task), status='ok', terminal_warning=False)
    except PageWarning as exc:
        result['status'] = exc.code
    except Exception:
        # Read/connection failures retain the shared permit; no retry or fallback.
        if response is None:
            result['actual_http_requests'] = None
        if result['status'] not in ('rate_limit', 'login', 'soft_block'):
            result['status'] = 'transport_uncertain'
        result['transport_completed'] = False
    finally:
        result['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
        if response is not None:
            response.close()
    return result
