"""
Runtime log retention helpers.
"""

from __future__ import annotations

import logging
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


class CapacityManagedFileHandler(logging.FileHandler):
    def __init__(
        self,
        filename: str | Path,
        mode: str = "a",
        encoding: str | None = "utf-8",
        delay: bool = True,
        max_total_bytes: int = RUNTIME_LOG_LIMIT_BYTES,
    ):
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay)
        self.max_total_bytes = max_total_bytes
        self._log_dir = Path(self.baseFilename).parent

    def _close_stream_for_prune(self) -> None:
        if self.stream is None:
            return
        self.flush()
        self.stream.close()
        setattr(self, "stream", None)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            encoding = self.encoding or "utf-8"
            reserve_bytes = len(f"{message}{self.terminator}".encode(encoding, errors="replace"))
            self._close_stream_for_prune()
            self._log_dir.mkdir(parents=True, exist_ok=True)
            prune_runtime_logs(
                self._log_dir,
                limit_bytes=self.max_total_bytes,
                reserve_bytes=reserve_bytes,
            )
        except Exception:
            pass

        super().emit(record)
