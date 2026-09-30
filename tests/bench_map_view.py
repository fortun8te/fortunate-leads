"""Measure viewport responses on an isolated, synthetic database."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'server'),
                str(Path(__file__).resolve().parents[1] / 'server' / 'tests')]
import db
import map_layout
import map_view
from map_view_fixture import make


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--people', type=int, default=100000)
    parser.add_argument('--runs', type=int, default=5)
    args = parser.parse_args()
    if args.people < 1 or args.runs < 1:
        parser.error('people and runs must be positive')
    with tempfile.TemporaryDirectory(prefix='fortunate-map-view-') as directory:
        path = Path(directory) / 'synthetic.sqlite'
        start = time.perf_counter()
        make(path, people=args.people)
        output = {'people_requested': args.people, 'fixture_seconds': round(time.perf_counter()-start, 2),
                  'build': map_layout.build(path, workers=1), 'views': {}}
        conn = db.connect(path)
        try:
            for mode in map_layout.MODES:
                samples = []
                for _ in range(args.runs):
                    start = time.perf_counter()
                    result = map_view.view(conn, path, {'mode':[mode], 'scope':['all']}, cache=False)
                    samples.append(round((time.perf_counter()-start)*1000, 2))
                body = json.loads(result.body)
                assert sum(c['count'] for c in body['clusters']) == body['hidden']
                output['views'][mode] = {'samples_ms': samples, 'response_bytes': len(result.body),
                                        'total': body['total'], 'shown': body['shown'], 'hidden': body['hidden']}
        finally:
            conn.close()
            map_view.close_pool()
        print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
