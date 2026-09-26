"""Text fidelity at the logged-out HTML boundary and in dataset exports."""

import csv
import io
import json
import sys
import unittest
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from igscraper import mapper
from igscraper.output import to_csv, to_json, to_jsonl


class UnicodeProfileReview(unittest.TestCase):
    def test_ssr_biography_survives_html_mapping_and_exports(self):
        bio = 'Nederland: café & thee\nDeutsch: Grüße „Hallo“\nالعربية: أهلاً\n中文: 你好 日本語: 東京\n✨ "quoted" \\ slash &amp; literal'
        user = {"pk": "42", "id": "fb42", "username": "example", "follower_count": 12,
                "full_name": "München 東京 ✨", "biography": bio}
        blob = json.dumps({"data": {"user": user}}, ensure_ascii=True).replace("<", "\\u003c")
        page = ("<html><head><meta content='12 Followers, 1 Posts' "
                "property='og:description'></head><body>"
                f"<script data-sjs type='application/json'>{blob}</script></body></html>")

        parsed = mapper.parse_profile_html(page)
        item = mapper.map_fallback("https://www.instagram.com/example", None, parsed)
        self.assertEqual(item["biography"], bio)
        self.assertEqual(item["fullName"], user["full_name"])
        self.assertEqual(item["id"], "42")
        self.assertEqual(item["postsCount"], 1)
        self.assertEqual(json.loads(to_json([item]))[0]["biography"], bio)
        self.assertEqual(json.loads(to_jsonl([item]).splitlines()[0])["biography"], bio)
        self.assertEqual(next(csv.DictReader(io.StringIO(to_csv([item]))))["biography"], bio)

    def test_json_bio_keeps_literal_entities_while_og_meta_decodes_once(self):
        bio = 'A &amp; B < C > D — "say hi" 😊'
        user = {"pk": "42", "username": "example", "follower_count": 1, "biography": bio}
        blob = json.dumps({"user": user}, ensure_ascii=False)
        desc = '1,234 Followers, 2 Posts — A &amp; B &quot;special&quot;'
        page = (f'<meta name="x" property="og:description" content="{escape(desc, quote=True)}">'
                f'<script type="application/json" data-sjs>{blob}</script>')

        parsed = mapper.parse_profile_html(page)
        self.assertEqual(parsed["biography"], bio)
        self.assertEqual(parsed["_og_description"], desc)
        self.assertEqual(mapper.og_counts(parsed["_og_description"])["posts"], 2)


if __name__ == "__main__":
    unittest.main()
