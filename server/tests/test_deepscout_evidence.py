"""Offline evidence checks. No Hermes, public network, or live database."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')

import db
import deepscout as scout


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'synthetic.sqlite')
        self.conn = db.init(self.path)
        scout.ensure(self.conn)
        self.pid = db.upsert_person(self.conn, {'handle': 'glow', 'name': 'Glow Goods',
                                               'bio': 'Founder of Glow Goods, a skincare brand.',
                                               'website': 'https://glow.example'})
        self.conn.execute("INSERT INTO verdicts(person_id,model,score,content_fit,tier) "
                          "VALUES(?,'offline-bulk',72,72,'hot')", (self.pid,))
        self.conn.commit()
        self.p = dict(self.conn.execute('SELECT * FROM people WHERE id=?', (self.pid,)).fetchone())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def answer(self, source='profile.bio', quote='Founder of Glow Goods, a skincare brand.', **changes):
        result = {'verdict': 'strong', 'reachable': True, 'summary': 'A skincare company.',
                  'tags': ['Skincare'], 'sources': [], 'evidence': [{'source': source, 'quote': quote}]}
        result.update(changes)
        return result

    def verdict(self):
        return tuple(self.conn.execute('SELECT model,score,content_fit FROM verdicts WHERE person_id=?',
                                       (self.pid,)).fetchone())

    def test_exact_profile_quote_promotes_and_malformed_quote_does_not(self):
        self.assertTrue(scout.apply(self.conn, self.p, self.answer()))
        self.assertEqual(self.verdict()[0], 'leadscout')
        self.conn.execute("UPDATE verdicts SET model='offline-bulk',score=72,content_fit=72 WHERE person_id=?", (self.pid,))
        self.assertFalse(scout.apply(self.conn, self.p, self.answer(quote='Not in the actual bio')))
        self.assertEqual(self.verdict(), ('offline-bulk', 72, 72))
        self.assertFalse(scout.fresh(self.conn, self.p))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE grp='scout'").fetchone()[0], 0)
        self.assertEqual(scout.candidates(self.conn, 5, set()), [])  # one-hour retry delay
        self.conn.execute("UPDATE deep_research SET retry_after='2000-01-01'")
        self.assertEqual([p['id'] for p in scout.candidates(self.conn, 5, set())], [self.pid])

    def test_fabricated_and_unrelated_urls_never_promote(self):
        with patch('qual_api.fetch') as fetch:
            self.assertFalse(scout.apply(self.conn, self.p, self.answer(
                source='https://unrelated.example/about', quote='We sell skincare products.')))
            fetch.assert_not_called()
            self.assertEqual(self.verdict(), ('offline-bulk', 72, 72))
            fetch.return_value = ('https://glow.example/about', '<html><body>We sell skincare products.</body></html>')
            self.assertFalse(scout.apply(self.conn, self.p, self.answer(
                source='https://glow.example/about', quote='A fabricated owner claim.')))
            self.assertEqual(self.verdict(), ('offline-bulk', 72, 72))
            self.assertTrue(scout.apply(self.conn, self.p, self.answer(
                source='https://glow.example/about', quote='We sell skincare products.')))
            self.assertEqual(self.verdict()[0], 'leadscout')

    def test_shared_host_needs_matching_profile_path(self):
        self.p['website'] = 'https://linktr.ee/glowgoods'
        with patch('qual_api.fetch') as fetch:
            self.assertFalse(scout.verify(self.p, self.answer(
                source='https://linktr.ee/unrelated', quote='We sell skincare products.'))[0])
            fetch.assert_not_called()

    def test_private_profile_website_is_rejected_before_http(self):
        self.p['website'] = 'http://127.0.0.1:1/'
        ok, reason = scout.verify(self.p, self.answer(
            source='http://127.0.0.1:1/', quote='We sell skincare products.'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'cited page could not be verified')

    def test_redirected_page_and_invalid_schema_fail_closed(self):
        with patch('qual_api.fetch', return_value=('https://other.example', '<p>Founder of Glow Goods</p>')):
            self.assertFalse(scout.apply(self.conn, self.p, self.answer(
                source='https://glow.example/about', quote='Founder of Glow Goods')))
        self.assertFalse(scout.apply(self.conn, self.p, self.answer(reachable='yes')))
        self.assertFalse(scout.apply(self.conn, self.p, self.answer(evidence=[])))
        with patch('qual_api.fetch', return_value=('https://glow.example/about', '<p>We use cookies for analytics.</p>')):
            self.assertFalse(scout.apply(self.conn, self.p, self.answer(
                source='https://glow.example/about', quote='We use cookies for analytics.')))
        self.assertEqual(self.verdict(), ('offline-bulk', 72, 72))

    def test_every_worker_reply_is_recorded_and_unverified_keeps_bulk(self):
        pool = scout.ScoutPool(self.path)
        with patch.object(scout, 'run', return_value=self.answer(quote='Invented quote')):
            pool._work(self.p)
        self.assertEqual(self.verdict(), ('offline-bulk', 72, 72))
        self.assertEqual(tuple(tuple(r) for r in self.conn.execute('SELECT outcome,verification_reason FROM deep_research_runs')),
                         (('unverified', 'quote not found in cited source'),))
        with patch.object(scout, 'run', return_value=None):
            pool._work(self.p)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM deep_research_runs').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT outcome FROM deep_research_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'failed')
        self.assertEqual(self.verdict(), ('offline-bulk', 72, 72))

    def test_legacy_row_loses_trust_on_upgrade(self):
        self.conn.execute('DROP TABLE deep_research')
        self.conn.execute("CREATE TABLE deep_research(person_id INTEGER PRIMARY KEY,verdict TEXT,reachable INT,"
                          "summary TEXT,tags TEXT,sources TEXT,raw TEXT,at TEXT NOT NULL)")
        self.conn.execute("INSERT INTO deep_research VALUES(?,'strong',1,'Old claim','[]','[]','{}',?)",
                          (self.pid, db.now()))
        self.conn.execute("UPDATE verdicts SET model='leadscout',score=90,content_fit=85 WHERE person_id=?", (self.pid,))
        scout.ensure(self.conn)
        self.assertFalse(scout.result(self.conn, self.pid)['verified'])
        self.assertFalse(scout.fresh(self.conn, self.p))
        self.assertEqual(self.verdict()[0], 'rules')
        self.assertIsNone(self.verdict()[1])


if __name__ == '__main__':
    unittest.main()
