"""Offline smoke coverage for M6 yt-dlp / Streamlink targeted enhancements."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engines.base_engine import EngineResult
from engines.streamlink_engine import StreamlinkEngine
from engines.ytdlp_engine import YtdlpEngine, resolve_cookie_file


class _Task:
    def __init__(self, **kwargs):
        self.url = kwargs.get("url", "https://example.com/watch/demo")
        self.filename = kwargs.get("filename", "m6_smoke")
        self.save_dir = kwargs.get("save_dir", str(PROJECT_ROOT))
        self.headers = kwargs.get("headers", {}) or {}
        self.quality = kwargs.get("quality", "")
        self.streamlink_quality = kwargs.get("streamlink_quality", "")
        self.error_message = kwargs.get("error_message", "")
        self.stop_requested = kwargs.get("stop_requested", False)
        self.stop_reason = kwargs.get("stop_reason", "")


def assert_ytdlp_m6_behaviour() -> None:
    cookies_dir = str(PROJECT_ROOT / "cookies")
    mapped = resolve_cookie_file(
        "https://clips.twitch.tv/example", cookies_base_path=cookies_dir
    )
    assert mapped and Path(mapped).name == "www.twitch.tv_cookies.txt"

    engine = YtdlpEngine(binary_path="yt-dlp")
    assert engine._should_try_generic_impersonate("Cloudflare checking your browser")
    assert not engine._should_try_generic_impersonate("ERROR: protected by DRM / Widevine")
    assert engine._should_try_browser_cookie_retry(
        "ERROR: No video formats found", "https://www.bilibili.com/video/BV1demo"
    )
    assert not engine._should_try_browser_cookie_retry(
        "ERROR: This video has been removed", "https://www.bilibili.com/video/BV1demo"
    )

    task = _Task(url="https://www.bilibili.com/video/BV1demo", headers={})
    calls: list[tuple[object, bool, bool]] = []
    engine.get_cookies_file_for_url = lambda _url: ""

    def fake_do_download(
        current_task,
        progress_callback,
        use_browser_cookies=None,
        allow_insecure_tls=False,
        generic_impersonate=False,
    ):
        calls.append((use_browser_cookies, allow_insecure_tls, generic_impersonate))
        current_task.error_message = "ERROR: This content is protected by DRM / Widevine"
        return False, False

    engine._do_download = fake_do_download
    assert engine.download(task, lambda _data: None) is False
    assert calls == [(None, False, False)]

    import engines.ytdlp_engine as ytdlp_module

    original_run = ytdlp_module.subprocess.run
    run_calls: list[list[str]] = []

    class _RunResult:
        returncode = 1
        stdout = ""
        stderr = "ERROR: This content is protected by DRM / Widevine"

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return _RunResult()

    try:
        ytdlp_module.subprocess.run = fake_run
        format_engine = YtdlpEngine(binary_path="yt-dlp")
        format_engine.get_cookies_file_for_url = lambda _url: ""
        assert format_engine.get_formats("https://www.bilibili.com/video/BV1demo") == []
        assert len(run_calls) == 1
    finally:
        ytdlp_module.subprocess.run = original_run


def assert_streamlink_m6_behaviour() -> None:
    engine = StreamlinkEngine(binary_path="streamlink")
    task = _Task(
        url="https://twitch.tv/example",
        headers={
            "User-Agent": "UA-1",
            "Origin": "https://twitch.tv",
            "Accept": "text/html",
            "Accept-Language": "zh-CN",
            "authorization": "Bearer token123",
            "_allow_authorization_header": True,
        },
        quality="best",
    )

    cmd = engine._build_command(task)
    assert "Origin=https://twitch.tv" in cmd
    assert "Accept=text/html" in cmd
    assert "Accept-Language=zh-CN" in cmd
    assert "Authorization=Bearer token123" in cmd

    assert engine._should_retry_transient("HTTP 503 temporarily unavailable")
    assert not engine._should_retry_transient("subscriber-only stream temporarily unavailable")
    assert engine._should_fallback_quality("Failed to open stream: 403 Forbidden")
    assert not engine._should_fallback_quality("stream is offline and failed to open stream")

    reason, suggestions = engine._diagnose_failure("not available in your country")
    assert "地理限制" in reason
    assert suggestions

    calls: list[tuple[str, int]] = []

    def fake_run_attempt(current_task, progress_callback, *, quality, attempt):
        calls.append((quality, attempt))
        return EngineResult(status="failed", returncode=1), [
            "subscriber-only stream requires login and is temporarily unavailable"
        ]

    engine._run_attempt = fake_run_attempt
    assert engine.download(_Task(url="https://twitch.tv/example"), lambda _data: None) is False
    assert calls == [("best", 1)]


def main() -> None:
    assert_ytdlp_m6_behaviour()
    assert_streamlink_m6_behaviour()
    print("smoke_ytdlp_streamlink_m6: OK")


if __name__ == "__main__":
    main()
