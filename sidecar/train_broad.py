"""Train the Broad head on the Bulk stage's Grok verdicts (the teacher), report held-out quality, save data/broad_head.pkl.

Labels, later sources overriding earlier ones for the same person:
  1. every person with a Grok verdict in the leads database
  2. data/labels/*.jsonl in name order (Grok batches, then hand reviews: "by" set -> weight 2)
  3. Michael's own marks (interested/talking/client = good, no = not) -> weight 3: his feedback always wins
A good lead = buyer with fit >= 60. The saved head only replaces the old one when it finds at least as many
good leads in its top 30% as the one in use (checked on the same folds).
usage: sidecar/.venv/bin/python sidecar/train_broad.py [--db data/leads.sqlite] [--min 300]"""
import argparse
import hashlib
import json
import pickle
import sqlite3
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings('ignore')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import broad  # noqa: E402  (sets HF_HOME before laya loads)

GOOD_FIT = 60


def labels(db_path):
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    rows = {}
    for r in con.execute("SELECT p.id, p.handle, p.name, p.category, p.bio, p.followers, v.role, v.content_fit AS fit "
                         "FROM verdicts v JOIN people p ON p.id=v.person_id WHERE v.model LIKE 'grok%' AND coalesce(p.bio,'')!=''"):
        rows[r['id']] = dict(r, w=1.0)
    for f in sorted((broad.HEAD.parent / 'labels').glob('*.jsonl')):
        for line in f.read_text().split('\n'):   # not splitlines(): bios contain U+2028
            if not line.strip():
                continue
            r = json.loads(line)
            rows[r['id']] = dict({k: r.get(k) for k in ('id', 'handle', 'name', 'category', 'bio', 'followers', 'role', 'fit')},
                                 w=2.0 if r.get('by') else 1.0)
    for r in con.execute("SELECT p.id, p.handle, p.name, p.category, p.bio, p.followers, m.status FROM marks m JOIN people p "
                         "ON p.id=m.person_id WHERE m.status IN ('interested','talking','client','no') AND coalesce(p.bio,'')!=''"):
        good = r['status'] != 'no'
        rows[r['id']] = dict({k: r[k] for k in ('id', 'handle', 'name', 'category', 'bio', 'followers')},
                             role='buyer' if good else 'unrelated', fit=85 if good else 10, w=3.0)
    rules = dict(con.execute("SELECT person_id, coalesce(content_fit, score) FROM verdicts"))
    tags = {}
    for pid, t in con.execute("SELECT person_id, tag FROM tags WHERE source='auto'"):
        tags.setdefault(pid, []).append(t)
    con.close()
    out = [r for r in rows.values() if r.get('role') and isinstance(r.get('fit'), (int, float))]
    return out, [rules.get(r['id']) or 0 for r in out], [tags.get(r['id'], []) for r in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(broad.HERE.parent / 'data' / 'leads.sqlite'))
    ap.add_argument('--min', type=int, default=300)
    a = ap.parse_args()
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold

    rows, rules, tags = labels(a.db)
    w = np.array([r['w'] for r in rows])
    good = np.array([r['role'] == 'buyer' and r['fit'] >= GOOD_FIT for r in rows])
    if len(rows) < a.min or good.sum() < 20:
        print(f'only {len(rows)} labels ({good.sum()} good): need {a.min}+ with 20+ good')
        return 1
    model = broad.Model()
    t = time.time()
    x = model.matrix(rows, rules, tags)
    print(f'{len(rows)} labels ({good.sum()} good), embedded in {time.time() - t:.0f}s')

    def head():
        return LogisticRegression(C=8.0, max_iter=4000, class_weight='balanced')

    def top30(score):
        k = max(1, int(len(score) * .3))
        return good[np.argsort(-score)[:k]].sum() / good.sum()

    new, old = np.zeros(len(rows)), np.zeros(len(rows))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(x, good):
        new[te] = head().fit(x[tr], good[tr], sample_weight=w[tr]).predict_proba(x[te])[:, 1]
    base = np.array(rules, dtype=float)
    q_new, q_rules = top30(new), top30(base)
    q_old = top30(np.array(model.score(rows, rules, tags))) if model.head and model.head.get('tags') == list(broad.TAGS) else 0   # optimistic for the old head: it may have seen these
    print(f'good leads in top 30%: new {q_new:.0%} | rules {q_rules:.0%} | current head {q_old:.0%}' if model.head else
          f'good leads in top 30%: new {q_new:.0%} | rules {q_rules:.0%}')
    if model.head and q_new + 0.02 < q_old:
        print('kept the current head')
        return 0
    clf = head().fit(x, good, sample_weight=w)
    version = 'broad-' + hashlib.sha256(pickle.dumps(clf.coef_)).hexdigest()[:10]
    broad.HEAD.parent.mkdir(parents=True, exist_ok=True)
    tmp = broad.HEAD.with_suffix('.tmp')
    tmp.write_bytes(pickle.dumps({'clf': clf, 'version': version, 'n': len(rows), 'good': int(good.sum()), 'top30': q_new,
                                  'rules_top30': q_rules, 'tags': list(broad.TAGS), 'at': time.time()}))
    tmp.replace(broad.HEAD)
    print('saved', version, 'to', broad.HEAD)
    return 0


if __name__ == '__main__':
    sys.exit(main())
