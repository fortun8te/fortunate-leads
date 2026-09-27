"""Durable review queue and exact removal of cached AI influence on downgrade."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import owner_notes
import processing_modes as modes
import processing_state as state


class ProcessingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'state.sqlite')
        owner_notes.ensure(self.conn)
        state.ensure(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def person(self, handle='brand', bio='Founder of a clothing brand'):
        pid = db.upsert_person(self.conn, {'handle': handle, 'bio': bio})
        return dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone())

    def test_failed_review_keeps_bounded_reason_and_success_clears_it(self):
        p = self.person()
        state.put_review(self.conn, p['id'], {'input_hash': 'a', 'status': 'unverified', 'error': 'x' * 500})
        self.assertEqual(self.conn.execute('SELECT failure_reason FROM local_reviews').fetchone()[0], 'x' * 240)
        state.put_review(self.conn, p['id'], {'input_hash': 'b', 'status': 'complete'})
        self.assertIsNone(self.conn.execute('SELECT failure_reason FROM local_reviews').fetchone()[0])

    def verdict(self, p, model='local:k2', score=90, pre=88):
        self.conn.execute('''INSERT OR REPLACE INTO verdicts
            (person_id,prefilter,score,tier,role,reason,model,input_hash,updated_at,content_fit)
            VALUES(?,?,?,'hot','buyer','AI reasoning',?,'hash',?,85)''',
            (p['id'],pre,score,model,p['updated_at']))
        self.conn.execute("INSERT OR REPLACE INTO tags VALUES(?,'AI tag','signal','auto')", (p['id'],))

    def rules(self, p):
        state.save_rules(self.conn, p, 41, {'score': 50, 'tier': 'warm', 'role': 'unclear',
            'reason': 'Rules only', 'content_fit': 40, 'input_hash': 'rule-hash'}, [('Brand','kind')])

    def mode(self, mode):
        out = modes.set_mode(self.conn, mode)
        self.conn.commit()
        return out

    def test_r_removes_ai_and_laya_but_preserves_manual_and_history(self):
        self.mode('RLAI')
        p = self.person()
        self.rules(p)
        self.verdict(p)
        self.conn.execute("INSERT INTO tags VALUES(?,'Friend','relationship','manual')", (p['id'],))
        out = self.mode('R')
        v = self.conn.execute('SELECT * FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
        self.assertEqual((v['model'],v['score'],v['prefilter']), ('rules',50,41))
        tags = {(r['tag'],r['source']) for r in self.conn.execute('SELECT * FROM tags')}
        self.assertEqual(tags, {('Friend','manual'),('Brand','auto')})
        archived = self.conn.execute('SELECT verdict,tags FROM processing_ai_history').fetchone()
        self.assertEqual(json.loads(archived[0])['score'], 90)
        self.assertEqual(json.loads(archived[1]), [['AI tag','signal']])
        self.assertEqual(out['transition'], {'restored':1,'cleared':0})

    def test_first_explicit_mode_normalizes_legacy_cached_ai(self):
        db.set_setting(self.conn, 'local_laya', True)
        self.conn.execute("DELETE FROM settings WHERE key='processing_mode'")
        p = self.person()
        self.rules(p)
        self.verdict(p,'llm')
        ticket = modes.begin_work(self.conn,'notes')
        out = self.mode('RLAI')
        self.assertEqual(out['generation'], ticket.generation+1)
        self.assertFalse(modes.result_current(self.conn,ticket))
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts').fetchone()[0],'rules')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM processing_ai_history').fetchone()[0],1)

    def test_normal_archives_keep_two_per_model_without_raw_prompt(self):
        p = self.person()
        self.verdict(p)
        for i in range(4):
            self.conn.execute('''UPDATE verdicts SET input_hash=?,updated_at=?,prompt=?,evidence=?
                WHERE person_id=?''', (f'hash-{i}', f'time-{i}', 'private owner note',
                                        'private evidence', p['id']))
            self.assertTrue(state.archive_verdict(self.conn, p['id']))
        rows = self.conn.execute('''SELECT input_hash,verdict FROM processing_ai_history
            WHERE person_id=? ORDER BY id''', (p['id'],)).fetchall()
        self.assertEqual([r['input_hash'] for r in rows], ['hash-2', 'hash-3'])
        self.assertTrue(all('private owner note' not in r['verdict'] and
                            'private evidence' not in r['verdict'] for r in rows))
        events = state.recent_history(self.conn, person_id=p['id'])
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]['model'], 'local:k2')
        self.assertEqual(events[0]['score'], 90)

    def test_compact_review_events_are_bounded(self):
        p = self.person()
        for i in range(10_005):
            state.record_review_event(self.conn, p['id'], 'local:k2', 'hot', 90,
                                      'private reason ' + 'x' * 300)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM processing_review_events').fetchone()[0],
                         10_000)
        newest = state.recent_history(self.conn, limit=1)[0]
        self.assertEqual(newest['id'], 10_005)
        self.assertEqual(len(newest['reason']), 200)

    def test_rlai_removes_external_but_keeps_local_and_never_resurrects_external(self):
        self.mode('RLEAI')
        local, external = self.person('local'), self.person('external')
        for p in (local,external):
            self.rules(p)
        self.verdict(local)
        self.verdict(external,'llm')
        self.mode('RLAI')
        models = dict(self.conn.execute('SELECT person_id,model FROM verdicts'))
        self.assertEqual(models, {local['id']:'local:k2',external['id']:'rules'})
        self.mode('RLEAI')
        self.assertEqual(dict(self.conn.execute('SELECT person_id,model FROM verdicts')), models)

    def test_missing_or_stale_fallback_is_unscored_and_queued(self):
        self.mode('RLAI')
        stale, absent = self.person('stale'), self.person('absent')
        self.rules(stale)
        self.conn.execute("UPDATE rule_assessments SET profile_updated_at='old'")
        self.verdict(stale)
        self.verdict(absent)
        self.mode('R')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM verdicts').fetchone()[0],0)
        self.assertEqual({r[0] for r in self.conn.execute('SELECT person_id FROM processing_rule_queue')},
                         {stale['id'],absent['id']})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tags').fetchone()[0],0)

    def test_rule_verdict_laya_prefilter_cannot_survive_r(self):
        self.mode('RLAI')
        p, missing = self.person('cached'), self.person('missing')
        self.rules(p)
        self.verdict(p,'rules',50,88)
        self.verdict(missing,'rules',50,88)
        self.mode('R')
        pres = dict(self.conn.execute('SELECT person_id,prefilter FROM verdicts'))
        self.assertEqual(pres, {p['id']:41,missing['id']:None})
        self.assertEqual(self.conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?',
                         (missing['id'],)).fetchone()[0],'')

    def test_owner_edit_precedes_bulk_but_respects_retry(self):
        for i in range(3000):
            self.person(f'bulk_{i}')
        urgent = self.person('owner_edited')['id']
        self.conn.execute("INSERT INTO marks VALUES(?,NULL,'Owner context','now')", (urgent,))
        selected = state.next_pending(self.conn)[0]
        self.assertEqual(selected['person_id'], urgent)
        # Background enqueue must never demote the owner's edit.
        state.enqueue(self.conn, urgent)
        selected = state.next_pending(self.conn)[0]
        self.assertEqual(selected['person_id'], urgent)
        state.retry(self.conn, urgent, selected['revision'], 'Resource busy', delay=60)
        self.assertNotEqual(state.next_pending(self.conn)[0]['person_id'], urgent)
        self.assertEqual(state.next_pending(self.conn, now=10**12)[0]['person_id'], urgent)

    def test_unchanged_owner_updates_do_not_requeue_completed_review(self):
        p = self.person()
        self.conn.execute("INSERT INTO marks VALUES(?,NULL,'Known note','before')", (p['id'],))
        self.conn.execute('DELETE FROM local_queue')
        self.conn.execute("UPDATE marks SET updated_at='after' WHERE person_id=?", (p['id'],))
        self.assertEqual(state.next_pending(self.conn), [])
        self.conn.execute("UPDATE marks SET note='New context' WHERE person_id=?", (p['id'],))
        self.assertEqual(state.next_pending(self.conn)[0]['person_id'], p['id'])

    def test_legacy_queue_adds_priority_without_losing_work(self):
        p = self.person()
        self.conn.execute('DROP TABLE local_queue')
        self.conn.execute('''CREATE TABLE local_queue(person_id INTEGER PRIMARY KEY,
            retry_at REAL NOT NULL DEFAULT 0,last_error TEXT,revision INTEGER NOT NULL DEFAULT 1)''')
        self.conn.execute("INSERT INTO local_queue VALUES(?,99999999999,'Busy',7)", (p['id'],))
        state.ensure(self.conn)
        row = self.conn.execute('SELECT * FROM local_queue').fetchone()
        self.assertEqual((row['priority'],row['revision'],row['retry_at']), (0,7,99999999999))

    def test_priority_migration_preserves_retry_and_is_idempotent(self):
        p = self.person()
        self.conn.execute("UPDATE local_queue SET retry_at=99999999999,revision=9,last_error='Busy'")
        self.conn.execute('DROP TRIGGER processing_marks_insert')
        self.conn.execute('''CREATE TRIGGER processing_marks_insert AFTER INSERT ON marks
            BEGIN INSERT OR IGNORE INTO local_queue(person_id) VALUES(NEW.person_id); END''')
        state.ensure(self.conn)
        state.ensure(self.conn)
        row = self.conn.execute('SELECT * FROM local_queue').fetchone()
        self.assertEqual((row['retry_at'],row['revision'],row['last_error']), (99999999999,9,'Busy'))
        self.conn.execute("INSERT INTO marks VALUES(?,NULL,'New owner note','now')", (p['id'],))
        self.assertEqual(self.conn.execute('SELECT priority FROM local_queue').fetchone()[0], 1)

    def test_queue_coalesces_updates_but_revisions_preserve_newer_work(self):
        p = self.person()
        initial = state.next_pending(self.conn)[0]
        self.conn.execute("UPDATE people SET bio=bio||' updated' WHERE id=?", (p['id'],))
        self.conn.execute("INSERT INTO marks VALUES(?,'client','note','now')", (p['id'],))
        self.conn.execute("INSERT INTO tags VALUES(?,'Known','custom','manual')", (p['id'],))
        self.assertEqual(state.queue_status(self.conn)['pending'],1)
        current = state.next_pending(self.conn)[0]
        self.assertGreater(current['revision'], initial['revision'])
        result = {'input_hash':'h','status':'accepted','verdict':{'score':80}}
        self.assertFalse(state.put_review(self.conn,p['id'],result,revision=initial['revision']))
        self.assertEqual(state.queue_status(self.conn)['pending'],1)
        self.assertTrue(state.put_review(self.conn,p['id'],result,revision=current['revision']))
        self.assertEqual(state.queue_status(self.conn)['pending'],0)

    def test_retry_is_indexed_and_new_data_clears_wait(self):
        p = self.person()
        row = state.next_pending(self.conn)[0]
        state.retry(self.conn,p['id'],row['revision'],'temporary',delay=3600)
        self.assertEqual(state.next_pending(self.conn),[])
        self.conn.execute("UPDATE people SET name='New name' WHERE id=?", (p['id'],))
        self.assertEqual(len(state.next_pending(self.conn)),1)
        plan = self.conn.execute('EXPLAIN QUERY PLAN SELECT person_id FROM local_queue WHERE retry_at<=? ORDER BY retry_at,person_id LIMIT 1', (0,)).fetchall()
        self.assertTrue(any('local_queue_ready' in r[3] for r in plan))

    def test_only_ready_note_changes_enqueue(self):
        p = self.person()
        self.conn.execute('DELETE FROM local_queue')
        self.conn.execute("INSERT INTO owner_note_reads VALUES(?,'s','running','[]',0,0)",(p['id'],))
        self.assertEqual(state.queue_status(self.conn)['pending'],0)
        self.conn.execute("UPDATE owner_note_reads SET state='ready',facts='[]'")
        self.assertEqual(state.queue_status(self.conn)['pending'],1)
        revision = state.next_pending(self.conn)[0]['revision']
        self.conn.execute('UPDATE owner_note_reads SET updated_at=1')
        self.assertEqual(state.next_pending(self.conn)[0]['revision'],revision)

    def test_bounded_seed_progress_and_restart_do_not_reset_retry(self):
        people = [self.person('p'+str(i), '' if i==2 else 'bio') for i in range(5)]
        self.conn.execute('DELETE FROM local_queue')
        self.assertEqual(state.seed_step(self.conn,2),2)
        self.assertEqual(state.queue_status(self.conn)['seed_cursor'],people[1]['id'])
        first = state.next_pending(self.conn)[0]
        state.retry(self.conn,first['person_id'],first['revision'],'wait',3600)
        state.ensure(self.conn)
        self.assertEqual(state.seed_step(self.conn,2),2)
        self.assertEqual(state.seed_step(self.conn,2),1)
        self.assertEqual(state.seed_step(self.conn,2),0)
        self.assertFalse(state.queue_status(self.conn)['seeding'])
        self.assertEqual(state.queue_status(self.conn)['pending'],4)
        self.assertNotIn(first['person_id'], [r['person_id'] for r in state.next_pending(self.conn,10)])

    def test_policy_change_requeues_existing_reviews_in_bounded_batches(self):
        first, second = self.person('first'), self.person('second')
        state.seed_step(self.conn, 10, policy='old-model')
        for p in (first, second):
            state.put_review(self.conn,p['id'],{'input_hash':'old','status':'accepted'})
        self.assertEqual(state.queue_status(self.conn)['pending'],0)
        self.assertEqual(state.seed_step(self.conn,1,policy='new-model'),1)
        self.assertEqual(state.queue_status(self.conn)['pending'],1)
        state.seed_step(self.conn,1,policy='new-model')
        state.seed_step(self.conn,1,policy='new-model')
        self.assertEqual(state.queue_status(self.conn)['pending'],2)
        for p in (first,second):
            state.put_review(self.conn,p['id'],{'input_hash':'new','status':'accepted'})
        self.assertEqual(state.seed_step(self.conn,10,policy='new-model'),0)
        self.assertEqual(state.queue_status(self.conn)['pending'],0)

    def test_clearing_bio_removes_phantom_work_and_review_but_keeps_manual_data(self):
        p = self.person()
        state.put_review(self.conn,p['id'],{'input_hash':'h','status':'accepted'})
        state.enqueue(self.conn,p['id'])
        self.conn.execute("INSERT INTO marks VALUES(?,'client','Keep private note','now')",(p['id'],))
        self.conn.execute("INSERT INTO tags VALUES(?,'Friend','relationship','manual')",(p['id'],))
        self.conn.execute("UPDATE people SET bio='   ' WHERE id=?",(p['id'],))
        self.assertEqual(state.queue_status(self.conn)['pending'],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT note FROM marks').fetchone()[0],'Keep private note')
        self.assertEqual(self.conn.execute("SELECT tag FROM tags WHERE source='manual'").fetchone()[0],'Friend')
        self.conn.execute("UPDATE people SET bio='New business bio' WHERE id=?",(p['id'],))
        self.assertEqual([r['person_id'] for r in state.next_pending(self.conn)],[p['id']])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0],0)

    def test_old_trigger_migrates_and_legacy_cleanup_is_bounded(self):
        people = [self.person('legacy'+str(i)) for i in range(4)]
        self.conn.execute('DROP TRIGGER processing_people_update')
        self.conn.execute("""CREATE TRIGGER processing_people_update AFTER UPDATE OF bio ON people
            BEGIN INSERT OR IGNORE INTO local_queue(person_id) SELECT NEW.id WHERE coalesce(NEW.bio,'')!=''; END""")
        for p in people:
            state.put_review(self.conn,p['id'],{'input_hash':'h','status':'accepted'})
            state.enqueue(self.conn,p['id'])
            self.conn.execute('UPDATE people SET bio=NULL WHERE id=?',(p['id'],))
        self.conn.execute('DELETE FROM local_queue WHERE person_id=?',(people[-1]['id'],))
        state.ensure(self.conn)
        self.assertEqual(state.cleanup_empty_step(self.conn,2),2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0],2)
        self.assertEqual(state.cleanup_empty_step(self.conn,2),2)
        self.assertEqual(state.cleanup_empty_step(self.conn,2),0)
        self.assertEqual(state.queue_status(self.conn)['pending'],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0],0)
        # The replacement trigger prevents recurrence after migration completes.
        pid = people[0]['id']
        self.conn.execute("UPDATE people SET bio='Restored bio' WHERE id=?",(pid,))
        self.assertEqual(state.queue_status(self.conn)['pending'],1)
        self.conn.execute('UPDATE people SET bio=NULL WHERE id=?',(pid,))
        self.assertEqual(state.queue_status(self.conn)['pending'],0)

    def test_deleting_person_removes_queued_and_saved_local_work(self):
        p = self.person()
        self.rules(p)
        state.put_review(self.conn,p['id'],{'input_hash':'h','status':'accepted'})
        state.enqueue(self.conn,p['id'])
        self.conn.execute('DELETE FROM people WHERE id=?',(p['id'],))
        for table in ('local_queue','local_reviews','rule_assessments','processing_rule_queue'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
