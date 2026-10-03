"""Bounded discovery from saved evidence, using the normal collector queue.

Reading suggestions never writes. Automatic additions happen only at an eligible
collector's idle poll, in its existing transaction. Follow evidence is a ranking
hint, never evidence of friendship, trust, or business fit.
"""
from datetime import datetime, timedelta, timezone
import json

import accounts
import control
import db
import discovery_policy

RANK_POOL = 2000
OWNER_POOL = 500
MAX_RESULTS = 20
# Bound outstanding discovery work, not how many fresh networks may finish in a day.
MAX_AUTO_PENDING = 2
ENABLED_KEY = 'auto_discover'
NETWORK_CURSOR_KEY = 'auto_discover_network_cursor'
NETWORK_REVISIT_SECONDS = 300
MAX_DEGREE = 9223372036854775807

# Two independent seeks matter: an OR condition on degree/person_id makes
# SQLite revisit the entire equal-degree prefix on late pages. Each branch
# returns at most one window; the final merge sorts at most two windows.
NETWORK_POOL = '''SELECT person_id,degree FROM (
 SELECT person_id,degree FROM (
   SELECT person_id,degree FROM map_person_degree INDEXED BY discovery_network_candidates
   WHERE degree>=2 AND hidden=0 AND degree=:network_degree AND person_id>:network_person
   ORDER BY degree DESC,person_id LIMIT :rank_pool)
 UNION ALL
 SELECT person_id,degree FROM (
   SELECT person_id,degree FROM map_person_degree INDEXED BY discovery_network_candidates
   WHERE degree>=2 AND hidden=0 AND degree<:network_degree
   ORDER BY degree DESC,person_id LIMIT :rank_pool)
) ORDER BY degree DESC,person_id LIMIT :rank_pool'''

NETWORK_FIRST_POOL = '''SELECT person_id FROM map_person_degree INDEXED BY discovery_network_candidates
   WHERE degree>=2 AND hidden=0 ORDER BY degree DESC,person_id LIMIT :rank_pool'''


def ensure(conn):
    """Called by database initialization, never by the read-only preview."""
    conn.execute('CREATE TABLE IF NOT EXISTS collection_discovery('
                 'handle TEXT PRIMARY KEY COLLATE NOCASE,person_id INTEGER,ig_id TEXT,'
                 'state TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,directions TEXT NOT NULL)')
    for name, column in (('person', 'person_id'), ('identity', 'ig_id'), ('at', 'at')):
        conn.execute(f'CREATE INDEX IF NOT EXISTS collection_discovery_{name} ON collection_discovery({column})')
    conn.execute("CREATE INDEX IF NOT EXISTS discovery_manual_tags ON tags(lower(trim(tag)),person_id DESC) WHERE source='manual'")
    conn.execute('CREATE INDEX IF NOT EXISTS discovery_owner_recent ON owner_context(updated_at DESC,person_id DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS discovery_network_candidates '
                 'ON map_person_degree(degree DESC,person_id) WHERE degree>=2 AND hidden=0')

