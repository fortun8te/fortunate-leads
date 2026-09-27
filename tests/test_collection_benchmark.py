"""The benchmark must count new links and honor its time window."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT))
import db  # noqa: E402
from ops.collection_benchmark import report  # noqa: E402


class BenchmarkTest(unittest.TestCase):
    def test_new_links_and_experiment_use_the_requested_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init(Path(tmp) / 'test.sqlite')
            try:
                conn.execute("INSERT INTO jobs(id,kind,seed,direction,state,page_size) "
                             "VALUES(1,'list','seed25','followers','queued',25),"
                             "(2,'list','seed50','followers','queued',50),"
                             "(3,'list','old25','followers','queued',25)")
                db.set_setting(conn, 'page_experiment_latest', {
                    'created_at': '2026-09-27T09:45:00+00:00', 'job_ids': [1, 2]})
                conn.execute("INSERT INTO edges VALUES('seed25',1,'followers','2026-09-27T10:05:00+00:00')")
                conn.execute("INSERT INTO edges VALUES('seed50',2,'followers','2026-09-27T09:50:00+00:00')")
                conn.execute("INSERT INTO pages VALUES(1,'a','2026-09-27T10:05:00+00:00','lane',50)")
                conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,requested_count,"
                             "returned_count,new_links) VALUES('2026-09-27T10:05:00+00:00','lane',1,'list',"
                             "'followers','page',25,50,1)")
                conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,requested_count,"
                             "returned_count,new_links) VALUES('2026-09-27T09:50:00+00:00','lane',2,'list',"
                             "'followers','page',50,50,1)")
                conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,requested_count,"
                             "returned_count,new_links) VALUES('2026-09-27T10:20:00+00:00','lane',3,'list',"
                             "'followers','page',25,25,0)")
                conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,reason,http_status) "
                             "VALUES('2026-09-27T10:10:00+00:00','lane',1,'list','followers',"
                             "'rate_limit','http_429',429)")
                conn.commit()
                result = report(conn, datetime(2026, 9, 27, 10, tzinfo=timezone.utc),
                                datetime(2026, 9, 27, 11, tzinfo=timezone.utc))
                self.assertEqual(result['new_links_all_sources']['followers'], 1)
                self.assertEqual(result['collector_new_links']['followers'], 1)
                self.assertEqual(result['pages_by_account'][0]['returned_users'], 50)
                self.assertEqual([(row['page_size'], row['accepted_pages'], row['rate_limits'])
                                  for row in result['experiment_in_window']], [(25, 1, 1)])
                self.assertEqual([row['page_size'] for row in result['experiment_cohort_current']], [25, 50])
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
