# Broad stage sidecar

The second of four qualification stages (Rules → **Broad** → Bulk → Special). It scores every person with a bio in
about a millisecond each, so the Bulk stage (Grok 4.7) spends its calls on the people most likely to be good leads.

How it works: Laya's multilingual encoder (`convaiinnovations/laya`, subfolder `multilingual`, frozen) turns a profile
into a vector; a small logistic head, trained on Grok's Bulk verdicts, turns that vector plus the rules score and
follower count into P(Grok would call this a good lead: buyer, fit ≥ 60).

Why not Laya's own yes/no questions: zero-shot they were no better than guessing on these profiles (Laya's model card
says the base checkpoints are near chance until specialised). Trained on 2,480 Grok labels, the head finds 74% of the
good leads in its top 30%, against 48% for the rules score.

## Run

    ops/install-broad.sh          # venv, sidecar LaunchAgent, nightly retrain at 04:30

By hand:

    sidecar/.venv/bin/python sidecar/train_broad.py      # labels: Grok verdicts in the DB + data/labels/*.jsonl
    sidecar/.venv/bin/python sidecar/broad_server.py     # 127.0.0.1:18742, loopback only
    curl -s 127.0.0.1:18742/health

The head lives in `data/broad_head.pkl`; the sidecar reloads it when it changes. A retrain only replaces it when the
new head finds at least as many good leads in its top 30% (within 2 points). Weights: `sidecar/.cache/huggingface`
(offline by default).

## API

`POST /decide` `{"items": [{"id", "person": {handle, name, category, bio, followers}, "rules": 0-100}]}` (≤ 512)
→ `{"model": "broad", "deployment_version", "results": [{"id", "p"}]}`. The server keys stored answers by
`deployment_version`, so a new head re-scores everyone (~36 min for 500k).
