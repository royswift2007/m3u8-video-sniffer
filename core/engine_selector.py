"""
Engine selector for intelligently choosing the best download engine.

This module exposes two public surfaces:

1. :class:`EngineSelector` — the legacy, engine-instance-aware selector used
   by ``download_manager`` / ``ui.main_window`` / ``main.py``. It preserves
   the historical ``select`` / ``predict`` / ``get_candidates`` API so the
   rest of the codebase keeps compiling while task 22.1 lands.

2. :func:`select_engine` + :class:`EngineDecision` — the Requirement 24 /
   design 3.9 pure-decision API. Given a URL (and optional manual override),
   it returns a structured :class:`EngineDecision` describing which engine
   name should run the download and **why** that engine was chosen. It does
   not require engine instances and can be unit-tested in isolation.

Decision priority (per Requirement 24.1-24.5):

    manual > HEAD MIME probe (2s, SSRF-guarded) > extension (query stripped)
    > live-platform host/path match > yt-dlp fallback

On HEAD failure (timeout, SSRF, network) the decision falls through to
extension matching with ``source="fallback_on_error"`` and an
``engine_select=fallback`` telemetry log line so operators can see how often
the probe path degrades in the wild.

The extension / HLS / live-platform rule sets are loaded once by
``core.engine_rules_loader`` (task 22.2) from ``resources/engine_rules.json``
and are **not** re-defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from core.engine_rules_loader import (
    DIRECT_EXTENSIONS,
    HLS_EXTENSIONS,
    LIVE_PLATFORMS,
)
from engines.base_engine import BaseEngine
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine
from engines.ytdlp_engine import YtdlpEngine
from engines.streamlink_engine import StreamlinkEngine
from engines.aria2_engine import Aria2Engine
from utils.logger import logger
from utils.i18n import TR
from utils import ssrf_guard

try:  # requests is already a project dependency, but keep the probe optional.
    import requests as _requests
except Exception:  # pragma: no cover - fallback if requests is unavailable
    _requests = None


# ---------------------------------------------------------------------------
# Canonical engine name constants (match the concrete engines' get_name()).
# ---------------------------------------------------------------------------

ENGINE_N_M3U8DL_RE: str = "N_m3u8DL-RE"
ENGINE_ARIA2: str = "Aria2"
ENGINE_STREAMLINK: str = "Streamlink"
ENGINE_YTDLP: str = "yt-dlp"


# HEAD probe timeout is fixed by Requirement 24 at 2 seconds.
_HEAD_PROBE_TIMEOUT_S: float = 2.0


# MIME fragments that identify HLS / DASH manifests. ``in`` matches are used
# so charset / vendor-tree variants ("application/vnd.apple.mpegurl;...",
# "application/x-mpegurl", etc.) are all covered without listing each one.
_HLS_MIME_NEEDLES: tuple[str, ...] = (
    "mpegurl",          # application/vnd.apple.mpegurl, application/x-mpegurl
    "dash+xml",         # application/dash+xml
)

# Direct video MIME fragments that route to aria2.
_DIRECT_MIME_NEEDLES: tuple[str, ...] = (
    "video/",
    "application/octet-stream",
)


# ---------------------------------------------------------------------------
# Public decision API (Requirement 24 / design 3.9).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineDecision:
    """Immutable decision record produced by :func:`select_engine`.

    Attributes:
        engine_name: Canonical engine name (matches ``BaseEngine.get_name``).
        source:      How the decision was reached. One of:
                     ``"manual"``, ``"mime"``, ``"extension"``, ``"live"``,
                     ``"fallback"``, ``"fallback_on_error"``.
        reason:      Optional human-readable detail (MIME value, matched
                     extension, matched host fragment, etc.). May be ``None``.
    """

    engine_name: str
    source: str
    reason: Optional[str] = None


def _strip_query(url: str) -> str:
    """Return ``url`` with its query and fragment removed.

    Invalid URLs flow through untouched so callers never need to try/except.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query and not parts.fragment:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _path_extension(url: str) -> str:
    """Return the lower-cased path suffix of ``url`` (e.g. ``.mp4``).

    Query strings are stripped before suffix extraction per Requirement 24.2.
    Returns an empty string when no suffix is present.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    path = parts.path or ""
    if not path:
        return ""
    return PurePosixPath(path).suffix.lower()


def _match_live_platform(url: str) -> Optional[str]:
    """Return the matched live-platform fragment or ``None``.

    The match is performed against host + path (both lower-cased) as a
    simple substring test, which mirrors the legacy
    ``StreamlinkEngine.can_handle`` behaviour so entries like
    ``"youtube.com/live"`` continue to work.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower()
    if not host and not path:
        # Fallback to comparing the raw URL for non-standard schemes.
        haystack = url.lower()
    else:
        haystack = f"{host}{path}"
    for platform in LIVE_PLATFORMS:
        if platform and platform in haystack:
            return platform
    return None


