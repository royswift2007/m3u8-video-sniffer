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

    with patched_requests_get(fake_get):
        result = HLSProbe.probe("https://example.test/playlist.m3u8", headers={})

    assert result["ok"] is False, result
    assert result["stage"] == "segment", result
    assert result["status_code"] == 429, result
    assert result["soft_fail"] is True, result
    assert result["hard_fail"] is False, result
    assert result["severity"] == "warning", result


def test_playlist_404_is_hard_fail():
    def fake_get(url, **kwargs):
        if url.endswith("missing.m3u8"):
            return FakeResponse(url=url, status_code=404, text="Not Found")
        raise AssertionError(f"unexpected url: {url}")

    with patched_requests_get(fake_get):
        result = HLSProbe.probe("https://example.test/missing.m3u8", headers={})

    assert result["ok"] is False, result
    assert result["stage"] == "playlist", result
    assert result["status_code"] == 404, result
    assert result["soft_fail"] is False, result
    assert result["hard_fail"] is True, result
    assert result["severity"] == "error", result


def main():
    test_segment_429_is_soft_fail()
    test_playlist_404_is_hard_fail()
    print("smoke_hls_probe_soft_fail: OK")


if __name__ == "__main__":
    main()
