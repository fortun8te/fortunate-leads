"""Offline Laya protocol and cache tests. No socket or model is used."""
import io
from contextlib import nullcontext
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db  # noqa: E402
import laya  # noqa: E402
import qualify  # noqa: E402
import server  # noqa: E402
import processing_modes


def local_conn(path):
    conn = db.init(path)
    processing_modes.set_mode(conn, 'RLAI')
    conn.commit()
    return conn


class Response:
    def __init__(self, value):
        self.stream = io.BytesIO(json.dumps(value).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, n):
        return self.stream.read(n)


def answer(pid, **changes):
    answers = {q['key']: {'p': 0.5} for q in laya.QUESTIONS}
    answers.update(changes)
    return {'id': str(pid), 'answers': answers}


def packet(results, model=None, deployment_version=None):
    return {'results': results, 'model': model or laya.MODEL,
            'deployment_version': deployment_version or laya.DEPLOYMENT_VERSION}


class ClientSafetyTest(unittest.TestCase):
    def tearDown(self):
        laya.reset()

    def test_health_requires_model_and_deployment_version(self):
        for body in ({'ok': True}, {'ok': True, 'model': 'wrong',
                                      'deployment_version': laya.DEPLOYMENT_VERSION}):
            laya.reset()
            with patch.object(laya, '_open', return_value=Response(body)):
                self.assertFalse(laya.available())
        laya.reset()
        with patch.object(laya, '_open', return_value=Response({
                'ok': True, 'model': laya.MODEL, 'deployment_version': laya.DEPLOYMENT_VERSION})):
            self.assertTrue(laya.available())
        with patch.object(laya.time, 'monotonic', return_value=laya._health['at'] + laya.HEALTH_TTL + 1):
            self.assertIsNone(laya.last_known())

    def test_decide_rejects_partial_duplicate_and_bad_probability(self):
        people = [{'id': 1, 'handle': 'one'}, {'id': 2, 'handle': 'two'}]
        bad = [packet([answer(1)]), packet([answer(1), answer(1)]),
               packet([answer(1, creator={'p': float('nan')}), answer(2)]),
               packet([answer(1, creator={'p': 0.5, 'x': 1}), {'id': '2', 'answers': {}}]),
               packet([answer(1), answer(2)], model='wrong')]
        for body in bad:
            laya.reset()
            with patch.object(laya, '_open', return_value=Response(body)):
                self.assertEqual(laya.decide(people), {})
        laya.reset()
        with patch.object(laya, '_open', return_value=Response(packet([answer(2), answer(1)]))):
            self.assertEqual(set(laya.decide(people)), {1, 2})

    def test_cache_signature_changes_with_model_questions_or_pipeline(self):
        baseline = laya.cache_signature()
        with patch.object(laya, 'MODEL', 'other'):
            self.assertNotEqual(laya.cache_signature(), baseline)
        with patch.object(laya, 'QUESTIONS', laya.QUESTIONS + ({'key': 'new', 'q': 'New?'},)):
            self.assertNotEqual(laya.cache_signature(), baseline)
        with patch.object(laya, 'PIPELINE_VERSION', 'future'):
            self.assertNotEqual(laya.cache_signature(), baseline)

    def test_invalid_later_batch_discards_entire_result(self):
        with patch.object(laya, 'BATCH', 1), patch.object(laya, '_open', side_effect=[
                Response(packet([answer(1)])), Response(packet([answer(1)]))]):
            self.assertEqual(laya.decide([{'id': 1}, {'id': 2}]), {})

    def test_duplicate_input_across_batches_never_calls_sidecar(self):
        with patch.object(laya, 'BATCH', 1), patch.object(laya, '_open') as call:
            self.assertEqual(laya.decide([{'id': 1}, {'id': '1'}]), {})
            call.assert_not_called()

    def test_probabilities_and_extra_questions_are_rejected(self):
        for value in (True, -0.1, 1.1, float('inf'), None, '0.9'):
            with self.subTest(value=value), patch.object(laya, '_open', return_value=Response(
                    packet([answer(1, creator={'p': value})]))):
                self.assertEqual(laya.decide([{'id': 1}]), {})
        with patch.object(laya, '_open', return_value=Response(packet([answer(1, extra={'p': 0.5})]))):
            self.assertEqual(laya.decide([{'id': 1}]), {})

    def test_zero_clock_health_is_cached_and_expired(self):
        with patch.object(laya, '_open', return_value=Response({'ok': True, 'model': laya.MODEL,
                'deployment_version': laya.DEPLOYMENT_VERSION})) as call:
            self.assertTrue(laya.available(now=0))
            self.assertTrue(laya.available(now=1))
            self.assertEqual(call.call_count, 1)
            with patch.object(laya.time, 'monotonic', return_value=60):
                self.assertIsNone(laya.last_known())

    def test_fit_policy_invalidates_cache_and_no_tag_confidence(self):
        baseline = laya.cache_signature()
        with patch.dict(laya.FIT_W, creator=-50):
            self.assertNotEqual(laya.cache_signature(), baseline)
        self.assertEqual(laya.tags({q['key']: 1 for q in laya.QUESTIONS}, set()), [])


