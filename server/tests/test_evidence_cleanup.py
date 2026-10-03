"""Quarantine is reviewed, reversible and local; owner facts are untouched."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import deepscout
import evidence_cleanup as cleanup


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'offline.sqlite'))
        deepscout.ensure(self.conn)
        self.pid = db.upsert_person(self.conn, {'handle': 'skincare_brand',
            'bio': 'Founder of a skincare brand. We sell skincare products.'})
        self.person = dict(self.conn.execute('SELECT * FROM people WHERE id=?', (self.pid,)).fetchone())
        self.conn.execute("INSERT INTO verdicts(person_id,model,score,content_fit) VALUES(?,'rules',62,62)", (self.pid,))
        self.conn.execute("INSERT INTO marks VALUES(?,'interested','PRIVATE OWNER NOTE',?)", (self.pid,db.now()))
        self.conn.execute("INSERT INTO tags VALUES(?,'Owner label','signal','manual')", (self.pid,))
        db.set_setting(self.conn, 'processing_mode', 'R')
        self.data = {'verdict': 'strong', 'reachable': True,
            'summary': 'A well funded founder with a million euro ad budget.',
            'tags': ['Skincare', 'Million euro budget', 'Can hire an agency'], 'sources': [],
            'evidence': [{'source':'profile.bio','quote':'Founder of a skincare brand'}]}
        deepscout.apply(self.conn, self.person, self.data, verification=(False,'No checked evidence'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_preview_has_no_private_or_generated_text_and_writes_nothing(self):
        before = self.conn.total_changes
        report = cleanup.preview(self.conn)
        self.assertEqual(report['count'], 1)
        self.assertIn('budget_claim_needs_evidence', report['entries'][0]['reasons'])
        self.assertNotIn('PRIVATE OWNER NOTE', json.dumps(report))
        self.assertNotIn('well funded', json.dumps(report))
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(cleanup._table(self.conn, 'evidence_quarantine'))

    def test_unverified_free_prose_is_never_displayed_even_before_cleanup(self):
        result = deepscout.result(self.conn, self.pid)
        self.assertFalse(result['verified'])
        self.assertEqual(result['summary'], '')
        self.assertEqual(result['tags'], [])
        self.assertIsNone(result['reachable'])

    def test_quarantine_restore_preserves_notes_settings_and_unknown_content(self):
        report = cleanup.preview(self.conn)
        with self.conn:
            result = cleanup.quarantine(self.conn,report,report['review_hash'])
        self.assertEqual(result['quarantined'], 1)
        self.assertTrue(deepscout.result(self.conn,self.pid)['quarantined'])
        self.assertEqual(self.conn.execute('SELECT summary FROM deep_research').fetchone()[0], '')
        restore = cleanup.restore_preview(self.conn)
        with self.conn:
            cleanup.restore(self.conn,restore,restore['review_hash'])
        self.assertEqual(self.conn.execute('SELECT summary FROM deep_research').fetchone()[0], self.data['summary'])
        self.assertEqual(deepscout.result(self.conn,self.pid)['summary'], '')
        self.assertEqual(self.conn.execute('SELECT note FROM marks').fetchone()[0], 'PRIVATE OWNER NOTE')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE source='manual'").fetchone()[0], 1)
        self.assertEqual(db.get_setting(self.conn, 'processing_mode'),'R')

    def test_later_preview_batches_are_not_hidden_by_earlier_quarantine(self):
        second = db.upsert_person(self.conn, {'handle': 'later_unverified', 'bio': 'Saved profile'})
        later = dict(self.conn.execute('SELECT * FROM people WHERE id=?', (second,)).fetchone())
        deepscout.apply(self.conn, later, self.data, verification=(False, 'No checked evidence'))
        self.conn.commit()
        first = cleanup.preview(self.conn, limit=1)
        self.assertEqual(first['entries'][0]['person_id'], self.pid)
        with self.conn:
            cleanup.quarantine(self.conn, first, first['review_hash'])
        next_report = cleanup.preview(self.conn, limit=1)
        self.assertEqual(next_report['count'], 1)
        self.assertEqual(next_report['entries'][0]['person_id'], second)
        with self.conn:
            cleanup.quarantine(self.conn, next_report, next_report['review_hash'])
        self.assertEqual(cleanup.preview(self.conn, limit=1)['count'], 0)

    def test_changed_record_or_tampered_manifest_cannot_apply(self):
        report = cleanup.preview(self.conn)
        report['entries'][0]['expected_hash'] = '0'*64
        with self.assertRaises(cleanup.ReviewChanged):
            cleanup.quarantine(self.conn,report,report['review_hash'])
        report = cleanup.preview(self.conn)
        self.conn.execute("UPDATE deep_research SET summary='New evidence' WHERE person_id=?",(self.pid,))
        with self.assertRaises(cleanup.ReviewChanged):
            cleanup.quarantine(self.conn,report,report['review_hash'])
        self.conn.rollback()
        self.assertFalse(cleanup.active(self.conn,self.pid))

    def test_restore_cannot_overwrite_new_profile_or_research(self):
        report = cleanup.preview(self.conn)
        with self.conn:
            cleanup.quarantine(self.conn,report,report['review_hash'])
        restore = cleanup.restore_preview(self.conn)
        self.conn.execute("UPDATE people SET bio='New profile source' WHERE id=?",(self.pid,))
        with self.assertRaises(cleanup.ReviewChanged):
            cleanup.restore(self.conn,restore,restore['review_hash'])
        self.conn.rollback()
        self.assertTrue(cleanup.active(self.conn,self.pid))

    def test_verified_result_shows_quotes_and_only_supported_tags(self):
        deepscout.apply(self.conn,self.person,self.data,verification=(True,None))
        result = deepscout.result(self.conn,self.pid)
        self.assertTrue(result['verified'])
        self.assertEqual(result['summary'],'Profile says: "Founder of a skincare brand".')
        self.assertEqual(result['tags'],['Skincare'])
        self.assertNotIn('million',self.conn.execute('SELECT reason FROM verdicts').fetchone()[0])
        self.assertFalse(any('budget' in r[0] for r in self.conn.execute("SELECT tag FROM tags WHERE source='auto'")))

    def test_corrupt_stored_tags_and_sources_cannot_crash_or_add_unchecked_links(self):
        deepscout.apply(self.conn,self.person,self.data,verification=(True,None))
        for tags, sources in [('null', 'not json'), ('12', '["https://unrelated.example"]'),
                              ('{}', 'null'), ('not json', '12')]:
            with self.subTest(tags=tags, sources=sources):
                self.conn.execute('UPDATE deep_research SET tags=?,sources=? WHERE person_id=?',
                                  (tags,sources,self.pid))
                result=deepscout.result(self.conn,self.pid)
                self.assertTrue(result['verified'])
                self.assertEqual(result['tags'],[])
                self.assertEqual(result['sources'],[])  # The actual evidence is profile.bio.
        # A checked website quotation supplies its own link independently of
        # legacy stored source metadata, including when that metadata is broken.
        self.conn.execute("UPDATE people SET website='https://shop.example' WHERE id=?",(self.pid,))
        person=dict(self.conn.execute('SELECT * FROM people WHERE id=?',(self.pid,)).fetchone())
        data=dict(self.data,evidence=[{'source':'https://shop.example/about','quote':'We sell skincare products.'}])
        deepscout.apply(self.conn,person,data,verification=(True,None))
        self.conn.execute("UPDATE deep_research SET tags='null',sources='not json' WHERE person_id=?",(self.pid,))
        self.assertEqual(deepscout.result(self.conn,self.pid)['sources'],['https://shop.example/about'])

    def test_new_checked_result_supersedes_quarantine_without_erasing_backup(self):
        report = cleanup.preview(self.conn)
        cleanup.quarantine(self.conn,report,report['review_hash'])
        deepscout.apply(self.conn,self.person,self.data,verification=(True,None))
        self.assertFalse(cleanup.active(self.conn,self.pid))
        self.assertEqual(cleanup.restore_preview(self.conn)['count'],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_quarantine').fetchone()[0],1)
        self.assertTrue(deepscout.result(self.conn,self.pid)['verified'])

    def test_cleanup_has_no_network_or_model_path(self):
        with patch.object(deepscout,'_fetch_cited_page',side_effect=AssertionError('No network')):
            report=cleanup.preview(self.conn)
            cleanup.quarantine(self.conn,report,report['review_hash'])
            self.assertEqual(deepscout.result(self.conn,self.pid)['summary'],'')


if __name__ == '__main__':
    unittest.main()
