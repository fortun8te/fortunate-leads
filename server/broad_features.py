"""Profile-only rule features for Broad training and inference.

Derive these afresh from the same visible profile fields in both paths. Stored
verdicts and auto tags can contain Grok's answer and must not be model inputs.
"""

import qualify


PROFILE_FIELDS = ('handle', 'name', 'category', 'bio', 'followers')


def from_profile(person):
    profile = {key: person.get(key) for key in PROFILE_FIELDS}
    tags = qualify.rule_tags(profile, [], None)
    verdict = qualify.rule_verdict(profile, tags, net={})
    return verdict['content_fit'] or 0, [tag for tag, group in tags if group != 'source']
