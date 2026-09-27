"""Throughput model for the split pipeline. Every number is labelled with where it comes from.

    python3 server/scale/model.py              # markdown tables for 40k / 100k / 200k per hour
    python3 server/scale/model.py --target 60000 --accounts 3

"profiles/hour" here = NEW unique profiles discovered by list pagination AND enriched (bio etc.)
by the logged-out pool in the same hour. Discovery needs logged-in bot accounts; enrichment
needs egress units (Tor circuits, IPv6 /64s, phones). The tier that runs out first binds.
"""
import argparse
import math

# name: (conservative, central, optimistic, source/label)
PARAMS = {
    'pages_per_account_hour': (100, 200, 350,
        'ESTIMATE. Ceiling ~390/h = extension window 72 req/11 min; instaloader mobile throttle 199/30 min. '
        'Enumeration trips "please wait" after "a few hundred calls in a short window" (instagrapi guide).'),
    'users_per_page': (20, 35, 75,
        'web followers ~15-25/page (RESEARCH.md, M-H), following ~50; mobile API reported 50-100 '
        '(instagrapi guide, L-M, not yet measured here).'),
    'active_hours_per_day': (14, 18, 20, 'ESTIMATE: burner accounts, rest windows included.'),
    'unique_yield': (0.5, 0.65, 0.8,
        'ESTIMATE: share of list rows that are new to the global seen-cache; depends on seed overlap. '
        'Measure with metrics pipeline/lists users_new vs users_seen.'),
    'enrich_per_unit_hour': (180, 250, 400,
        'MEASURED 2026-09-24: 36 Tor circuits -> ~152 profiles/min = 4.2/min/circuit = ~253/h (igscraper). '
        'Optimistic assumes the 1-request Android route at ~7 profiles/min/unit (ESTIMATE).'),
    'usable_tor_circuits': (150, 300, 600,
        'ESTIMATE: ~1,400 Tor exit relays exist (Sep 2026) and path selection is bandwidth-weighted, so '
        'many circuits share exits; measured only up to 36 circuits (near-linear).'),
}

SCEN = {'conservative': 0, 'central': 1, 'optimistic': 2}


def p(name, scen):
    return PARAMS[name][SCEN[scen]]


def per_account_hour(scen):
    return p('pages_per_account_hour', scen) * p('users_per_page', scen) * p('unique_yield', scen)


def plan(target, scen='central', accounts=3):
    acct_rate = per_account_hour(scen)
    need_accts = math.ceil(target / acct_rate)
    need_units = math.ceil(target / p('enrich_per_unit_hour', scen))
    tor = p('usable_tor_circuits', scen)
    extra = max(0, need_units - tor)
    have_lists = accounts * acct_rate
    have_enrich = tor * p('enrich_per_unit_hour', scen)
    binding = 'bot accounts (list pagination)' if have_lists < min(target, have_enrich) else \
        'egress units (enrichment)' if have_enrich < target else 'none at this target'
    return {'target_per_hour': target, 'scenario': scen, 'new_profiles_per_account_hour': round(acct_rate),
            'bot_accounts_needed': need_accts, 'enrichment_units_needed': need_units,
            'tor_circuits_usable': tor, 'non_tor_units_needed': extra,
            'with_accounts': accounts, 'lists_capacity_per_hour': round(have_lists),
            'enrich_capacity_tor_only_per_hour': round(have_enrich),
            'achievable_per_hour_now': round(min(have_lists, have_enrich)), 'binding': binding}


def table(targets=(40000, 100000, 200000), accounts=3):
    rows = ['| target/h | scenario | new/acct/h | accounts needed | enrich units | of which non-Tor | '
            'capacity/h with %d accts (own IPs) | binding |' % accounts, '|---|---|---|---|---|---|---|---|']
    for t in targets:
        for s in SCEN:
            r = plan(t, s, accounts)
            rows.append('| %s | %s | %s | %s | %s | %s | %s | %s |' % (
                f'{t:,}', s, f"{r['new_profiles_per_account_hour']:,}", r['bot_accounts_needed'],
                r['enrichment_units_needed'], r['non_tor_units_needed'], f"{r['achievable_per_hour_now']:,}",
                r['binding']))
    return '\n'.join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--target', type=int, action='append')
    ap.add_argument('--accounts', type=int, default=3)
    a = ap.parse_args(argv)
    print(table(tuple(a.target or (40000, 100000, 200000)), a.accounts))
    print()
    for k, v in PARAMS.items():
        print('- %s: %s / %s / %s. %s' % (k, v[0], v[1], v[2], v[3]))


if __name__ == '__main__':
    main()