# Rank indexes bound the work before joining profiles. This deliberately offers a
# useful shortlist, not an exhaustive search of every saved person or graph edge.
CANDIDATES = """
WITH ranked AS MATERIALIZED (
 SELECT person_id FROM (SELECT person_id FROM verdicts INDEXED BY verdicts_score ORDER BY score DESC LIMIT :rank_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM verdicts INDEXED BY verdicts_prefilter ORDER BY prefilter DESC LIMIT :rank_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM marks WHERE status IN ('interested','talking','client') ORDER BY updated_at DESC,person_id DESC LIMIT :owner_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM tags WHERE source='manual'
   AND lower(trim(tag)) IN ('client','fit: strong','fit: good','good fit','strong fit','exceptional fit','exceptional opportunity','design partner') ORDER BY person_id DESC LIMIT :owner_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM owner_context WHERE relationships!='[]' ORDER BY updated_at DESC,person_id DESC LIMIT :owner_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM map_person_degree INDEXED BY discovery_network_candidates
   WHERE degree>=2 AND hidden=0 ORDER BY degree DESC,person_id LIMIT :rank_pool)
)
SELECT p.id,p.ig_id,p.handle,p.name,p.pic_file,p.followers,p.following,v.content_fit,
 coalesce(nullif(m.status,''),CASE WHEN EXISTS (SELECT 1 FROM tags t
   WHERE t.person_id=p.id AND t.source='manual' AND lower(trim(t.tag))='client')
   OR instr(coalesce(oc.relationships,''),'"client"')>0 THEN 'client' END) AS owner_status,
 coalesce(oc.relationships,'[]') AS relationships,
 max(coalesce(m.updated_at,''),coalesce(oc.updated_at,'')) AS feedback_at,
 EXISTS (SELECT 1 FROM tags t WHERE t.person_id=p.id AND t.source='manual'
   AND lower(trim(t.tag)) IN ('fit: strong','fit: good','good fit','strong fit','exceptional fit','exceptional opportunity','design partner')) AS manual_fit,
 coalesce(d.degree,0) AS observed_sources,
 (p.ig_id IS NOT NULL AND p.ig_id!='' AND p.is_private=0 AND coalesce(d.hidden,0)=0
   AND d.degree>=2 AND (v.content_fit IS NULL OR coalesce(v.tier,'unread')='unread')
   AND (SELECT count(*) FROM (SELECT DISTINCT ce.seed FROM current_edges ce
     WHERE ce.person_id=p.id LIMIT 2))>=2) AS network_candidate,
 NOT EXISTS (SELECT 1 FROM lists l WHERE l.seed=p.handle AND l.direction='followers')
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='list' AND j.seed=p.handle COLLATE NOCASE AND j.direction='followers'
                   ) AS can_followers,
 NOT EXISTS (SELECT 1 FROM lists l WHERE l.seed=p.handle AND l.direction='following')
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='list' AND j.seed=p.handle COLLATE NOCASE AND j.direction='following'
                   ) AS can_following
FROM ranked r JOIN people p ON p.id=r.person_id
 LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id
 LEFT JOIN owner_context oc ON oc.person_id=p.id
 LEFT JOIN map_person_degree d ON d.person_id=p.id
WHERE instr(p.handle,'~')=0 AND coalesce(p.is_private,0)=0 AND p.handle!='fortun8te' COLLATE NOCASE
 AND NOT EXISTS (SELECT 1 FROM seeds s WHERE s.is_me=1 AND
   (s.handle=p.handle OR (p.ig_id IS NOT NULL AND s.ig_id=p.ig_id)))
 AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.handle=p.handle COLLATE NOCASE OR (p.ig_id IS NOT NULL AND a.ig_id=p.ig_id))
 AND NOT EXISTS (SELECT 1 FROM account_identity_state a WHERE p.ig_id IS NOT NULL AND a.ig_id=p.ig_id)
 AND coalesce(m.status,'')!='no'
 AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.person_id=p.id AND t.source='manual'
   AND lower(trim(t.tag)) IN ('blocked','not a fit','not fit','do not scrape'))
 AND NOT EXISTS (SELECT 1 FROM collection_discovery h WHERE h.handle=p.handle COLLATE NOCASE)
 AND NOT EXISTS (SELECT 1 FROM collection_discovery h WHERE h.person_id=p.id)
 AND NOT EXISTS (SELECT 1 FROM collection_discovery h WHERE p.ig_id IS NOT NULL AND h.ig_id=p.ig_id)
 AND (owner_status IN ('interested','talking','client') OR relationships!='[]' OR manual_fit
      OR (coalesce(v.tier,'unread')!='unread' AND v.content_fit>=45)
      OR (network_candidate AND can_following AND coalesce(p.following,1)>0))
 AND ((can_followers AND coalesce(p.followers,1)>0) OR (can_following AND coalesce(p.following,1)>0))
 AND (:following_only=0 OR (can_following AND coalesce(p.following,1)>0))
ORDER BY CASE owner_status WHEN 'client' THEN 0 WHEN 'talking' THEN 1 WHEN 'interested' THEN 2 ELSE 3 END,
 (relationships!='[]') DESC, manual_fit DESC,
 (coalesce(v.tier,'unread')!='unread' AND v.content_fit>=45) DESC,
 feedback_at DESC, v.content_fit DESC,
 CASE WHEN network_candidate THEN -observed_sources ELSE observed_sources END,p.id
LIMIT :limit
"""


def policy(conn):
    value = db.get_setting(conn, discovery_policy.POLICY_KEY, discovery_policy.LEGACY)
    return value if value in discovery_policy.POLICIES else discovery_policy.LEGACY