def _head_probe_mime(url: str) -> Optional[str]:
    """Send a 2-second HEAD request and return the lower-cased Content-Type.

    The request is SSRF-guarded via :func:`utils.ssrf_guard.ensure_public`
    and pinned to the first resolved IP so a DNS rebind cannot swing the
    connection to a private address between the guard check and the HEAD.

    Returns ``None`` when the probe cannot be performed (invalid scheme,
    SSRF block, no ``requests`` library, network/timeout error, non-2xx
    status, etc.). Exceptions are NEVER raised to the caller — a ``None``
    return lets :func:`select_engine` fall through to extension matching.
    """
    if _requests is None:
        return None

    try:
        resolved = ssrf_guard.ensure_public(url)
    except ssrf_guard.SSRFBlocked:
        return None
    except Exception:
        return None

    # Pin the TCP connection to the first already-vetted IP while keeping
    # the original hostname for TLS SNI / Host header. If that fails for
    # any reason, fall back to letting ``requests`` resolve the hostname
    # again (still inside the 2s timeout envelope).
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname or ""
    pinned_url = url
    pinned_host_header: Optional[str] = None
    if resolved.ips and host:
        first_ip = resolved.ips[0]
        # Bracket IPv6 literals in the URL authority.
        ip_literal = f"[{first_ip}]" if ":" in str(first_ip) else str(first_ip)
        port = f":{parts.port}" if parts.port else ""
        userinfo = ""
        if parts.username:
            userinfo = parts.username
            if parts.password:
                userinfo += f":{parts.password}"
            userinfo += "@"
        new_netloc = f"{userinfo}{ip_literal}{port}"
        pinned_url = urlunsplit(
            (parts.scheme, new_netloc, parts.path, parts.query, parts.fragment)
        )
        pinned_host_header = host if parts.port is None else f"{host}:{parts.port}"

    headers = {}
    if pinned_host_header:
        headers["Host"] = pinned_host_header

    try:
        # allow_redirects=False so a 30x hop doesn't silently re-resolve
        # via requests' own DNS path (which would bypass our pinning).
        resp = _requests.head(
            pinned_url,
            timeout=_HEAD_PROBE_TIMEOUT_S,
            allow_redirects=False,
            headers=headers or None,
        )
    except Exception:
        # ``requests`` can raise anything from ConnectionError / Timeout
        # through SSLError / InvalidURL; the probe is strictly best-effort,
        # so we fall through silently and let the caller degrade to the
        # extension table.
        return None

    status = getattr(resp, "status_code", 0) or 0
    if status < 200 or status >= 400:
        return None

    ctype = ""
    try:
        raw = resp.headers.get("Content-Type") or resp.headers.get("content-type") or ""
        ctype = raw.split(";", 1)[0].strip().lower()
    except Exception:
        ctype = ""

    return ctype or None


def _decide_from_mime(mime: str, ext: str) -> Optional[tuple[str, str]]:
    """Map a Content-Type string to (engine_name, reason) or ``None``."""
    if not mime:
        return None
    for needle in _HLS_MIME_NEEDLES:
        if needle in mime:
            return (ENGINE_N_M3U8DL_RE, mime)
    for needle in _DIRECT_MIME_NEEDLES:
        if needle in mime:
            # Only accept application/octet-stream when the path suffix also
            # looks like a direct video file, to avoid misrouting generic
            # binary downloads (installers, archives, ...) to aria2.
            if needle == "application/octet-stream" and ext not in DIRECT_EXTENSIONS:
                continue
            return (ENGINE_ARIA2, mime)
    return None


