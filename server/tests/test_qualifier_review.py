"""Offline review regressions. No provider or account data is accessed."""
import json
import os
import sys
import types
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qualify as q


class QualifierReview(unittest.TestCase):
    def person(self, handle='glow', bio='Founder of a skincare brand @glowskin. Shipping to USA'):
        return {'handle': handle, 'bio': bio, 'name': 'Zoé'}

    def reply(self, **overrides):
        return dict({'id': 0, 'handle': 'glow', 'role': 'buyer', 'fit': 85,
                     'evidence': ['Founder of a skincare brand'], 'extra_tags': ['Skincare'],
                     'niche': 'Skincare', 'decision_maker': True}, **overrides)

    def verdict(self, reply=None, person=None, net=None):
        return q._verdict(reply or self.reply(), person or self.person(), [], 'offline', 'test', net)

    def batch(self, replies):
        items = [{'person': self.person(handle), 'tags': [], 'edges': []} for handle in ('glow', 'other')]
        provider = types.SimpleNamespace(chat=lambda *a, **k: (json.dumps({'results': replies}), 'offline'))
        with patch.object(q, '_providers', return_value=provider):
            return q.llm_verdicts(items)

    def test_malformed_result_does_not_discard_other_person(self):
        for field, value in [('evidence', 3), ('extra_tags', 3), ('stage', []), ('role', []),
                             ('fit', float('nan')), ('fit', float('inf')), ('fit', True)]:
            with self.subTest(field=field, value=value):
                out = self.batch([self.reply(**{field: value}), self.reply(id=1, handle='other')])
                self.assertIsNone(out[0])
                self.assertEqual(out[1]['role'], 'buyer')

    def test_batch_identity_requires_id_and_echoed_handle(self):
        good = self.reply(id=1, handle='@OTHER')
        for bad in [self.reply(handle='other'), {k: v for k, v in self.reply().items() if k != 'handle'},
                    self.reply(id=True), self.reply(handle=['glow'])]:
            with self.subTest(bad=bad):
                out = self.batch([bad, good])
                self.assertIsNone(out[0])
                self.assertIsNotNone(out[1])
        out = self.batch([good, self.reply()])
        self.assertTrue(all(out))
        self.assertEqual(self.batch([self.reply(), self.reply()]), [None, None])

    def test_canonical_unicode_quotes_are_preserved(self):
        quote = 'Zoé'
        p = self.person()
        p['name'] = unicodedata.normalize('NFD', quote)
        reply = self.reply(evidence=[quote, 'Founder of a skincare brand', 'invented company'])
        self.assertEqual(self.verdict(reply, p)['evidence'], [quote, 'Founder of a skincare brand'])
        p['name'] = quote
        reply['evidence'] = [unicodedata.normalize('NFD', quote)]
        self.assertEqual(self.verdict(reply, p)['evidence'], reply['evidence'])

    def test_invented_evidence_cannot_leave_positive_claims(self):
        p = self.person(bio='Content creator and coffee enthusiast')
        self.assertIsNone(self.verdict(self.reply(fit=99, evidence=['Founder of a skincare brand']), p))
        v = self.verdict(self.reply(fit=99, evidence=['Content creator']), p)
        self.assertEqual(v['role'], 'unrelated')
        self.assertLess(v['fit'], 40)
        self.assertEqual(v['evidence'], ['Content creator'])
        self.assertNotIn(('AI: Top fit', 'ai'), v['tags'])
        self.assertNotIn(('AI: Decision maker', 'ai'), v['tags'])
        self.assertIsNone(self.verdict(person={'handle': 'empty', 'followers': 9_000_000}))

    def test_valid_quotes_cannot_support_unrelated_claims(self):
        v = self.verdict(self.reply(runs_ads=True, us_market=True, stage='established',
                                  brand_handle='@invented', extra_tags=['Pets', 'Skincare']))
        tags = {t for t, _ in v['tags']}
        self.assertNotIn('AI: Runs ads', tags)
        self.assertNotIn('AI: Established', tags)
        self.assertNotIn('Pets', tags)
        self.assertIn('AI: US market', tags)
        self.assertIn('AI: Decision maker', tags)
        self.assertIsNone(v['brand_handle'])
        self.assertEqual(self.verdict(self.reply(brand_handle='@glowskin'))['brand_handle'], '@glowskin')

    def test_reason_uses_verified_evidence_instead_of_model_invention(self):
        v = self.verdict(self.reply(reason='Makes millions and knows Michael personally.'))
        self.assertNotIn('millions', v['reason'])
        self.assertIn('Founder of a skincare brand', v['reason'])

    def test_rules_require_business_activity(self):
        for bio in ['Founder at a tech startup. I love skincare.', 'Freelancer',
                    'Freelance photographer', 'Founder. Interested in Shopify and supplements.',
                    'Founder at tech startup. Skincare brand enthusiast.']:
            with self.subTest(bio=bio):
                p = self.person(bio=bio)
                v = q.rule_verdict(p, q.rule_tags(p, [], None))
                self.assertNotIn(v['role'], ['buyer', 'connector'])
                self.assertLess(v['content_fit'], 55)
        for bio, role in [('We help DTC brands with paid ads', 'connector'),
                          ('Shopify developer for ecommerce brands', 'connector'),
                          ('Founder of a skincare brand', 'buyer'),
                          ('Handmade candles. Shop now', 'buyer')]:
            p = self.person(bio=bio)
            self.assertEqual(q.rule_verdict(p, q.rule_tags(p, [], None))['role'], role)

    def test_network_changes_rank_without_changing_business_fit(self):
        low = self.verdict(net={'lists': 0})
        high = self.verdict(net={'lists': 4, 'me': 'mutual', 'client_seeds': 2})
        self.assertEqual(low['content_fit'], high['content_fit'])
        self.assertEqual(low['tags'], high['tags'])
        self.assertGreater(high['score'], low['score'])

    def test_single_reply_requires_identity_too(self):
        for missing in ('id', 'handle'):
            reply = {k: v for k, v in self.reply().items() if k != missing}
            provider = types.SimpleNamespace(chat=lambda *a, **k: (json.dumps(reply), 'offline'))
            with patch.object(q, '_providers', return_value=provider):
                self.assertIsNone(q.llm_verdict(self.person(), [], []))

    def test_provider_is_exact_shared_pool_even_with_proxy_override(self):
        pool = object()
        with patch.object(q.llm, 'get', return_value=pool), patch.object(q, 'PROXY', 'unused'):
            self.assertIs(q._providers(), pool)
            self.assertIs(q._providers(), pool)

    def test_invalid_boolean_claims_are_isolated(self):
        for field in ('decision_maker', 'runs_ads', 'us_market'):
            for value in (1, 'true', [], {}):
                out = self.batch([self.reply(**{field: value}), self.reply(id=1, handle='other')])
                self.assertIsNone(out[0])
                self.assertIsNotNone(out[1])

    def test_multilingual_brand_evidence_and_founder_claims(self):
        bios = ['Oprichter van een kledingmerk', 'Gründer einer Hautpflege-Marke',
                'Fondatrice de notre marque de cosmétiques', 'Fundadora de una marca de ropa',
                '护肤品牌创始人', 'مؤسسة علامة تجارية لمستحضرات التجميل']
        for bio in bios:
            with self.subTest(bio=bio):
                v = self.verdict(self.reply(evidence=[bio]), self.person(bio=bio))
                self.assertEqual(v['role'], 'buyer')
                self.assertIn(('AI: Decision maker', 'ai'), v['tags'])
                self.assertEqual(v['evidence'], [bio])

    def test_audience_and_brand_deals_do_not_establish_ownership(self):
        for bio in ('Content creator. Skincare brand deals',
                    'Photographer for a skincare brand',
                    'Agency serving a clothing brand',
                    'Founder at software startup. Skincare brand enthusiast.',
                    'Content creator and coffee enthusiast'):
            person = self.person(bio=bio)
            person['followers'] = 9000000
            v = self.verdict(self.reply(fit=99, evidence=[bio]), person)
            self.assertNotEqual(v['role'], 'buyer')
            self.assertLess(v['content_fit'], 66)

    def test_quotes_cannot_be_empty_after_stripping(self):
        self.assertIsNone(self.verdict(self.reply(evidence=['"""'])))


if __name__ == '__main__':
    unittest.main()
