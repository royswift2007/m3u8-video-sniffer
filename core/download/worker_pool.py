"""Worker pool bookkeeping for :class:`DownloadManager`.

This module isolates the thread-management concerns that used to live
directly on ``DownloadManager``:

* spawning named ``DownloadWorker-N`` daemon threads,
* recording each worker's ``soft_exit`` event so
  :meth:`WorkerPool.set_max_concurrent` can drain newest-first,
* running a supervisor that enforces the 30s ``worker_exit_timeout``
  ceiling from R19.2 (WARN + drop — never force-kill),
* exposing a consistent :attr:`WorkerPool.active_workers` count.

Design lineage: ``security-stability-hardening`` spec, Requirement 19
(Stage 3 / P2-4). Task 25.1 extracts the pool out of
``core/download_manager.py`` so the manager can compose it with
``TaskQueue`` and the stateless classifier functions.

The module deliberately stays decoupled from ``DownloadManager``: the
pool only knows how to run a caller-supplied ``worker_target(soft_exit,
stop_flag)`` callable. That keeps the split behaviour-preserving — the
manager still owns the admission gate, the queue, and the task lifecycle
— and makes ``WorkerPool`` testable without a full manager fixture.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from utils.i18n import TR
from utils.logger import logger


__all__ = ["WorkerPool"]


WorkerTarget = Callable[[threading.Event], None]


class WorkerPool:
    """Manage a set of ``DownloadWorker-N`` daemon threads.

    Responsibilities:

    * Spawn workers via ``target(soft_exit_event)``; ``target`` is the
      ``DownloadManager._worker`` bound method in production.
    * Track each worker in ``(thread, soft_exit_event)`` tuples so
      :meth:`set_max_concurrent` can drain the *newest* workers first
      (preserving fairness across the original pool).
    * Run a supervisor thread that enforces R19.2: if a worker fails to
      exit within 30 s of being soft-exited, log ``worker_exit_timeout``
      and drop it from the tracking list without force-killing it (the
      thread may still be finishing an in-flight download).
    * Expose :attr:`active_workers` as the live, tracked count.

    The pool itself does **not** touch the task queue or the worker
    admission gate — those are still owned by ``DownloadManager``. The
    manager signals the pool (``supervisor_wake`` / ``stop_flag``) and
    reads :attr:`active_workers`; the pool never reaches back into the
    manager.
    """

    def __init__(
        self,
        *,
        target: WorkerTarget,
        stop_flag: threading.Event,
        max_concurrent: int,
        notify_workers: Optional[Callable[[], None]] = None,
    ) -> None:
        """Create a pool with ``max_concurrent`` daemon workers ready to run.

        Args:
            target: Callable invoked as ``target(soft_exit_event)`` in
                each worker thread. In production this is
                ``DownloadManager._worker``.
            stop_flag: Event set when the owning manager is shutting
                down. The supervisor consults it to exit its own loop
                promptly instead of sleeping on its poll timer.
            max_concurrent: Initial worker count. ``__init__`` spawns
                exactly this many workers synchronously before returning
                so :attr:`active_workers` is accurate immediately (this
                matches the pre-split ``DownloadManager`` contract).
            notify_workers: Optional callback invoked after the pool
                mutates worker state (shrink/grow). The manager hooks
                this to its ``_worker_gate`` so freshly-started workers
                notice admission and shrunk workers notice ``soft_exit``
                without waiting on the gate's poll timer.
        """

        self._target = target
        self._stop_flag = stop_flag
        self._notify_workers = notify_workers or (lambda: None)

        # Scalar current-size; the manager reads this to drive the
        # admission gate (``running_slots >= max_concurrent`` blocks).
        # Mutations happen under ``_lock`` together with the worker list
        # so concurrent ``set_max_concurrent`` calls are race-free.
        self.max_concurrent = int(max_concurrent)

        # ``_workers_meta`` holds ``(Thread, soft_exit_event)`` tuples in
        # insertion order; :meth:`set_max_concurrent` signals the
        # **newest** entries when shrinking so the original pool is
        # preserved for fairness, and the supervisor thread prunes
        # timed-out workers from the tail. ``_lock`` serialises mutations
        # of both ``_workers`` and ``_workers_meta`` plus
        # ``max_concurrent`` so concurrent adjustments are idempotent.
        self._workers: list[threading.Thread] = []
        self._workers_meta: list[tuple[threading.Thread, threading.Event]] = []
        self._lock = threading.RLock()

        # Next sequential id used when naming spawned workers. We never
        # reuse ids even after a worker exits so log lines stay greppable
        # across lifetime.
        self._worker_seq = 0

        # Supervisor state for R19.2 (``worker_exit_timeout`` enforcement).
        # ``_soft_exit_deadlines`` maps a ``Thread`` to the monotonic
        # timestamp by which it must have terminated after ``soft_exit``
        # was requested. The supervisor polls this dict and prunes both
        # the deadline map and ``_workers_meta`` when a worker exceeds 30s.
        self._soft_exit_deadlines: dict[threading.Thread, float] = {}
        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_wake = threading.Event()

        # Workers are not spawned here on purpose: the owning
        # ``DownloadManager`` passes its bound ``_worker`` method as
        # ``target``, and that method reads manager state (queue,
        # admission gate) that is only valid once the manager's
        # ``__init__`` has finished installing ``self._worker_pool``.
        # Call :meth:`start` once the manager has completed assignment.

    def start(self) -> None:
        """Spawn the initial worker pool and start the supervisor.

        Separated from ``__init__`` so the owning manager can finish
        installing its ``_worker_pool`` attribute before any worker
        thread touches manager state. Safe to call multiple times; the
        second call is a no-op if the pool is already populated.
        """

        with self._lock:
            if self._workers_meta:
                # Already started.
                self._start_supervisor_locked()
                return
            for _ in range(max(0, self.max_concurrent)):
                self._spawn_worker_locked()
            self._start_supervisor_locked()
        if self.max_concurrent > 0:
            logger.info(
                f"{TR('log_worker_started').replace('{count}', str(self.max_concurrent))}"
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def active_workers(self) -> int:
        """Return the count of live tracked workers.

        R19.3: after any ``soft_exit`` / supervisor sweep, this reflects
        the **effective** pool size — i.e. workers that have neither
        exited nor been abandoned after a ``worker_exit_timeout``. Reads
        are taken under :attr:`_lock` so concurrent adjustments can't
        hand out a torn count.
        """

        with self._lock:
            # Opportunistically reflect natural exits without waiting for
            # the supervisor's next tick. We do **not** honour soft-exit
            # deadlines here because timeouts are only supposed to be
            # declared by the supervisor path (single source of truth for
            # the ``worker_exit_timeout`` log line).
            return sum(1 for w, _e in self._workers_meta if w.is_alive())

    def workers(self) -> List[threading.Thread]:
        """Return a snapshot of currently-tracked worker threads."""

        with self._lock:
            return list(self._workers)

    # ------------------------------------------------------------------
    # Dynamic resizing (R19.1)
    # ------------------------------------------------------------------
    def set_max_concurrent(self, new_value: int) -> None:
        """Dynamically adjust the concurrent worker count.

        See ``DownloadManager.set_max_concurrent`` in the original file
        for the full behaviour contract. Summary:

        * Idempotent when ``new_value == current``.
        * Grow: spawn ``new_value - current`` workers.
        * Shrink: soft-exit the newest ``current - new_value`` workers;
          supervisor enforces the 30s ceiling.
        * Thread-safe under :attr:`_lock`.
        """

        if new_value is None:
            return
        try:
            new_value = int(new_value)
        except (TypeError, ValueError):
            return
        if new_value < 0:
            new_value = 0

        soft_exited_names: list[str] = []
        spawned = 0
        old_value = 0
        with self._lock:
            # Prune any naturally-dead workers so we count against a live
            # pool. This keeps the idempotency check below honest when
            # the supervisor hasn't run yet.
            self._workers_meta = [
                (w, ev) for (w, ev) in self._workers_meta if w.is_alive()
            ]
            live_count = len(self._workers_meta)
            old_value = self.max_concurrent
            self.max_concurrent = new_value

            if new_value == live_count:
                # Idempotent no-op (beyond syncing max_concurrent which
                # drives the worker gate admission check in the manager).
                if new_value != old_value:
                    logger.info(
                        f"{TR('log_concurrent_adjusted')}: {old_value} -> {new_value}"
                    )
                self._notify_workers()
                return

            if new_value > live_count:
                # Grow: spawn the delta. Supervisor is already running.
                for _ in range(new_value - live_count):
                    self._spawn_worker_locked()
                    spawned += 1
            else:
                # Shrink: signal the newest workers to drain. Iterate
                # from the tail so the original workers stay in the pool.
                excess = live_count - new_value
                deadline = time.monotonic() + 30.0
                drained = 0
                for worker, event in reversed(self._workers_meta):
                    if drained >= excess:
                        break
                    if event.is_set():
                        # Already asked to exit (e.g. from an earlier
                        # shrink that hasn't finished); don't double-count.
                        continue
                    event.set()
                    self._soft_exit_deadlines[worker] = deadline
                    soft_exited_names.append(worker.name)
                    drained += 1
                # Make sure the supervisor is running so the 30s ceiling
                # is actually enforced even if __init__ was short-circuited
                # (e.g. test doubles that skipped the normal constructor).
                self._start_supervisor_locked()
                self._supervisor_wake.set()

        # Wake workers so soft-exited ones notice their event promptly
        # instead of parking on the poll inside the admission gate loop.
        self._notify_workers()

        if spawned:
            logger.info(
                f"{TR('log_concurrent_adjusted')}: {old_value} -> {new_value}，"
                f"{TR('log_worker_started_new')}",
                event="worker_pool_grow",
                delta=spawned,
            )
        elif soft_exited_names:
            logger.info(
                f"{TR('log_concurrent_adjusted')}: {old_value} -> {new_value}",
                event="worker_pool_shrink",
                soft_exited=soft_exited_names,
            )
        else:
            logger.info(
                f"{TR('log_concurrent_adjusted')}: {old_value} -> {new_value}"
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def wake_supervisor(self) -> None:
        """Wake the supervisor immediately (e.g. on shutdown)."""

        self._supervisor_wake.set()

    def join_all(self, timeout: float = 3.0) -> None:
        """Join every tracked worker with ``timeout`` seconds, then reset state."""

        for worker in list(self._workers):
            worker.join(timeout=timeout)
        with self._lock:
            self._workers = []
            self._workers_meta = []
            self._soft_exit_deadlines.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _spawn_worker_locked(self) -> tuple[threading.Thread, threading.Event]:
        """Create and start a single worker thread.

        Must be called with :attr:`_lock` held. Returns the newly created
        ``(thread, soft_exit_event)`` pair so callers can use the event
        for bookkeeping if needed. The worker is always registered in
        both :attr:`_workers` (legacy list kept for backwards compatibility
        with ``shutdown()`` join) and :attr:`_workers_meta` (R19 primary
        view used by :attr:`active_workers` and
        :meth:`set_max_concurrent`).
        """

        self._worker_seq += 1
        soft_exit = threading.Event()
        worker = threading.Thread(
            target=self._target,
            args=(soft_exit,),
            name=f"DownloadWorker-{self._worker_seq}",
            daemon=True,
        )
        worker.start()
        self._workers.append(worker)
        self._workers_meta.append((worker, soft_exit))
        return worker, soft_exit

    def _start_supervisor_locked(self) -> None:
        """Lazily start the soft-exit supervisor thread.

        The supervisor polls :attr:`_soft_exit_deadlines` every second
        and enforces the 30s R19.2 ceiling by logging
        ``worker_exit_timeout`` and removing the worker from
        :attr:`_workers_meta` so :attr:`active_workers` no longer reports
        it. The worker thread is **not** force-killed; it may still hold
        a task and will exit on its own once ``_stop_flag`` is set or the
        process terminates.
        """

        if self._supervisor_thread is not None and self._supervisor_thread.is_alive():
            return
        self._supervisor_thread = threading.Thread(
            target=self._supervise_workers,
            name="DownloadWorkerSupervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

    def _supervise_workers(self) -> None:
        """Monitor soft-exit deadlines and prune timed-out workers.

        R19.2: when a worker fails to exit within 30 s of
        ``soft_exit_event`` being set, emit a ``worker_exit_timeout``
        WARN and drop it from ``_workers_meta``. Also prunes entries that
        have already exited cleanly so :attr:`active_workers` stays
        accurate without waiting for the next concurrency change.
        """

        while not self._stop_flag.is_set():
            # Wait up to 1s between passes; the event is set by
            # ``set_max_concurrent`` / ``wake_supervisor`` to wake us
            # immediately.
            self._supervisor_wake.wait(timeout=1.0)
            self._supervisor_wake.clear()
            if self._stop_flag.is_set():
                return
            try:
                self._supervisor_sweep()
            except Exception as sweep_error:  # pragma: no cover - defensive
                logger.warning(
                    f"[WORKER] supervisor sweep error: {sweep_error}",
                    event="worker_supervisor_error",
                )

    def _supervisor_sweep(self) -> None:
        """One pass of the supervisor loop (extracted for testability)."""

        now = time.monotonic()
        expired: list[tuple[threading.Thread, float]] = []
        finished: list[threading.Thread] = []
        with self._lock:
            # Collect soft-exit timeouts first, then clean up naturally
            # exited workers (including any that honoured soft_exit in time).
            for worker, deadline in list(self._soft_exit_deadlines.items()):
                if not worker.is_alive():
                    finished.append(worker)
                    continue
                if now >= deadline:
                    expired.append((worker, deadline))
            # Any worker in _workers_meta that has exited on its own (e.g.
            # because its soft_exit was honoured) should also be dropped so
            # active_workers reflects reality even when no timeout fired.
            for worker, _event in list(self._workers_meta):
                if not worker.is_alive() and worker not in finished:
                    finished.append(worker)

            for worker in finished:
                self._soft_exit_deadlines.pop(worker, None)
                self._workers_meta = [
                    (w, ev) for (w, ev) in self._workers_meta if w is not worker
                ]

            for worker, _deadline in expired:
                self._soft_exit_deadlines.pop(worker, None)
                # Remove from the tracking list; the thread keeps running
                # (it may still be finishing a download) but is no longer
                # counted against ``active_workers``. We intentionally do
                # not force-kill per R19.2 design.
                self._workers_meta = [
                    (w, ev) for (w, ev) in self._workers_meta if w is not worker
                ]

        for worker, deadline in expired:
            logger.warning(
                f"[WORKER] worker_exit_timeout: {worker.name}",
                event="worker_exit_timeout",
                worker=worker.name,
                deadline_monotonic=deadline,
            )
