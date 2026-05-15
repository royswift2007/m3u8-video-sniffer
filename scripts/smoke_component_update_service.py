"""
Offline end-to-end smoke checks for ComponentUpdateService orchestration.

The script uses fake release/downloader/installer/probe objects and temporary
manifest/state files only. It performs no UI work, no real network calls, and no
writes to the repository's real bin directory.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.component_update_downloader import ComponentDownloadStageResult
from core.component_update_installer import ComponentInstallResult
from core.component_update_models import ComponentUpdateProgressEvent, ComponentVersionInfo, RemoteReleaseInfo
from core.component_update_service import ComponentUpdateService
from core.component_update_state import ComponentUpdateStateStore
from core.dependency_manifest import DependencyEntry, DependencyManifest


class FakeProbe:
    def __init__(self, versions: dict[str, list[str | None]], errors: dict[str, list[str | None]] | None = None):
        self.versions = {key: list(value) for key, value in versions.items()}
        self.errors = {key: list(value) for key, value in (errors or {}).items()}
        self.calls: list[str] = []

    def probe(self, entry: DependencyEntry) -> ComponentVersionInfo:
        self.calls.append(entry.id)
        versions = self.versions.setdefault(entry.id, [None])
        version = versions.pop(0) if len(versions) > 1 else versions[0]
        errors = self.errors.setdefault(entry.id, [None])
        error = errors.pop(0) if len(errors) > 1 else errors[0]
        return ComponentVersionInfo(
            component_id=entry.id,
            label=entry.label,
            path=str(entry.path),
            exists=True,
            version=version,
            raw_output=version,
            error=error,
        )


class FakeReleaseClient:
    def __init__(self, remotes: dict[str, RemoteReleaseInfo]):
        self.remotes = remotes
        self.calls: list[tuple[str, bool]] = []

    def fetch_latest(self, entry: DependencyEntry, force: bool = False) -> RemoteReleaseInfo:
        self.calls.append((entry.id, force))
        return self.remotes[entry.id]


class FakeDownloader:
    def __init__(self, results: dict[str, ComponentDownloadStageResult]):
        self.results = results
        self.calls: list[tuple[str, str | None]] = []

    def download_and_stage(
        self,
        entry: DependencyEntry,
        remote: RemoteReleaseInfo,
        timeout: int | None = None,
        progress_callback: Any = None,
    ) -> ComponentDownloadStageResult:
        self.calls.append((entry.id, remote.asset_url))
        # Exercise the progress pipe in tests so a future regression
        # in the service callback wiring shows up here rather than
        # silently in production.
        if progress_callback is not None:
            try:
                progress_callback(1024, 2048)
            except Exception:
                pass
        return self.results[entry.id]


class FakeInstaller:
    def __init__(self, results: dict[str, ComponentInstallResult]):
        self.results = results
        self.calls: list[tuple[str, str | None]] = []

    def install_staged_update(
        self,
        download_result: ComponentDownloadStageResult,
        entry: DependencyEntry,
        expected_version: str | None = None,
    ) -> ComponentInstallResult:
        self.calls.append((entry.id, expected_version))
        return self.results[entry.id]


def _manifest(path: Path, ids: list[str]) -> DependencyManifest:
    payload = {
        "required": [
            {
                "id": component_id,
                "label": component_id.replace("_", " ").title(),
                "path": str(path.parent / "sandbox_bin" / f"{component_id}.exe").replace("\\", "/"),
                "version": {
                    "command": ["{path}", "--version"],
                    "regex": r"(?P<version>.+)",
                    "timeout": 1,
                },
                "update": {
                    "enabled": True,
                    "release_source": "direct",
                    "latest_url": f"https://example.invalid/{component_id}.exe",
                    "asset_pattern": f"{component_id}.exe",
                    "version_source": "url",
                    "install_strategy": "replace_file",
                },
            }
            for component_id in ids
        ],
        "recommended": [],
        "optional": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return DependencyManifest(path)


def _remote(component_id: str, version: str | None, *, error: str | None = None, asset: bool = True) -> RemoteReleaseInfo:
    return RemoteReleaseInfo(
        component_id=component_id,
        latest_version=version,
        release_url=f"https://example.invalid/releases/{component_id}",
        published_at="2026-01-01T00:00:00Z",
        asset_name=f"{component_id}.exe" if asset else None,
        asset_url=f"https://example.invalid/{component_id}.exe" if asset else None,
        error=error,
    )


def _download(component_id: str, tmp_root: Path, *, success: bool = True, code: str = "ok") -> ComponentDownloadStageResult:
    staging = tmp_root / "component_updates" / component_id / "run" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / f"{component_id}.exe"
    if success:
        staged.write_bytes(b"MZ fake exe")
    return ComponentDownloadStageResult(
        component_id=component_id,
        success=success,
        code=code,
        message="download staged" if success else "download failed",
        download_path=str(staging / f"{component_id}.download"),
        staging_dir=str(staging),
        staged_exe_path=str(staged) if success else None,
        asset_name=f"{component_id}.exe",
        bytes_downloaded=12 if success else 0,
        weak_validation=True,
    )


def _install(
    component_id: str,
    *,
    success: bool = True,
    code: str = "ok",
    rollback_success: bool | None = None,
    warning: str | None = None,
    new_version: str | None = "2.0.0",
) -> ComponentInstallResult:
    return ComponentInstallResult(
        component_id=component_id,
        success=success,
        code=code,
        message="installed with warning" if warning else ("installed" if success else "install failed"),
        target_path=f"X:/{component_id}.exe",
        backup_path=f"X:/backup/{component_id}.exe" if success else "X:/backup/original.exe",
        old_version="1.0.0",
        new_version=new_version if success else None,
        post_check_error=warning,
        warning=warning,
        rollback_attempted=not success,
        rollback_success=rollback_success if not success else None,
        rollback_error="rollback failed" if rollback_success is False else None,
    )


def _service(
    tmp_root: Path,
    component_ids: list[str],
    probe_versions: dict[str, list[str | None]],
    remotes: dict[str, RemoteReleaseInfo],
    downloads: dict[str, ComponentDownloadStageResult] | None = None,
    installs: dict[str, ComponentInstallResult] | None = None,
    events: list[ComponentUpdateProgressEvent] | None = None,
) -> ComponentUpdateService:
    manifest = _manifest(tmp_root / "deps.json", component_ids)
    state_store = ComponentUpdateStateStore(tmp_root / "component_updates.json")
    return ComponentUpdateService(
        manifest=manifest,
        state_store=state_store,
        progress_callback=events.append if events is not None else None,
        release_client=FakeReleaseClient(remotes),
        downloader=FakeDownloader(downloads or {}),
        installer=FakeInstaller(installs or {}),
        version_probe=FakeProbe(probe_versions),
    )


def _state(service: ComponentUpdateService, component_id: str) -> dict:
    return service.state_store.get_component_state(component_id)


def assert_latest_skips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        events: list[ComponentUpdateProgressEvent] = []
        service = _service(tmp_root, ["latest_tool"], {"latest_tool": ["2.0.0"]}, {"latest_tool": _remote("latest_tool", "2.0.0")}, events=events)
        result = service.update_component("latest_tool")
        assert result.success and result.skipped and result.status == "latest", result
        assert [event.event for event in events] == ["checking", "latest"], events
        state = _state(service, "latest_tool")
        assert state["status"] == "latest", state
        assert state["last_checked_at"] and state["latest_version"] == "2.0.0", state
        assert state["local_version"] == "2.0.0" and state.get("last_error") is None, state


def assert_update_success_chain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        events: list[ComponentUpdateProgressEvent] = []
        service = _service(
            tmp_root,
            ["update_tool"],
            {"update_tool": ["1.0.0"]},
            {"update_tool": _remote("update_tool", "2.0.0")},
            {"update_tool": _download("update_tool", tmp_root)},
            {"update_tool": _install("update_tool")},
            events,
        )
        result = service.update_component("update_tool")
        assert result.success and not result.skipped and result.status == "updated", result
        assert result.old_version == "1.0.0" and result.new_version == "2.0.0", result
        # The service now emits per-chunk ``downloading`` progress so a
        # 128 MB FFmpeg zip no longer looks frozen in the UI. Allow any
        # number of ``downloading`` events between "checking" and
        # "staged"; the surrounding milestones still have to arrive in
        # the canonical order.
        event_names = [event.event for event in events]
        assert event_names[0] == "checking", event_names
        assert event_names[-3:] == ["staged", "installing", "updated"], event_names
        assert event_names[1] == "downloading", event_names
        assert all(e == "downloading" for e in event_names[1:-3]), event_names
        assert len(event_names) >= 5, event_names
        state = _state(service, "update_tool")
        assert state["status"] == "updated" and state["latest_version"] == "2.0.0", state
        assert state["local_version"] == "2.0.0" and state["last_update_result"]["success"] is True, state


def assert_force_updates_unknown_versions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        service = _service(
            tmp_root,
            ["force_tool"],
            {"force_tool": ["custom-local"]},
            {"force_tool": _remote("force_tool", "custom-remote")},
            {"force_tool": _download("force_tool", tmp_root)},
            {"force_tool": _install("force_tool", success=True)},
        )
        result = service.update_component("force_tool", force=True)
        assert result.success and result.status == "updated", result
        assert service.release_client.calls == [("force_tool", True)], service.release_client.calls
        assert service.downloader.calls and service.installer.calls, result


def assert_remote_failure_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = _service(Path(tmp), ["remote_fail"], {"remote_fail": ["1.0.0"]}, {"remote_fail": _remote("remote_fail", None, error="remote boom")})
        result = service.update_component("remote_fail")
        assert not result.success and result.code == "remote_check_failed" and result.error == "remote boom", result
        state = _state(service, "remote_fail")
        assert state["status"] == "failed" and state["last_error"] == "remote boom", state


def assert_download_failure_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        service = _service(
            tmp_root,
            ["download_fail"],
            {"download_fail": ["1.0.0"]},
            {"download_fail": _remote("download_fail", "2.0.0")},
            {"download_fail": _download("download_fail", tmp_root, success=False, code="hash_mismatch")},
        )
        result = service.update_component("download_fail")
        assert not result.success and result.code == "hash_mismatch", result
        state = _state(service, "download_fail")
        assert state["status"] == "failed" and state["last_download"]["code"] == "hash_mismatch", state


def assert_install_and_rollback_failure_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        service = _service(
            tmp_root,
            ["install_fail"],
            {"install_fail": ["1.0.0"]},
            {"install_fail": _remote("install_fail", "2.0.0")},
            {"install_fail": _download("install_fail", tmp_root)},
            {"install_fail": _install("install_fail", success=False, code="post_check_failed", rollback_success=False)},
        )
        result = service.update_component("install_fail")
        assert not result.success and result.code == "post_check_failed", result
        assert result.rollback_attempted and result.rollback_success is False and result.rollback_error == "rollback failed", result
        state = _state(service, "install_fail")
        assert state["status"] == "failed" and state["last_install"]["rollback_success"] is False, state


def assert_update_success_with_warning_persists_warning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        service = _service(
            tmp_root,
            ["warn_tool"],
            {"warn_tool": ["1.0.0"]},
            {"warn_tool": _remote("warn_tool", "2.0.0")},
            {"warn_tool": _download("warn_tool", tmp_root)},
            {"warn_tool": _install("warn_tool", code="ok_with_warning", warning="version probe timed out", new_version=None)},
        )
        result = service.update_component("warn_tool")
        assert result.success and result.status == "updated", result
        assert result.code == "ok_with_warning", result
        assert result.warning == "version probe timed out", result
        assert result.new_version == "2.0.0", result
        state = _state(service, "warn_tool")
        assert state["status"] == "updated" and state["last_warning"] == "version probe timed out", state
        assert state["last_install"]["warning"] == "version probe timed out", state



def assert_batch_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        service = _service(
            tmp_root,
            ["batch_latest", "batch_update", "batch_fail"],
            {"batch_latest": ["2.0.0"], "batch_update": ["1.0.0"], "batch_fail": ["1.0.0"]},
            {
                "batch_latest": _remote("batch_latest", "2.0.0"),
                "batch_update": _remote("batch_update", "2.0.0"),
                "batch_fail": _remote("batch_fail", None, error="remote fail"),
            },
            {"batch_update": _download("batch_update", tmp_root)},
            {"batch_update": _install("batch_update")},
        )
        batch = service.update_components()
        assert batch.total == 3 and batch.success_count == 1 and batch.skipped_count == 1 and batch.failure_count == 1, batch.to_dict()
        assert not batch.success, batch.to_dict()
        statuses = {result.component_id: result.status for result in batch.results}
        assert statuses == {"batch_latest": "latest", "batch_update": "updated", "batch_fail": "failed"}, statuses


def assert_manifest_statuses_and_probe_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        manifest_path = tmp_root / "deps.json"
        bin_dir = tmp_root / "sandbox_bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        slow_script = bin_dir / "slow_probe.py"
        slow_script.write_text("import time\ntime.sleep(10)\nprint('9.9.9')\n", encoding="utf-8")
        fast_script = bin_dir / "fast_probe.py"
        fast_script.write_text("print('1.2.3')\n", encoding="utf-8")
        payload = {
            "required": [
                {
                    "id": "slow_tool",
                    "label": "Slow Tool",
                    "path": str(slow_script).replace("\\", "/"),
                    "version": {"command": [sys.executable, "{path}"], "regex": r"(?P<version>\d+\.\d+\.\d+)", "timeout": 2},
                    "update": {"enabled": True, "release_source": "direct", "latest_url": "https://example.invalid/slow.exe"},
                },
                {
                    "id": "fast_tool",
                    "label": "Fast Tool",
                    "path": str(fast_script).replace("\\", "/"),
                    "version": {"command": [sys.executable, "{path}"], "regex": r"(?P<version>\d+\.\d+\.\d+)", "timeout": 5},
                    "update": {"enabled": True, "release_source": "direct", "latest_url": "https://example.invalid/fast.exe"},
                },
            ],
            "recommended": [],
            "optional": [],
        }
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        service = ComponentUpdateService(
            manifest=DependencyManifest(manifest_path),
            state_store=ComponentUpdateStateStore(tmp_root / "component_updates.json"),
        )
        manifest_statuses = service.get_manifest_statuses()
        assert [status.component_id for status in manifest_statuses] == ["slow_tool", "fast_tool"], manifest_statuses
        assert all(status.status == "manifest_listed" for status in manifest_statuses), manifest_statuses
        started = time.monotonic()
        local_statuses = service.refresh_local_status()
        elapsed = time.monotonic() - started
        by_id = {status.component_id: status for status in local_statuses}
        assert elapsed < 7, elapsed
        assert by_id["slow_tool"].status == "local_check_failed", by_id["slow_tool"]
        assert "timed out" in (by_id["slow_tool"].local.error or ""), by_id["slow_tool"]
        assert by_id["fast_tool"].status == "local_checked", by_id["fast_tool"]
        assert by_id["fast_tool"].local.version == "1.2.3", by_id["fast_tool"]


def assert_streamlink_slow_probe_uses_manifest_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        manifest_path = tmp_root / "deps.json"
        bin_dir = tmp_root / "sandbox_bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        streamlink_script = bin_dir / "streamlink_probe.py"
        streamlink_script.write_text("import time\ntime.sleep(9)\nprint('streamlink 7.3.0')\n", encoding="utf-8")
        payload = {
            "required": [],
            "recommended": [
                {
                    "id": "streamlink",
                    "label": "Streamlink",
                    "path": str(streamlink_script).replace("\\", "/"),
                    "version": {"command": [sys.executable, "{path}"], "regex": r"streamlink\s+(?P<version>\S+)", "timeout": 12},
                    "update": {"enabled": True, "release_source": "direct", "latest_url": "https://example.invalid/streamlink.exe"},
                }
            ],
            "optional": [],
        }
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        service = ComponentUpdateService(
            manifest=DependencyManifest(manifest_path),
            state_store=ComponentUpdateStateStore(tmp_root / "component_updates.json"),
        )
        started = time.monotonic()
        statuses = service.refresh_local_status(component_ids=["streamlink"])
        elapsed = time.monotonic() - started
        assert elapsed >= 8, elapsed
        assert elapsed < 14, elapsed
        assert len(statuses) == 1, statuses
        status = statuses[0]
        assert status.status == "local_checked", status
        assert status.local.version == "7.3.0", status


def run() -> None:
    checks = [
        assert_latest_skips,
        assert_update_success_chain,
        assert_force_updates_unknown_versions,
        assert_remote_failure_objectized,
        assert_download_failure_objectized,
        assert_install_and_rollback_failure_objectized,
        assert_update_success_with_warning_persists_warning,
        assert_batch_summary,
        assert_manifest_statuses_and_probe_timeout,
        assert_streamlink_slow_probe_uses_manifest_timeout,
    ]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("component update service smoke passed")


if __name__ == "__main__":
    run()
