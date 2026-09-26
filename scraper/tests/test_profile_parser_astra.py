"""Offline source-selection regressions using synthetic public profile payloads."""
import json
import unittest
from unittest.mock import AsyncMock

from igscraper import mapper
from igscraper.config import ScraperConfig
from igscraper.inputs import parse_target
from igscraper.net import SchemaError
from igscraper.scraper import ProfileScraper


def script(value):
    return '<script type="application/json">' + json.dumps(value) + '</script>'


class ProfileParserTests(unittest.TestCase):
    def test_requested_profile_after_recommendation_in_same_script(self):
        wrong = {"username": "other", "pk": "99", "follower_count": 2, "biography": "wrong"}
        right = {"username": "Example", "pk": "42", "biography": "café\n東京 👩🏽‍💻 &amp;"}
        parsed = mapper.parse_profile_html(script({"recommended": wrong, "profile": right}), username="example")
        self.assertEqual(parsed, right)

    def test_later_script_replaces_partial_duplicate_with_full_bio(self):
        partial = {"username": "example", "pk": "42", "follower_count": 2}
        full = {**partial, "biography": "first\nsecond\nthird"}
        parsed = mapper.parse_profile_html(script(partial) + script(full), username="example")
        self.assertEqual(parsed["biography"], full["biography"])

    def test_wrong_users_and_metadata_cannot_supply_a_biography(self):
        html = '<meta property="og:description" content="summary…">' + script(
            {"username": "other", "pk": "99", "follower_count": 2, "biography": "wrong"})
        parsed = mapper.parse_profile_html(html, username="example")
        self.assertNotIn("biography", parsed)
        self.assertNotIn("username", parsed)

    def test_explicit_empty_bio_is_preserved(self):
        user = {"username": "example", "pk": "42", "biography": ""}
        self.assertEqual(mapper.parse_profile_html(script(user), username="example"), user)

    def test_no_literal_none_id_from_partial_fallbacks(self):
        for source in ("threads_user", "gql_user"):
            with self.subTest(source=source):
                item = mapper.map_fallback("https://www.instagram.com/example", None, None,
                                           **{source: {"username": "example", "biography": "bio"}})
                self.assertIsNone(item["id"])

    def test_threads_pk_is_preserved(self):
        item = mapper.map_fallback("https://www.instagram.com/example", None, None,
                                   threads_user={"pk": "42", "username": "example"})
        self.assertEqual(item["id"], "42")

    def test_authoritative_empty_links_clear_old_derived_url(self):
        item = mapper.map_fallback("https://www.instagram.com/example", None, None,
            threads_user={"id": "42", "username": "example", "full_name": "Old name",
                          "bio_links": [{"url": "https://old.example"}]},
            gql_user={"pk": "42", "username": "example", "biography": "", "bio_links": [], "full_name": ""})
        self.assertEqual(item["externalUrls"], [])
        self.assertIsNone(item["externalUrl"])
        self.assertEqual(item["fullName"], "")

    def test_current_gql_picture_overrides_html_picture(self):
        item = mapper.map_fallback("https://www.instagram.com/example", None,
            {"pk": "42", "username": "example", "profile_pic_url": "old"},
            gql_user={"pk": "42", "username": "example", "profile_pic_url": "new"})
        self.assertEqual(item["profilePicUrl"], "new")


class FallbackParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_id_drives_graphql_lookup(self):
        config = ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0, gql_fallback=True)
        async with ProfileScraper(config) as scraper:
            scraper.fetch_web_profile_info = AsyncMock(side_effect=SchemaError("schema failed"))
            scraper.fetch_feed = AsyncMock(return_value={"user": {"id": "42", "username": "example"}, "items": []})
            scraper.fetch_gql_profile = AsyncMock(return_value={"pk": "42", "username": "example", "biography": "complete"})
            scraper.fetch_profile_html = AsyncMock(return_value={})
            item = await scraper.scrape_target(parse_target("example"))
            scraper.fetch_gql_profile.assert_awaited_once_with("42", None)
            self.assertEqual(item["biography"], "complete")
            self.assertEqual(item["_scrape"]["bioStatus"], "observed")
            scraper.fetch_profile_html.assert_not_awaited()

    async def test_missing_primary_and_graphql_bio_continue_to_full_html(self):
        for value in (None, ["invalid"]):
            with self.subTest(biography=value):
                config = ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0, gql_fallback=True)
                async with ProfileScraper(config) as scraper:
                    identity = {"id": "42", "username": "example"}
                    scraper.fetch_web_profile_info = AsyncMock(return_value=identity)
                    scraper.fetch_feed = AsyncMock(return_value={"user": identity, "items": []})
                    scraper.fetch_gql_profile = AsyncMock(return_value={**identity, "biography": value})
                    scraper.fetch_profile_html = AsyncMock(return_value={"pk": "42", "username": "example",
                                                                          "biography": "full HTML bio"})
                    item = await scraper.scrape_target(parse_target("example"))
                    self.assertEqual(item["biography"], "full HTML bio")
                    self.assertEqual(item["_scrape"]["source"], "fallback_feed_html")
                    self.assertEqual(item["_scrape"]["bioStatus"], "observed")
                    scraper.fetch_profile_html.assert_awaited_once()

    async def test_empty_primary_bio_needs_no_fallback(self):
        config = ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0)
        async with ProfileScraper(config) as scraper:
            scraper.fetch_web_profile_info = AsyncMock(return_value={"id": "42", "username": "example", "biography": ""})
            scraper.fetch_feed = AsyncMock()
            item = await scraper.scrape_target(parse_target("example"))
            self.assertEqual(item["_scrape"]["bioStatus"], "empty")
            scraper.fetch_feed.assert_not_awaited()

    async def test_rejected_graphql_bio_cannot_verify_threads_bio(self):
        config = ScraperConfig(health_interval=0, rate_per_ip=0, max_retries=0, gql_fallback=True)
        async with ProfileScraper(config) as scraper:
            async def wpi(username, app_id=None, retries=None):
                if app_id:
                    return {"id": "42", "username": "example", "biography": "Threads bio"}
                raise SchemaError("schema failed")
            scraper.fetch_web_profile_info = AsyncMock(side_effect=wpi)
            scraper.fetch_feed = AsyncMock(return_value={"user": {"pk": "42", "username": "example"}, "items": []})
            scraper.fetch_gql_profile = AsyncMock(return_value={"pk": "99", "username": "example", "biography": "wrong bio"})
            item = await scraper.scrape_target(parse_target("example"))
            self.assertEqual(item["biography"], "Threads bio")
            self.assertEqual(item["_scrape"]["bioStatus"], "unavailable")
            self.assertEqual(item["error"], "incomplete_profile")


if __name__ == "__main__":
    unittest.main()
