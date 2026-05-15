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

All functions in this module are pure: no I/O, no module-level caches,
no random sources. This is the property the R18.5 property-based test
relies on, and it is what makes the manager's retry loop deterministic
under test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from utils.logger import logger

if TYPE_CHECKING:  # pragma: no cover - imports only for type checkers
    from core.task_model import DownloadTask


__all__ = [
    "STOP_REASON_CLASSIFICATION",
    "classify_failure",
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

    # 3. Message keyword fallback + telemetry.
    effective_message = message
    if effective_message is None and task is not None:
        effective_message = task.error_message or ""
    effective_message = effective_message or ""

    classification = classify_message_keywords(effective_message)
    logger.debug(
        "[classify] fallback_message",
        event="download_classify_fallback",
        classification="fallback_message",
        resolved=classification,
    )
    return classification


def classify_message_keywords(message: str) -> str:
    """Legacy keyword-based classifier (kept for R18.4 fallback)."""
    if not message:
        return "unknown"
    text = message.lower()
    if "用户取消" in text or "用户暂停" in text or "cancelled" in text or "paused" in text:
        return "stopped"
    if "401" in text or "403" in text or "forbidden" in text or "unauthorized" in text:
        return "auth"
    if "signature" in text or "nsig" in text or "parse" in text or "no video formats" in text:
        return "parse"
    if "timeout" in text or "timed out" in text or "connection reset" in text:
        return "timeout"
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
    if "401" in text or "403" in text or "forbidden" in text or "unauthorized" in text:
        return "auth"
    if (
        "m3u8" in text
        or "master playlist" in text
        or "media playlist" in text
        or "manifest" in text
    ):
        return "playlist"
    if "ext-x-key" in text or "enc.key" in text or "decrypt" in text:
        return "key"
    if ".ts" in text or "segment" in text or "fragment" in text or "chunk" in text:
        return "segment"
    if "mux" in text or "merge" in text or "ffmpeg" in text:
        return "merge"
    return "unknown"
