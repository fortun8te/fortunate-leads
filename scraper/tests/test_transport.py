import asyncio
import time
import unittest
from datetime import datetime, timezone
import httpx
from igscraper.net import AdaptiveLimiter, EgressPool, Fetcher, Stats, retry_after_seconds
from igscraper.egress import EgressSpec


def pool(handler, **kw):
    specs = kw.pop('specs', [EgressSpec('direct')])
    return EgressPool(specs, limiter_factory=lambda _: AdaptiveLimiter(mode='unlimited'),
        timeout=1, http2=False, max_failures=3, park_cooldown=900, park_max=14400,
        max_park_wait=0, transport_factory=lambda _: httpx.MockTransport(handler), **kw)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_tor_backoff_reduces_rate(self):
        limiter = AdaptiveLimiter(start_rpm=10, max_rpm=15)
        limiter.on_block()
        self.assertEqual(limiter.rpm, 5)
        limiter.on_block()
        self.assertLessEqual(limiter.rpm, 5)

    def test_retry_after_date_and_seconds(self):
        self.assertEqual(retry_after_seconds('3600'), 3600)
        self.assertEqual(retry_after_seconds('Thu, 24 Sep 2026 15:00:00 GMT',
            datetime(2026,9,24,14,tzinfo=timezone.utc)), 3600)
        self.assertIsNone(retry_after_seconds('nan'))
        self.assertIsNone(retry_after_seconds('invalid'))

    async def test_tor_retains_retry_after(self):
        p = pool(lambda r: httpx.Response(200), specs=[EgressSpec('proxy',
            'socks5://test:x@127.0.0.1:9152', tor=True)])
        try:
            self.assertGreaterEqual(p.block(p.egresses[0], '429', severity='rate', retry_after=3600),3600)
            self.assertGreater(p.health()[0]['parkedForS'], 3500)
        finally:
            await p.aclose()

    async def test_other_backoff_retains_retry_after(self):
        p = pool(lambda r: httpx.Response(200))
        try:
            self.assertGreaterEqual(p.block(p.egresses[0], '429', severity='rate', retry_after=3600),3600)
            p.transient(p.egresses[0], '503', retry_after=5)
            self.assertGreater(p.health()[0]['parkedForS'],3500)
        finally:
            await p.aclose()

    async def test_inflight_bound_and_release(self):
        active = peak = 0
        async def handler(req):
            nonlocal active, peak
            active += 1
            peak = max(peak,active)
            await asyncio.sleep(.03)
            active -= 1
            return httpx.Response(200,json={'ok':True})
        p = pool(handler,max_inflight_per_egress=2)
        f = Fetcher(p,max_retries=0,backoff_base=0,backoff_max=0,stats=Stats())
        try:
            await asyncio.gather(*(f.get('https://example.test/',{},expect_json=True) for _ in range(12)))
            self.assertEqual(peak,2)
            self.assertEqual(p.egresses[0].inflight,0)
        finally:
            await p.aclose()

    async def test_cancel_releases_slot(self):
        entered = asyncio.Event()
        async def handler(req):
            entered.set()
            await asyncio.sleep(100)
        p = pool(handler)
        f = Fetcher(p,max_retries=0,backoff_base=0,backoff_max=0,stats=Stats())
        task = asyncio.create_task(f.get('https://example.test/',{},expect_json=True))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(p.egresses[0].inflight,0)
        await p.aclose()

    async def test_egress_400_counts_one_request(self):
        p = pool(lambda r: httpx.Response(400,text='proxy error'))
        stats=Stats()
        f=Fetcher(p,max_retries=0,backoff_base=0,backoff_max=0,stats=stats)
        try:
            with self.assertRaises(Exception):
                await f.get('https://example.test/',{},expect_json=True)
            self.assertEqual(stats.requests,1)
        finally:
            await p.aclose()

    async def test_retired_lane_reprobes_without_failing_queue(self):
        p = pool(lambda r: httpx.Response(200))
        p.max_park_wait = None
        e = p.egresses[0]
        e.retired = True
        e.retired_until = time.monotonic() + .01
        try:
            got = await asyncio.wait_for(p.acquire(), 1)
            self.assertIs(got,e)
            self.assertFalse(e.retired)
            p.release(got)
        finally:
            await p.aclose()

    async def test_retired_reprobe_precedes_parked_lane(self):
        p = pool(lambda r: httpx.Response(200), specs=[
            EgressSpec('direct', name='parked'),
            EgressSpec('proxy', 'http://127.0.0.1:8888', name='reprobe')])
        p.max_park_wait = .2
        parked, reprobe = p.egresses
        parked.parked_until = time.monotonic() + 60
        reprobe.retired = True
        reprobe.retired_until = time.monotonic() + .02
        try:
            got = await asyncio.wait_for(p.acquire(), .3)
            self.assertIs(got, reprobe)
            p.release(got)
        finally:
            await p.aclose()

    async def test_cancel_while_waiting_does_not_take_slot(self):
        p = pool(lambda r: httpx.Response(200), max_inflight_per_egress=1)
        try:
            held = await p.acquire()
            waiting = asyncio.create_task(p.acquire())
            await asyncio.sleep(.01)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(p.egresses[0].inflight, 1)
            p.release(held)
            self.assertEqual(p.egresses[0].inflight, 0)
            self.assertFalse(p._client_inflight)
        finally:
            await p.aclose()

    async def test_fast_lane_continues_while_slow_lane_is_full(self):
        entered, release_slow = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            lane = request.headers['x-lane']
            calls.append(lane)
            if lane == 'slow':
                entered.set()
                await release_slow.wait()
            return httpx.Response(200, json={'status': 'ok'})
        p = pool(handler, specs=[
            EgressSpec('direct', name='slow', headers={'x-lane': 'slow'}),
            EgressSpec('direct', name='fast', headers={'x-lane': 'fast'})],
            max_inflight_per_egress=1)
        f = Fetcher(p, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
        try:
            slow = asyncio.create_task(f.get('https://example.test/', {}, expect_json=True))
            await entered.wait()
            await asyncio.wait_for(asyncio.gather(*[
                f.get('https://example.test/', {}, expect_json=True) for _ in range(3)]), .3)
            self.assertEqual(calls, ['slow', 'fast', 'fast', 'fast'])
            self.assertFalse(slow.done())
            release_slow.set()
            await slow
            self.assertEqual([e.inflight for e in p.egresses], [0, 0])
        finally:
            release_slow.set()
            await p.aclose()

    async def test_idle_rotations_do_not_keep_closed_client_bookkeeping(self):
        p = pool(lambda r: httpx.Response(200), specs=[EgressSpec('proxy',
            'socks5://t:x@127.0.0.1:9152', tor=True)])
        try:
            old = p.egresses[0].client
            p._rotate_tor(p.egresses[0], 'test')
            for _ in range(20):
                if old.is_closed:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(old.is_closed)
            self.assertNotIn(old, p._client_inflight)
            self.assertNotIn(old, p._client_drained)
        finally:
            await p.aclose()

    async def test_server_wait_on_503_is_not_capped(self):
        p = pool(lambda r: httpx.Response(503,headers={'retry-after':'3600'}))
        f = Fetcher(p,max_retries=0,backoff_base=0,backoff_max=30,stats=Stats())
        try:
            with self.assertRaises(Exception):
                await f.get('https://example.test/',{},expect_json=True)
            self.assertGreater(p.health()[0]['parkedForS'],3500)
        finally:
            await p.aclose()

    async def test_late_old_circuit_success_does_not_reset_new_health(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def handler(req):
            started.set()
            await finish.wait()
            return httpx.Response(200,json={'status':'ok'})
        p = pool(handler, specs=[EgressSpec('proxy','socks5://t:x@127.0.0.1:9152',tor=True)])
        f = Fetcher(p,max_retries=0,backoff_base=0,backoff_max=0,stats=Stats())
        task = asyncio.create_task(f.get('https://example.test/',{},expect_json=True))
        await started.wait()
        e = p.egresses[0]
        p.block(e,'429',severity='rate',retry_after=60)
        finish.set()
        try:
            await task
            self.assertEqual(e.ok,0)
            self.assertEqual(e.tor_streak,1)
            self.assertEqual(e.inflight,0)
        finally:
            await p.aclose()

    async def test_late_old_circuit_cooldowns_reach_outcome_hooks(self):
        for status in (429, 503, 403):
            with self.subTest(status=status):
                started, finish = asyncio.Event(), asyncio.Event()
                async def handler(req):
                    started.set()
                    await finish.wait()
                    return httpx.Response(status, headers={'retry-after': '3600'})
                p = pool(handler, specs=[EgressSpec('proxy',
                    'socks5://t:x@127.0.0.1:9152', tor=True)])
                f = Fetcher(p, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
                task = asyncio.create_task(f.get('https://example.test/', {}, expect_json=True))
                await started.wait()
                e = p.egresses[0]
                p.block(e, 'first 429', severity='rate')
                hook_name = 'transient' if status == 503 else 'block'
                original = getattr(p, hook_name)
                recorded = []
                def record(*args, **kwargs):
                    result = original(*args, **kwargs)
                    recorded.append(p.health()[0]['parkedForS'])
                    return result
                setattr(p, hook_name, record)
                finish.set()
                try:
                    with self.assertRaises(Exception):
                        await task
                    self.assertGreater(p.health()[0]['parkedForS'], 3500)
                    self.assertGreater(recorded[-1], 3500)
                    self.assertEqual(e.spec.rotation, 1)
                finally:
                    await p.aclose()

    async def test_rotated_clients_close_only_after_their_requests_drain(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def handler(req):
            started.set()
            await finish.wait()
            return httpx.Response(200, json={'status': 'ok'})
        p = pool(handler, specs=[EgressSpec('proxy',
            'socks5://t:x@127.0.0.1:9152', tor=True)])
        f = Fetcher(p, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
        e = p.egresses[0]
        original = e.client
        task = asyncio.create_task(f.get('https://example.test/', {}, expect_json=True))
        await started.wait()
        p.timeout = -.99  # Compress the old timeout+1 retirement timer.
        p._rotate_tor(e, 'first rotation')
        p._rotate_tor(e, 'second rotation')
        try:
            await asyncio.sleep(.03)
            self.assertFalse(original.is_closed, 'an in-flight old client must remain open')
        finally:
            finish.set()
            await task
            await asyncio.sleep(.01)
            drained_closed = original.is_closed
            await p.aclose()
        self.assertTrue(drained_closed)

    async def test_503_server_wait_parks_lane_without_stalling_healthy_retry(self):
        calls = 0
        async def handler(req):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, headers={'retry-after': '3600'})
            return httpx.Response(200, json={'status': 'ok'})
        p = pool(handler, specs=[EgressSpec('direct', name='a'), EgressSpec('direct', name='b')])
        stats = Stats()
        f = Fetcher(p, max_retries=1, backoff_base=0, backoff_max=0, stats=stats)
        try:
            response = await asyncio.wait_for(f.get('https://example.test/', {}, expect_json=True), .3)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(stats.requests, 2)
            self.assertEqual(stats.retries, 1)
            self.assertGreater(p.health()[0]['parkedForS'], 3500)
            self.assertEqual(p.egresses[1].ok, 1)
        finally:
            await p.aclose()

    async def test_late_retry_after_after_rotation_rest_expires_does_not_rotate_again(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def handler(req):
            started.set()
            await finish.wait()
            return httpx.Response(429, headers={'retry-after': '3600'})
        p = pool(handler, specs=[EgressSpec('proxy',
            'socks5://t:x@127.0.0.1:9152', tor=True)])
        f = Fetcher(p, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
        task = asyncio.create_task(f.get('https://example.test/', {}, expect_json=True))
        await started.wait()
        e = p.egresses[0]
        p.block(e, 'first 429', severity='rate')
        e.parked_until = 0
        finish.set()
        try:
            with self.assertRaises(Exception):
                await task
            self.assertGreater(p.health()[0]['parkedForS'], 3500)
            self.assertEqual(e.spec.rotation, 1)
        finally:
            await p.aclose()

    async def test_shutdown_wakes_capacity_and_parked_waiters(self):
        from igscraper.net import NoEgress
        for parked in (False, True):
            with self.subTest(parked=parked):
                p = pool(lambda r: httpx.Response(200), max_inflight_per_egress=1)
                p.max_park_wait = None
                if parked:
                    p.egresses[0].parked_until = time.monotonic() + 3600
                else:
                    held = await p.acquire()
                waiting = asyncio.create_task(p.acquire())
                await asyncio.sleep(.01)
                await p.aclose()
                try:
                    with self.assertRaises(NoEgress):
                        await asyncio.wait_for(waiting, .1)
                    with self.assertRaises(NoEgress):
                        await p.acquire()
                finally:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)

    async def test_hard_timeout_releases_socks_slot(self):
        async def handler(req):
            await asyncio.sleep(100)
        p = pool(handler, specs=[EgressSpec('proxy', 'socks5://t:x@127.0.0.1:9152', tor=True)])
        p.timeout = .01
        stats = Stats()
        f = Fetcher(p, max_retries=0, backoff_base=0, backoff_max=0, stats=stats)
        try:
            with self.assertRaises(Exception):
                await asyncio.wait_for(f.get('https://example.test/', {}, expect_json=True), .2)
            self.assertEqual(p.egresses[0].inflight, 0)
            self.assertFalse(p._client_inflight)
            self.assertEqual(stats.requests, 1)
            self.assertEqual(stats.statuses['network_error'], 1)
        finally:
            await p.aclose()

    async def test_old_circuit_rate_envelope_keeps_wait_without_rotation(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def handler(req):
            started.set()
            await finish.wait()
            return httpx.Response(200, json={'status':'fail', 'message':'too many requests'},
                                  headers={'retry-after':'3600'})
        p = pool(handler, specs=[EgressSpec('proxy', 'socks5://t:x@127.0.0.1:9152', tor=True)])
        f = Fetcher(p, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
        task = asyncio.create_task(f.get('https://example.test/', {}, expect_json=True))
        await started.wait()
        e = p.egresses[0]
        p.block(e, 'first 429', severity='rate')
        e.parked_until = 0
        finish.set()
        try:
            with self.assertRaises(Exception):
                await task
            self.assertGreater(p.health()[0]['parkedForS'], 3500)
            self.assertEqual(e.spec.rotation, 1)
        finally:
            await p.aclose()

    async def test_cancelled_close_finishes_cleanup_and_can_be_awaited_again(self):
        p = pool(lambda r: httpx.Response(200))
        started, finish = asyncio.Event(), asyncio.Event()
        client = p.egresses[0].client
        original_close = client.aclose
        async def slow_close():
            started.set()
            await finish.wait()
            await original_close()
        client.aclose = slow_close
        closing = asyncio.create_task(p.aclose())
        await started.wait()
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        finish.set()
        await p.aclose()
        self.assertTrue(client.is_closed)
        self.assertFalse(p._close_tasks)
        self.assertFalse(p._client_inflight)

    async def test_late_block_after_shutdown_cannot_create_new_client(self):
        p = pool(lambda r: httpx.Response(200), specs=[EgressSpec('proxy',
            'socks5://t:x@127.0.0.1:9152', tor=True)])
        client = p.egresses[0].client
        await p.aclose()
        p.block(p.egresses[0], 'late block', severity='rate')
        self.assertIs(p.egresses[0].client, client)
        self.assertTrue(client.is_closed)
        self.assertFalse(p._close_tasks)
