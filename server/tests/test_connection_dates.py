"""Recorded-date regressions using fictional imports and temporary databases.

Dates bound saved observations. These tests do not imply follow validity intervals,
simultaneous paths, or an unfollow when a later page omits an earlier member.
"""

import itertools
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db
import migrate


EARLY = '2026-01-01T10:00:00.000000+00:00'
MIDDLE = '2026-01-02T10:00:00.000000+00:00'
LATE = '2026-01-03T10:00:00.000000+00:00'
IMPORT_TIME = '2026-09-26T10:00:00.000000+00:00'


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class ConnectionDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'leads.sqlite'
        self.conn = db.init(str(self.path))
        self.addCleanup(self.conn.close)

    def person(self, handle, ig_id):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig_id}, EARLY)

    def observe(self, seed, seed_id, member, member_id, direction, when):
        db.add_edge(self.conn, seed, member, direction, when,
                    seed_ig_id=seed_id, person_ig_id=member_id)

    def evidence(self, source='ig:101', target='ig:202'):
        return connections.explore(self.conn, source, target)['edges'][0]['evidence']

    def import_old(self, dates, destination=None):
        """Exercise the actual supported research-database migration format."""
        old_path = Path(self.tmp.name) / 'old.sqlite'
        with sqlite3.connect(old_path) as old:
            old.executescript('''
                CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, platform TEXT, handle TEXT,
                    platform_id TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, relationship TEXT, observed_date TEXT);
                CREATE TABLE IF NOT EXISTS collections(source_entity TEXT, side TEXT, displayed_count INT,
                    captured_count INT, coverage TEXT, observed_date TEXT, stop_reason TEXT);
                CREATE TABLE IF NOT EXISTS profile_current(entity_id TEXT, field TEXT, value_json TEXT,
                    observed_at TEXT);
                CREATE TABLE IF NOT EXISTS exporter_commands(type TEXT, payload TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS known_people(entity_id TEXT, relation TEXT, note TEXT, added_at TEXT);
                DELETE FROM edges;
                DELETE FROM collections;
            ''')
            old.executemany('INSERT OR IGNORE INTO entities VALUES(?,?,?,?,?)', [
                ('source', 'instagram', 'fictional_source', '101', EARLY),
                ('member', 'instagram', 'fictional_member', '202', EARLY),
            ])
            old.executemany('INSERT INTO edges VALUES(?,?,?,?)', [
                ('source', 'member', 'follows', date) for date in dates
            ])
            old.execute('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',
                        ('source', 'following', 1, 1, 'complete', dates[-1], 'end'))
        self.conn.commit()
        with patch.object(db, 'now', return_value=IMPORT_TIME):
            return migrate.migrate(old_path, destination or self.path, None)

    def test_older_archive_imported_later_preserves_original_recording_bounds(self):
        self.import_old([MIDDLE, LATE])
        self.import_old([EARLY])
        result = self.evidence()
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['first_seen'], result[0]['last_seen']), (EARLY, LATE))
        self.import_old([EARLY])
        self.assertEqual(self.evidence(), result)
        self.assertNotIn(IMPORT_TIME, (result[0]['first_seen'], result[0]['last_seen']))

    def test_supporting_lists_keep_separate_dates_and_reverse_edge(self):
        a, b = self.person('fictional_a', '101'), self.person('fictional_b', '202')
        observations = [
            ('fictional_a', '101', b, '202', 'following', MIDDLE),
            ('fictional_a', '101', b, '202', 'following', EARLY),
            ('fictional_b', '202', a, '101', 'followers', LATE),
            ('fictional_b', '202', a, '101', 'following', MIDDLE),
        ]
        expected = None
        for order in itertools.permutations(observations):
            self.conn.execute('DELETE FROM edge_observations')
            self.conn.execute('DELETE FROM edges')
            for observation in order:
                self.observe(*observation)
            result = connections.explore(self.conn, 'ig:101', 'ig:202')
            current = result['edges']
            if expected is None:
                expected = current
            self.assertEqual(current, expected)
            self.assertEqual(connections.explore(self.conn, 'ig:202', 'ig:101')['edges'], expected)
        forward, reverse = expected
        self.assertEqual((forward['from'], forward['to']), ('ig:101', 'ig:202'))
        self.assertEqual([(e['seed'], e['direction'], e['first_seen'], e['last_seen'])
                          for e in forward['evidence']], [
            ('fictional_a', 'following', EARLY, MIDDLE),
            ('fictional_b', 'followers', LATE, LATE),
        ])
        self.assertEqual((reverse['from'], reverse['to']), ('ig:202', 'ig:101'))
        self.assertEqual([(e['first_seen'], e['last_seen']) for e in reverse['evidence']], [(MIDDLE, MIDDLE)])
        self.assertTrue(forward['reciprocal'])
        self.assertTrue(reverse['reciprocal'])

    def test_legacy_last_recording_stays_unknown_after_queue_update_and_reopen(self):
        member = self.person('fictional_legacy_member', '202')
        self.conn.execute('INSERT INTO edges VALUES(?,?,?,?)',
                          ('fictional_legacy_source', member, 'following', EARLY))
        self.conn.execute("DELETE FROM settings WHERE key='edge_observations_v1'")
        self.conn.commit()
        migrated = db.init(str(self.path))
        migrated.close()
        source, target = 'legacy:fictional_legacy_source', 'legacy-person:' + str(member)
        before = self.evidence(source, target)
        self.assertEqual((before[0]['first_seen'], before[0]['last_seen']), (EARLY, None))
        with patch.object(db, 'now', return_value=LATE):
            self.assertTrue(db.queue_list(self.conn, 'fictional_legacy_source', 'following'))
        self.conn.commit()
        reopened = db.init(str(self.path))
        reopened.close()
        self.assertEqual(self.evidence(source, target), before)
        result = connections.explore(self.conn, source, target)
        coverage = next(c for c in result['coverage'] if c['seed'] == 'fictional_legacy_source'
                        and c['direction'] == 'following')
        self.assertEqual(coverage['updated_at'], LATE)
        self.assertIsNone(result['edges'][0]['evidence'][0]['last_seen'])

    def test_empty_later_page_and_requeue_change_list_date_without_refreshing_edge(self):
        # Lazy import avoids importing test_server or changing its qualification
        # stub during discovery. Calling the handler does not open a socket.
        import server

        db.queue_list(self.conn, 'fictional_source', 'following')
        job_id = self.conn.execute('SELECT id FROM jobs').fetchone()[0]
        payload = {'job_id': job_id, 'seed': 'fictional_source', 'ig_id': '101',
                   'direction': 'following', 'next_cursor': 'page-2', 'total': 2,
                   'users': [{'handle': 'fictional_member', 'ig_id': '202'}]}
        with patch.object(db, 'now', return_value=EARLY):
            self.assertEqual(server.ext_list_page(self.conn, {}, payload), {'received': 1})
        before = self.evidence()
        with patch.object(db, 'now', return_value=MIDDLE):
            empty_page = dict(payload, users=[], next_cursor='page-3')
            self.assertEqual(server.ext_list_page(self.conn, {}, empty_page), {'received': 1})
        self.assertEqual(self.evidence(), before)
        with patch.object(db, 'now', return_value=LATE):
            self.assertTrue(db.queue_list(self.conn, 'fictional_source', 'following'))
        result = connections.explore(self.conn, 'ig:101', 'ig:202')
        coverage = next(c for c in result['coverage'] if c['seed'] == 'fictional_source'
                        and c['direction'] == 'following')
        self.assertEqual((coverage['state'], coverage['updated_at']), ('queued', LATE))
        self.assertEqual(result['edges'][0]['evidence'], before)
        self.assertEqual((before[0]['first_seen'], before[0]['last_seen']), (EARLY, EARLY))
        self.assertEqual(result['paths'][0]['nodes'], ['ig:101', 'ig:202'])

    def test_person_merge_keeps_earliest_first_and_latest_known_last(self):
        # Legacy rows have no last observation. Folding duplicate local records
        # must retain the earlier first date and the later actual observation.
        for keep_last, drop_last in ((None, LATE), (LATE, None), (None, None)):
            with self.subTest(keep_last=keep_last, drop_last=drop_last):
                self.conn.execute('DELETE FROM edge_observations')
                self.conn.execute('DELETE FROM people')
                keep = self.person('fictional_known', '202')
                drop = self.person('fictional_unidentified', None)
                for person, first, last in ((keep, MIDDLE, keep_last), (drop, EARLY, drop_last)):
                    self.conn.execute('INSERT INTO edge_observations VALUES(?,?,?,?,?,?,?)',
                                      ('fictional_source', person, 'following', '', '', first, last))
                db.merge_people(self.conn, keep, drop)
                evidence = self.evidence('legacy:fictional_source', 'legacy-person:' + str(keep))[0]
                self.assertEqual(evidence['first_seen'], EARLY)
                self.assertEqual(evidence['last_seen'], LATE if keep_last or drop_last else None)

    def test_importer_compares_iso_offset_dates_as_instants(self):
        # This is the migration API's accepted TEXT input, not a hypothetical
        # future interval feature. Chronological order differs from text order.
        dates = ['2026-01-01T23:30:00-02:00', '2026-01-02T00:30:00+00:00']
        expected = (min(map(instant, dates)), max(map(instant, dates)))
        for index, order in enumerate(itertools.permutations(dates)):
            with self.subTest(order=order):
                destination = Path(self.tmp.name) / ('offset-' + str(index) + '.sqlite')
                self.import_old(order, destination)
                imported = db.connect(str(destination))
                try:
                    evidence = connections.explore(imported, 'ig:101', 'ig:202')['edges'][0]['evidence'][0]
                    self.assertEqual((instant(evidence['first_seen']), instant(evidence['last_seen'])), expected)
                finally:
                    imported.close()

    def test_importer_compares_mixed_iso_precision_chronologically(self):
        dates = ['2026-01-01T10:00:00Z', '2026-01-01T10:00:00.500000+00:00']
        self.import_old(dates)
        evidence = self.evidence()[0]
        self.assertEqual((instant(evidence['first_seen']), instant(evidence['last_seen'])),
                         (min(map(instant, dates)), max(map(instant, dates))))

    def test_person_merge_compares_existing_iso_offset_dates_chronologically(self):
        # Existing stored values need correct comparison too; normalizing only
        # future imports would leave these accepted historical records wrong.
        keep = self.person('fictional_known', '202')
        drop = self.person('fictional_unidentified', None)
        dates = ['2026-01-01T23:30:00-02:00', '2026-01-02T00:30:00+00:00']
        for person, date in zip((keep, drop), dates):
            self.conn.execute('INSERT INTO edge_observations VALUES(?,?,?,?,?,?,?)',
                              ('fictional_source', person, 'following', '', '', date, date))
        db.merge_people(self.conn, keep, drop)
        evidence = self.evidence('legacy:fictional_source', 'legacy-person:' + str(keep))[0]
        self.assertEqual((instant(evidence['first_seen']), instant(evidence['last_seen'])),
                         (min(map(instant, dates)), max(map(instant, dates))))


if __name__ == '__main__':
    unittest.main()