def _log_fallback_on_error(url: str, engine_name: str, source: str, reason: Optional[str]) -> None:
    """Emit the ``engine_select=fallback`` telemetry line (Requirement 24.3)."""
    try:
        from utils.redact import redact_url
        safe_url = redact_url(url)
    except Exception:
        safe_url = url
    logger.warning(
        f"[engine_select] HEAD probe unavailable, falling back to extension matching",
        event="engine_select_fallback",
        engine_select="fallback",
        url=safe_url,
        engine=engine_name,
        source=source,
        reason=reason or "",
    )


def select_engine(url: str, manual: Optional[str] = None) -> EngineDecision:
    """Choose an engine for ``url`` and return the structured decision.

    Priority (Requirement 24.1-24.5):

    1. ``manual`` — if provided, returned as-is with ``source="manual"``.
    2. HEAD MIME probe (2s, SSRF-guarded) — HLS/DASH → N_m3u8DL-RE,
       direct video MIME → Aria2. Failures fall through.
    3. Extension match (query stripped) against HLS / direct extension
       tables loaded by :mod:`core.engine_rules_loader`.
    4. Live-platform host/path substring match → Streamlink.
    5. yt-dlp fallback.

    When the HEAD probe raises or is skipped but the final decision comes
    from the extension table, ``source`` is tagged ``"fallback_on_error"``
    and an ``engine_select=fallback`` telemetry warning is emitted.
    """
    # 1. Manual override — highest priority.
    if manual:
        name = manual.strip()
        if name:
            return EngineDecision(engine_name=name, source="manual", reason=None)

    # Parse the URL once for extension / live-platform / probe decisions.
    pure_url = _strip_query(url or "")
    ext = _path_extension(pure_url)

    # 2. HEAD MIME probe (SSRF-guarded, 2s).
    probe_attempted = False
    probe_mime: Optional[str] = None
    try:
        probe_attempted = True
        probe_mime = _head_probe_mime(url)
    except Exception:
        probe_mime = None

    if probe_mime:
        hit = _decide_from_mime(probe_mime, ext)
        if hit is not None:
            engine_name, reason = hit
            return EngineDecision(engine_name=engine_name, source="mime", reason=reason)
        # Probe returned a MIME but it wasn't decisive → fall through to
        # extension matching without emitting the fallback warning.

    # 3. Extension match (query already stripped).
    if ext:
        if ext in HLS_EXTENSIONS:
            source = "fallback_on_error" if probe_attempted and probe_mime is None else "extension"
            if source == "fallback_on_error":
                _log_fallback_on_error(url, ENGINE_N_M3U8DL_RE, source, ext)
            return EngineDecision(engine_name=ENGINE_N_M3U8DL_RE, source=source, reason=ext)
        if ext in DIRECT_EXTENSIONS:
            source = "fallback_on_error" if probe_attempted and probe_mime is None else "extension"
            if source == "fallback_on_error":
                _log_fallback_on_error(url, ENGINE_ARIA2, source, ext)
            return EngineDecision(engine_name=ENGINE_ARIA2, source=source, reason=ext)

    # 4. Live-platform match.
    platform = _match_live_platform(url or "")
    if platform is not None:
        return EngineDecision(engine_name=ENGINE_STREAMLINK, source="live", reason=platform)

    # 5. yt-dlp fallback.
    return EngineDecision(engine_name=ENGINE_YTDLP, source="fallback", reason="default")


# Convenience alias so callers can opt into the explicit decision API
# without renaming imports.
def select_engine_decision(url: str, manual: Optional[str] = None) -> EngineDecision:
    """Alias of :func:`select_engine` preserved for call-site readability."""
    return select_engine(url, manual=manual)


# ---------------------------------------------------------------------------
# Legacy engine-instance-aware selector (preserved for existing callers).
# ---------------------------------------------------------------------------


