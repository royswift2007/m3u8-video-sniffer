"""Offline smoke test for rate-limit retry strategy.

The script drives ``DownloadManager._execute_download`` with a fake engine that
always reports HTTP 429.  It verifies failure classification and captures the
extended backoff timeout without sleeping.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core.download.manager as manager_module
from core.download.manager import DownloadManager
from core.download_context import RESOURCE_TYPE_HLS
from core.task_model import DownloadTask
from engines.base_engine import BaseEngine
import utils.config_manager as config_module


class _RateLimitedEngine(BaseEngine):
    def __init__(self) -> None:
        super().__init__(binary_path="dummy")
        self.calls = 0

    def download(self, task: DownloadTask, progress_callback) -> bool:
        self.calls += 1
        task.error_message = "HTTP 429 Too Many Requests while fetching segment"
        return False

    def parse_progress(self, line: str) -> dict:
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url: str) -> bool:
        return True

    def get_name(self) -> str:
        return "RateLimitSmoke"


def _fake_config_get(key: str, default: Any = None) -> Any:
    values = {
        "max_retry_attempts": 1,
        "retry_backoff_seconds": 2,
        "features": {
            "download_retry_enabled": True,
            "download_engine_fallback": False,
            "hls_probe_enabled": False,
            "hls_probe_hard_fail": True,
            "download_candidate_ranking_enabled": False,
            "download_auth_retry_first": False,
            "download_auth_retry_per_engine": 0,
            "download_rate_limit_backoff_multiplier": 4,
            "forward_cookie_headers": True,
            "forward_authorization_headers": False,
        },
        "site_rules": [],
        "site_rules_auto.enabled": False,
        "auto_delete_temp": False,
    }
    return values.get(key, default)


def _patch_environment() -> Callable[[], None]:
    original_config_get = config_module.config.get
    original_started = manager_module.notify_download_started
    original_completed = manager_module.notify_download_completed
    original_failed = manager_module.notify_download_failed

    config_module.config.get = _fake_config_get
    manager_module.notify_download_started = lambda *_args, **_kwargs: None
    manager_module.notify_download_completed = lambda *_args, **_kwargs: None
    manager_module.notify_download_failed = lambda *_args, **_kwargs: None

    def restore() -> None:
        config_module.config.get = original_config_get
        manager_module.notify_download_started = original_started
        manager_module.notify_download_completed = original_completed
        manager_module.notify_download_failed = original_failed

    return restore


def _make_task() -> DownloadTask:
    return DownloadTask(
        url="https://cdn.example.test/rate/master.m3u8",
        save_dir=str(PROJECT_ROOT / "Temp"),
        filename="rate_limit_strategy",
        headers={"referer": "https://www.example.test/watch/rate"},
        page_url="https://www.example.test/watch/rate",
        resource_type=RESOURCE_TYPE_HLS,
    )


def assert_rate_limit_uses_extended_backoff() -> None:
    engine = _RateLimitedEngine()
    manager = DownloadManager([engine], max_concurrent=0)
    task = _make_task()
    task.engine = engine.get_name()
    waits: list[float | None] = []
    original_wait = manager._stop_flag.wait

    def fake_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        return False

    manager._stop_flag.wait = fake_wait
    try:
        assert manager._classify_failure(task, "HTTP 429 Too Many Requests") == "rate_limit"

        manager._execute_download(task, engine, user_specified=False)

        assert task.status == "failed"
        assert task in manager.failed_tasks
        assert engine.calls == 2
        assert waits == [8]
        assert task.retry_count == 2

        metrics = manager.get_quality_metrics()
        assert metrics["by_reason"]["rate_limit"]["failed"] == 1
        assert metrics["by_retry_action"]["rate_limit_backoff"]["failed"] == 1
    finally:
        manager._stop_flag.wait = original_wait
        manager.shutdown()


def main() -> None:
    restore = _patch_environment()
    try:
        assert_rate_limit_uses_extended_backoff()
    finally:
        restore()
    print("smoke_rate_limit_strategy: OK")


if __name__ == "__main__":
    main()
