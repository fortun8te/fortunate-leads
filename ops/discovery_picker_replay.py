#!/usr/bin/env python3
"""Replay the next-seed picker on history. Read only: no Instagram requests, no writes to the database.

Question answered: given the lists we really collected, would choosing the next seed with the picker have
returned more new people per request than first-in-first-out, most-overlap-first or random order?

How: the real following lists collected after the cohort start are the pool. A policy picks one at a time.
Each pick is charged its request cost (one lookup plus ceil(members / 45) pages), and returns the members
that nobody earlier in the simulated order had returned and that were not already saved at the start.
Order changes who gets credit for a person, so this is an exact set computation, not an estimate.
Every policy sees only what would have been known then: lists simulated so far, plus the lists collected
before the cohort. Candidate size is the saved profile's following count, which was read at lookup time, so
a variant that hides it shows how much the answer depends on it.

Limits, stated plainly: the pool holds only seeds that were collected (hand-picked by Michael or by the old
suggestion rule), so this tests ranking skill inside that set. It cannot score accounts that were never
collected. A list's members are what Instagram returned, including partial lists. Saved follower and
following counts come from the current snapshot, so they are slightly retrospective.
"""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from math import ceil
from pathlib import Path
import random
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import discovery_picker as picker  # noqa: E402

CUTOFF = '2026-09-24T15:50:00+00:00'
BUDGETS = (100, 250, 500, 1000, 1500, 2000)


def stamp(value):
    value = value.replace('Z', '+00:00')
    if len(value) == 10:
        value += 'T00:00:00+00:00'
    return datetime.fromisoformat(value).timestamp()


class Dataset:
    """Everything the simulation needs, loaded once."""

    def __init__(self, conn, cutoff=CUTOFF, min_members=10):
        cut = stamp(cutoff)
        self.people = {r['id']: dict(r) for r in conn.execute(
            'SELECT id,handle,first_seen,followers,following,is_verified,is_business,category,is_private FROM people')}
        by_handle = {p['handle'].lower(): p for p in self.people.values()}
        members, first = defaultdict(set), {}
        for row in conn.execute("SELECT seed,person_id,first_seen FROM edges WHERE direction='following'"):
            seed = row['seed'].lower()
            members[seed].add(row['person_id'])
            t = stamp(row['first_seen'])
            if seed not in first or t < first[seed]:
                first[seed] = t
        listed = {r['seed'].lower() for r in conn.execute("SELECT seed FROM lists WHERE direction='following'")}
        self.baseline = {s for s in members if first[s] < cut or s not in listed}
        self.pool = {}
        for seed, ids in members.items():
            person = by_handle.get(seed)
            if seed in self.baseline or person is None or len(ids) < min_members:
                continue
            self.pool[seed] = {'members': frozenset(ids), 'start': first[seed], 'person': person,
                               'cost': picker.cost(len(ids))}
        self.members = {s: frozenset(m) for s, m in members.items()}
        self.containers = defaultdict(set)         # person -> following lists (any) that contain them
        for seed, ids in members.items():
            for pid in ids:
                self.containers[pid].add(seed)
        self.known0 = {pid for pid, p in self.people.items() if stamp(p['first_seen']) < cut}
        self.order = sorted(self.pool, key=lambda s: self.pool[s]['start'])


def candidate(ds, seed, processed, hide_size=False):
    person = ds.pool[seed]['person']
    parents = frozenset(s for s in ds.containers[person['id']] if s != seed and (s in ds.baseline or s in processed))
    return picker.Candidate(handle=seed, person_id=person['id'], following=None if hide_size else person['following'],
                            followers=person['followers'], verified=bool(person['is_verified']),
                            business=None if person['is_business'] is None and not person['category']
                            else bool(person['is_business'] or person['category']),
                            parents=parents, overlap=len(parents))


