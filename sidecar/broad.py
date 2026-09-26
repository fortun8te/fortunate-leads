"""Broad stage model: Laya's multilingual encoder (frozen) + a small head trained on the Bulk stage's Grok verdicts.

Measured on 2,480 labels (Grok, 40 corrected by hand review), 5-fold: the top 10% by Broad holds 62% of the good leads
(buyer, fit >= 60) and the top 30% holds 88%, against 24% and 51% for the rules score. ~265 profiles/s on an M1 Max
(500k in ~32 min).
Shared by broad_server.py (inference) and train_broad.py (training) so both build the same text and features."""
import math
import os
import pickle
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault('HF_HOME', str(HERE / '.cache' / 'huggingface'))
os.environ.setdefault('HF_HUB_OFFLINE', '1')

HEAD = Path(os.environ.get('BROAD_HEAD', HERE.parent / 'data' / 'broad_head.pkl'))
FEATURE_VERSION = 'profile-rules-v1'
CHECKPOINT, SUBFOLDER = 'convaiinnovations/laya', 'multilingual'
FEATURE_WEIGHT = 3   # rules features scaled next to the unit-norm embedding
MAX_LEN = 96         # bios are short: same quality as 128 tokens, ~20% faster
# Rule tags the head sees (one 0/1 column each): they carry the network-free facts the encoder is weak at.
TAGS = ('Verified', 'Business', 'Too big', 'Founder', 'Link Hub', 'US', 'Email', 'Coach', 'Creative', 'UK', 'Ecom', 'Creator', 'NL',
        'Agency', 'Other market', 'Brand', 'Scaling', 'Shop Link', 'SaaS', 'Personal', 'Freelancer', 'Supplier', 'Store', 'Shopify',
        'Hiring', 'Fitness', 'Beauty', 'Apparel', 'Wellness', 'Food & Drink', 'Supplements', 'Jewelry', 'Skincare', 'Home')


def text(p):
    size = f"{p['followers']:,} followers" if isinstance(p.get('followers'), int) else ''
    return ' | '.join(str(x).strip() for x in ('@' + str(p.get('handle') or ''), p.get('name'), p.get('category'), size,
                                                str(p.get('bio') or '').replace('\n', ' ')[:300]) if x)


def features(rules, followers, tags=()):
    tags = set(tags or ())
    return ([FEATURE_WEIGHT * (rules or 0) / 100, FEATURE_WEIGHT * math.log10(max(10, followers or 10)) / 7]
            + [1.0 if t in tags else 0.0 for t in TAGS])


class Model:
    def __init__(self, device=None):
        import laya
        from laya.shortlist import embed_fn_from_agent
        self.agent = laya.load(CHECKPOINT, device=device or _device(), subfolder=SUBFOLDER)
        self.embed_fn = embed_fn_from_agent(self.agent, max_length=MAX_LEN, batch_size=128)
        self.head = None
        self.reload()

    def reload(self):
        head = pickle.loads(HEAD.read_bytes()) if HEAD.exists() else None
        self.head = head if head and head.get('tags') == list(TAGS) and head.get('feature_version') == FEATURE_VERSION else None
        return self.head is not None

    @property
    def version(self):
        return self.head['version'] if self.head else None

    def embed(self, people):
        import numpy as np
        x = self.embed_fn([text(p) for p in people])
        return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)

    def matrix(self, people, rules, tags):
        import numpy as np
        return np.hstack([self.embed(people), np.array([features(r, p.get('followers'), t) for p, r, t in zip(people, rules, tags)])])

    def score(self, people, rules, tags):
        """Return uncalibrated ranking scores; the balanced head is not a probability of a qualified client."""
        return self.head['clf'].predict_proba(self.matrix(people, rules, tags))[:, 1].tolist()


def _device():
    import torch
    return 'mps' if torch.backends.mps.is_available() else 'cuda' if torch.cuda.is_available() else 'cpu'
