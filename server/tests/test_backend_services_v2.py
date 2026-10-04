"""Isolated service resource ownership and bounded worker regressions."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import processing_modes
from backend.common import AppConfig
from backend.photos import PhotoService
from backend.qualification import LLMPool, QualificationService
import backend.photos as photo_module
import backend.qualification as qualification_module


class BackendServiceResources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = AppConfig(str(Path(self.tmp.name) / 'leads.sqlite'), port=0)
        self.conn = db.init(self.config.db)
        self.addCleanup(self.conn.close)

    def test_construction_and_idle_status_do_not_create_external_resources(self):
        with patch.object(qualification_module, 'DaemonExecutor', side_effect=AssertionError('pool created')), \
                patch.object(photo_module.ssl, 'create_default_context', side_effect=AssertionError('SSL created')):
            service = QualificationService(self.config)
            photos = PhotoService(self.config)
            self.assertFalse(service.external_active())
            self.assertIsNone(service._research_pool)
            self.assertIsNone(service._llm_pool)
            self.assertIsNone(photos._pic_opener)
            service.shutdown()
        with self.assertRaises(RuntimeError):
            _ = service.llm_pool
        with self.assertRaises(RuntimeError):
            _ = service.research_pool

    def test_photo_cursor_and_paths_belong_to_each_workspace(self):
        other = AppConfig(str(Path(self.tmp.name) / 'other' / 'leads.sqlite'), port=0)
        first, second = PhotoService(self.config), PhotoService(other)
        pid = db.upsert_person(self.conn, {'handle': 'alice'})
        self.conn.commit()
        first.repair_pfp_cache(self.conn)
        self.assertEqual(first.check_id, pid)
        self.assertEqual(second.check_id, 0)
        self.assertNotEqual(first.pfp_dir(), second.pfp_dir())
        self.assertIsNot(first.pic_opener, second.pic_opener)

    def test_model_pool_limits_dispatch_uses_own_database_and_joins(self):
        processing_modes.set_mode(self.conn, 'RLEAI')
        db.set_setting(self.conn, 'llm_workers', 3)
        self.conn.commit()
        release = threading.Event()
        all_started = threading.Event()
        calls = []
        lock = threading.Lock()
        config = self.config

        class FakeService:
            def __init__(self):
                self.config = config

            def llm_candidates(self, conn, limit, exclude):
                return [{'id': pid} for pid in range(6) if pid not in exclude][:limit]

            def run_llm(self, conn, rows, skip):
                path = conn.execute('PRAGMA database_list').fetchone()[2]
                with lock:
                    calls.append((rows[0]['id'], path))
                    if len(calls) == 3:
                        all_started.set()
                release.wait(5)

        pool = LLMPool(FakeService())
        try:
            self.assertTrue(pool.step(self.conn))
            self.assertTrue(all_started.wait(3))
            self.assertFalse(pool.step(self.conn))
            self.assertEqual(pool.running, 3)
        finally:
            release.set()
            pool.shutdown()
        self.assertTrue(pool.idle())
        self.assertEqual(len(calls), 3)
        self.assertEqual({Path(path).resolve() for _, path in calls}, {Path(self.config.db).resolve()})
        self.assertFalse(pool.step(self.conn))

    def test_model_connection_failure_releases_reserved_capacity(self):
        processing_modes.set_mode(self.conn, 'RLEAI')
        self.conn.commit()
        service = QualificationService(self.config)
        pool = service.llm_pool
        with patch.object(service, 'llm_candidates', return_value=[{'id': 1}]), \
                patch.object(db, 'connect', side_effect=sqlite3.OperationalError('test failure')), \
                patch.object(qualification_module.traceback, 'print_exc'):
            self.assertTrue(pool.step(self.conn))
            pool.shutdown()
        self.assertTrue(pool.idle())
        self.assertEqual(pool.inflight, set())
        service.shutdown()

    def test_research_pool_is_owned_ordered_and_shutdown(self):
        first, second = QualificationService(self.config), QualificationService(self.config)
        try:
            first.safe_research_lookup = lambda person: {'id': person['id']}
            people = [{'id': pid} for pid in range(20)]
            self.assertEqual(list(first._research_map(people)), people)
            self.assertIsNone(second._research_pool)
            pool = first.research_pool
        finally:
            first.shutdown()
            second.shutdown()
        self.assertTrue(pool._shutdown)

    def test_scout_constructor_is_lazy_and_closed_pool_admits_nothing(self):
        import deepscout
        with patch('backend.executor.DaemonExecutor', side_effect=AssertionError('pool created')):
            pool = deepscout.ScoutPool(self.config.db)
            self.assertIsNone(pool._executor)
            self.assertTrue(pool.idle())
            pool.shutdown()
            with patch.object(deepscout, 'available', side_effect=AssertionError('admission continued')):
                self.assertFalse(pool.step(self.conn))

    def test_scout_caps_submissions_and_cancels_queued_jobs_on_shutdown(self):
        import deepscout
        from backend.executor import DaemonExecutor
        processing_modes.set_mode(self.conn, 'RLEAI')
        db.set_setting(self.conn, 'scout', True)
        db.set_setting(self.conn, 'scout_workers', 100)
        self.conn.commit()
        release, started = threading.Event(), threading.Event()
        calls = []
        pool = deepscout.ScoutPool(self.config.db)

        def work(person):
            calls.append(person['id'])
            started.set()
            release.wait(5)

        with patch.object(deepscout, 'available', return_value=True), \
                patch.object(deepscout, 'candidates', side_effect=lambda conn, limit, exclude:
                    [{'id': pid} for pid in range(20) if pid not in exclude][:limit]), \
                patch.object(pool, '_work', side_effect=work), \
                patch('backend.executor.DaemonExecutor', side_effect=lambda **kwargs:
                    DaemonExecutor(max_workers=1, max_queue=8)):
            try:
                self.assertTrue(pool.step(self.conn))
                self.assertTrue(started.wait(3))
                self.assertEqual(len(pool.inflight), 8)
                self.assertFalse(pool.step(self.conn))
                pool.shutdown(wait=False)
                self.assertFalse(pool.idle())
                self.assertEqual(len(pool.inflight), 1)
                self.assertFalse(pool.step(self.conn))
            finally:
                release.set()
                pool.shutdown(wait=True)
        self.assertTrue(pool.idle())
        self.assertEqual(len(calls), 1)

    def test_scout_shutdown_during_model_call_skips_verification_and_result_commit(self):
        import deepscout
        processing_modes.set_mode(self.conn, 'RLEAI')
        db.set_setting(self.conn, 'scout', True)
        pid = db.upsert_person(self.conn, {'handle': 'brand', 'bio': 'Founder of a skincare brand'})
        self.conn.execute('INSERT INTO verdicts(person_id,score,content_fit,model) VALUES(?,80,80,?)',
                          (pid, 'reviewed'))
        self.conn.commit()
        release, started = threading.Event(), threading.Event()
        pool = deepscout.ScoutPool(self.config.db)

        def model(person, model):
            started.set()
            release.wait(5)
            return {'verdict': 'strong'}

        with patch.object(deepscout, 'available', return_value=True), \
                patch.object(deepscout, 'run', side_effect=model), \
                patch.object(deepscout, 'verify', side_effect=AssertionError('follow-on verification')) as verify:
            try:
                self.assertTrue(pool.step(self.conn))
                self.assertTrue(started.wait(3))
                pool.shutdown(wait=False)
                self.assertFalse(pool.idle())
            finally:
                release.set()
                pool.shutdown(wait=True)
            verify.assert_not_called()
        self.assertTrue(pool.idle())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM deep_research_runs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'reviewed')


    def _profile_for_local_review(self):
        import owner_notes
        import processing_state
        owner_notes.ensure(self.conn)
        processing_state.ensure(self.conn)
        processing_modes.set_mode(self.conn, 'RLAI')
        pid = db.upsert_person(self.conn, {'handle': 'testbrand', 'bio': 'Founder of a skincare brand. Shop our products.'})
        self.conn.commit()
        return pid

    def test_local_answer_returning_after_shutdown_is_discarded(self):
        import local_model
        pid = self._profile_for_local_review()
        service = QualificationService(self.config)
        mode = processing_modes.snapshot(self.conn)
        def complete(*args, **kwargs):
            service.shutdown(wait=False)
            return {'handle': 'testbrand', 'role': 'buyer', 'fit': 84,
                    'evidence': ['Founder of a skincare brand'], 'research_needed': None}
        with patch.object(local_model, 'complete_json', side_effect=complete):
            self.assertFalse(service.local_processing_step(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM local_queue WHERE person_id=?', (pid,)).fetchone())
        self.assertEqual(processing_modes.snapshot(self.conn), mode)

    def test_close_after_local_result_write_rolls_back_accepted_result(self):
        import local_model
        import processing_state
        self._profile_for_local_review()
        service = QualificationService(self.config)
        original = processing_state.put_review
        def put_review(*args, **kwargs):
            wrote = original(*args, **kwargs)
            service.shutdown(wait=False)
            return wrote
        output = {'handle': 'testbrand', 'role': 'buyer', 'fit': 84,
                  'evidence': ['Founder of a skincare brand'], 'research_needed': None}
        with patch.object(local_model, 'complete_json', return_value=output), \
                patch.object(processing_state, 'put_review', side_effect=put_review):
            self.assertFalse(service.local_processing_step(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_notes_returning_after_shutdown_do_not_save_or_dispatch_another_model(self):
        import owner_notes
        import local_qualification
        pid = self._profile_for_local_review()
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (pid, None, 'He is my friend.', db.now()))
        self.conn.commit()
        service = QualificationService(self.config)
        def interpret(*args, **kwargs):
            service.shutdown(wait=False)
            return [{'kind': 'friend', 'quote': 'He is my friend.'}]
        with patch.object(owner_notes, 'interpret', side_effect=interpret), \
                patch.object(local_qualification, 'evaluate', side_effect=AssertionError('new inference after close')):
            self.assertFalse(service.local_processing_step(self.conn))
        self.assertEqual(self.conn.execute('SELECT state,facts FROM owner_note_reads').fetchone()[:], ('pending', '[]'))
        self.assertEqual(self.conn.execute('SELECT note FROM marks').fetchone()[0], 'He is my friend.')

    def test_laya_answer_returning_after_shutdown_is_discarded(self):
        from contextlib import nullcontext
        import laya
        import resource_budget
        self._profile_for_local_review()
        service = QualificationService(self.config)
        def decide(rows):
            service.shutdown(wait=False)
            return {row['id']: {question['key']: 0.5 for question in laya.QUESTIONS} for row in rows}
        with patch.object(laya, 'available', return_value=True), \
                patch.object(laya, 'runtime_status', return_value={'busy': False}), \
                patch.object(laya, 'decide', side_effect=decide), \
                patch.object(resource_budget, 'lease', side_effect=lambda stage: nullcontext()):
            self.assertFalse(service.laya_step(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM laya').fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_external_answer_after_close_is_discarded_and_attempt_stays_terminal(self):
        import external_harness
        import external_queue
        import local_model
        import local_qualification
        import owner_notes
        from backend.evidence import with_owner
        pid = self._profile_for_local_review()
        processing_modes.set_mode(self.conn, 'RLEAI')
        service = QualificationService(self.config)
        service.qualify_batch(self.conn)
        person = with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()))
        context = owner_notes.local_context(self.conn, pid)
        fingerprint = local_qualification.input_hash(person, context)
        self.conn.execute('''INSERT OR REPLACE INTO local_reviews
            (person_id,input_hash,prompt,model,model_version,status,escalation_reason,updated_at,private_context_hash)
            VALUES(?,?,?,?,?,'needs_research','business_unclear',?,?)''',
            (pid, fingerprint, local_qualification.PROMPT_VERSION, local_model.MODEL,
             local_model.MODEL_DIGEST, db.now(), (context or {}).get('snapshot')))
        self.conn.execute('UPDATE verdicts SET model=? WHERE person_id=?', ('local:' + local_model.MODEL, pid))
        self.conn.execute('DELETE FROM local_queue WHERE person_id=?', (pid,))
        self.conn.commit()
        prior = self.conn.execute('SELECT score,model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[:]
        mode = processing_modes.snapshot(self.conn)
        def broad(*args, **kwargs):
            service.shutdown(wait=False)
            return {'score': 99, 'model': 'provider-returned-after-close'}
        rows = self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchall()
        with patch.object(external_harness, 'broad', side_effect=broad) as call:
            self.assertEqual(service.run_llm(self.conn, rows, {}), 0)
            call.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT score,model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[:], prior)
        self.assertEqual(self.conn.execute('SELECT state FROM external_attempts WHERE person_id=?', (pid,)).fetchone()[0], 'unverified')
        self.assertFalse(external_queue.eligible(self.conn, pid, fingerprint, now=10 ** 20))
        self.assertEqual(processing_modes.snapshot(self.conn), mode)


    def test_daemon_executor_has_bounded_admission_and_cancelled_futures_notify_waiters(self):
        from concurrent.futures import as_completed, wait
        from backend.executor import DaemonExecutor
        release, started = threading.Event(), threading.Event()
        executor = DaemonExecutor(max_workers=1, max_queue=1)
        self.assertEqual(executor._threads, [])
        def blocked():
            started.set()
            release.wait(5)
        try:
            active = executor.submit(blocked)
            self.assertTrue(started.wait(3))
            queued = executor.submit(lambda: 'queued')
            with self.assertRaises(RuntimeError):
                executor.submit(lambda: 'overflow')
            self.assertTrue(queued.cancel())
            self.assertEqual(wait([queued], timeout=1).done, {queued})
            self.assertEqual(list(as_completed([queued], timeout=1)), [queued])
            replacement = executor.submit(lambda: 'replacement')
            executor.shutdown(wait=False, cancel_futures=True)
            self.assertTrue(replacement.cancelled())
            self.assertEqual(wait([replacement], timeout=1).done, {replacement})
            self.assertFalse(active.done())
            self.assertTrue(all(thread.daemon for thread in executor._threads))
        finally:
            release.set()
            executor.shutdown(wait=True)
        self.assertTrue(active.done())

    def test_daemon_executor_preserves_errors_and_reentrant_worker_shutdown(self):
        from backend.executor import DaemonExecutor
        executor = DaemonExecutor(max_workers=1)
        try:
            def error():
                raise ValueError('expected task error')
            with self.assertRaisesRegex(ValueError, 'expected task error'):
                executor.submit(error).result(timeout=2)
            callback_done = threading.Event()
            release = threading.Event()
            future = executor.submit(lambda: release.wait(2) and 'answer')
            def close(completed):
                executor.shutdown(wait=True)
                callback_done.set()
            future.add_done_callback(close)
            release.set()
            self.assertEqual(future.result(timeout=2), 'answer')
            self.assertTrue(callback_done.wait(2))
        finally:
            executor.shutdown(wait=True)

    def test_process_exit_does_not_wait_for_hung_daemon_task(self):
        import subprocess
        source = '''from backend.executor import DaemonExecutor
import threading
started, forever = threading.Event(), threading.Event()
def hang():
    started.set()
    forever.wait()
pool = DaemonExecutor(1)
future = pool.submit(hang)
assert started.wait(1)
pool.shutdown(wait=False)
assert not future.done()
print("process can exit", flush=True)
'''
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, timeout=3,
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('process can exit', result.stdout)


if __name__ == '__main__':
    unittest.main()
