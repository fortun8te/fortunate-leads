"""Connection evidence refreshes existing rankings without inventing a new fit."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control
import processing_modes
import db
import qualify
import server


class NetworkReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'network.sqlite')
        db.set_setting(self.conn, 'qualify', True)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, ig_id=None):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig_id,
                                            'bio': 'Founder of a skincare brand'})

    def source_tags(self, pid):
        return {r[0] for r in self.conn.execute(
            "SELECT tag FROM tags WHERE person_id=? AND source='auto' AND grp='source'", (pid,))}

    def stored(self, pid):
        return self.conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()

    def fake_existing_verdict(self, pid, fit=90):
        server.qualify_batch(self.conn)
        snap = server.network_snapshot(self.conn, [pid])[pid]
        score = qualify.blend(fit, snap['net'])
        self.conn.execute("UPDATE verdicts SET model='offline-model',score=?,content_fit=?,input_hash=?,tier=? WHERE person_id=?",
                          (score, fit, qualify.input_hash(snap['person'], snap['edges'], snap['net']),
                           qualify._tier(score, True), pid))
        self.conn.execute("INSERT OR IGNORE INTO tags VALUES(?,'AI: Top fit','ai','auto')", (pid,))

    def test_seed_rename_replaces_source_tag_and_keeps_existing_fit(self):
        c = self.conn
        c.execute("INSERT INTO seeds(handle,ig_id,is_me) VALUES('oldseed','100',0)")
        self.person('oldseed', '100')
        lead = self.person('lead', '200')
        db.add_edge(c, 'oldseed', lead, 'following')
        self.fake_existing_verdict(lead)
        self.assertIn('via @oldseed', self.source_tags(lead))
        prior = server.network_snapshot(c, [lead])
        db.upsert_person(c, {'ig_id': '100', 'handle': 'newseed'})
        server.refresh_network(c, [lead], prior)
        self.assertEqual(self.source_tags(lead), {'via @newseed'})
        self.assertEqual(self.stored(lead)['model'], 'offline-model')
        self.assertEqual(self.stored(lead)['input_hash'], qualify.input_hash(
            server.network_snapshot(c, [lead])[lead]['person'], server.edges_of(c, lead),
            server.network_context(c, [lead])[lead]))
        self.assertEqual(server.qualify_batch(c), 1)  # renamed seed only
        self.assertEqual(self.stored(lead)['model'], 'offline-model')

    def test_disproved_edge_reblends_and_keeps_ai_tag_without_new_fit(self):
        c = self.conn
        c.execute("INSERT INTO seeds(handle,ig_id,is_me) VALUES('me','100',1)")
        lead = self.person('lead', '200')
        db.add_edge(c, 'me', lead, 'following', '2026-02-01')
        self.fake_existing_verdict(lead)
        before = self.stored(lead)['score']
        prior = server.network_snapshot(c, [lead])
        c.execute("UPDATE edge_evidence SET active=0,checked_at='2026-02-02' WHERE seed='me' AND person_id=?", (lead,))
        server.refresh_network(c, [lead], prior)
        self.assertLess(self.stored(lead)['score'], before)
        self.assertEqual(self.stored(lead)['model'], 'offline-model')
        self.assertEqual(self.source_tags(lead), set())
        self.assertEqual(c.execute("SELECT count(*) FROM tags WHERE person_id=? AND tag='AI: Top fit'", (lead,)).fetchone()[0], 1)

    def test_unknown_fit_stays_unknown_when_connection_changes(self):
        c = self.conn
        lead = self.person('lead')
        prior = server.network_snapshot(c, [lead])
        db.add_edge(c, 's', lead, 'followers')
        server.refresh_network(c, [lead], prior)
        self.assertIsNone(self.stored(lead))
        self.assertEqual(self.source_tags(lead), {'via @s'})

    def test_marked_edge_change_reblends_peer_from_prior_snapshot(self):
        c = self.conn
        marked, peer = self.person('marked'), self.person('peer')
        db.add_edge(c, 's', marked, 'followers')
        db.add_edge(c, 's', peer, 'followers')
        server.set_status(c, [marked], status='client')
        self.fake_existing_verdict(peer)
        before = self.stored(peer)['score']
        prior = server.network_snapshot(c, [marked, peer])
        c.execute("UPDATE edge_evidence SET active=0,checked_at='2026-02-02' WHERE seed='s' AND person_id=?", (marked,))
        server.refresh_network(c, [marked, peer], prior)
        self.assertLess(self.stored(peer)['score'], before)
        self.assertEqual(self.stored(peer)['model'], 'offline-model')

    def test_dirty_drain_preserves_content_and_recalculates_peer_yield_while_paused(self):
        c = self.conn
        marked, peer, unknown = self.person('marked'), self.person('peer'), self.person('unknown')
        db.add_edge(c, 's', peer, 'followers')
        self.fake_existing_verdict(peer)
        server.set_status(c, [marked], status='client')
        c.execute('DELETE FROM network_dirty')
        old = dict(self.stored(peer))
        # Pause AI without changing its cumulative mode or discarding saved fit.
        # Stop all deliberately selects R and is a different contract now.
        processing_modes.set_paused(c, True)
        db.add_edge(c, 's', marked, 'followers')
        db.add_edge(c, 's', unknown, 'followers')
        c.execute('DELETE FROM verdicts WHERE person_id=?', (unknown,))
        with patch.object(qualify, 'rule_verdict', side_effect=AssertionError('new fit forbidden')), \
             patch.object(qualify, 'llm_verdicts', side_effect=AssertionError('models forbidden')):
            self.assertFalse(server.local_processing_step(c))
            self.assertFalse(server.LLMPool().step(c))
            server.drain_network_dirty(c)
        after = self.stored(peer)
        self.assertGreater(after['score'], old['score'])
        for field in ('content_fit', 'model', 'reason', 'role', 'input_hash'):
            self.assertEqual(after[field], old[field])
        self.assertIsNone(self.stored(unknown))
        self.assertEqual(self.source_tags(unknown), {'via @s'})
        self.assertEqual(c.execute('SELECT count(*) FROM network_dirty').fetchone()[0], 0)

    def test_dirty_compare_delete_keeps_newer_work(self):
        c = self.conn
        pid = self.person('lead')
        db.mark_network_dirty(c, [pid])
        refresh = server.refresh_network
        def update_during_refresh(conn, ids):
            result = refresh(conn, ids)
            db.mark_network_dirty(conn, [pid])
            return result
        with patch.object(server, 'refresh_network', side_effect=update_during_refresh):
            self.assertEqual(server.drain_network_dirty(c), 1)
        self.assertEqual(c.execute('SELECT count(*) FROM network_dirty').fetchone()[0], 1)
        server.drain_network_dirty(c)
        self.assertEqual(c.execute('SELECT count(*) FROM network_dirty').fetchone()[0], 0)

    def test_changed_content_is_not_certified_by_network_refresh(self):
        c = self.conn
        pid = self.person('lead')
        self.fake_existing_verdict(pid)
        old = dict(self.stored(pid))
        c.execute("UPDATE people SET bio='Now a different business',updated_at='2099' WHERE id=?", (pid,))
        db.add_edge(c, 'seed', pid, 'following')
        server.drain_network_dirty(c)
        current = self.stored(pid)
        self.assertEqual(current['updated_at'], old['updated_at'])
        self.assertEqual(current['input_hash'], old['input_hash'])

    def test_paused_seed_rename_drains_without_snapshot(self):
        c = self.conn
        c.execute("INSERT INTO seeds(handle,ig_id,is_me) VALUES('oldseed','100',0)")
        self.person('oldseed', '100')
        pid = self.person('lead')
        db.add_edge(c, 'oldseed', pid, 'following')
        self.fake_existing_verdict(pid)
        db.set_setting(c, 'qualify', False)
        db.upsert_person(c, {'ig_id': '100', 'handle': 'newseed'})
        server.background_qualify(c)
        self.assertEqual(self.source_tags(pid), {'via @newseed'})
        self.assertEqual(self.stored(pid)['model'], 'offline-model')


if __name__ == '__main__':
    unittest.main()
