"""Bounded, read-only next-target suggestions from already saved evidence.

Extends the existing snowball idea (owner-positive people) with current profile
fit. This does not claim their audiences are suitable, or queue collection.
"""

RANK_POOL = 2000
OWNER_POOL = 500
MAX_RESULTS = 20

# Rank indexes bound the work before joining profiles. This deliberately offers a
# useful shortlist, not an exhaustive search of every saved person or graph edge.
CANDIDATES = """
WITH ranked AS MATERIALIZED (
 SELECT person_id FROM (SELECT person_id FROM verdicts INDEXED BY verdicts_score ORDER BY score DESC LIMIT :rank_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM verdicts INDEXED BY verdicts_prefilter ORDER BY prefilter DESC LIMIT :rank_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM marks WHERE status IN ('interested','talking','client') LIMIT :owner_pool)
 UNION SELECT person_id FROM (SELECT person_id FROM tags WHERE tag='Client' AND source='manual' LIMIT :owner_pool)
)
SELECT p.id,p.handle,p.name,p.pic_file,p.followers,p.following,v.content_fit,
 coalesce(nullif(m.status,''),CASE WHEN EXISTS (SELECT 1 FROM tags t
   WHERE t.person_id=p.id AND t.source='manual' AND lower(trim(t.tag))='client') THEN 'client' END) AS owner_status,
 coalesce(d.degree,0) AS observed_sources,
 NOT EXISTS (SELECT 1 FROM lists l WHERE l.seed=p.handle AND l.direction='followers')
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='list' AND j.seed=p.handle AND j.direction='followers'
                   AND j.state IN ('queued','leased','done')) AS can_followers,
 NOT EXISTS (SELECT 1 FROM lists l WHERE l.seed=p.handle AND l.direction='following')
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='list' AND j.seed=p.handle AND j.direction='following'
                   AND j.state IN ('queued','leased','done')) AS can_following
FROM ranked r JOIN people p ON p.id=r.person_id
 LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id
 LEFT JOIN map_person_degree d ON d.person_id=p.id
WHERE instr(p.handle,'~')=0 AND coalesce(p.is_private,0)=0 AND p.handle!='fortun8te' COLLATE NOCASE
 AND NOT EXISTS (SELECT 1 FROM seeds s WHERE s.handle=p.handle AND s.is_me=1)
 AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.handle=p.handle COLLATE NOCASE OR (p.ig_id IS NOT NULL AND a.ig_id=p.ig_id))
 AND NOT EXISTS (SELECT 1 FROM account_identity_state a WHERE p.ig_id IS NOT NULL AND a.ig_id=p.ig_id)
 AND coalesce(m.status,'')!='no'
 AND (owner_status IN ('interested','talking','client') OR (coalesce(v.tier,'unread')!='unread' AND v.content_fit>=45))
 AND ((can_followers AND coalesce(p.followers,1)>0) OR (can_following AND coalesce(p.following,1)>0))
ORDER BY CASE owner_status WHEN 'client' THEN 0 WHEN 'talking' THEN 1 WHEN 'interested' THEN 2 ELSE 3 END,
 v.content_fit DESC, observed_sources DESC,p.id
LIMIT :limit
"""


def suggest(conn, limit=6):
    limit = min(MAX_RESULTS, max(1, int(limit)))
    rows = conn.execute(CANDIDATES, {'rank_pool': RANK_POOL, 'owner_pool': OWNER_POOL, 'limit': limit})
    suggestions = []
    for row in rows:
        status = row['owner_status']
        reason = {'client': 'Marked as your client', 'talking': 'You are already talking',
                  'interested': 'Marked as interested'}.get(status)
        if not reason:
            reason = 'Strong saved business fit' if row['content_fit'] >= 70 else 'Good saved business fit'
        if row['followers'] is not None:
            reason += f" · {row['followers']:,} followers on saved profile"
        elif row['observed_sources']:
            reason += f" · Seen in {row['observed_sources']} collected accounts’ lists"
        directions = [direction for direction in ('followers', 'following')
                      if row['can_' + direction] and (row[direction] is None or row[direction] > 0)]
        suggestions.append({'handle': row['handle'], 'display_name': row['name'],
                            'pic': f"/img/{row['id']}" if row['pic_file'] else None,
                            'reason': reason, 'directions': directions, 'followers': row['followers'],
                            'following': row['following'], 'business_fit': row['content_fit'],
                            'observed_sources': row['observed_sources']})
    return {'suggestions': suggestions, 'bounded': True}
