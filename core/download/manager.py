"""Download manager for task queue management and execution.

Task 25.1 of the ``security-stability-hardening`` spec splits the
original 1600-line ``core/download_manager.py`` across a cohesive
``core.download`` package. This module keeps the public
:class:`DownloadManager` class — the orchestrator the UI talks to — and
composes the extracted helpers:

* :mod:`core.download.task_queue` owns the FIFO queue (R11.1).
* :mod:`core.download.worker_pool` owns the worker-pool bookkeeping and
  supervisor thread (R19.x).
* :mod:`core.download.classifier` owns the pure classification helpers
  (R18.x).

Behaviour is strictly equivalent to the pre-split implementation — the
Stage 3 regression smoke scripts (``smoke_worker_convergence.py``,
``smoke_stop_response_benchmark.py``, ``smoke_backoff_retry.py``) and
the stage_gate unit tests are expected to pass unchanged.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from queue import Empty, Queue
import threading
import time
from datetime import datetime
from typing import Callable, List, Literal, Optional
from urllib.parse import urlparse

from core.site_rule_utils import set_header_if_missing, site_rule_matches
from core.task_model import DownloadTask, TaskSnapshot
from core.engine_selector import EngineSelector
from core.download.classifier import (
    STOP_REASON_CLASSIFICATION,
    classify_failure,
    classify_message_keywords,
    detect_failure_stage,
)
from core.download.worker_pool import WorkerPool
from engines.base_engine import BaseEngine
from utils.logger import logger
from utils.i18n import TR
from utils.notification import (
    notify_download_started,
    notify_download_completed,
    notify_download_failed,
)
from utils.win_path import sanitize_title


__all__ = ["DownloadManager", "AddResult", "manifest_estimated_size"]


# ---------------------------------------------------------------------------
# security-stability-hardening R34 (tasks.md 28.1) — idempotency + precheck
# ---------------------------------------------------------------------------
#
# ``manifest_estimated_size`` is a stub today: producing a reliable upper
# bound on an HLS/direct download from the manifest URL alone requires a
# network round-trip (``HEAD`` + manifest parse + ``#EXT-X-TARGETDURATION``
# × segment count), which is future work owned by the HLS probe (R34 is
# explicitly covered under the "future work" note in tasks.md 28.1). Until
# then, callers fall back to the 500 MiB default. Returning ``None`` — not
# ``0`` — signals "no estimate available" so the caller can distinguish it
# from a genuine zero-byte manifest.
_DEFAULT_MANIFEST_SIZE_BYTES = 500 * 1024 * 1024  # 500 MiB fallback
# Required-free multiplier applied to the estimate before comparing against
# ``shutil.disk_usage(...).free`` (mirrors the 20% headroom the component
# installer enforces in R17 and keeps the two precheck paths aligned).
_DISK_HEADROOM_FACTOR = 1.2


def manifest_estimated_size(url: str) -> Optional[int]:
    """Return an estimated download size (in bytes) for ``url``, or ``None``.

    R34.1 / tasks.md 28.1: the real implementation must inspect the
    HLS manifest (or ``Content-Length`` for direct downloads) to
    produce a byte estimate. The production implementation is future
    work; the stub always returns ``None`` so callers fall back to the
    ``_DEFAULT_MANIFEST_SIZE_BYTES`` default (500 MiB). Kept as a module
    level function (rather than a ``DownloadManager`` method) so tests
    can monkey-patch it without instantiating the manager.
    """
    return None


@dataclass(frozen=True)
class AddResult:
    """Structured outcome of :meth:`DownloadManager.add_task`.

    R34 / tasks.md 28.1: ``add_task`` now returns a structured value
    so callers can distinguish "accepted and queued" from "merged into
    an existing task" or "needs user confirmation (disk precheck)".
    The dataclass is frozen because the value is treated as a snapshot
    by the UI and must never mutate after construction.

    ``status`` values:

    * ``"queued"``             -- task accepted and enqueued; a fresh
      entry was recorded in ``DownloadManager._by_key``.
    * ``"merged"``             -- the idempotency key already maps to
      an existing task; the pre-existing task is returned in ``task``
      and the new task is **not** enqueued.
    * ``"needs_confirmation"`` -- disk precheck reported insufficient
      free space; the caller should prompt the user and, on approval,
      call ``add_task(..., bypass_disk_check=True)``.
    * ``"failed"``             -- reserved for callers that prefer a
      structured failure return over an exception (the production path
      still raises for backward compatibility with existing UI code
      that ignores the return value).
    """

    status: Literal["queued", "merged", "needs_confirmation", "failed"]
    task: Optional[DownloadTask] = None
    reason: Optional[str] = None


class DownloadManager:
    """Download task manager."""

    # Kept as a ``ClassVar`` alias on the class for backwards
    # compatibility with callers that read
    # ``DownloadManager._STOP_REASON_CLASSIFICATION`` directly; the
    # authoritative definition lives in :mod:`core.download.classifier`.
    _STOP_REASON_CLASSIFICATION = STOP_REASON_CLASSIFICATION

    def __init__(self, engines: list[BaseEngine], max_concurrent: int = 3):
        self.engines = engines
        self.selector = EngineSelector(engines)
        self.task_queue = Queue()
        self.active_tasks: List[DownloadTask] = []
        self.paused_tasks: List[DownloadTask] = []
        self.completed_tasks: List[DownloadTask] = []
        self.failed_tasks: List[DownloadTask] = []
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()
        self._worker_gate = threading.Condition()
        self._running_slots = 0
        self.on_task_update: Callable | None = None
        # security-stability-hardening R11.7 / R29 — TaskSnapshot channel.
        # ``on_task_snapshot`` is the Stage 2 path that UI consumers
        # (``MainWindow.task_update_received``) migrate to; it delivers
        # an immutable ``TaskSnapshot`` captured under ``task.lock`` so
        # the UI never reads half-written volatile fields. The legacy
        # ``on_task_update(task)`` callback is preserved during the
        # migration wave because ``ui/download_queue.py`` still reads
        # the raw task for tree rendering (Stage 4 R26 will finish the
        # migration). Both callbacks are invoked by :meth:`_emit_snapshot`.
        self.on_task_snapshot: Callable[[TaskSnapshot], None] | None = None
        self._metrics = {
            "success_total": 0,
            "failed_total": 0,
            "by_engine": {},
            "by_stage": {},
        }
        # Worker pool bookkeeping is delegated to ``WorkerPool``; the
        # manager still owns the admission gate (``_worker_gate`` +
        # ``_running_slots``) and the per-task lifecycle. We construct
        # the pool first but defer spawning workers until this
        # ``__init__`` completes, so ``_worker`` (which reads
        # ``self._worker_pool``) never sees a half-initialised manager.
        self._worker_pool = WorkerPool(
            target=self._worker,
            stop_flag=self._stop_flag,
            max_concurrent=max_concurrent,
            notify_workers=self._notify_workers,
        )
        self._worker_pool.start()

        # security-stability-hardening R34 / tasks.md 28.1 — idempotency map.
        #
        # ``_by_key`` maps a deterministic sha1 of
        # ``url|engine|out_dir|sanitize_title(title)`` to the
        # :class:`DownloadTask` that owns it. On repeat ``add_task`` calls
        # the same key short-circuits to ``AddResult.merged(...)`` instead
        # of queueing a duplicate task (which in the legacy code risked
        # ``rmtree``-ing the existing temp directory). The entry is
        # removed when the task reaches a terminal state
        # (``completed`` / ``failed`` / ``removed``) so a later retry can
        # re-enqueue under the same key. Access is serialized via
        # ``self._lock`` — the same lock that guards the state-list
        # buckets — so the key table and the state buckets stay
        # consistent.
        self._by_key: dict[str, DownloadTask] = {}

    # ------------------------------------------------------------------
    # Backwards-compatible façade over WorkerPool
    # ------------------------------------------------------------------
    @property
    def max_concurrent(self) -> int:
        return self._worker_pool.max_concurrent

    @max_concurrent.setter
    def max_concurrent(self, value: int) -> None:
        # Some legacy call sites assign directly; keep behaviour by
        # routing through ``set_max_concurrent`` so the worker pool
        # observes the change.
        self._worker_pool.set_max_concurrent(value)

    @property
    def _workers(self) -> list[threading.Thread]:
        # Read-only façade so shutdown() and external inspectors still
        # see the live worker list (used historically for join loops).
        return self._worker_pool.workers()

    @property
    def _workers_meta(self) -> list[tuple[threading.Thread, threading.Event]]:
        return list(self._worker_pool._workers_meta)

    @property
    def _workers_lock(self) -> threading.RLock:
        return self._worker_pool._lock

    @property
    def _soft_exit_deadlines(self) -> dict[threading.Thread, float]:
        return self._worker_pool._soft_exit_deadlines

    @property
    def _supervisor_thread(self) -> threading.Thread | None:
        return self._worker_pool._supervisor_thread

    @property
    def _supervisor_wake(self) -> threading.Event:
        return self._worker_pool._supervisor_wake

    @property
    def active_workers(self) -> int:
        """Return the count of workers currently tracked by the pool.

        R19.3: after any ``soft_exit`` / supervisor sweep, this reflects
        the **effective** pool size. Delegated to :class:`WorkerPool`.
        """

        return self._worker_pool.active_workers

    def set_max_concurrent(self, new_value: int) -> None:
        """Dynamically adjust the concurrent worker count (R19).

        Delegated to :meth:`WorkerPool.set_max_concurrent`. The full
        behaviour contract (idempotency, grow, shrink with soft-exit,
        supervisor-enforced 30s ceiling) is documented there.
        """

        self._worker_pool.set_max_concurrent(new_value)

    def add_task(
        self,
        task: DownloadTask,
        user_engine_preference: str | None = None,
        *,
        bypass_disk_check: bool = False,
    ) -> "AddResult":
        """Add a download task into queue.

        R34 / tasks.md 28.1: the method now returns an :class:`AddResult`
        describing the outcome. Existing callers (``ui/main_window.py``
        and ``ui/main_window_sniff_flow.py``) ignore the return value,
        so behaviour stays compatible; new callers can inspect the
        result to distinguish ``"queued"`` / ``"merged"`` /
        ``"needs_confirmation"`` paths.

        Parameters
        ----------
        task:
            The :class:`DownloadTask` to enqueue. Its ``url`` / ``engine``
            / ``save_dir`` / ``filename`` are hashed into the
            idempotency key (see :meth:`_compute_idempotency_key`).
        user_engine_preference:
            Optional engine preference forwarded to the engine
            selector; unchanged from the pre-R34 signature.
        bypass_disk_check:
            When ``True``, the disk precheck is skipped and a
            ``disk_precheck=bypassed`` marker is logged. Used by the UI
            after the user explicitly dismisses the
            ``needs_confirmation`` dialog.
        """

        # ------------------------------------------------------------------
        # R34.3 — idempotency key check (before any state mutation).
        # ------------------------------------------------------------------
        #
        # The key is computed from the *current* task attributes — which
        # means if the caller changes ``save_dir`` or ``filename`` before
        # retrying, the retry is considered a *different* task and will
        # not merge. This matches the acceptance criterion which keys on
        # ``url + engine + out_dir + title_hash``.
        idempotency_key = self._compute_idempotency_key(task, user_engine_preference)
        with self._lock:
            existing = self._by_key.get(idempotency_key)
        if existing is not None and existing is not task:
            logger.info(
                f"[QUEUE] {TR('log_queue_exists_skip')}: {task.filename}",
                event="download_queue_add_merged",
                filename=getattr(task, "filename", ""),
                url=getattr(task, "url", ""),
                existing_filename=getattr(existing, "filename", ""),
                idempotency_key=idempotency_key,
            )
            return AddResult(status="merged", task=existing, reason="duplicate_key")

        # ------------------------------------------------------------------
        # R34.1 / R34.2 — disk precheck (skippable).
        # ------------------------------------------------------------------
        if not bypass_disk_check:
            precheck = self._check_disk_space(task)
            if precheck is not None:
                # ``precheck`` is already an ``AddResult(needs_confirmation)``
                # populated with reason details; log and return as-is.
                logger.warning(
                    f"[QUEUE] disk_precheck_blocked: {task.filename}",
                    event="download_queue_disk_precheck_blocked",
                    filename=getattr(task, "filename", ""),
                    url=getattr(task, "url", ""),
                    reason=precheck.reason,
                )
                return precheck
        else:
            # R34.2 — user explicitly chose to bypass; record the marker
            # so the Stage 4 telemetry can attribute a later "disk full"
            # failure back to the decision.
            logger.info(
                f"[QUEUE] disk_precheck=bypassed: {task.filename}",
                event="download_queue_disk_precheck_bypassed",
                filename=getattr(task, "filename", ""),
                url=getattr(task, "url", ""),
                disk_precheck="bypassed",
            )

        logger.info(
            f"[QUEUE] {TR('log_queue_preparing_add')}",
            event="download_queue_add_start",
            filename=getattr(task, "filename", ""),
            url=getattr(task, "url", ""),
            user_engine_preference=user_engine_preference or TR("strategy_auto"),
        )

        try:
            with self._lock:
                if task in self.active_tasks:
                    logger.info(f"{TR('log_queue_executing_skip')}: {task.filename}")
                    return AddResult(status="merged", task=task, reason="already_active")
                self._remove_task_from_state_lists(task)

            if self._is_task_queued(task):
                logger.info(f"{TR('log_queue_exists_skip')}: {task.filename}")
                return AddResult(status="merged", task=task, reason="already_queued")

            engine, engine_name = self.selector.select(task.url, user_engine_preference)
            if engine is None:
                raise RuntimeError(f"{TR('msg_engine_not_found_text')}: {user_engine_preference or TR('strategy_auto')}")

            self._reset_task_runtime(task)
            task.engine = engine_name
            task.status = "waiting"

            user_specified = user_engine_preference is not None
            self.task_queue.put((task, engine, user_specified))

            # Record the idempotency mapping only after every enqueue
            # side-effect has completed so a failure in between (e.g.
            # ``_reset_task_runtime`` raising) doesn't leave a stale
            # entry. ``_by_key`` is removed when the task reaches a
            # terminal state in ``_execute_download``.
            with self._lock:
                self._by_key[idempotency_key] = task
                # Cache the key on the task so the terminal-state
                # cleanup can remove it without recomputing against
                # potentially-mutated attributes (e.g. ``save_dir`` may
                # be normalized by an engine on first run).
                setattr(task, "_idempotency_key", idempotency_key)

            self._notify_workers()
            logger.info(
                f"{TR('log_queue_added')}: {task.filename} (引擎: {engine_name}, 用户指定: {user_specified})"
            )

            if self.on_task_update or self.on_task_snapshot:
                self._emit_snapshot(task)

            return AddResult(status="queued", task=task)
        except Exception as e:
            task.status = "failed"
            task.error_message = str(e)
            logger.error(
                f"[QUEUE] {TR('log_queue_add_failed')}: {task.filename} - {e}",
                event="download_queue_add_failed",
                filename=getattr(task, "filename", ""),
                url=getattr(task, "url", ""),
            )
            if self.on_task_update or self.on_task_snapshot:
                try:
                    self._emit_snapshot(task)
                except Exception as callback_error:
                    logger.error(f"[QUEUE] {TR('log_queue_callback_failed')}: {callback_error}")
            raise

    # ------------------------------------------------------------------
    # security-stability-hardening R34 — helpers for add_task
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_idempotency_key(
        task: DownloadTask, user_engine_preference: str | None
    ) -> str:
        """Return the sha1 idempotency key for ``task`` (R34.3).

        The key combines four dimensions: the raw URL, the effective
        engine name (manual preference wins over the already-assigned
        ``task.engine`` so the key is stable across re-selection), the
        output directory, and the sanitized title. ``sanitize_title``
        from :mod:`utils.win_path` is the same helper used by the
        ``M3U8Resource`` path construction (R12), so the title
        component is computed the same way the filesystem sees it.
        """

        engine_name = (user_engine_preference or task.engine or "").strip().lower()
        out_dir = task.save_dir or ""
        # ``task.filename`` is the post-sanitization title used for the
        # on-disk file; running it through ``sanitize_title`` again is
        # idempotent (``sanitize_title(sanitize_title(x)) == sanitize_title(x)``)
        # and guards against callers that hand a raw title in.
        title_component = sanitize_title(task.filename or "")
        raw = f"{task.url}|{engine_name}|{out_dir}|{title_component}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _check_disk_space(self, task: DownloadTask) -> "AddResult | None":
        """Return ``AddResult(needs_confirmation)`` if free space is low.

        R34.1: compare ``manifest_estimated_size(url) or 500 MiB`` (with
        a 20% headroom factor, matching the R17 installer precheck)
        against ``shutil.disk_usage(task.save_dir).free``. Returns
        ``None`` when there is enough space, or when the disk query
        itself fails — a precheck that can't run must not block an
        otherwise-valid task (the engine's own disk-full handling will
        surface a structured error downstream).
        """

        estimate = manifest_estimated_size(task.url)
        if estimate is None:
            estimate = _DEFAULT_MANIFEST_SIZE_BYTES
        needed = int(estimate * _DISK_HEADROOM_FACTOR)

        probe_path = task.save_dir or "."
        try:
            free = shutil.disk_usage(probe_path).free
        except OSError as exc:
            # ``save_dir`` may not exist yet on first run; the engine
            # will create it. Log at debug and let the engine surface
            # any real disk failure.
            logger.debug(
                "download_manager: disk precheck skipped (%s)",
                type(exc).__name__,
                event="download_queue_disk_precheck_skipped",
                filename=getattr(task, "filename", ""),
            )
            return None

        if free >= needed:
            return None

        return AddResult(
            status="needs_confirmation",
            task=task,
            reason="insufficient_disk",
        )

    def _forget_idempotency_key(self, task: DownloadTask) -> None:
        """Drop ``task``'s entry from ``_by_key`` (R34.3 terminal cleanup).

        Called from ``_execute_download`` when a task reaches a terminal
        state (``completed`` / ``failed`` / ``removed``) so that a
        subsequent retry with the same ``url + engine + out_dir +
        title`` combination can re-enqueue under the same key. Safe to
        call repeatedly; missing entries are ignored. The lookup
        prefers the cached ``_idempotency_key`` attribute (captured at
        enqueue time) to avoid recomputing against attributes that may
        have been mutated mid-download.
        """

        cached = getattr(task, "_idempotency_key", None)
        with self._lock:
            if cached is not None:
                existing = self._by_key.get(cached)
                if existing is task:
                    self._by_key.pop(cached, None)
                    return
            # Cached key missing or stale — fall back to identity sweep.
            stale_keys = [k for k, v in self._by_key.items() if v is task]
            for key in stale_keys:
                self._by_key.pop(key, None)

    def _reset_task_runtime(self, task: DownloadTask):
        """Reset task runtime fields before queueing."""
        task.error_message = ""
        task.stop_requested = False
        task.stop_reason = ""
        task.speed = ""
        task.downloaded_size = ""
        task.retry_count = 0
        task.started_at = None
        task.completed_at = None
        task.process = None
        task.progress = 0.0
        setattr(task, "_history_recorded_status", None)

    # ------------------------------------------------------------------
    # security-stability-hardening R11.7 / R29 — snapshot fan-out
    # ------------------------------------------------------------------
    def _emit_snapshot(self, task: DownloadTask) -> None:
        """Fan a task change out to both the legacy and snapshot callbacks.

        The single entry-point keeps the Stage 2 migration coherent:

        * ``on_task_update(task)`` is preserved for the raw-task consumers
          (``ui/download_queue.py`` still reads ``task.filename``/
          ``task.status`` directly while it renders the queue tree).
        * ``on_task_snapshot(TaskSnapshot)`` is the R11.7 / R29 channel
          consumed by ``MainWindow.task_update_received``. The snapshot
          is captured via :meth:`TaskSnapshot.from_task`, which takes
          ``task.lock`` so the UI thread never observes a half-written
          task state.

        Snapshot construction is best-effort: a broken task object (e.g.
        a test double that is missing ``lock``) must not bring down a
        download worker. Consumer exceptions are caught separately so a
        flaky callback cannot starve the worker thread either.
        """

        cb_raw = self.on_task_update
        if cb_raw is not None:
            try:
                cb_raw(task)
            except Exception as cb_error:  # pragma: no cover - defensive
                logger.warning(
                    f"[QUEUE] {TR('log_queue_callback_failed')}: {cb_error}",
                    event="download_task_callback_failed",
                )

        cb_snap = self.on_task_snapshot
        if cb_snap is None:
            return
        try:
            snapshot = TaskSnapshot.from_task(task)
        except Exception as snap_error:  # pragma: no cover - defensive
            logger.warning(
                f"[QUEUE] task_snapshot_build_failed: {snap_error}",
                event="download_task_snapshot_build_failed",
                filename=getattr(task, "filename", ""),
            )
            return
        try:
            cb_snap(snapshot)
        except Exception as cb_error:  # pragma: no cover - defensive
            logger.warning(
                f"[QUEUE] task_snapshot_dispatch_failed: {cb_error}",
                event="download_task_snapshot_dispatch_failed",
                filename=getattr(task, "filename", ""),
            )

    def _remove_task_from_state_lists(self, task: DownloadTask):
        """Remove task from all in-memory state lists (dedup-safe)."""
        for bucket in (
            self.active_tasks,
            self.paused_tasks,
            self.completed_tasks,
            self.failed_tasks,
        ):
            while task in bucket:
                bucket.remove(task)

    def _is_task_queued(self, task: DownloadTask) -> bool:
        """Check if task already exists in queue."""
        with self.task_queue.mutex:
            return any(entry[0] is task for entry in self.task_queue.queue)

    def _snapshot_queued_tasks(self) -> list[DownloadTask]:
        """Thread-safe queued task snapshot."""
        with self.task_queue.mutex:
            return [entry[0] for entry in list(self.task_queue.queue)]

    def _remove_task_from_queue(self, task: DownloadTask) -> int:
        """Remove queued entries matching task and fix queue counters."""
        removed = 0
        with self.task_queue.mutex:
            old_entries = list(self.task_queue.queue)
            kept_entries = []
            for entry in old_entries:
                if entry[0] is task:
                    removed += 1
                else:
                    kept_entries.append(entry)
            if removed:
                self.task_queue.queue.clear()
                self.task_queue.queue.extend(kept_entries)
                self.task_queue.unfinished_tasks = max(
                    0, self.task_queue.unfinished_tasks - removed
                )
                if self.task_queue.unfinished_tasks == 0:
                    self.task_queue.all_tasks_done.notify_all()
                self.task_queue.not_full.notify_all()
        if removed:
            self._notify_workers()
        return removed

    @staticmethod
    def _unique_tasks(tasks: list[DownloadTask]) -> list[DownloadTask]:
        """Deduplicate tasks by object identity while keeping order."""
        seen = set()
        result = []
        for task in tasks:
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(task)
        return result

    def _notify_workers(self):
        """Wake workers after queue or concurrency state changes."""
        with self._worker_gate:
            self._worker_gate.notify_all()

    # ------------------------------------------------------------------
    # security-stability-hardening R18 — classification façade
    # ------------------------------------------------------------------
    # The three methods below preserve the pre-split call-site API
    # (``self._classify_failure`` / ``self._classify_message_keywords`` /
    # ``self._detect_failure_stage``) while delegating to the stateless
    # helpers in :mod:`core.download.classifier`. Keeping them as bound
    # methods means external tests and debug hooks that patched
    # ``DownloadManager._classify_failure`` continue to work unchanged.
    def _classify_failure(
        self,
        task: DownloadTask | None,
        message: str | None = None,
    ) -> str:
        """Delegate to :func:`core.download.classifier.classify_failure`."""

        return classify_failure(task, message)

    @staticmethod
    def _classify_message_keywords(message: str) -> str:
        """Delegate to :func:`core.download.classifier.classify_message_keywords`."""

        return classify_message_keywords(message)

    def _detect_failure_stage(self, message: str) -> str:
        """Delegate to :func:`core.download.classifier.detect_failure_stage`."""

        return detect_failure_stage(message)

    def _is_task_stop_requested(self, task: DownloadTask) -> bool:
        """Return True when task should stop retrying immediately."""
        return self._stop_flag.is_set() or bool(getattr(task, "stop_requested", False))

    def _apply_site_rules_to_task(self, task: DownloadTask) -> bool:
        """Fill missing auth headers from site_rules config."""
        from utils.config_manager import config

        site_rules = config.get("site_rules", []) or []
        task.headers = dict(task.headers or {})
        page_url = task.headers.get("referer", "")
        changed = False

        for rule in site_rules:
            if not rule.get("domains"):
                continue
            if not site_rule_matches(rule, task.url, page_url):
                continue

            referer = rule.get("referer")
            user_agent = rule.get("user_agent")
            headers = rule.get("headers", {}) or {}

            changed = set_header_if_missing(task.headers, "referer", referer) or changed
            changed = set_header_if_missing(task.headers, "user-agent", user_agent) or changed
            for key, value in headers.items():
                changed = set_header_if_missing(task.headers, key, value) or changed

            if changed:
                logger.info(TR("log_auth_headers_updated"), event="download_auth_headers", url=task.url)
            return changed

        return changed

    def _score_m3u8_candidate(self, url: str, task: DownloadTask) -> int:
        """Heuristic score for pre-download candidate ranking."""
        score = 0
        url_lower = (url or "").lower()
        headers = getattr(task, "headers", {}) or {}

        if url_lower.startswith("https://"):
            score += 20
        if ".m3u8" in url_lower:
            score += 40
        if any(k in url_lower for k in ("/hls/", "playlist", "index.m3u8", "media.m3u8")):
            score += 20
        if "master.m3u8" in url_lower:
            score -= 5
        if any(k in url_lower for k in ("ad", "ads", "promo", "tracker")):
            score -= 25

        if headers.get("referer"):
            score += 15
        if headers.get("origin"):
            score += 8
        if headers.get("cookie"):
            score += 25
        if headers.get("authorization"):
            score += 10

        try:
            host = urlparse(url).hostname or ""
            page_host = urlparse(headers.get("referer", "")).hostname or ""
            if host and page_host and host == page_host:
                score += 8
        except ValueError:
            # urlparse raises ValueError on malformed IPv6 / percent-encoding;
            # the host/referer match is a scoring nudge, so a miss is fine.
            logger.debug("download_manager: candidate host match skipped")

        return score

    def _rank_task_candidates(self, task: DownloadTask):
        """Rank task URL candidates and pick the best as primary URL."""
        candidates = []
        seen = set()
        for candidate in [task.url, getattr(task, "media_url", None), getattr(task, "master_url", None)]:
            if not candidate:
                continue
            value = candidate.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            score = self._score_m3u8_candidate(value, task)
            candidates.append((value, score))

        if len(candidates) < 2:
            return

        candidates.sort(key=lambda x: x[1], reverse=True)
        best_url = candidates[0][0]
        if best_url != task.url:
            logger.info(
                f"[RANK] {TR('log_rank_candidates')}",
                event="download_candidate_rank",
                best=best_url,
                ranked=" | ".join([f"{u} ({s})" for u, s in candidates]),
            )
            task.url = best_url
        setattr(task, "candidate_scores", {u: s for u, s in candidates})

    def _record_metric(self, engine: str, stage: str, success: bool):
        """Update aggregated runtime metrics for observability."""
        engine = engine or "unknown"
        stage = stage or "unknown"
        with self._lock:
            if success:
                self._metrics["success_total"] += 1
            else:
                self._metrics["failed_total"] += 1

            by_engine = self._metrics["by_engine"]
            if engine not in by_engine:
                by_engine[engine] = {"success": 0, "failed": 0}
            if success:
                by_engine[engine]["success"] += 1
            else:
                by_engine[engine]["failed"] += 1

            by_stage = self._metrics["by_stage"]
            if stage not in by_stage:
                by_stage[stage] = {"success": 0, "failed": 0}
            if success:
                by_stage[stage]["success"] += 1
            else:
                by_stage[stage]["failed"] += 1

            snapshot = {
                "success_total": self._metrics["success_total"],
                "failed_total": self._metrics["failed_total"],
            }
        logger.info(
            f"[METRICS] {TR('log_metrics_updated')}",
            event="download_metrics_snapshot",
            engine=engine,
            stage=stage,
            success=success,
            snapshot=snapshot,
        )

    def _learn_site_rule_from_task(self, task: DownloadTask):
        """Learn stable site rule from successful task (opt-in)."""
        from utils.config_manager import config

        if not config.get("site_rules_auto.enabled", False):
            return

        url = (task.url or "").strip()
        headers = task.headers or {}
        referer = headers.get("referer")
        user_agent = headers.get("user-agent")
        origin = headers.get("origin")
        cookie = headers.get("cookie")

        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return
        if not referer and not user_agent:
            return

        site_rules = config.get("site_rules", []) or []
        max_rules = int(config.get("site_rules_auto.max_rules", 50))
        allow_cookie = bool(config.get("site_rules_auto.allow_cookie", False))
        rule_name = f"auto:{host}"

        existing = None
        for rule in site_rules:
            if rule.get("name") == rule_name:
                existing = rule
                break

        rule_headers = {}
        if origin:
            rule_headers["origin"] = origin
        if allow_cookie and cookie:
            rule_headers["cookie"] = cookie

        if existing:
            changed = False
            domains = existing.get("domains", []) or []
            if host not in domains:
                domains.append(host)
                existing["domains"] = domains
                changed = True
            if referer and not existing.get("referer"):
                existing["referer"] = referer
                changed = True
            if user_agent and not existing.get("user_agent"):
                existing["user_agent"] = user_agent
                changed = True
            existing_headers = existing.get("headers", {}) or {}
            for k, v in rule_headers.items():
                if k not in existing_headers:
                    existing_headers[k] = v
                    changed = True
            if changed:
                existing["headers"] = existing_headers
                config.config["site_rules"] = site_rules
                config.save()
                logger.info(
                    f"[AUTO-RULE] {TR('log_auto_rule_updated')}",
                    event="site_rule_auto_learned",
                    host=host,
                    rule=rule_name,
                )
            return

        if len(site_rules) >= max_rules:
            logger.warning(
                f"[AUTO-RULE] {TR('log_auto_rule_max_reached')}",
                event="site_rule_auto_skipped",
                host=host,
                reason="max_rules_reached",
                max_rules=max_rules,
            )
            return

        new_rule = {
            "name": rule_name,
            "domains": [host],
            "url_keywords": ["m3u8"],
            "referer": referer or "",
            "user_agent": user_agent or "",
            "headers": rule_headers,
            "auto": True,
        }
        site_rules.append(new_rule)
        config.config["site_rules"] = site_rules
        config.save()
        logger.info(
            f"[AUTO-RULE] {TR('log_auto_rule_added')}",
            event="site_rule_auto_learned",
            host=host,
            rule=rule_name,
        )

    def _worker(self, soft_exit: threading.Event | None = None):
        """Worker thread loop.

        ``soft_exit`` is the per-worker :class:`threading.Event`
        registered in :class:`WorkerPool`. When set by
        :meth:`WorkerPool.set_max_concurrent` (R19.1), the worker
        finishes any in-flight task and exits at the next loop
        iteration — it does **not** interrupt an active download. The
        parameter is optional so tests that invoke ``_worker`` directly
        (without the spawn path) still work.
        """

        if soft_exit is None:
            soft_exit = threading.Event()
        while not self._stop_flag.is_set():
            # R19.1: check between tasks (i.e. before we try to reserve a
            # new slot or pull from the queue). If soft_exit was requested
            # while we were running a download, _execute_download has
            # already returned and we exit cleanly here.
            if soft_exit.is_set():
                logger.info(
                    f"[WORKER] soft_exit honoured by {threading.current_thread().name}",
                    event="worker_soft_exit",
                    worker=threading.current_thread().name,
                )
                return
            reserved_slot = False
            got_task = False
            try:
                try:
                    with self._worker_gate:
                        while not self._stop_flag.is_set():
                            if soft_exit.is_set():
                                # Re-check under the gate so we don't
                                # silently park in wait() after being
                                # asked to exit.
                                break
                            if self.max_concurrent <= 0:
                                self._worker_gate.wait(timeout=0.2)
                                continue
                            if self._running_slots >= self.max_concurrent:
                                self._worker_gate.wait(timeout=0.2)
                                continue
                            with self.task_queue.mutex:
                                has_queued_tasks = bool(self.task_queue.queue)
                            if not has_queued_tasks:
                                self._worker_gate.wait(timeout=0.2)
                                continue
                            self._running_slots += 1
                            reserved_slot = True
                            break
                    if soft_exit.is_set():
                        # Loop around so the top-of-loop check logs + returns.
                        continue
                    if not reserved_slot:
                        continue

                    task, engine, user_specified = self.task_queue.get_nowait()
                except Empty:
                    continue
                got_task = True

                try:
                    self._execute_download(task, engine, user_specified)
                finally:
                    self.task_queue.task_done()
                    got_task = False
            except Exception as e:
                logger.error(f"{TR('log_worker_exception')}: {e}")
                if got_task:
                    # Defensive fallback: ensure queue counter does not leak.
                    try:
                        self.task_queue.task_done()
                    except ValueError:
                        # task_done() called more times than get()'d; harmless
                        # here because we only reach this branch on exception
                        # paths — log and continue.
                        logger.debug(
                            "download_manager: task_done double-counted in worker fallback"
                        )
            finally:
                if reserved_slot:
                    with self._worker_gate:
                        self._running_slots = max(0, self._running_slots - 1)
                        self._worker_gate.notify_all()

    def _execute_download(self, task: DownloadTask, engine: BaseEngine, user_specified: bool = False):
        """Execute one download task with retry/fallback."""
        from utils.config_manager import config

        if self._is_task_stop_requested(task):
            logger.info(f"[SKIP] {TR('log_skip_stopped')}: {task.filename}")
            return

        task.status = "downloading"
        task.started_at = datetime.now()
        task.retry_count = 0
        task.max_retries = int(config.get("max_retry_attempts", 2))
        backoff_seconds = int(config.get("retry_backoff_seconds", 1))
        features = config.get("features", {}) or {}
        retry_enabled = features.get("download_retry_enabled", True)
        fallback_enabled = features.get("download_engine_fallback", True)
        hls_probe_enabled = features.get("hls_probe_enabled", True)
        hls_probe_hard_fail = features.get("hls_probe_hard_fail", True)
        ranking_enabled = features.get("download_candidate_ranking_enabled", True)
        auth_retry_first = features.get("download_auth_retry_first", True)
        try:
            auth_retry_per_engine = int(features.get("download_auth_retry_per_engine", 1))
        except (TypeError, ValueError):
            auth_retry_per_engine = 1
        auth_retry_per_engine = max(auth_retry_per_engine, 0)

        with self._lock:
            self._remove_task_from_state_lists(task)
            self.active_tasks.append(task)

        if self.on_task_update or self.on_task_snapshot:
            self._emit_snapshot(task)

        if ranking_enabled and ".m3u8" in (task.url or "").lower():
            self._rank_task_candidates(task)

        # Optional m3u8 preflight probe (playlist -> key -> segment)
        if hls_probe_enabled and ".m3u8" in (task.url or "").lower():
            try:
                from core.services.hls_probe import HLSProbe

                probe_result = HLSProbe.probe(task.url, task.headers)
                probe_stage = probe_result.get("stage", "unknown")
                setattr(task, "probe_stage", probe_stage)
                setattr(task, "probe_result", probe_result)

                if probe_result.get("ok"):
                    logger.info(
                        f"[HLS-PROBE] {TR('log_hls_probe_ok')}",
                        event="hls_probe_ok",
                        url=task.url,
                        stage=probe_stage,
                        playlist=probe_result.get("playlist_url"),
                    )
                elif probe_result.get("soft_fail"):
                    probe_warning = probe_result.get("warning") or probe_result.get("error") or "unknown"
                    setattr(task, "probe_warning", probe_warning)
                    logger.warning(
                        "[HLS-PROBE] 预探测分片软失败，允许实际下载引擎继续尝试",
                        event="hls_probe_soft_failed",
                        url=task.url,
                        stage=probe_stage,
                        status_code=probe_result.get("status_code"),
                        segment=probe_result.get("segment_url"),
                        warning=probe_warning,
                    )
                else:
                    probe_error = probe_result.get("error", "unknown")
                    task.error_message = f"HLS probe failed at {probe_stage}: {probe_error}"
                    logger.warning(
                        f"[HLS-PROBE] {TR('log_hls_probe_failed')}",
                        event="hls_probe_failed",
                        url=task.url,
                        stage=probe_stage,
                        error=probe_error,
                        status_code=probe_result.get("status_code"),
                    )

                    if hls_probe_hard_fail and probe_result.get("hard_fail", True) and not self._is_task_stop_requested(task):
                        task.status = "failed"
                        with self._lock:
                            self._remove_task_from_state_lists(task)
                            self.failed_tasks.append(task)
                        notify_download_failed(task.filename, task.error_message)
                        logger.error(
                            f"[FAILED] {TR('log_task_failed')}: {task.filename}",
                            event="download_failed",
                            engine=task.engine,
                            url=task.url,
                            failure_kind="probe",
                            stage=probe_stage,
                            status_code=probe_result.get("status_code"),
                        )
                        with self._lock:
                            while task in self.active_tasks:
                                self.active_tasks.remove(task)
                        if self.on_task_update or self.on_task_snapshot:
                            self._emit_snapshot(task)
                        return
            except Exception as e:
                logger.warning(
                    f"[HLS-PROBE] {TR('log_hls_probe_exception')}",
                    event="hls_probe_exception",
                    url=task.url,
                    error=str(e),
                )

        notify_download_started(task.filename, task.engine)

        def progress_callback(data: dict):
            try:
                task.progress = data.get("progress", task.progress)
                task.speed = data.get("speed", "")
                task.downloaded_size = data.get("downloaded", "")
                if self.on_task_update or self.on_task_snapshot:
                    self._emit_snapshot(task)
            except Exception as e:
                logger.debug(f"{TR('log_progress_update_exception')}: {e}")

        def _try_download(selected_engine: BaseEngine, engine_name: str) -> bool:
            if self._is_task_stop_requested(task):
                return False
            try:
                task.engine = engine_name
                return selected_engine.download(task, progress_callback)
            except Exception as e:
                task.error_message = str(e)
                logger.error(
                    f"[FAILED] {TR('log_task_exception')}: {task.filename} - {e}",
                    event="download_engine_exception",
                    engine=engine_name,
                    url=task.url,
                    stage="engine_invoke",
                )
                return False

        candidates = self.selector.get_candidates(task.url)
        if user_specified:
            preferred = self.selector.get_engine_by_name(task.engine)
            if preferred:
                auto_candidates = [
                    (engine, engine.get_name())
                    for engine in self.engines
                    if engine != preferred and engine.can_handle(task.url)
                ]
                candidates = [(preferred, task.engine)] + auto_candidates

        success = False
        last_failure_kind = "unknown"
        last_failure_stage = "unknown"
        recovered_from_fallback = False
        recovered_from_engine_name = ""

        while task.retry_count <= task.max_retries and not success:
            if self._is_task_stop_requested(task):
                break

            last_error_message = ""
            last_failure_kind = "unknown"
            last_failure_stage = "unknown"
            candidate_list = candidates
            if not fallback_enabled and not user_specified:
                candidate_list = candidates[:1]

            for candidate_index, (candidate_engine, candidate_name) in enumerate(candidate_list):
                if self._is_task_stop_requested(task):
                    break

                logger.info(
                    f"[TRY] {TR('label_engine')}: {candidate_name}，{TR('label_attempts')}: {task.retry_count + 1}/{task.max_retries + 1}"
                )
                success = _try_download(candidate_engine, candidate_name)
                if success:
                    if candidate_index > 0:
                        recovered_from_fallback = True
                        recovered_from_engine_name = candidate_name
                    break

                if self._is_task_stop_requested(task):
                    last_failure_kind = "stopped"
                    last_failure_stage = "stopped"
                    last_error_message = task.error_message or ""
                    break

                last_error_message = task.error_message or ""
                last_failure_kind = self._classify_failure(task, last_error_message)
                last_failure_stage = self._detect_failure_stage(last_error_message)
                logger.warning(
                    f"[RETRY] {TR('log_failure_kind')}: {last_failure_kind}",
                    event="download_retry",
                    engine=candidate_name,
                    url=task.url,
                    stage=last_failure_stage,
                )

                if last_failure_kind == "auth":
                    self._apply_site_rules_to_task(task)
                    if auth_retry_first and auth_retry_per_engine > 0:
                        for auth_try in range(auth_retry_per_engine):
                            if self._is_task_stop_requested(task):
                                last_failure_kind = "stopped"
                                last_failure_stage = "stopped"
                                break

                            logger.info(
                                (
                                    f"[AUTH-RETRY] {TR('log_auth_retry_same')}: {candidate_name} "
                                    f"({auth_try + 1}/{auth_retry_per_engine})"
                                ),
                                event="download_auth_retry",
                                engine=candidate_name,
                                url=task.url,
                                stage="auth",
                            )
                            success = _try_download(candidate_engine, candidate_name)
                            if success:
                                break

                            last_error_message = task.error_message or ""
                            last_failure_kind = self._classify_failure(task, last_error_message)
                            last_failure_stage = self._detect_failure_stage(last_error_message)
                            logger.warning(
                                f"[AUTH-RETRY] {TR('log_auth_retry_failed')}",
                                event="download_auth_retry_failed",
                                engine=candidate_name,
                                url=task.url,
                                stage=last_failure_stage,
                                failure_kind=last_failure_kind,
                            )
                            if last_failure_kind != "auth":
                                break

                    if success:
                        break

                    if self._is_task_stop_requested(task):
                        break

                if candidate_index + 1 < len(candidate_list):
                    if user_specified and candidate_index == 0:
                        logger.warning(
                            f"[FALLBACK] {TR('log_fallback_user_engine_failed')}: {candidate_name}",
                            event="download_fallback_from_user_engine",
                            engine=task.engine,
                            fallback_to=candidate_list[candidate_index + 1][1],
                            url=task.url,
                            stage=last_failure_stage,
                            failure_kind=last_failure_kind,
                        )
                        user_specified = False
                        continue
                    if last_failure_kind == "parse" and fallback_enabled:
                        continue

            if not success:
                if self._is_task_stop_requested(task):
                    break
                task.retry_count += 1
                if task.retry_count > task.max_retries or not retry_enabled:
                    break

                effective_backoff = backoff_seconds
                if last_failure_kind == "timeout":
                    effective_backoff = max(
                        backoff_seconds * (2 ** (task.retry_count - 1)),
                        backoff_seconds,
                    )

                task.status = "waiting"
                if self.on_task_update or self.on_task_snapshot:
                    self._emit_snapshot(task)
                if effective_backoff > 0:
                    self._stop_flag.wait(timeout=effective_backoff)

        stop_reason = getattr(task, "stop_reason", "")
        if stop_reason == "removed":
            task.process = None
            with self._lock:
                self._remove_task_from_state_lists(task)
            # R34.3 / tasks.md 28.1 — terminal state: drop idempotency key
            # so a future re-add of the same (url, engine, out_dir, title)
            # combination enqueues freshly instead of merging into the
            # now-removed record.
            self._forget_idempotency_key(task)
            logger.info(f"[REMOVED] {TR('log_task_removed')}: {task.filename}")
        elif success:
            task.status = "completed"
            task.progress = 100.0
            task.completed_at = datetime.now()
            task.process = None
            self._learn_site_rule_from_task(task)
            self._record_metric(task.engine, "completed", True)
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.completed_tasks.append(task)
            # R34.3 — terminal state; see note above.
            self._forget_idempotency_key(task)
            notify_download_completed(task.filename)
            logger.info(f"[OK] {TR('log_task_completed')}: {task.filename}")
            if recovered_from_fallback:
                logger.warning(
                    f"[FALLBACK-RECOVERED] 用户指定引擎失败后已成功回退并完成: {task.filename}",
                    event="download_fallback_recovered",
                    engine=recovered_from_engine_name,
                    url=task.url,
                    original_engine=engine.get_name(),
                    failure_kind=last_failure_kind,
                    stage=last_failure_stage,
                )
        else:
            task.process = None
            if stop_reason == "paused":
                task.status = "paused"
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.paused_tasks.append(task)
                # ``paused`` is resumable and ``resume_task`` re-enters
                # ``add_task`` with the same (url, engine, out_dir,
                # title) — keeping the idempotency entry lets the
                # second call detect the re-entry and short-circuit
                # ``merged`` if someone else tries to duplicate while
                # we're paused. The entry is dropped by the normal
                # terminal branches once the task finally finishes.
                logger.info(f"[PAUSED] 任务已暂停: {task.filename}")
            elif stop_reason == "cancelled":
                task.status = "failed"
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                # R34.3 — terminal state: key cleared so retry works.
                self._forget_idempotency_key(task)
                self._record_metric(task.engine, "cancelled", False)
                logger.info(f"[CANCELLED] 任务已取消: {task.filename}")
            elif stop_reason == "shutdown":
                self._record_metric(task.engine, "shutdown", False)
                # Application is terminating; the manager instance is
                # being torn down too, so the key map will go with it.
                # Drop the entry for symmetry and so any lingering
                # reference (e.g. a snapshot emitter) observes a clean
                # table.
                self._forget_idempotency_key(task)
                logger.info(f"[STOP] 应用关闭，终止任务: {task.filename}")
            else:
                task.status = "failed"
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                # R34.3 — terminal state; see note above.
                self._forget_idempotency_key(task)
                self._record_metric(task.engine, last_failure_stage, False)
                notify_download_failed(task.filename, task.error_message or "所有引擎均失败")
                logger.error(
                    f"[FAILED] 任务失败: {task.filename}",
                    event="download_failed",
                    engine=task.engine,
                    url=task.url,
                    failure_kind=last_failure_kind,
                    stage=last_failure_stage,
                )

        with self._lock:
            while task in self.active_tasks:
                self.active_tasks.remove(task)
        if self.on_task_update or self.on_task_snapshot:
            self._emit_snapshot(task)

    def pause_task(self, task: DownloadTask):
        """Pause task."""
        task.stop_requested = True
        task.stop_reason = "paused"
        task.error_message = "用户暂停"
        removed_from_queue = self._remove_task_from_queue(task)
        if removed_from_queue > 0:
            logger.info(f"任务已从等待队列移除并暂停: {task.filename}")
        if task.process:
            try:
                self._kill_process_tree(task.process)
                task.status = "paused"
                logger.info(f"任务已暂停: {task.filename}")
            except Exception as e:
                logger.error(f"暂停任务失败: {e}")
        if task.status in {"waiting", "paused"}:
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.paused_tasks.append(task)
            task.status = "paused"
        if self.on_task_update or self.on_task_snapshot:
            self._emit_snapshot(task)

    def resume_task(self, task: DownloadTask):
        """Resume task."""
        logger.info(f"正在继续任务: {task.filename}")
        with self._lock:
            already_active = task in self.active_tasks
            already_queued = self._is_task_queued(task)
            process = task.process
            process_alive = False
            if process is not None:
                try:
                    process_alive = process.poll() is None
                except Exception:
                    process_alive = True

            if already_active or already_queued or process_alive:
                logger.warning(
                    f"[RESUME-SKIP] 任务仍有旧执行上下文，拒绝重复继续: {task.filename}",
                    event="resume_task_skipped_reentrant",
                    active=already_active,
                    queued=already_queued,
                    process_alive=process_alive,
                    url=task.url,
                )
                return

            self._remove_task_from_state_lists(task)
        self.add_task(task, task.engine or None)

    def cancel_task(self, task: DownloadTask):
        """Cancel task."""
        task.stop_requested = True
        task.stop_reason = "cancelled"
        task.error_message = "用户取消"
        task.status = "failed"
        removed_from_queue = self._remove_task_from_queue(task)
        if removed_from_queue > 0:
            logger.info(f"任务已从等待队列移除并取消: {task.filename}")
        if task.process:
            try:
                self._kill_process_tree(task.process)
                logger.info(f"任务已取消: {task.filename}")
            except Exception as e:
                logger.error(f"取消任务失败: {e}")
        if task.status in {"waiting", "paused", "failed"} and task not in self.active_tasks:
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.failed_tasks.append(task)
            # R34.3 — cancelling a non-active task means ``_execute_download``
            # will not run its terminal branch for this task, so clean up
            # the idempotency entry here. Active tasks are handled by
            # ``_execute_download``'s ``stop_reason == "cancelled"`` branch.
            self._forget_idempotency_key(task)
        if self.on_task_update or self.on_task_snapshot:
            self._emit_snapshot(task)

    def remove_task(self, task: DownloadTask):
        """Remove task from manager."""
        task.stop_requested = True
        task.stop_reason = "removed"
        task.error_message = "用户删除任务"
        if task.process:
            try:
                self._kill_process_tree(task.process)
            except Exception as e:
                logger.error(f"删除任务时终止进程失败: {e}")
        task.status = "removed"
        self._remove_task_from_queue(task)
        with self._lock:
            self._remove_task_from_state_lists(task)
        # R34.3 — dropped from all state lists; drop the idempotency
        # entry too so the same (url, engine, out_dir, title) can be
        # re-added later as a fresh task.
        self._forget_idempotency_key(task)
        logger.info(f"任务已从管理器移除: {task.filename}")

    def _kill_process_tree(self, process, *, expected_name: str | None = None) -> str:
        """Try to terminate process tree.

        security-stability-hardening R30.1 (tasks.md 27.1): when an
        ``expected_name`` is provided, the helper consults
        :func:`psutil.Process.name` on ``process.pid`` and refuses to kill
        anything whose image name does not match. This protects the
        engine-switch path against pid reuse — the OS may have recycled
        the pid after the engine exited but before ``_kill_process_tree``
        runs, in which case terminating that pid would hit an unrelated
        (possibly critical) process.

        Parameters
        ----------
        process:
            ``subprocess.Popen`` (or compatible object exposing ``.pid``
            and ``.kill()``).
        expected_name:
            Optional engine image name (e.g. ``"yt-dlp"``). Compared
            case-insensitively and ``.exe``-insensitively — see
            :func:`engines.base_engine._engine_name_matches`.

        Returns
        -------
        str
            * ``"ok"``              — process tree terminated (or already
              gone).
            * ``"pid_mismatch"``    — ``expected_name`` did not match;
              nothing killed. Callers should treat this as a successful
              no-op; the real engine process (if any) is already dead.
            * ``"no_such_process"`` — pid not found; treated as success
              by callers that ignore the return value.

        All historical call sites pass a ``process`` positionally and
        ignore the return value, so the new contract is fully backward
        compatible.
        """

        import os
        import subprocess

        pid = getattr(process, "pid", None)

        # ------------------------------------------------------------------
        # Optional pid-ownership guard (R30.1)
        # ------------------------------------------------------------------
        if expected_name and isinstance(pid, int) and pid > 0:
            try:
                import psutil  # type: ignore
                # Deferred import of the name-matcher keeps this module
                # decoupled from ``engines.base_engine`` at import time
                # (``engines.base_engine`` imports ``core.task_model``; we
                # avoid any reverse coupling here).
                from engines.base_engine import _engine_name_matches

                try:
                    actual_name = psutil.Process(pid).name()
                except psutil.NoSuchProcess:
                    logger.debug(
                        f"_kill_process_tree pid={pid} "
                        f"expected_name={expected_name!r}: process already exited"
                    )
                    return "no_such_process"
                except psutil.AccessDenied as exc:
                    logger.warning(
                        f"_kill_process_tree pid={pid} "
                        f"expected_name={expected_name!r}: psutil access denied "
                        f"({exc}); proceeding without name-match guard"
                    )
                else:
                    if not _engine_name_matches(actual_name, expected_name):
                        logger.warning(
                            f"_kill_process_tree skipped: pid={pid} "
                            f"actual_name={actual_name!r} does not match "
                            f"expected_name={expected_name!r} (likely pid reuse)"
                        )
                        return "pid_mismatch"
            except ImportError:
                logger.debug(
                    f"_kill_process_tree pid={pid}: psutil missing, "
                    f"skipping expected_name={expected_name!r} guard"
                )

        # ------------------------------------------------------------------
        # Platform-specific termination
        # ------------------------------------------------------------------
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                logger.debug(f"已使用 taskkill 终止进程: {process.pid}")
                return "ok"
            except Exception as e:
                logger.warning(f"taskkill 失败: {e}")

        try:
            import psutil

            try:
                proc = psutil.Process(process.pid)
            except psutil.NoSuchProcess:
                logger.debug(
                    f"_kill_process_tree: pid={process.pid} already gone before kill"
                )
                return "no_such_process"
            try:
                children = proc.children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    continue
                except Exception:
                    continue
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                return "no_such_process"
            logger.debug(
                f"已使用 psutil 终止进程树(PID: {process.pid}, 子进程: {len(children)})"
            )
            return "ok"
        except Exception as psutil_exc:
            # psutil may raise or be missing; fall back to direct
            # Popen.kill() and swallow any secondary error with a
            # specific handler.
            logger.debug(
                "download_manager: psutil tree kill failed (%s); falling back to Popen.kill()",
                type(psutil_exc).__name__,
            )
            try:
                process.kill()
            except OSError as kill_exc:
                # Process already exited or handle invalid — nothing to do.
                logger.debug(
                    "download_manager: fallback Popen.kill() skipped (%s)",
                    type(kill_exc).__name__,
                )
            return "ok"

    def get_all_tasks(self) -> List[DownloadTask]:
        """Return queued + active + completed + failed tasks."""
        queued_tasks = self._snapshot_queued_tasks()
        with self._lock:
            merged = (
                queued_tasks
                + list(self.active_tasks)
                + list(self.paused_tasks)
                + list(self.completed_tasks)
                + list(self.failed_tasks)
            )
        return self._unique_tasks(merged)

    def get_stats(self) -> dict:
        """Return task statistics."""
        queued_tasks = self._unique_tasks(self._snapshot_queued_tasks())
        with self._lock:
            active_tasks = self._unique_tasks(list(self.active_tasks))
            paused_tasks = self._unique_tasks(list(self.paused_tasks))
            completed_tasks = self._unique_tasks(list(self.completed_tasks))
            failed_tasks = self._unique_tasks(list(self.failed_tasks))
        return {
            "queued": len(queued_tasks),
            "active": len(active_tasks),
            "paused": len(paused_tasks),
            "completed": len(completed_tasks),
            "failed": len(failed_tasks),
            "total": (
                len(queued_tasks)
                + len(active_tasks)
                + len(paused_tasks)
                + len(completed_tasks)
                + len(failed_tasks)
            ),
        }

    def get_quality_metrics(self) -> dict:
        """Return aggregated success/failure metrics by engine and stage."""
        with self._lock:
            return {
                "success_total": self._metrics["success_total"],
                "failed_total": self._metrics["failed_total"],
                "by_engine": dict(self._metrics["by_engine"]),
                "by_stage": dict(self._metrics["by_stage"]),
            }

    def shutdown(self):
        """Shutdown download manager and workers."""
        logger.info(TR("log_closing_dl_mgr"))
        self._stop_flag.set()
        # Wake the supervisor so it notices _stop_flag promptly instead of
        # sleeping up to 1s on its poll timer.
        self._worker_pool.wake_supervisor()
        self._notify_workers()

        # Mark and cancel active tasks first.
        for task in list(self.active_tasks):
            task.stop_requested = True
            task.stop_reason = "shutdown"
            if task.process:
                try:
                    self._kill_process_tree(task.process)
                except OSError as exc:
                    # Process may already be gone; shutdown path continues.
                    logger.debug(
                        "download_manager: shutdown kill skipped (%s)",
                        type(exc).__name__,
                    )

        # Drain waiting queue to avoid lingering unfinished tasks.
        drained_tasks = []
        with self.task_queue.mutex:
            while self.task_queue.queue:
                entry = self.task_queue.queue.popleft()
                drained_tasks.append(entry[0])
            if drained_tasks:
                self.task_queue.unfinished_tasks = max(
                    0, self.task_queue.unfinished_tasks - len(drained_tasks)
                )
            if self.task_queue.unfinished_tasks == 0:
                self.task_queue.all_tasks_done.notify_all()
            self.task_queue.not_full.notify_all()
        self._notify_workers()

        for task in drained_tasks:
            task.stop_requested = True
            task.stop_reason = "shutdown"
            if task.status == "waiting":
                task.status = "failed"
            with self._lock:
                self._remove_task_from_state_lists(task)

        self._worker_pool.join_all(timeout=3.0)

        logger.info(TR("log_dl_mgr_closed"))
