# Broad stage sidecar

The second of four qualification stages (Rules → **Broad** → Bulk → Special). It scores every person with a bio in
about a millisecond each, so the Bulk stage (Grok 4.7) spends its calls on the people most likely to be good leads.

How it works: Laya's multilingual encoder (`convaiinnovations/laya`, subfolder `multilingual`, frozen) turns a profile
into a vector; a small logistic head, trained on Grok's Bulk verdicts, turns that vector plus profile-only rule signals and
follower count into a ranking score for Grok's good-lead label (buyer, fit ≥ 60). This score is not calibrated.

The 2,480 Grok labels and 40 later Claude corrections support a candidate ranking model. Cross-validation on those
labels measures agreement with Grok, and multiple model choices were tried on that same sample. It does not establish
that the ranking finds Michael's actual qualified clients. The head is therefore an experiment until independent review.

## Run

    ops/install-broad.sh          # venv and sidecar LaunchAgent; no automatic training

By hand:

    sidecar/.venv/bin/python sidecar/train_broad.py      # writes a candidate head, not the served head
    sidecar/.venv/bin/python sidecar/train_broad.py --promote  # serves that exact candidate after independent review
    sidecar/.venv/bin/python sidecar/broad_server.py     # 127.0.0.1:18742, loopback only
    curl -s 127.0.0.1:18742/health

The served head lives in `data/broad_head.pkl`; the sidecar reloads it when it changes. Training writes
`data/broad_candidate.pkl` by default. Grok verdicts and auto tags are never used as ranking features; the rule
features are re-derived from each profile in the same way at training and inference. Weights are cached in
`sidecar/.cache/huggingface` (offline by default).

## API

`POST /decide` `{"items": [{"id", "person": {handle, name, category, bio, followers}, "rules": 0-100}]}` (≤ 512)
→ `{"model": "broad", "deployment_version", "results": [{"id", "p"}]}`. The server keys stored answers by
`deployment_version`, so a new head re-scores everyone (~36 min for 500k).
