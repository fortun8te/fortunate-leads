"""Bounded task execution without interpreter-exit joins.

Daemon workers let the process exit even when an external call never returns.
They cannot interrupt a running call: its Future finishes only when it does.
"""

from collections import deque
from concurrent.futures import Executor, Future
import logging
import threading


class DaemonExecutor(Executor):
    """A lazy, bounded executor with the standard Future interface.

    At most ``max_workers + max_queue`` unfinished tasks are admitted. Worker
    slots are included in this limit even before a worker takes its first task.
    ``max_queue`` defaults to ``max_workers``; zero permits no extra backlog.
    A full executor raises RuntimeError immediately, including from a worker,
    rather than blocking submit and risking a reentrant deadlock.

    External shutdown(wait=True) joins the workers. Shutdown from a worker or
    recursively from a cancellation callback only signals shutdown, avoiding
    self-joins and mutual joins between workers' completion callbacks.
    """

    def __init__(self, max_workers, thread_name_prefix="", *, max_queue=None):
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if max_queue is None:
            max_queue = max_workers
        if not isinstance(max_queue, int) or isinstance(max_queue, bool) or max_queue < 0:
            raise ValueError("max_queue must be a nonnegative integer")
        self._max_workers = max_workers
        self._capacity = max_workers + max_queue
        self._name = thread_name_prefix or "daemon-executor"
        self._condition = threading.Condition()
        self._queue = deque()
        self._futures = set()
        self._withdrawn = set()
        self._threads = []
        self._shutdown = False
        self._shutdown_local = threading.local()

    def submit(self, fn, /, *args, **kwargs):
        if not callable(fn):
            raise TypeError("fn must be callable")
        with self._condition:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if len(self._futures) >= self._capacity:
                raise RuntimeError("executor task capacity reached")
            if not self._threads:
                self._start_workers()
            future = Future()
            # Register before publishing the Future. This callback releases
            # admission before user callbacks run and can safely resubmit.
            future.add_done_callback(self._forget)
            self._futures.add(future)
            self._queue.append((future, fn, args, kwargs))
            self._condition.notify()
            return future

    def _start_workers(self):
        # Called under the condition lock; started workers wait for publication.
        try:
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"{self._name}_{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
        except BaseException:
            self._shutdown = True
            self._condition.notify_all()
            raise

    def _forget(self, future):
        notify_cancel = False
        with self._condition:
            self._futures.discard(future)
            if future.cancelled():
                # Cancellation frees both admission and references to queued
                # call arguments without waiting for another worker to wake.
                previous_size = len(self._queue)
                self._queue = deque(task for task in self._queue if task[0] is not future)
                notify_cancel = len(self._queue) != previous_size or future in self._withdrawn
                self._withdrawn.discard(future)
            self._condition.notify_all()
        if notify_cancel:
            # wait()/as_completed() require executor acknowledgment of a
            # canceled task. Only the party that removed it from the queue
            # does this; a worker that already took it acknowledges itself.
            future.set_running_or_notify_cancel()

    def _worker(self):
        while True:
            with self._condition:
                while not self._queue:
                    if self._shutdown:
                        return
                    self._condition.wait()
                task = self._queue.popleft()
            future, fn, args, kwargs = task
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = fn(*args, **kwargs)
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
            except BaseException:
                # Future already holds the result/error. A user's completion
                # callback can raise SystemExit; do not lose a worker slot.
                logging.getLogger(__name__).exception("Executor completion callback failed")
            finally:
                # Do not retain a completed call while this worker is idle.
                task = future = fn = args = kwargs = result = None

    def shutdown(self, wait=True, *, cancel_futures=False):
        nested = getattr(self._shutdown_local, "active", False)
        self._shutdown_local.active = True
        try:
            with self._condition:
                self._shutdown = True
                queued = list(self._queue) if cancel_futures else []
                if cancel_futures:
                    self._withdrawn.update(task[0] for task in queued)
                    self._queue.clear()
                threads = tuple(self._threads)
                self._condition.notify_all()
            # Future callbacks execute arbitrary user code, including shutdown
            # or submit. Never invoke them while holding the executor lock.
            for future, _fn, _args, _kwargs in queued:
                try:
                    future.cancel()
                except BaseException:
                    logging.getLogger(__name__).exception("Executor cancellation callback failed")
            if wait and not nested and threading.current_thread() not in threads:
                for thread in threads:
                    thread.join()
        finally:
            self._shutdown_local.active = nested
