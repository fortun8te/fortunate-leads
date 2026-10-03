"""Buyer evidence regressions from a public-profile review. No live data or AI calls."""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qualify


class BuyerEvidence(unittest.TestCase):
    def verdict(self, bio, name='', website=None, category=None):
        person = dict(handle='sample', bio=bio, name=name, website=website,
                      category=category, followers=5000)
        tags = qualify.rule_tags(person, [], None)
        return qualify.rule_verdict(person, tags, {'lists': 10})

    def test_publication_is_not_a_product_brand(self):
        for bio, name, website in [
            ('The business of consumer brands, health and wellness, explained daily. '
             'For founders, operators, and investors. Newsletter: The Digest',
             'Consumer Brand News', 'https://digest.substack.com'),
            ('Agenda-setting intelligence, analysis and advice for the global beauty and wellness industries.',
             'The Business of Beauty', 'https://likeshop.me/beauty'),
        ]:
            with self.subTest(bio=bio):
                verdict = self.verdict(bio, name, website)
                self.assertNotEqual(verdict['role'], 'buyer')
                self.assertLessEqual(verdict['content_fit'], 42)

    def test_cfo_serving_brands_is_a_connector(self):
        verdict = self.verdict('Making your ecom brand way more profitable | CFO to 80+ brands | '
                               '8 figures in ad spend | Follow for daily ecom insight', 'Ecom Profit Expert')
        self.assertEqual(verdict['role'], 'connector')

    def test_founder_education_and_boutique_agency_need_product_evidence(self):
        for bio in ('Helping Founders Build Freedom-First Supplement Brands. Sharing Everything I Would Do.',
                    'Boutique full stack growth partner and brand holding co.'):
            with self.subTest(bio=bio):
                self.assertNotEqual(self.verdict(bio)['role'], 'buyer')

    def test_product_store_still_counts(self):
        for bio in ('Boutique clothing store | Shop our apparel | Ships to USA',
                    'Handcrafted skincare. Shop now. Subscribe to our newsletter',
                    'Founder of a supplement brand. Sharing my newsletter'):
            with self.subTest(bio=bio):
                self.assertEqual(self.verdict(bio, website='https://glow.com/shop')['role'], 'buyer')

    def test_service_shop_link_without_products_is_not_ownership(self):
        verdict = self.verdict('Boutique growth partner. Shop our services', website='https://growth.com/shop')
        self.assertNotEqual(verdict['role'], 'buyer')


if __name__ == '__main__':
    unittest.main()
