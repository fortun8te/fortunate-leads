import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import processing_progress as progress
import processing_state as state


class ProcessingProgress(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.temp.name) / 'test.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_failed_attempts_do_not_inflate_speed_and_pauses_hide_eta(self):
        pid = db.upsert_person(self.conn, {'handle': 'testbrand', 'bio': 'Shop our products'})
        for n in range(10):
            with patch.object(progress.time, 'time', return_value=1000+n*12):
                progress.record(self.conn, pid, {'status': 'complete' if n % 2 else 'unverified'}, 5)
        result = progress.snapshot(self.conn, 100, now=1120)
        self.assertEqual((result['attempts'], result['successful'], result['unverified']), (10,5,5))
        self.assertEqual(result['per_minute'], 2.4)
        self.assertLessEqual(abs(result['eta_seconds'] - 2500), 1)
        self.assertIsNone(progress.snapshot(self.conn,100,paused=True,now=1120)['eta_seconds'])
        self.assertEqual(progress.snapshot(self.conn,100,now=1400)['per_minute'],0)

    def test_owner_edits_first_then_business_fit_bands(self):
        low = db.upsert_person(self.conn, {'handle':'firstlow','bio':'A bio'})
        high = db.upsert_person(self.conn, {'handle':'secondhigh','bio':'Our product brand'})
        own = db.upsert_person(self.conn, {'handle':'ownedit','bio':'Someone I know'})
        self.conn.execute('INSERT INTO rule_assessments(person_id,content_fit) VALUES(?,80)',(high,))
        state.refresh_rank_step(self.conn)
        self.conn.execute('UPDATE local_queue SET priority=1 WHERE person_id=?',(own,))
        rows = state.next_pending(self.conn,limit=3)
        self.assertEqual([r['person_id'] for r in rows],[own,high,low])

    def test_active_item_is_bounded_and_does_not_leak_note(self):
        pid = db.upsert_person(self.conn, {'handle':'active','bio':'Bio'})
        with patch.object(progress.time,'time',return_value=1000):
            progress.active(self.conn,pid)
        self.assertEqual(progress.snapshot(self.conn,1,now=1001)['active']['handle'],'active')
        self.assertIsNone(progress.snapshot(self.conn,1,now=1091)['active'])


if __name__ == '__main__':
    unittest.main()
