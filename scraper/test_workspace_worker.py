"""Offline lifecycle checks for the public bio worker."""
import asyncio
import sys
import subprocess
import textwrap
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workspace_worker as worker


class FakeScraper:
    instances = []

    def __init__(self, config):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.fetches = 0
        self.runs = 0
        self._pool = SimpleNamespace(egresses=[], block=lambda *a, **k: None,
                                     transient=lambda *a, **k: None,
                                     network_failure=lambda *a, **k: None)
        self._fetch = SimpleNamespace(get=self.get)
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self):
        self.fetches += 1

    async def run(self, handles, concurrency, on_item=None, collect=True):
        self.runs += 1
        await self._fetch.get()
        self.started.set()
        await self.finish.wait()
        item = {'id': '123', 'username': handles[0], 'biography': 'new',
                '_scrape': {'bioStatus': 'observed', 'source': 'test',
                            'observedAt': worker.db.now()}}
        if on_item:
            on_item(item)
        return [item] if collect else []

    def healthy_count(self):
        return 1


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.db_path = self.root / 'one.sqlite'
        self.conn = worker.db.init(self.db_path)
        worker.bridge.init(self.conn)
        worker.db.set_setting(self.conn, 'public_bios',
                              {**worker.bridge.DEFAULTS, 'on': True, 'concurrency': 1})
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','person',?)",
                          (worker.db.now(),))
        self.conn.commit()
        FakeScraper.instances = []
        self.patches = [patch.object(worker, 'ROOT', self.root),
                        patch.object(worker, 'ProfileScraper', FakeScraper),
                        patch.object(worker, 'load_yaml_egresses', return_value=[object()]),
                        patch.object(worker.bridge, 'config_path', return_value=self.root / 'data' / 'egresses.yaml')]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.conn.close()
        self.temp.cleanup()

    async def wait_for(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('worker did not reach expected point')

    def job(self):
        return tuple(self.conn.execute('SELECT state,attempts,lane FROM jobs').fetchone())

    async def test_pause_after_response_saves_complete_result(self):
        task = asyncio.create_task(worker.run(self.db_path))
        try:
            await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
            worker.db.set_setting(self.conn, 'paused_bios', True)
            self.conn.commit()
            with self.assertRaises(asyncio.CancelledError):
                await FakeScraper.instances[0]._fetch.get()
            self.assertEqual(FakeScraper.instances[0].fetches, 1)
            FakeScraper.instances[0].finish.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 1)
            self.assertEqual(self.job()[0], 'done')
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_pause_between_lease_and_start_prevents_traffic(self):
        original = worker.bridge.lease
        def pause_after_lease(conn, lane):
            job = original(conn, lane)
            worker.db.set_setting(self.conn, 'paused_bios', True)
            self.conn.commit()
            return job
        with patch.object(worker.bridge, 'lease', side_effect=pause_after_lease):
            await asyncio.wait_for(worker.run(self.db_path), 2)
        self.assertEqual(FakeScraper.instances[0].runs, 0)
        self.assertEqual(FakeScraper.instances[0].fetches, 0)
        self.assertEqual(self.job(), ('queued', 0, None))

    async def test_config_change_after_response_saves_complete_result(self):
        task = asyncio.create_task(worker.run(self.db_path))
        try:
            await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
            worker.db.set_setting(self.conn, 'public_bios',
                                  {**worker.bridge.DEFAULTS, 'on': True, 'concurrency': 2})
            self.conn.commit()
            FakeScraper.instances[0].finish.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 1)
            self.assertEqual(self.job()[0], 'done')
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_parent_death_blocks_new_fetch_and_releases_lease(self):
        parent = [100]
        with patch.object(worker.os, 'getppid', side_effect=lambda: parent[0]):
            task = asyncio.create_task(worker.run(self.db_path))
            try:
                await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
                parent[0] = 101
                with self.assertRaises(asyncio.CancelledError):
                    await FakeScraper.instances[0]._fetch.get()
                FakeScraper.instances[0].finish.set()
                await asyncio.wait_for(task, 2)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 1)
                self.assertEqual(self.job()[0], 'done')
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_task_error_releases_owned_lease(self):
        async def broken_run(self, handles, concurrency, **kwargs):
            raise RuntimeError('synthetic transport failure')
        with patch.object(FakeScraper, 'run', broken_run):
            with self.assertRaisesRegex(RuntimeError, 'synthetic transport failure'):
                await asyncio.wait_for(worker.run(self.db_path), 2)
        self.assertEqual(self.job(), ('queued', 0, None))
        self.assertEqual(worker.db.get_setting(self.conn, 'public_bios_heartbeat')['epoch'], 0)
        self.assertIn('stopped unexpectedly', worker.db.get_setting(self.conn, 'public_bios_error'))

    async def test_failed_import_rolls_back_shared_connection_before_release(self):
        def broken_import(conn, job, item):
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO public_bio_results(job_id,handle,outcome) VALUES(?,'person','success')",
                         (job['id'],))
            raise RuntimeError('synthetic failed import')
        with patch.object(worker.bridge, 'import_item', side_effect=broken_import):
            task = asyncio.create_task(worker.run(self.db_path))
            try:
                await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
                FakeScraper.instances[0].finish.set()
                with self.assertRaisesRegex(RuntimeError, 'synthetic failed import'):
                    await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 0)
        self.assertEqual(self.job(), ('queued', 0, None))

    async def test_signal_cancels_inflight_and_releases_lease(self):
        callbacks = {}
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'add_signal_handler', side_effect=lambda sig, callback: callbacks.setdefault(sig, callback)):
            task = asyncio.create_task(worker.run(self.db_path))
            try:
                await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
                callbacks[worker.signal.SIGTERM]()
                await asyncio.wait_for(task, 2)
                self.assertEqual(self.job(), ('queued', 0, None))
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_after_callback_preserves_completed_result(self):
        delivered = asyncio.Event()
        original = FakeScraper.run
        async def hold_after_delivery(scraper, *args, **kwargs):
            await original(scraper, *args, **kwargs)
            delivered.set()
            await asyncio.Event().wait()
        with patch.object(FakeScraper, 'run', hold_after_delivery):
            task = asyncio.create_task(worker.run(self.db_path))
            await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
            FakeScraper.instances[0].finish.set()
            await asyncio.wait_for(delivered.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.job()[0], 'done')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 1)

    async def test_import_failure_rolls_back_before_scraper_can_yield(self):
        observed = []
        def broken_import(conn, job, item):
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO public_bio_results(job_id,handle,outcome) VALUES(?,'person','success')",
                         (job['id'],))
            raise RuntimeError('failed import')
        original = FakeScraper.run
        async def observe_transaction(scraper, *args, **kwargs):
            try:
                return await original(scraper, *args, **kwargs)
            except RuntimeError:
                # A separate connection can acquire the write lock immediately.
                self.conn.execute('BEGIN IMMEDIATE')
                observed.append(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0])
                self.conn.rollback()
                raise
        with patch.object(worker.bridge, 'import_item', side_effect=broken_import), \
                patch.object(FakeScraper, 'run', observe_transaction):
            task = asyncio.create_task(worker.run(self.db_path))
            await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
            FakeScraper.instances[0].finish.set()
            with self.assertRaisesRegex(RuntimeError, 'failed import'):
                await asyncio.wait_for(task, 2)
        self.assertEqual(observed, [0])

    async def test_killed_process_resumes_unfinished_lease_without_repeating_completed(self):
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','second',?)",
                          (worker.db.now(),))
        self.conn.commit()
        script = textwrap.dedent(r"""
            import asyncio, sys
            from pathlib import Path
            from types import SimpleNamespace
            sys.path.insert(0, sys.argv[1])
            import workspace_worker as w
            w.ROOT = Path(sys.argv[2])
            w.load_yaml_egresses = lambda _: [object()]
            w.bridge.config_path = lambda _: w.ROOT / 'unused.yaml'
            class Fake:
                def __init__(self, config):
                    self._pool = SimpleNamespace(egresses=[], block=lambda *a, **k: None,
                        transient=lambda *a, **k: None, network_failure=lambda *a, **k: None)
                    self._fetch = SimpleNamespace(get=self.get)
                async def get(self): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def healthy_count(self): return 1
                async def run(self, handles, on_item, concurrency, collect):
                    assert collect is False
                    handle = handles[0]
                    with open(w.ROOT / 'calls.txt', 'a') as out:
                        out.write(handle + '\n')
                    if handle == 'second' and sys.argv[3] == 'first':
                        await asyncio.Event().wait()
                    on_item({'id': '123' if handle == 'person' else '456',
                             'username': handle, 'biography': 'saved',
                             '_scrape': {'bioStatus': 'observed', 'observedAt': w.db.now()}})
                    if sys.argv[3] == 'resume':
                        conn = w.db.connect(sys.argv[4])
                        w.db.set_setting(conn, 'paused_bios', True)
                        conn.commit()
                        conn.close()
                    return []
            w.ProfileScraper = Fake
            asyncio.run(w.run(sys.argv[4]))
        """)
        args = [sys.executable, '-c', script, str(Path(worker.__file__).parent),
                str(self.root)]
        first = subprocess.Popen([*args, 'first', str(self.db_path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(300):
                calls = self.root / 'calls.txt'
                if calls.exists() and calls.read_text().splitlines() == ['person', 'second']:
                    break
                if first.poll() is not None:
                    self.fail(first.communicate()[1].decode())
                await asyncio.sleep(.01)
            else:
                self.fail('mock worker did not lease the second job')
            first.kill()
            first.wait(timeout=3)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait(timeout=3)
            first.stderr.close()
        self.assertEqual([r[0] for r in self.conn.execute('SELECT state FROM jobs ORDER BY id')],
                         ['done', 'leased'])
        resumed = subprocess.Popen([*args, 'resume', str(self.db_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(300):
                if resumed.poll() is not None:
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('resumed worker did not finish')
            self.assertEqual(resumed.returncode, 0, resumed.communicate()[1].decode())
        finally:
            if resumed.poll() is None:
                resumed.kill()
                resumed.wait(timeout=3)
            resumed.stderr.close()
        self.assertEqual((self.root / 'calls.txt').read_text().splitlines(),
                         ['person', 'second', 'second'])
        self.assertEqual([r[0] for r in self.conn.execute('SELECT state FROM jobs ORDER BY id')],
                         ['done', 'done'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM public_bio_results').fetchone()[0], 2)

    async def test_lock_is_scoped_to_database_path(self):
        first = asyncio.create_task(worker.run(self.db_path))
        try:
            await self.wait_for(lambda: FakeScraper.instances and FakeScraper.instances[0].started.is_set())
            await asyncio.wait_for(worker.run(self.db_path), 2)
            self.assertEqual(len(FakeScraper.instances), 1)
            other = self.root / 'two.sqlite'
            second_conn = worker.db.init(other)
            worker.bridge.init(second_conn)
            worker.db.set_setting(second_conn, 'public_bios',
                                  {**worker.bridge.DEFAULTS, 'on': True, 'concurrency': 1})
            second_conn.commit()
            second = asyncio.create_task(worker.run(other))
            try:
                await self.wait_for(lambda: len(FakeScraper.instances) == 2)
                worker.db.set_setting(second_conn, 'paused_bios', True)
                second_conn.commit()
                await asyncio.wait_for(second, 2)
            finally:
                second_conn.close()
                if not second.done():
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
            worker.db.set_setting(self.conn, 'paused_bios', True)
            self.conn.commit()
            FakeScraper.instances[0].finish.set()
            await asyncio.wait_for(first, 2)
        finally:
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
