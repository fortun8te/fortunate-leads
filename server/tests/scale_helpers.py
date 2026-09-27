import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from scale.transport import Response  # noqa: E402


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeTransport:
    """Scripted responses: a function (egress, url) -> Response, plus a request log."""

    def __init__(self, fn):
        self.fn, self.log = fn, []

    def send(self, egress, method, url, headers=None, body=None, timeout=20):
        self.log.append((egress.id if egress else None, getattr(egress, 'username', None), url))
        r = self.fn(egress, url)
        if isinstance(r, Exception):
            raise r
        return r


def resp(status=200, body=None, url='https://i.instagram.com/x', headers=None):
    text = body if isinstance(body, str) else json.dumps(body if body is not None else {})
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()}, text, url)


def profile_json(handle, bio='Founder of @brand', pk='123'):
    return {'data': {'user': {'id': pk, 'username': handle, 'full_name': handle.title(), 'biography': bio,
                              'external_url': 'https://brand.com', 'edge_followed_by': {'count': 1234},
                              'edge_follow': {'count': 56}, 'edge_owner_to_timeline_media': {'count': 7},
                              'is_private': False, 'is_verified': False, 'is_business_account': True,
                              'category_name': 'Brand', 'profile_pic_url': 'https://scontent.cdninstagram.com/p.jpg'}},
            'status': 'ok'}


class ListSink:
    def __init__(self):
        self.rows = []

    def write(self, p):
        self.rows.append(p)
