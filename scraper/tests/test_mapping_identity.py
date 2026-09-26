import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from igscraper import mapper
from igscraper.config import ScraperConfig
from igscraper.inputs import parse_target
from igscraper.net import SchemaError
from igscraper.scraper import ProfileScraper


class MappingIdentityTests(unittest.TestCase):
    def test_feed_and_html_with_same_handle_but_different_ids_do_not_merge(self):
        item = mapper.map_fallback("https://www.instagram.com/a",
            {"user": {"pk": 42, "username": "a"}, "items": []},
            {"pk": 99, "id": "fb99", "username": "a", "biography": "wrong bio", "follower_count": 12})
        self.assertEqual(item["id"], "42")
        self.assertIsNone(item["biography"])
        self.assertIsNone(item["followersCount"])
        self.assertIsNone(item["fbid"])

    def test_html_without_pk_cannot_enrich_a_feed_id_by_handle_alone(self):
        item = mapper.map_fallback("https://www.instagram.com/a",
            {"user": {"pk": 42, "username": "a"}, "items": []},
            {"id": "1784-fb-id", "username": "a", "biography": "unverified bio"})
        self.assertEqual(item["id"], "42")
        self.assertIsNone(item["biography"])

    def test_graphql_explicit_empty_links_override_threads_links(self):
        item = mapper.map_fallback("https://www.instagram.com/a", None, None,
            threads_user={"id": "42", "username": "a", "bio_links": [{"url": "https://old.example"}],
                          "biography": "Threads bio"},
            gql_user={"pk": "42", "username": "a", "biography": "", "bio_links": [],
                      "external_url": None, "external_lynx_url": ""})
        self.assertEqual(item["biography"], "")
        self.assertEqual(item["externalUrls"], [])
        self.assertIsNone(item["externalUrl"])
        self.assertEqual(item["externalUrlShimmed"], "")

    def test_missing_graphql_links_leave_older_values_available(self):
        item = mapper.map_fallback("https://www.instagram.com/a", None, None,
            threads_user={"id": "42", "username": "a", "bio_links": [{"url": "https://old.example"}]},
            gql_user={"pk": "42", "username": "a", "biography": ""})
        self.assertEqual(item["externalUrl"], "https://old.example")
        self.assertEqual(item["externalUrls"][0]["url"], "https://old.example")

    def test_wpi_has_channel_preserves_false_and_true(self):
        for value in (False, True):
            with self.subTest(value=value):
                item = mapper.map_web_profile_info({"id": "42", "username": "a", "biography": "",
                                                    "has_channel": value}, "https://www.instagram.com/a")
                self.assertIs(item["hasChannel"], value)


class FallbackIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def _scrape(self, feed, page, threads=None):
        async with ProfileScraper(ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0)) as scraper:
            scraper.fetch_web_profile_info = AsyncMock(side_effect=SchemaError("schema failed"))
            scraper.fetch_feed = AsyncMock(side_effect=feed if isinstance(feed, Exception) else None,
                                           return_value=feed if isinstance(feed, dict) else None)
            if threads is not None:
                async def wpi(_username, app_id=None, retries=None):
                    if app_id:
                        return threads
                    raise SchemaError("schema failed")
                scraper.fetch_web_profile_info.side_effect = wpi
            scraper.fetch_profile_html = AsyncMock(return_value=page)
            return await scraper.scrape_target(parse_target("a"))

    async def test_html_only_without_instagram_id_is_partial(self):
        item = await self._scrape(SchemaError("feed failed"),
                                  {"id": "1784-fb-id", "username": "a", "biography": "real bio"})
        self.assertEqual(item["biography"], "real bio")
        self.assertIsNone(item["id"])
        self.assertEqual(item["error"], "incomplete_profile")
        self.assertEqual(item["_scrape"]["bioStatus"], "unavailable")

    async def test_html_mismatched_id_cannot_mark_feed_profile_observed(self):
        item = await self._scrape({"user": {"pk": 42, "username": "a"}, "items": []},
                                  {"pk": 99, "username": "a", "biography": "wrong bio"})
        self.assertEqual(item["id"], "42")
        self.assertIsNone(item["biography"])
        self.assertEqual(item["error"], "incomplete_profile")

    async def test_resolved_numeric_id_must_still_match_profile(self):
        async with ProfileScraper(ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0)) as scraper:
            scraper.resolve_user_id = AsyncMock(return_value="a")
            scraper.fetch_web_profile_info = AsyncMock(return_value={"id": "99", "username": "a", "biography": "wrong"})
            item = await scraper.scrape_target(parse_target("42"))
        self.assertEqual(item["error"], "bad_response")
        self.assertNotIn("biography", item)


if __name__ == "__main__":
    unittest.main()
