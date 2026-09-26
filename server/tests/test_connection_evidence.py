"""Observed relationships must not be confused with all-time discovery edges."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server
import qualify


class ConnectionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'evidence.sqlite')
        self.conn.execute("INSERT INTO seeds(handle,ig_id,is_me,added_at) VALUES('fortun8te','1',1,'2026-01-01')")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_row(self, job_id, members, state='done', total=None):
        c = self.conn
        c.execute("INSERT INTO jobs(id,kind,seed,direction,state,created_at) VALUES(?,'list','fortun8te','following',?,'2026-02-01')",
                  (job_id, state))
        c.execute("INSERT INTO list_runs(job_id,first_page_seen,total,total_source) VALUES(?,1,?,'current_run')", (job_id, total))
        c.execute('INSERT INTO list_page_requests VALUES(?,?,NULL)', (job_id, ''))
        for pid in members:
            c.execute('INSERT INTO list_members VALUES(?,?,?)', (job_id, pid, f'2026-02-{job_id:02d}'))
        c.execute('INSERT INTO lists(seed,direction,state,cursor,received,total,updated_at,run_job_id) '
                  'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(seed,direction) DO UPDATE SET state=excluded.state, '
                  'cursor=excluded.cursor,received=excluded.received,total=excluded.total,updated_at=excluded.updated_at, '
                  'run_job_id=excluded.run_job_id',
                  ('fortun8te', 'following', state, None if state == 'done' else 'next', len(members), total,
                   f'2026-02-{job_id:02d}', job_id))

    def test_complete_snapshot_disproves_old_follow_but_preserves_history_and_manual_knowledge(self):
        c = self.conn
        old = db.upsert_person(c, {'handle': 'old'})
        new = db.upsert_person(c, {'handle': 'new'})
        db.add_edge(c, 'fortun8te', old, 'following', '2026-02-01')
        db.add_edge(c, 'fortun8te', new, 'following', '2026-02-02')
        c.execute("INSERT INTO tags VALUES(?,'knows you','source','manual')", (old,))
        self.run_row(2, [new], total=1)
        self.assertEqual(db.complete_list_snapshot(c, 'fortun8te', 'following', 2, '2026-02-02'), {old})
        self.assertEqual(server.edges_of(c, old), [])
        self.assertIsNone(server.network_context(c, [old])[old]['me'])
        self.assertNotIn('you follow', {t for t, _ in qualify.rule_tags({'handle': 'old'}, server.edges_of(c, old), 'fortun8te')})
        self.assertEqual(server.edge_history_of(c, old)[0]['state'], 'absent')
        self.assertEqual(c.execute("SELECT source FROM tags WHERE person_id=? AND tag='knows you'", (old,)).fetchone()[0], 'manual')

    def test_partial_and_refresh_do_not_resurrect_or_disprove_but_positive_page_restores(self):
        c = self.conn
        old = db.upsert_person(c, {'handle': 'old'})
        new = db.upsert_person(c, {'handle': 'new'})
        db.add_edge(c, 'fortun8te', old, 'following', '2026-02-01')
        db.add_edge(c, 'fortun8te', new, 'following', '2026-02-02')
        self.run_row(2, [new], total=1)
        db.complete_list_snapshot(c, 'fortun8te', 'following', 2, '2026-02-02')
        self.run_row(3, [], state='partial', total=1)
        self.assertEqual(db.complete_list_snapshot(c, 'fortun8te', 'following', 3, '2026-02-03'), set())
        self.assertEqual(server.edges_of(c, old), [])
        self.assertEqual(len(server.edges_of(c, new)), 1)
        db.add_edge(c, 'fortun8te', old, 'following', '2026-02-04')
        self.assertEqual(len(server.edges_of(c, old)), 1)

    def test_legacy_edge_unverified_and_seed_rename_moves_evidence(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'person'})
        db.add_edge(c, 'fortun8te', pid, 'following', '2026-01-01', observed=False)
        self.assertEqual(server.edge_history_of(c, pid)[0]['state'], 'unverified')
        self.assertEqual(server.edges_of(c, pid), [])
        db.add_edge(c, 'fortun8te', pid, 'following', '2026-02-01')
        db.move_seed(c, 'fortun8te', 'newme')
        self.assertEqual(server.edges_of(c, pid)[0]['seed'], 'newme')
        self.assertEqual(server.edge_history_of(c, pid)[0]['state'], 'observed')

    def test_unknown_total_cannot_prove_absence(self):
        c = self.conn
        pid = db.upsert_person(c, {'handle': 'old'})
        db.add_edge(c, 'fortun8te', pid, 'following', '2026-02-01')
        self.run_row(2, [], total=None)
        self.assertEqual(db.complete_list_snapshot(c, 'fortun8te', 'following', 2, '2026-02-02'), set())
        self.assertEqual(len(server.edges_of(c, pid)), 1)


if __name__ == '__main__':
    unittest.main()
