"""The automatic AI switch must respect incomplete Instagram collection."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server


class AutoQualifyGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'test.sqlite')
        db.set_setting(self.conn, 'qualify_auto', True)
        self.conn.execute("INSERT INTO seeds(handle) VALUES('seed')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def complete(self, direction):
        job_id = self.conn.execute(
            "INSERT INTO jobs(kind,seed,direction,state) VALUES('list','seed',?,'done')",
            (direction,)).lastrowid
        self.conn.execute("INSERT INTO list_runs(job_id,first_page_seen,total,total_source,member_count) "
                          "VALUES(?,1,1,'current_run',1)", (job_id,))
        self.conn.execute('INSERT INTO list_page_requests(job_id,requested_cursor,next_cursor) VALUES(?,?,NULL)',
                          (job_id, ''))
        self.conn.execute("INSERT INTO lists(seed,direction,state,received,total,run_job_id) "
                          "VALUES('seed',?,'done',1,1,?)", (direction, job_id))
        self.conn.commit()
        return job_id

    def test_missing_direction_or_active_job_keeps_ai_off(self):
        self.complete('followers')
        self.assertFalse(server.auto_qualify(self.conn))
        self.complete('following')
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list','seed','followers','queued')")
        self.conn.commit()
        self.assertFalse(server.auto_qualify(self.conn))
        self.assertFalse(db.get_setting(self.conn, 'qualify'))

    def test_verified_complete_lists_enable_ai(self):
        self.complete('followers')
        self.complete('following')
        self.assertTrue(server.auto_qualify(self.conn))
        self.assertTrue(db.get_setting(self.conn, 'qualify'))

    def test_unverified_done_list_does_not_enable_ai(self):
        job = self.complete('followers')
        self.complete('following')
        self.conn.execute('DELETE FROM list_page_requests WHERE job_id=?', (job,))
        self.conn.commit()
        self.assertFalse(server.auto_qualify(self.conn))

    def test_recoverable_partial_and_error_keep_ai_off(self):
        self.complete('followers')
        self.complete('following')
        self.conn.execute("UPDATE lists SET state='partial',error='Instagram ended early' "
                          "WHERE direction='followers'")
        self.conn.commit()
        self.assertFalse(server.auto_qualify(self.conn))
        self.conn.execute("UPDATE lists SET state='error' WHERE direction='followers'")
        self.conn.commit()
        self.assertFalse(server.auto_qualify(self.conn))
        self.assertFalse(db.get_setting(self.conn, 'qualify'))

    def test_explicit_cap_and_private_list_allow_ai(self):
        self.complete('followers')
        self.complete('following')
        self.conn.execute("UPDATE lists SET state='partial',error='Instagram limited this list; coverage is partial.' "
                          "WHERE direction='followers'")
        self.conn.execute("UPDATE lists SET state='private' WHERE direction='following'")
        self.conn.commit()
        self.assertTrue(server.auto_qualify(self.conn))

    def test_private_partial_and_exhausted_retry_budget_allow_ai(self):
        self.complete('followers')
        self.complete('following')
        self.conn.execute("UPDATE lists SET state='partial',released_why='private' WHERE direction='followers'")
        self.conn.execute("UPDATE lists SET state='partial',error='Instagram ended early' WHERE direction='following'")
        db.set_setting(self.conn, 'lists_reopened', {'seed|following': db.REOPEN_MAX})
        self.conn.commit()
        self.assertTrue(server.auto_qualify(self.conn))


if __name__ == '__main__':
    unittest.main()
