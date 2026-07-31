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
from pathlib import Path
import threading
import time
from datetime import datetime
from typing import Callable, List, Literal, Optional
from urllib.parse import urljoin, urlparse

from core.site_rule_utils import iter_matching_site_rules, set_header_if_missing
from core.task_model import DownloadTask, InvalidTransition, TaskSnapshot
from core.engine_selector import EngineSelector
from core.download_context import RESOURCE_TYPE_HLS, build_engine_select_context
from utils.headers import normalized_forward_headers
from core.download.classifier import (
    STOP_REASON_CLASSIFICATION,
    classify_failure,
    classify_message_keywords,
    detect_failure_stage,
)
from core.download.worker_pool import WorkerPool
from core.download.task_queue import QueuedDownload, TaskQueue, default_task_key
from engines.base_engine import BaseEngine
from utils.logger import logger
from utils.redact import redact_url
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
# ``manifest_estimated_size`` (F-08) now derives a real byte estimate:
# HLS manifests go through the HLS probe (target duration × segment
# count × conservative bitrate), and direct download URLs fall back to
# a HEAD ``Content-Length``. Both paths are best-effort and return
# ``None`` on any failure so the caller keeps the 500 MiB default.
_DEFAULT_MANIFEST_SIZE_BYTES = 500 * 1024 * 1024  # 500 MiB fallback
# Required-free multiplier applied to the estimate before comparing against
# ``shutil.disk_usage(...).free`` (mirrors the 20% headroom the component
# installer enforces in R17 and keeps the two precheck paths aligned).
_DISK_HEADROOM_FACTOR = 1.2

# Default browser User-Agent used to fill missing ``user-agent`` headers
# before dispatching a download. Mirrors the UA hard-coded across the
# sniffer / m3u8 parser / HLS probe paths so every network-facing layer
# advertises the same browser identity.
_DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Progress callbacks can arrive at stdout-line frequency (hundreds or
# thousands of fragments per second for N_m3u8DL-RE).  The UI only needs
# meaningful changes, so DownloadManager coalesces snapshots while still
# updating the underlying task fields on every callback.
_PROGRESS_EMIT_MIN_INTERVAL_S = 0.5
_PROGRESS_EMIT_MIN_DELTA_PERCENT = 0.2
_PROGRESS_EMIT_TERMINAL_STATUSES = frozenset({"completed", "failed", "paused", "removed"})
_MANIFEST_HEAD_TIMEOUT_S = 5
_MANIFEST_HEAD_MAX_REDIRECTS = 5
_MANIFEST_HEAD_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_INTERNAL_HEADER_KEYS = frozenset({"_cookie_file", "_allow_authorization_header"})
_COMMON_TWO_LEVEL_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "com.au",
        "com.br",
        "com.cn",
        "com.hk",
        "com.tw",
        "co.jp",
        "co.kr",
        "co.in",
    }
)


def _manifest_head_no_redirects(url: str):
    """Issue one SSRF-guarded HEAD request without auto-following redirects."""

    import requests as _requests
    from utils.proxy_config import requests_proxies
    from utils.ssrf_guard import ensure_public, make_pinned_session

    resolved = ensure_public(url)
    session = None
    if resolved is not None and getattr(resolved, "ips", None):
        try:
            session = make_pinned_session(resolved)
        except Exception:
            session = None

    request_kwargs = {
        "timeout": _MANIFEST_HEAD_TIMEOUT_S,
        "allow_redirects": False,
    }
    proxies = requests_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies

    client = session if session is not None else _requests
    try:
        return client.head(url, **request_kwargs)
    except TypeError as exc:
        # Some tests replace ``requests.head`` with very small fakes that
        # predate the explicit no-redirect kwarg. Keep those fakes working
        # without weakening the production path.
        if "allow_redirects" not in str(exc):
            raise
        request_kwargs.pop("allow_redirects", None)
        return client.head(url, **request_kwargs)


def _manifest_head_follow_redirects(url: str):
    """Follow HEAD redirects with per-hop SSRF validation and no auto-follow."""

    import requests as _requests

    current_url = url
    for redirect_count in range(_MANIFEST_HEAD_MAX_REDIRECTS + 1):
        response = _manifest_head_no_redirects(current_url)
        status_code = getattr(response, "status_code", None)
        if status_code not in _MANIFEST_HEAD_REDIRECT_STATUSES:
            return response

        headers = getattr(response, "headers", {}) or {}
        location = headers.get("Location") or headers.get("location")
        if not location:
            return response
        if redirect_count >= _MANIFEST_HEAD_MAX_REDIRECTS:
            raise _requests.TooManyRedirects(
                f"Exceeded {_MANIFEST_HEAD_MAX_REDIRECTS} redirects"
            )

        next_url = urljoin(current_url, str(location).strip())
        close_response = getattr(response, "close", None)
        if callable(close_response):
            close_response()
        current_url = next_url