class State:
    def __init__(self, ds):
        self.ds, self.known, self.processed = ds, set(ds.known0), set()
        self.ctx = picker.Context()
        self.remaining = list(ds.order)
        self.spent, self.fresh, self.log = 0, 0, []
        self.predicted = {}
        self.queue = []                      # picked, waiting: learning lags the queue like production

    def take(self, seed, predicted=None):
        ds = self.ds
        entry = ds.pool[seed]
        new = entry['members'] - self.known
        ratio = len(new) / len(entry['members'])
        cand = candidate(ds, seed, self.processed)
        picker.update_context(self.ctx, cand.parents, ratio, len(entry['members']))
        self.known |= entry['members']
        self.processed.add(seed)
        self.remaining.remove(seed)
        self.log.append({'seed': seed, 'cost': entry['cost'], 'fresh': len(new), 'members': len(entry['members']),
                         'ratio': ratio, 'predicted': predicted, 'spent_before': self.spent})
        self.spent += entry['cost']
        self.fresh += len(new)


# ---- policies: (state, rng) -> (seed, predicted new people per request or None) ----

def avail(state):
    return [s for s in state.remaining if s not in state.queue]


def fifo(state, rng):
    return avail(state)[0], None


def random_pick(state, rng):
    return rng.choice(avail(state)), None


def most_overlap(state, rng):
    pool = avail(state)
    best = max(pool, key=lambda s: (candidate(state.ds, s, state.processed).overlap, -pool.index(s)))
    return best, None


def cheapest_first(state, rng):
    ds = state.ds
    pool = avail(state)
    best = min(pool, key=lambda s: (ds.pool[s]['person']['following'] is None,
                                    ds.pool[s]['person']['following'] or 0, pool.index(s)))
    return best, None


def largest_first(state, rng):
    ds = state.ds
    pool = avail(state)
    return max(pool, key=lambda s: (ds.pool[s]['person']['following'] or 0, -pool.index(s))), None


def oracle(state, rng):
    """Not a real policy: knows each list's true marginal yield. Upper bound for the pool."""
    ds = state.ds
    best = max(avail(state), key=lambda s: len(ds.pool[s]['members'] - state.known) / ds.pool[s]['cost'])
    return best, None


def adaptive(hide_size=False, **overrides):
    def pick(state, rng):
        with patched(**overrides):
            cands = [candidate(state.ds, s, state.processed, hide_size) for s in avail(state)]
            waiting = [candidate(state.ds, s, state.processed).parents for s in state.queue]
            chosen = picker.choose(cands, state.ctx, 1, pending_parents=waiting)
        if not chosen:       # every candidate outside the size limits: fall back to the oldest
            return avail(state)[0], None
        c, detail = chosen[0]
        return c.handle, detail['per_request']
    return pick


class patched:
    """Temporarily set picker constants, so the replay can try other weights on the same code."""

    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = getattr(picker, key)
            setattr(picker, key, value)

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            setattr(picker, key, value)


def run(ds, policy, seeds=(0,), pool=None, budget=max(BUDGETS), depth=2):
    """Cumulative new-people curves and logs. `depth` lists wait in the queue, as in production, so what a
    finished list taught the picker only reaches the next top-up."""
    curves, logs = [], []
    for seed in seeds:
        rng = random.Random(seed)
        state = State(ds)
        if pool is not None:
            state.remaining = [s for s in ds.order if s in pool]
        while (state.remaining or state.queue) and state.spent < budget:
            while len(state.queue) < depth and len(avail(state)) > 0:
                choice, predicted = policy(state, rng)
                state.queue.append(choice)
                state.predicted[choice] = predicted
            choice = state.queue.pop(0)
            state.take(choice, state.predicted.get(choice))
        curves.append(curve(state))
        logs.append(state.log)
    return curves, logs


def curve(state):
    """Cumulative new people at each budget, prorating the list that straddles it."""
    out = {}
    for budget in BUDGETS:
        total = 0.0
        for item in state.log:
            if item['spent_before'] >= budget:
                break
            share = min(1.0, (budget - item['spent_before']) / item['cost'])
            total += item['fresh'] * share
        out[budget] = total
    return out


def mean_curve(curves):
    return {b: sum(c[b] for c in curves) / len(curves) for b in BUDGETS}


def area(c):
    return sum(c.values())


