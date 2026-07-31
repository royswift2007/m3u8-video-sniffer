"""Offline smoke tests for sniffer segment suppression.

The script validates that dense HLS/DASH segment captures are folded as noise
while playlist resources remain visible.  It uses monkey-patched SSRF and
notification hooks so it never touches the network or desktop notification
stack.
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

from core.download_context import RESOURCE_TYPE_HLS, RESOURCE_TYPE_SEGMENT
import core.m3u8_sniffer as sniffer_module
from core.m3u8_sniffer import M3U8Sniffer


def _fake_config_get(key: str, default: Any = None) -> Any:
    values = {
        "site_rules": [],
        "features": {
            "segment_suppression_enabled": True,
            "segment_suppression_threshold": 3,
            "sniffer_dedup_enabled": True,
            "sniffer_rules_enabled": False,
            "forward_cookie_headers": True,
            "forward_authorization_headers": False,
        },
        "security.allow_private_networks": False,
    }
    return values.get(key, default)


def _patch_sniffer_environment(notifications: list[str]) -> Callable[[], None]:
    original_ensure_public = sniffer_module.ensure_public
    original_notify = sniffer_module.notify_resource_found
    original_config_get = sniffer_module.config.get

    sniffer_module.ensure_public = lambda *_args, **_kwargs: None
    sniffer_module.notify_resource_found = lambda title: notifications.append(str(title))
    sniffer_module.config.get = _fake_config_get

    def restore() -> None:
        sniffer_module.ensure_public = original_ensure_public
        sniffer_module.notify_resource_found = original_notify
        sniffer_module.config.get = original_config_get

    return restore


def assert_playlist_context_suppresses_segments(notifications: list[str]) -> None:
    sniffer = M3U8Sniffer()
    page_url = "https://www.example.test/watch/playlist-context"
    before_notify_count = len(notifications)

    playlist = sniffer.add_resource(
        "https://cdn.example.test/hls/master.m3u8",
        {"Referer": page_url},
        page_url,
        page_title="Playlist Context",
        mime="application/vnd.apple.mpegurl",
    )
    assert playlist is not None
    assert playlist.resource_type == RESOURCE_TYPE_HLS
    assert playlist.is_suppressed is False
    assert len(notifications) == before_notify_count + 1

    segment = sniffer.add_resource(
        "https://cdn.example.test/hls/segment-0001.ts?token=abc",
        {"Referer": page_url},
        page_url,
        page_title="Playlist Context",
    )
    assert segment is not None
    assert segment.resource_type == RESOURCE_TYPE_SEGMENT
    assert segment.is_suppressed is True
    assert segment.suppression_reason == "segment_grouped"
    assert segment.segment_group_count == 1
    assert segment.segment_group_key
    assert segment.candidate_score == -80
    assert len(notifications) == before_notify_count + 1


def assert_dense_segment_threshold_suppresses_later_segments(notifications: list[str]) -> None:
    sniffer = M3U8Sniffer()
    page_url = "https://www.example.test/watch/dense-segments"
    before_notify_count = len(notifications)

    first = sniffer.add_resource(
        "https://media.example.test/vod/chunk-0001.ts",
        {"Referer": page_url},
        page_url,
    )
    second = sniffer.add_resource(
        "https://media.example.test/vod/chunk-0002.ts",
        {"Referer": page_url},
        page_url,
    )
    third = sniffer.add_resource(
        "https://media.example.test/vod/chunk-0003.ts",
        {"Referer": page_url},
        page_url,
    )

    assert first is not None and second is not None and third is not None
    assert first.resource_type == RESOURCE_TYPE_SEGMENT
    assert second.resource_type == RESOURCE_TYPE_SEGMENT
    assert third.resource_type == RESOURCE_TYPE_SEGMENT
    assert first.segment_group_key == second.segment_group_key == third.segment_group_key
    assert first.segment_group_count == 1
    assert second.segment_group_count == 2
    assert third.segment_group_count == 3
    assert first.is_suppressed is False
    assert second.is_suppressed is False
    assert third.is_suppressed is True
    assert first.candidate_score == -40
    assert second.candidate_score == -40
    assert third.candidate_score == -80
    assert len(notifications) == before_notify_count + 2


def main() -> None:
    notifications: list[str] = []
    restore = _patch_sniffer_environment(notifications)
    try:
        assert_playlist_context_suppresses_segments(notifications)
        assert_dense_segment_threshold_suppresses_later_segments(notifications)
    finally:
        restore()
    print("smoke_segment_suppression: OK")


if __name__ == "__main__":
    main()
