"""Temp-cache cleanup helpers.

security-stability-hardening Task 28.2:

Historically the only entry point here was :func:`clear_cache`, a standalone
CLI that ``shutil.rmtree``'d everything under ``config["temp_dir"]``. That was
safe for the script context but too aggressive to trigger from the GUI on
every ``add_task`` call — it would wipe downloaded but not-yet-merged
fragments belonging to *other* paused/running tasks.

This module now exposes :func:`clean_temp_cache`, a library-style function
that the new "清空临时文件 / Clear Temp Files" toolbar entry in
``ui/main_window.py`` calls. Callers can pass ``skip_filenames`` to preserve
any per-task artifact whose fragment dir is named after a currently running
download, keeping the partial fragments intact.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Ensure we can import from the parent directory when this module is used as
# a stand-alone script.
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from utils.config_manager import config
except ImportError:  # pragma: no cover - defensive for CLI usage
    print("Error: Could not import utils.config_manager.")
    sys.exit(1)


# Subdirectories of ``temp_dir`` that are known to belong to download engines.
# When the GUI triggers a cleanup we only prune entries *inside* these
# subdirs (and never the subdirs themselves) so a future engine launch can
# recreate its scratch area without recreating the directory tree.
_ENGINE_TEMP_SUBDIRS: tuple[str, ...] = (
    "n_m3u8dl",
    "ffmpeg",
    "aria2",
    "streamlink",
    "ytdlp",
)


@dataclass
class CleanTempResult:
    """Summary of a :func:`clean_temp_cache` run."""

    temp_dir: str = ""
    existed: bool = False
    files_removed: int = 0
    bytes_removed: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def human_size(self) -> str:
        b = self.bytes_removed
        if b > 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024 * 1024):.2f} GB"
        if b > 1024 * 1024:
            return f"{b / (1024 * 1024):.2f} MB"
        if b > 1024:
            return f"{b / 1024:.2f} KB"
        return f"{b} B"


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                # Entry may vanish mid-walk; size accounting is best-effort.
                pass
    return total


def _artifact_matches_skip(entry_name: str, skip_tokens: Iterable[str]) -> bool:
    """Conservative token match so we don't nuke an active task's scratch dir.

    Token matching mirrors the logic that ``ui/download_queue.py`` uses for
    per-task cleanup: if the entry name starts with, equals, or embeds one of
    the running task's filenames (case-insensitive), treat it as owned by
    that task and leave it alone.
    """

    lowered = entry_name.lower()
    for token in skip_tokens:
        if not token:
            continue
        t = token.lower()
        if lowered == t or lowered.startswith(t) or t in lowered:
            return True
    return False


def _prune_entries(parent: Path, skip_tokens: Iterable[str], result: CleanTempResult) -> None:
    """Remove entries inside ``parent`` unless an active task owns them."""

    for item in parent.iterdir():
        if _artifact_matches_skip(item.name, skip_tokens):
            result.skipped.append(str(item))
            continue
        try:
            if item.is_dir():
                result.bytes_removed += _dir_size(item)
                shutil.rmtree(item)
            else:
                try:
                    result.bytes_removed += item.stat().st_size
                except OSError:
                    pass
                item.unlink()
            result.files_removed += 1
        except OSError as exc:
            result.errors.append(f"{item.name}: {exc}")


def clean_temp_cache(
    *,
    skip_filenames: Iterable[str] = (),
    temp_dir: str | os.PathLike | None = None,
) -> CleanTempResult:
    """Clean engine scratch dirs under ``temp_dir`` without touching active tasks.

    Parameters
    ----------
    skip_filenames:
        Iterable of filenames (as set on ``DownloadTask.filename``) for tasks
        that are currently running/paused. Any entry inside the engine temp
        subdirs whose name matches one of these is preserved — this is what
        keeps a paused download's fragments alive across a cleanup run.
    temp_dir:
        Override for the temp root; defaults to ``config["temp_dir"]``. The
        main-window entry point passes the configured value.

    Returns
    -------
    CleanTempResult
        Counters + skipped/error lists that the UI surfaces in a status
        message.
    """

    target = temp_dir if temp_dir is not None else config.get("temp_dir")
    result = CleanTempResult(temp_dir=str(target) if target else "")
    if not target:
        result.errors.append("temp_dir not configured")
        return result

    temp_path = Path(target)
    result.existed = temp_path.exists()
    if not result.existed:
        return result

    skip_tokens = tuple(t for t in skip_filenames if t)

    # Prune known engine subdirs (keep the subdir itself so engines don't
    # have to recreate their scratch-area layout on the next run).
    for sub in _ENGINE_TEMP_SUBDIRS:
        sub_path = temp_path / sub
        if sub_path.exists() and sub_path.is_dir():
            _prune_entries(sub_path, skip_tokens, result)

    # Also prune stray loose files sitting directly in temp_dir itself —
    # those are not keyed to a task and are safe to remove regardless.
    for item in temp_path.iterdir():
        if item.is_dir():
            # Directories other than the known engine subdirs might be
            # caller-owned (e.g. per-task workspaces created elsewhere); we
            # still skip anything that token-matches an active task.
            if item.name in _ENGINE_TEMP_SUBDIRS:
                continue
            if _artifact_matches_skip(item.name, skip_tokens):
                result.skipped.append(str(item))
                continue
            try:
                result.bytes_removed += _dir_size(item)
                shutil.rmtree(item)
                result.files_removed += 1
            except OSError as exc:
                result.errors.append(f"{item.name}: {exc}")
        else:
            if _artifact_matches_skip(item.name, skip_tokens):
                result.skipped.append(str(item))
                continue
            try:
                try:
                    result.bytes_removed += item.stat().st_size
                except OSError:
                    pass
                item.unlink()
                result.files_removed += 1
            except OSError as exc:
                result.errors.append(f"{item.name}: {exc}")

    return result


def clear_cache() -> None:
    """CLI entry point — preserved for backwards compat.

    Unlike :func:`clean_temp_cache`, the CLI wipes the entire contents of
    ``temp_dir`` regardless of any running GUI task, matching the historical
    behaviour of this script.
    """

    print("=" * 40)
    print("M3U8 Video Sniffer - Cache Cleaner")
    print("=" * 40)

    try:
        temp_dir = config.get("temp_dir")
        if not temp_dir:
            print("[!] Temp directory not configured in config.json")
            return

        temp_path = Path(temp_dir)
        print(f"[*] Target Directory: {temp_path}")

        if not temp_path.exists():
            print("[-] Directory does not exist. Nothing to clean.")
            return

        print("[*] Cleaning...")

        files_removed = 0
        bytes_removed = 0

        for item in temp_path.iterdir():
            try:
                if item.is_dir():
                    bytes_removed += _dir_size(item)
                    shutil.rmtree(item)
                else:
                    try:
                        bytes_removed += item.stat().st_size
                    except OSError:
                        pass
                    item.unlink()
                files_removed += 1
            except Exception as e:
                print(f"[!] Failed to delete {item.name}: {e}")

        print(f"[+] Done! Cleaned {files_removed} items.")

        if bytes_removed > 1024 * 1024 * 1024:
            size_str = f"{bytes_removed / (1024 * 1024 * 1024):.2f} GB"
        elif bytes_removed > 1024 * 1024:
            size_str = f"{bytes_removed / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{bytes_removed / 1024:.2f} KB"

        print(f"[+] Freed approximately {size_str}")

    except Exception as e:
        print(f"[!] Error during cleanup: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    clear_cache()
    print("\nPress any key to exit...")
    os.system("pause >nul")
