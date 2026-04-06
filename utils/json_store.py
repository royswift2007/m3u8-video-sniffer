"""
Helpers for durable JSON persistence and recovery.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def backup_path_for(path: str | Path) -> Path:
    """Return the sibling backup path for a JSON document."""
    file_path = Path(path)
    return file_path.with_name(f"{file_path.name}.bak")


def corrupt_path_for(path: str | Path) -> Path:
    """Return a timestamped quarantine path for a corrupted document."""
    file_path = Path(path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return file_path.with_name(f"{file_path.name}.corrupt-{timestamp}")


def write_json_atomic(
    path: str | Path,
    payload: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    write_backup: bool = True,
) -> None:
    """Atomically write JSON to the target path and optionally refresh its backup."""
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    _write_json_file(target_path, payload, indent=indent, ensure_ascii=ensure_ascii)
    if write_backup:
        _write_json_file(
            backup_path_for(target_path),
            payload,
            indent=indent,
            ensure_ascii=ensure_ascii,
        )


def _write_json_file(
    path: Path,
    payload: Any,
    *,
    indent: int,
    ensure_ascii: bool,
) -> None:
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)

    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=indent, ensure_ascii=ensure_ascii)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
