"""Atomic interaction regressions against isolated SQLite databases, without HTTP or live data."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import server
import workflows


INITIAL_TIME = '2026-09-20T10:00:00.000000+00:00'
SAVE_TIME = '2026-09-26T12:00:00.000000+00:00'
MANUAL_TIME = '2026-09-25T13:00:00.000000+00:00'


class AtomicInteractions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'atomic.sqlite')
        self.conn = db.init(self.path)
        with patch.object(db, 'now', return_value=INITIAL_TIME):
            self.pid = db.upsert_person(self.conn, {'handle': 'atomic_alice', 'ig_id': 'atomic-1'})
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)',
                          (self.pid, 'contacted', 'Keep the profile note', INITIAL_TIME))
        self.conn.execute('INSERT INTO followups VALUES(?,?,?,?,?)',
                          (self.pid, '2026-09-25', 'Send the old portfolio', None, INITIAL_TIME))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def log(self, **changes):
        body = {'kind': 'reply', 'body': '  Wants examples  ',
                'happened_at': '2026-09-25T15:00:00+02:00'}
        body.update(changes)
        with patch.object(db, 'now', return_value=SAVE_TIME):
            return workflows.api_interaction(self.conn, {}, body, self.pid)

    def snapshot(self, conn=None):
        conn = conn or self.conn
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('people', 'marks', 'followups', 'activity')}

    def events(self):
        rows = []
        for row in self.conn.execute('SELECT * FROM activity ORDER BY id'):
            out = dict(row)
            for field in ('before_value', 'after_value'):
                out[field] = json.loads(out[field]) if out[field] is not None else None
            rows.append(out)
        return rows

    def mark(self):
        row = self.conn.execute('SELECT status,note,updated_at FROM marks WHERE person_id=?',
                                (self.pid,)).fetchone()
        return dict(row) if row else None

    def assert_unchanged(self, before):
        # Do not roll back in the test: the endpoint must release its own failed transaction.
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.snapshot(), before)
        reader = db.connect(self.path)
        try:
            self.assertEqual(self.snapshot(reader), before)
        finally:
            reader.close()

    def test_omitted_options_preserve_current_reminder_status_and_profile_note(self):
        before = self.snapshot()
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        response = self.log()
        after = self.snapshot()
        for table in ('people', 'marks', 'followups'):
            self.assertEqual(after[table], before[table])
        self.assertEqual(response['status'], 'contacted')
        self.assertEqual(response['follow_up'], old_follow_up)
        self.assertEqual(response['rows'], workflows.history(self.conn, self.pid)['rows'])
        self.assertIsNone(response['next_cursor'])
        [manual] = self.events()
        self.assertEqual((manual['kind'], manual['body']), ('reply', 'Wants examples'))
        self.assertEqual(manual['happened_at'], MANUAL_TIME)
        self.assertEqual(manual['created_at'], SAVE_TIME)
        self.assertIsNone(manual['before_value'])
        self.assertIsNone(manual['after_value'])

    def test_one_save_commits_manual_status_and_schedule_with_exact_history(self):
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        response = self.log(status='talking', follow_up={'due_on': '2026-10-02', 'note': '  Send examples  '})
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(response['status'], 'talking')
        self.assertEqual(self.mark()['note'], 'Keep the profile note')
        new_follow_up = {'due_on': '2026-10-02', 'note': 'Send examples',
                         'completed_at': None, 'updated_at': SAVE_TIME}
        self.assertEqual(response['follow_up'], new_follow_up)
        events = self.events()
        self.assertCountEqual([row['kind'] for row in events], ['reply', 'status', 'follow_up_scheduled'])
        changes = {row['kind']: row for row in events}
        self.assertEqual((changes['status']['before_value'], changes['status']['after_value']),
                         ('contacted', 'talking'))
        self.assertEqual(changes['follow_up_scheduled']['before_value'], old_follow_up)
        self.assertEqual(changes['follow_up_scheduled']['after_value'], new_follow_up)
        for kind in ('status', 'follow_up_scheduled'):
            self.assertEqual(changes[kind]['happened_at'], SAVE_TIME)
            self.assertEqual(changes[kind]['created_at'], SAVE_TIME)
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.mark()['status'], 'talking')
        self.assertEqual(workflows.follow_up(self.conn, self.pid), new_follow_up)
        self.assertEqual(self.events(), events)

    def test_unchanged_explicit_values_append_only_manual_event(self):
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        self.log(status='contacted', follow_up={'due_on': old_follow_up['due_on'], 'note': old_follow_up['note']})
        self.assertEqual([row['kind'] for row in self.events()], ['reply'])
        self.assertEqual(workflows.follow_up(self.conn, self.pid), old_follow_up)

    def test_status_only_preserves_reminder(self):
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        response = self.log(status='no')
        self.assertEqual(response['status'], 'no')
        self.assertEqual(response['follow_up'], old_follow_up)
        self.assertCountEqual([row['kind'] for row in self.events()], ['reply', 'status'])

    def test_null_status_clears_status_but_keeps_profile_note(self):
        response = self.log(status=None)
        self.assertIsNone(response['status'])
        self.assertIsNone(self.mark()['status'])
        self.assertEqual(self.mark()['note'], 'Keep the profile note')
        status_event = next(row for row in self.events() if row['kind'] == 'status')
        self.assertEqual(status_event['before_value'], 'contacted')
        self.assertIsNone(status_event['after_value'])

    def test_status_clear_removes_empty_mark_and_returns_null(self):
        self.conn.execute('UPDATE marks SET note=NULL WHERE person_id=?', (self.pid,))
        self.conn.commit()
        response = self.log(status=None)
        self.assertIsNone(self.mark())
        self.assertIsNone(response['status'])
        self.assertCountEqual([row['kind'] for row in self.events()], ['reply', 'status'])

    def test_all_current_statuses_and_legacy_good_are_supported(self):
        for supplied, expected in [('interested', 'interested'), ('contacted', 'contacted'),
                                   ('talking', 'talking'), ('client', 'client'), ('no', 'no'),
                                   ('good', 'interested')]:
            with self.subTest(status=supplied):
                response = self.log(status=supplied)
                self.assertEqual(response['status'], expected)
                self.assertEqual(self.mark()['status'], expected)

    def test_complete_and_schedule_preserves_completed_reminder_in_history(self):
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        response = self.log(follow_up={'action': 'complete_and_schedule',
                                     'due_on': '2026-10-03', 'note': 'Review examples'})
        self.assertEqual(response['status'], 'contacted')
        self.assertEqual(response['follow_up'], {'due_on': '2026-10-03', 'note': 'Review examples',
                                               'completed_at': None, 'updated_at': SAVE_TIME})
        events = self.events()
        self.assertCountEqual([row['kind'] for row in events],
                              ['reply', 'follow_up_completed', 'follow_up_scheduled'])
        completed = next(row for row in events if row['kind'] == 'follow_up_completed')
        scheduled = next(row for row in events if row['kind'] == 'follow_up_scheduled')
        self.assertLess(completed['id'], scheduled['id'])
        self.assertEqual(completed['before_value'], old_follow_up)
        self.assertEqual(completed['after_value'], dict(old_follow_up, completed_at=SAVE_TIME, updated_at=SAVE_TIME))
        self.assertEqual(scheduled['before_value'], completed['after_value'])
        self.assertEqual(scheduled['after_value'], response['follow_up'])

    def test_complete_and_schedule_without_open_reminder_does_not_invent_completion(self):
        for previous in ('missing', 'completed'):
            with self.subTest(previous=previous):
                self.conn.execute('DELETE FROM activity')
                if previous == 'missing':
                    self.conn.execute('DELETE FROM followups')
                else:
                    self.conn.execute('UPDATE followups SET completed_at=? WHERE person_id=?',
                                      (INITIAL_TIME, self.pid))
                self.conn.commit()
                old_follow_up = workflows.follow_up(self.conn, self.pid)
                response = self.log(follow_up={'action': 'complete_and_schedule', 'due_on': '2026-10-03'})
                self.assertCountEqual([row['kind'] for row in self.events()], ['reply', 'follow_up_scheduled'])
                scheduled = next(row for row in self.events() if row['kind'] == 'follow_up_scheduled')
                self.assertEqual(scheduled['before_value'], old_follow_up)
                self.assertEqual(scheduled['after_value'], response['follow_up'])
                self.assertIsNone(response['follow_up']['completed_at'])
                self.assertEqual(response['follow_up']['note'], '')

    def test_repeated_completion_logs_each_interaction_but_only_one_completion(self):
        first = self.log(follow_up={'action': 'complete'})
        second = self.log(body='A separate later call', kind='call', follow_up={'action': 'complete'})
        self.assertEqual(first['follow_up'], second['follow_up'])
        self.assertEqual(second['follow_up']['completed_at'], SAVE_TIME)
        self.assertCountEqual([row['kind'] for row in self.events()], ['reply', 'call', 'follow_up_completed'])

    def test_repeated_clear_logs_each_interaction_but_only_one_clear(self):
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        first = self.log(follow_up={'action': 'clear'})
        second = self.log(follow_up={'action': 'clear'})
        self.assertIsNone(first['follow_up'])
        self.assertIsNone(second['follow_up'])
        self.assertCountEqual([row['kind'] for row in self.events()], ['reply', 'reply', 'follow_up_cleared'])
        cleared = next(row for row in self.events() if row['kind'] == 'follow_up_cleared')
        self.assertEqual(cleared['before_value'], old_follow_up)
        self.assertIsNone(cleared['after_value'])

    def test_completing_missing_reminder_does_not_create_history_or_state(self):
        self.conn.execute('DELETE FROM followups')
        self.conn.commit()
        response = self.log(follow_up={'action': 'complete'})
        self.assertIsNone(response['follow_up'])
        self.assertEqual([row['kind'] for row in self.events()], ['reply'])

    def test_schedule_reopens_completed_reminder_and_allows_explicit_schedule_action(self):
        self.conn.execute('UPDATE followups SET completed_at=? WHERE person_id=?', (INITIAL_TIME, self.pid))
        self.conn.commit()
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        response = self.log(follow_up={'action': 'schedule', 'due_on': old_follow_up['due_on'], 'note': old_follow_up['note']})
        self.assertIsNone(response['follow_up']['completed_at'])
        self.assertCountEqual([row['kind'] for row in self.events()], ['reply', 'follow_up_scheduled'])
        scheduled = next(row for row in self.events() if row['kind'] == 'follow_up_scheduled')
        self.assertEqual(scheduled['before_value'], old_follow_up)
        self.assertEqual(scheduled['after_value'], response['follow_up'])

    def test_invalid_status_never_partially_saves_log_or_valid_reminder(self):
        before = self.snapshot()
        for value in ('', 'open', 'Contacted', 'talking ', 0, True, [], {}):
            with self.subTest(status=value):
                with self.assertRaises(ValueError):
                    self.log(status=value, follow_up={'action': 'complete_and_schedule', 'due_on': '2026-10-02'})
                self.assert_unchanged(before)

    def test_invalid_reminder_never_partially_saves_log_or_valid_status(self):
        before = self.snapshot()
        invalid = [None, False, [], '2026-10-02', {}, {'due_on': None}, {'due_on': 20261002},
                   {'due_on': '2026-2-02'}, {'due_on': '2026-02-29'}, {'due_on': '2026-09-31'},
                   {'due_on': '2026-10-02T12:00:00Z'}, {'due_on': '2026-10-02', 'note': None},
                   {'due_on': '2026-10-02', 'note': 42}, {'due_on': '2026-10-02', 'note': 'x' * 501},
                   {'action': 'delete'}, {'action': 'complete', 'due_on': '2026-10-02'},
                   {'action': 'clear', 'note': ''}, {'action': 'complete_and_schedule'},
                   {'action': 'complete_and_schedule', 'due_on': '2026-02-30'},
                   {'action': 'schedule', 'due_on': '2026-10-00'}]
        for value in invalid:
            with self.subTest(follow_up=value):
                with self.assertRaises(ValueError):
                    self.log(status='talking', follow_up=value)
                self.assert_unchanged(before)

    def test_invalid_manual_fields_never_apply_valid_status_or_completion(self):
        before = self.snapshot()
        invalid = [{'kind': None}, {'kind': 'email'}, {'kind': []}, {'body': None}, {'body': 12},
                   {'body': ''}, {'body': ' \n\t '}, {'body': 'x' * 5001}]
        for fields in invalid:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    self.log(status='talking', follow_up={'action': 'complete'}, **fields)
                self.assert_unchanged(before)

    def test_invalid_timestamps_never_save_any_part_of_the_request(self):
        before = self.snapshot()
        for stamp in (None, 0, [], {}, '', 'yesterday', '2026-09-26', '2026-09-26T12:00:00',
                      '2026-02-30T12:00:00Z', '2026-09-26T25:00:00Z', '2026-09-26T12:00:00+25:00',
                      '0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00'):
            with self.subTest(happened_at=stamp):
                with self.assertRaises(ValueError):
                    self.log(happened_at=stamp, status='talking', follow_up={'action': 'complete'})
                self.assert_unchanged(before)

    def test_database_failure_after_prior_writes_rolls_back_every_change(self):
        before = self.snapshot()
        self.conn.execute("""CREATE TEMP TRIGGER reject_schedule_history BEFORE INSERT ON activity
                             WHEN NEW.kind = 'follow_up_scheduled'
                             BEGIN SELECT RAISE(ABORT, 'test rejects final history write'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'test rejects final history write'):
            self.log(status='talking', follow_up={'action': 'complete_and_schedule',
                                               'due_on': '2026-10-02', 'note': 'New reminder'})
        self.assert_unchanged(before)
        self.conn.execute('DROP TRIGGER reject_schedule_history')
        response = self.log(status='talking', follow_up={'due_on': '2026-10-02'})
        self.assertEqual(response['status'], 'talking')
        self.assertEqual(response['follow_up']['due_on'], '2026-10-02')
        self.assertEqual(len(self.events()), 3)

    def test_response_read_failure_rolls_back_changes_before_client_retries(self):
        before = self.snapshot()
        with patch.object(workflows, 'history', side_effect=sqlite3.OperationalError('test history read failed')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'test history read failed'):
                self.log(status='talking', follow_up={'action': 'complete_and_schedule', 'due_on': '2026-10-02'})
        self.assert_unchanged(before)
        response = self.log(status='talking', follow_up={'action': 'complete_and_schedule', 'due_on': '2026-10-02'})
        self.assertEqual(response['status'], 'talking')
        self.assertEqual(response['follow_up']['due_on'], '2026-10-02')
        self.assertCountEqual([row['kind'] for row in self.events()],
                              ['reply', 'status', 'follow_up_completed', 'follow_up_scheduled'])

    def test_backdated_interactions_never_infer_status_or_complete_existing_work(self):
        self.conn.execute("UPDATE marks SET status='no' WHERE person_id=?", (self.pid,))
        self.conn.commit()
        before = self.snapshot()
        old_follow_up = workflows.follow_up(self.conn, self.pid)
        for kind in ('dm', 'reply', 'call', 'meeting', 'note'):
            with self.subTest(kind=kind):
                response = self.log(kind=kind, happened_at='2000-01-02T00:30:00+02:00')
                self.assertEqual(response['status'], 'no')
                self.assertEqual(response['follow_up'], old_follow_up)
                for table in ('people', 'marks', 'followups'):
                    self.assertEqual(self.snapshot()[table], before[table])
        self.assertEqual([row['kind'] for row in self.events()], ['dm', 'reply', 'call', 'meeting', 'note'])
        for row in self.events():
            self.assertEqual(row['happened_at'], '2000-01-01T22:30:00.000000+00:00')
            self.assertEqual(row['created_at'], SAVE_TIME)

    def test_new_person_without_status_or_reminder_returns_null_current_state(self):
        self.pid = db.upsert_person(self.conn, {'handle': 'atomic_bob'})
        self.conn.commit()
        response = self.log()
        self.assertIsNone(response['status'])
        self.assertIsNone(response['follow_up'])
        self.assertEqual(len(response['rows']), 1)
        self.assertEqual(response['rows'][0]['kind'], 'reply')

    def test_response_keeps_paginated_history_contract(self):
        for index in range(51):
            self.log(body=f'Prior note {index}', kind='note')
        response = self.log(status='talking')
        self.assertEqual(len(response['rows']), 50)
        self.assertIsInstance(response['next_cursor'], str)
        older = workflows.api_history(self.conn, {'cursor': [response['next_cursor']]}, {}, self.pid)
        combined = response['rows'] + older['rows']
        self.assertEqual(len(combined), 53)
        self.assertEqual(len({row['id'] for row in combined}), 53)
        self.assertIsNone(older['next_cursor'])
        self.assertEqual(combined, workflows.history(self.conn, self.pid, {'limit': ['100']})['rows'])


if __name__ == '__main__':
    unittest.main()
