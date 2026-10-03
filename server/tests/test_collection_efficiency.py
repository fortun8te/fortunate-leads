import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import collection_efficiency
import db


class CollectionEfficiencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(Path(self.temp.name) / 'test.sqlite')
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)

    def event(self, direction='following', returned=200, saved=180, new=80, outcome='page', age=1):
        self.conn.execute(
            'INSERT INTO collector_events(at,kind,direction,outcome,returned_count,saved_entries,new_links) '
            "VALUES(?,'list',?,?,?,?,?)",
            ((self.now - timedelta(hours=age)).isoformat(), direction, outcome, returned, saved, new))

    def test_separate_directions_actual_distinct_memberships_and_no_failed_page_inflation(self):
        self.event()
        self.event(saved=0, new=0)
        self.event('followers', 25, 25, 20)
        self.event(outcome='rate_limit')
        self.event(age=25)
        self.event(age=-1)
        out = collection_efficiency.summary(self.conn, self.now)
        following, followers = out['directions']
        self.assertEqual((following['pages'], following['returned_entries'], following['saved_entries'],
                          following['saved_per_page'], following['new_connections']), (2, 400, 180, 90, 80))
        self.assertEqual(followers['saved_per_page'], 25)
        self.assertEqual(out['scope'], 'successful_list_pages')
        self.assertFalse(out['sampled'])

    def test_legacy_missing_metrics_stay_unknown(self):
        self.event(saved=None, new=None)
        out = collection_efficiency.summary(self.conn, self.now)['directions'][0]
        self.assertIsNone(out['saved_entries'])
        self.assertIsNone(out['saved_per_page'])
        self.assertIsNone(out['new_connections'])

    def test_bounded_sample_is_labelled_and_read_only(self):
        for _ in range(4):
            self.event()
        self.conn.commit()
        before = self.conn.total_changes
        with patch.object(collection_efficiency, 'MAX_EVENTS', 2):
            out = collection_efficiency.summary(self.conn, self.now)
        self.assertTrue(out['sampled'])
        self.assertEqual(out['directions'][0]['pages'], 2)
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.conn.in_transaction)

    def test_large_non_list_history_does_not_get_scanned(self):
        self.conn.executemany(
            "INSERT INTO collector_events(at,kind,outcome) VALUES(?,'profile','ok')",
            [(self.now.isoformat(),)] * 100000)
        self.event()
        self.conn.commit()
        # A row LIMIT alone still scans the irrelevant history with a date-only index.
        steps = [0]

        def track():
            steps[0] += 1000
            return int(steps[0] > 10000)

        self.conn.set_progress_handler(track, 1000)
        try:
            result = collection_efficiency.summary(self.conn, self.now)
        finally:
            self.conn.set_progress_handler(None, 0)
        self.assertEqual(result['directions'][0]['pages'], 1)
        self.assertLess(steps[0], 10000)
