"""Offline smoke for progress attempt/source consistency.

Covers the progress-display P4 path without spawning external engines:

* same attempt/source progress is monotonic and cannot visibly roll back;
* a new attempt/source can reset progress while snapshots expose metadata;
* N_m3u8DL-RE progress callbacks carry their runtime source label;
* DownloadManager consumes the TaskQueue abstraction instead of queue.Queue internals.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.download.manager as manager_module
import utils.config_manager as config_module
from core.download.manager import DownloadManager
from core.download.task_queue import QueuedDownload, TaskQueue
from core.task_model import DownloadTask
from engines.base_engine import BaseEngine, EngineResult
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine


class _ProgressEngine(BaseEngine):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        super().__init__(binary_path="dummy")
        self.events = events
        self.observed: list[tuple[float, int, str]] = []

    def download(self, task: DownloadTask, progress_callback: Callable[[dict[str, Any]], None]) -> bool:
        for payload in self.events:
            progress_callback(payload)
            self.observed.append(
                (
                    float(getattr(task, "progress", 0.0) or 0.0),
                    int(getattr(task, "progress_attempt", 0) or 0),
                    str(getattr(task, "progress_source_label", "") or ""),
                )
            )
        return True

    def parse_progress(self, line: str) -> dict:
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url: str) -> bool:
        return True

    def get_name(self) -> str:
        return "ProgressSmoke"


def _make_task(filename: str) -> DownloadTask:
    return DownloadTask(
        url="https://example.test/video/index.m3u8",
        save_dir="C:\\Users\\qinghua\\Downloads",
        filename=filename,
        headers={},
        engine="ProgressSmoke",
    )


def _patch_environment() -> Callable[[], None]:
    original_config_get = config_module.config.get
    original_started = manager_module.notify_download_started
    original_completed = manager_module.notify_download_completed
    original_failed = manager_module.notify_download_failed

    def fake_get(key: str, default: Any = None) -> Any:
        values: dict[str, Any] = {
            "max_retry_attempts": 0,
            "retry_backoff_seconds": 0,
            "features": {
                "download_retry_enabled": False,
                "download_engine_fallback": False,
                "hls_probe_enabled": False,
                "download_candidate_ranking_enabled": False,
                "download_auth_retry_first": False,
                "download_auth_retry_per_engine": 0,
            },
            "site_rules_auto.enabled": False,
            "auto_delete_temp": False,
        }
        return values.get(key, default)

    config_module.config.get = fake_get  # type: ignore[method-assign]
    manager_module.notify_download_started = lambda *args, **kwargs: None
    manager_module.notify_download_completed = lambda *args, **kwargs: None
    manager_module.notify_download_failed = lambda *args, **kwargs: None

    def restore() -> None:
        config_module.config.get = original_config_get  # type: ignore[method-assign]
        manager_module.notify_download_started = original_started
        manager_module.notify_download_completed = original_completed
        manager_module.notify_download_failed = original_failed

    return restore


def assert_manager_progress_scope_consistency() -> None:
    restore = _patch_environment()
    try:
        clamped_engine = _ProgressEngine(
            [
                {"progress": 40.0, "speed": "1MB/s", "downloaded": "40/100", "attempt": 0, "source": "primary"},
                {"progress": 5.0, "speed": "1MB/s", "downloaded": "5/100", "attempt": 0, "source": "primary"},
            ]
        )
        dm = DownloadManager([clamped_engine], max_concurrent=0)
        snapshots = []
        try:
            dm.on_task_snapshot = snapshots.append
            dm._execute_download(_make_task("progress_clamped"), clamped_engine, user_specified=False)
            assert clamped_engine.observed == [(40.0, 0, "primary"), (40.0, 0, "primary")]
            assert not any(
                snapshot.status == "downloading" and snapshot.progress == 5.0 and snapshot.source_label == "primary"
                for snapshot in snapshots
            )
        finally:
            dm.shutdown()

        reset_engine = _ProgressEngine(
            [
                {"progress": 40.0, "speed": "1MB/s", "downloaded": "40/100", "attempt": 0, "source": "primary"},
                {"progress": 5.0, "speed": "1MB/s", "downloaded": "5/100", "attempt": 1, "source": "primary-safe"},
            ]
        )
        dm = DownloadManager([reset_engine], max_concurrent=0)
        snapshots = []
        try:
            dm.on_task_snapshot = snapshots.append
            dm._execute_download(_make_task("progress_reset"), reset_engine, user_specified=False)
            assert reset_engine.observed == [(40.0, 0, "primary"), (5.0, 1, "primary-safe")]
            assert any(
                snapshot.status == "downloading"
                and snapshot.progress == 5.0
                and snapshot.attempt == 1
                and snapshot.source_label == "primary-safe"
                for snapshot in snapshots
            )
        finally:
            dm.shutdown()
    finally:
        restore()


def assert_nm3u8dl_source_label_injection() -> None:
    engine = N_m3u8DL_RE_Engine(binary_path="N_m3u8DL-RE")
    task = _make_task("nm3u8dl_source")
    events: list[dict[str, Any]] = []
    line = "Vid Kbps ------------------------------ 17/340 5.00% 17.10MB/387.52MB2.75MBps00:02:54"

    class _FakeProcess:
        pid = 9876

    def fake_read_loop(process: object, current_task: DownloadTask, line_callback: Callable[[str, str], None]) -> EngineResult:
        assert getattr(process, "pid", None) == 9876
        assert current_task is task
        line_callback("stdout", line)
        return EngineResult(status="ok", returncode=0)

    engine.spawn = lambda *args, **kwargs: _FakeProcess()  # type: ignore[method-assign]
    engine.read_loop = fake_read_loop  # type: ignore[method-assign]
    engine._start_temp_progress_monitor = lambda **kwargs: None  # type: ignore[method-assign]

    ok, tail = engine._run_command(
        task,
        ["N_m3u8DL-RE", "https://example.test/video/index.m3u8"],
        events.append,
        "primary-safe",
    )

    assert ok is True
    assert tail == ""
    assert len(events) == 1
    assert events[0]["progress"] == 5.0
    assert events[0]["source"] == "primary-safe"
    assert events[0]["source_label"] == "primary-safe"


def assert_task_queue_abstraction() -> None:
    engine = _ProgressEngine([])
    first = _make_task("queue_first")
    second = _make_task("queue_second")
    queue: TaskQueue[QueuedDownload] = TaskQueue()

    queue.put(QueuedDownload(task=first, engine=engine, user_specified=True))
    queue.put(QueuedDownload(task=second, engine=engine, user_specified=False))

    snapshot = queue.snapshot()
    assert [entry.task.filename for entry in snapshot] == ["queue_first", "queue_second"]
    assert snapshot[0].user_specified is True
    assert queue.remove(f"task-{id(first):x}") is True
    popped = queue.pop_ready()
    assert popped is not None
    assert popped.task is second
    assert popped.user_specified is False
    assert queue.pop_ready() is None
    assert not queue


def main() -> None:
    assert_manager_progress_scope_consistency()
    assert_nm3u8dl_source_label_injection()
    assert_task_queue_abstraction()
    print("smoke_progress_consistency: OK")


if __name__ == "__main__":
    main()
