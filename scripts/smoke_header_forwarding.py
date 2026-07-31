"""Offline smoke tests for download header forwarding policy.

The script validates the task-level header normalization that feeds all engine
command builders: browser context headers survive, Origin is derived from
Referer, Cookie follows the feature flag, and Authorization remains opt-in.
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

from core.download.manager import DownloadManager
from core.download_context import RESOURCE_TYPE_HLS
from core.task_model import DownloadTask
from engines.base_engine import BaseEngine
import utils.config_manager as config_module


class _DummyEngine(BaseEngine):
    def __init__(self) -> None:
        super().__init__(binary_path="dummy")

    def download(self, task: DownloadTask, progress_callback) -> bool:
        return True

    def parse_progress(self, line: str) -> dict:
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url: str) -> bool:
        return True

    def get_name(self) -> str:
        return "HeaderSmoke"


def _fake_config_get(key: str, default: Any = None) -> Any:
    values = {
        "features": {
            "forward_cookie_headers": True,
            "temporary_cookie_forwarding_enabled": True,
            "forward_authorization_headers": False,
        }
    }
    return values.get(key, default)


def _patch_config() -> Callable[[], None]:
    original_get = config_module.config.get
    config_module.config.get = _fake_config_get

    def restore() -> None:
        config_module.config.get = original_get

    return restore


def _make_task(filename: str, headers: dict[str, Any]) -> DownloadTask:
    return DownloadTask(
        url="https://cdn.example.test/video/master.m3u8",
        save_dir=str(PROJECT_ROOT / "Temp"),
        filename=filename,
        headers=headers,
        page_url="https://www.example.test/watch/1",
        resource_type=RESOURCE_TYPE_HLS,
    )


def assert_browser_context_headers_are_forwarded() -> None:
    manager = DownloadManager([_DummyEngine()], max_concurrent=0)
    try:
        task = _make_task(
            "header_forwarding",
            {
                "Referer": "https://www.example.test/watch/1",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": "sid=abc",
                "Authorization": "Bearer should-not-forward",
                "X-Unsafe": "drop-me",
            },
        )

        changed = manager._normalize_task_headers_for_download(task)

        assert changed is True
        assert task.headers["referer"] == "https://www.example.test/watch/1"
        assert task.headers["origin"] == "https://www.example.test"
        assert task.headers["accept-language"] == "zh-CN,zh;q=0.9"
        assert "cookie" not in task.headers
        assert task.headers["user-agent"]
        assert "authorization" not in task.headers
        assert "x-unsafe" not in task.headers
    finally:
        manager.shutdown()


def assert_temporary_cookie_authorization_keeps_same_site_cookie() -> None:
    manager = DownloadManager([_DummyEngine()], max_concurrent=0)
    try:
        task = _make_task(
            "temporary_cookie_allowed",
            {
                "Referer": "https://www.example.test/watch/1",
                "Cookie": "sid=abc",
            },
        )
        task.temporary_cookie_allowed = True
        task.cookie_policy_source = "ui_prompt"

        manager._normalize_task_headers_for_download(task)

        assert task.headers["cookie"] == "sid=abc"
        assert task.headers["referer"] == "https://www.example.test/watch/1"
        assert task.headers["origin"] == "https://www.example.test"
        assert task.cookie_policy_source == "ui_prompt"
    finally:
        manager.shutdown()


def assert_authorization_requires_explicit_opt_in() -> None:
    manager = DownloadManager([_DummyEngine()], max_concurrent=0)
    try:
        task = _make_task(
            "authorization_opt_in",
            {
                "Referer": "https://www.example.test/watch/1",
                "Authorization": "Bearer allowed",
                "_allow_authorization_header": True,
            },
        )

        manager._normalize_task_headers_for_download(task)

        assert task.headers["authorization"] == "Bearer allowed"
        assert task.headers["_allow_authorization_header"] is True
        assert task.headers["origin"] == "https://www.example.test"
    finally:
        manager.shutdown()


def main() -> None:
    restore = _patch_config()
    try:
        assert_browser_context_headers_are_forwarded()
        assert_temporary_cookie_authorization_keeps_same_site_cookie()
        assert_authorization_requires_explicit_opt_in()
    finally:
        restore()
    print("smoke_header_forwarding: OK")


if __name__ == "__main__":
    main()
