"""Handles storage ownership; writes remain in the caller transaction."""

import re
from urllib.parse import unquote, urlsplit


_HANDLE = re.compile(r'[A-Za-z0-9._]{1,30}\Z')


_PARKED = re.compile(r'[A-Za-z0-9._]{1,30}~[A-Za-z0-9~]+\Z')


_IG_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com', 'instagr.am', 'www.instagr.am'}


_RESERVED = {'p', 'reel', 'reels', 'tv', 'stories', 'explore', 'accounts', 'direct',
             'about', 'developer', '_u'}


def norm_handle(h):
    """Canonical handle, or empty text for an invalid external identity.

    Parked identities are retained for internal DB maintenance; queue_list
    applies the narrower external contract before creating work.
    """
    if not isinstance(h, str):
        return ''
    s = h.strip()
    if not s:
        return ''
    if s.startswith('@'):
        s = s[1:]
    if _PARKED.fullmatch(s):
        return s.lower()
    if s.lower() in _IG_HOSTS:
        return ''
    if '/' in s or '?' in s or '#' in s or ':' in s:
        url = s if s.lower().startswith(('http://', 'https://')) else 'https://' + s
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (parsed.scheme.lower() not in ('http', 'https') or host not in _IG_HOSTS
                    or parsed.username is not None or parsed.password is not None or parsed.port is not None):
                return ''
        except ValueError:
            return ''
        parts = parsed.path.split('/')
        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2]):
            return ''
        s = unquote(parts[1])
    if not _HANDLE.fullmatch(s) or s.lower() in _RESERVED:
        return ''
    return s.lower()


def normalize_ig_id(value):
    """Blank external IDs are missing evidence, never a replacement identity."""
    return (str(value).strip() or None) if value is not None else None
