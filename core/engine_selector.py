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

from core.download_context import (
    EngineSelectContext,
    RESOURCE_TYPE_HLS as CTX_HLS,
    RESOURCE_TYPE_DASH as CTX_DASH,
    RESOURCE_TYPE_DIRECT_VIDEO as CTX_DIRECT_VIDEO,
    RESOURCE_TYPE_PAGE as CTX_PAGE,
    RESOURCE_TYPE_SEGMENT as CTX_SEGMENT,
    RESOURCE_TYPE_UNKNOWN as CTX_UNKNOWN,
    build_engine_select_context,
)
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
from utils.proxy_config import requests_proxies
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


def _aria2_is_unsafe_for_context(url: str, context: EngineSelectContext | None) -> bool:
    """判断手动选择 Aria2 是否不安全，应被覆盖。

    仅在上下文/URL 明确表明这是 HLS/MPD 或带嗅探上下文的 segment 时返回 True。
    裸 .ts 直链且无上下文时返回 False，兼容完整 MPEG-TS 文件下载。
    """
    ext = _path_extension(url)
    if ext in {".m3u8", ".mpd"}:
        return True

    if context is None:
        return False

    if context.resource_type in {CTX_HLS, CTX_DASH}:
        return True

    if context.resource_type == CTX_SEGMENT:
        if (
            context.master_url
            or context.media_url
            or context.page_url
            or context.source != CTX_UNKNOWN
        ):
            return True

    mime = (context.mime or "").lower()
    if "mpegurl" in mime or "dash+xml" in mime:
        return True

    return False


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

    # F-02: honour the security.allow_private_networks opt-in for the
    # HEAD probe path. The import is lazy so this pure-decision module
    # keeps its zero-config import footprint for unit tests.
    try:
        from utils.config_manager import config as _config
        allow_private = bool(_config.get("security.allow_private_networks", False))
    except Exception:
        allow_private = False
    try:
        resolved = ssrf_guard.ensure_public(url, allow_private=allow_private)
    except ssrf_guard.SSRFBlocked:
        return None
    except Exception:  # NOSONAR: HEAD probe is best-effort; any resolver/transport error must degrade to extension matching, never raise to the selector caller
        return None
    if allow_private:
        try:
            logger.warning(
                "[engine_select] allow_private_networks is enabled; private HEAD target accepted",
                event="ssrf_private_allowed",
                stage="ssrf",
                url=url,
            )
        except (OSError, RuntimeError):
            # Telemetry-only path: a logger failure must never abort
            # engine selection. Narrowed to the real failure modes of
            # the logging stack (closed stderr / broken handler).
            pass

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
        request_kwargs = {
            "timeout": _HEAD_PROBE_TIMEOUT_S,
            "allow_redirects": False,
            "headers": headers or None,
        }
        proxies = requests_proxies()
        if proxies:
            request_kwargs["proxies"] = proxies
        resp = _requests.head(
            pinned_url,
            **request_kwargs,
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


def _decide_from_context(context: EngineSelectContext, ext: str) -> Optional[tuple[str, str]]:
    """Map context.resource_type / context.mime to (engine_name, reason)."""
    if context.resource_type in {CTX_HLS, CTX_DASH}:
        return ENGINE_N_M3U8DL_RE, f"context.resource_type={context.resource_type}"

    if context.resource_type == CTX_SEGMENT:
        if context.master_url or context.media_url:
            return ENGINE_N_M3U8DL_RE, "context.resource_type=segment.playlist_hint"
        # A bare .ts URL manually pasted by the user may be a complete MPEG-TS
        # file, so only suppress automatic Aria2 routing when the segment came
        # from a page/sniffer context. Without page/source context, fall through
        # to the existing extension rules for backwards compatibility.
        if context.page_url or context.source != CTX_UNKNOWN:
            return ENGINE_YTDLP, "context.resource_type=segment.wait_for_playlist"
        return None

    mime_hit = _decide_from_mime(context.mime.lower(), ext)
    if mime_hit:
        engine, reason = mime_hit
        return engine, f"context.mime={reason}"

    if context.resource_type == CTX_DIRECT_VIDEO:
        return ENGINE_ARIA2, "context.resource_type=direct_video"

    if context.resource_type == CTX_PAGE:
        return ENGINE_YTDLP, "context.resource_type=page"

    return None


def select_engine(
    url: str,
    manual: Optional[str] = None,
    *,
    context: EngineSelectContext | None = None,
) -> EngineDecision:
    """Choose an engine for ``url`` and return the structured decision.

    Priority (Requirement 24.1-24.5 + context unification):

    1. ``manual`` — if provided, returned as-is with ``source="manual"``.
    2. ``context`` — resource_type / mime based decision when available.
    3. HEAD MIME probe (2s, SSRF-guarded) — HLS/DASH → N_m3u8DL-RE,
       direct video MIME → Aria2. Failures fall through.
    4. Extension match (query stripped) against HLS / direct extension
       tables loaded by :mod:`core.engine_rules_loader`.
    5. Live-platform host/path substring match → Streamlink.
    6. yt-dlp fallback.

    When the HEAD probe raises or is skipped but the final decision comes
    from the extension table, ``source`` is tagged ``"fallback_on_error"``
    and an ``engine_select=fallback`` telemetry warning is emitted.
    """
    # 1. Manual override — highest priority.
    if manual:
        name = manual.strip()
        if name:
            return EngineDecision(engine_name=name, source="manual", reason=None)

    # Build default context when none provided.
    if context is None:
        context = build_engine_select_context(url=url)

    # Parse the URL once for extension / live-platform / probe decisions.
    pure_url = _strip_query(url or "")
    ext = _path_extension(pure_url)

    # 2. Context-driven decision.
    context_hit = _decide_from_context(context, ext)
    if context_hit is not None:
        engine_name, reason = context_hit
        return EngineDecision(engine_name=engine_name, source="context", reason=reason)

    # 3. HEAD MIME probe (SSRF-guarded, 2s).
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

    # 4. Extension match (query already stripped).
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

    # 5. Live-platform match.
    platform = _match_live_platform(url or "")
    if platform is not None:
        return EngineDecision(engine_name=ENGINE_STREAMLINK, source="live", reason=platform)

    # 6. yt-dlp fallback.
    return EngineDecision(engine_name=ENGINE_YTDLP, source="fallback", reason="default")


# Convenience alias so callers can opt into the explicit decision API
# without renaming imports.
def select_engine_decision(
    url: str,
    manual: Optional[str] = None,
    *,
    context: EngineSelectContext | None = None,
) -> EngineDecision:
    """Alias of :func:`select_engine` preserved for call-site readability."""
    return select_engine(url, manual=manual, context=context)


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

    def get_candidates(
        self,
        url: str,
        *,
        context: EngineSelectContext | None = None,
        include_generic_engines: bool = True,
    ) -> list[tuple[BaseEngine, str]]:
        """按优先级返回可用引擎列表"""
        if not self.engines:
            return []
        candidates = []
        seen_names: set[str] = set()

        is_segment_context = bool(context and context.resource_type == CTX_SEGMENT)

        # When context can unambiguously pick an engine, put it first. Segment
        # URLs without playlist hints intentionally resolve to yt-dlp rather
        # than Aria2 so automatic mode does not download a lone fragment as the
        # main video.
        if context:
            decision = select_engine(url, context=context)
            engine = self._engine_map.get(decision.engine_name)
            if engine:
                candidates.append((engine, decision.engine_name))
                seen_names.add(decision.engine_name)

        priority_order = self._get_priority_order()
        for engine_class in priority_order:
            if is_segment_context and engine_class is Aria2Engine:
                continue
            for engine in self.engines:
                engine_name = engine.get_name()
                if engine_name not in seen_names and isinstance(engine, engine_class) and self._safe_can_handle(engine, url):
                    candidates.append((engine, engine_name))
                    seen_names.add(engine_name)

        if include_generic_engines and not is_segment_context:
            # Keep custom/test engines and third-party adapters usable as
            # execution-stage fallback candidates even when they are not one
            # of the built-in priority classes.  The context/priority passes
            # above still decide the first choice, while this final pass
            # preserves legacy behaviour where any engine advertising
            # ``can_handle(url)`` can recover a failed manual or primary
            # engine attempt.
            for engine in self.engines:
                engine_name = engine.get_name()
                if engine_name not in seen_names and self._safe_can_handle(engine, url):
                    candidates.append((engine, engine_name))
                    seen_names.add(engine_name)

        if not candidates and self.engines:
            if is_segment_context:
                fallback = next(
                    (engine for engine in self.engines if not isinstance(engine, Aria2Engine)),
                    None,
                )
                if fallback is not None:
                    candidates.append((fallback, fallback.get_name()))
            else:
                fallback = self.engines[0]
                candidates.append((fallback, fallback.get_name()))
        return candidates

    def _select_safe_replacement_for_aria2(
        self,
        url: str,
        context: EngineSelectContext | None,
    ) -> tuple[BaseEngine, str]:
        """当手动 Aria2 不安全时，选择安全替代引擎。"""
        candidates = [
            (engine, name)
            for engine, name in self.get_candidates(
                url, context=context, include_generic_engines=False
            )
            if name != ENGINE_ARIA2
        ]
        if candidates:
            return candidates[0]

        for name in (ENGINE_N_M3U8DL_RE, ENGINE_YTDLP):
            engine = self._engine_map.get(name)
            if engine:
                return engine, name

        raise RuntimeError("当前资源不适合 Aria2，且没有可用的 HLS/通用下载引擎")

    def predict(
        self,
        url: str,
        user_preference: Optional[str] = None,
        *,
        context: EngineSelectContext | None = None,
    ) -> tuple[BaseEngine, str]:
        """
        预测探测阶段应显示的引擎。

        设计目标：
        - 用户显式指定时，优先反映该选择；
        - 只有在 can_handle 已明确返回 False 时，才不继续显示该显式引擎；
        - URL 信息不足、识别不完整时，不因为缺少候选就武断改成别的引擎。
        """
        if user_preference and user_preference in self._engine_map:
            # Aria2 在 HLS/MPD/带上下文 segment 场景下不安全：UI 预测阶段同步覆盖，
            # 避免界面显示 Aria2、实际执行却被切换造成困惑。
            if user_preference == ENGINE_ARIA2 and _aria2_is_unsafe_for_context(url, context):
                try:
                    engine, engine_name = self._select_safe_replacement_for_aria2(url, context)
                except RuntimeError:
                    engine, engine_name = self._engine_map[user_preference], user_preference
                logger.info(
                    TR("log_engine_predict_overridden"),
                    event="predict_aria2_overridden_for_context",
                    preferred_engine=user_preference,
                    predicted_engine=engine_name,
                    url=url,
                )
                return engine, engine_name

            preferred_engine = self._engine_map[user_preference]
            if self._safe_can_handle(preferred_engine, url):
                logger.info(f"{TR('log_engine_predict_user_pref')}: {user_preference}")
                return preferred_engine, user_preference

            auto_candidates = self.get_candidates(
                url,
                context=context,
                include_generic_engines=False,
            )
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

        candidates = self.get_candidates(url, context=context)
        if not candidates:
            raise RuntimeError("无可用下载引擎，请检查引擎配置或二进制文件")
        engine, engine_name = candidates[0]
        logger.info(f"{TR('log_engine_predict_auto')}: {engine_name}")
        return engine, engine_name

    def select(
        self,
        url: str,
        user_preference: Optional[str] = None,
        *,
        context: EngineSelectContext | None = None,
    ) -> tuple[BaseEngine, str]:
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
            # Aria2 在 HLS/MPD/带上下文 segment 场景下不安全：覆盖手动选择，
            # 防止误用 Aria2 下载单分片/manifest 后标记为完成。
            if user_preference == ENGINE_ARIA2 and _aria2_is_unsafe_for_context(url, context):
                try:
                    engine, engine_name = self._select_safe_replacement_for_aria2(url, context)
                except RuntimeError:
                    engine, engine_name = self._engine_map[user_preference], user_preference
                logger.warning(
                    "[selector] 手动 Aria2 被覆盖：当前资源不适合 Aria2",
                    event="manual_aria2_overridden_for_context",
                    preferred_engine=user_preference,
                    replacement_engine=engine_name,
                    resource_type=str(getattr(context, "resource_type", "") if context else ""),
                    source=str(getattr(context, "source", "") if context else ""),
                    url=url,
                )
                return engine, engine_name

            preferred_engine = self._engine_map[user_preference]
            logger.info(f"{TR('log_engine_use_user_pref')}: {user_preference}")
            return preferred_engine, user_preference

        # 2️⃣ 自动选择：按优先级匹配
        candidates = self.get_candidates(url, context=context)
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
