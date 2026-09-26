#!/usr/bin/env python3
"""Offline Laya load/inference smoke test; never starts an HTTP service.

Run with sidecar/.venv/bin/python and a local cached snapshot directory.
The snapshot is read only; Hugging Face and Torch scratch paths are temporary.
"""
import argparse
import json
import os
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path


def rss_mb():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 * 1024 if sys.platform == 'darwin' else 1024), 1)


def mirror_snapshot(source, target):
    """Keep mutable model metadata in scratch while reusing large weights read only."""
    for item in source.rglob('*'):
        dest = target / item.relative_to(source)
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item.name.endswith('.safetensors'):
                dest.symlink_to(item.resolve())
            else:
                shutil.copy2(item, dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path, help='local cached repo snapshot (contains multilingual/)')
    parser.add_argument('--device', choices=('cpu', 'mps', 'cuda'), default='cpu')
    parser.add_argument('--sizes', default='1,64,256', help='comma-separated synthetic batch sizes')
    args = parser.parse_args()
    if not (args.snapshot / 'multilingual' / 'model.safetensors').is_file():
        parser.error('multilingual checkpoint is missing from this snapshot')
    sizes = [int(n) for n in args.sizes.split(',')]
    if not sizes or any(n <= 0 for n in sizes):
        parser.error('sizes must be positive')

    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='laya-offline-smoke-') as scratch:
        os.environ['HF_HOME'] = scratch
        os.environ['TORCH_HOME'] = scratch
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        import laya_server

        mirrored = Path(scratch) / 'snapshot'
        mirror_snapshot(args.snapshot, mirrored)
        question_list = [
            {'key': 'dtc_founder', 'q': 'Is this person a founder, owner or decision-maker of a direct-to-consumer brand that sells its own physical products?'},
            {'key': 'brand_account', 'q': 'Is this the official account of a brand or company rather than a personal account?'},
            {'key': 'creator', 'q': 'Is this person mainly a content creator, influencer or UGC creator who makes content for other brands?'},
            {'key': 'service_provider', 'q': 'Is this an agency, freelancer, consultant or other service provider selling services rather than products?'},
            {'key': 'netherlands', 'q': 'Is this person or business based in the Netherlands?'},
        ]
        questions = laya_server.to_laya_questions(question_list)
        before = rss_mb()
        started = time.perf_counter()
        backend = laya_server.LayaBackend(str(mirrored) + ':multilingual', args.device)
        print(json.dumps({'phase': 'loaded', 'device': backend.device, 'load_seconds': round(time.perf_counter() - started, 3),
                          'peak_rss_mb_before': before, 'peak_rss_mb_loaded': rss_mb()}), flush=True)
        for count in sizes:
            states = [f'Handle: @sample{i}\nName: Sample {i}\nBio: Dutch founder of a skincare brand'
                      for i in range(count)]
            started = time.perf_counter()
            raw = backend.predict_batch(states, questions)
            seconds = time.perf_counter() - started
            if not isinstance(raw, list) or len(raw) != count:
                raise RuntimeError('model returned an incomplete batch')
            valid = sum(1 for row in raw if len(laya_server.from_laya_answers(question_list, row['answers'],
                                                                               set(questions))) == len(question_list))
            if valid != count:
                raise RuntimeError('model returned incomplete answers')
            print(json.dumps({'phase': 'batch', 'items': count, 'seconds': round(seconds, 3),
                              'items_per_second': round(count / seconds, 2), 'peak_rss_mb': rss_mb()}), flush=True)


if __name__ == '__main__':
    main()
