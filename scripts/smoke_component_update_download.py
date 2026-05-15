"""
Offline smoke checks for component update download, validation, and staging.

No real network, no UI, and no writes to the real bin directory are performed.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.component_update_downloader import ComponentUpdateDownloader
from core.component_update_models import RemoteReleaseInfo
from core.dependency_manifest import DependencyEntry, DependencyManifest


class FakeResponse:
    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


class QueueOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("fake opener has no queued response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _exe_bytes(label: str = "fake") -> bytes:
    return b"MZ" + label.encode("utf-8") + b"\0payload"


def _zip_bytes(member_name: str = "tools/FakeTool.exe", content: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content if content is not None else _exe_bytes("zip"))
        archive.writestr("README.txt", "not executable")
    return output.getvalue()


def _manifest(path: Path, download_type: str = "file", member: str | None = None, checksum: dict | None = None) -> DependencyManifest:
    # Audit-finding B1: the downloader now refuses updates whose manifest
    # entry has neither a static checksum, nor a sha256 sidecar, nor a
    # URL on the TOFU-trusted HTTPS host allowlist. This fake manifest
    # models a GitHub-hosted component so the TOFU path takes the
    # unchecksummed smoke cases (see
    # ``assert_direct_exe_download_staged`` / ``assert_empty_file_objectized``
    # / ``assert_asset_member_mismatch_objectized`` / ``assert_zip_member_extract_staged``)
    # through to the same outcomes they had under the old lax policy.
    # Smoke cases that DO provide a ``checksum`` keep using it and
    # exercise the static-pin path unchanged.
    asset_url = "https://github.com/m3u8d-fake/fake_tool/releases/download/v1.2.3/fake_asset"
    payload = {
        "required": [
            {
                "id": "fake_tool",
                "label": "Fake Tool",
                "path": "bin/fake_tool.exe",
                "download": {
                    "source": "direct",
                    "type": download_type,
                    "url": asset_url,
                    "member": member,
                    "timeout": 5,
                },
                "update": {
                    "enabled": True,
                    "release_source": "direct",
                    "latest_url": asset_url,
                    "asset_pattern": "fake_asset",
                    "version_source": "url",
                    "version_regex": None,
                    "install_strategy": "replace_zip_member" if download_type == "zip" else "replace_file",
                    "requires_process_free": True,
                    "checksum": checksum,
                },
            }
        ],
        "recommended": [],
        "optional": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return DependencyManifest(path)


def _entry(manifest: DependencyManifest) -> DependencyEntry:
    return manifest.get_update_enabled_entries(include_recommended=False)[0]


def _remote(asset_name: str, url: str = "https://example.invalid/asset") -> RemoteReleaseInfo:
    return RemoteReleaseInfo(
        component_id="fake_tool",
        latest_version="1.2.3",
        release_url="https://example.invalid/release",
        asset_name=asset_name,
        asset_url=url,
        asset_size=123,
    )


def _assert_under(child: str | None, parent: Path) -> None:
    assert child, "expected path"
    resolved_child = Path(child).resolve()
    resolved_parent = parent.resolve()
    assert resolved_child == resolved_parent or resolved_parent in resolved_child.parents, (resolved_child, resolved_parent)


def assert_direct_exe_download_staged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp) / "component_updates"
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        manifest = _manifest(Path(tmp) / "deps.json", download_type="file")
        result = ComponentUpdateDownloader(
            temp_root=tmp_root,
            opener=QueueOpener([FakeResponse(_exe_bytes("direct"))]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.exe"))
        assert result.success, result
        # Audit-finding B1: TOFU created a pin on first update.
        assert result.code in ("ok", "tofu_pin_created"), result
        assert result.staged_exe_path and Path(result.staged_exe_path).read_bytes().startswith(b"MZ"), result
        _assert_under(result.download_path, tmp_root)
        _assert_under(result.staging_dir, tmp_root)
        _assert_under(result.staged_exe_path, tmp_root)
        assert not (bin_dir / "fake_tool.exe").exists(), "real bin directory must not be written"


def assert_zip_member_extract_staged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp) / "component_updates"
        manifest = _manifest(Path(tmp) / "deps.json", download_type="zip", member="FakeTool.exe")
        result = ComponentUpdateDownloader(
            temp_root=tmp_root,
            opener=QueueOpener([FakeResponse(_zip_bytes("nested/FakeTool.exe"))]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.zip"))
        assert result.success, result
        assert result.staged_exe_path and Path(result.staged_exe_path).name == "FakeTool.exe", result
        _assert_under(result.staged_exe_path, tmp_root)


def assert_asset_member_mismatch_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json", download_type="zip", member="Expected.exe")
        result = ComponentUpdateDownloader(
            temp_root=Path(tmp) / "component_updates",
            opener=QueueOpener([FakeResponse(_zip_bytes("Other.exe"))]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.zip"))
        assert not result.success, result
        assert result.code == "asset_member_mismatch", result
        assert "zip member does not match" in result.message, result


def assert_empty_file_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json", download_type="file")
        result = ComponentUpdateDownloader(
            temp_root=Path(tmp) / "component_updates",
            opener=QueueOpener([FakeResponse(b"")]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.exe"))
        assert not result.success, result
        assert result.code == "empty_download", result


def assert_hash_mismatch_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        checksum = {"sha256": "0" * 64}
        manifest = _manifest(Path(tmp) / "deps.json", download_type="file", checksum=checksum)
        result = ComponentUpdateDownloader(
            temp_root=Path(tmp) / "component_updates",
            opener=QueueOpener([FakeResponse(_exe_bytes("hash"))]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.exe"))
        assert not result.success, result
        assert result.code == "hash_mismatch", result
        assert result.sha256 == hashlib.sha256(_exe_bytes("hash")).hexdigest(), result


def assert_hash_match_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        payload = _exe_bytes("hash-ok")
        checksum = {"sha256": hashlib.sha256(payload).hexdigest()}
        manifest = _manifest(Path(tmp) / "deps.json", download_type="file", checksum=checksum)
        result = ComponentUpdateDownloader(
            temp_root=Path(tmp) / "component_updates",
            opener=QueueOpener([FakeResponse(payload)]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.exe"))
        assert result.success, result
        assert not result.weak_validation, result
        assert result.sha256 == checksum["sha256"], result


def assert_network_failure_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json", download_type="file")
        result = ComponentUpdateDownloader(
            temp_root=Path(tmp) / "component_updates",
            opener=QueueOpener([URLError("offline")]),
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), _remote("fake_tool.exe"))
        assert not result.success, result
        assert result.code == "network_error", result
        assert "offline" in result.message, result


def assert_missing_asset_url_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json", download_type="file")
        remote = replace(_remote("fake_tool.exe"), asset_url=None)
        result = ComponentUpdateDownloader(
            temp_root=Path(tmp) / "component_updates",
            tofu_pin_path=Path(tmp) / "pins.json",
        ).download_and_stage(_entry(manifest), remote)
        assert not result.success, result
        assert result.code == "missing_asset_url", result


def main() -> int:
    checks = [
        assert_direct_exe_download_staged,
        assert_zip_member_extract_staged,
        assert_asset_member_mismatch_objectized,
        assert_empty_file_objectized,
        assert_hash_mismatch_objectized,
        assert_hash_match_success,
        assert_network_failure_objectized,
        assert_missing_asset_url_objectized,
    ]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("PASS component update download/staging offline smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
