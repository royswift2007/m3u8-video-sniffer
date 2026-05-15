"""Engine executable path validation and trust registry.

This module implements Requirement 7 of ``security-stability-hardening``:
the engine ``*.exe`` path loaded from ``config.json`` must fall under a
controlled set of roots (``bin/`` or ``_MEIPASS/bin/`` when frozen) or
be listed in ``engine_paths_trusted.json`` with a matching sha256.

The public surface is intentionally small:

* :func:`validate_engine_exe` — pure-ish validator returning the resolved
  :class:`~pathlib.Path` on success or ``None`` on rejection.
* :func:`is_engine_path_trusted` — query the trust registry.
* :func:`add_engine_path_trust` — record a user-approved path.
* :func:`reconcile_trusted_registry` — drop entries whose sha256 no longer
  matches the on-disk binary. Called once at startup.
* :func:`find_engine_on_path` — PATH fallback used only when the default
  ``bin/`` binary is missing; rejects non-``.exe`` hits.

The module performs filesystem reads (``resolve`` / ``is_file`` / sha256)
but never executes a subprocess and never mutates ``config.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.app_paths import (
    SAFE_ENGINE_ROOTS,
    get_engine_paths_trusted_path,
    get_safe_engine_roots,
)
from utils.logger import logger


__all__ = (
    "validate_engine_exe",
    "is_engine_path_trusted",
    "add_engine_path_trust",
    "reconcile_trusted_registry",
    "find_engine_on_path",
    "compute_file_sha256",
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

# Process-wide lock guarding reads/writes of ``engine_paths_trusted.json``.
# The registry is tiny (expected <= a handful of entries) so a single lock
# is adequate and avoids file-lock cross-platform complexity.
_REGISTRY_LOCK = threading.RLock()


def _is_unc(path: Path) -> bool:
    """Return True for Windows UNC paths (``\\\\server\\share\\...``).

    ``Path`` on POSIX happily round-trips a leading double backslash as a
    regular segment, so we also guard against the string form directly.
    """
    try:
        s = str(path)
    except Exception:
        return False
    return s.startswith("\\\\") or s.startswith("//")


def _has_parent_traversal(raw: str | Path) -> bool:
    """Return True if the ORIGINAL (pre-resolve) path contains ``..``.

    ``Path(...).resolve()`` collapses ``..`` segments so the post-resolve
    form cannot carry them; we check the pre-resolve form to detect the
    attempt regardless of what the symlink graph happens to do.
    """
    return ".." in Path(raw).parts


def _path_is_under(path: Path, root: Path) -> bool:
    """Return True when ``path`` is the same as or a descendant of ``root``.

    Both sides are resolved (non-strict) so that:

    * symlinks inside ``path`` are followed — an attacker cannot smuggle a
      link whose target is outside any safe root;
    * the comparison is case-sensitive on POSIX and case-insensitive via
      ``Path`` behavior on Windows (which is what we want for drive
      letters and 8.3 names).
    """
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError:
        return False
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def compute_file_sha256(path: str | Path, *, chunk_size: int = 1 << 16) -> str | None:
    """Compute the sha256 hex digest of a file, or ``None`` if unreadable.

    Used both when seeding the trust registry and when re-checking an
    entry at startup. Failures (missing file, permission, IO error) are
    logged at DEBUG and turned into ``None`` so callers can treat them as
    "registry entry is no longer valid".
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError) as exc:
        logger.debug(f"compute_file_sha256 失败: {exc} ({path})")
        return None


# ---------------------------------------------------------------------------
# Trust registry I/O
# ---------------------------------------------------------------------------

def _load_registry() -> list[dict[str, Any]]:
    """Load ``engine_paths_trusted.json`` as a list of entries.

    Missing / unreadable / malformed files are treated as "no trusted
    entries" — a silent reset is safer than crashing the boot path.
    """
    registry_path = get_engine_paths_trusted_path()
    if not registry_path.exists():
        return []
    try:
        with open(registry_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"读取 engine_paths_trusted.json 失败，忽略既有条目: {exc}")
        return []

    if not isinstance(payload, list):
        logger.warning("engine_paths_trusted.json 顶层不是数组，忽略既有条目")
        return []

    clean: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        path = raw.get("path")
        sha256 = raw.get("sha256")
        added_at = raw.get("added_at")
        if not isinstance(path, str) or not path:
            continue
        if not isinstance(sha256, str) or len(sha256) != 64:
            continue
        if not isinstance(added_at, str):
            added_at = ""
        clean.append({"path": path, "sha256": sha256, "added_at": added_at})
    return clean


def _save_registry(entries: Iterable[Mapping[str, Any]]) -> None:
    """Atomically persist the trust registry."""
    # Import lazily to avoid a cycle (``utils.json_store`` imports stdlib
    # only, but keeping the import here makes the hot path — validation —
    # independent of the persistence module.)
    from utils.json_store import write_json_atomic

    payload = [
        {
            "path": e["path"],
            "sha256": e["sha256"],
            "added_at": e.get("added_at", ""),
        }
        for e in entries
    ]
    write_json_atomic(
        get_engine_paths_trusted_path(),
        payload,
        indent=2,
        ensure_ascii=False,
        write_backup=False,
    )


def _normalize_key(path: str | Path) -> str:
    """Return a stable string key for a registry path.

    We store the resolved absolute form so that ``C:\\bin\\x.exe`` and
    ``C:/bin/x.exe`` collapse to the same entry.
    """
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return str(path)


