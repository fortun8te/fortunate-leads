"""Owner facts and their presentation, independent of automated profile assessment."""

def owner_status(person):
    """Explicit pipeline stage wins; legacy manual Client remains an owner fact."""
    status = person.get('status')
    if status:
        return status
    return 'client' if any(str(t).strip().casefold() == 'client'
                           for t in person.get('manual_tags') or []) else None


def visible_tags(person, tags):
    """Project current facts without deleting user labels or historical research."""
    status = owner_status(person)
    hidden = set()
    if status in ('client', 'talking'):
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
        # The stage already has its own UI; preserve the stored label for reversibility.
        if person.get('status') == 'client' and name == 'client':
            continue
        seen.add(name)
        result.append(tag)
    return result


def owner_recommendation(person, verdict):
    """Keep model business-fit evidence separate from the owner's relationship decision."""
    status = owner_status(person)
    if status == 'client':
        return dict(verdict, reason='Existing client. Relationship confirmed by you.')
    if status == 'talking':
        return dict(verdict, reason='Already in conversation. Follow up on the current discussion.')
    if status == 'no':
        return dict(verdict, score=0, tier='cold', reason='Marked not a fit by you.')
    return verdict



def owner_conflict(person):
    labels = {str(tag).strip().casefold() for tag in person.get('manual_tags') or []}
    if person.get('status') and person['status'] != 'client' and 'client' in labels:
        stage = {'no': 'Not a fit', 'interested': 'Interested', 'contacted': 'Contacted', 'talking': 'Talking'}.get(person['status'], person['status'])
        return f'Client label conflicts with {stage} status. Choose the current status.'
    if owner_status(person) in ('client', 'talking') and 'not reachable' in labels:
        return 'Not reachable label conflicts with the saved relationship. Update the status or label.'
    return None


def visible_tag_sql(tag='t', mark='tm'):
    """SQL counterpart for filters/facets; aliases are internal, never user input."""
    status = (f"coalesce(nullif({mark}.status,''), CASE WHEN EXISTS "
              f"(SELECT 1 FROM tags ot WHERE ot.person_id={tag}.person_id "
              "AND ot.source='manual' AND lower(trim(ot.tag))='client') THEN 'client' ELSE '' END)")
    return (f"NOT (({tag}.source='auto' AND ("
            f"({status} IN ('client','talking') AND lower({tag}.tag) IN ('not reachable','scout: no')) OR "
            f"({status}='client' AND lower({tag}.tag) IN ('too big','other market')) OR "
            f"({status}='no' AND lower({tag}.tag) IN ('scout: strong','scout: possible','ai: top fit','fit: strong','fit: good'))"
            f")) OR (coalesce({mark}.status,'')='client' AND lower({tag}.tag)='client'))")
