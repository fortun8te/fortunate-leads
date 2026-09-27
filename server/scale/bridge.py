"""Glue between the existing fortunate-leads server and the scale pipeline.

Wiring (small, opt-in; see docs/scaling-plan.md "Integration"):
  1. server.py, after a list page is ingested (the /api/ext/list-page handler):
         bridge.on_list_page(users, seed, direction)
     -> every returned user lands in the global seen-cache; new ones queue for logged-out enrichment.
  2. once at start (and daily): bridge.import_known_from_leads(conn)
     -> people that already have a bio are never fetched again.
  3. run the enrichment worker as its own process (own venv with curl_cffi), writing back through
     FortunateDBSink:  python3 server/scale/cli.py enrich --from-queue --db data/leads.sqlite --tor 4x40
The seen-cache lives in data/scale.sqlite (gitignored with data/).
"""
import os
import threading

from scale.dedupe import SeenStore

_store = None
_lock = threading.Lock()


def store(path=None):
    global _store
    with _lock:
        if _store is None:
            path = path or os.environ.get('FL_SCALE_DB') or os.path.join(
                os.path.dirname(__file__), '..', '..', 'data', 'scale.sqlite')
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            _store = SeenStore(path)
        return _store


def on_list_page(users, seed, direction, st=None):
    """users: the contract list-page users [{ig_id, handle, name, is_private, is_verified}]."""
    st = st or store()
    return st.add_discovered(users or [], source='%s/%s' % (str(seed).lower(), direction))


def import_known_from_leads(conn, st=None):
    st = st or store()
    rows = conn.execute("SELECT handle FROM people WHERE bio_at IS NOT NULL AND instr(handle,'~')=0").fetchall()
    st.import_known([r[0] for r in rows])
    return len(rows)


def queue_unread_from_leads(conn, st=None, limit=100000):
    """Seed the enrichment queue with people the lists already found but nobody has read yet."""
    st = st or store()
    rows = conn.execute("SELECT handle, ig_id, is_private, is_verified FROM people WHERE bio_at IS NULL "
                        "AND instr(handle,'~')=0 AND handle NOT IN (SELECT handle FROM seeds) LIMIT ?",
                        (int(limit),)).fetchall()
    return len(st.add_discovered([{'handle': r[0], 'ig_id': r[1], 'is_private': r[2], 'is_verified': r[3]}
                                  for r in rows], source='leads_db'))
