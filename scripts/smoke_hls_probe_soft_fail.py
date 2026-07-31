"""Smoke tests for HLS probe hard/soft failure classification.

This script does not access real user URLs. It monkey-patches requests.get to
simulate playlist and segment HTTP responses.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from core.services.hls_probe import HLSProbe


@dataclass
class FakeResponse:
    url: str
    status_code: int
    text: str = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(
                f"{self.status_code} Client Error for url: {self.url}",
            )
            error.response = self
            raise error

    def close(self):
        return None


@contextmanager
def patched_requests_get(handler: Callable):
    original_get = requests.get
    requests.get = handler
    try:
        yield
    finally:
        requests.get = original_get


@contextmanager
def patched_ssrf():
    """Make the probe SSRF-guard offline-friendly.

    The smoke uses ``example.test`` which has no real DNS record. Pin
    ``ensure_public`` to a fake public resolved host and disable the
    pinned-session path so the test exercises the (monkeypatched)
    module-level ``requests.get`` exactly like the pytest suite does.
    """
    from core.services import hls_probe as _hp
    from utils.ssrf_guard import ResolvedHost
    from ipaddress import IPv4Address

    _mock = ResolvedHost(hostname="example.test", ips=(IPv4Address("203.0.113.42"),))
    orig_ensure = _hp.ensure_public
    orig_pinned = _hp.make_pinned_session
    _hp.ensure_public = lambda url, *a, **kw: _mock
    _hp.make_pinned_session = lambda *a, **k: None
    try:
        yield
    finally:
        _hp.ensure_public = orig_ensure
        _hp.make_pinned_session = orig_pinned


def _media_playlist() -> str:
    return "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXTINF:4.000,",
            "seg-00001.ts",
            "#EXT-X-ENDLIST",
        ]
    )


def test_segment_429_is_soft_fail():
    def fake_get(url, **kwargs):
        if url.endswith("playlist.m3u8"):
            return FakeResponse(url=url, status_code=200, text=_media_playlist())
        if url.endswith("seg-00001.ts"):
            return FakeResponse(url=url, status_code=429, text="Too Many Requests")
        raise AssertionError(f"unexpected url: {url}")

    with patched_requests_get(fake_get), patched_ssrf():
        result = HLSProbe.probe("https://example.test/playlist.m3u8", headers={})

    assert result["ok"] is False, result
    assert result["stage"] == "segment", result
    assert result["status_code"] == 429, result
    assert result["soft_fail"] is True, result
    assert result["hard_fail"] is False, result
    assert result["severity"] == "warning", result
    diagnostics = result["stage_diagnostics"]
    assert [item["stage"] for item in diagnostics] == ["playlist", "segment", "segment"], result
    assert diagnostics[-1]["status_code"] == 429, result
    assert diagnostics[-1]["soft_fail"] is True, result


def test_playlist_404_is_hard_fail():
    def fake_get(url, **kwargs):
        if url.endswith("missing.m3u8"):
            return FakeResponse(url=url, status_code=404, text="Not Found")
        raise AssertionError(f"unexpected url: {url}")

    with patched_requests_get(fake_get), patched_ssrf():
        result = HLSProbe.probe("https://example.test/missing.m3u8", headers={})

    assert result["ok"] is False, result
    assert result["stage"] == "playlist", result
    assert result["status_code"] == 404, result
    assert result["soft_fail"] is False, result
    assert result["hard_fail"] is True, result
    assert result["severity"] == "error", result


def test_key_429_is_soft_fail():
    def fake_get(url, **kwargs):
        if url.endswith("playlist.m3u8"):
            return FakeResponse(
                url=url,
                status_code=200,
                text="\n".join(
                    [
                        "#EXTM3U",
                        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
                        "#EXTINF:4.000,",
                        "seg-00001.ts",
                        "#EXT-X-ENDLIST",
                    ]
                ),
            )
        if url.endswith("key.bin"):
            return FakeResponse(url=url, status_code=429, text="Too Many Requests")
        raise AssertionError(f"unexpected url: {url}")

    with patched_requests_get(fake_get), patched_ssrf():
        result = HLSProbe.probe("https://example.test/playlist.m3u8", headers={})

    assert result["ok"] is False, result
    assert result["stage"] == "key", result
    assert result["status_code"] == 429, result
    assert result["soft_fail"] is True, result
    assert result["hard_fail"] is False, result
    diagnostics = result["stage_diagnostics"]
    assert [item["stage"] for item in diagnostics] == ["playlist", "key", "key"], result
    assert diagnostics[-1]["soft_fail"] is True, result


def test_segment_range_416_retries_without_range():
    calls = []

    def fake_get(url, **kwargs):
        headers = kwargs.get("headers") or {}
        calls.append((url, dict(headers)))
        if url.endswith("playlist.m3u8"):
            return FakeResponse(url=url, status_code=200, text=_media_playlist())
        if url.endswith("seg-00001.ts") and headers.get("Range"):
            return FakeResponse(url=url, status_code=416, text="Range Not Satisfiable")
        if url.endswith("seg-00001.ts"):
            return FakeResponse(url=url, status_code=200, text="")
        raise AssertionError(f"unexpected url: {url}")

    with patched_requests_get(fake_get), patched_ssrf():
        result = HLSProbe.probe("https://example.test/playlist.m3u8", headers={})

    assert result["ok"] is True, result
    assert result["stage"] == "ready", result
    assert result["range_retry"] is True, result
    segment_diagnostics = [item for item in result["stage_diagnostics"] if item["stage"] == "segment"]
    assert [item["action"] for item in segment_diagnostics] == [
        "segment_range_probe",
        "segment_full_probe_after_range",
    ], result
    assert [item["status_code"] for item in segment_diagnostics] == [416, 200], result
    segment_headers = [headers for url, headers in calls if url.endswith("seg-00001.ts")]
    assert len(segment_headers) == 2, calls
    assert segment_headers[0].get("Range") == "bytes=0-2047", calls
    assert "Range" not in segment_headers[1], calls


def main():
    test_segment_429_is_soft_fail()
    test_playlist_404_is_hard_fail()
    test_key_429_is_soft_fail()
    test_segment_range_416_retries_without_range()
    print("smoke_hls_probe_soft_fail: OK")


if __name__ == "__main__":
    main()
