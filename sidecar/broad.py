"""Broad stage model: Laya's multilingual encoder (frozen) + a small head trained on the Bulk stage's Grok verdicts.

Measured on 2,480 Grok-labelled leads (5-fold): the top 30% by Broad holds 74% of Grok's good leads (buyer, fit >= 60),
against 48% for the rules score alone. Embedding runs ~230 profiles/s on an M1 Max (500k in ~36 min).
Shared by broad_server.py (inference) and train_broad.py (training) so both build the same text and features."""
import math
import os
import pickle
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault('HF_HOME', str(HERE / '.cache' / 'huggingface'))
os.environ.setdefault('HF_HUB_OFFLINE', '1')

HEAD = Path(os.environ.get('BROAD_HEAD', HERE.parent / 'data' / 'broad_head.pkl'))
CHECKPOINT, SUBFOLDER = 'convaiinnovations/laya', 'multilingual'
FEATURE_WEIGHT = 3   # rules features scaled next to the unit-norm embedding


def text(p):
    size = f"{p['followers']:,} followers" if isinstance(p.get('followers'), int) else ''
    return ' | '.join(str(x).strip() for x in ('@' + str(p.get('handle') or ''), p.get('name'), p.get('category'), size,
                                                str(p.get('bio') or '').replace('\n', ' ')[:300]) if x)


def features(rules, followers):
    return [(rules or 0) / 100, math.log10(max(10, followers or 10)) / 7]


class Model:
    def __init__(self, device=None):
        import laya
        from laya.shortlist import embed_fn_from_agent
        self.agent = laya.load(CHECKPOINT, device=device or _device(), subfolder=SUBFOLDER)
        self.embed_fn = embed_fn_from_agent(self.agent, max_length=128, batch_size=64)
        self.head = None
        self.reload()

    def reload(self):
        self.head = pickle.loads(HEAD.read_bytes()) if HEAD.exists() else None
        return self.head is not None

    @property
    def version(self):
        return self.head['version'] if self.head else None

    def embed(self, people):
        import numpy as np
        x = self.embed_fn([text(p) for p in people])
        return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)

    def score(self, people, rules):
        """people: dicts with handle/name/category/bio/followers; rules: rules score per person -> P(good lead) each."""
        import numpy as np
        x = np.hstack([self.embed(people), np.array([features(r, p.get('followers')) for p, r in zip(people, rules)]) * FEATURE_WEIGHT])
        return self.head['clf'].predict_proba(x)[:, 1].tolist()


def _device():
    import torch
    return 'mps' if torch.backends.mps.is_available() else 'cuda' if torch.cuda.is_available() else 'cpu'
