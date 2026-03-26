"""
Command-line entry for downloading runtime dependencies.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.app_paths import initialize_runtime_directories
from core.dependency_installer import (
    DependencyInstallBatchResult,
    DependencyInstallRunResult,
    DependencyProgressEvent,
    install_dependency_categories,
)
from core.dependency_manifest import CATEGORY_LABELS, load_dependency_manifest
from utils.logger import logger

EXIT_SUCCESS = 0
EXIT_INSTALL_FAILED = 1
EXIT_USAGE_ERROR = 2
EXIT_RUNTIME_ERROR = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="下载 M3U8D 依赖。默认仅下载必须依赖。"
    )
    parser.add_argument(
        "--include-recommended",
        action="store_true",
        help="同时下载建议依赖。",
    )
    return parser.parse_args(argv)


def resolve_categories(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve install categories from CLI options."""
    categories = ["required"]
    if args.include_recommended:
        categories.append("recommended")
    return tuple(categories)


def configure_cli_logging() -> None:
    """Keep CLI output concise while preserving file logs."""
    for handler in logger.logger.handlers:
        if isinstance(handler, logging.FileHandler):
            continue
        if isinstance(handler, logging.StreamHandler):
            handler.setLevel(logging.WARNING)


def format_size(byte_count: int | None) -> str:
    """Format byte count for CLI output."""
    if byte_count is None:
        return "未知大小"

    units = ["B", "KB", "MB", "GB"]
    size = float(byte_count)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def print_planned_dependencies(categories: tuple[str, ...]) -> None:
    """Print dependency plan before installation starts."""
    manifest = load_dependency_manifest()
    print(f"[PLAN] categories={','.join(categories)}")
    for category in categories:
        entries = manifest.get_entries(category)
        category_label = CATEGORY_LABELS.get(category, category)
        print(f"[PLAN] {category_label} count={len(entries)}")
        if not entries:
            print(f"[PLAN]   - 无")
            continue
        for index, entry in enumerate(entries, start=1):
            print(f"[PLAN]   {index}. {entry.label} ({entry.relative_path})")


def print_progress_event(event: DependencyProgressEvent) -> None:
    """Print installer-friendly progress lines."""
    prefix = f"[{event.category_label} {event.current_index}/{event.total_count}]"
    label = event.label or event.entry_id or "未知依赖"

    if event.event == "item_started":
        print(f"[ITEM] {prefix} 开始下载: {label}")
        return

    if event.event == "item_completed":
        print(f"[ITEM] {prefix} 已完成: {label}")
        return

    if event.event == "item_skipped":
        detail = f" ({event.detail})" if event.detail else ""
        print(f"[ITEM] {prefix} 已跳过: {label}{detail}")
        return

    if event.event == "item_failed":
        detail = f" ({event.detail})" if event.detail else ""
        print(f"[ITEM] {prefix} 失败: {label}{detail}")
        return

    if event.event == "item_progress":
        downloaded_text = format_size(event.bytes_downloaded)
        total_text = format_size(event.total_bytes)
        if event.total_bytes:
            percent = min(100, int((event.bytes_downloaded or 0) * 100 / event.total_bytes))
            print(f"[PROGRESS] {prefix} {label}: {downloaded_text} / {total_text} ({percent}%)")
        else:
            print(f"[PROGRESS] {prefix} {label}: 已下载 {downloaded_text}")


def print_batch_summary(batch_result: DependencyInstallBatchResult) -> None:
    """Print one batch summary line."""
    print(
        "[SUMMARY]"
        f" category={batch_result.category}"
        f" requested={batch_result.requested_count}"
        f" success={batch_result.success_count}"
        f" skipped={batch_result.skipped_count}"
        f" failed={batch_result.failed_count}"
    )


def print_run_summary(run_result: DependencyInstallRunResult) -> None:
    """Print aggregated result summary."""
    print(
        "[SUMMARY]"
        f" categories={','.join(run_result.categories)}"
        f" requested={run_result.requested_count}"
        f" success={run_result.success_count}"
        f" skipped={run_result.skipped_count}"
        f" failed={run_result.failed_count}"
    )
    for batch_result in run_result.batch_results:
        print_batch_summary(batch_result)
    for error_message in run_result.get_error_messages():
        print(f"[ERROR] {error_message}")


def main(argv: list[str] | None = None) -> int:
    """Run the dependency download CLI."""
    args = parse_args(argv)
    categories = resolve_categories(args)

    configure_cli_logging()
    initialize_runtime_directories()

    print_planned_dependencies(categories)

    try:
        run_result = install_dependency_categories(
            categories=categories,
            progress_callback=print_progress_event,
        )
    except Exception as exc:
        print(
            "[SUMMARY]"
            f" categories={','.join(categories)}"
            " requested=0 success=0 skipped=0 failed=0"
        )
        print(f"[ERROR] 依赖下载入口执行失败: {exc}")
        return EXIT_RUNTIME_ERROR

    print_run_summary(run_result)
    return EXIT_SUCCESS if run_result.ok else EXIT_INSTALL_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
