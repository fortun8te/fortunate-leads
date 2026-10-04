#!/usr/bin/env python3
"""Read-only evidence for docs/SECOND_PROFILE_TEST.md: how Instagram limited three accounts on one Mac and one IP.

  python3 ops/twin_evidence.py PATH/TO/leads.sqlite

Opens the database read-only. Prints: the busiest rolling hour per account and for two accounts together, every
adverse event with the account's own activity in the hour before it, how close events on different accounts fall to
each other (with the chance of that under random timing), people per request by page size, and how much of the clock
any account collected at all. Counts only; no judgement.
"""
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone


def ts(value):
    return datetime.fromisoformat(value).timestamp()


def busiest(times, window):
    best, j = (0, None), 0
    for i, t in enumerate(times):
        while times[j] <= t - window:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, t)
    return best


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%d %b %H:%M:%S')


def main(path):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    names = {r['lane_id']: ('@' + (r['handle'] or r['lane_id'])) for r in db.execute('SELECT lane_id, handle FROM accounts')}
    pages = defaultdict(list)
    for r in db.execute('SELECT lane, at FROM pages WHERE at IS NOT NULL ORDER BY at'):
        pages[r['lane']].append(ts(r['at']))
    everyone = sorted(t for v in pages.values() for t in v)
    print('1. busiest rolling hour of saved list pages (any hour with no list rate limit)')
    for lane, v in pages.items():
        n, at = busiest(v, 3600)
        print(f'   {names.get(lane, lane):<20} {n:>4} pages in the hour ending {stamp(at)} UTC   ({len(v)} pages in total)')
    n, at = busiest(everyone, 3600)
    print(f'   {"all accounts":<20} {n:>4} pages in the hour ending {stamp(at)} UTC')

    events = [dict(r) for r in db.execute(
        "SELECT at, lane, kind, direction, outcome, reason, http_status, job_id FROM collector_events "
        "WHERE outcome IN ('rate_limit','soft_block','challenge','login') OR reason IN ('list_html_home_redirect','scraping_warning') ORDER BY at")]
    print('\n2. adverse events (429, soft block, challenge, login, home-page redirect), with the same account\'s pages in the hour before')
    limits = []
    for e in events:
        t = ts(e['at'])
        before = sum(1 for x in pages.get(e['lane'], []) if t - 3600 <= x < t)
        if e['outcome'] == 'rate_limit':
            limits.append((t, e['lane']))
        if e['outcome'] != 'other' or e['reason'] != 'list_html_home_redirect':
            print(f"   {stamp(t)}  {names.get(e['lane'], e['lane']):<18} {e['kind'] or '-':<8} {e['outcome']}/{e['reason']} http {e['http_status']}   pages in the previous hour: {before}")
    redirects = [e for e in events if e['reason'] == 'list_html_home_redirect']
    by_job = defaultdict(set)
    for e in redirects:
        by_job[e['job_id']].add(e['lane'])
    shared = sum(1 for lanes in by_job.values() if len(lanes) > 1)
    print(f'   plus {len(redirects)} followers home-page redirects; {shared} of {len(by_job)} redirecting jobs were redirected for more than one account')

    print('\n3. do events on different accounts cluster in time?')
    if len(limits) >= 3:
        lo, hi = min(t for t, _ in limits), max(t for t, _ in limits)
        labels = [lane for _, lane in limits]
        close = min((abs(a[0] - b[0]), a, b) for i, a in enumerate(limits) for b in limits[i + 1:] if a[1] != b[1])
        print(f'   closest pair on different accounts: {close[0]:.0f} s apart ({names.get(close[1][1])} {stamp(close[1][0])}, {names.get(close[2][1])} {stamp(close[2][0])})')
        rng, trials, hit_pair, hit_all = random.Random(7), 200000, 0, 0
        distinct = len(set(labels))
        for _ in range(trials):
            ts_ = [rng.uniform(lo, hi) for _ in labels]
            if any(labels[i] != labels[j] and abs(ts_[i] - ts_[j]) <= close[0] for i in range(len(ts_)) for j in range(i + 1, len(ts_))):
                hit_pair += 1
            span = 17 * 60
            if any(len({labels[i] for i in range(len(ts_)) if s <= ts_[i] <= s + span}) == distinct for s in ts_):
                hit_all += 1
        print(f'   if the {len(limits)} rate limits fell at random between the first and the last: '
              f'{hit_pair / trials:.2%} chance of a pair that close, {hit_all / trials:.2%} chance of every account hit inside 17 minutes')
        print('   (the events were looked at before the pattern was chosen, so treat these as suggestive only)')

    print('\n4. people per request, by direction and page size asked for')
    for r in db.execute("SELECT direction, requested_count, count(*) n, round(avg(returned_count),1) ret, round(avg(new_links),1) new "
                        "FROM collector_events WHERE outcome='page' GROUP BY 1,2 ORDER BY 1,2"):
        print(f"   {r['direction']:<10} asked {r['requested_count']:>3}  pages {r['n']:>5}  returned {r['ret']:>6}  new links {r['new']}")

    print('\n5. how much of the clock any account collected')
    hours = {t // 3600 for t in everyone}
    span = (everyone[-1] - everyone[0]) / 3600
    print(f'   {len(hours)} clock hours with at least one page, out of {span:.0f} hours between the first and last page ({len(hours) / span:.0%})')
    for lane, v in pages.items():
        h = {t // 3600 for t in v}
        print(f'   {names.get(lane, lane):<20} {len(h):>3} active hours between {stamp(v[0])} and {stamp(v[-1])}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