class StoredAnswerTest(unittest.TestCase):
    def test_stale_profile_and_model_answers_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                pid = db.upsert_person(conn, {'handle': 'sample', 'bio': 'A brand'})
                row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
                fields = (row[k] for k in ('handle', 'name', 'bio', 'category', 'website', 'followers'))
                answers = {q['key']: 0.5 for q in laya.QUESTIONS}
                conn.execute('INSERT INTO laya VALUES(?,?,?,?,?)',
                             (pid, server.laya_hash(*fields), json.dumps(answers), 80, db.now()))
                self.assertEqual(server.laya_row(conn, pid), (answers, 50))
                conn.execute("UPDATE people SET bio='A different brand' WHERE id=?", (pid,))
                self.assertEqual(server.laya_row(conn, pid), (None, None))
                conn.execute("UPDATE people SET bio='A brand' WHERE id=?", (pid,))
                with patch.object(laya, 'DEPLOYMENT_VERSION', 'new-checkpoint'):
                    self.assertEqual(server.laya_row(conn, pid), (None, None))
            finally:
                conn.close()


class LayaQueueTest(unittest.TestCase):
    def setUp(self):
        lease = patch.object(server.resource_budget, 'lease', side_effect=lambda *_a, **_k: nullcontext())
        lease.start()
        self.addCleanup(lease.stop)

    def test_rebuild_commits_small_ranges_and_resumes_without_early_scoring(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'leads.sqlite')
            conn = local_conn(path)
            try:
                ids = [db.upsert_person(conn, {'handle': f'person{i}', 'bio': 'bio'}) for i in range(5)]
                conn.execute("INSERT INTO seeds(handle,is_me) VALUES('person1',1)")
                row = conn.execute('SELECT handle,name,bio,category,website,followers FROM people WHERE id=?',
                                   (ids[2],)).fetchone()
                conn.execute('INSERT INTO laya VALUES(?,?,?,?,?)',
                             (ids[2], server.laya_hash(*row), '{}', 50, db.now()))
                conn.commit()
                with patch.object(server.control, 'stage_paused', return_value=False), \
                        patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide', return_value={}) as decide, \
                        patch('backend.qualification.LAYA_REBUILD_BATCH', 2):
                    self.assertTrue(server.laya_step(conn))
                    self.assertFalse(conn.in_transaction)
                    self.assertIsNone(db.get_setting(conn, 'laya_queue_signature'))
                    decide.assert_not_called()
                    # A profile already swept remains current through its trigger.
                    conn.execute("UPDATE people SET bio=NULL WHERE id=?", (ids[0],))
                    conn.commit()
                    conn.close()
                    conn = db.connect(path)
                    self.assertTrue(server.laya_step(conn))
                    decide.assert_not_called()
                    self.assertFalse(server.laya_step(conn))
                    self.assertEqual(db.get_setting(conn, 'laya_queue_signature'), laya.cache_signature())
                    self.assertEqual([r[0] for r in conn.execute('SELECT person_id FROM laya_queue ORDER BY person_id')],
                                     [ids[0], ids[3], ids[4]])
                    self.assertEqual(conn.execute('SELECT bio_blank FROM laya_queue WHERE person_id=?',
                                                  (ids[0],)).fetchone()[0], 1)
            finally:
                conn.close()

    def test_caller_transaction_is_not_committed_by_failed_or_successful_step(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'leads.sqlite')
            conn = local_conn(path)
            reader = db.connect(path)
            try:
                pid = db.upsert_person(conn, {'handle': 'pending', 'bio': 'A brand'})
                self.assertTrue(conn.in_transaction)
                with patch.object(server.control, 'stage_paused', return_value=False), \
                        patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide', return_value={}):
                    self.assertFalse(server.laya_step(conn))
                self.assertTrue(conn.in_transaction)
                self.assertIsNone(db.get_setting(conn, 'laya_queue_signature'))
                self.assertEqual(reader.execute('SELECT count(*) FROM people').fetchone()[0], 0)

                answers = {pid: {q['key']: 0.5 for q in laya.QUESTIONS}}
                with patch.object(server.control, 'stage_paused', return_value=False), \
                        patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide', return_value=answers):
                    self.assertTrue(server.laya_step(conn))
                self.assertTrue(conn.in_transaction)
                self.assertEqual(reader.execute('SELECT count(*) FROM laya').fetchone()[0], 0)
                conn.rollback()
                self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT count(*) FROM laya').fetchone()[0], 0)
            finally:
                reader.close()
                conn.close()

    def test_order_changes_rebuild_and_profile_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                ids = [db.upsert_person(conn, {'handle': f'person{i}', 'bio': bio})
                       for i, bio in enumerate(('bio', 'bio', 'bio', None))]
                conn.executemany('INSERT INTO verdicts(person_id,prefilter) VALUES(?,?)',
                                 [(ids[0], 40), (ids[1], 80), (ids[2], 60), (ids[3], 100)])
                conn.commit()
                batches = []
                def decide(people):
                    batches.append([p['id'] for p in people])
                    return {p['id']: {q['key']: 0.5 for q in laya.QUESTIONS} for p in people}
                with patch.object(server.control, 'stage_paused', return_value=False), \
                        patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide', side_effect=decide), \
                        patch('backend.qualification.LAYA_BATCH', 2):
                    self.assertTrue(server.laya_step(conn))
                    self.assertEqual(batches.pop(), [ids[1], ids[2]])
                    self.assertTrue(server.laya_step(conn))
                    self.assertEqual(batches.pop(), [ids[0], ids[3]])
                    self.assertFalse(server.laya_step(conn))
                    # Rank and profile changes put an already scored person back
                    # into exactly the same ordered queue.
                    conn.execute("UPDATE people SET bio='new bio' WHERE id=?", (ids[0],))
                    conn.execute('UPDATE verdicts SET prefilter=95 WHERE person_id=?', (ids[0],))
                    conn.commit()
                    self.assertTrue(server.laya_step(conn))
                    self.assertEqual(batches.pop(), [ids[0]])
                    # A model deployment change invalidates every cached hash.
                    with patch.object(laya, 'DEPLOYMENT_VERSION', 'next-checkpoint'):
                        self.assertTrue(server.laya_step(conn))
                        self.assertEqual(batches.pop(), [ids[0], ids[1]])
            finally:
                conn.close()

    def test_excluded_and_reverted_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                a = db.upsert_person(conn, {'handle': 'person', 'bio': 'original'})
                db.upsert_person(conn, {'handle': 'parked~old', 'bio': 'original'})
                conn.execute("INSERT INTO seeds(handle,is_me) VALUES('self',1)")
                db.upsert_person(conn, {'handle': 'self', 'bio': 'original'})
                conn.commit()
                seen = []
                def decide(people):
                    seen.extend(p['id'] for p in people)
                    return {p['id']: {q['key']: 0.5 for q in laya.QUESTIONS} for p in people}
                with patch.object(server.control, 'stage_paused', return_value=False), \
                        patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide', side_effect=decide):
                    self.assertTrue(server.laya_step(conn))
                    self.assertEqual(seen, [a])
                    conn.execute("UPDATE people SET bio='temporary' WHERE id=?", (a,))
                    conn.execute("UPDATE people SET bio='original' WHERE id=?", (a,))
                    conn.commit()
                    self.assertFalse(server.laya_step(conn))
                    self.assertEqual(seen, [a])
                    self.assertEqual(conn.execute('SELECT count(*) FROM laya_queue').fetchone()[0], 0)
            finally:
                conn.close()


    def test_malformed_cached_probability_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                pid = db.upsert_person(conn, {'handle': 'sample', 'bio': 'A brand'})
                row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
                fields = [row[k] for k in ('handle', 'name', 'bio', 'category', 'website', 'followers')]
                answers = {q['key']: 0.5 for q in laya.QUESTIONS}
                answers['creator'] = True
                conn.execute('INSERT INTO laya VALUES(?,?,?,?,?)',
                    (pid, server.laya_hash(*fields), json.dumps(answers), 100, db.now()))
                self.assertEqual(server.laya_row(conn, pid), (None, None))
            finally:
                conn.close()

    def test_profile_change_during_decide_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                pid = db.upsert_person(conn, {'handle': 'sample', 'bio': 'A brand'})
                conn.commit()
                def decide(_):
                    conn.execute("UPDATE people SET bio='changed' WHERE id=?", (pid,))
                    return {pid: {q['key']: 0.5 for q in laya.QUESTIONS}}
                with patch.object(server.control, 'stage_paused', return_value=False), \
                        patch.object(laya, 'available', return_value=True), patch.object(laya, 'decide', side_effect=decide):
                    self.assertFalse(server.laya_step(conn))
                self.assertEqual(conn.execute('SELECT count(*) FROM laya').fetchone()[0], 0)
            finally:
                conn.close()

    def test_pause_during_decide_prevents_write(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                pid = db.upsert_person(conn, {'handle': 'sample', 'bio': 'A brand'})
                conn.commit()
                answers = {pid: {q['key']: 0.5 for q in laya.QUESTIONS}}
                def pause_during_decide(_):
                    processing_modes.set_paused(conn, True)
                    conn.commit()
                    return answers
                with patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide', side_effect=pause_during_decide) as decide:
                    self.assertFalse(server.laya_step(conn))
                    decide.assert_called_once()
                self.assertEqual(conn.execute('SELECT count(*) FROM laya').fetchone()[0], 0)
            finally:
                conn.close()

    def test_pause_during_rebuild_prevents_sidecar_call(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                db.upsert_person(conn, {'handle': 'sample', 'bio': 'A brand'})
                conn.commit()
                original = server.get_application().qualification.rebuild_laya_queue
                def pause_after_rebuild(dbconn, signature):
                    completed = original(dbconn, signature)
                    processing_modes.set_paused(dbconn, True)
                    dbconn.commit()
                    return completed
                with patch.object(server.get_application().qualification, 'rebuild_laya_queue', side_effect=pause_after_rebuild), \
                        patch.object(laya, 'available', return_value=True), \
                        patch.object(laya, 'decide') as decide:
                    self.assertFalse(server.laya_step(conn))
                    decide.assert_not_called()
            finally:
                conn.close()


class LayaScoreCapTest(unittest.TestCase):
    def test_laya_respects_private_and_unreachable_caps(self):
        base = {'handle': 'glowfounder', 'name': 'Glow Founder', 'followers': 1000}
        private = dict(base, is_private=True)
        unreachable = dict(base, followers=300_000)
        other_market = dict(base, bio='Based in Mumbai')
        self.assertLessEqual(qualify.prefilter(private, ['seed'], laya_fit=100), 35)
        self.assertLessEqual(qualify.prefilter(unreachable, ['seed'], laya_fit=100), 15)
        self.assertLessEqual(qualify.prefilter(other_market, ['seed'], laya_fit=100), 15)

    def test_laya_still_blends_uncapped_profiles(self):
        person = {'handle': 'glowfounder', 'name': 'Glow Founder', 'followers': 1000}
        plain = qualify.prefilter(person, ['seed'])
        self.assertEqual(qualify.prefilter(person, ['seed'], laya_fit=100),
                         round(0.75 * plain + 25))


class PrefilterRefreshTest(unittest.TestCase):
    def test_policy_change_refreshes_only_laya_rows_once(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = local_conn(os.path.join(directory, 'leads.sqlite'))
            try:
                scored = db.upsert_person(conn, {'handle': 'largefounder', 'followers': 300_000})
                plain = db.upsert_person(conn, {'handle': 'plainfounder'})
                stamp = db.now()
                conn.executemany('INSERT INTO verdicts(person_id,prefilter,updated_at) VALUES(?,?,?)',
                                 [(scored, 90, stamp), (plain, 40, stamp)])
                row = conn.execute('SELECT handle,name,bio,category,website,followers FROM people WHERE id=?',
                                   (scored,)).fetchone()
                answers = {q['key']: (0 if q['key'] in ('creator', 'service_provider') else 1)
                           for q in laya.QUESTIONS}
                conn.execute('INSERT INTO laya VALUES(?,?,?,?,?)',
                             (scored, server.laya_hash(*row), json.dumps(answers), 100, stamp))
                self.assertEqual(server.refresh_laya_prefilter_if_changed(conn), 1)
                self.assertEqual(conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?',
                                              (scored,)).fetchone()[0], '')
                self.assertEqual(conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?',
                                              (plain,)).fetchone()[0], stamp)
                self.assertEqual(server.qualify_batch(conn), 1)
                self.assertLessEqual(conn.execute('SELECT prefilter FROM verdicts WHERE person_id=?',
                                                  (scored,)).fetchone()[0], 15)
                self.assertEqual(server.refresh_laya_prefilter_if_changed(conn), 0)
                self.assertEqual(conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?',
                                              (plain,)).fetchone()[0], stamp)
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