def manifest_estimated_size(url: str) -> Optional[int]:
    """Return an estimated download size (in bytes) for ``url``, or ``None``.

    F-08 / R34.1: derives a byte estimate from the manifest:

    * HLS (``.m3u8`` / HLS MIME): run :class:`core.services.hls_probe.
      HLSProbe.probe` and read its ``estimated_bytes`` field (target
      duration × segment count × 2 Mbps). Live/sliding playlists yield
      ``None``.
    * Direct download: a single HEAD request returning
      ``Content-Length``.
    * Any error / SSRF block / timeout → ``None`` (caller falls back
      to ``_DEFAULT_MANIFEST_SIZE_BYTES``).

    Kept as a module-level function so tests can monkey-patch it
    without instantiating the manager.
    """

    if not url or not isinstance(url, str):
        return None

    # Cheap path suffix hint: HLS-looking URLs go through the probe,
    # which already handles master/media redirects and SSRF pinning.
    looks_hls = ".m3u8" in url.lower()
    if looks_hls:
        try:
            from core.services.hls_probe import HLSProbe

            probe_result = HLSProbe.probe(url, headers={}, timeout=8)
            if isinstance(probe_result, dict):
                est = probe_result.get("estimated_bytes")
                if isinstance(est, int) and est > 0:
                    return est
        except Exception:
            # Best-effort: any probe failure → fall through to HEAD /
            # None. The disk precheck will use the 500 MiB default.
            return None

    # Direct-download fallback: HEAD for Content-Length.  Redirects are
    # followed manually so every hop is re-checked by the SSRF guard;
    # requests' automatic redirect handling must stay disabled here.
    try:
        resp = _manifest_head_follow_redirects(url)
        headers = getattr(resp, "headers", {}) or {}
        cl = headers.get("Content-Length") or headers.get("content-length")
        if cl:
            try:
                value = int(cl)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                return None
    except Exception:
        return None
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
        self.task_queue: TaskQueue[QueuedDownload] = TaskQueue()
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
        # ``on_task_snapshot`` is the primary display path: it delivers
        # an immutable ``TaskSnapshot`` captured under ``task.lock`` so
        # the UI never reads half-written volatile progress fields. The
        # legacy ``on_task_update(task)`` callback remains wired for raw
        # control handles and history persistence. Both callbacks are
        # invoked by :meth:`_emit_snapshot`.
        self.on_task_snapshot: Callable[[TaskSnapshot], None] | None = None
        self._metrics = {
            "success_total": 0,
            "failed_total": 0,
            "by_engine": {},
            "by_stage": {},
            "by_reason": {},
            "by_strategy": {},
            "by_retry_action": {},
            "by_probe_stage": {},
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
        self._progress_emit_state: dict[int, dict[str, object]] = {}
        self._progress_emit_lock = threading.Lock()

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

            selector_context = self._build_task_engine_context(task)
            engine, engine_name = self.selector.select(
                task.url,
                user_engine_preference,
                context=selector_context,
            )
            if engine is None:
                raise RuntimeError(f"{TR('msg_engine_not_found_text')}: {user_engine_preference or TR('strategy_auto')}")

            self._reset_task_runtime(task)
            task._set_fields_locked(engine=engine_name)
            task.transition("waiting")

            user_specified = user_engine_preference is not None
            self.task_queue.put(
                QueuedDownload(task=task, engine=engine, user_specified=user_specified)
            )

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

            logger.info(
                f"{TR('log_queue_added')}: {task.filename} (引擎: {engine_name}, 用户指定: {user_specified})"
            )

            if self.on_task_update or self.on_task_snapshot:
                logger.debug(
                    "下载任务首次 UI 快照发送开始",
                    event="download_queue_initial_emit_start",
                    filename=getattr(task, "filename", ""),
                    status=getattr(task, "status", ""),
                    engine=engine_name,
                )
                self._emit_snapshot(task)
                logger.debug(
                    "下载任务首次 UI 快照发送完成",
                    event="download_queue_initial_emit_done",
                    filename=getattr(task, "filename", ""),
                    status=getattr(task, "status", ""),
                    engine=engine_name,
                )

            # 首次 queued 快照/控制句柄更新已投递后再唤醒 worker，减少
            # UI 控制句柄注册与 worker 线程立即切换 downloading 状态之间
            # 的竞态窗口。
            self._notify_workers()
            logger.debug(
                "下载 worker 已唤醒",
                event="download_queue_worker_notified",
                filename=getattr(task, "filename", ""),
                status=getattr(task, "status", ""),
                engine=engine_name,
            )

            return AddResult(status="queued", task=task)
        except Exception as e:
            task.transition("failed")
            task._set_fields_locked(error_message=str(e))
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
    def _build_task_engine_context(task: DownloadTask):
        """Build selector context from the current ``DownloadTask`` fields."""
        return build_engine_select_context(
            url=getattr(task, "url", "") or "",
            page_url=getattr(task, "page_url", "") or "",
            page_title=getattr(task, "page_title", "") or "",
            headers=getattr(task, "headers", None),
            source=getattr(task, "source", None),
            resource_type=getattr(task, "resource_type", None),
            mime=getattr(task, "mime", "") or "",
            master_url=getattr(task, "master_url", None),
            media_url=getattr(task, "media_url", None),
        )

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

        This method runs on the caller's thread. In the desktop UI the caller is
        the Qt main thread, so it must not perform network I/O: synchronous HLS
        probing or HTTP HEAD requests here make the resource-table "download"
        button freeze for several seconds before the user can switch tabs.

        Use the conservative 500 MiB fallback for the immediate enqueue-time
        guard and leave real manifest probing to the worker-thread download
        flow. ``manifest_estimated_size`` remains available for tests and any
        non-UI/background caller that explicitly wants a best-effort network
        estimate.
        """

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
                f"download_manager: disk precheck skipped ({type(exc).__name__})",
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

    # Engines whose download path produces segment fragments under a
    # per-engine scratch subdir (``temp/n_m3u8dl`` etc.). ``aria2`` /
    # ``streamlink`` / ``ytdlp`` write directly to the save dir, so their
    # temp footprint is negligible and auto-delete need not fire.
    # Values match ``engine.get_name().lower()`` (``N_m3u8DL-RE`` -> ``n_m3u8dl-re``).
    _TEMP_SEGMENT_ENGINES: tuple[str, ...] = ("n_m3u8dl-re", "ffmpeg")

    def _maybe_auto_delete_temp(self, task: DownloadTask) -> None:
        """F-10: clean this task's temp fragments when ``auto_delete_temp`` is on.

        Triggered from the ``_execute_download`` success branch. Only
        engines that actually leave segment residue (N_m3u8DL-RE /
        ffmpeg) qualify. The completed task's own filename is *not*
        added to the skip list (its fragments are no longer needed),
        but every other running/paused task's filename is, so parallel
        downloads with the same stem are preserved. Failures are
        logged at debug — cleanup is best-effort and must never fail
        the download that just succeeded.
        """

        try:
            from utils.config_manager import config

            if not config.get("auto_delete_temp", True):
                return
        except Exception:  # pragma: no cover - config read never raises in practice
            return

        engine_name = (task.engine or "").lower()
        if engine_name not in self._TEMP_SEGMENT_ENGINES:
            return

        try:
            from utils.cache_cleaner import clean_temp_cache

            # Skip filenames of *other* active/paused tasks so we don't
            # delete fragments belonging to a concurrent download.
            with self._lock:
                skip_filenames = {
                    getattr(t, "filename", "")
                    for t in list(self.active_tasks) + list(self.paused_tasks)
                    if t is not task and getattr(t, "filename", "")
                }

            result = clean_temp_cache(skip_filenames=tuple(skip_filenames))
            logger.debug(
                f"auto_delete_temp: removed={result.files_removed} files, "
                f"{float(result.bytes_removed):.2f} bytes freed, "
                f"skipped={len(result.skipped)}, errors={len(result.errors)}",
                event="auto_delete_temp",
                engine=engine_name,
                filename=task.filename,
            )
            if result.errors:
                logger.warning(
                    f"auto_delete_temp: {len(result.errors)} errors while "
                    f"cleaning temp (engine={engine_name})",
                    event="auto_delete_temp_errors",
                    engine=engine_name,
                )
        except Exception as exc:
            logger.debug(
                f"auto_delete_temp: cleanup skipped ({type(exc).__name__}: {exc})",
                event="auto_delete_temp_failed",
                engine=engine_name,
            )

    # ------------------------------------------------------------------
    # Aria2 artifact validation helpers
    # ------------------------------------------------------------------
    # Task attribute that carries the actual aria2 output filename (set by
    # Aria2Engine._build_command after resolving the output extension).
    _ARIA2_OUTPUT_ATTR = "_aria2_output_filename"

    def _task_output_candidates(self, task: DownloadTask) -> list[Path]:
        """Return plausible on-disk artifact paths for *task*, sorted best-first."""
        save_dir = Path(str(getattr(task, "save_dir", "") or ""))
        filename = str(getattr(task, "filename", "") or "").strip()
        if not save_dir.exists() or not filename:
            return []

        candidates: list[Path] = []
        seen: set[Path] = set()

        # The actual filename that aria2 wrote to disk (may differ from task.filename).
        actual_name = str(getattr(task, self._ARIA2_OUTPUT_ATTR, "") or "").strip()
        if actual_name:
            actual = save_dir / actual_name
            if actual.is_file() and actual not in seen:
                candidates.append(actual)
                seen.add(actual)

        # Exact stem match (old behaviour — could be suffixless).
        direct = save_dir / filename
        if direct.is_file() and direct not in seen:
            candidates.append(direct)
            seen.add(direct)

        # Glob stem.*
        for p in save_dir.glob(f"{filename}.*"):
            if p.is_file() and p not in seen:
                candidates.append(p)
                seen.add(p)

        # Sort: preferred media suffixes first, then by name.
        _preferred = (".mp4", ".mkv", ".ts", ".m4v", ".mov", ".webm", ".flv")
        candidates.sort(
            key=lambda p: (
                0 if p.suffix.lower() in _preferred else 1,
                p.name.lower(),
            )
        )
        return candidates

    @staticmethod
    def _read_artifact_head(path: Path, size: int = 512) -> bytes:
        """Return up to *size* bytes from the beginning of *path*, or empty bytes."""
        try:
            with path.open("rb") as fh:
                return fh.read(size)
        except Exception:
            return b""

    @staticmethod
    def _classify_artifact_head(head: bytes) -> str:
        """Lightweight classification of file-head content.

        Returns one of:
          hls_manifest, dash_manifest, html_error, json_error,
          mp4, matroska, mpeg_ts, unknown
        """
        if not head:
            return "unknown"

        # Strip UTF‑8 BOM and leading whitespace.
        text = head.lstrip(b"\xef\xbb\xbf").lstrip()
        text_upper = text[:64].upper()

        if text_upper.startswith(b"#EXTM3U"):
            return "hls_manifest"
        if b"<MPD" in text_upper or b"XMLNS" in text_upper:
            return "dash_manifest"
        if text_upper.startswith(b"<HTML") or text_upper.startswith(b"<!DOCTYPE"):
            return "html_error"
        if head[:1] in (b"{", b"[") and len(head) < 2048:
            return "json_error"

        # ftyp box (MP4/MOV family) — bytes 4-7
        try:
            if head[4:8] == b"ftyp":
                return "mp4"
        except IndexError:
            pass

        # Matroska EBML magic (1A 45 DF A3)
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return "matroska"

        # TS sync byte: check first byte and the typical 188-byte alignment.
        if head[0:1] == b"\x47":
            return "mpeg_ts"

        return "unknown"

    def _validate_aria2_artifact(self, task: DownloadTask) -> tuple[bool, str]:
        """Post-download sanity check for Aria2 engine output.

        Returns ``(False, reason)`` when the artifact is clearly *not*
        a final playable media file (manifest, HTML error page, etc.),
        and ``(True, kind)`` when the file looks acceptable.
        """
        candidates = self._task_output_candidates(task)
        if not candidates:
            return False, "aria2_no_output_artifact"

        # Even if engine selection already tried to block Aria2 for HLS/segment,
        # double-check here in case the task was queued through a code path
        # that bypassed the selector (e.g. batch / history replay).
        resource_type = str(getattr(task, "resource_type", "") or "").lower()
        if resource_type in {"hls", "dash"}:
            return False, "aria2_not_allowed_for_hls"
        if resource_type == "segment":
            source = str(getattr(task, "source", "") or "").lower()
            if (
                getattr(task, "master_url", None)
                or getattr(task, "media_url", None)
                or getattr(task, "page_url", "")
                or source not in {"", "unknown"}
            ):
                return False, "aria2_not_allowed_for_segment_with_context"

        artifact = candidates[0]
        head = self._read_artifact_head(artifact)
        kind = self._classify_artifact_head(head)

        if kind in {"hls_manifest", "dash_manifest", "html_error", "json_error"}:
            return False, f"aria2_invalid_artifact:{kind}"

        if kind in {"mp4", "matroska", "mpeg_ts"}:
            return True, kind

        # Unknown content — trust if the suffix looks like media.
        _media_suffixes = frozenset(
            {".mp4", ".mkv", ".ts", ".m4v", ".mov", ".webm", ".flv", ".avi"}
        )
        if artifact.suffix.lower() in _media_suffixes:
            return True, "extension_trusted"

        return False, "aria2_unknown_suffixless_artifact"

    def _reset_task_runtime(self, task: DownloadTask):
        """Reset task runtime fields before queueing."""
        self._forget_progress_emit_state(task)
        # R11 — volatile fields in ``_LOCKED_FIELDS`` must be written
        # under ``task.lock`` via ``_set_fields_locked``.
        task._set_fields_locked(
            error_message="",
            progress=0.0,
            speed="",
            downloaded_size="",
            progress_attempt=0,
            progress_source_label="",
            stop_requested=False,
            stop_reason="",
            retry_count=0,
            process=None,
        )
        task.started_at = None
        task.completed_at = None
        setattr(task, "_history_recorded_status", None)

    # ------------------------------------------------------------------
    # security-stability-hardening R11.7 / R29 — snapshot fan-out
    # ------------------------------------------------------------------
    def _emit_snapshot(self, task: DownloadTask) -> None:
        """Fan a task change out to both the legacy and snapshot callbacks.

        The single entry-point keeps the two UI channels coherent:

        * ``on_task_update(task)`` is preserved for raw-task consumers that
          need control handles or terminal-state history records.
        * ``on_task_snapshot(TaskSnapshot)`` is the R11.7 / R29 channel
          consumed by ``MainWindow.task_update_received`` and drives queue
          rendering. The snapshot is captured via
          :meth:`TaskSnapshot.from_task`, which takes ``task.lock`` so the
          UI thread never observes a half-written task state.

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
        return self.task_queue.contains(default_task_key(task))

    def _snapshot_queued_tasks(self) -> list[DownloadTask]:
        """Thread-safe queued task snapshot."""
        return [entry.task for entry in self.task_queue.snapshot()]

    def _remove_task_from_queue(self, task: DownloadTask) -> int:
        """Remove a queued task without touching queue internals."""
        removed = self.task_queue.remove(default_task_key(task))
        if removed:
            self._notify_workers()
        return int(removed)

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
    # Download progress snapshot coalescing
    # ------------------------------------------------------------------
    def _should_emit_progress_snapshot(
        self,
        task: DownloadTask,
        *,
        progress: float,
        speed: str,
        downloaded: str,
        status: str,
        attempt: int | None = None,
        source_label: str | None = None,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        """Return whether a progress callback should fan out a snapshot.

        Worker-side progress callbacks can be extremely noisy.  This
        predicate keeps UI updates bounded while preserving all important
        visual transitions:

        * first progress callback for a task;
        * explicit force / terminal states;
        * task status changes;
        * 0 → positive progress so the UI leaves the "stuck at 0" state;
        * percentage movement of at least
          ``_PROGRESS_EMIT_MIN_DELTA_PERCENT``;
        * otherwise, at most one changed progress/speed/downloaded update
          per ``_PROGRESS_EMIT_MIN_INTERVAL_S``.

        The method is intentionally side-effect free; callers must record
        an actual emit with :meth:`_record_progress_snapshot_emit` only
        after the snapshot has been dispatched.
        """

        if force or status in _PROGRESS_EMIT_TERMINAL_STATUSES:
            return True

        current_time = time.monotonic() if now is None else float(now)
        speed = str(speed or "")
        downloaded = str(downloaded or "")

        with self._progress_emit_lock:
            last = self._progress_emit_state.get(id(task))
            if last is None:
                return True

            last_status = str(last.get("status") or "")
            if status != last_status:
                return True

            try:
                last_progress = float(str(last.get("progress", 0.0) or 0.0))
            except (TypeError, ValueError):
                last_progress = 0.0
            last_speed = str(last.get("speed") or "")
            last_downloaded = str(last.get("downloaded") or "")
            if attempt is not None:
                try:
                    last_attempt = int(str(last.get("attempt", 0) or 0))
                except (TypeError, ValueError):
                    last_attempt = 0
                if int(attempt) != last_attempt:
                    return True
            if source_label is not None:
                last_source_label = str(last.get("source_label") or "")
                if str(source_label or "") != last_source_label:
                    return True
            try:
                last_at = float(str(last.get("last_at", 0.0) or 0.0))
            except (TypeError, ValueError):
                last_at = 0.0

        if last_progress <= 0.0 < progress:
            return True

        if progress >= 0.0 and last_progress >= 0.0:
            if abs(progress - last_progress) >= _PROGRESS_EMIT_MIN_DELTA_PERCENT:
                return True

        elapsed = max(0.0, current_time - last_at)
        if elapsed < _PROGRESS_EMIT_MIN_INTERVAL_S:
            return False

        # Unknown-total fallback uses progress=-1 and relies on the
        # downloaded text ("N segments") to show life.  Time-gate these
        # text-only changes so a temp-dir poll loop cannot flood the UI.
        if progress < 0.0:
            return downloaded != last_downloaded or speed != last_speed

        if not last_speed and speed:
            return True

        return (
            progress != last_progress
            or speed != last_speed
            or downloaded != last_downloaded
        )

    def _record_progress_snapshot_emit(
        self,
        task: DownloadTask,
        *,
        progress: float,
        speed: str,
        downloaded: str,
        status: str,
        attempt: int | None = None,
        source_label: str | None = None,
        now: float | None = None,
    ) -> None:
        """Remember the last progress snapshot emitted for ``task``."""

        current_time = time.monotonic() if now is None else float(now)
        with self._progress_emit_lock:
            self._progress_emit_state[id(task)] = {
                "last_at": current_time,
                "progress": float(progress),
                "speed": str(speed or ""),
                "downloaded": str(downloaded or ""),
                "status": str(status or ""),
                "attempt": int(attempt or 0),
                "source_label": str(source_label or ""),
            }

    def _forget_progress_emit_state(self, task: DownloadTask) -> None:
        """Drop progress-throttle bookkeeping for ``task`` if present."""

        with self._progress_emit_lock:
            self._progress_emit_state.pop(id(task), None)

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

    @staticmethod
    def _truthy_policy_value(value: object) -> bool:
        """Return True for explicit opt-in style config values."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on", "allow", "allowed", "enabled", "temporary", "prompt"}

    @staticmethod
    def _registrable_domain(host: str) -> str:
        """Return a conservative registrable-domain approximation."""
        labels = [part for part in (host or "").strip(".").lower().split(".") if part]
        if len(labels) <= 2:
            return ".".join(labels)
        suffix = ".".join(labels[-2:])
        if suffix in _COMMON_TWO_LEVEL_PUBLIC_SUFFIXES and len(labels) >= 3:
            return ".".join(labels[-3:])
        return suffix

    @classmethod
    def _urls_share_site(cls, first_url: str, second_url: str) -> bool:
        """Return True when two URLs are same-host or same registrable site."""
        try:
            first_host = (urlparse(first_url or "").hostname or "").lower()
            second_host = (urlparse(second_url or "").hostname or "").lower()
        except ValueError:
            return False
        if not first_host or not second_host:
            return False
        if first_host == second_host:
            return True
        if first_host.endswith("." + second_host) or second_host.endswith("." + first_host):
            return True
        return cls._registrable_domain(first_host) == cls._registrable_domain(second_host)

    @classmethod
    def _allow_cookie_for_task(cls, task: DownloadTask, features: dict, raw_headers: dict | None = None) -> bool:
        """Return whether Cookie may be forwarded for this task."""
        if not bool(features.get("forward_cookie_headers", True)):
            return False

        source = (getattr(task, "cookie_policy_source", "") or "").strip().lower()
        if source.startswith("site_rule"):
            return True

        if not bool(features.get("temporary_cookie_forwarding_enabled", False)):
            return True

        if not bool(getattr(task, "temporary_cookie_allowed", False)):
            return False

        raw_headers = raw_headers or {}
        page_url = (
            (getattr(task, "page_url", "") or "").strip()
            or str(raw_headers.get("referer", "") or raw_headers.get("Referer", "") or "").strip()
        )
        return cls._urls_share_site(getattr(task, "url", "") or "", page_url)

    def _apply_site_rules_to_task(self, task: DownloadTask) -> bool:
        """Fill missing auth headers from site_rules config."""
        from utils.config_manager import config

        site_rules = config.get("site_rules", []) or []
        features = config.get("features", {}) or {}
        global_auth_opt_in = bool(features.get("forward_authorization_headers", False))
        task.headers = dict(task.headers or {})
        page_url = (
            (getattr(task, "page_url", "") or "").strip()
            or task.headers.get("referer", "")
            or task.headers.get("Referer", "")
            or task.url
        )

        resource_type = getattr(task, "resource_type", "") or ""
        for rule in iter_matching_site_rules(site_rules, task.url, page_url, resource_type):
            if not rule.get("domains"):
                continue

            before_headers = dict(task.headers)
            rule_auth_opt_in = bool(
                rule.get("allow_authorization", False)
                or rule.get("authorization_policy") == "prompt"
                or global_auth_opt_in
            )
            rule_cookie_opt_in = self._truthy_policy_value(
                rule.get("allow_cookie", False)
                or rule.get("cookie_policy")
                or rule.get("cookies_policy")
            )
            referer = str(rule.get("referer") or "")
            user_agent = str(rule.get("user_agent") or "")
            headers = rule.get("headers", {}) or {}
            if not isinstance(headers, dict):
                headers = {}

            set_header_if_missing(task.headers, "referer", referer)
            set_header_if_missing(task.headers, "user-agent", user_agent)
            for key, value in headers.items():
                set_header_if_missing(task.headers, str(key), str(value or ""))

            include_cookie = bool(features.get("forward_cookie_headers", True)) and (
                self._allow_cookie_for_task(task, features, task.headers)
                or rule_cookie_opt_in
            )
            normalized: dict[str, object] = dict(
                normalized_forward_headers(
                    task.headers,
                    include_cookie=include_cookie,
                    include_authorization=rule_auth_opt_in,
                )
            )
            for internal_key in _INTERNAL_HEADER_KEYS:
                if before_headers.get(internal_key):
                    normalized[internal_key] = before_headers[internal_key]
            if rule_cookie_opt_in and normalized.get("cookie"):
                task.temporary_cookie_allowed = True
                task.cookie_policy_source = f"site_rule:{rule.get('name', 'unknown')}"
            if rule_auth_opt_in and normalized.get("authorization"):
                normalized["_allow_authorization_header"] = True
                if not getattr(task, "auth_policy_source", ""):
                    task.auth_policy_source = f"site_rule:{rule.get('name', 'unknown')}"

            task.headers = normalized
            changed = task.headers != before_headers
            if changed:
                logger.info(
                    TR("log_auth_headers_updated"),
                    event="download_auth_headers",
                    url=task.url,
                    page_url=page_url,
                    resource_type=resource_type or "unknown",
                    rule=rule.get("name", "unknown"),
                    rule_priority=rule.get("priority", 0),
                    cookie_enabled=bool(normalized.get("cookie")),
                    cookie_policy_source=getattr(task, "cookie_policy_source", "") or "none",
                    authorization_enabled=bool(normalized.get("_allow_authorization_header")),
                )
            return changed

        return False

    def _normalize_task_headers_for_download(self, task: DownloadTask) -> bool:
        """Normalize ``task.headers`` in place before dispatching a download.

        Restores the header-normalization step that was deleted in the
        "wrong edit" regression (ISS-01): engine command builders expect a
        lowercase, browser-default-aware header map so downstream argv
        (``--user-agent`` / ``Origin:`` / ``--referer``) line up across
        engines. The method is idempotent and conservative:

        * All header keys are lowercased (duplicates merge, last value
          wins, mirroring HTTP duplicate-header combination).
        * A browser-default ``user-agent`` is filled only when no
          case-insensitive ``user-agent`` is already present.
        * ``origin`` is derived from ``referer`` (``scheme://netloc``)
          only when ``referer`` is set and no ``origin`` is present.
        * Existing explicit values are never overwritten.

        Returns ``True`` when any normalization change was applied so the
        download flow can decide whether to re-log the final headers.
        """
        from utils.config_manager import config

        raw = dict(task.headers or {})
        features = config.get("features", {}) or {}
        include_cookie = self._allow_cookie_for_task(task, features, raw)
        include_authorization = bool(
            features.get("forward_authorization_headers", False)
            or raw.get("_allow_authorization_header")
        )
        headers: dict[str, object] = dict(
            normalized_forward_headers(
                raw,
                include_cookie=include_cookie,
                include_authorization=include_authorization,
            )
        )
        for internal_key in _INTERNAL_HEADER_KEYS:
            if raw.get(internal_key):
                headers[internal_key] = raw[internal_key]
        if include_authorization and headers.get("authorization"):
            headers["_allow_authorization_header"] = True

        old_normalized: dict[str, object] = {}
        for key, value in raw.items():
            old_normalized[str(key).lower()] = value
        changed = headers != old_normalized

        if not headers.get("user-agent"):
            headers["user-agent"] = _DEFAULT_BROWSER_USER_AGENT
            changed = True

        referer = str(headers.get("referer", "") or "")
        if referer and not headers.get("origin"):
            try:
                parsed = urlparse(referer)
                if parsed.scheme and parsed.netloc:
                    headers["origin"] = f"{parsed.scheme}://{parsed.netloc}"
                    changed = True
            except (ValueError, TypeError):
                logger.debug(
                    "download_manager: origin derivation skipped for malformed referer"
                )

        task.headers = headers
        cookie_len = len(str(headers.get("cookie", "") or ""))
        logger.info(
            "[QUEUE] download header 摘要: "
            f"has_cookie={cookie_len > 0} cookie_len={cookie_len}",
            event="download_headers_summary",
            filename=getattr(task, "filename", ""),
            has_cookie=cookie_len > 0,
            cookie_len=cookie_len,
            cookie_policy_source=getattr(task, "cookie_policy_source", "") or "none",
            temporary_cookie_allowed=bool(getattr(task, "temporary_cookie_allowed", False)),
            header_count=len(headers),
        )
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
                best=redact_url(best_url),
                ranked=" | ".join([f"{redact_url(u)} ({s})" for u, s in candidates]),
            )
            task.url = best_url
        setattr(task, "candidate_scores", {u: s for u, s in candidates})

    def _record_metric(
        self,
        engine: str,
        stage: str,
        success: bool,
        *,
        reason: str = "",
        strategy: str = "",
        retry_action: str = "",
        probe_stage: str = "",
    ):
        """Update aggregated runtime metrics for observability."""
        engine = engine or "unknown"
        stage = stage or "unknown"
        reason = reason or ("completed" if success else stage) or "unknown"
        strategy = strategy or "none"
        retry_action = retry_action or "none"
        probe_stage = probe_stage or getattr(self, "_last_probe_stage", "") or "none"
        outcome_key = "success" if success else "failed"

        def bump(bucket: dict, key: str) -> None:
            if key not in bucket:
                bucket[key] = {"success": 0, "failed": 0}
            bucket[key][outcome_key] += 1

        with self._lock:
            if success:
                self._metrics["success_total"] += 1
            else:
                self._metrics["failed_total"] += 1

            bump(self._metrics["by_engine"], engine)
            bump(self._metrics["by_stage"], stage)
            bump(self._metrics["by_reason"], reason)
            bump(self._metrics["by_strategy"], strategy)
            bump(self._metrics["by_retry_action"], retry_action)
            bump(self._metrics["by_probe_stage"], probe_stage)

            snapshot = {
                "success_total": self._metrics["success_total"],
                "failed_total": self._metrics["failed_total"],
            }
        if not success and reason:
            logger.warning(
                f"[METRICS] fail_reason={reason}",
                event=f"fail_reason_{reason}",
                engine=engine,
                stage=stage,
                reason=reason,
                strategy=strategy,
                retry_action=retry_action,
                probe_stage=probe_stage,
            )
        logger.info(
            f"[METRICS] {TR('log_metrics_updated')}",
            event="download_metrics_snapshot",
            engine=engine,
            stage=stage,
            reason=reason,
            strategy=strategy,
            retry_action=retry_action,
            probe_stage=probe_stage,
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
        rule_name = f"auto:{host}"

        existing = None
        for rule in site_rules:
            if rule.get("name") == rule_name:
                existing = rule
                break

        rule_headers = {}
        if origin:
            rule_headers["origin"] = origin

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
            "enabled": True,
            "priority": -100,
            "domains": [host],
            "url_keywords": ["m3u8"],
            "apply_to": ["hls", "dash"],
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
                        if not self.task_queue:
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

                queued_entry = self.task_queue.pop_ready()
                if queued_entry is None:
                    continue
                task = queued_entry.task
                engine = queued_entry.engine
                user_specified = queued_entry.user_specified
                got_task = True
                logger.debug(
                    "下载 worker 取得任务",
                    event="download_worker_task_dequeued",
                    worker=threading.current_thread().name,
                    filename=getattr(task, "filename", ""),
                    status=getattr(task, "status", ""),
                    engine=getattr(task, "engine", ""),
                    user_specified=user_specified,
                )

                try:
                    logger.debug(
                        "下载 worker 执行任务开始",
                        event="download_worker_execute_start",
                        worker=threading.current_thread().name,
                        filename=getattr(task, "filename", ""),
                        status=getattr(task, "status", ""),
                        engine=getattr(task, "engine", ""),
                    )
                    self._execute_download(task, engine, user_specified)
                    logger.debug(
                        "下载 worker 执行任务返回",
                        event="download_worker_execute_done",
                        worker=threading.current_thread().name,
                        filename=getattr(task, "filename", ""),
                        status=getattr(task, "status", ""),
                        engine=getattr(task, "engine", ""),
                    )
                finally:
                    got_task = False
            except Exception as e:
                logger.error(f"{TR('log_worker_exception')}: {e}")
                if got_task:
                    logger.debug("download_manager: worker exception occurred with dequeued task")
            finally:
                if reserved_slot:
                    with self._worker_gate:
                        self._running_slots = max(0, self._running_slots - 1)
                        self._worker_gate.notify_all()

    def _execute_download(self, task: DownloadTask, engine: BaseEngine, user_specified: bool = False):
        """Execute one download task with retry/fallback."""
        from utils.config_manager import config

        logger.debug(
            "下载执行入口",
            event="download_execute_enter",
            worker=threading.current_thread().name,
            filename=getattr(task, "filename", ""),
            status=getattr(task, "status", ""),
            engine=getattr(task, "engine", ""),
            user_specified=user_specified,
        )
        if self._is_task_stop_requested(task):
            logger.info(f"[SKIP] {TR('log_skip_stopped')}: {task.filename}")
            return

        logger.debug(
            "下载任务状态切换 downloading 开始",
            event="download_execute_transition_start",
            worker=threading.current_thread().name,
            filename=getattr(task, "filename", ""),
            status=getattr(task, "status", ""),
        )
        task.transition("downloading")
        logger.debug(
            "下载任务状态切换 downloading 完成",
            event="download_execute_transition_done",
            worker=threading.current_thread().name,
            filename=getattr(task, "filename", ""),
            status=getattr(task, "status", ""),
        )
        task.started_at = datetime.now()
        # Normalize headers (lowercase keys, fill browser UA, derive
        # Origin) before any engine/probe consumes them — restores the
        # ISS-01 regression where this step was deleted.
        self._normalize_task_headers_for_download(task)
        task._set_fields_locked(retry_count=0)
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
        try:
            rate_limit_backoff_multiplier = int(
                features.get("download_rate_limit_backoff_multiplier", 3)
            )
        except (TypeError, ValueError):
            rate_limit_backoff_multiplier = 3
        rate_limit_backoff_multiplier = max(rate_limit_backoff_multiplier, 1)

        with self._lock:
            self._remove_task_from_state_lists(task)
            self.active_tasks.append(task)

        if self.on_task_update or self.on_task_snapshot:
            logger.debug(
                "下载任务 running UI 快照发送开始",
                event="download_execute_emit_running_start",
                worker=threading.current_thread().name,
                filename=getattr(task, "filename", ""),
                status=getattr(task, "status", ""),
                engine=getattr(task, "engine", ""),
            )
            self._emit_snapshot(task)
            logger.debug(
                "下载任务 running UI 快照发送完成",
                event="download_execute_emit_running_done",
                worker=threading.current_thread().name,
                filename=getattr(task, "filename", ""),
                status=getattr(task, "status", ""),
                engine=getattr(task, "engine", ""),
            )

        preflight_failure_kind = self._classify_failure(task, None)
        if preflight_failure_kind in {"drm", "expired"} and not self._is_task_stop_requested(task):
            if preflight_failure_kind == "drm":
                systems = ", ".join((getattr(task, "playlist_diagnostics", {}) or {}).get("drm_systems", [])[:3])
                task._set_fields_locked(
                    error_message=(
                        "Playlist diagnostics detected DRM"
                        + (f" ({systems})" if systems else "")
                    )
                )
            else:
                task._set_fields_locked(error_message="Playlist diagnostics detected expired signed URL")
            task.transition("failed")
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.failed_tasks.append(task)
            self._forget_idempotency_key(task)
            self._record_metric(
                task.engine,
                "preflight",
                False,
                reason=preflight_failure_kind,
                strategy="playlist_diagnostics",
                retry_action="none",
                probe_stage="playlist_diagnostics",
            )
            notify_download_failed(task.filename, task.error_message or preflight_failure_kind)
            logger.warning(
                "[PLAYLIST-DIAG] 任务因播放列表诊断被下载前拦截",
                event="download_playlist_diagnostics_preflight_blocked",
                engine=task.engine,
                url=task.url,
                failure_kind=preflight_failure_kind,
                is_drm=bool((getattr(task, "playlist_diagnostics", {}) or {}).get("is_drm")),
                ttl_warning=bool((getattr(task, "playlist_diagnostics", {}) or {}).get("ttl_warning")),
            )
            with self._lock:
                while task in self.active_tasks:
                    self.active_tasks.remove(task)
            if self.on_task_update or self.on_task_snapshot:
                self._emit_snapshot(task)
            return

        selector_context = self._build_task_engine_context(task)
        is_hls_task = (
            selector_context.resource_type == RESOURCE_TYPE_HLS
            or "mpegurl" in (selector_context.mime or "")
            or ".m3u8" in (task.url or "").lower()
        )

        if ranking_enabled and is_hls_task:
            self._rank_task_candidates(task)
            selector_context = self._build_task_engine_context(task)

        # Optional m3u8 preflight probe (playlist -> key -> segment)
        if hls_probe_enabled and is_hls_task:
            try:
                logger.debug(
                    "HLS 预探测开始",
                    event="download_hls_probe_start",
                    worker=threading.current_thread().name,
                    filename=getattr(task, "filename", ""),
                    url=getattr(task, "url", ""),
                )
                from core.services.hls_probe import HLSProbe

                probe_result = HLSProbe.probe(task.url, task.headers)
                diagnostics = []
                if isinstance(probe_result, dict):
                    raw_diagnostics = probe_result.get("stage_diagnostics", [])
                    if isinstance(raw_diagnostics, list):
                        diagnostics = [item for item in raw_diagnostics if isinstance(item, dict)]
                diagnostic_statuses = [
                    item.get("status_code")
                    for item in diagnostics
                    if item.get("status_code") is not None
                ]
                logger.debug(
                    "HLS 预探测返回",
                    event="download_hls_probe_done",
                    worker=threading.current_thread().name,
                    filename=getattr(task, "filename", ""),
                    ok=probe_result.get("ok") if isinstance(probe_result, dict) else None,
                    stage=probe_result.get("stage") if isinstance(probe_result, dict) else None,
                    diagnostic_count=len(diagnostics),
                    diagnostic_statuses=diagnostic_statuses,
                )
                probe_stage = probe_result.get("stage", "unknown")
                setattr(task, "probe_stage", probe_stage)
                setattr(task, "probe_result", probe_result)
                if probe_result.get("status_code") == 429 or any(status == 429 for status in diagnostic_statuses):
                    setattr(task, "rate_limit_retry_hint", True)

                if probe_result.get("ok"):
                    logger.info(
                        f"[HLS-PROBE] {TR('log_hls_probe_ok')}",
                        event="hls_probe_ok",
                        url=task.url,
                        stage=probe_stage,
                        playlist=probe_result.get("playlist_url"),
                        probe_action=probe_result.get("probe_action", ""),
                        range_retry=bool(probe_result.get("range_retry", False)),
                        diagnostic_count=len(diagnostics),
                        diagnostic_statuses=diagnostic_statuses,
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
                        probe_action=probe_result.get("probe_action", ""),
                        range_retry=bool(probe_result.get("range_retry", False)),
                        diagnostic_count=len(diagnostics),
                        diagnostic_statuses=diagnostic_statuses,
                    )
                else:
                    probe_error = probe_result.get("error", "unknown")
                    status_code = probe_result.get("status_code")
                    error_code = probe_result.get("error_code") or ""
                    task._set_fields_locked(
                        error_message=f"HLS probe failed at {probe_stage}: {probe_error}"
                    )
                    logger.warning(
                        f"[HLS-PROBE] {TR('log_hls_probe_failed')}",
                        event="hls_probe_failed",
                        url=task.url,
                        stage=probe_stage,
                        error=probe_error,
                        status_code=status_code,
                        probe_action=probe_result.get("probe_action", ""),
                        range_retry=bool(probe_result.get("range_retry", False)),
                        diagnostic_count=len(diagnostics),
                        diagnostic_statuses=diagnostic_statuses,
                    )

                    # Only security/policy failures should stop before the real
                    # engine runs. Playlist/key HTTP failures from hostile or
                    # rate-limited CDNs (403/429/5xx/timeouts) are often false
                    # negatives for this lightweight probe: N_m3u8DL-RE/ffmpeg
                    # may still succeed with their own retry cadence and request
                    # shape. Treat them as soft probe failures so the download
                    # engine gets the final say, improving both success rate and
                    # UI stability after repeated probe failures.
                    is_security_block = error_code == "ssrf_blocked" or str(probe_error).startswith("ssrf_blocked")
                    should_hard_fail = (
                        hls_probe_hard_fail
                        and probe_result.get("hard_fail", True)
                        and is_security_block
                        and not self._is_task_stop_requested(task)
                    )
                    if should_hard_fail:
                        task.transition("failed")
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
                            status_code=status_code,
                            probe_action=probe_result.get("probe_action", ""),
                            range_retry=bool(probe_result.get("range_retry", False)),
                        )
                        with self._lock:
                            while task in self.active_tasks:
                                self.active_tasks.remove(task)
                        if self.on_task_update or self.on_task_snapshot:
                            self._emit_snapshot(task)
                        return

                    setattr(task, "probe_warning", probe_error)
                    logger.warning(
                        "[HLS-PROBE] 预探测失败但允许实际下载引擎继续尝试",
                        event="hls_probe_failed_soft_allowed",
                        url=task.url,
                        stage=probe_stage,
                        status_code=status_code,
                        error=probe_error,
                        probe_action=probe_result.get("probe_action", ""),
                        range_retry=bool(probe_result.get("range_retry", False)),
                        diagnostic_count=len(diagnostics),
                        diagnostic_statuses=diagnostic_statuses,
                    )
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
                # R41 — once the main thread has removed this task,
                # every subsequent snapshot tries to resurrect a deleted
                # QTreeWidgetItem in the UI and crashes the native widget
                # layer. Silently drop the callback so the download
                # worker can wind down without collateral damage.
                #
                # Check BOTH ``status`` AND ``stop_reason``: the main
                # thread sets ``stop_reason="removed"`` *before* killing
                # the child process, but ``transition("removed")`` runs
                # *after* the kill.  During the kill window the worker's
                # ``read_loop`` drains its output queue and fires
                # ``progress_callback`` — at that moment ``status`` is
                # still ``"downloading"`` even though removal is already
                # in flight.  Both conditions independently gate the
                # snapshot so we catch the kill-window race.
                _removed = (
                    task._read_status() == "removed"
                    or getattr(task, "stop_reason", "") == "removed"
                )
                if _removed:
                    return

                if not isinstance(data, dict):
                    logger.debug(
                        "下载进度回调忽略非 dict 数据",
                        event="download_progress_ignored_invalid_payload",
                        filename=getattr(task, "filename", ""),
                        payload_type=type(data).__name__,
                    )
                    return

                (
                    progress_value,
                    speed_value,
                    downloaded_value,
                    attempt_value,
                    source_label_value,
                    previous_progress,
                ) = task.update_progress_locked(data)
                with task.lock:
                    current_status = task.status

                should_emit = self.on_task_update or self.on_task_snapshot
                if should_emit:
                    now = time.monotonic()
                    should_emit = self._should_emit_progress_snapshot(
                        task,
                        progress=progress_value,
                        speed=speed_value,
                        downloaded=downloaded_value,
                        status=current_status,
                        attempt=attempt_value,
                        source_label=source_label_value,
                        now=now,
                    )
                if should_emit:
                    logger.debug(
                        "下载进度 UI 快照发送",
                        event="download_progress_emit",
                        filename=getattr(task, "filename", ""),
                        status=current_status,
                        progress=progress_value,
                        previous_progress=previous_progress,
                        attempt=attempt_value,
                        source_label=source_label_value,
                        speed_present=bool(speed_value),
                        downloaded_present=bool(downloaded_value),
                    )
                    self._emit_snapshot(task)
                    self._record_progress_snapshot_emit(
                        task,
                        progress=progress_value,
                        speed=speed_value,
                        downloaded=downloaded_value,
                        status=current_status,
                        attempt=attempt_value,
                        source_label=source_label_value,
                        now=now,
                    )
            except Exception as e:
                logger.debug(f"{TR('log_progress_update_exception')}: {e}")

        def _try_download(selected_engine: BaseEngine, engine_name: str) -> bool:
            if self._is_task_stop_requested(task):
                return False
            try:
                task._set_fields_locked(engine=engine_name)
                ok = selected_engine.download(task, progress_callback)
                if ok and engine_name.lower() == "aria2":
                    valid, reason = self._validate_aria2_artifact(task)
                    if not valid:
                        task._set_fields_locked(error_message=reason)
                        logger.warning(
                            f"[Aria2] 产物校验失败：{reason}",
                            event="aria2_artifact_validation_failed",
                            filename=getattr(task, "filename", ""),
                            artifact_name=getattr(
                                task, self._ARIA2_OUTPUT_ATTR, ""
                            ),
                            reason=reason,
                            resource_type=str(
                                getattr(task, "resource_type", "")
                            ),
                            next_action="fallback",
                        )
                        return False
                return ok
            except Exception as e:
                task._set_fields_locked(error_message=str(e))
                logger.error(
                    f"[FAILED] {TR('log_task_exception')}: {task.filename} - {e}",
                    event="download_engine_exception",
                    engine=engine_name,
                    url=task.url,
                    stage="engine_invoke",
                )
                return False

        selector_context = self._build_task_engine_context(task)
        candidates = self.selector.get_candidates(task.url, context=selector_context)
        if user_specified:
            preferred = self.selector.get_engine_by_name(task.engine)
            if preferred:
                auto_candidates = [
                    (candidate_engine, candidate_name)
                    for candidate_engine, candidate_name in self.selector.get_candidates(
                        task.url,
                        context=selector_context,
                    )
                    if candidate_engine != preferred
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
                    failure_kind=last_failure_kind,
                    probe_stage=getattr(task, "probe_stage", "") or "none",
                )

                if last_failure_kind == "auth":
                    auth_headers_changed = self._apply_site_rules_to_task(task)
                    if auth_retry_first and auth_retry_per_engine > 0 and auth_headers_changed:
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
                                logger.info(
                                    "[AUTH-RETRY] 授权 headers 更新后同引擎重试成功",
                                    event="auth_retry_success",
                                    engine=candidate_name,
                                    url=task.url,
                                    stage="auth",
                                    retry_index=auth_try + 1,
                                )
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

                    if not auth_headers_changed:
                        logger.info(
                            "[AUTH-RETRY] 站点规则未补充新鉴权 headers，跳过同引擎认证重试",
                            event="download_auth_retry_skipped",
                            engine=candidate_name,
                            url=task.url,
                            reason="headers_unchanged",
                        )

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
                            retry_action="engine_fallback",
                        )
                        user_specified = False
                        continue
                    if last_failure_kind == "parse" and fallback_enabled:
                        continue

            if not success:
                if self._is_task_stop_requested(task):
                    break
                if last_failure_kind in {"drm", "expired"}:
                    logger.info(
                        "[RETRY] 当前失败类型不适合重复重试，已跳过后续 retry",
                        event="download_retry_skipped",
                        engine=task.engine,
                        url=task.url,
                        failure_kind=last_failure_kind,
                        stage=last_failure_stage,
                    )
                    break
                # ISS-29: retry_count is a volatile task field; update it
                # under task.lock instead of using an unlocked ``+=``.
                with task.lock:
                    next_retry_count = task.retry_count + 1
                    task._set_fields_locked(retry_count=next_retry_count)
                if next_retry_count > task.max_retries or not retry_enabled:
                    break

                effective_backoff = backoff_seconds
                if last_failure_kind == "rate_limit":
                    effective_backoff = max(
                        backoff_seconds
                        * rate_limit_backoff_multiplier
                        * (2 ** (next_retry_count - 1)),
                        backoff_seconds,
                    )
                    setattr(task, "rate_limit_retry_hint", True)
                    logger.warning(
                        "[RETRY] 检测到 429/限流，使用更长退避后重试",
                        event="download_rate_limit_backoff",
                        engine=task.engine,
                        url=task.url,
                        retry_count=next_retry_count,
                        backoff_seconds=effective_backoff,
                        rate_limit_retry_hint=True,
                    )
                elif last_failure_kind == "timeout":
                    effective_backoff = max(
                        backoff_seconds * (2 ** (next_retry_count - 1)),
                        backoff_seconds,
                    )

                task.transition("waiting")
                if self.on_task_update or self.on_task_snapshot:
                    self._emit_snapshot(task)
                if effective_backoff > 0:
                    self._stop_flag.wait(timeout=effective_backoff)
                # Backoff elapsed — re-enter the downloading state so the
                # next retry attempt can complete via downloading→completed.
                if not self._is_task_stop_requested(task):
                    task.transition("downloading")
                    if self.on_task_update or self.on_task_snapshot:
                        self._emit_snapshot(task)

        stop_reason = getattr(task, "stop_reason", "")
        # R41 — when the main thread's ``remove_task`` has already
        # transitioned this task to ``removed``, the worker MUST NOT
        # repeat the cleanup steps (they are already done on the main
        # thread) so we don't double-remove from the state lists or
        # double-clear the idempotency key. Just log and skip.
        _already_removed_by_main = (task._read_status() == "removed")
        if stop_reason == "removed":
            if not _already_removed_by_main:
                task._set_fields_locked(process=None)
                # R41: transition("removed") may raise InvalidTransition
                # when the main thread's remove_task() beat us here
                # (it blocks on taskkill while we unwind the read_loop).
                # Only proceed with state-list cleanup when *we* own the
                # transition — otherwise the main thread already did it.
                try:
                    task.transition("removed", reason="removed")
                except InvalidTransition:
                    logger.debug(
                        f"worker: 主线程已先一步移除任务，跳过重复 transition: {task.filename}",
                    )
                else:
                    with self._lock:
                        self._remove_task_from_state_lists(task)
                    # R34.3 / tasks.md 28.1 — terminal state: drop
                    # idempotency key so a future re-add of the same
                    # (url, engine, out_dir, title) combination enqueues
                    # freshly instead of merging into the now-removed record.
                    self._forget_idempotency_key(task)
            logger.info(f"[REMOVED] {TR('log_task_removed')}: {task.filename}")
        elif success:
            task.transition("completed")
            task._set_fields_locked(progress=100.0)
            task.completed_at = datetime.now()
            task._set_fields_locked(process=None)
            self._learn_site_rule_from_task(task)
            self._record_metric(
                task.engine,
                "completed",
                True,
                reason="completed",
                strategy="fallback" if recovered_from_fallback else "primary",
                retry_action="engine_fallback" if recovered_from_fallback else "none",
                probe_stage=getattr(task, "probe_stage", "") or "none",
            )
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.completed_tasks.append(task)
            # R34.3 — terminal state; see note above.
            self._forget_idempotency_key(task)
            notify_download_completed(task.filename)
            logger.info(f"[OK] {TR('log_task_completed')}: {task.filename}")
            # F-10: auto_delete_temp 接管。当配置开启且引擎为产生
            # temp 分段的引擎（N_m3u8DL-RE/ffmpeg）时，清理该引擎
            # scratch 子目录中的分段残留，但跳过其它仍在运行/暂停
            # 任务的同名产物，避免误删并行任务。
            self._maybe_auto_delete_temp(task)
            if recovered_from_fallback:
                logger.warning(
                    f"[FALLBACK-RECOVERED] 用户指定引擎失败后已成功回退并完成: {task.filename}",
                    event="download_fallback_recovered",
                    engine=recovered_from_engine_name,
                    url=task.url,
                    original_engine=engine.get_name(),
                    failure_kind=last_failure_kind,
                    stage=last_failure_stage,
                    retry_action="engine_fallback",
                )
        else:
            task._set_fields_locked(process=None)
            if stop_reason == "paused":
                task.transition("paused", reason="paused")
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
                task.transition("failed", reason="cancelled")
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                # R34.3 — terminal state: key cleared so retry works.
                self._forget_idempotency_key(task)
                self._record_metric(
                    task.engine,
                    "cancelled",
                    False,
                    reason="cancelled",
                    strategy="stop_reason",
                    retry_action="none",
                    probe_stage=getattr(task, "probe_stage", "") or "none",
                )
                logger.info(f"[CANCELLED] 任务已取消: {task.filename}")
            elif stop_reason == "shutdown":
                task.transition("failed", reason="shutdown")
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                self._record_metric(
                    task.engine,
                    "shutdown",
                    False,
                    reason="shutdown",
                    strategy="stop_reason",
                    retry_action="none",
                    probe_stage=getattr(task, "probe_stage", "") or "none",
                )
                # Application is terminating; the manager instance is
                # being torn down too, so the key map will go with it.
                # Drop the entry for symmetry and so any lingering
                # reference (e.g. a snapshot emitter) observes a clean
                # table.
                self._forget_idempotency_key(task)
                logger.info(f"[STOP] 应用关闭，终止任务: {task.filename}")
            else:
                task.transition("failed")
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                # R34.3 — terminal state; see note above.
                self._forget_idempotency_key(task)
                self._record_metric(
                    task.engine,
                    last_failure_stage,
                    False,
                    reason=last_failure_kind,
                    strategy="final_failure",
                    retry_action=(
                        "rate_limit_backoff"
                        if last_failure_kind == "rate_limit"
                        else "auth_retry"
                        if last_failure_kind == "auth"
                        else "none"
                    ),
                    probe_stage=getattr(task, "probe_stage", "") or "none",
                )
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
        # R41 — when the main thread's ``remove_task`` has already
        # transitioned this task to ``removed`` and torn down the
        # QTreeWidgetItem, emitting a late snapshot via Qt
        # QueuedConnection will resurrect a deleted item in
        # ``DownloadQueuePanel.add_or_update_task`` and crash the
        # native widget layer. Skip the snapshot entirely when the
        # main thread already handled removal.
        #
        # The kill-window race means ``status`` can still be
        # ``"downloading"`` while ``stop_reason`` is already
        # ``"removed"``.  Check both.
        if self.on_task_update or self.on_task_snapshot:
            _removed = (
                task._read_status() == "removed"
                or getattr(task, "stop_reason", "") == "removed"
            )
            if not _removed:
                self._emit_snapshot(task)
        if task._read_status() in _PROGRESS_EMIT_TERMINAL_STATUSES:
            self._forget_progress_emit_state(task)

    def pause_task(self, task: DownloadTask):
        """Pause task."""
        task._set_fields_locked(
            stop_requested=True,
            stop_reason="paused",
            error_message="用户暂停",
        )
        removed_from_queue = self._remove_task_from_queue(task)
        if removed_from_queue > 0:
            logger.info(f"任务已从等待队列移除并暂停: {task.filename}")
        if task.process:
            try:
                self._kill_process_tree(
                    task.process,
                    expected_name=getattr(task, "_expected_engine_name", None),
                )
                task.transition("paused", reason="paused")
                logger.info(f"任务已暂停: {task.filename}")
            except Exception as e:
                logger.error(f"暂停任务失败: {e}")
        if task.status in {"waiting", "paused", "downloading"}:
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.paused_tasks.append(task)
            task.transition("paused", reason="paused")
        elif task.status == "failed":
            # ISS-25: pausing an already-failed non-active task must not make
            # it disappear from all state lists. ``failed -> paused`` is not a
            # legal transition, so preserve the failed terminal bucket.
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.failed_tasks.append(task)
        if self.on_task_update or self.on_task_snapshot:
            self._emit_snapshot(task)
        if task.status in _PROGRESS_EMIT_TERMINAL_STATUSES:
            self._forget_progress_emit_state(task)

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
                    poll = getattr(process, "poll", None)
                    process_alive = callable(poll) and poll() is None
                except Exception as e:
                    # ISS-27: a broken/stale process handle should not block
                    # resume forever. Treat it as not alive and clear it.
                    logger.debug(f"resume_task: stale process handle ignored: {e}")
                    task._set_fields_locked(process=None)
                    process_alive = False

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
        task._set_fields_locked(
            stop_requested=True,
            stop_reason="cancelled",
            error_message="用户取消",
        )
        task.transition("failed", reason="cancelled")
        removed_from_queue = self._remove_task_from_queue(task)
        if removed_from_queue > 0:
            logger.info(f"任务已从等待队列移除并取消: {task.filename}")
        if task.process:
            try:
                self._kill_process_tree(
                    task.process,
                    expected_name=getattr(task, "_expected_engine_name", None),
                )
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
        if task.status in _PROGRESS_EMIT_TERMINAL_STATUSES:
            self._forget_progress_emit_state(task)

    def remove_task(self, task: DownloadTask):
        """Remove task from manager."""
        task._set_fields_locked(
            stop_requested=True,
            stop_reason="removed",
            error_message="用户删除任务",
        )
        if task.process:
            try:
                self._kill_process_tree(
                    task.process,
                    expected_name=getattr(task, "_expected_engine_name", None),
                )
            except Exception as e:
                logger.error(f"删除任务时终止进程失败: {e}")
        # R41: _kill_process_tree blocks on subprocess.run(taskkill).
        # While it waits, the worker thread's read_loop detects the
        # process death, unwinds through _execute_download's terminal
        # section, and independently transitions the task to "removed".
        # A duplicate transition("removed") raises InvalidTransition
        # on the main thread — and in PyQt6, unhandled Python exceptions
        # in Qt-connected slots propagate to Qt's event loop and cause
        # STATUS_FAIL_FAST_EXCEPTION (0xc0000409) in Qt6Core.dll.
        try:
            task.transition("removed", reason="removed")
        except InvalidTransition:
            logger.debug(f"任务已由 worker 先一步移除，跳过重复 transition: {task.filename}")
        self._remove_task_from_queue(task)
        with self._lock:
            self._remove_task_from_state_lists(task)
        # R34.3 — dropped from all state lists; drop the idempotency
        # entry too so the same (url, engine, out_dir, title) can be
        # re-added later as a fresh task.
        self._forget_idempotency_key(task)
        self._forget_progress_emit_state(task)
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
                "download_manager: psutil tree kill failed "
                f"({type(psutil_exc).__name__}); falling back to Popen.kill()",
            )
            try:
                process.kill()
            except OSError as kill_exc:
                # Process already exited or handle invalid — nothing to do.
                logger.debug(
                    "download_manager: fallback Popen.kill() skipped "
                    f"({type(kill_exc).__name__})",
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
        """Return aggregated success/failure metrics by engine, stage, reason and strategy."""

        def clone_bucket(bucket: dict) -> dict:
            return {key: dict(value) for key, value in bucket.items()}

        with self._lock:
            return {
                "success_total": self._metrics["success_total"],
                "failed_total": self._metrics["failed_total"],
                "by_engine": clone_bucket(self._metrics["by_engine"]),
                "by_stage": clone_bucket(self._metrics["by_stage"]),
                "by_reason": clone_bucket(self._metrics["by_reason"]),
                "by_strategy": clone_bucket(self._metrics["by_strategy"]),
                "by_retry_action": clone_bucket(self._metrics["by_retry_action"]),
                "by_probe_stage": clone_bucket(self._metrics["by_probe_stage"]),
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
            task._set_fields_locked(
                stop_requested=True,
                stop_reason="shutdown",
            )
            if task.process:
                try:
                    self._kill_process_tree(
                        task.process,
                        expected_name=getattr(task, "_expected_engine_name", None),
                    )
                except OSError as exc:
                    # Process may already be gone; shutdown path continues.
                    logger.debug(
                        f"download_manager: shutdown kill skipped ({type(exc).__name__})",
                    )

        # Drain waiting queue to avoid lingering pending tasks.
        drained_tasks = [entry.task for entry in self.task_queue.clear()]
        self._notify_workers()

        for task in drained_tasks:
            task._set_fields_locked(
                stop_requested=True,
                stop_reason="shutdown",
            )
            if task.status == "waiting":
                task.transition("failed", reason="shutdown")
            with self._lock:
                self._remove_task_from_state_lists(task)

        self._worker_pool.join_all(timeout=3.0)

        logger.info(TR("log_dl_mgr_closed"))
