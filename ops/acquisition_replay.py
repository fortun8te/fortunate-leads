"""Offline counterfactual replay of immutable captured requests; no live calls.

JSONL starts with {type:baseline, cutoff:UNIX_SECONDS, edges:[[src,dst]],
people:[stable_id]}. Subsequent records have job_id,target_id,direction,at,
request_count (null means unknown),role (page/lookup/error),members:[stable_id].
Only explicit baseline evidence predating the capture is used. Scheduling sees
job identities and observed results, never remaining payloads or final totals.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from acquisition_planner import MAX_CANDIDATES, choose, observe


def _identity(value):
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError('positive numeric stable IDs must be strings')
    return str(int(value))


def validate(baseline, records):
    cutoff = baseline['cutoff']
    if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff):
        raise ValueError('numeric baseline cutoff required')
    edges = {(_identity(a), _identity(b)) for a, b in baseline.get('edges', [])}
    people = {_identity(p) for p in baseline.get('people', [])}
    people.update(p for edge in edges for p in edge)
    events = []
    jobs = {}
    for ordinal, raw in enumerate(records):
        event = dict(raw)
        if isinstance(event['at'], bool) or not isinstance(event['at'], (int, float)) or not math.isfinite(event['at']):
            raise ValueError('finite numeric capture timestamp required')
        if event['at'] < cutoff:
            raise ValueError('capture precedes baseline cutoff')
        event['target_id'] = _identity(event['target_id'])
        if event['direction'] not in ('following', 'followers'):
            raise ValueError('invalid direction')
        if event.get('role', 'page') not in ('page', 'lookup', 'error'):
            raise ValueError('invalid request role')
        count = event.get('request_count')
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError('invalid confirmed request count')
        event['members'] = [_identity(p) for p in event.get('members', [])]
        if event['members'] and (not count or event.get('role', 'page') != 'page'):
            raise ValueError('member evidence requires a confirmed page request')
        key = str(event['job_id'])
        identity = (event['target_id'], event['direction'])
        if key in jobs and jobs[key] != identity:
            raise ValueError('job identity changed')
        jobs[key] = identity
        event.update(job_id=key, ordinal=ordinal)
        events.append(event)
    events.sort(key=lambda e: (e['at'], e['ordinal']))
    return edges, people, events


def load_jsonl(path):
    with open(path) as source:
        rows = [json.loads(line) for line in source if line.strip()]
    if not rows or rows[0].get('type') != 'baseline':
        raise ValueError('explicit first baseline record required')
    return rows[0], rows[1:], {'source': 'JSONL capture', 'baseline_people_scope': 'explicit captured baseline'}


def load_benchmark(path):
    """Read benchmark requests/observations without using current task end states.

    Benchmark schema records membership per request but not per-page cursor
    chains. Reconstruct task chronology only; no complete-list proof is inferred.
    """
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        meta = json.loads(conn.execute('SELECT value FROM metadata').fetchone()[0])
        baseline = dict(type='baseline', cutoff=meta['created_at'],
                        edges=[list(row) for row in conn.execute('SELECT src,dst FROM baseline')], people=[])
        memberships = defaultdict(list)
        for row in conn.execute('SELECT o.request_id,o.src,o.dst,t.direction FROM observations o JOIN requests r USING(request_id) JOIN tasks t USING(task_id)'):
            memberships[row['request_id']].append(row['dst'] if row['direction'] == 'following' else row['src'])
        records = []
        for row in conn.execute('SELECT r.*,t.target_id,t.direction,t.arm,t.warmup FROM requests r JOIN tasks t USING(task_id) ORDER BY r.started_at,r.rowid'):
            result = json.loads(row['result'] or '{}')
            records.append(dict(job_id=str(row['task_id']), target_id=row['target_id'], direction=row['direction'],
                                at=row['started_at'], request_count=result.get('actual_http_requests'),
                                role=row['role'], members=memberships[row['request_id']],
                                status=result.get('status', 'unknown'), arm=row['arm'], warmup=bool(row['warmup'])))
    return baseline, records, dict(source='saved benchmark SQLite',
        baseline_people_scope='baseline edge endpoints only; isolated previously known profiles are unavailable',
        baseline_identity=meta.get('baseline_identity', 'identity history not proven'),
        chain_proof=False, chronology='per task request order; no historical per-page cursor chain',
        transport_scope='all recorded arms and warmups included, including confirmed errors and lookups')


def _epoch(value, assume_utc=False):
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        if not assume_utc:
            raise ValueError('timestamps must include timezone')
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_production(path, since, until=None, max_events=10000):
    """Saved list-page response replay, explicitly NOT total Instagram requests.

    Profile traffic is not reliably attributable to list jobs in this schema.
    Successful list responses prove one received request; status zero/NULL does
    not prove transport cost. Legacy pages use unique (job,time) attribution only.
    """
    if type(max_events) is not int or not 1 <= max_events <= 10000:
        raise ValueError('max_events must be between 1 and 10000')
    cutoff = _epoch(since)
    end = _epoch(until) if until else float('inf')
    if end <= cutoff:
        raise ValueError('until must follow since')
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        required = {'collector_events','edge_observations','people','seeds','jobs'}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables):
            raise ValueError('production capture schema missing required tables')
        identities = defaultdict(set)
        person_ids = {}
        known_people = set()
        for row in conn.execute('SELECT id,handle,ig_id,first_seen FROM people'):
            try:
                identity = _identity(row['ig_id'])
            except ValueError:
                continue
            person_ids[row['id']] = identity
            identities[row['handle'].lower()].add(identity)
            if row['first_seen'] and _epoch(row['first_seen'], assume_utc=True) < cutoff:
                known_people.add(identity)
        for row in conn.execute('SELECT handle,ig_id FROM seeds'):
            try:
                identities[row['handle'].lower()].add(_identity(row['ig_id']))
            except ValueError:
                continue
        def resolve(handle):
            values = identities.get((handle or '').lower(), set())
            return next(iter(values)) if len(values) == 1 else None
        # Parse timestamps rather than relying on mixed ISO offset string order.
        all_events = [dict(row) for row in conn.execute(
            'SELECT c.*,j.seed,j.target_ig_id,j.created_at AS job_created_at FROM collector_events c LEFT JOIN jobs j ON j.id=c.job_id ORDER BY c.id')]
        window = [e for e in all_events if cutoff <= _epoch(e['at'], assume_utc=True) < end]
        all_page_events = [e for e in window if e['kind'] == 'list']
        eligible_pages = [e for e in all_page_events if e.get('job_created_at') and _epoch(e['job_created_at'], assume_utc=True) < cutoff]
        future_candidate_events = len(all_page_events) - len(eligible_pages)
        page_events = sorted(eligible_pages, key=lambda e: (_epoch(e['at'], assume_utc=True),e['id']))
        truncated = len(page_events) > max_events
        page_events = page_events[:max_events]
        selected_jobs = {e['job_id'] for e in page_events}
        members_by_key = defaultdict(set)
        keys_by_time = defaultdict(set)
        baseline_edges = set()
        unresolved_baseline = 0
        for row in conn.execute('SELECT seed,person_id,direction,page_key,job_id,observed_at FROM edge_observations'):
            target, member = resolve(row['seed']), person_ids.get(row['person_id'])
            observed_at = _epoch(row['observed_at'], assume_utc=True)
            if observed_at < cutoff:
                if target and member and row['direction'] in ('following','followers'):
                    baseline_edges.add((target,member) if row['direction']=='following' else (member,target))
                else:
                    unresolved_baseline += 1
            elif observed_at < end and row['job_id'] in selected_jobs:
                key = (row['job_id'],row['page_key'])
                keys_by_time[(row['job_id'], row['observed_at'])].add(key)
                if member:
                    members_by_key[key].add(member)
        event_count_by_time = defaultdict(int)
        for event in window:
            if event['kind'] != 'list':
                continue
            event_count_by_time[(event['job_id'],event['at'])] += 1
        records = []
        unresolved_targets = unmatched_pages = timestamp_matches = exact_matches = 0
        for event in page_events:
            target = event.get('target_ig_id')
            try:
                target = _identity(target) if target else resolve(event['seed'])
            except ValueError:
                target = None
            if not target:
                unresolved_targets += 1
                continue
            key = (event['job_id'],event['event_id'])
            members = members_by_key.get(key)
            if members is not None:
                exact_matches += 1
            else:
                stamp = (event['job_id'],event['at'])
                keys = keys_by_time.get(stamp, set())
                if event_count_by_time[stamp] == 1 and len(keys) == 1:
                    members = members_by_key.get(next(iter(keys)), set())
                    timestamp_matches += 1
                else:
                    members = set()
                    unmatched_pages += int(bool(event.get('returned_count')))
            status = event.get('http_status')
            count = 1 if isinstance(status, int) and 100 <= status <= 599 else None
            records.append(dict(job_id=str(event['job_id']), target_id=target,
                direction=event['direction'], at=_epoch(event['at'], assume_utc=True), members=sorted(members) if count else [],
                request_count=count, role='page', status='ok' if status == 200 else str(status),
                source_event_id=event['id']))
    baseline = dict(type='baseline', cutoff=cutoff, edges=sorted(baseline_edges), people=sorted(known_people),
                    request_scope='recorded_list_page_responses_only')
    profiles = [e for e in window if e['kind'] != 'list']
    provenance = dict(source='production snapshot, read only', since=since, until=until,
        timestamp_assumption='Legacy database timestamps without timezone interpreted as UTC; explicit cutoff requires timezone.',
        request_scope='Recorded list-page response attempts only; profile lookups and unrecorded transport attempts are excluded. This is a page budget comparison, not total request efficiency.',
        profile_events_excluded=len(profiles),
        confirmed_profile_responses_excluded=sum(isinstance(e['http_status'],int) and 100<=e['http_status']<=599 for e in profiles),
        captured_list_events=len(page_events), truncated=truncated, max_events=max_events,
        candidate_scope='Only jobs created strictly before cutoff; later job identities never admitted retrospectively.',
        later_or_undated_candidate_events_excluded=future_candidate_events,
        unresolved_target_events_excluded=unresolved_targets, positive_pages_without_attributed_members=unmatched_pages,
        exact_event_id_matches=exact_matches, unique_job_timestamp_matches=timestamp_matches,
        baseline_unresolved_observations=unresolved_baseline,
        baseline_identity='Stable IDs resolved from snapshot people/seeds or job target; historical handle identity is not proven. Conflicting handle IDs excluded.',
        baseline_people_scope='Snapshot people with first_seen strictly before cutoff, plus prior positive edge endpoints.',
        coverage='Observed positive evidence only; absent historic observations do not prove novel-to-Instagram or complete lists.', chain_proof=False)
    return baseline, records, provenance


def replay(baseline, records, budget=400):
    if type(budget) is not int or budget < 0:
        raise ValueError('nonnegative integer budget required')
    base_edges, base_people, events = validate(baseline, records)
    histories = defaultdict(list)
    for event in events:
        histories[event['job_id']].append(event)
    results = {}
    for policy in ('chronological', 'round_robin', 'adaptive'):
        edges, people = set(base_edges), set(base_people)
        # Known target identities are part of the initial candidate pool, not discoveries.
        people.update(event['target_id'] for event in events)
        initial_people = len(people)
        positions = {job: 0 for job in histories}
        # Candidate ordering uses stable job IDs, never future first-page times.
        active = sorted(histories, key=lambda key: (0, int(key)) if key.isdigit() else (1, key))
        state = {}
        cost = pages = uncertain = consumed = errors = lookups = 0
        selections = []
        while active:
            window = active[:MAX_CANDIDATES]
            if policy == 'chronological':
                job = min(active, key=lambda k: (histories[k][positions[k]]['at'], histories[k][positions[k]]['ordinal']))
            elif policy == 'round_robin':
                job = window[0]
            else:
                job = str(choose([{'job_id': k} for k in window], state)['job_id'])
            event = histories[job][positions[job]]
            count = event.get('request_count')
            # Unknown transport cost is not free: stop before it in every policy.
            if count is None:
                uncertain += 1
                break
            if cost + count > budget:
                break
            before_edges, before_people = len(edges), len(people)
            for member in event['members']:
                pair = (event['target_id'], member) if event['direction'] == 'following' else (member, event['target_id'])
                edges.add(pair)
                people.add(member)
            role = event.get('role', 'page')
            state = observe(state, job, requests=count, new_edges=len(edges)-before_edges,
                            new_people=len(people)-before_people, is_page=role == 'page')
            cost += count
            pages += int(role == 'page')
            errors += int(role == 'error' or event.get('status', 'ok') != 'ok')
            lookups += int(role == 'lookup')
            consumed += 1
            selections.append(dict(job_id=job, ordinal=event['ordinal']))
            positions[job] += 1
            active.remove(job)
            if positions[job] < len(histories[job]):
                active.append(job)
            if cost == budget:
                break
        new_edges, new_people = len(edges)-len(base_edges), len(people)-initial_people
        results[policy] = dict(confirmed_requests=cost, recorded_events_consumed=consumed,
            page_events=pages, error_events=errors, lookup_events=lookups,
            canonical_new_edges=new_edges, unique_new_people=new_people,
            new_edges_per_request=new_edges/cost if cost else None,
            new_people_per_request=new_people/cost if cost else None,
            stopped_on_unknown_cost=bool(uncertain), remaining_recorded_events=len(events)-consumed,
            targets_started=len(state.get('jobs', {})), completed_lists=None,
            exhausted_recorded_histories=sum(positions[k] == len(v) for k,v in histories.items()), selections=selections)
    return dict(experimental=True, request_scope=baseline.get('request_scope', 'all_captured_confirmed_requests'), budget=budget, baseline_edges=len(base_edges), baseline_people=len(base_people),
        recorded_requests=sum(e.get('request_count') or 0 for e in events),
        unknown_cost_events=sum(e.get('request_count') is None for e in events),
        candidate_jobs=len(histories), results=results,
        limitations=['Counterfactual saved-response ordering, not a live rate or sustainable daily capacity.',
                    'Candidates are a fixed captured cohort; future members and request outcomes are hidden until consumed.',
                    'No completion or cursor-chain proof; exhausted captured history remains unknown coverage.',
                    'Unknown request costs stop replay; no invented lookup costs or free error attempts.',
                    'No held-out win demonstrated; experimental only.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--db', type=Path)
    source.add_argument('--jsonl', type=Path)
    parser.add_argument('--budget', type=int, default=400)
    parser.add_argument('--since', help='Production snapshot baseline cutoff, ISO timestamp with timezone')
    parser.add_argument('--until', help='Exclusive production snapshot end timestamp')
    parser.add_argument('--max-events', type=int, default=10000)
    args = parser.parse_args()
    if args.since and not args.db:
        parser.error('--since requires --db')
    baseline, records, provenance = (load_production(args.db,args.since,args.until,args.max_events) if args.since
                                     else load_benchmark(args.db) if args.db else load_jsonl(args.jsonl))
    result = replay(baseline, records, args.budget)
    result['provenance'] = provenance
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