class EngineSelector:
    """智能引擎选择器"""

    def __init__(self, engines: list[BaseEngine]):
        self.engines = engines
        self._engine_map = {engine.get_name(): engine for engine in engines}

    def _get_priority_order(self) -> list[type[BaseEngine]]:
        """引擎优先级顺序"""
        return [
            N_m3u8DL_RE_Engine,
            StreamlinkEngine,
            Aria2Engine,
            YtdlpEngine,  # 万能兜底
        ]

    def _safe_can_handle(self, engine: BaseEngine, url: str) -> bool:
        """Safely evaluate whether an engine can clearly handle the current URL."""
        try:
            return bool(engine.can_handle(url))
        except Exception as exc:
            logger.warning(
                f"{TR('log_engine_handle_exception')}: {engine.get_name()} - {exc}"
            )
            return False

    def get_candidates(self, url: str) -> list[tuple[BaseEngine, str]]:
        """按优先级返回可用引擎列表"""
        if not self.engines:
            return []
        candidates = []
        priority_order = self._get_priority_order()
        for engine_class in priority_order:
            for engine in self.engines:
                if isinstance(engine, engine_class) and self._safe_can_handle(engine, url):
                    engine_name = engine.get_name()
                    candidates.append((engine, engine_name))
        if not candidates and self.engines:
            fallback = self.engines[0]
            candidates.append((fallback, fallback.get_name()))
        return candidates

    def predict(
        self,
        url: str,
        user_preference: Optional[str] = None,
    ) -> tuple[BaseEngine, str]:
        """
        预测探测阶段应显示的引擎。

        设计目标：
        - 用户显式指定时，优先反映该选择；
        - 只有在 can_handle 已明确返回 False 时，才不继续显示该显式引擎；
        - URL 信息不足、识别不完整时，不因为缺少候选就武断改成别的引擎。
        """
        if user_preference and user_preference in self._engine_map:
            preferred_engine = self._engine_map[user_preference]
            if self._safe_can_handle(preferred_engine, url):
                logger.info(f"{TR('log_engine_predict_user_pref')}: {user_preference}")
                return preferred_engine, user_preference

            auto_candidates = self.get_candidates(url)
            auto_names = {name for _, name in auto_candidates}
            if auto_candidates and user_preference not in auto_names:
                engine, engine_name = auto_candidates[0]
                logger.info(
                    TR("log_engine_predict_overridden"),
                    event="predict_engine_overridden",
                    preferred_engine=user_preference,
                    predicted_engine=engine_name,
                    url=url,
                )
                return engine, engine_name

            logger.info(
                f"{TR('log_engine_predict_keep_user')}: {user_preference}",
                event="predict_engine_keep_user_preference",
                preferred_engine=user_preference,
                url=url,
            )
            return preferred_engine, user_preference

        candidates = self.get_candidates(url)
        if not candidates:
            raise RuntimeError("无可用下载引擎，请检查引擎配置或二进制文件")
        engine, engine_name = candidates[0]
        logger.info(f"{TR('log_engine_predict_auto')}: {engine_name}")
        return engine, engine_name

    def select(self, url: str, user_preference: Optional[str] = None) -> tuple[BaseEngine, str]:
        """
        智能选择引擎

        Args:
            url: 资源 URL
            user_preference: 用户在全局 UI 中指定的引擎名称（None = 自动选择）

        Returns:
            (engine, engine_name) 元组
        """
        # 1️⃣ 真正入队/执行前仍优先使用用户指定的引擎
        if user_preference and user_preference in self._engine_map:
            preferred_engine = self._engine_map[user_preference]
            logger.info(f"{TR('log_engine_use_user_pref')}: {user_preference}")
            return preferred_engine, user_preference

        # 2️⃣ 自动选择：按优先级匹配
        candidates = self.get_candidates(url)
        if not candidates:
            raise RuntimeError(TR("msg_engine_not_found_text"))
        engine, engine_name = candidates[0]
        logger.info(f"自动选择引擎: {engine_name}")
        return engine, engine_name

    def get_engine_by_name(self, name: str) -> Optional[BaseEngine]:
        """根据名称获取引擎"""
        return self._engine_map.get(name)

    def list_available_engines(self) -> list[str]:
        """列出所有可用引擎"""
        return list(self._engine_map.keys())


__all__ = [
    "EngineDecision",
    "select_engine",
    "select_engine_decision",
    "EngineSelector",
    "ENGINE_N_M3U8DL_RE",
    "ENGINE_ARIA2",
    "ENGINE_STREAMLINK",
    "ENGINE_YTDLP",
]
