"""Planner source counts match current evidence, including zero and duplicate directions."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import server  # noqa: E402


class ProfilePlannerDegreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))
        self.addCleanup(self.conn.close)
        self.ts = '2026-01-01T00:00:00+00:00'
        db.set_setting(self.conn, 'bio_min', 0)
        db.set_setting(self.conn, 'budget', {'list': 10, 'profile': 0})

    def person(self, handle):
        pid = db.upsert_person(self.conn, {'handle': handle})
        self.conn.execute('INSERT INTO verdicts(person_id,prefilter,updated_at) VALUES(?,?,?)',
                          (pid, 50, self.ts))
        return pid

    def jobs(self):
        return dict(self.conn.execute("SELECT handle,priority FROM jobs WHERE kind='profile'"))

    def test_current_distinct_seeds_and_zero_degree(self):
        zero = self.person('zero')
        duplicate = self.person('duplicate')
        two = self.person('two')
        historical = self.person('historical')
        db.add_edge(self.conn, 's1', duplicate, 'followers', self.ts)
        db.add_edge(self.conn, 's1', duplicate, 'following', self.ts)
        db.add_edge(self.conn, 's1', two, 'followers', self.ts)
        db.add_edge(self.conn, 's2', two, 'followers', self.ts)
        db.add_edge(self.conn, 's3', historical, 'followers', self.ts)
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE person_id=?", (historical,))
        self.assertEqual(dict(self.conn.execute('SELECT person_id,degree FROM map_person_degree')),
                         {duplicate: 1, two: 2})

        # While a list is collecting, only two different current sources qualify.
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('waiting','followers','queued')")
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 1)
        self.assertEqual(self.jobs(), {'two': server.plan_priority(2, 50)})

        # A second direction from the same source must still count only once.
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE person_id=? AND seed='s2'", (two,))
        self.conn.execute('DELETE FROM jobs')
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 0)

        # Once lists complete, people with no current sources remain eligible.
        self.conn.execute("UPDATE lists SET state='done'")
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 4)
        self.assertEqual(self.jobs(), {'zero': server.plan_priority(0, 50),
                                      'duplicate': server.plan_priority(1, 50),
                                      'two': server.plan_priority(1, 50),
                                      'historical': server.plan_priority(0, 50)})
        self.assertNotIn(zero, dict(self.conn.execute('SELECT person_id,degree FROM map_person_degree')))

    def test_unready_summary_uses_exact_count(self):
        pid = self.person('candidate')
        db.add_edge(self.conn, 's1', pid, 'followers', self.ts)
        db.add_edge(self.conn, 's2', pid, 'followers', self.ts)
        self.conn.execute('DELETE FROM map_person_degree')
        self.conn.execute("DELETE FROM settings WHERE key='map_person_degree_v1'")
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('waiting','followers','queued')")
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 1)
        self.assertEqual(self.jobs(), {'candidate': server.plan_priority(2, 50)})


if __name__ == '__main__':
    unittest.main()
