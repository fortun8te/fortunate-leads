"""Response envelopes must not turn an unavailable bio into a verified observation."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from igscraper.config import ScraperConfig
from igscraper.net import BadResponse, EgressPool, Fetcher, LoginWall, NotFound, RateLimited, Stats, AdaptiveLimiter
from igscraper.egress import EgressSpec
from igscraper.scraper import ProfileScraper


def config(**kwargs):
    return ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0, fallback=False, **kwargs)


class EnvelopeReview(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_transport_has_wall_deadline_and_releases_egress(self):
        cancelled = asyncio.Event()

        async def hang(request):
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

        pool = EgressPool([EgressSpec("direct")], limiter_factory=lambda _: AdaptiveLimiter(mode="unlimited"),
                          timeout=.02, http2=False, max_failures=3, park_cooldown=30, park_max=60,
                          max_park_wait=0, transport_factory=lambda _: httpx.MockTransport(hang))
        try:
            fetcher = Fetcher(pool, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
            with self.assertRaises(Exception) as outcome:
                await asyncio.wait_for(fetcher.get("https://example.test/", {}, expect_json=True), .5)
            self.assertNotIsInstance(outcome.exception, asyncio.TimeoutError)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(pool.egresses[0].inflight, 0)
            self.assertEqual(pool.egresses[0].bad, 1)
        finally:
            await pool.aclose()

    async def test_http_200_failure_envelopes_never_cache_an_empty_bio(self):
        cases = [
            {"status": "fail", "message": "login_required"},
            {"status": "ok", "challenge": {"url": "/challenge/"}},
            {"status": "fail", "message": "Please wait a few minutes"},
            {"status": "ok", "errors": [{"message": "upstream failed"}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                transport = lambda _: httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
                async with ProfileScraper(config(cache_path=str(Path(tmp) / "cache.sqlite")), transport_factory=transport) as s:
                    item = (await s.run(["example"]))[0]
                    self.assertIn("error", item)
                    self.assertNotEqual(item["error"], "not_found")
                    self.assertNotIn("biography", item)
                    self.assertIsNone(s.store.cache_get("u:example", 3600))
                    self.assertEqual(s.last_run_counts["freshSuccess"], 0)
                    self.assertEqual(s._pool.egresses[0].ok, 0)

    async def test_200_malformed_json_and_html_fail_without_success(self):
        responses = [httpx.Response(200, headers={"content-type": "application/json"}, text="{"),
                     httpx.Response(200, headers={"content-type": "text/html"}, text="<html>login</html>")]
        for response in responses:
            with self.subTest(content_type=response.headers["content-type"]):
                pool = EgressPool([EgressSpec("direct")], limiter_factory=lambda _: AdaptiveLimiter(mode="unlimited"),
                                  timeout=1, http2=False, max_failures=3, park_cooldown=900, park_max=14400,
                                  max_park_wait=0, transport_factory=lambda _: httpx.MockTransport(lambda request: response))
                try:
                    fetcher = Fetcher(pool, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
                    with self.assertRaises((BadResponse, LoginWall)):
                        await fetcher.get("https://example.test/", {}, expect_json=True)
                    self.assertEqual(pool.egresses[0].ok, 0)
                finally:
                    await pool.aclose()

    async def test_only_explicit_instagram_404_is_terminal_not_found(self):
        responses = [
            (httpx.Response(404, headers={"content-type": "text/html"}, text="relay 404"), BadResponse),
            (httpx.Response(404, json={"status": "fail", "message": "temporary issue"}), BadResponse),
            (httpx.Response(404, json={"status": "fail", "message": "User not found"}), NotFound),
        ]
        for response, expected in responses:
            with self.subTest(body=response.text):
                pool = EgressPool([EgressSpec("direct")], limiter_factory=lambda _: AdaptiveLimiter(mode="unlimited"),
                                  timeout=1, http2=False, max_failures=3, park_cooldown=900, park_max=14400,
                                  max_park_wait=0, transport_factory=lambda _: httpx.MockTransport(lambda request: response))
                try:
                    fetcher = Fetcher(pool, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
                    with self.assertRaises(expected):
                        await fetcher.get("https://example.test/", {}, expect_json=True)
                    self.assertEqual(pool.egresses[0].ok, int(expected is NotFound))
                finally:
                    await pool.aclose()

    async def test_200_rate_envelope_honours_retry_after(self):
        pool = EgressPool([EgressSpec("direct")], limiter_factory=lambda _: AdaptiveLimiter(mode="unlimited"),
                          timeout=1, http2=False, max_failures=3, park_cooldown=30, park_max=60,
                          max_park_wait=0, transport_factory=lambda _: httpx.MockTransport(
                              lambda request: httpx.Response(200, headers={"retry-after": "3600"},
                                                             json={"status": "fail", "message": "rate limit"})))
        try:
            fetcher = Fetcher(pool, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
            with self.assertRaises(RateLimited):
                await fetcher.get("https://example.test/", {}, expect_json=True)
            self.assertGreater(pool.health()[0]["parkedForS"], 3500)
        finally:
            await pool.aclose()

    async def test_http_status_takes_precedence_over_conflicting_body(self):
        pool = EgressPool([EgressSpec("direct")], limiter_factory=lambda _: AdaptiveLimiter(mode="unlimited"),
                          timeout=1, http2=False, max_failures=3, park_cooldown=30, park_max=60,
                          max_park_wait=0, transport_factory=lambda _: httpx.MockTransport(
                              lambda request: httpx.Response(429, headers={"retry-after": "3600"},
                                                             json={"status": "fail", "message": "User not found"})))
        try:
            fetcher = Fetcher(pool, max_retries=0, backoff_base=0, backoff_max=0, stats=Stats())
            with self.assertRaises(RateLimited):
                await fetcher.get("https://example.test/", {}, expect_json=True)
            self.assertGreater(pool.health()[0]["parkedForS"], 3500)
            self.assertEqual(pool.egresses[0].ok, 0)
        finally:
            await pool.aclose()


if __name__ == "__main__":
    unittest.main()
