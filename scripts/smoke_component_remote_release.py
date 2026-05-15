"""
Offline smoke checks for read-only component remote release querying.

No real network, downloads, extraction, replacement, rollback, or UI are used.
"""

from __future__ import annotations

import json
import sys
import tempfile
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.component_release_client import ComponentReleaseClient
from core.component_update_service import ComponentUpdateService
from core.component_update_state import ComponentUpdateStateStore
from core.component_version_probe import ComponentVersionProbe
from core.dependency_manifest import DependencyManifest


class FakeHeaders(dict):
    def items(self):
        return super().items()


def _message_headers(headers: dict[str, str]) -> Message:
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return message


class FakeResponse:
    def __init__(self, payload: dict, headers: dict[str, str] | None = None):
        self.payload = payload
        self.headers = FakeHeaders(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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


def _github_payload(tag="v2.1.0", asset_name="tool-win-x64.zip"):
    return {
        "tag_name": tag,
        "published_at": "2026-05-01T00:00:00Z",
        "html_url": "https://github.com/example/tool/releases/tag/v2.1.0",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://example.invalid/{asset_name}",
                "size": 12345,
            },
            {
                "name": "tool-linux.zip",
                "browser_download_url": "https://example.invalid/tool-linux.zip",
                "size": 456,
            },
        ],
    }


def _manifest(path: Path) -> DependencyManifest:
    payload = {
        "required": [
            {
                "id": "fake_tool",
                "label": "Fake Tool",
                "path": "bin/fake_tool.exe",
                "version": {
                    "command": ["{path}", "--version"],
                    "regex": "(?P<version>\\d+(?:\\.\\d+)+)",
                    "normalize": "strip_v",
                    "timeout": 1,
                },
                "download": {"source": "github_release", "type": "zip", "timeout": 10},
                "update": {
                    "enabled": True,
                    "release_source": "github_latest",
                    "repo": "example/tool",
                    "asset_pattern": "*win-x64*.zip",
                    "version_source": "tag_name",
                    "version_regex": "(?P<version>v?\\d+(?:\\.\\d+)+)",
                    "install_strategy": "replace_zip_member",
                    "requires_process_free": True,
                    "checksum": None,
                },
            }
        ],
        "recommended": [],
        "optional": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return DependencyManifest(path)


def _entry(manifest: DependencyManifest):
    return manifest.get_update_enabled_entries(include_recommended=False)[0]


def assert_github_json_asset_and_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json")
        state = ComponentUpdateStateStore(Path(tmp) / "state.json")
        opener = QueueOpener([FakeResponse(_github_payload(), {"ETag": '"abc"', "X-RateLimit-Remaining": "59"})])
        client = ComponentReleaseClient(state, opener=opener)
        info = client.fetch_latest(_entry(manifest))
        assert info.latest_version == "2.1.0", info
        assert info.published_at == "2026-05-01T00:00:00Z", info
        assert info.asset_name == "tool-win-x64.zip", info
        assert info.asset_url == "https://example.invalid/tool-win-x64.zip", info
        assert info.etag == '"abc"', info
        assert state.get_etag("fake_tool", "https://api.github.com/repos/example/tool/releases/latest") == '"abc"'


def assert_304_uses_cached_release() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json")
        state = ComponentUpdateStateStore(Path(tmp) / "state.json")
        first = QueueOpener([FakeResponse(_github_payload(), {"ETag": '"abc"'})])
        ComponentReleaseClient(state, opener=first).fetch_latest(_entry(manifest))
        http_304 = HTTPError(
            "https://api.github.com/repos/example/tool/releases/latest",
            304,
            "Not Modified",
            _message_headers({"X-RateLimit-Remaining": "58"}),
            None,
        )
        second = QueueOpener([http_304])
        info = ComponentReleaseClient(state, opener=second).fetch_latest(_entry(manifest))
        assert info.latest_version == "2.1.0", info
        assert info.asset_name == "tool-win-x64.zip", info
        assert second.requests[0][0].headers.get("If-none-match") == '"abc"' or second.requests[0][0].headers.get("If-None-Match") == '"abc"'


def assert_rate_limit_and_network_errors() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json")
        state = ComponentUpdateStateStore(Path(tmp) / "state.json")
        limited = HTTPError(
            "https://api.github.com/repos/example/tool/releases/latest",
            403,
            "Forbidden",
            _message_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1770000000"}),
            None,
        )
        info = ComponentReleaseClient(state, opener=QueueOpener([limited])).fetch_latest(_entry(manifest), force=True)
        assert info.error and "rate limit" in info.error, info
        assert info.rate_limit_remaining == 0, info

        offline = ComponentReleaseClient(state, opener=QueueOpener([URLError("offline")])).fetch_latest(_entry(manifest), force=True)
        assert offline.error and "network unavailable" in offline.error, offline


def assert_asset_pattern_miss() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json")
        state = ComponentUpdateStateStore(Path(tmp) / "state.json")
        info = ComponentReleaseClient(
            state,
            opener=QueueOpener([FakeResponse(_github_payload(asset_name="tool-linux.zip"), {"ETag": '"miss"'})]),
        ).fetch_latest(_entry(manifest), force=True)
        assert info.error and "does not match pattern" in info.error, info
        assert info.asset_url is None, info


def assert_version_compare() -> None:
    service = ComponentUpdateService.__new__(ComponentUpdateService)
    assert service.compare_versions("v1.2.3", "1.2.4") == -1
    assert service.compare_versions("2024.05.01", "2024.05.01") == 0
    assert service.compare_versions("2024.05.02", "2024.05.01") == 1
    assert service.compare_versions("nightly", "stable") is None
    assert service.compare_versions(None, "1.0.0") is None


def assert_service_check_updates_with_fake_probe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _manifest(Path(tmp) / "deps.json")
        state = ComponentUpdateStateStore(Path(tmp) / "state.json")
        opener = QueueOpener([FakeResponse(_github_payload(tag="v2.1.0"), {"ETag": '"svc"'})])
        client = ComponentReleaseClient(state, opener=opener)
        service = ComponentUpdateService(manifest=manifest, state_store=state, release_client=client)

        class Probe(ComponentVersionProbe):
            def probe(self, entry):
                from core.component_update_models import ComponentVersionInfo

                return ComponentVersionInfo(
                    component_id=entry.id,
                    label=entry.label,
                    path=str(entry.path),
                    exists=True,
                    version="2.0.0",
                )

        service.version_probe = Probe()
        statuses = service.check_updates(force=True)
        assert len(statuses) == 1
        assert statuses[0].update_available is True, statuses[0]
        assert statuses[0].status == "update_available", statuses[0]
        persisted = state.get_component_state("fake_tool")
        assert persisted["update_available"] is True, persisted
        assert persisted["remote_asset_name"] == "tool-win-x64.zip", persisted


def main() -> None:
    assert_github_json_asset_and_state()
    assert_304_uses_cached_release()
    assert_rate_limit_and_network_errors()
    assert_asset_pattern_miss()
    assert_version_compare()
    assert_service_check_updates_with_fake_probe()
    print("component remote release smoke checks passed")


if __name__ == "__main__":
    main()
