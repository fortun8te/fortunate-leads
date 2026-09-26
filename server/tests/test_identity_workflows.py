"""Identity merges preserve work and its original history in isolated databases."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import qual_api


class IdentityWorkflows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'identity.sqlite')
        self.conn = db.init(self.path)
        self.serial = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def pair(self):
        self.serial += 1
        keep = db.upsert_person(self.conn, {'handle': f'old{self.serial}', 'ig_id': str(self.serial)},
                                ts='2026-09-01T10:00:00+00:00')
        drop = db.upsert_person(self.conn, {'handle': f'current{self.serial}'},
                                ts='2026-09-02T10:00:00+00:00')
        return keep, drop

    def insert(self, table, record):
        columns = ','.join(record)
        self.conn.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in record)})',
                          tuple(record.values()))
        return self.row(table, record['person_id'])

    def row(self, table, pid):
        row = self.conn.execute(f'SELECT * FROM {table} WHERE person_id=?', (pid,)).fetchone()
        return dict(row) if row else None

    def events(self, pid, kind):
        rows = self.conn.execute('SELECT * FROM activity WHERE person_id=? AND kind=? ORDER BY id',
                                 (pid, kind)).fetchall()
        return [dict(row, before_value=json.loads(row['before_value']) if row['before_value'] else None,
                     after_value=json.loads(row['after_value']) if row['after_value'] else None) for row in rows]

    def site(self, pid, **overrides):
        self.conn.execute(qual_api.SCHEMA)
        record = dict(person_id=pid, url='https://brand.example', final_url='https://brand.example/shop',
                      title='The shop', summary='Makes small-batch candles.', signals='{"shop":"Shopify"}',
                      error=None, model='fixture-model', at='2026-09-20T10:00:00+00:00')
        record.update(overrides)
        # A site read only counts for the website currently on the profile.
        self.conn.execute('UPDATE people SET website=? WHERE id=? AND website IS NULL', (record['url'], pid))
        return self.insert('site_reads', record)

    def research(self, pid, suffix):
        return {
            'laya': self.insert('laya', dict(person_id=pid, input_hash=f'hash-{suffix}',
                                           answers=json.dumps({'evidence': f'Original answer {suffix}'}), fit=1,
                                           updated_at='2026-09-20T10:00:00+00:00')),
            'verdicts': self.insert('verdicts', dict(person_id=pid, prefilter=40, score=85, tier='hot',
                                                   role='founder', reason=f'Original reason {suffix}',
                                                   model='fixture-model', input_hash=f'hash-{suffix}',
                                                   updated_at='2026-09-20T10:00:00+00:00',
                                                   prompt=f'Original prompt\n{suffix}',
                                                   evidence=json.dumps([{'quote': f'Original quote {suffix}'}])))
        }

    def test_automatic_identity_merge_keeps_event_ids_times_and_open_reminder(self):
        cases = (
            ('completed survivor', '2026-09-01', '2026-09-02T10:00:00+00:00', '2026-09-28', None, 'drop'),
            ('completed duplicate', '2026-09-29', None, '2026-09-01', '2026-09-02T10:00:00+00:00', 'keep'),
            ('two open reminders', '2026-09-29', None, '2026-09-26', None, 'drop'),
        )
        for name, keep_due, keep_completed, drop_due, drop_completed, winner in cases:
            with self.subTest(name=name):
                keep, drop = self.pair()
                kept = self.insert('followups', dict(person_id=keep, due_on=keep_due, note='Kept next action',
                                                    completed_at=keep_completed, updated_at='2026-09-22T12:00:00+00:00'))
                dropped = self.insert('followups', dict(person_id=drop, due_on=drop_due, note='Duplicate next action',
                                                       completed_at=drop_completed, updated_at='2026-09-23T13:00:00+00:00'))
                for pid, kind, body, stamp in ((keep, 'dm', 'Sent portfolio', '2026-09-19T11:10:00.123456+00:00'),
                                               (drop, 'reply', 'Asked for samples', '2026-09-21T09:15:00.654321+00:00')):
                    self.conn.execute('INSERT INTO activity(person_id,kind,body,before_value,after_value,happened_at,created_at) '
                                      'VALUES(?,?,?,NULL,NULL,?,?)',
                                      (pid, kind, body, stamp, '2026-09-24T12:00:00.123456+00:00'))
                original = [dict(row) for row in self.conn.execute(
                    'SELECT * FROM activity WHERE person_id IN (?,?) ORDER BY id', (keep, drop))]
                # This is the production trigger: a known Instagram ID acquires the placeholder's handle.
                resolved = db.upsert_person(self.conn, {'ig_id': str(self.serial), 'handle': f'current{self.serial}'})
                self.conn.commit()
                self.assertEqual(resolved, keep)
                self.assertIsNone(self.conn.execute('SELECT id FROM people WHERE id=?', (drop,)).fetchone())
                chosen = kept if winner == 'keep' else dropped
                self.assertEqual(self.row('followups', keep), dict(chosen, person_id=keep))
                self.assertIsNone(self.row('followups', drop))
                for previous in original:
                    actual = dict(self.conn.execute('SELECT * FROM activity WHERE id=?', (previous['id'],)).fetchone())
                    self.assertEqual(actual, dict(previous, person_id=keep))
                merged = self.events(keep, 'follow_up_merged')
                self.assertEqual(len(merged), 1)
                without_id = lambda record: {key: value for key, value in record.items() if key != 'person_id'}
                self.assertEqual(merged[0]['before_value'], {'kept': without_id(kept), 'merged': without_id(dropped)})
                self.assertEqual(merged[0]['after_value'], without_id(chosen))

    def test_overflow_notes_remain_exactly_recoverable_after_a_later_note_edit(self):
        keep, drop = self.pair()
        kept_text = 'Keep this original\n' + ('café 🕯️\t' * 420) + '\nEND KEEP'
        dropped_text = 'Duplicate original\n' + ('résumé 🧵\t' * 420) + '\nEND DROP'
        self.assertGreater(len(kept_text) + len(dropped_text) + 2, 5000)
        self.assertLess(len(kept_text), 5000)
        self.assertLess(len(dropped_text), 5000)
        self.insert('marks', dict(person_id=keep, status='contacted', note=kept_text,
                                  updated_at='2026-09-23T11:00:00+00:00'))
        dropped = self.insert('marks', dict(person_id=drop, status='talking', note=dropped_text,
                                            updated_at='2026-09-24T11:00:00+00:00'))
        db.merge_people(self.conn, keep, drop)
        self.assertEqual(self.row('marks', keep)['note'], kept_text)
        self.assertEqual(self.row('marks', keep)['status'], 'talking')   # the newer explicit status wins
        # Once the editable note changes, both originals must still be recoverable in history.
        self.conn.execute('UPDATE marks SET note=? WHERE person_id=?', ('Current follow-up notes', keep))
        self.conn.commit()
        self.conn.close()
        self.conn = db.connect(self.path)
        merge = self.events(keep, 'identity_merged')
        self.assertEqual(len(merge), 1)
        self.assertEqual(merge[0]['before_value'], dropped)
        self.assertEqual(merge[0]['after_value'], {'status': 'talking', 'note': kept_text})
        self.assertIsNone(self.row('marks', drop))

    def test_duplicate_only_site_read_moves_without_an_orphan(self):
        keep, drop = self.pair()
        original = self.site(drop)
        db.merge_people(self.conn, keep, drop)
        self.conn.commit()
        self.assertEqual(self.row('site_reads', keep), dict(original, person_id=keep))
        self.assertIsNone(self.row('site_reads', drop))
        self.assertEqual(qual_api.site_row(self.conn, keep)['summary'], original['summary'])

    def test_site_conflicts_choose_newest_useful_result_and_preserve_both_sources(self):
        failed = dict(title=None, summary=None, signals='{}', model=None, error='could not reach the website')
        cases = (
            ('newer summary', {}, {'summary': 'Now sells candles and soap.', 'at': '2026-09-25T10:00:00+00:00'}, 'drop'),
            ('newer failed fetch', {}, dict(failed, at='2026-09-25T10:00:00+00:00'), 'keep'),
            ('older useful read', dict(failed, at='2026-09-25T10:00:00+00:00'), {}, 'drop'),
            ('newer facts with AI warning', {}, {'summary': None, 'model': None,
                                               'error': 'the AI was not available; page facts are still shown',
                                               'at': '2026-09-25T10:00:00+00:00'}, 'drop'),
            ('timestamp offsets', {'at': '2026-09-25T10:00:00+00:00'},
                                  {'summary': 'Read one hour later.', 'at': '2026-09-25T06:00:00-05:00'}, 'drop'),
        )
        for name, keep_changes, drop_changes, winner in cases:
            with self.subTest(name=name):
                keep, drop = self.pair()
                kept = self.site(keep, **keep_changes)
                dropped = self.site(drop, **drop_changes)
                db.merge_people(self.conn, keep, drop)
                chosen = kept if winner == 'keep' else dropped
                self.assertEqual(self.row('site_reads', keep), dict(chosen, person_id=keep))
                self.assertIsNone(self.row('site_reads', drop))
                merged = self.events(keep, 'identity_merged')
                self.assertEqual(len(merged), 1)
                self.assertEqual(merged[0]['before_value']['records']['site_reads'], {'kept': kept, 'merged': dropped})
                self.assertEqual(merged[0]['after_value']['records']['site_reads'], dict(chosen, person_id=keep))

    def test_conflicting_laya_and_verdicts_preserve_full_research_without_marks(self):
        keep, drop = self.pair()
        kept = self.research(keep, 'keep')
        dropped = self.research(drop, 'drop')
        db.merge_people(self.conn, keep, drop)
        self.conn.commit()
        merged = self.events(keep, 'identity_merged')
        self.assertEqual(len(merged), 1, 'research conflicts need history even when neither person has a note')
        for table in kept:
            with self.subTest(table=table):
                self.assertEqual(self.row(table, keep), kept[table])
                self.assertIsNone(self.row(table, drop))
                self.assertEqual(merged[0]['before_value']['records'][table],
                                 {'kept': kept[table], 'merged': dropped[table]})
                self.assertEqual(merged[0]['after_value']['records'][table], kept[table])

    def test_nonconflicting_research_moves_when_optional_site_table_is_absent(self):
        keep, drop = self.pair()
        dropped = self.research(drop, 'only duplicate')
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='site_reads'").fetchone())
        db.merge_people(self.conn, keep, drop)
        for table, original in dropped.items():
            self.assertEqual(self.row(table, keep), dict(original, person_id=keep))
            self.assertIsNone(self.row(table, drop))
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='site_reads'").fetchone())

    def test_manual_tag_origin_wins_in_either_merge_direction_and_conflicts_remain_in_history(self):
        for automatic in ('auto', 'rule'):
            for keep_source, drop_source in ((automatic, 'manual'), ('manual', automatic)):
                with self.subTest(keep=keep_source, drop=drop_source):
                    keep, drop = self.pair()
                    kept = self.insert('tags', dict(person_id=keep, tag='Founder', grp='kept-group', source=keep_source))
                    dropped = self.insert('tags', dict(person_id=drop, tag='Founder', grp='dropped-group', source=drop_source))
                    chosen = kept if keep_source == 'manual' else dropped
                    db.merge_people(self.conn, keep, drop)
                    self.assertEqual(self.row('tags', keep), dict(chosen, person_id=keep))
                    self.assertIsNone(self.row('tags', drop))
                    merge = self.events(keep, 'identity_merged')[0]
                    self.assertEqual(merge['before_value']['records']['tags'], [{'kept': kept, 'merged': dropped}])
                    self.assertEqual(merge['after_value']['records']['tags'], [dict(chosen, person_id=keep)])

    def test_overlapping_list_membership_preserves_earliest_observation(self):
        keep, drop = self.pair()
        self.insert('edges', dict(seed='Studio', person_id=keep, direction='followers', first_seen='2026-09-22'))
        self.insert('edges', dict(seed='studio', person_id=drop, direction='followers', first_seen='2026-08-01'))
        self.conn.execute('INSERT INTO edges VALUES(?,?,?,?)', ('other', drop, 'following', '2026-09-12'))
        db.merge_people(self.conn, keep, drop)
        rows = [dict(row) for row in self.conn.execute('SELECT * FROM edges WHERE person_id=? ORDER BY seed', (keep,))]
        self.assertEqual(rows, [dict(seed='other', person_id=keep, direction='following', first_seen='2026-09-12'),
                                dict(seed='Studio', person_id=keep, direction='followers', first_seen='2026-08-01')])
        self.assertIsNone(self.row('edges', drop))

    def test_failed_merge_restores_all_work_and_preserves_callers_pending_write(self):
        keep, drop = self.pair()
        self.research(keep, 'keep')
        self.research(drop, 'drop')
        self.site(drop)
        for pid in (keep, drop):
            self.insert('marks', dict(person_id=pid, status='contacted', note=f'Notes for {pid}', updated_at='2026-09-25'))
            self.insert('followups', dict(person_id=pid, due_on=f'2026-09-{27 + pid:02d}', note=f'Action for {pid}',
                                         completed_at=None, updated_at='2026-09-25'))
            self.insert('tags', dict(person_id=pid, tag='Founder', grp='role', source='manual' if pid == drop else 'auto'))
            self.insert('edges', dict(person_id=pid, seed='studio', direction='following', first_seen='2026-09-01'))
        self.conn.execute("INSERT INTO activity(person_id,kind,body,happened_at,created_at) VALUES(?,'reply','Keep this reply','2026-09-22','2026-09-23')", (drop,))
        self.conn.execute(f"CREATE TEMP TRIGGER refuse_merge BEFORE DELETE ON people WHEN old.id={drop} "
                          "BEGIN SELECT RAISE(ABORT,'simulated final delete failure'); END")
        self.conn.commit()
        tables = ('people', 'marks', 'followups', 'activity', 'tags', 'edges', 'laya', 'verdicts', 'site_reads')
        before = {table: [dict(row) for row in self.conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                  for table in tables}
        db.set_setting(self.conn, 'caller_pending', 'must survive failed merge')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'simulated final delete failure'):
            db.merge_people(self.conn, keep, drop)
        self.assertEqual(db.get_setting(self.conn, 'caller_pending'), 'must survive failed merge')
        self.conn.commit()
        for table, rows in before.items():
            with self.subTest(table=table):
                self.assertEqual([dict(row) for row in self.conn.execute(f'SELECT * FROM {table} ORDER BY rowid')], rows)


if __name__ == '__main__':
    unittest.main()
