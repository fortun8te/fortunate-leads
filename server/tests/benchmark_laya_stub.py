"""Measure local Laya HTTP overhead without loading a model or reading the live DB.

Run from the repository root: python3 server/tests/benchmark_laya_stub.py
"""
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'sidecar'))
import laya  # noqa: E402
import laya_server  # noqa: E402


class StubBackend:
    model = laya.MODEL
    device = 'cpu-stub'

    def __init__(self):
        self.calls = 0
        self.items = 0

    def predict_batch(self, states, questions):
        self.calls += 1
        self.items += len(states)
        return [{'answers': {key: {'noul': 0.5} for key in questions}} for _ in states]


def measure(fn, repeats):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {'runs': repeats, 'median_ms': round(statistics.median(samples), 3),
            'p95_ms': round(samples[int(0.95 * (len(samples) - 1))], 3)}


def main():
    backend = StubBackend()
    server = laya_server.make_server(backend, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    people64 = [{'id': i + 1, 'handle': f'person{i + 1}', 'bio': 'Founder of a Dutch product brand'} for i in range(64)]
    people512 = [{'id': i + 1, 'handle': f'person{i + 1}', 'bio': 'Founder of a Dutch product brand'} for i in range(512)]
    try:
        with patch.object(laya, 'URL', f'http://127.0.0.1:{server.server_port}'):
            def cold64():
                laya.reset()
                assert laya.available() and len(laya.decide(people64)) == 64

            def warm64():
                assert laya.available() and len(laya.decide(people64)) == 64

            def large512():
                assert len(laya.decide(people512)) == 512

            metrics = {'cold_health_plus_64': measure(cold64, 20),
                       'warm_health_plus_64': measure(warm64, 30),
                       'warm_512': measure(large512, 10)}
            calls = {'backend_calls': backend.calls, 'backend_items': backend.items,
                     'client_batch_size': laya.BATCH}
            print(json.dumps({'metrics': metrics, 'calls': calls}, sort_keys=True))
    finally:
        laya.reset()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
