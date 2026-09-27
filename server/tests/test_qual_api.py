import os; os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qual_api  # noqa: E402
import qualify  # noqa: E402
import db  # noqa: E402


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
        self.assertTrue(s['ad_tracking_detected'] and s['meta_pixel'] and s['cart'] and s['usd'])
        self.assertIsNone(s['runs_ads'])
        title, desc, text = qual_api.page_text(doc)
        self.assertEqual((title, desc), ('Glow Co', 'Clean skincare'))
        self.assertNotIn('fbq', text)
        self.assertIn('AI: Ad tracking detected', qual_api.site_tags({'us_market': True, 'product_category': 'Skincare'}, s))
        self.assertNotIn('AI: Runs ads', qual_api.site_tags({'us_market': True, 'product_category': 'Skincare'}, s))

    def test_pixel_tags_are_replaced_on_reread_without_touching_manual_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init(str(Path(tmp) / 'leads.sqlite'))
            try:
                qual_api._ensure(conn)
                db.set_setting(conn, 'qualify', True)
                pid = db.upsert_person(conn, {'handle': 'glow', 'website': 'https://glow.co'})
                conn.execute("INSERT INTO verdicts(person_id,model) VALUES(?,'test-model')", (pid,))
                conn.execute("INSERT INTO site_reads(person_id,signals) VALUES(?,?)", (pid, json.dumps({'runs_ads': True})))
                conn.execute("INSERT INTO tags VALUES(?,'AI: Runs ads','ai','auto')", (pid,))
                conn.execute("INSERT INTO tags VALUES(?,'Keep manual','signal','manual')", (pid,))
                pixel_page = '<html><title>Glow</title><script>fbq("init")</script></html>'
                clean_page = '<html><title>Glow</title></html>'
                with patch.object(qual_api, 'fetch', side_effect=[('https://glow.co', pixel_page),
                                                                   ('https://glow.co', clean_page)]), \
                     patch.object(qual_api, 'summarise', return_value=({'summary': 'Glow sells products.', 'sells_physical_products': True, 'us_market': None, 'product_category': None, 'stage': 'unknown'}, 'test-model')):
                    first = qual_api.read_site(conn, pid, 'https://glow.co', force=True)
                    self.assertTrue(first['signals']['ad_tracking_detected'])
                    self.assertIsNone(first['signals']['runs_ads'])
                    tags = {r['tag'] for r in conn.execute('SELECT tag FROM tags WHERE person_id=?', (pid,))}
                    self.assertIn('AI: Runs ads', tags)
                    self.assertIn('AI: Ad tracking detected', first['tags'])
                    conn.execute("UPDATE tags SET source='manual' WHERE person_id=? AND tag='AI: Runs ads'", (pid,))
                    conn.execute('UPDATE site_reads SET signals=? WHERE person_id=?', (json.dumps({'runs_ads': True}), pid))
                    second = qual_api.read_site(conn, pid, 'https://glow.co', force=True)
                self.assertFalse(second['signals']['ad_tracking_detected'])
                tags = {r['tag'] for r in conn.execute('SELECT tag FROM tags WHERE person_id=?', (pid,))}
                self.assertNotIn('AI: Ad tracking detected', tags)
                self.assertNotIn('AI: Ad tracking detected', second['tags'])
                self.assertIn('AI: Runs ads', tags)
                self.assertIn('Keep manual', tags)
            finally:
                conn.close()

    def test_hub_links_skip_socials(self):
        doc = '<a href="https://instagram.com/x">ig</a><a href="https://shop.glow.co/">shop</a>'
        self.assertEqual(qual_api.links_out(doc, 'https://linktr.ee/glow'), ['https://shop.glow.co/'])

    def test_reread_replaces_site_claims_without_erasing_profile_or_manual_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init(str(Path(tmp) / 'leads.sqlite'))
            qual_api._ensure(conn)
            db.set_setting(conn, 'qualify', True)
            pid = db.upsert_person(conn, {'handle': 'glow', 'website': 'https://glow.co'})
            conn.execute("INSERT INTO verdicts(person_id,model) VALUES(?,'test-model')", (pid,))
            conn.execute("INSERT INTO tags VALUES(?,'AI: US market','ai','auto')", (pid,))
            conn.execute("INSERT INTO tags VALUES(?,'Keep manual','signal','manual')", (pid,))
            pages = [('<html><title>Glow</title><script src="https://cdn.shopify.com/x.js"></script>Add to cart</html>',
                      {'summary': 'Skincare shop.', 'sells_physical_products': True, 'product_category': 'Skincare',
                       'stage': 'growing', 'us_market': True}),
                     ('<html><title>Glow</title>Software platform</html>',
                      {'summary': 'Software.', 'sells_physical_products': False, 'product_category': None,
                       'stage': 'unknown', 'us_market': False})]
            for doc, data in pages:
                with patch.object(qual_api, 'fetch', return_value=('https://glow.co', doc)), \
                     patch.object(qual_api, 'summarise', return_value=(data, 'test-model')):
                    row = qual_api.read_site(conn, pid, 'https://glow.co', force=True)
                if data['summary'] == 'Skincare shop.':
                    self.assertIn('AI: Skincare', row['tags'])
                    self.assertIn('AI: Has online shop', row['tags'])
                else:
                    self.assertEqual(row['tags'], [])
            tags = {r['tag'] for r in conn.execute('SELECT tag FROM tags WHERE person_id=?', (pid,))}
            self.assertEqual(tags, {'AI: US market', 'Keep manual'})
            conn.close()

    def test_cache_skips_repeat_fetch_and_changed_website_hides_old_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init(str(Path(tmp) / 'leads.sqlite'))
            qual_api._ensure(conn)
            db.set_setting(conn, 'qualify', True)
            pid = db.upsert_person(conn, {'handle': 'glow', 'website': 'https://glow.co'})
            conn.execute("INSERT INTO verdicts(person_id,model) VALUES(?,'test-model')", (pid,))
            data = {'summary': 'Skincare shop.', 'sells_physical_products': True, 'product_category': 'Skincare',
                    'stage': 'growing', 'us_market': True}
            with patch.object(qual_api, 'fetch', return_value=('https://glow.co', '<title>Glow</title>Shopify Add to cart')) as fetch, \
                 patch.object(qual_api, 'summarise', return_value=(data, 'test-model')) as model:
                first = qual_api.read_site(conn, pid, 'https://glow.co')
                second = qual_api.read_site(conn, pid, 'https://glow.co')
                self.assertEqual((fetch.call_count, model.call_count), (1, 1))
                self.assertEqual(first, second)
            db.upsert_person(conn, {'handle': 'glow', 'website': 'https://other.co'})
            self.assertIsNone(qual_api.site_row(conn, pid))
            conn.close()

    def test_parse_meta_order_and_main_content(self):
        doc = '<title>Glow</title><meta content="Clean skincare" name="description"><nav>' + ('Menu ' * 800) + \
              '</nav><main><h1>Skincare</h1><p>' + ('Sells serum to US customers. ' * 6) + '</p></main>'
        title, desc, text = qual_api.page_text(doc)
        self.assertEqual((title, desc), ('Glow', 'Clean skincare'))
        self.assertTrue(text.startswith('Skincare Sells serum'))
        self.assertNotIn('Menu', text)

    def test_malformed_summary_is_rejected(self):
        self.assertIsNone(qual_api._valid_summary({'summary': 'Shop', 'stage': 'huge', 'us_market': 'true'}))
        self.assertIsNone(qual_api._valid_summary({'summary': 'Shop', 'stage': 'early', 'product_category': 'Invented'}))



class SiteEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'test.sqlite'))
        self.addCleanup(self.conn.close)
        db.set_setting(self.conn, 'qualify', True)
        self.url = 'https://glow.co'
        self.pid = db.upsert_person(self.conn, {'handle': 'glow', 'website': self.url})
        self.data = {'summary': 'Skincare shop.', 'sells_physical_products': True,
                     'us_market': True, 'product_category': 'Skincare', 'stage': 'growing'}
        self.doc = '<title>Glow</title><script>fbq("init")</script>Add to cart'

    def read(self, doc=None, data=None, force=True):
        with patch.object(qual_api, 'fetch', return_value=(self.url, self.doc if doc is None else doc)), \
             patch.object(qual_api, 'summarise', return_value=(self.data if data is None else data, 'test-model')):
            return qual_api.read_site(self.conn, self.pid, self.url, force=force)

    def test_site_claims_independent_of_profile_verdict(self):
        row = self.read()
        self.assertIn('AI: Skincare', row['tags'])
        self.assertIn('AI: Ad tracking detected', row['tags'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tags').fetchone()[0], 0)
        self.conn.execute("DELETE FROM tags WHERE source='auto'")
        self.assertEqual(qual_api.site_row(self.conn, self.pid)['tags'], row['tags'])

    def test_failure_clears_previous_site_claims(self):
        self.read()
        with patch.object(qual_api, 'fetch', side_effect=ValueError('could not reach the website')), \
             patch.object(qual_api, 'summarise') as model:
            row = qual_api.read_site(self.conn, self.pid, self.url, force=True)
        model.assert_not_called()
        self.assertEqual(row['tags'], [])
        self.assertIsNone(row['summary'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM site_evidence').fetchone()[0], 0)

    def test_changed_content_with_malformed_model_clears_old_inferences(self):
        self.read()
        row = self.read(doc='<title>Changed</title><script>fbq("init")</script>', data={'summary': 'bad'})
        self.assertEqual(row['tags'], ['AI: Ad tracking detected'])
        self.assertIsNone(row['summary'])
        self.assertIsNone(row['model'])
        self.assertIsNotNone(row['error'])
        self.assertEqual([r['tag'] for r in self.conn.execute('SELECT tag FROM site_evidence')], row['tags'])

    def test_forced_unchanged_content_reuses_model(self):
        first = self.read()
        with patch.object(qual_api, 'fetch', return_value=(self.url, self.doc)) as fetch, \
             patch.object(qual_api, 'summarise') as model:
            row = qual_api.read_site(self.conn, self.pid, self.url, force=True)
        fetch.assert_called_once()
        model.assert_not_called()
        self.assertEqual(row['summary'], first['summary'])
        self.assertEqual(row['tags'], first['tags'])

    def test_expired_and_future_reads_cannot_supply_current_tags(self):
        self.read()
        for at in ('2000-01-01T00:00:00Z', '2999-01-01T00:00:00Z', 'invalid'):
            self.conn.execute('UPDATE site_reads SET at=?', (at,))
            row = qual_api.site_row(self.conn, self.pid)
            self.assertTrue(row['stale'])
            self.assertEqual(row['tags'], [])

    def test_paused_reads_show_facts_and_cache_without_model_calls(self):
        db.set_setting(self.conn, 'qualify', False)
        with patch.object(qual_api, 'fetch', return_value=(self.url, self.doc)) as fetch, \
             patch.object(qual_api, 'summarise') as model:
            first = qual_api.read_site(self.conn, self.pid, self.url)
            second = qual_api.read_site(self.conn, self.pid, self.url)
        fetch.assert_called_once()
        model.assert_not_called()
        self.assertEqual(first, second)
        self.assertIn('AI: Ad tracking detected', first['tags'])
        self.assertNotIn('AI: Skincare', first['tags'])

    def test_malformed_field_types_are_rejected_without_exceptions(self):
        for field, value in [('product_category', []), ('product_category', {}),
                             ('stage', []), ('us_market', 1), ('sells_physical_products', None),
                             ('summary', []), ('summary', '   ')]:
            data = dict(self.data, **{field: value})
            self.assertIsNone(qual_api._valid_summary(data))
        with patch.object(qual_api.qualify, '_providers') as provider:
            provider.return_value.chat.return_value = ('{"summary":"Shop", "product_category": []}', 'fake')
            data, model = qual_api.summarise(self.url, 'Glow', '', '', {})
        self.assertIsNone(data)

    def test_evidence_writes_invalidate_lead_revision(self):
        before = int(self.conn.execute("SELECT value FROM settings WHERE key='lead_data_rev'").fetchone()[0])
        self.read()
        after = int(self.conn.execute("SELECT value FROM settings WHERE key='lead_data_rev'").fetchone()[0])
        self.assertGreater(after, before)
        self.conn.execute('DELETE FROM site_evidence')
        deleted = int(self.conn.execute("SELECT value FROM settings WHERE key='lead_data_rev'").fetchone()[0])
        self.assertGreater(deleted, after)

    def test_source_change_requires_model_even_if_page_is_same(self):
        self.read()
        self.url = 'https://other.co'
        db.upsert_person(self.conn, {'handle': 'glow', 'website': self.url})
        with patch.object(qual_api, 'fetch', return_value=('https://glow.co', self.doc)), \
             patch.object(qual_api, 'summarise', return_value=(self.data, 'test-model')) as model:
            row = qual_api.read_site(self.conn, self.pid, self.url)
        model.assert_called_once()
        self.assertEqual(row['url'], self.url)


class AiTags(unittest.TestCase):
    def test_only_from_model_fields(self):
        v = {'role': 'buyer', 'fit': 80, 'niche': 'Skincare', 'stage': 'growing', 'decision_maker': True, 'runs_ads': True, 'us_market': None}
        tags = qualify.ai_tags(v, 80)
        self.assertEqual(tags, ['AI: Skincare', 'AI: Growing', 'AI: Decision maker', 'AI: Runs ads', 'AI: Top fit'])
        self.assertEqual(qualify.ai_tags({'role': 'unrelated', 'niche': 'Skincare', 'stage': 'early'}, 20), [])

    def test_verdict_marks_ai_group_and_rules_never(self):
        v = qualify._verdict({'role': 'buyer', 'fit': 70, 'reason': 'Ships to the US.', 'us_market': True,
                              'evidence': ['skincare brand']},
                             {'handle': 'a', 'bio': 'Founder of a skincare brand, ships to the US'}, [], 'm', 'q')
        self.assertIn(('AI: US market', 'ai'), v['tags'])
        rv = qualify.rule_verdict({'handle': 'a', 'bio': 'founder of glow skincare'}, [])
        self.assertFalse(any(g == 'ai' for _, g in rv.get('tags') or []))


if __name__ == '__main__':
    unittest.main()