def spearman(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 5:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out
    ra, rb = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return round(num / den, 3) if den else None


GRID = {'OVERLAP_WEIGHT': (0.0, 0.03, 0.06, 0.1, 0.15), 'SIZE_WEIGHT': (0.0, 0.1, 0.2), 'VERIFIED_WEIGHT': (0.0, 0.08, 0.2), 'NEIGHBOUR_SHRINK': (1.0, 3.0, 8.0),
        'EXPLORE_WEIGHT': (0.0, 0.05, 0.15), 'DIVERSITY_WEIGHT': (0.0, 0.25, 0.5)}


def objective(ds, pool, weights):
    """Area under the new-people curve, averaged over 'size known' and 'size hidden' (most real candidates
    have no saved following count, so both cases matter)."""
    return (area(run(ds, adaptive(**weights), pool=pool)[0][0])
            + area(run(ds, adaptive(hide_size=True, **weights), pool=pool)[0][0])) / 2


def tune(ds, pool):
    """Coordinate search over a fixed small grid."""
    best = {key: getattr(picker, key) for key in GRID}
    best_score = objective(ds, pool, best)
    for _ in range(2):
        for key, values in GRID.items():
            for value in values:
                trial = dict(best, **{key: value})
                got = objective(ds, pool, trial)
                if got > best_score + 1e-9:
                    best, best_score = trial, got
    return best, best_score


def evaluate(ds, folds=6, seed=7):
    """Honest check: tune on one random half of the pool, score on the other half (and swap), repeated."""
    rng = random.Random(seed)
    names = sorted(ds.pool)
    rows = []
    for fold in range(folds):
        rng.shuffle(names)
        half = len(names) // 2
        for train, test in ((set(names[:half]), set(names[half:])), (set(names[half:]), set(names[:half]))):
            weights, _ = tune(ds, train)
            res = {'tuned': weights}
            for label, pol in (('adaptive_tuned', adaptive(**weights)), ('adaptive_tuned_size_hidden', adaptive(hide_size=True, **weights)),
                               ('adaptive_default', adaptive()), ('adaptive_default_size_hidden', adaptive(hide_size=True)),
                               ('fifo', fifo), ('most_overlap', most_overlap), ('cheapest_first', cheapest_first),
                               ('largest_first', largest_first), ('oracle', oracle)):
                res[label] = mean_curve(run(ds, pol, pool=test)[0])
            res['random'] = mean_curve(run(ds, random_pick, seeds=range(40), pool=test)[0])
            rows.append(res)
    return rows


def summarise(rows):
    labels = [k for k in rows[0] if k != 'tuned']
    out = {}
    for label in labels:
        out[label] = {b: round(sum(r[label][b] for r in rows) / len(rows)) for b in BUDGETS}
    return out


def report(db_path, folds):
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    ds = Dataset(conn)
    result = {'pool_lists': len(ds.pool), 'baseline_lists': len(ds.baseline), 'known_at_start': len(ds.known0),
              'pool_requests': sum(v['cost'] for v in ds.pool.values())}
    full_budgets = tuple(BUDGETS)
    # 1. whole pool, default weights, all policies
    policies = {'fifo (actual order)': fifo, 'most_overlap_first': most_overlap, 'cheapest_first': cheapest_first,
                'largest_first': largest_first, 'adaptive (default weights)': adaptive(),
                'adaptive (size hidden)': adaptive(hide_size=True), 'oracle (upper bound)': oracle}
    result['whole_pool'] = {}
    for name, pol in policies.items():
        result['whole_pool'][name] = {b: round(v) for b, v in mean_curve(run(ds, pol)[0]).items()}
    result['whole_pool']['random (mean of 200)'] = {b: round(v) for b, v in mean_curve(run(ds, random_pick, seeds=range(200))[0]).items()}
    # 2. predicted vs realised per pick (adaptive, whole pool)
    log = run(ds, adaptive())[1][0]
    pred = [i['predicted'] for i in log]
    real = [i['fresh'] / i['cost'] for i in log]
    result['prediction'] = {'picks': len(log), 'spearman': spearman(pred, real),
                            'mean_predicted_per_request': round(sum(pred) / len(pred), 1),
                            'mean_realised_per_request': round(sum(real) / len(real), 1),
                            'top_third_realised': round(sum(sorted(real, reverse=True)[:len(real) // 3]) / (len(real) // 3), 1)}
    # 3. held-out evaluation with tuning
    rows = evaluate(ds, folds=folds)
    result['held_out'] = summarise(rows)
    result['held_out_runs'] = len(rows)
    result['tuned_weights_seen'] = [r['tuned'] for r in rows[:6]]
    result['budgets'] = list(full_budgets)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--folds', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(report(args.db, args.folds), indent=2))
