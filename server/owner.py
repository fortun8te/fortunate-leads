"""Owner facts and their presentation, independent of automated profile assessment."""
import json

RELATIONSHIPS = {'worked_with': 'Worked with', 'client': 'Client', 'colleague': 'Colleague',
                 'friend': 'Friend', 'acquaintance': 'Acquaintance'}
FAMILIARITIES = {'close': 'Close', 'know_them': 'Know them', 'briefly': 'Briefly'}


def normalize_relationships(values):
    if not isinstance(values, list) or any(not isinstance(v, str) or v not in RELATIONSHIPS for v in values):
        raise ValueError('Choose a known relationship')
    values = set(values)
    if 'client' in values:
        values.add('worked_with')
    return [k for k in RELATIONSHIPS if k in values]


def relationships(person):
    values = person.get('relationships') or []
    if isinstance(values, str):
        values = json.loads(values)
    values = list(values)
    if person.get('status') == 'client' or any(str(t).strip().casefold() == 'client' for t in person.get('manual_tags') or []):
        values.append('client')
    return normalize_relationships(values)


def hydrate(conn, people):
    """Batch owner context for bounded list/map pages; no per-person database reads."""
    if not people:
        return
    by_id = {p['id']: p for p in people}
    ids = list(by_id)
    for offset in range(0, len(ids), 800):
        batch = ids[offset:offset + 800]
        for r in conn.execute(f"SELECT * FROM owner_context WHERE person_id IN ({','.join('?' for _ in batch)})", batch):
            p = by_id[r['person_id']]
            p['relationships'] = json.loads(r['relationships'])
            p['familiarity'] = r['familiarity']
            p['mark_rev'] = max(p.get('mark_rev') or '', r['updated_at'] or '')
    for p in people:
        p['relationships'] = relationships(p)
        p.setdefault('familiarity', None)


def has_connection(person):
    return bool(relationships(person)) or person.get('status') in ('talking', 'spoke_before')


def client_sql(alias='m'):
    return (f"({alias}.status='client' OR EXISTS (SELECT 1 FROM owner_context oc "
            f"WHERE oc.person_id={alias}.person_id AND instr(oc.relationships,'\"client\"')>0))")


def owner_status(person):
    """Explicit pipeline stage wins; legacy manual Client remains an owner fact."""
    status = person.get('status')
    if status:
        return status
    return 'client' if 'client' in relationships(person) else None


def visible_tags(person, tags):
    """Project current facts without deleting user labels or historical research."""
    status = owner_status(person)
    hidden = set()
    if has_connection(person):
        hidden |= {'not reachable', 'scout: no'}
    if status == 'client':
        hidden |= {'too big', 'other market'}
    if status == 'no':
        hidden |= {'scout: strong', 'scout: possible', 'ai: top fit', 'fit: strong', 'fit: good'}
    seen, result = set(), []
    for tag in tags:
        name = tag['tag'].casefold()
        if name in seen or (tag.get('source') == 'auto' and name in hidden):
            continue
        seen.add(name)
        result.append(tag)
    return result


def owner_recommendation(person, verdict):
    """Keep model business-fit evidence separate from the owner's relationship decision."""
    status = owner_status(person)
    if status == 'client':
        return dict(verdict, reason='Existing client relationship recorded by you. Timing and closeness are unspecified.')
    if status == 'talking':
        return dict(verdict, reason='Already in conversation. Follow up on the current discussion.')
    if relationships(person):
        verdict = dict(verdict, reason='You know them: ' + ', '.join(RELATIONSHIPS[k] for k in relationships(person)) + '. ' + (verdict.get('reason') or ''))
    if status == 'no':
        return dict(verdict, score=0, tier='cold', reason='Marked not a fit by you.')
    return verdict



def owner_conflict(person):
    labels = {str(tag).strip().casefold() for tag in person.get('manual_tags') or []}
    if has_connection(person) and 'not reachable' in labels:
        return 'Not reachable label conflicts with the saved relationship. Update the status or label.'
    return None


def visible_tag_sql(tag='t', mark='tm'):
    """SQL counterpart of visible_tags for filters/facets."""
    client = (f"({mark}.status='client' OR EXISTS(SELECT 1 FROM owner_context oc "
              f"WHERE oc.person_id={tag}.person_id AND oc.relationships LIKE '%client%') OR "
              f"EXISTS(SELECT 1 FROM tags ot WHERE ot.person_id={tag}.person_id "
              "AND ot.source='manual' AND lower(trim(ot.tag))='client'))")
    known = (f"({client} OR {mark}.status IN ('talking','spoke_before') OR EXISTS "
             f"(SELECT 1 FROM owner_context oc WHERE oc.person_id={tag}.person_id AND oc.relationships!='[]'))")
    status_client = f"(coalesce({mark}.status,'') IN ('','client') AND {client})"
    return (f"NOT (({tag}.source='auto' AND ("
            f"(coalesce({known},0) AND lower({tag}.tag) IN ('not reachable','scout: no')) OR "
            f"(coalesce({status_client},0) AND lower({tag}.tag) IN ('too big','other market')) OR "
            f"(coalesce({mark}.status,'')='no' AND lower({tag}.tag) IN ('scout: strong','scout: possible','ai: top fit','fit: strong','fit: good'))"
            f")))")


def feedback_marks_sql():
    """Small owner feedback relation. Friendship/work history alone are not business-fit labels."""
    return """SELECT m.person_id,
        CASE WHEN m.status='no' THEN 'no' WHEN oc.relationships LIKE '%client%' THEN 'client' ELSE m.status END AS status,
        max(coalesce(m.updated_at,''),coalesce(oc.updated_at,'')) AS updated_at
        FROM marks m LEFT JOIN owner_context oc ON oc.person_id=m.person_id
        UNION ALL SELECT oc.person_id,'client',oc.updated_at FROM owner_context oc
        WHERE oc.relationships LIKE '%client%' AND NOT EXISTS(SELECT 1 FROM marks m WHERE m.person_id=oc.person_id)"""
