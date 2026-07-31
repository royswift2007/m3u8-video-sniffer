"""Stateless classification helpers extracted from ``DownloadManager``.

The original ``core/download_manager.py`` encoded three concerns about
*why* a download attempt stopped:

1. An authoritative ``task.stop_reason`` produced by the worker loop or
   the engine layer (security-stability-hardening R18.1 / R18.3).
2. A structured ``error.code`` propagated via ``task.structured_error``
   (R18.1).
3. A keyword match over free-form error messages kept as a backwards
   compatibility fallback for engines that have not yet migrated to
   structured errors (R18.4).

Those classifiers were originally methods on ``DownloadManager`` even
though they never read any manager state. Task 25.1 of the
``security-stability-hardening`` spec splits ``DownloadManager`` across
four modules; this file owns the classifier concern and exposes the same
behaviour as the previous methods — callers pass the task (and optional
message) explicitly.

All functions in this module avoid I/O, module-level caches, and random
sources. Playlist expiry classification reads wall-clock time only when
explicit diagnostics include an ``expires_at`` timestamp; the remaining
keyword and stage helpers stay deterministic for unit tests.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from utils.logger import logger

if TYPE_CHECKING:  # pragma: no cover - imports only for type checkers
    from core.task_model import DownloadTask


__all__ = [
    "STOP_REASON_CLASSIFICATION",
    "classify_failure",
    "classify_playlist_diagnostics",
    "classify_message_keywords",
    "detect_failure_stage",
]


# Mapping from ``DownloadTask.stop_reason`` strings to the classification
# vocabulary consumed by ``_execute_download``'s retry loop and the
# Stage 3 observability pipeline (security-stability-hardening R18.2).
# Any non-empty ``stop_reason`` that isn't listed here is mapped to the
# generic ``"other"`` bucket rather than falling through to the message
# keyword path; this guarantees stop-reason semantics always win over
# locale-dependent keyword matching (R18.1 / R18.3).
STOP_REASON_CLASSIFICATION: Mapping[str, str] = {
    "paused": "paused",
    "cancelled": "cancelled",
    "removed": "removed",
    "shutdown": "shutdown",
    "engine_switch": "engine_switch",
    "ssrf_blocked": "ssrf_blocked",
    "checksum_mismatch": "checksum_mismatch",
    "insufficient_disk": "insufficient_disk",
    "path_tampered": "path_tampered",
}


def classify_failure(
    task: "DownloadTask | None",
    message: str | None = None,
) -> str:
    """Classify a failed download attempt into a coarse bucket.

    security-stability-hardening R18.1 / R18.3 / R18.4: structured
    signals are consulted in order of trust:

    1. ``task.stop_reason`` (read under ``task.lock``) — the
       authoritative reason the worker stopped. ``paused`` must surface
       as ``"paused"`` rather than ``"failed"`` so the retry loop does
       not treat a user-initiated pause like a network failure.
    2. ``task.structured_error.code`` — a ``StructuredError`` attached
       by the engine layer (``utils.errors.StructuredError``). The
       ``code`` string is returned verbatim because downstream
       telemetry already uses those codes (see
       ``core/component_update_downloader.py`` and
       ``core/m3u8_parser.py``).
    3. Message keyword match — kept for backwards compatibility with
       engines that have not yet migrated to structured errors. When
       this branch is taken a ``download_classify_fallback`` debug
       event is emitted so Stage 3 can quantify how often the legacy
       path remains in use (R18.4).

    The returned vocabulary preserves the legacy values
    (``auth``/``parse``/``timeout``/``stopped``/``unknown``) so
    existing retry/fallback branches in ``_execute_download`` keep
    behaving identically; the R18 additions (``paused``/``cancelled``/
    ``ssrf_blocked``/etc.) are strictly new values.

    Args:
        task: The failed task. ``None`` is tolerated for defensive
            callers and makes this function degrade to the keyword path.
        message: Optional explicit error message. When omitted the
            function falls back to ``task.error_message``.

    Returns:
        A classification string drawn from either
        :data:`STOP_REASON_CLASSIFICATION`, the structured error code,
        or the legacy keyword vocabulary.
    """

    # 1. Authoritative stop reason first. Read under the task lock so we
    # never observe a torn write while the worker is in the middle of a
    # ``transition()`` call (R11.4).
    if task is not None:
        try:
            with task.lock:
                stop_reason = (task.stop_reason or "").strip()
        except AttributeError:
            # ``task.lock`` is installed by ``DownloadTask.__post_init__``
            # but a test double or a pre-R11 task may not expose it; fall
            # back to an unlocked read rather than raising.
            stop_reason = (getattr(task, "stop_reason", "") or "").strip()

        if stop_reason:
            mapped = STOP_REASON_CLASSIFICATION.get(stop_reason)
            if mapped is not None:
                return mapped
            # Any non-empty but unknown ``stop_reason`` still wins over
            # the keyword path; bucket it explicitly so Stage 3 can
            # surface the unexpected value without losing data.
            return "other"

        # 2. Structured error attached to the task.
        structured = getattr(task, "structured_error", None)
        if structured is not None:
            code = getattr(structured, "code", None)
            if isinstance(code, str) and code:
                return code

        existing_error = (getattr(task, "error_message", "") or "").strip()
        if message is None and not existing_error:
            preflight_classification = classify_playlist_diagnostics(task, "")
            if preflight_classification in {"drm", "expired"}:
                logger.debug(
                    "[classify] playlist_diagnostics_preflight",
                    event="download_classify_playlist_diagnostics",
                    classification="playlist_diagnostics_preflight",
                    resolved=preflight_classification,
                )
                return preflight_classification

    # 3. Playlist diagnostics attached during M3U8 parsing. These signals
    # are more reliable than engine-specific free-form text for terminal DRM
    # and already-expired signed URLs, but still come after stop/structured
    # errors so authoritative runtime signals keep precedence.
    effective_message = message
    if effective_message is None and task is not None:
        effective_message = task.error_message or ""
    effective_message = effective_message or ""

    diagnostic_classification = classify_playlist_diagnostics(task, effective_message)
    if diagnostic_classification:
        logger.debug(
            "[classify] playlist_diagnostics",
            event="download_classify_playlist_diagnostics",
            classification="playlist_diagnostics",
            resolved=diagnostic_classification,
        )
        return diagnostic_classification

    # 4. Message keyword fallback + telemetry.
    classification = classify_message_keywords(effective_message)
    logger.debug(
        "[classify] fallback_message",
        event="download_classify_fallback",
        classification="fallback_message",
        resolved=classification,
    )
    return classification


def classify_playlist_diagnostics(
    task: "DownloadTask | None",
    message: str | None = None,
) -> str:
    """Classify failures from playlist diagnostics attached to a task."""
    if task is None:
        return ""

    diagnostics = getattr(task, "playlist_diagnostics", {}) or {}
    if not isinstance(diagnostics, Mapping):
        diagnostics = {}
    text = (message or getattr(task, "error_message", "") or "").lower()

    if bool(diagnostics.get("is_drm")):
        return "drm"

    expires_at = diagnostics.get("expires_at", getattr(task, "expires_at", None))
    try:
        expires_at_float = float(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expires_at_float = None
    if expires_at_float is not None and expires_at_float <= time.time():
        return "expired"

    ephemeral = bool(diagnostics.get("ephemeral_url", getattr(task, "ephemeral_url", False)))
    ttl_warning = bool(diagnostics.get("ttl_warning", getattr(task, "ttl_warning", False)))
    if ephemeral and ttl_warning and any(
        kw in text for kw in ("expired", "signature", "token", "403", "forbidden", "unauthorized")
    ):
        return "expired"

    if bool(diagnostics.get("key_cross_domain")) and any(
        kw in text
        for kw in (
            "key",
            "ext-x-key",
            "enc.key",
            "decrypt",
            "license",
            "401",
            "403",
            "forbidden",
            "unauthorized",
        )
    ):
        return "key_cross_domain"

    if bool(diagnostics.get("segment_cross_domain")) and any(
        kw in text
        for kw in ("segment", "fragment", "chunk", ".ts", ".m4s", "403", "forbidden", "404")
    ):
        return "segment_cross_domain"

    if bool(diagnostics.get("variant_cross_domain")) and any(
        kw in text for kw in ("m3u8", "playlist", "manifest", "403", "forbidden", "404")
    ):
        return "playlist_cross_domain"

    return ""



def classify_message_keywords(message: str) -> str:
    """Legacy keyword-based classifier (kept for R18.4 fallback)."""
    if not message:
        return "unknown"
    text = message.lower()
    if "用户取消" in text or "用户暂停" in text or "cancelled" in text or "paused" in text:
        return "stopped"
    if (
        "drm" in text
        or "widevine" in text
        or "license request" in text
        or "license server" in text
        or "encrypted media" in text
        or "encryptedmedia" in text
    ):
        return "drm"
    if (
        "signature expired" in text
        or "token expired" in text
        or "url expired" in text
        or "link expired" in text
        or "expired" in text
    ):
        return "expired"
    if (
        "geo restricted" in text
        or "georestricted" in text
        or "not available in your country" in text
        or "region blocked" in text
        or "地域" in text
        or "地区限制" in text
    ):
        return "geo"
    if (
        "certificate" in text
        or "cert verify" in text
        or "ssl" in text
        or "tls" in text
    ):
        return "tls"
    if "429" in text or "too many requests" in text or "rate limit" in text or "ratelimit" in text:
        return "rate_limit"
    if "key cross-domain" in text or "key_cross_domain" in text or "跨域 key" in text:
        return "key_cross_domain"
    if "segment cross-domain" in text or "segment_cross_domain" in text or "分片跨域" in text:
        return "segment_cross_domain"
    if "playlist cross-domain" in text or "playlist_cross_domain" in text or "播放列表跨域" in text:
        return "playlist_cross_domain"
    if "401" in text or "403" in text or "forbidden" in text or "unauthorized" in text:
        return "auth"
    if "timeout" in text or "timed out" in text or "connection reset" in text:
        return "timeout"
    if (
        "no space" in text
        or "disk full" in text
        or "insufficient disk" in text
        or "not enough space" in text
    ):
        return "disk"
    if (
        "segment noise" in text
        or "single segment" in text
        or "fragment-only" in text
        or "疑似分片" in text
    ):
        return "segment_noise"
    if "signature" in text or "nsig" in text or "parse" in text or "no video formats" in text:
        return "parse"
    if "usage information" in text or "--help" in text or "unknown option" in text:
        return "parse"
    return "unknown"


def detect_failure_stage(message: str) -> str:
    """Infer rough failure stage for observability."""
    if not message:
        return "unknown"
    text = message.lower()

    if "cancelled" in text or "paused" in text or "用户取消" in text or "用户暂停" in text:
        return "stopped"
    if (
        "no space" in text
        or "disk full" in text
        or "insufficient disk" in text
        or "not enough space" in text
        or "write error" in text
    ):
        return "disk"
    if "hls probe" in text or "probe" in text or "预探测" in text:
        return "probe"
    if (
        "spawn" in text
        or "popen" in text
        or "engine_invoke" in text
        or "cannot start" in text
        or "failed to start" in text
    ):
        return "engine_start"
    if "429" in text or "too many requests" in text or "rate limit" in text or "ratelimit" in text:
        return "rate_limit"
    if "401" in text or "403" in text or "forbidden" in text or "unauthorized" in text:
        return "auth"
    if "ext-x-key" in text or "enc.key" in text or "decrypt" in text or "key" in text:
        return "key"
    if (
        "m3u8" in text
        or "master playlist" in text
        or "media playlist" in text
        or "manifest" in text
    ):
        return "playlist"
    if ".ts" in text or "segment" in text or "fragment" in text or "chunk" in text:
        return "segment_download"
    if "postprocess" in text or "post-process" in text:
        return "postprocess"
    if "mux" in text or "merge" in text or "ffmpeg" in text:
        return "merge"
    if "parse" in text or "extractor" in text or "no video formats" in text:
        return "parse"
    return "unknown"