def set_policy(conn, value):
    """Explicit opt-in; caller owns the transaction, as with set_enabled."""
    if value not in discovery_policy.POLICIES:
        raise ValueError('auto_discover_policy must be saved_fit or bounded_completion')
    db.set_setting(conn, discovery_policy.POLICY_KEY, value)


def _network_position(conn, now=None):
    now = now or datetime.now(timezone.utc)
    value = db.get_setting(conn, NETWORK_CURSOR_KEY, {})
    if not isinstance(value, dict):
        value = {}
    degree, person = value.get('degree', MAX_DEGREE), value.get('person_id', 0)
    if type(degree) is not int or type(person) is not int or not 0 <= degree <= MAX_DEGREE or person < 0:
        degree, person = MAX_DEGREE, 0
    try:
        revisit = datetime.fromisoformat(value.get('not_before', ''))
        waiting = revisit.tzinfo is not None and revisit > now
    except (ValueError, TypeError):
        waiting = False
    return {'network_degree': 0 if waiting else degree, 'network_person': person,
            'rank_pool': RANK_POOL}, waiting


def _advance_network_window(conn, now):
    """Called only by an eligible idle collector after an empty shortlist.

    Read one indexed window, then remember its last key. Reaching the tail
    schedules a quiet revisit instead of repeatedly rescanning an empty graph.
    """
    params, waiting = _network_position(conn, now)
    if waiting:
        return
    rows = conn.execute(NETWORK_POOL, params).fetchall()
    if len(rows) < RANK_POOL:
        value = {'degree': MAX_DEGREE, 'person_id': 0,
                 'not_before': (now + timedelta(seconds=NETWORK_REVISIT_SECONDS)).isoformat()}
    else:
        value = {'degree': rows[-1]['degree'], 'person_id': rows[-1]['person_id']}
    db.set_setting(conn, NETWORK_CURSOR_KEY, value)


def suggest(conn, limit=6, *, following_only=False, automatic=False, now=None):
    limit = min(MAX_RESULTS, max(1, int(limit)))
    selected_policy = policy(conn)
    completion = automatic and following_only and selected_policy == discovery_policy.COMPLETION
    position, _ = _network_position(conn, now)
    sql = CANDIDATES.replace(NETWORK_FIRST_POOL, 'SELECT person_id FROM (' + NETWORK_POOL + ')')
    rows = list(conn.execute(sql, {**position, 'owner_pool': OWNER_POOL,
                'limit': discovery_policy.CANDIDATE_POOL if completion else limit,
                'following_only': bool(following_only)}))
    if completion:
        admitted = conn.execute("SELECT count(*) FROM collection_discovery WHERE state='queued'").fetchone()[0]
        rows = discovery_policy.rank_for_completion(rows, admitted)[:limit]
    suggestions = []
    for row in rows:
        status = row['owner_status']
        reason = {'client': 'Marked as your client', 'talking': 'You are already talking',
                  'interested': 'Marked as interested'}.get(status)
        if not reason and row['relationships'] != '[]':
            reason = 'Personal connection recorded by you'
        if not reason and row['manual_fit']:
            reason = 'Good fit recorded by you'
        network_only = not reason and row['network_candidate']
        if network_only:
            reason = f"Seen in {row['observed_sources']} collected accounts’ lists"
        elif not reason:
            reason = 'Strong saved business fit' if row['content_fit'] >= 70 else 'Good saved business fit'
        if row['followers'] is not None:
            reason += f" · {row['followers']:,} followers on saved profile"
        if row['observed_sources'] and not network_only:
            reason += f" · Seen in {row['observed_sources']} collected accounts’ lists"
        directions = [direction for direction in ('followers', 'following')
                      if (not network_only or direction == 'following') and row['can_' + direction]
                      and (row[direction] is None or row[direction] > 0)]
        suggestions.append({'handle': row['handle'], 'person_id': row['id'], 'ig_id': row['ig_id'], 'display_name': row['name'],
                            'pic': f"/img/{row['id']}" if row['pic_file'] else None,
                            'reason': reason, 'directions': directions, 'followers': row['followers'],
                            'following': row['following'], 'business_fit': None if network_only else row['content_fit'],
                            'observed_sources': row['observed_sources'],
                            'evidence_kind': 'connections' if network_only else 'saved_fit'})
    today = datetime.now(timezone.utc).date().isoformat()
    return {'suggestions': suggestions, 'bounded': True, 'auto_discover': enabled(conn),
            'auto_discover_policy': selected_policy,
            'added_today': _added_today(conn, today),
            'history': [dict(r, directions=json.loads(r['directions'])) for r in conn.execute(
                'SELECT handle,state,at,reason,directions FROM collection_discovery ORDER BY at DESC,handle LIMIT 10')]}


