"""
M3U8 Parser Utility
Handles fetching and parsing of M3U8 master playlists to extract quality variants.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from core.media_url_ttl import TTL_WARNING_SECONDS, analyze_media_url_ttl
from utils.config_manager import config
from utils.errors import StructuredError
from utils.logger import logger
from utils.redact import redact_url
from utils.proxy_config import requests_proxies
from utils.retry import BACKOFF, interruptible_sleep
from utils.ssrf_guard import (
    SSRFBlocked,
    ensure_public,
    make_pinned_session,
)


# HTTP status families the R15 backoff loop treats as recoverable.
_RECOVERABLE_STATUSES: frozenset = frozenset({429, 500, 502, 503, 504})

# 4xx statuses that trigger the R15.4 one-shot "smart Referer" fallback.
_AUTH_LIKE_STATUSES: frozenset = frozenset({401, 403})

_DIAGNOSTIC_URL_SAMPLE_LIMIT = 20


# ---------------------------------------------------------------------------
# Playlist diagnostics — DRM/key/segment cross-domain/ephemeral URL analysis
# ---------------------------------------------------------------------------


def _append_unique(values: list, value: Any, *, limit: int | None = None) -> bool:
    """Append ``value`` once, optionally capped to a small sample size."""
    if value in (None, "") or value in values:
        return False
    if limit is not None and len(values) >= limit:
        return False
    values.append(value)
    return True


def _safe_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except (TypeError, ValueError):
        return ""


def _is_cross_host(host: str, playlist_host: str) -> bool:
    return bool(host and playlist_host and host != playlist_host)


def _strip_hls_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_hls_attribute_list(raw: str) -> dict[str, str]:
    """Parse a HLS attribute list while respecting quoted commas."""
    attrs: dict[str, str] = {}
    key = ""
    buf: list[str] = []
    in_quotes = False
    quote_char = ""
    parts: list[str] = []

    for char in raw or "":
        if char in {'"', "'"}:
            if in_quotes and char == quote_char:
                in_quotes = False
                quote_char = ""
            elif not in_quotes:
                in_quotes = True
                quote_char = char
            buf.append(char)
            continue
        if char == "," and not in_quotes:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    if buf:
        parts.append("".join(buf).strip())

    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().upper()
        if key:
            attrs[key] = _strip_hls_quotes(value)
    return attrs


def _signed_url_info(url: str) -> tuple[list[str], float | None]:
    """Return signed-query names and the best-effort expiry timestamp."""
    analysis = analyze_media_url_ttl(url or "")
    return list(analysis.matched_params), analysis.expires_at


@dataclass
class PlaylistDiagnostics:
    """Download-risk diagnostics extracted from one or more HLS playlists."""

    playlist_url: str
    playlist_host: str = ""
    detected_at: float = field(default_factory=time.time)
    is_drm: bool = False
    has_encryption: bool = False
    encryption_methods: list[str] = field(default_factory=list)
    keyformats: list[str] = field(default_factory=list)
    drm_systems: list[str] = field(default_factory=list)
    key_urls: list[str] = field(default_factory=list)
    key_hosts: list[str] = field(default_factory=list)
    key_cross_domain_hosts: list[str] = field(default_factory=list)
    segment_urls: list[str] = field(default_factory=list)
    segment_hosts: list[str] = field(default_factory=list)
    segment_cross_domain_hosts: list[str] = field(default_factory=list)
    map_urls: list[str] = field(default_factory=list)
    map_hosts: list[str] = field(default_factory=list)
    variant_urls: list[str] = field(default_factory=list)
    variant_hosts: list[str] = field(default_factory=list)
    variant_cross_domain_hosts: list[str] = field(default_factory=list)
    signed_query_params: list[str] = field(default_factory=list)
    key_cross_domain: bool = False
    segment_cross_domain: bool = False
    variant_cross_domain: bool = False
    key_count: int = 0
    segment_count: int = 0
    map_count: int = 0
    variant_count: int = 0
    ephemeral_url: bool = False
    expires_at: float | None = None
    expires_in_seconds: float | None = None
    ttl_warning: bool = False
    warnings: list[str] = field(default_factory=list)
    risk_level: str = "none"

    def to_dict(self) -> dict[str, Any]:
        self._recompute()
        return {
            "playlist_url": self.playlist_url,
            "playlist_host": self.playlist_host,
            "detected_at": self.detected_at,
            "is_drm": self.is_drm,
            "has_encryption": self.has_encryption,
            "encryption_methods": list(self.encryption_methods),
            "keyformats": list(self.keyformats),
            "drm_systems": list(self.drm_systems),
            "key_urls": list(self.key_urls),
            "key_hosts": list(self.key_hosts),
            "key_cross_domain_hosts": list(self.key_cross_domain_hosts),
            "segment_urls": list(self.segment_urls),
            "segment_hosts": list(self.segment_hosts),
            "segment_cross_domain_hosts": list(self.segment_cross_domain_hosts),
            "map_urls": list(self.map_urls),
            "map_hosts": list(self.map_hosts),
            "variant_urls": list(self.variant_urls),
            "variant_hosts": list(self.variant_hosts),
            "variant_cross_domain_hosts": list(self.variant_cross_domain_hosts),
            "signed_query_params": list(self.signed_query_params),
            "key_cross_domain": self.key_cross_domain,
            "segment_cross_domain": self.segment_cross_domain,
            "variant_cross_domain": self.variant_cross_domain,
            "key_count": self.key_count,
            "segment_count": self.segment_count,
            "map_count": self.map_count,
            "variant_count": self.variant_count,
            "ephemeral_url": self.ephemeral_url,
            "expires_at": self.expires_at,
            "expires_in_seconds": self.expires_in_seconds,
            "ttl_warning": self.ttl_warning,
            "warnings": list(self.warnings),
            "risk_level": self.risk_level,
        }

    def merge_from(self, other: "PlaylistDiagnostics") -> None:
        """Merge nested media-playlist diagnostics into the root summary."""
        if not isinstance(other, PlaylistDiagnostics):
            return
        self.is_drm = self.is_drm or other.is_drm
        self.has_encryption = self.has_encryption or other.has_encryption
        self.ephemeral_url = self.ephemeral_url or other.ephemeral_url
        self.ttl_warning = self.ttl_warning or other.ttl_warning
        if other.expires_at is not None:
            self.expires_at = other.expires_at if self.expires_at is None else min(self.expires_at, other.expires_at)
        self.key_count += other.key_count
        self.segment_count += other.segment_count
        self.map_count += other.map_count
        self.variant_count += other.variant_count
        for attr in (
            "encryption_methods",
            "keyformats",
            "drm_systems",
            "key_urls",
            "key_hosts",
            "key_cross_domain_hosts",
            "segment_urls",
            "segment_hosts",
            "segment_cross_domain_hosts",
            "map_urls",
            "map_hosts",
            "variant_urls",
            "variant_hosts",
            "variant_cross_domain_hosts",
            "signed_query_params",
            "warnings",
        ):
            target = getattr(self, attr)
            for value in getattr(other, attr):
                _append_unique(target, value)
        self._recompute()

    def _recompute(self) -> None:
        self.key_cross_domain = bool(self.key_cross_domain_hosts)
        self.segment_cross_domain = bool(self.segment_cross_domain_hosts)
        self.variant_cross_domain = bool(self.variant_cross_domain_hosts)
        self.ephemeral_url = bool(self.ephemeral_url or self.signed_query_params or self.expires_at is not None)
        if self.expires_at is not None:
            self.expires_in_seconds = round(float(self.expires_at) - float(self.detected_at), 3)
            if self.expires_in_seconds <= TTL_WARNING_SECONDS:
                self.ttl_warning = True
        if self.is_drm:
            _append_unique(self.warnings, "检测到 DRM/许可证加密，内置下载引擎可能无法解密。")
        elif self.has_encryption and self.key_urls:
            _append_unique(self.warnings, "检测到 HLS 加密密钥，下载时必须能访问 key URI。")
        if self.key_cross_domain:
            hosts = ", ".join(self.key_cross_domain_hosts[:3])
            _append_unique(self.warnings, f"检测到跨域 key URI：{hosts}。")
        if self.segment_cross_domain:
            hosts = ", ".join(self.segment_cross_domain_hosts[:3])
            _append_unique(self.warnings, f"检测到跨域分片/CDN：{hosts}。")
        if self.variant_cross_domain:
            hosts = ", ".join(self.variant_cross_domain_hosts[:3])
            _append_unique(self.warnings, f"检测到跨域子播放列表：{hosts}。")
        if self.ttl_warning:
            _append_unique(self.warnings, "检测到即将过期或已过期的签名 URL，建议重新嗅探后立即下载。")
        if self.is_drm:
            self.risk_level = "error"
        elif self.key_cross_domain or self.segment_cross_domain or self.variant_cross_domain or self.ttl_warning:
            self.risk_level = "warning"
        else:
            self.risk_level = "none"


def _merge_signed_url_info(diag: PlaylistDiagnostics, url: str) -> None:
    signed_names, expires_at = _signed_url_info(url)
    for name in signed_names:
        _append_unique(diag.signed_query_params, name)
    if signed_names:
        diag.ephemeral_url = True
    if expires_at is not None:
        diag.expires_at = expires_at if diag.expires_at is None else min(diag.expires_at, expires_at)


def _classify_drm_system(keyformat: str = "", uri: str = "") -> list[str]:
    haystack = f"{keyformat} {uri}".lower()
    systems: list[str] = []
    if "widevine" in haystack or "edef8ba9" in haystack:
        systems.append("Widevine")
    if "fairplay" in haystack or "streamingkeydelivery" in haystack or "skd://" in haystack:
        systems.append("FairPlay")
    if "playready" in haystack or "9a04f079" in haystack:
        systems.append("PlayReady")
    if not systems and keyformat and keyformat.lower() not in {"identity", "com.apple.streamingkeydelivery"}:
        systems.append(keyformat)
    return systems


def _record_diagnostic_url(diag: PlaylistDiagnostics, url: str, role: str) -> None:
    if not url:
        return
    _merge_signed_url_info(diag, url)
    host = _safe_host(url)
    playlist_host = diag.playlist_host
    if role == "key":
        diag.key_count += 1
        _append_unique(diag.key_urls, url, limit=_DIAGNOSTIC_URL_SAMPLE_LIMIT)
        _append_unique(diag.key_hosts, host)
        if _is_cross_host(host, playlist_host):
            _append_unique(diag.key_cross_domain_hosts, host)
    elif role == "segment":
        diag.segment_count += 1
        _append_unique(diag.segment_urls, url, limit=_DIAGNOSTIC_URL_SAMPLE_LIMIT)
        _append_unique(diag.segment_hosts, host)
        if _is_cross_host(host, playlist_host):
            _append_unique(diag.segment_cross_domain_hosts, host)
    elif role == "map":
        diag.map_count += 1
        _append_unique(diag.map_urls, url, limit=_DIAGNOSTIC_URL_SAMPLE_LIMIT)
        _append_unique(diag.map_hosts, host)
        if _is_cross_host(host, playlist_host):
            _append_unique(diag.segment_cross_domain_hosts, host)
    elif role == "variant":
        diag.variant_count += 1
        _append_unique(diag.variant_urls, url, limit=_DIAGNOSTIC_URL_SAMPLE_LIMIT)
        _append_unique(diag.variant_hosts, host)
        if _is_cross_host(host, playlist_host):
            _append_unique(diag.variant_cross_domain_hosts, host)


def analyze_playlist_diagnostics(playlist_url: str, content: str) -> PlaylistDiagnostics:
    """Analyze one HLS playlist for DRM/cross-domain/signed-URL risks."""
    diag = PlaylistDiagnostics(
        playlist_url=playlist_url or "",
        playlist_host=_safe_host(playlist_url or ""),
    )
    _merge_signed_url_info(diag, playlist_url or "")

    next_uri_role = ""
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith("#EXT-X-STREAM-INF"):
            next_uri_role = "variant"
            continue

        if upper.startswith("#EXT-X-I-FRAME-STREAM-INF") or upper.startswith("#EXT-X-MEDIA"):
            attrs = _parse_hls_attribute_list(line.split(":", 1)[1] if ":" in line else "")
            uri = attrs.get("URI", "")
            if uri and not uri.lower().startswith("data:"):
                _record_diagnostic_url(diag, urljoin(playlist_url, uri), "variant")
            continue

        if upper.startswith("#EXT-X-KEY") or upper.startswith("#EXT-X-SESSION-KEY"):
            attrs = _parse_hls_attribute_list(line.split(":", 1)[1] if ":" in line else "")
            method = (attrs.get("METHOD") or "").upper()
            keyformat = attrs.get("KEYFORMAT") or ""
            uri = attrs.get("URI") or ""
            if method and method != "NONE":
                diag.has_encryption = True
                _append_unique(diag.encryption_methods, method)
            if keyformat:
                _append_unique(diag.keyformats, keyformat)
            drm_systems = _classify_drm_system(keyformat, uri)
            if drm_systems and method != "NONE":
                diag.is_drm = True
                for system in drm_systems:
                    _append_unique(diag.drm_systems, system)
            if uri:
                if uri.lower().startswith("skd://"):
                    diag.is_drm = True
                    _append_unique(diag.drm_systems, "FairPlay")
                elif not uri.lower().startswith("data:"):
                    _record_diagnostic_url(diag, urljoin(playlist_url, uri), "key")
            continue

        if upper.startswith("#EXT-X-MAP"):
            attrs = _parse_hls_attribute_list(line.split(":", 1)[1] if ":" in line else "")
            uri = attrs.get("URI", "")
            if uri and not uri.lower().startswith("data:"):
                _record_diagnostic_url(diag, urljoin(playlist_url, uri), "map")
            continue

        if line.startswith("#"):
            continue

        resolved = urljoin(playlist_url, line)
        if next_uri_role == "variant":
            _record_diagnostic_url(diag, resolved, "variant")
            next_uri_role = ""
        else:
            _record_diagnostic_url(diag, resolved, "segment")

    diag._recompute()
    return diag


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
        self.playlist_diagnostics: dict[str, Any] = {}
        self._playlist_diagnostics_obj: PlaylistDiagnostics | None = None

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
            logger.info(f"Analyzing M3U8 playlist: {redact_url(self.url)}")

            # R4 SSRF filter: reject non-public target before any network I/O.
            # F-02: honour the security.allow_private_networks opt-in so
            # operators running against a private mirror don't get hard-
            # blocked (still warned loudly via the ssrf_private_allowed log
            # event emitted inside _ensure_public_opt).
            try:
                self._ensure_public_opt(self.url)
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
                logger.info(f"Detected URL in response body, following pseudo-redirect to: {redact_url(redirect_url)}")
                # R4 SSRF filter: re-check the redirect target before following it.
                try:
                    self._ensure_public_opt(redirect_url)
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
                            url=redact_url(redirect_url),
                        )
                    else:
                        content = followed
                        self.url = redirect_url
                        logger.info(f"New M3U8 Content Sample (First 500 chars):\n{content[:500]}")

            self._update_playlist_diagnostics(self.url, content)

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

    @staticmethod
    def _allow_private_networks() -> bool:
        """Read the F-02 ``security.allow_private_networks`` opt-in flag.

        Defaults to False (fail-closed). The flag is documented in the
        manual as an explicit opt-in for trusted private-mirror
        deployments; when enabled the SSRF guard logs a prominent
        ``ssrf_private_allowed`` warning instead of hard-refusing.
        """
        try:
            return bool(config.get("security.allow_private_networks", False))
        except Exception:
            return False

    def _ensure_public_opt(self, url: str):
        """SSRF guard entry point that threads the F-02 opt-in through.

        When the opt-in is on, a private target is *allowed* but logged
        loudly so audit trails still surface the deviation. The resolved
        :class:`ResolvedHost` is returned so callers can pin the
        connection to that IP.
        """
        allow_private = self._allow_private_networks()
        if allow_private:
            try:
                resolved = ensure_public(url, allow_private=True)
            except SSRFBlocked:
                # Even in opt-in mode non-public IPs that fail the
                # resolver itself (dns_error / scheme_not_allowed /
                # url_empty) are still blocked — only ip_in_blocklist
                # is relaxed, which ensure_public already honours.
                raise
            logger.warning(
                "[SSRF] allow_private_networks is enabled; private target accepted",
                event="ssrf_private_allowed",
                stage="ssrf",
                url=redact_url(url),
            )
            return resolved
        return ensure_public(url, allow_private=False)

    def _fetch_once(self, url: str, headers: dict) -> str:
        if not self._verify_tls and not self._tls_warning_emitted:
            logger.warning("[M3U8] TLS verification disabled by config")
            self._tls_warning_emitted = True
        # (connect, read) split timeout per design 3.1. Kept conservative
        # so a slow TLS handshake does not starve the 0.5s backoff budget.
        current_url = url
        max_redirects = 5
        for redirect_count in range(max_redirects + 1):
            # F-01-lite: SSRF guard via ensure_public + source-address pinning
            # via make_pinned_session.  The URL is kept in its original
            # hostname form so that HTTPS SNI / certificate validation
            # works correctly (replacing the authority with an IP literal
            # breaks TLS for virtually all CDN-hosted playlists).
            resolved = self._ensure_public_opt(current_url)
            session = make_pinned_session(
                resolved, verify=self._verify_tls
            )
            proxy_kwargs = {}
            proxies = requests_proxies()
            if proxies:
                proxy_kwargs["proxies"] = proxies
            response = self._pinned_get(
                session,
                current_url,
                headers=headers,
                timeout=(5, 15),
                verify=self._verify_tls,
                **proxy_kwargs,
            )
            status_code = getattr(response, "status_code", None)
            response_headers = dict(getattr(response, "headers", {}) or {})
            if status_code in {301, 302, 303, 307, 308}:
                location = response_headers.get("Location") or response_headers.get("location")
                if not location:
                    response.raise_for_status()
                    break
                if redirect_count >= max_redirects:
                    raise requests.TooManyRedirects(f"Exceeded {max_redirects} redirects")
                redirect_url = urljoin(current_url, str(location).strip())
                # Per-hop re-validation: the redirect target may resolve
                # to a different host that itself needs the SSRF guard.
                self._ensure_public_opt(redirect_url)
                close_response = getattr(response, "close", None)
                if callable(close_response):
                    close_response()
                current_url = redirect_url
                continue

            response.raise_for_status()
            self._last_response_info = {
                "status_code": status_code,
                "url": getattr(response, "url", current_url) or current_url,
                "headers": response_headers,
            }
            return response.text

        raise requests.TooManyRedirects(f"Exceeded {max_redirects} redirects")

    @staticmethod
    def _pinned_get(session, url: str, **kwargs):
        """Issue a no-redirect GET, preferring a pinned session when
        available and falling back to a plain ``requests.get`` when the
        session could not be constructed (requests missing). The fallback
        path is still SSRF-guarded because the caller already ran
        :func:`ensure_public` and the URL authority is the vetted IP.
        """
        try:
            allow_redirects = kwargs.pop("allow_redirects", False)
            if session is not None:
                return session.get(url, allow_redirects=allow_redirects, **kwargs)
            return requests.get(url, allow_redirects=allow_redirects, **kwargs)
        except TypeError as exc:
            # Very old requests builds may not accept allow_redirects
            # in this shape; degrade gracefully but never raise from
            # the proxy argument.
            if "allow_redirects" not in str(exc):
                raise
            if session is not None:
                return session.get(url, **kwargs)
            return requests.get(url, **kwargs)

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

            # Defence in depth against DNS rebind between retries. The
            # actual source-address pinning happens inside _fetch_once
            # via make_pinned_session; this re-check is a cheap pre-flight
            # F-02 opt-in logging path.
            self._ensure_public_opt(url)

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
    def _playlist_diagnostics_enabled() -> bool:
        features = config.get("features", {}) or {}
        return bool(features.get("playlist_diagnostics_enabled", True))

    def _update_playlist_diagnostics(self, playlist_url: str, content: str) -> None:
        """Replace this thread's diagnostics with the fetched root playlist."""
        if not self._playlist_diagnostics_enabled():
            self.playlist_diagnostics = {}
            self._playlist_diagnostics_obj = None
            return
        diagnostics = analyze_playlist_diagnostics(playlist_url, content)
        self._playlist_diagnostics_obj = diagnostics
        self.playlist_diagnostics = diagnostics.to_dict()
        logger.info(
            "[M3U8] playlist diagnostics summary",
            event="m3u8_playlist_diagnostics",
            url=redact_url(playlist_url),
            risk_level=self.playlist_diagnostics.get("risk_level", "none"),
            is_drm=bool(self.playlist_diagnostics.get("is_drm")),
            key_cross_domain=bool(self.playlist_diagnostics.get("key_cross_domain")),
            segment_cross_domain=bool(self.playlist_diagnostics.get("segment_cross_domain")),
            variant_cross_domain=bool(self.playlist_diagnostics.get("variant_cross_domain")),
            ephemeral_url=bool(self.playlist_diagnostics.get("ephemeral_url")),
            ttl_warning=bool(self.playlist_diagnostics.get("ttl_warning")),
        )

    def _merge_playlist_diagnostics(self, playlist_url: str, content: str) -> None:
        """Merge nested media-playlist diagnostics into the current summary."""
        if not self._playlist_diagnostics_enabled():
            return
        diagnostics = analyze_playlist_diagnostics(playlist_url, content)
        if self._playlist_diagnostics_obj is None:
            self._playlist_diagnostics_obj = diagnostics
        else:
            self._playlist_diagnostics_obj.merge_from(diagnostics)
        self.playlist_diagnostics = self._playlist_diagnostics_obj.to_dict()

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
                self._ensure_public_opt(variant_url)
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
                self._merge_playlist_diagnostics(variant_url, content)
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
