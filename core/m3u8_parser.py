"""
M3U8 Parser Utility
Handles fetching and parsing of M3U8 master playlists to extract quality variants.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from utils.config_manager import config
from utils.errors import StructuredError
from utils.logger import logger
from utils.redact import redact_url
from utils.retry import BACKOFF, interruptible_sleep
from utils.ssrf_guard import SSRFBlocked, ensure_public


# HTTP status families the R15 backoff loop treats as recoverable.
_RECOVERABLE_STATUSES: frozenset = frozenset({429, 500, 502, 503, 504})

# 4xx statuses that trigger the R15.4 one-shot "smart Referer" fallback.
_AUTH_LIKE_STATUSES: frozenset = frozenset({401, 403})


# ---------------------------------------------------------------------------
# R16 — Nested variant fan-out budget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NestedBudget:
    """Static limits for nested master-playlist resolution (R16.2/16.5).

    A malicious or misbehaving master playlist can advertise thousands of
    nested sub-masters. Without a budget the recursive resolver would
    issue unbounded HTTP requests and block the UI thread. These
    defaults mirror the design document:

    * ``max_depth``    — at most N levels of master-in-master nesting.
    * ``per_level``    — at most N variants processed within one level.
    * ``total``        — at most N variants processed across all levels.
    * ``wallclock_s``  — total wall-clock budget, enforced via
                         ``time.monotonic`` so a long-running resolve
                         is truncated rather than stalling forever.
    """

    max_depth: int = 3
    per_level: int = 16
    total: int = 64
    wallclock_s: float = 30.0


class NestedBudgetState:
    """Mutable running state paired with a :class:`NestedBudget`.

    Shared across recursive ``_resolve_nested_variants`` frames so the
    totals reflect the whole traversal, not just a single level.

    ``exceeded_reason`` records the *first* limit that was hit. Later
    callers consult this to decide whether to attach a structured
    ``variants_truncated`` error to the outer frame (R16.3).
    """

    __slots__ = (
        "budget",
        "total_processed",
        "start_monotonic",
        "per_level_counts",
        "exceeded_reason",
    )

    def __init__(self, budget: NestedBudget | None = None) -> None:
        self.budget = budget or NestedBudget()
        self.total_processed: int = 0
        self.start_monotonic: float = time.monotonic()
        self.per_level_counts: dict[int, int] = {}
        # One of: None | "depth" | "per_level" | "total" | "wallclock".
        self.exceeded_reason: str | None = None

    def check(self, depth: int) -> bool:
        """Return True iff there is budget for one more variant at ``depth``.

        Evaluation order is intentional: depth > total > per_level >
        wallclock. The first-hit reason is frozen into
        ``exceeded_reason`` so the outer frame can report a single,
        deterministic truncation cause.
        """

        b = self.budget
        if depth >= b.max_depth:
            if self.exceeded_reason is None:
                self.exceeded_reason = "depth"
            return False
        if self.total_processed >= b.total:
            if self.exceeded_reason is None:
                self.exceeded_reason = "total"
            return False
        if self.per_level_counts.get(depth, 0) >= b.per_level:
            if self.exceeded_reason is None:
                self.exceeded_reason = "per_level"
            return False
        if (time.monotonic() - self.start_monotonic) >= b.wallclock_s:
            if self.exceeded_reason is None:
                self.exceeded_reason = "wallclock"
            return False
        return True

    def record(self, depth: int) -> None:
        """Mark one variant as processed at ``depth``."""
        self.total_processed += 1
        self.per_level_counts[depth] = self.per_level_counts.get(depth, 0) + 1


class M3U8FetchThread(QThread):
    """Background thread to fetch and parse M3U8 playlist.

    Network behaviour follows Requirement 15 (security-stability-hardening):

    * Recoverable errors (connection/read timeouts, 429, 5xx, connection
      resets) are retried up to ``len(BACKOFF)`` times with ±20 % jitter.
    * Sleeps between retries use :func:`utils.retry.interruptible_sleep`
      so ``stop_event`` is observed within 100 ms (R15.3 / R15.5).
    * A single free "smart Referer" retry on ``attempt == 0`` for a 401/403
      response that appears to lack ``Referer`` (R15.4); this retry does
      NOT count against the backoff schedule.
    * Every attempt re-runs :func:`utils.ssrf_guard.ensure_public` as
      defence-in-depth against DNS rebinding between retries.

    On success ``finished`` still emits the variant list as before.
    On failure it emits ``[]`` for backwards compatibility and ALSO emits
    ``error_occurred`` carrying the :class:`StructuredError`; callers that
    care about the failure reason can connect that signal.
    """

    finished = pyqtSignal(list)
    # New optional signal (R15.2 / R18): carries the StructuredError so
    # the UI can distinguish between cancelled / fetch_failed / SSRF-blocked
    # without having to scrape log lines. Existing listeners ignore it.
    error_occurred = pyqtSignal(object)

    def __init__(
        self,
        url: str,
        headers: dict = None,
        *,
        stop_event: Optional[threading.Event] = None,
    ):
        super().__init__()
        self.url = url
        self.headers = headers or {}
        self._last_response_info: dict = {}
        # Task spec: ``stop_event`` default None but caller may set it.
        # We fall back to a private, never-set Event so the retry loop
        # always has a real object to check and ``request_stop`` is a
        # safe no-op before a caller wires one in.
        self.stop_event: threading.Event = stop_event if stop_event is not None else threading.Event()
        self.last_error: StructuredError | None = None
        feature_flags = config.get("features", {}) or {}
        self._max_nested_depth = max(1, min(5, int(feature_flags.get("m3u8_nested_depth", 3))))
        self._verify_tls = bool(feature_flags.get("network_verify_tls", True))
        self._tls_warning_emitted = False

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """Request cancellation of the current fetch.

        Safe to call from any thread. The retry loop observes this
        within ~100 ms via :func:`interruptible_sleep` (R15.3).
        """
        if self.stop_event is not None:
            self.stop_event.set()

    # ------------------------------------------------------------------
    # Main thread entry point
    # ------------------------------------------------------------------

    def run(self):
        try:
            logger.info(f"Analyzing M3U8 playlist: {self.url}")

            # R4 SSRF filter: reject non-public target before any network I/O.
            try:
                ensure_public(self.url)
            except SSRFBlocked as exc:
                logger.warning(
                    f"[SSRF] playlist fetch blocked: {exc.reason}",
                    event="m3u8_ssrf_blocked",
                    stage="ssrf",
                    reason=exc.reason,
                    url=redact_url(self.url),
                )
                self._emit_structured_failure(
                    StructuredError(
                        code="ssrf_blocked",
                        reason=exc.reason,
                        details={"url": redact_url(self.url)},
                        stage="ssrf",
                    )
                )
                return

            # R15.5: if the caller already requested stop, do not start.
            if self.stop_event.is_set():
                self._emit_structured_failure(
                    StructuredError(
                        code="cancelled",
                        reason="stop_event set before start",
                        details={"url": redact_url(self.url)},
                        stage="network",
                    )
                )
                return

            headers = self.headers.copy()
            if "User-Agent" not in headers and "user-agent" not in headers:
                headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )

            fetched = self._fetch_with_retry(self.url, headers)
            if isinstance(fetched, StructuredError):
                self._emit_structured_failure(fetched)
                return
            content = fetched
            logger.info(f"M3U8 Content Sample (First 500 chars):\n{content[:500]}")

            # Some sites return a plain URL body as pseudo redirect.
            if content.strip().startswith("http") and "#EXTM3U" not in content:
                redirect_url = content.strip()
                logger.info(f"Detected URL in response body, following pseudo-redirect to: {redirect_url}")
                # R4 SSRF filter: re-check the redirect target before following it.
                try:
                    ensure_public(redirect_url)
                except SSRFBlocked as exc:
                    logger.warning(
                        f"[SSRF] pseudo-redirect blocked: {exc.reason}",
                        event="m3u8_pseudo_redirect_ssrf_blocked",
                        stage="ssrf",
                        reason=exc.reason,
                        url=redact_url(redirect_url),
                    )
                    # Treat as if the redirect were absent; keep the
                    # already-fetched playlist content and original URL.
                else:
                    # R15 reuse: follow the single pseudo-redirect hop
                    # through the same retry + cancel + smart-Referer
                    # pipeline used for the initial fetch, so transient
                    # 429/5xx on the redirect target are handled
                    # identically and ``stop_event`` is honoured.
                    followed = self._fetch_with_retry(redirect_url, headers)
                    if isinstance(followed, StructuredError):
                        logger.warning(
                            "Failed to follow pseudo-redirect; keeping original content",
                            event="m3u8_pseudo_redirect_failed",
                            stage="fetch_redirect",
                            code=followed.code,
                            reason=followed.reason,
                            url=redirect_url,
                        )
                    else:
                        content = followed
                        self.url = redirect_url
                        logger.info(f"New M3U8 Content Sample (First 500 chars):\n{content[:500]}")

            if not self._is_master_playlist(content):
                logger.info("Detected media playlist (no variants in master playlist)")
                self.finished.emit([])
                return

            variants = self._parse_m3u8_variants(content, self.url)
            if variants:
                budget_state = NestedBudgetState()
                variants = self._resolve_nested_variants(
                    variants,
                    headers,
                    depth=0,
                    visited={self.url},
                    budget_state=budget_state,
                )
                if budget_state.exceeded_reason is not None:
                    truncation = StructuredError(
                        code="variants_truncated",
                        reason=f"nested_budget_{budget_state.exceeded_reason}",
                        details={
                            "limit": budget_state.exceeded_reason,
                            "max_depth": budget_state.budget.max_depth,
                            "per_level": budget_state.budget.per_level,
                            "total": budget_state.budget.total,
                            "wallclock_s": budget_state.budget.wallclock_s,
                            "total_processed": budget_state.total_processed,
                            "elapsed_s": round(
                                time.monotonic() - budget_state.start_monotonic, 3
                            ),
                            "kept_variants": len(variants),
                            "url": redact_url(self.url),
                        },
                        stage="manifest",
                    )
                    # Expose to callers that care (e.g. UI) without
                    # failing the overall fetch: the variants we did
                    # resolve are still valid and useful.
                    self.last_error = truncation
                    logger.warning(
                        "[M3U8] nested variant budget exceeded; returning truncated list",
                        event="m3u8_variants_truncated",
                        stage="manifest",
                        reason=truncation.reason,
                        limit=budget_state.exceeded_reason,
                        total_processed=budget_state.total_processed,
                        elapsed_s=round(
                            time.monotonic() - budget_state.start_monotonic, 3
                        ),
                        url=redact_url(self.url),
                    )
                logger.info(f"Found {len(variants)} variants in M3U8")
            else:
                logger.info("No variants found in M3U8 (master playlist empty)")

            self.finished.emit(variants)

        except SSRFBlocked as exc:
            logger.warning(
                f"[SSRF] fetch aborted mid-retry: {exc.reason}",
                event="m3u8_ssrf_blocked_mid_retry",
                stage="ssrf",
                reason=exc.reason,
                url=redact_url(self.url),
            )
            self._emit_structured_failure(
                StructuredError(
                    code="ssrf_blocked",
                    reason=exc.reason,
                    details={"url": redact_url(self.url)},
                    stage="ssrf",
                )
            )
        except Exception as e:
            if self._last_response_info:
                logger.error(
                    f"Failed to parse M3U8: {e} | "
                    f"status={self._last_response_info.get('status_code')} "
                    f"url={self._last_response_info.get('url')}",
                    event="m3u8_parse_failed",
                    stage="run",
                    error_type=type(e).__name__,
                )
            else:
                logger.error(
                    f"Failed to parse M3U8: {e}",
                    event="m3u8_parse_failed",
                    stage="run",
                    error_type=type(e).__name__,
                )
            self._emit_structured_failure(
                StructuredError(
                    code="fetch_failed",
                    reason=f"unhandled_{type(e).__name__}",
                    details={"url": redact_url(self.url), "last_error": repr(e)},
                    stage="network",
                )
            )

    # ------------------------------------------------------------------
    # Single-shot fetch (used by retry loop and nested resolver)
    # ------------------------------------------------------------------

    def _fetch_once(self, url: str, headers: dict) -> str:
        if not self._verify_tls and not self._tls_warning_emitted:
            logger.warning("[M3U8] TLS verification disabled by config")
            self._tls_warning_emitted = True
        # (connect, read) split timeout per design 3.1. Kept conservative
        # so a slow TLS handshake does not starve the 0.5s backoff budget.
        response = requests.get(url, headers=headers, timeout=(5, 15), verify=self._verify_tls)
        response.raise_for_status()
        self._last_response_info = {
            "status_code": getattr(response, "status_code", None),
            "url": getattr(response, "url", url),
            "headers": dict(getattr(response, "headers", {}) or {}),
        }
        return response.text

    # ------------------------------------------------------------------
    # R15 retry / backoff / cancel / smart-Referer
    # ------------------------------------------------------------------

    def _fetch_with_retry(self, url: str, headers: dict) -> Any:
        """Fetch ``url`` with R15 backoff + cancel + smart-Referer.

        Returns:
            * ``str`` on success (the playlist body).
            * :class:`StructuredError` on cancellation or exhausted retries
              (callers should branch via ``isinstance(result, StructuredError)``).

        Raises:
            :class:`SSRFBlocked` if a per-attempt re-check of the target
            host fails. The outer ``run`` handler converts this into a
            ``stage=ssrf`` StructuredError; the exception must not be
            silently swallowed.
        """

        working_headers: dict = dict(headers)
        referer_fallback_used = False
        last_error_repr: str = ""
        last_status_code: int | None = None

        attempt = 0
        max_retries = len(BACKOFF)  # 3 → initial + 3 retries = 4 attempts

        while attempt <= max_retries:
            # R15.5: cancellation observed before every new HTTP request.
            if self.stop_event.is_set():
                return StructuredError(
                    code="cancelled",
                    reason="stop_event set before attempt",
                    details={"url": redact_url(url), "attempts": attempt},
                    stage="network",
                )

            # Defence in depth against DNS rebind between retries.
            ensure_public(url)

            try:
                return self._fetch_once(url, working_headers)

            except requests.HTTPError as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                last_error_repr = repr(e)
                last_status_code = status_code

                # R15.4 smart Referer fallback: only on attempt 0, only
                # once, and only if no Referer is already present.
                if (
                    status_code in _AUTH_LIKE_STATUSES
                    and attempt == 0
                    and not referer_fallback_used
                    and not self._headers_has_key(working_headers, "referer")
                ):
                    referer_fallback_used = True
                    self._inject_smart_referer(working_headers, url)
                    logger.info(
                        "M3U8 attempt 0 4xx with missing Referer; auto-adding and retrying once",
                        event="m3u8_smart_referer_retry",
                        stage="fetch_playlist",
                        status_code=status_code,
                        url=redact_url(url),
                    )
                    # Deliberately do NOT increment ``attempt`` — this
                    # free retry is independent of the backoff schedule.
                    continue

                if status_code in _RECOVERABLE_STATUSES:
                    # fall through to the backoff branch below
                    pass
                else:
                    # Non-recoverable 4xx (not 401/403, or already
                    # auto-patched). Fail fast per R15.2.
                    return StructuredError(
                        code="fetch_failed",
                        reason=f"non_recoverable_http_{status_code}",
                        details={
                            "url": redact_url(url),
                            "status_code": status_code,
                            "attempts": attempt + 1,
                            "last_error": last_error_repr,
                        },
                        stage="network",
                    )

            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as e:
                # Covers connection/read timeouts AND connection resets.
                last_error_repr = repr(e)

            except SSRFBlocked:
                # Never retried; re-raise so the outer handler can tag
                # the failure with ``stage=ssrf``.
                raise

            except Exception as e:
                # Truly unexpected error: surface as fetch_failed rather
                # than quietly retrying something we do not understand.
                return StructuredError(
                    code="fetch_failed",
                    reason=f"unexpected_{type(e).__name__}",
                    details={
                        "url": redact_url(url),
                        "attempts": attempt + 1,
                        "last_error": repr(e),
                    },
                    stage="network",
                )

            # --- Recoverable-failure path: backoff or give up ---------
            if attempt >= max_retries:
                return StructuredError(
                    code="fetch_failed",
                    reason="max_retries_exhausted",
                    details={
                        "url": redact_url(url),
                        "attempts": attempt + 1,
                        "status_code": last_status_code,
                        "last_error": last_error_repr,
                    },
                    stage="network",
                )

            delay = BACKOFF[attempt] * random.uniform(0.8, 1.2)
            logger.warning(
                "[M3U8] fetch recoverable failure; backing off",
                event="m3u8_fetch_backoff",
                stage="fetch_playlist",
                attempt=attempt + 1,
                delay_s=round(delay, 3),
                status_code=last_status_code,
                url=redact_url(url),
            )
            if not interruptible_sleep(delay, self.stop_event):
                return StructuredError(
                    code="cancelled",
                    reason="stop_event set during backoff",
                    details={"url": redact_url(url), "attempts": attempt + 1},
                    stage="network",
                )
            attempt += 1

        # Should be unreachable (the loop always returns) but keep a
        # defensive fallback to satisfy static analysers.
        return StructuredError(
            code="fetch_failed",
            reason="loop_exhausted",
            details={
                "url": redact_url(url),
                "attempts": attempt,
                "last_error": last_error_repr,
            },
            stage="network",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _headers_has_key(headers: dict, name: str) -> bool:
        """Case-insensitive header presence check."""
        target = name.lower()
        for k in headers:
            if isinstance(k, str) and k.lower() == target:
                return True
        return False

    @staticmethod
    def _inject_smart_referer(headers: dict, url: str) -> None:
        """Populate Referer (and Origin when derivable) for the free retry."""
        headers["Referer"] = url
        # Only add Origin if the URL has a recognisable scheme+netloc and
        # the caller has not already provided one.
        has_origin = False
        for k in headers:
            if isinstance(k, str) and k.lower() == "origin":
                has_origin = True
                break
        if has_origin:
            return
        try:
            parsed = urlparse(url)
            if parsed.scheme and parsed.netloc:
                headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        except Exception as parse_err:
            logger.debug(
                f"[M3U8] origin parse failed: {parse_err}",
                event="m3u8_origin_parse_failed",
                stage="fetch_playlist",
                error_type=type(parse_err).__name__,
            )

    def _emit_structured_failure(self, err: StructuredError) -> None:
        """Record + emit a structured failure.

        Backwards compatible: ``finished`` still fires with ``[]`` so
        existing listeners keep working. The new ``error_occurred``
        signal carries the :class:`StructuredError` for callers that
        need to distinguish between cancellation, SSRF block, and
        transient network failure.
        """
        self.last_error = err
        logger.warning(
            f"[M3U8] fetch failed: {err.code} reason={err.reason}",
            event="m3u8_fetch_structured_error",
            stage=err.stage,
            code=err.code,
            reason=err.reason,
        )
        try:
            self.error_occurred.emit(err)
        except Exception as emit_err:  # pragma: no cover - defensive
            logger.debug(
                f"[M3U8] error_occurred emit failed: {emit_err}",
                event="m3u8_error_signal_failed",
                error_type=type(emit_err).__name__,
            )
        self.finished.emit([])

    @staticmethod
    def _is_master_playlist(content: str) -> bool:
        return "#EXT-X-STREAM-INF" in content

    def _resolve_nested_variants(
        self,
        variants: list,
        headers: dict,
        depth: int = 0,
        visited: set | None = None,
        *,
        budget_state: NestedBudgetState | None = None,
    ) -> list:
        """Resolve nested master playlists under a depth + fan-out budget.

        R16: every recursive fetch consumes quota from ``budget_state``
        (total / per-level / depth / wallclock). Exceeding any limit
        stops further recursion and returns the variants processed so
        far — we never raise. The caller inspects
        ``budget_state.exceeded_reason`` and may wrap the result in a
        :class:`StructuredError` with ``code="variants_truncated"``.

        R15 reuse: between variants we observe ``self.stop_event`` so
        cancellation during a long fan-out exits promptly, matching the
        retry-loop contract in ``_fetch_with_retry``.
        """

        visited = visited or set()
        if budget_state is None:
            # Backwards-compatible entry point: callers (incl. the
            # existing smoke tests) that did not yet know about the
            # budget still get sane default limits.
            budget_state = NestedBudgetState()

        # Honour the explicit legacy depth cap as well; the frozen
        # NestedBudget.max_depth is the primary gate, but
        # ``self._max_nested_depth`` remains user-configurable.
        effective_max_depth = min(budget_state.budget.max_depth, self._max_nested_depth)
        if depth >= effective_max_depth:
            if budget_state.exceeded_reason is None:
                budget_state.exceeded_reason = "depth"
            logger.warning(
                "[M3U8] nested depth limit reached",
                event="m3u8_nested_depth_limit",
                stage="parse_nested",
                depth=depth,
                max_depth=effective_max_depth,
            )
            return variants

        resolved: list = []
        # Track position within the current level's ``variants`` list.
        # ``resolved`` may grow faster than ``variants`` (nested calls
        # extend with multiple entries), so we cannot use
        # ``len(resolved)`` to slice remaining items.
        for idx, variant in enumerate(variants):
            # R15.5 cancel fan-out promptly.
            if self.stop_event.is_set():
                logger.info(
                    "[M3U8] nested resolve cancelled by stop_event",
                    event="m3u8_nested_cancelled",
                    stage="parse_nested",
                    depth=depth,
                )
                # Preserve any variants not yet processed at this level.
                resolved.extend(variants[idx:])
                break

            # Budget check before doing any work for this variant.
            if not budget_state.check(depth):
                # Keep the remaining variants as-is (unresolved) so the
                # UI still sees the master playlist's raw advertised
                # list rather than an empty page.
                resolved.extend(variants[idx:])
                break

            variant_url = (variant.get("url") or "").strip()
            if ".m3u8" not in variant_url.lower():
                resolved.append(variant)
                budget_state.record(depth)
                continue

            if variant_url in visited:
                logger.warning(
                    f"[M3U8] nested loop detected: {variant_url}",
                    event="m3u8_nested_loop_detected",
                    stage="parse_nested",
                    depth=depth,
                )
                resolved.append(variant)
                budget_state.record(depth)
                continue

            # R4 SSRF filter: refuse to fetch nested variants that point
            # at loopback/private/link-local/metadata hosts. This is a
            # SOFT failure: the variant is kept (so the UI still shows
            # it, marked via ``ssrf_blocked``) and other variants in the
            # master playlist continue to be resolved.
            try:
                ensure_public(variant_url)
            except SSRFBlocked as exc:
                logger.warning(
                    f"[SSRF] nested variant blocked: {exc.reason}",
                    event="m3u8_nested_ssrf_blocked",
                    stage="ssrf",
                    reason=exc.reason,
                    depth=depth,
                    url=redact_url(variant_url),
                )
                # Tag the variant so downstream consumers can tell the
                # difference between "unresolved because the fetch
                # failed" and "unresolved because policy forbade it".
                variant = dict(variant)
                variant["resolved"] = False
                variant["unresolved_reason"] = "ssrf_blocked"
                resolved.append(variant)
                budget_state.record(depth)
                continue

            try:
                content = self._fetch_once(variant_url, headers)
                budget_state.record(depth)
                if self._is_master_playlist(content):
                    nested = self._parse_m3u8_variants(content, variant_url)
                    if nested:
                        next_visited = set(visited)
                        next_visited.add(variant_url)
                        resolved.extend(
                            self._resolve_nested_variants(
                                nested,
                                headers,
                                depth=depth + 1,
                                visited=next_visited,
                                budget_state=budget_state,
                            )
                        )
                        # A nested call may have tripped the budget;
                        # stop advancing at this level too so we do not
                        # keep firing HTTP requests after truncation.
                        if budget_state.exceeded_reason is not None:
                            # Preserve any variants we have not yet
                            # visited at this level as raw unresolved.
                            resolved.extend(variants[idx + 1:])
                            break
                    else:
                        resolved.append(variant)
                else:
                    resolved.append(variant)
            except Exception as e:
                logger.warning(
                    f"Nested m3u8 fetch failed: {variant_url} - {e}",
                    event="m3u8_nested_fetch_failed",
                    stage="parse_nested",
                    depth=depth,
                    error_type=type(e).__name__,
                    url=variant_url,
                )
                resolved.append(variant)
                # Still count the attempted fetch toward the budget so
                # a flood of failing sub-masters cannot starve us.
                budget_state.record(depth)

        resolved.sort(key=lambda x: x.get("height", 0), reverse=True)
        return resolved

    def _parse_m3u8_variants(self, content: str, base_url: str) -> list:
        """Parse master playlist variants."""
        variants = []
        pattern = re.compile(r"#EXT-X-STREAM-INF:([^\n]+)(?:\n#(?!EXT).*)*\n\s*([^\n#]+)", re.MULTILINE)
        matches = pattern.findall(content)
        logger.debug(f"Regex matches found: {len(matches)}")

        for info_str, url_line in matches:
            url_line = url_line.strip()
            if not url_line:
                continue

            variant_url = urljoin(base_url, url_line)

            bandwidth = 0
            resolution = None
            height = 0
            width = 0

            bw_match = re.search(r"BANDWIDTH=(\d+)", info_str)
            if bw_match:
                bandwidth = int(bw_match.group(1))

            res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", info_str)
            if res_match:
                width = int(res_match.group(1))
                height = int(res_match.group(2))
                resolution = f"{width}x{height}"

            variants.append(
                {
                    "format_id": f"{height}p" if height else "auto",
                    "url": variant_url,
                    "height": height,
                    "width": width,
                    "resolution": resolution,
                    "tbr": round(bandwidth / 1024) if bandwidth else 0,
                    "filesize_str": f"{round(bandwidth / 8 / 1024 / 1024, 2)}MB/min" if bandwidth else "N/A",
                    "ext": "m3u8",
                    "vcodec": "H.264",
                    "fps": 30,
                }
            )

        variants.sort(key=lambda x: x["height"], reverse=True)
        return variants
