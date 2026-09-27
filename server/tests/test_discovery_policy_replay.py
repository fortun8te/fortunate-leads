import importlib.util
from pathlib import Path
import sqlite3
import unittest

spec = importlib.util.spec_from_file_location('discovery_replay', Path(__file__).resolve().parents[2] / 'ops' / 'discovery_policy_replay.py')
replay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_module)


class DiscoveryReplay(unittest.TestCase):
    def test_fixed_budget_counts_distinct_unknown_people_and_never_completes_partial(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE jobs(id,seed,state,kind,direction,created_at);
            CREATE TABLE people(id,handle,following,first_seen);
            CREATE TABLE pages(job_id,at,users);
            CREATE TABLE list_members(job_id,person_id,observed_at);
            INSERT INTO jobs VALUES(1,'large','done','list','following','cohort'),
              (2,'small','done','list','following','cohort'),
              (3,'partial','partial','list','following','cohort');
            INSERT INTO people VALUES(1,'large',180,'2025-01-01T00:00:00Z'),
              (2,'small',1,'2025-01-01T00:00:00Z'),
              (3,'partial',2,'2025-01-01T00:00:00Z'),
              (4,'old',1,'2025-01-01T00:00:00Z'),
              (5,'new',1,'2026-01-01T01:00:00+01:00');
            INSERT INTO pages VALUES(1,'2026-01-01T00:00:00Z',1),
              (1,'2026-01-01T00:01:00Z',1),(1,'2026-01-01T00:02:00Z',1),
              (1,'2026-01-01T00:03:00Z',1),(2,'2026-01-01T00:04:00Z',2),
              (3,'2026-01-01T00:05:00Z',1);
            INSERT INTO list_members VALUES(1,4,'2026-01-01T00:00:00Z'),
              (2,4,'2026-01-01T00:04:00Z'),(2,5,'2026-01-01T00:04:00Z'),
              (3,5,'2026-01-01T00:05:00Z');
        ''')
        before = conn.total_changes
        result = replay_module.replay(conn, 'cohort', budgets=(4, 20))['results']
        original, completion = result['original_job_order'], result['bounded_completion']
        self.assertEqual((original[0]['requests_used'], original[0]['completed_lists']), (4, 0))
        self.assertEqual((completion[0]['requests_used'], completion[0]['completed_lists']), (4, 1))
        self.assertEqual(completion[0]['new_distinct_people'], 1)
        self.assertEqual(completion[1]['completed_lists'], 2)
        self.assertEqual(completion[1]['new_distinct_people'], 1)
        self.assertEqual(conn.total_changes, before)


if __name__ == '__main__':
    unittest.main()