def _added_today(conn, today):
    return conn.execute("SELECT count(*) FROM collection_discovery WHERE state='queued' AND at>=? AND at<?",
                        (today, today + 'z')).fetchone()[0]


def _pending_auto(conn):
    """At most two automatic targets can be outstanding, including delayed retries."""
    return len(conn.execute(
        "SELECT h.handle FROM collection_discovery h LEFT JOIN people p ON p.id=h.person_id JOIN jobs j "
        "ON j.kind='list' AND (j.seed=h.handle COLLATE NOCASE OR j.seed=p.handle COLLATE NOCASE) "
        "WHERE h.state='queued' AND j.state IN ('queued','leased') "
        "GROUP BY h.handle LIMIT ?", (MAX_AUTO_PENDING,)).fetchall())


def enabled(conn):
    return db.get_setting(conn, ENABLED_KEY, True) is True


def set_enabled(conn, value):
    if type(value) is not bool:
        raise ValueError('auto_discover must be true or false')
    db.set_setting(conn, ENABLED_KEY, value)


def hide(conn, handle):
    """Hide permanently by saved identity; callers own commit/rollback."""
    handle = db.norm_handle(handle)
    row = conn.execute('SELECT id,ig_id,handle FROM people WHERE handle=?', (handle,)).fetchone()
    if not row:
        raise ValueError('Unknown saved profile')
    conn.execute('INSERT OR IGNORE INTO collection_discovery(handle,person_id,ig_id,state,at,reason,directions) '
                 "VALUES(?,?,?,'hidden',?,'Hidden by you','[]')", (row['handle'],row['id'],row['ig_id'],db.now()))


def queue_when_idle(conn, row, now=None):
    """Add one fresh target while saved list work is idle; never retry a target.

    Caller owns the transaction and must still call accounts.pick_job to lease work.
    All Instagram operations remain behind the existing collector's account guards.
    """
    now = now or datetime.now(timezone.utc)
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    if (not enabled(conn) or control.stage_paused(conn, 'lists')
            or control.shared_collection_wait(conn, now) or not accounts.healthy(row, now)
            or control.lane_wait(conn, row, 'list', now)
            or 'list' not in accounts.kinds_for(conn, row, ['list'], now)):
        return None
    if _pending_auto(conn) >= MAX_AUTO_PENDING:
        return None
    # A healthy lane finishes available work first. Future retries must not
    # block unrelated discovery, but an active list keeps the queue bounded.
    if conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND state='leased' "
                    "AND leased_until>? LIMIT 1", (accounts.iso(now),)).fetchone():
        return None
    if accounts.pick_job(conn, row['lane_id'], ['list'], now) is not None:
        return None
    # Automatic exploration never recruits the protected main account.
    all_accounts = conn.execute('SELECT * FROM accounts').fetchall()
    if not accounts.keeps_lists(conn, row, now, accounts.list_share(conn, all_accounts)):
        return None
    # Explore a fresh following network first. Automatic discovery does not
    # add another potentially enormous follower crawl or refresh old coverage.
    # Explicit requested follower lists still win the pick_job check above.
    candidates = suggest(conn, 1, following_only=True, automatic=True, now=now)['suggestions']
    if not candidates:
        _advance_network_window(conn, now)
        return None
    candidate = candidates[0]
    directions = ['following']
    queued = [direction for direction in directions
              if db.queue_list(conn, candidate['handle'], direction, priority=-10)]
    if not queued:
        return None
    conn.execute('INSERT INTO collection_discovery(handle,person_id,ig_id,state,at,reason,directions) '
                 "VALUES(?,?,?,'queued',?,?,?)", (candidate['handle'], candidate['person_id'],candidate['ig_id'],
                                                now.isoformat(),candidate['reason'],json.dumps(queued)))
    return dict(candidate, directions=queued)
