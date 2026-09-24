import os; os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qual_api  # noqa: E402
import qualify  # noqa: E402


class SafeFetch(unittest.TestCase):
    def test_refuses_local_and_odd_urls(self):
        for url in ('http://127.0.0.1:8777/api/leads', 'http://localhost/', 'file:///etc/passwd', 'ftp://example.com/',
                    'http://10.0.0.1/', 'http://169.254.169.254/latest', 'http://example.com:8080/'):
            with self.assertRaises(ValueError):
                qual_api._check(url)

    def test_signals_and_text(self):
        doc = ('<html><head><title>Glow Co</title><meta name="description" content="Clean skincare"><script>fbq("init")</script>'
               '<script src="https://cdn.shopify.com/x.js"></script></head><body><h1>Serum</h1> $24 Add to cart</body></html>')
        s = qual_api.signals(doc, 'https://glow.co')
        self.assertEqual(s['shop'], 'Shopify')
        self.assertTrue(s['runs_ads'] and s['cart'] and s['usd'])
        title, desc, text = qual_api.page_text(doc)
        self.assertEqual((title, desc), ('Glow Co', 'Clean skincare'))
        self.assertNotIn('fbq', text)
        self.assertIn('AI: Runs ads', qual_api.site_tags({'us_market': True, 'product_category': 'Skincare'}, s))

    def test_hub_links_skip_socials(self):
        doc = '<a href="https://instagram.com/x">ig</a><a href="https://shop.glow.co/">shop</a>'
        self.assertEqual(qual_api.links_out(doc, 'https://linktr.ee/glow'), ['https://shop.glow.co/'])


class AiTags(unittest.TestCase):
    def test_only_from_model_fields(self):
        v = {'role': 'buyer', 'fit': 80, 'niche': 'Skincare', 'stage': 'growing', 'decision_maker': True, 'runs_ads': True, 'us_market': None}
        tags = qualify.ai_tags(v, 80)
        self.assertEqual(tags, ['AI: Skincare', 'AI: Growing', 'AI: Decision maker', 'AI: Runs ads', 'AI: Top fit'])
        self.assertEqual(qualify.ai_tags({'role': 'unrelated', 'niche': 'Skincare', 'stage': 'early'}, 20), [])

    def test_verdict_marks_ai_group_and_rules_never(self):
        v = qualify._verdict({'role': 'buyer', 'fit': 70, 'reason': 'x', 'us_market': True}, {'handle': 'a', 'bio': 'b'}, [], 'm', 'q')
        self.assertIn(('AI: US market', 'ai'), v['tags'])
        rv = qualify.rule_verdict({'handle': 'a', 'bio': 'founder of glow skincare'}, [])
        self.assertFalse(any(g == 'ai' for _, g in rv.get('tags') or []))


if __name__ == '__main__':
    unittest.main()
