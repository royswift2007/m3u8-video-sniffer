"""
Engine rules loader.

Loads the ``DIRECT_EXTENSIONS / HLS_EXTENSIONS / LIVE_PLATFORMS`` rule sets
from ``resources/engine_rules.json`` once on first import and caches the
result. Designed to be read-only at runtime and non-breaking: existing
engine ``can_handle`` implementations continue to own their built-in
constants, and this loader only becomes authoritative once task 22.1
rewires ``select_engine`` to consume ``ENGINE_RULES`` directly.

Fallback behaviour:
- Missing file, malformed JSON, or wrong schema → log a WARN and return the
  built-in defaults harvested from the engines (see ``_DEFAULT_*`` below).
- Partially populated sections are filled from defaults so selection stays
  stable.

See:
    - engines/aria2_engine.py::Aria2Engine.DIRECT_EXTENSIONS
    - engines/n_m3u8dl_re.py::N_m3u8DL_RE_Engine.can_handle (.m3u8 / .mpd)
    - engines/streamlink_engine.py::StreamlinkEngine.LIVE_PLATFORMS
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.app_paths import get_resource_path
from utils.logger import logger


# ---------------------------------------------------------------------------
# Built-in defaults (harvested from the concrete engine implementations).
# These are the source of truth when resources/engine_rules.json is missing
# or unusable so selection behaviour never degrades.
# ---------------------------------------------------------------------------

_DEFAULT_DIRECT_EXTENSIONS: tuple[str, ...] = (
    ".mp4", ".flv", ".ts", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".m4v", ".3gp", ".mpg", ".mpeg", ".f4v",
)

_DEFAULT_HLS_EXTENSIONS: tuple[str, ...] = (
    ".m3u8",
    ".mpd",
)

_DEFAULT_LIVE_PLATFORMS: tuple[str, ...] = (
    "twitch.tv",
    "douyu.com",
    "huya.com",
    "youtube.com/live",
    "bilibili.com/live",
    "afreecatv.com",
    "mixer.com",
    "facebook.com/live",
)

ENGINE_RULES_RESOURCE: str = "engine_rules.json"


# ---------------------------------------------------------------------------
# Data class + normalization helpers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineRules:
    """Immutable snapshot of the engine selection rules."""

    direct_extensions: frozenset[str]
    hls_extensions: frozenset[str]
    live_platforms: tuple[str, ...]
    source: str  # "resource" | "defaults_missing_file" | "defaults_invalid_file" | ...
    path: str | None = None


def _normalize_extensions(items: Iterable) -> frozenset[str]:
    """Lower-case, strip and ensure a leading dot for each extension entry."""
    out: set[str] = set()
    for item in items or ():
        if not isinstance(item, str):
            continue
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        out.add(ext)
    return frozenset(out)


def _normalize_platforms(items: Iterable) -> tuple[str, ...]:
    """Lower-case, strip and de-duplicate platform match strings, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items or ():
        if not isinstance(item, str):
            continue
        host = item.strip().lower()
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return tuple(out)


def _build_defaults(source: str, path: str | None = None) -> EngineRules:
    return EngineRules(
        direct_extensions=_normalize_extensions(_DEFAULT_DIRECT_EXTENSIONS),
        hls_extensions=_normalize_extensions(_DEFAULT_HLS_EXTENSIONS),
        live_platforms=_normalize_platforms(_DEFAULT_LIVE_PLATFORMS),
        source=source,
        path=path,
    )


# ---------------------------------------------------------------------------
# Disk loader (single-shot, no caching; call through _load_engine_rules()).
# ---------------------------------------------------------------------------


def _load_engine_rules_from_disk() -> EngineRules:
    path: Path = get_resource_path(ENGINE_RULES_RESOURCE)
    path_str = str(path)

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.warning(
            f"[engine_rules] resource not found at {path_str}, falling back to built-in defaults"
        )
        return _build_defaults("defaults_missing_file", path_str)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning(
            f"[engine_rules] failed to read {path_str} ({type(exc).__name__}: {exc}); "
            "falling back to built-in defaults"
        )
        return _build_defaults("defaults_invalid_file", path_str)

    if not isinstance(data, dict):
        logger.warning(
            f"[engine_rules] {path_str} root is not a JSON object; falling back to built-in defaults"
        )
        return _build_defaults("defaults_invalid_schema", path_str)

    direct = _normalize_extensions(data.get("direct_extensions") or ())
    hls = _normalize_extensions(data.get("hls_extensions") or ())
    live = _normalize_platforms(data.get("live_platforms") or ())

    if not direct and not hls and not live:
        logger.warning(
            f"[engine_rules] {path_str} contained no usable rules; falling back to built-in defaults"
        )
        return _build_defaults("defaults_empty_file", path_str)

    # Partial schemas are tolerated: missing sections fall back to defaults so
    # one typo in the JSON cannot silently disable an entire engine path.
    defaults = _build_defaults("resource", path_str)
    return EngineRules(
        direct_extensions=direct or defaults.direct_extensions,
        hls_extensions=hls or defaults.hls_extensions,
        live_platforms=live or defaults.live_platforms,
        source="resource",
        path=path_str,
    )


# ---------------------------------------------------------------------------
# Module-level cache.
# ---------------------------------------------------------------------------


_cached_rules: EngineRules | None = None


def _load_engine_rules() -> EngineRules:
    """Return cached engine rules, loading from disk on first invocation."""
    global _cached_rules
    if _cached_rules is None:
        _cached_rules = _load_engine_rules_from_disk()
    return _cached_rules


def reload_engine_rules() -> EngineRules:
    """Force a re-read from disk. Intended for tests and diagnostic tools."""
    global _cached_rules
    _cached_rules = _load_engine_rules_from_disk()
    return _cached_rules


# Populate on import so startup pays the cost once and any fallback warning
# is emitted before the first download request touches the selector.
ENGINE_RULES: EngineRules = _load_engine_rules()

# Convenience aliases mirror the constants that live on the engine classes.
# Task 22.1 will wire ``select_engine`` against these; until then the engine
# ``can_handle`` methods remain authoritative and behaviour is unchanged.
DIRECT_EXTENSIONS: frozenset[str] = ENGINE_RULES.direct_extensions
HLS_EXTENSIONS: frozenset[str] = ENGINE_RULES.hls_extensions
LIVE_PLATFORMS: tuple[str, ...] = ENGINE_RULES.live_platforms


__all__ = [
    "EngineRules",
    "ENGINE_RULES",
    "ENGINE_RULES_RESOURCE",
    "DIRECT_EXTENSIONS",
    "HLS_EXTENSIONS",
    "LIVE_PLATFORMS",
    "reload_engine_rules",
]
