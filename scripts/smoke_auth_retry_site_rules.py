"""Offline smoke test for auth retry via site rules.

The script drives ``DownloadManager._execute_download`` with a fake engine that
fails once with an auth-shaped 403, then succeeds after matching site rules add
Cookie/Authorization/Referer headers for the same engine retry.
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


_SITE_RULE = {
    "name": "smoke-auth-rule",
    "enabled": True,
    "priority": 100,
    "domains": ["cdn.example.test"],
    "path_patterns": ["*/secure/*"],
    "apply_to": ["hls"],
    "referer": "https://www.example.test/watch/secure",
    "user_agent": "SmokeBrowser/1.0",
    "headers": {
        "Cookie": "session=ok",
        "Authorization": "Bearer ok",
        "Origin": "https://www.example.test",
        "Accept-Language": "zh-CN,zh;q=0.9",
    },
    "allow_authorization": True,
}


class _AuthRetryEngine(BaseEngine):
    def __init__(self) -> None:
        super().__init__(binary_path="dummy")
        self.calls = 0
        self.header_snapshots: list[dict[str, Any]] = []

    def download(self, task: DownloadTask, progress_callback) -> bool:
        self.calls += 1
        self.header_snapshots.append(dict(task.headers or {}))
        if (
            task.headers.get("cookie") == "session=ok"
            and task.headers.get("authorization") == "Bearer ok"
            and task.headers.get("_allow_authorization_header") is True
        ):
            progress_callback({"progress": 100.0, "speed": "", "downloaded": ""})
            return True
        task.error_message = "HTTP 403 Forbidden: authentication required"
        return False

    def parse_progress(self, line: str) -> dict:
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url: str) -> bool:
        return True

    def get_name(self) -> str:
        return "AuthRetrySmoke"


def _fake_config_get(key: str, default: Any = None) -> Any:
    values = {
        "max_retry_attempts": 0,
        "retry_backoff_seconds": 0,
        "features": {
            "download_retry_enabled": False,
            "download_engine_fallback": False,
            "hls_probe_enabled": False,
            "hls_probe_hard_fail": True,
            "download_candidate_ranking_enabled": False,
            "download_auth_retry_first": True,
            "download_auth_retry_per_engine": 1,
            "download_rate_limit_backoff_multiplier": 3,
            "forward_cookie_headers": True,
            "forward_authorization_headers": False,
        },
        "site_rules": [_SITE_RULE],
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
        url="https://cdn.example.test/secure/master.m3u8",
        save_dir=str(PROJECT_ROOT / "Temp"),
        filename="auth_retry_site_rules",
        headers={"referer": "https://www.example.test/watch/secure"},
        page_url="https://www.example.test/watch/secure",
        resource_type=RESOURCE_TYPE_HLS,
    )


def assert_auth_retry_uses_matching_site_rule() -> None:
    engine = _AuthRetryEngine()
    manager = DownloadManager([engine], max_concurrent=0)
    task = _make_task()
    task.engine = engine.get_name()

    try:
        manager._execute_download(task, engine, user_specified=False)

        assert task.status == "completed"
        assert task in manager.completed_tasks
        assert engine.calls == 2
        assert len(engine.header_snapshots) == 2

        first_headers, second_headers = engine.header_snapshots
        assert "cookie" not in first_headers
        assert "authorization" not in first_headers
        assert second_headers["cookie"] == "session=ok"
        assert second_headers["authorization"] == "Bearer ok"
        assert second_headers["_allow_authorization_header"] is True
        assert second_headers["referer"] == "https://www.example.test/watch/secure"
        assert second_headers["origin"] == "https://www.example.test"
    finally:
        manager.shutdown()


def main() -> None:
    restore = _patch_environment()
    try:
        assert_auth_retry_uses_matching_site_rule()
    finally:
        restore()
    print("smoke_auth_retry_site_rules: OK")


if __name__ == "__main__":
    main()
