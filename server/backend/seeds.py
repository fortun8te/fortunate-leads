"""HTTP side of the seed workflow (see seeds.py and docs/ACCOUNTS_UX.md).

GET  /api/seeds         list with filters; ETag revision so an unchanged list costs a 304
POST /api/seeds/check   read pasted text, change nothing
POST /api/seeds/batch   add pasted text; atomic; a repeated request_id returns the first answer
POST /api/seeds/act     pause | resume | remove | restore | next | directions on many seeds
POST /api/seeds/undo    take back one add by its request_id
"""
import json

from datetime import datetime, timezone

import accounts
import collection_progress
import db
import map_view
import seeds

from .common import Bad, NotFound, qint


class SeedService:

    def __init__(self, config, collection):
        self.config = config
        self.collection = collection

    def _scope(self, conn):
        return 'following' if db.get_setting(conn, 'follower_lists') is False else 'both'

    def api_list(self, conn, q, b):
        first = lambda key, default='': (q.get(key) or [default])[0]
        coverage, _ = self.collection.list_coverage(conn)
        now = datetime.now(timezone.utc)
        summary = collection_progress.summary(conn, coverage, accounts.listing(conn, now, include_lists=False), now)
        queue = {k: summary.get(k) for k in ('pending', 'finished', 'partial', 'limited', 'needs_review', 'unknown_lists', 'status', 'message', 'eta')}
        try:
            payload = seeds.listing(conn, coverage, q=first('q'), status=first('status', 'all'), sort=first('sort', 'newest'),
                                    limit=qint(q, 'limit') or 200, offset=qint(q, 'offset') or 0, scope=self._scope(conn), queue=queue)
        except ValueError as exc:
            raise Bad(str(exc)) from None
        return map_view.Raw(json.dumps(payload).encode(), etag='"' + payload['revision'] + '"',
                            headers={'Content-Type': 'application/json'})

    def api_check(self, conn, q, b):
        text = b.get('text', b.get('handles'))
        if not isinstance(text, (str, list)):
            raise Bad('text must be pasted text or a list of text')
        dirs = b.get('directions', list(seeds.DIRECTIONS))
        if not isinstance(dirs, list) or not dirs or any(d not in seeds.DIRECTIONS for d in dirs):
            raise Bad('directions must be followers, following or both')
        tokens = seeds.split_tokens(text)
        if len(tokens) > seeds.MAX_BATCH:
            raise Bad(f'Check up to {seeds.MAX_BATCH} profiles at a time.')
        items = seeds.classify(conn, tokens, dirs)
        conn.rollback()
        return {'items': [seeds._public(dict(i, result=None)) for i in items], 'summary': seeds.summarize(items)}

    def api_batch(self, conn, q, b):
        text = b.get('text', b.get('handles'))
        try:
            out = seeds.add_batch(conn, text, b.get('directions', list(seeds.DIRECTIONS)), b.get('request_id'))
        except ValueError as exc:
            raise Bad(str(exc)) from None
        return out

    def api_act(self, conn, q, b):
        try:
            return seeds.act(conn, b.get('action'), b.get('handles'), b.get('directions'))
        except ValueError as exc:
            raise Bad(str(exc)) from None

    def api_undo(self, conn, q, b):
        try:
            return seeds.undo_batch(conn, b.get('request_id'))
        except LookupError as exc:
            raise NotFound(str(exc)) from None
