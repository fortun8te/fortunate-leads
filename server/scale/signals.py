"""Classify Instagram responses into the codes the collector acts on.

Mirrors extension/lib/core.js `classify` so the server-side workers and the extension
react to the same signals (RESEARCH.md section 3).
"""
import json
import re

OK = 'ok'
RATE_LIMIT = 'rate_limit'      # 429 / "please wait a few minutes"
SOFT_BLOCK = 'soft_block'      # feedback_required / spam / empty page while count > 0
LOGIN_WALL = 'login_wall'      # logged-out request refused (401, require_login, login redirect)
CHALLENGE = 'challenge'        # checkpoint / challenge: an account needs a human
NOT_FOUND = 'not_found'
PRIVATE = 'private'
NETWORK = 'network'            # transport trouble, never counted as an Instagram limit
IP_BLOCKED = 'ip_blocked'      # proxy_address_is_blocked / 403 on a fresh exit
OTHER = 'other'

# Codes that mean "Instagram is pushing back on this identity (account or IP)".
PUSHBACK = frozenset({RATE_LIMIT, SOFT_BLOCK, LOGIN_WALL, IP_BLOCKED})
# Codes that must stop a logged-in account until a human looks at it.
HOLD = frozenset({CHALLENGE})

_PREFIX = re.compile(r'^\s*(for\s*\(;;\);|while\s*\(1\);|\)\]\}\',?)\s*')


def parse_body(text):
    t = _PREFIX.sub('', str(text or '').lstrip('\ufeff').strip())
    if not t[:1] in ('{', '['):
        return None
    try:
        j = json.loads(t)
    except ValueError:
        return None
    return j if isinstance(j, (dict, list)) else None


def _path(url):
    m = re.match(r'https?://[^/]+(/[^?#]*)', url or '')
    return m.group(1) if m else ''


def classify(status, text='', url='', json_body=None, kind='profile'):
    """-> (code, reason). `kind` is 'profile' (logged-out enrichment) or 'list'."""
    status = int(status or 0)
    j = json_body if json_body is not None else parse_body(text)
    raw = str(text or '')[:4000].lower()
    if isinstance(j, dict):
        msg = ' '.join(str(j.get(k) or '') for k in (
            'message', 'error_title', 'error_type', 'error_body', 'feedback_title')).lower()
        if isinstance(j.get('error'), str):
            msg += ' ' + j['error'].lower()
        if j.get('checkpoint_url'):
            msg += ' checkpoint_required'
    else:
        msg = raw
    p = _path(url)
    if p.startswith('/challenge/') or p.startswith('/accounts/suspended') or p.startswith('/accounts/disabled'):
        return CHALLENGE, 'challenge_redirect'
    if p.startswith('/accounts/login'):
        return LOGIN_WALL, 'login_redirect'
    if re.search(r'checkpoint_required|challenge_required|/challenge/', msg):
        return CHALLENGE, 'checkpoint'
    if 'proxy_address_is_blocked' in msg or 'ip_block' in msg:
        return IP_BLOCKED, 'proxy_address_is_blocked'
    if status == 429:
        return RATE_LIMIT, 'http_429'
    if re.search(r'please wait a few minutes|wait a few minutes before|rate.?limit|too many requests', msg):
        return RATE_LIMIT, 'please_wait'
    if 'feedback_required' in msg or (isinstance(j, dict) and j.get('spam') is True):
        return SOFT_BLOCK, 'feedback_required'
    if status == 401 or 'login_required' in msg or (isinstance(j, dict) and j.get('require_login')):
        return LOGIN_WALL, 'require_login'
    if status == 404 or (isinstance(j, dict) and re.search(r'user not found|not.found', msg)):
        return NOT_FOUND, 'not_found'
    if status == 403:
        return IP_BLOCKED, 'http_403'
    if not status:
        return NETWORK, 'transport'
    if not 200 <= status < 300:
        return OTHER, 'http_%d' % status
    if j is None:
        if re.search(r'login|password', raw):
            return LOGIN_WALL, 'html_login'
        return OTHER, 'not_json'
    if isinstance(j, dict) and j.get('status') not in (None, 'ok'):
        return OTHER, 'status_' + str(j.get('status'))[:20]
    if kind == 'list':
        return list_check(j)
    return OK, ''


def users_of(j):
    if not isinstance(j, dict):
        return None
    if isinstance(j.get('users'), list):
        return j['users']
    d = j.get('data')
    if isinstance(d, dict) and isinstance(d.get('users'), list):
        return d['users']
    return None


def list_check(j):
    users = users_of(j)
    if users is None:
        return OTHER, 'no_users_field'
    if j.get('should_limit_list_of_followers'):
        return OK, 'limited'
    more = j.get('has_more')
    cursor = j.get('next_max_id')
    if users:
        if more is True and not cursor:
            return OTHER, 'missing_cursor'
        return OK, ''
    if more is True or (more is not False and cursor):
        return SOFT_BLOCK, 'empty_page_with_more'
    return OK, 'end'
