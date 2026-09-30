"""Source feedback depends on distinct marked members, not audience size."""
import tempfile
import unittest
from pathlib import Path

import db
import owner
import server


class NetworkYieldScale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'network.sqlite')
        self.ids = {name: db.upsert_person(self.conn, {'handle': name}) for name in
                    ('lead', 'positive', 'negative', 'client', 'contacted', 'friend', 'other')}
        for name, seed, directions in (('lead', 'a', ('followers',)), ('lead', 'b', ('following',)),
                                        ('positive', 'a', ('followers', 'following')), ('positive', 'b', ('followers',)),
                                        ('negative', 'a', ('followers', 'following')), ('client', 'a', ('followers', 'following')),
                                        ('contacted', 'b', ('followers',)), ('friend', 'a', ('followers',)),
                                        ('other', 'unrelated', ('following',))):
            for direction in directions:
                db.add_edge(self.conn, seed, self.ids[name], direction)
        for name, status in (('positive', 'interested'), ('negative', 'no'), ('contacted', 'contacted'),
                             ('friend', None), ('other', 'talking')):
            self.conn.execute('INSERT INTO marks(person_id,status) VALUES(?,?)', (self.ids[name], status))
        for name, relationships in (('negative', '["client"]'), ('client', '["client"]'), ('friend', '["friend"]')):
            self.conn.execute('INSERT INTO owner_context VALUES(?,?,NULL,?)', (self.ids[name], relationships, 'now'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def check(self):
        # Independent oracle: enumerate current memberships, deduplicate both
        # directions, then count only explicit effective feedback.
        feedback = {row['person_id']: row['status'] for row in self.conn.execute(owner.feedback_marks_sql())}
        candidates = []
        for seed in ('a', 'b'):
            members = {row[0] for row in self.conn.execute('SELECT person_id FROM current_edges WHERE seed=?', (seed,))}
            marked = [feedback[pid] for pid in members if feedback.get(pid) is not None]
            good = sum(status in ('interested', 'talking', 'client') for status in marked)
            if marked:
                candidates.append(((good + 1) / (len(marked) + 4), len(marked)))
        expected_yield, expected_marked = max(candidates)
        db.set_setting(self.conn, 'map_seed_member_v1', True)
        prepared = server.network_context(self.conn, [self.ids['lead']], me='')[self.ids['lead']]
        db.set_setting(self.conn, 'map_seed_member_v1', False)
        fallback = server.network_context(self.conn, [self.ids['lead']], me='')[self.ids['lead']]
        self.assertEqual(prepared, fallback)
        self.assertEqual((prepared['seed_yield'], prepared['seed_marked']), (round(expected_yield, 3), expected_marked))

    def test_distinct_directions_owner_feedback_and_evidence_changes(self):
        self.check()
        self.conn.execute('UPDATE edge_evidence SET active=0 WHERE person_id=?', (self.ids['negative'],))
        self.conn.execute("UPDATE marks SET status='no' WHERE person_id=?", (self.ids['positive'],))
        self.check()
        self.conn.execute("UPDATE owner_context SET relationships='[]' WHERE person_id=?", (self.ids['client'],))
        self.check()


if __name__ == '__main__':
    unittest.main()
