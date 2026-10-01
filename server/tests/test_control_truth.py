"""Collection activity must describe requests and committed results, not job leases."""
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import control


class CollectionTruthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'truth.sqlite')
        self.now = datetime.now(timezone.utc)
        self.at = self.now.isoformat()
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,handle,role,last_seen) VALUES('alt','2','alternate','both',?)", (self.at,))
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,state,lane,leased_until) VALUES('list','seed','following','leased','alt',?)", ((self.now + timedelta(minutes=10)).isoformat(),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_lease_alone_is_waiting_not_reading(self):
        state = control.snapshot(self.conn)
        self.assertEqual(state['accounts'][0]['state'], 'waiting')
        self.assertEqual(state['stages'][0]['state'], 'waiting')
        self.assertNotEqual(state['accounts'][0]['status'], 'running')

    def test_active_request_and_stop_confirmation(self):
        db.set_setting(self.conn, 'instagram_request_gate', {'active': {'lane': 'alt', 'kind': 'list', 'until': self.now.timestamp() + 100}})
        self.assertEqual(control.snapshot(self.conn)['accounts'][0]['state'], 'running')
        control.stop_all(self.conn)
        state = control.snapshot(self.conn)
        self.assertEqual(state['accounts'][0]['state'], 'stopping')
        self.assertFalse(state['stop_acknowledged'])
        db.set_setting(self.conn, 'instagram_request_gate', {'active': None})
        self.assertTrue(control.snapshot(self.conn)['stop_acknowledged'])

    def test_distinct_new_profiles_are_not_returned_list_entries(self):
        pid = db.upsert_person(self.conn, {'handle': 'person'})
        self.conn.executemany("INSERT INTO collector_events(event_id,at,kind,outcome,saved_entries) VALUES(?,?,'list','page',?)", [('a', self.at, 20), ('b', self.at, 20)])
        self.conn.execute("INSERT INTO collector_events(event_id,at,kind,outcome,reason) VALUES('read',?,'profile','profile','saved')", (self.at,))
        self.conn.execute("INSERT INTO collector_events(event_id,at,kind,outcome,reason) VALUES('stale',?,'profile','profile','stale_capture')", (self.at,))
        state = control.snapshot(self.conn)
        self.assertEqual(state['progress']['unique_new_profiles']['hour'], 1)
        self.assertEqual(state['progress']['saved_list_entries']['hour'], 40)
        self.assertEqual(state['progress']['bios_read']['hour'], 1)
        self.assertEqual(state['stages'][0]['unit'], 'list entries')

    def test_reserved_main_does_not_mask_alternate_daily_cap(self):
        self.conn.execute('DELETE FROM jobs')
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list','seed','following','queued')")
        self.conn.execute("UPDATE accounts SET today='{\"list\":200}',budget='{\"list\":200,\"profile\":100}' WHERE lane_id='alt'")
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,handle,role,is_main,last_seen) VALUES('main','1','owner','both',1,?)", (self.at,))
        state = control.snapshot(self.conn)
        self.assertEqual(state['accounts'][0]['reason_code'], 'main_reserved')
        self.assertEqual(state['stages'][0]['wait']['why'], 'Daily request budget reached')

    def test_expired_request_is_unconfirmed_not_running(self):
        db.set_setting(self.conn, 'instagram_request_gate', {'active': {'lane': 'alt', 'kind': 'list', 'until': self.now.timestamp() - 1}})
        state = control.snapshot(self.conn)
        self.assertTrue(state['collection']['unconfirmed'])
        self.assertEqual(state['accounts'][0]['state'], 'waiting')
        self.assertEqual(state['stages'][0]['state'], 'waiting')

    def test_leased_account_at_daily_limit_reports_limit(self):
        self.conn.execute("UPDATE accounts SET today='{\"list\":200}',budget='{\"list\":200}' WHERE lane_id='alt'")
        state = control.snapshot(self.conn)
        self.assertEqual(state['accounts'][0]['wait']['why'], 'Daily request budget reached')
        self.assertEqual(state['stages'][0]['wait']['why'], 'Daily request budget reached')

    def test_progress_queries_use_timestamp_indexes(self):
        for query, index in [
            ("SELECT count(*) FROM people WHERE first_seen>=?", 'people_first_seen'),
            ("SELECT coalesce(sum(saved_entries),0) FROM collector_events WHERE at>=? AND outcome='page'", 'collector_events_at'),
            ("SELECT count(*) FROM collector_events WHERE at>=? AND kind='profile' AND outcome='profile' AND reason='saved'", 'collector_events_at'),
        ]:
            plan = ' '.join(row[3] for row in self.conn.execute('EXPLAIN QUERY PLAN ' + query, (self.at,)))
            self.assertIn(index, plan)
            self.assertIn('SEARCH', plan)


if __name__ == '__main__':
    unittest.main()
