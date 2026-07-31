"""
Unified download context model and helpers.

Provides a frozen :class:`EngineSelectContext` dataclass that bundles all
metadata needed by the engine selector and download pipeline, together with
normalisation / inference helpers so that every entry-point (internal
browser, CatCatch, manual) can produce the same structured context.

Design principles:
    * All new fields carry sensible defaults — old callers keep compiling.
    * ``source`` / ``resource_type`` / ``mime`` are hints, not authorisation.
    * Headers are never mutated in-place by the context builders; real
      sanitisation remains in ``utils.headers.sanitize_headers`` and the
      sniffer / download-manager paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping, Optional
from urllib.parse import unquote, urlsplit

# ---------------------------------------------------------------------------
# Canonical source constants
# ---------------------------------------------------------------------------

SOURCE_CATCATCH = "catcatch"
SOURCE_INTERNAL_BROWSER = "internal_browser"
SOURCE_MANUAL = "manual"
SOURCE_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Canonical resource-type constants
# ---------------------------------------------------------------------------

RESOURCE_TYPE_HLS = "hls"
RESOURCE_TYPE_DASH = "dash"
RESOURCE_TYPE_DIRECT_VIDEO = "direct_video"
RESOURCE_TYPE_PAGE = "page"
RESOURCE_TYPE_SEGMENT = "segment"
RESOURCE_TYPE_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Recognised URL path suffixes
# ---------------------------------------------------------------------------

_DIRECT_VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".flv", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".m4v", ".f4v", ".3gp", ".mpg", ".mpeg",
})

_SEGMENT_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".m4s", ".aac", ".key",
})

_SEGMENT_PATTERN_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".m4s", ".aac", ".mp4",
})

_SEGMENT_NAME_RE = re.compile(
    r"^(?:seg(?:ment)?|frag(?:ment)?|chunk|part|slice)[-_.]?\d+(?:[-_.]\d+)*$",
    re.IGNORECASE,
)

_INIT_SEGMENT_NAMES: frozenset[str] = frozenset({
    "init",
    "init-segment",
    "init_segment",
    "init-stream",
    "init_stream",
})

_SEGMENT_PATH_MARKERS: tuple[str, ...] = (
    "/segment/",
    "/segments/",
    "/chunk/",
    "/chunks/",
    "/frag/",
    "/fragments/",
    "/dash/",
)


# ---------------------------------------------------------------------------
# EngineSelectContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineSelectContext:
    """Immutable bundle of metadata available at engine-selection time.

    Every field beyond ``url`` carries a default, so old callers that only
    provide a URL continue to work unchanged.
    """

    url: str
    page_url: str = ""
    page_title: str = ""
    source: str = SOURCE_UNKNOWN
    resource_type: str = RESOURCE_TYPE_UNKNOWN
    mime: str = ""
    headers: Mapping[str, str] | None = None
    master_url: Optional[str] = None
    media_url: Optional[str] = None
    metadata: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_ALLOWED_SOURCE_VALUES: frozenset[str] = frozenset({
    SOURCE_CATCATCH,
    SOURCE_INTERNAL_BROWSER,
    SOURCE_MANUAL,
    SOURCE_UNKNOWN,
})


def normalize_source(source: str | None) -> str:
    """Return a canonical source string.

    * ``None`` / empty → ``SOURCE_UNKNOWN``.
    * Input is lower-cased.
    * Values outside the allow-list are returned as ``"unknown"``.
    """
    if not source:
        return SOURCE_UNKNOWN
    key = source.strip().lower()
    if key in _ALLOWED_SOURCE_VALUES:
        return key
    return SOURCE_UNKNOWN


def infer_resource_type(
    url: str,
    *,
    mime: str = "",
    headers: Mapping[str, str] | None = None,
) -> str:
    """Infer a canonical resource type from URL, MIME and optional headers.

    Rules (first match wins):
    1. MIME contains ``"mpegurl"`` → ``hls``
    2. MIME contains ``"dash+xml"`` → ``dash``
    3. URL contains ``.m3u8`` → ``hls``
    4. URL contains ``.mpd`` → ``dash``
    5. URL path suffix / path pattern identifies an HLS/DASH segment
       (``.ts``, ``.m4s``, ``.aac``, ``.key``, ``chunk_001.m4s``,
       ``init.mp4`` etc.) → ``segment``
    6. URL path suffix is a known direct-video extension (``.mp4`` etc.) →
       ``direct_video``
    7. MIME starts with ``"video/"`` → ``direct_video``
    8. Otherwise → ``unknown``
    """
    mime_lower = (mime or "").strip().lower()
    url_lower = (url or "").lower()

    # 1 & 2 — MIME-based checks
    if mime_lower:
        if "mpegurl" in mime_lower:
            return RESOURCE_TYPE_HLS
        if "dash+xml" in mime_lower:
            return RESOURCE_TYPE_DASH

    # 3 & 4 — URL substring checks
    if ".m3u8" in url_lower:
        return RESOURCE_TYPE_HLS
    if ".mpd" in url_lower:
        return RESOURCE_TYPE_DASH

    # 5 & 6 — path-suffix / segment-pattern checks. Segment patterns are
    # evaluated before direct-video extensions so HLS/DASH init fragments such
    # as ``init.mp4`` do not get promoted to a downloadable full video.
    suffix = _path_suffix(url or "")
    segment_like = _looks_like_segment_url(url or "")
    if suffix:
        if suffix in _SEGMENT_EXTENSIONS or segment_like:
            return RESOURCE_TYPE_SEGMENT
        if suffix in _DIRECT_VIDEO_EXTENSIONS:
            return RESOURCE_TYPE_DIRECT_VIDEO
    elif segment_like:
        return RESOURCE_TYPE_SEGMENT

    # 7 — MIME starting with ``video/``
    if mime_lower.startswith("video/"):
        return RESOURCE_TYPE_DIRECT_VIDEO

    return RESOURCE_TYPE_UNKNOWN


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def build_engine_select_context(
    *,
    url: str,
    page_url: str = "",
    page_title: str = "",
    headers: Mapping[str, str] | None = None,
    source: str | None = None,
    resource_type: str | None = None,
    mime: str = "",
    master_url: str | None = None,
    media_url: str | None = None,
    metadata: Mapping[str, str] | None = None,
) -> EngineSelectContext:
    """Build a normalised :class:`EngineSelectContext`.

    * ``source`` is run through :func:`normalize_source`.
    * ``resource_type`` defaults to the result of :func:`infer_resource_type`
      when empty/``None``.
    * ``page_url`` / ``page_title`` / ``mime`` are stripped.
    * The caller's ``headers`` dict/mapping is **not** mutated.
    """
    normalized_source = normalize_source(source)
    stripped_mime = (mime or "").strip().lower()

    rt = (resource_type or "").strip().lower()
    if not rt or rt == RESOURCE_TYPE_UNKNOWN:
        rt = infer_resource_type(url, mime=stripped_mime, headers=headers)

    ctx_headers: Mapping[str, str] | None = None
    if headers is not None:
        ctx_headers = dict(headers)

    return EngineSelectContext(
        url=url,
        page_url=(page_url or "").strip(),
        page_title=(page_title or "").strip(),
        source=normalized_source,
        resource_type=rt,
        mime=stripped_mime,
        headers=ctx_headers,
        master_url=master_url or None,
        media_url=media_url or None,
        metadata=dict(metadata) if metadata else {},
    )


def context_from_resource(resource) -> EngineSelectContext:
    """Build an :class:`EngineSelectContext` from an :class:`M3U8Resource`.

    Uses duck-typing / ``getattr`` to avoid a circular import of
    ``core.task_model``.  Every attribute falls back to a sensible default
    so this function is safe to call on any resource-like object.
    """
    url = getattr(resource, "url", "")
    return build_engine_select_context(
        url=url,
        page_url=getattr(resource, "page_url", "") or "",
        page_title=getattr(resource, "page_title", "") or "",
        headers=getattr(resource, "headers", None),
        source=getattr(resource, "source", None),
        resource_type=getattr(resource, "resource_type", None),
        mime=getattr(resource, "mime", "") or "",
        master_url=getattr(resource, "master_url", None),
        media_url=getattr(resource, "media_url", None),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalized_path(url: str) -> str:
    """Return a decoded lower-cased URL path without query/fragment."""
    stripped = (url or "").strip()
    if not stripped:
        return ""

    try:
        parts = urlsplit(stripped)
        path = parts.path or stripped
    except ValueError:
        path = stripped

    for sep in ("?", "#"):
        idx = path.find(sep)
        if idx >= 0:
            path = path[:idx]

    try:
        path = unquote(path)
    except Exception:
        pass
    return path.lower()


def _looks_like_segment_url(url: str) -> bool:
    """Return True when a URL path looks like an HLS/DASH segment/key."""
    path = _normalized_path(url)
    if not path:
        return False

    suffix = _path_suffix(path)
    name = path.rsplit("/", 1)[-1]
    stem = name[: -len(suffix)] if suffix else name

    if suffix in _SEGMENT_EXTENSIONS:
        return True

    if suffix == ".mp4" and stem in _INIT_SEGMENT_NAMES:
        return True

    if suffix in _SEGMENT_PATTERN_EXTENSIONS and _SEGMENT_NAME_RE.match(stem):
        return True

    if suffix in _SEGMENT_PATTERN_EXTENSIONS and any(marker in path for marker in _SEGMENT_PATH_MARKERS):
        if any(ch.isdigit() for ch in stem) or stem in _INIT_SEGMENT_NAMES:
            return True

    return False


def _path_suffix(url: str) -> str:
    """Return the lower-cased path suffix of *url* (e.g. ``.mp4``, ``.ts``).

    Returns ``""`` when no suffix is present or the suffix is too long to be
    a plausible extension (>10 chars).
    """
    stripped = (url or "").strip()
    if not stripped:
        return ""

    # Isolate the path component before any query / fragment.
    pure = stripped
    for sep in ("?", "#"):
        idx = pure.find(sep)
        if idx >= 0:
            pure = pure[:idx]

    # Walk backwards from the end of the path.
    dot = pure.rfind(".")
    if dot < 0:
        return ""
    slash = pure.rfind("/")
    if dot < slash:
        return ""
    suffix = pure[dot:].lower()
    if len(suffix) > 10:
        return ""
    return suffix