def is_engine_path_trusted(path: str | Path, current_sha: str | None = None) -> bool:
    """Return True when ``path`` has a matching trust registry entry.

    If ``current_sha`` is provided, the registry's recorded sha256 must
    equal it. When omitted, the sha256 is computed on the fly; a missing
    file (``compute_file_sha256`` returns ``None``) is treated as untrusted.
    """
    key = _normalize_key(path)
    with _REGISTRY_LOCK:
        for entry in _load_registry():
            if _normalize_key(entry["path"]) != key:
                continue
            live = current_sha if current_sha is not None else compute_file_sha256(path)
            if live is None:
                return False
            return live.lower() == entry["sha256"].lower()
    return False


def add_engine_path_trust(path: str | Path) -> bool:
    """Record ``path`` as a user-approved engine exe.

    The sha256 is computed from the current on-disk contents; a missing
    or unreadable file is rejected (returns ``False``). Adding an entry
    that already exists with the same sha256 is a no-op.
    """
    resolved = Path(path).resolve(strict=False)
    sha256 = compute_file_sha256(resolved)
    if sha256 is None:
        logger.warning(f"add_engine_path_trust 拒绝：无法读取 {resolved}")
        return False

    with _REGISTRY_LOCK:
        entries = _load_registry()
        key = _normalize_key(resolved)
        for entry in entries:
            if _normalize_key(entry["path"]) == key:
                if entry["sha256"].lower() == sha256.lower():
                    return True
                entry["sha256"] = sha256
                entry["added_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                _save_registry(entries)
                return True
        entries.append(
            {
                "path": str(resolved),
                "sha256": sha256,
                "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        try:
            _save_registry(entries)
        except OSError as exc:
            logger.error(f"保存 engine_paths_trusted.json 失败: {exc}")
            return False
    return True


def reconcile_trusted_registry() -> int:
    """Recompute sha256 for each trusted entry and drop the mismatches.

    Returns the number of entries that were evicted; ``0`` when every
    entry still matches. Called once from application startup.
    """
    with _REGISTRY_LOCK:
        entries = _load_registry()
        if not entries:
            return 0
        kept: list[dict[str, Any]] = []
        dropped = 0
        for entry in entries:
            live = compute_file_sha256(entry["path"])
            if live is not None and live.lower() == entry["sha256"].lower():
                kept.append(entry)
            else:
                dropped += 1
                logger.warning(
                    "engine_paths_trusted.json 条目 sha256 不一致，已移出 trusted: "
                    f"{entry['path']}"
                )
        if dropped:
            try:
                _save_registry(kept)
            except OSError as exc:
                logger.error(f"写回 engine_paths_trusted.json 失败: {exc}")
        return dropped


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_engine_exe(path: str | Path) -> Path | None:
    """Validate a path recorded in ``config.json::engines.*.path``.

    Returns the resolved :class:`~pathlib.Path` when every check passes,
    otherwise ``None``. Checks performed (all must pass):

    1. Non-empty input and conversion to :class:`Path` does not raise.
    2. Extension is ``.exe`` (case-insensitive).
    3. Pre-resolve form does not contain ``..`` segments.
    4. Not a UNC / ``\\\\server\\share`` path.
    5. Either (a) the resolved path lives under one of the safe roots
       returned by :func:`core.app_paths.get_safe_engine_roots` *or*
       (b) the trust registry has a matching sha256 entry.
    6. The target file exists and is a regular file.

    The caller is responsible for handling ``None`` — in practice,
    :class:`ConfigManager` clears the offending field, falls back to the
    default ``bin/<engine>.exe`` path, and records an
    ``engine_path_tampered`` warning.
    """
    if path is None:
        return None
    try:
        raw = str(path)
    except Exception:
        return None
    if not raw:
        return None

    # Reject pre-resolve traversal + UNC before touching the filesystem.
    if _has_parent_traversal(raw):
        return None
    # ``Path.resolve`` on POSIX keeps leading ``//`` in some cases; check
    # raw + resolved form to catch both.
    if raw.startswith("\\\\") or raw.startswith("//"):
        return None

    try:
        resolved = Path(raw).resolve(strict=False)
    except (OSError, ValueError):
        return None

    if _is_unc(resolved):
        return None
    if resolved.suffix.lower() != ".exe":
        return None

    # Must refer to an existing regular file — otherwise defaults kick in.
    try:
        if not resolved.is_file():
            return None
    except OSError:
        return None

    # Roots are re-read via the function so tests that monkey-patch
    # ``sys._MEIPASS`` / ``sys.frozen`` see the refreshed tuple.
    roots = get_safe_engine_roots() or SAFE_ENGINE_ROOTS
    for root in roots:
        if _path_is_under(resolved, root):
            return resolved

    # Fall through to the user-authorized registry. ``is_engine_path_trusted``
    # recomputes sha256 so symlink / replacement attacks still fail closed.
    if is_engine_path_trusted(resolved):
        return resolved

    return None


# ---------------------------------------------------------------------------
# PATH fallback
# ---------------------------------------------------------------------------

def find_engine_on_path(executable_name: str) -> Path | None:
    """Return ``shutil.which(executable_name)`` iff the hit ends in ``.exe``.

    Used by :class:`ConfigManager` only when the default bundled binary
    is missing. Rejects:

    * empty / ``None`` input;
    * non-``.exe`` matches (protects against ``foo.bat`` / ``foo.cmd``);
    * UNC matches.
    """
    if not executable_name:
        return None
    hit = shutil.which(executable_name, mode=os.F_OK | os.X_OK)
    if not hit:
        return None
    candidate = Path(hit)
    if _is_unc(candidate):
        return None
    if candidate.suffix.lower() != ".exe":
        return None
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return resolved
