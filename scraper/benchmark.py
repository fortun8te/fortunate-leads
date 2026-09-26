#!/usr/bin/env python3
"""Bounded profile-engine benchmark. Default is OFFLINE: no Instagram requests.

Offline timing models the configured response latency and per-lane concurrency, not
Instagram/Tor capacity. --live requires a supplied handles file, at most 32 profiles,
and keeps the engine's configured rate limits, cooldowns, and shared-lane limits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'app'))
import httpx
from igscraper import ProfileScraper, ScraperConfig
from igscraper.inputs import dedupe, parse_target
from igscraper.mapper import with_coverage
from igscraper.store import Store
from igscrape_app.runner import build_config, parse_usernames


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live', action='store_true')
    p.add_argument('--handles-file', type=Path)
    p.add_argument('--profiles', type=int, default=None)
    p.add_argument('--seconds', type=float, default=20)
    p.add_argument('--latency-ms', type=float, default=400, help='offline simulated response latency')
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument('--inflight', type=int, default=2, help='offline per-lane simultaneous requests')
    p.add_argument('--cache-fraction', type=float, default=.1, help='offline cached fraction')
    p.add_argument('--egresses', help='live egress YAML, parsed by existing app configuration')
    p.add_argument('--output', type=Path)
    return p


def validate(args):
    if not 0 < args.seconds <= 120:
        raise ValueError('--seconds must be between 0 and 120')
    if not 1 <= args.concurrency <= 64 or not 1 <= args.inflight <= 64:
        raise ValueError('concurrency and inflight must be 1..64')
    if args.latency_ms < 0 or not 0 <= args.cache_fraction <= 1:
        raise ValueError('latency must be nonnegative and cache fraction must be 0..1')
    if args.live:
        if not args.handles_file:
            raise ValueError('--live requires --handles-file with user-supplied handles')
        names = dedupe(parse_usernames(args.handles_file.read_text()))
        limit = args.profiles if args.profiles is not None else 32
        if not 1 <= limit <= 32:
            raise ValueError('live benchmark is limited to 1..32 profiles')
        names = names[:limit]
        if not names:
            raise ValueError('handles file is empty')
        for name in names:
            parse_target(name)
        return names
    n = args.profiles if args.profiles is not None else 120
    if not 1 <= n <= 10000:
        raise ValueError('offline profiles must be 1..10000')
    return [f'benchmark_{i:05}' for i in range(n)]


def summarize(items, total, elapsed):
    cached = sum(not x.get('error') and bool(x.get('_scrape', {}).get('cacheHit')) for x in items)
    partial = sum(x.get('error') == 'incomplete_profile' for x in items)
    errors = sum(bool(x.get('error')) and x.get('error') != 'incomplete_profile' for x in items)
    fresh = sum(not x.get('error') and not x.get('_scrape', {}).get('cacheHit')
                and x.get('_scrape', {}).get('bioStatus') in ('observed', 'empty') for x in items)
    return {'requested': total, 'processed': len(items), 'unfinished': max(0, total-len(items)),
            'freshSuccess': fresh, 'cached': cached, 'partial': partial, 'errors': errors,
            'freshEmptyBio': sum(not x.get('error') and not x.get('_scrape', {}).get('cacheHit')
                                 and x.get('_scrape', {}).get('bioStatus') == 'empty' for x in items),
            'elapsedSeconds': round(elapsed, 3),
            'freshBiosPerMinute': round(fresh/max(elapsed, .000001)*60, 2),
            'processedPerMinute': round(len(items)/max(elapsed, .000001)*60, 2)}


async def benchmark(args):
    names = validate(args)
    with tempfile.TemporaryDirectory(prefix='ig-benchmark-') as scratch:
        if args.live:
            cfg, source = build_config(egress_file=args.egresses, concurrency=args.concurrency, cache=False)
            # A benchmark always observes fresh results and cannot reuse a prior durable job.
            cfg.job = None
            factory = None
        else:
            source = 'synthetic mock transport; one simulated connection'
            cfg = ScraperConfig(concurrency=args.concurrency, max_inflight_per_egress=args.inflight,
                                rate_per_ip=0, max_retries=0, health_interval=0,
                                cache_path=str(Path(scratch)/'cache.sqlite'))
            st = Store(cfg.cache_path)
            for name in names[:int(len(names)*args.cache_fraction)]:
                st.cache_put(with_coverage({'username': name, 'id': name, 'biography': 'Previously observed'}, 'synthetic'))
            st.close()
            async def response(request):
                await asyncio.sleep(args.latency_ms/1000)
                name = request.url.params.get('username', '')
                i = int(name.rsplit('_', 1)[-1])
                if i % 11 == 10:
                    return httpx.Response(404, text='synthetic missing profile')
                user = {'id': str(100000+i), 'username': name, 'is_private': i % 7 == 0,
                        'full_name': f'Benchmark {i}', 'biography': '' if i % 13 == 0 else 'Synthetic public profile'}
                if i % 17 == 16:
                    user.pop('biography')
                return httpx.Response(200, json={'data': {'user': user}, 'status': 'ok'})
            factory = lambda _: httpx.MockTransport(response)
        async with ProfileScraper(cfg, transport_factory=factory) as engine:
            started = time.monotonic()
            items = await asyncio.wait_for(engine.run(names, return_partial_on_cancel=True), timeout=args.seconds)
            elapsed = time.monotonic()-started
            report = {'mode': 'live' if args.live else 'offline', 'configurationSource': source,
                      'networkVerified': bool(args.live),
                      'claim': 'Measured fresh profile observations' if args.live else
                               'Synthetic timing only; does not verify Instagram or Tor throughput',
                      **summarize(items, len(names), elapsed),
                      'concurrency': cfg.concurrency, 'maxInflightPerLane': cfg.max_inflight_per_egress,
                      'timeLimitSeconds': args.seconds, 'stats': engine.stats.as_dict(),
                      'target200ReachedInThisSample': False, 'target500ReachedInThisSample': False,
                      'errorCounts': {code: sum(i.get('error') == code for i in items)
                                      for code in sorted({i['error'] for i in items if i.get('error')})}}
            report['target200ReachedInThisSample'] = report['freshBiosPerMinute'] >= 200
            report['target500ReachedInThisSample'] = report['freshBiosPerMinute'] >= 500
            if not args.live:
                report['simulatedLatencyMs'] = args.latency_ms
                report['idealRequestCeilingPerMinute'] = round(min(args.concurrency, args.inflight)*60000/max(args.latency_ms, .001), 2)
            else:
                # Retain real observations for ingestion instead of discarding a measured sample.
                report['items'] = items
            return report


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        report = asyncio.run(benchmark(args))
    except ValueError as exc:
        p.error(str(exc))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text+'\n')
    print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
