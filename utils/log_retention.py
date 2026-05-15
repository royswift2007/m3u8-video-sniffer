"""
Runtime log retention helpers.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path


RUNTIME_LOG_LIMIT_BYTES = 50 * 1024 * 1024
_RUNTIME_LOG_PATTERNS = ("m3u8sniffer_*.log",)
_RUNTIME_LOG_NAMES = {"protocol_handler.log"}


def _oldest_first_key(path: Path) -> tuple[float, str]:
    try:
        stat = path.stat()
        return stat.st_mtime, path.name.lower()
    except OSError:
        return float("inf"), path.name.lower()


def iter_runtime_log_files(log_dir: Path) -> list[Path]:
    if not log_dir.exists():
        return []

    matched: dict[Path, None] = {}
    for pattern in _RUNTIME_LOG_PATTERNS:
        for path in log_dir.glob(pattern):
            if path.is_file():
                matched[path] = None

    for name in _RUNTIME_LOG_NAMES:
        path = log_dir / name
        if path.is_file():
            matched[path] = None

    return sorted(matched.keys(), key=_oldest_first_key)


def _prune_listing(
    files: list[Path],
    limit_bytes: int,
    reserve_bytes: int = 0,
) -> list[Path]:
    """Evict oldest log files from an already-known listing until under ``limit_bytes``.

    Accepts a pre-computed listing so the caller can reuse a cached ``glob`` result
    instead of re-scanning the directory on every call.
    """
    allowed_bytes = max(limit_bytes - reserve_bytes, 0)
    total_bytes = 0
    file_sizes: dict[Path, int] = {}

    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        file_sizes[path] = size
        total_bytes += size

    deleted: list[Path] = []
    for path in sorted(file_sizes, key=_oldest_first_key):
        if total_bytes <= allowed_bytes:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        total_bytes -= file_sizes[path]
        deleted.append(path)

    return deleted


def prune_runtime_logs(
    log_dir: Path,
    limit_bytes: int = RUNTIME_LOG_LIMIT_BYTES,
    reserve_bytes: int = 0,
) -> list[Path]:
    if limit_bytes < 0:
        raise ValueError("limit_bytes must be non-negative")
    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must be non-negative")

    files = iter_runtime_log_files(log_dir)
    return _prune_listing(files, limit_bytes=limit_bytes, reserve_bytes=reserve_bytes)


class CapacityManagedFileHandler(logging.FileHandler):
    """FileHandler that enforces a total-bytes budget on the log directory.

    Design notes (Requirement 13):
      - ``emit`` no longer closes + reopens the stream on every record. The
        stream stays open (standard ``logging.FileHandler`` behaviour) and
        capacity checks are throttled.
      - A rotation check runs when EITHER ``rotate_check_interval_n`` emits
        (default 1000) have accumulated OR ``rotate_check_interval_s`` seconds
        (default 5.0) have elapsed since the last check.
      - The directory listing is cached in ``_cached_listing`` and only
        recomputed when the parent directory's ``mtime`` changes.
      - Rotation failures are written once to ``stderr`` and swallowed so the
        logging pipeline can never crash the host process.
      - The default file-level is ``INFO``; setting ``M3U8D_LOG_DEBUG=1`` in the
        environment elevates it to ``DEBUG``.

    Constructor signature stays backwards-compatible with existing callers
    (``CapacityManagedFileHandler(log_file, encoding='utf-8')``).
    """

    def __init__(
        self,
        filename: str | Path,
        mode: str = "a",
        encoding: str | None = "utf-8",
        delay: bool = True,
        max_total_bytes: int = RUNTIME_LOG_LIMIT_BYTES,
        *,
        rotate_check_interval_n: int = 1000,
        rotate_check_interval_s: float = 5.0,
    ):
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay)
        self.max_total_bytes = max_total_bytes
        self._log_dir = Path(self.baseFilename).parent
        # Guard against nonsensical values without breaking callers.
        self._rotate_check_interval_n = max(1, int(rotate_check_interval_n))
        self._rotate_check_interval_s = max(0.0, float(rotate_check_interval_s))
        self._emit_count = 0
        self._last_check = time.monotonic()
        self._cached_listing: list[Path] | None = None
        self._cached_listing_mtime: float = -1.0

        # Default file level: INFO. Elevated to DEBUG only via env var.
        if os.environ.get("M3U8D_LOG_DEBUG") == "1":
            self.setLevel(logging.DEBUG)
        else:
            self.setLevel(logging.INFO)

    # ------------------------------------------------------------------ public
    def emit(self, record: logging.LogRecord) -> None:
        # Delegate to FileHandler; stream is kept open across emits.
        super().emit(record)
        self._emit_count += 1
        now = time.monotonic()
        if (
            self._emit_count >= self._rotate_check_interval_n
            or (now - self._last_check) >= self._rotate_check_interval_s
        ):
            self._emit_count = 0
            self._last_check = now
            self._maybe_rotate()

    # ----------------------------------------------------------------- private
    def _maybe_rotate(self) -> None:
        try:
            try:
                self._log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                # Parent directory unreachable; skip this check quietly.
                return

            try:
                parent_mtime = self._log_dir.stat().st_mtime
            except OSError:
                parent_mtime = -1.0

            if (
                self._cached_listing is None
                or parent_mtime != self._cached_listing_mtime
            ):
                self._cached_listing = iter_runtime_log_files(self._log_dir)
                self._cached_listing_mtime = parent_mtime

            deleted = _prune_listing(
                self._cached_listing,
                limit_bytes=self.max_total_bytes,
            )
            if deleted:
                # Directory contents changed; force a re-scan next round.
                self._cached_listing = None
                self._cached_listing_mtime = -1.0
        except Exception as exc:  # pragma: no cover - defensive
            try:
                sys.stderr.write(
                    f"CapacityManagedFileHandler rotate failed: {exc}\n"
                )
            except (OSError, ValueError):
                # Even stderr can fail (closed on exit); never propagate.
                pass
