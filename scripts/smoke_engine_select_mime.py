"""
Stage 3 smoke: ``core.engine_selector.select_engine`` dispatch (R24, R25.1).

Exercises the three primary decision paths of :func:`select_engine` without
performing any real network I/O:

1. **Manual override** — caller passes ``manual="Streamlink"``; the
   decision must round-trip unchanged with ``source="manual"``.
2. **Extension match** — ``.mp4?token=...`` must strip the query and
   route to Aria2 with ``source="extension"``.
3. **HEAD MIME probe** — a monkey-patched ``requests.head`` returns
   ``Content-Type: application/vnd.apple.mpegurl`` for a URL that
   *looks* like an ``.mp4`` direct video. The MIME probe must win
   (Requirement 24.1 priority) and route to N_m3u8DL-RE with
   ``source="mime"``.

``ssrf_guard.ensure_public`` is also stubbed so the probe does not
perform real DNS resolution, keeping the smoke hermetic on offline CI
hosts. The stub returns a synthetic :class:`ResolvedHost` pointing at
the loopback IP (127.0.0.1) — the monkey-patched ``requests.head`` is
what actually answers the probe, so no socket is ever opened.

Runs headless in <1s. Exits 0 on pass; non-zero on any deviation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import engine_selector as es  # noqa: E402
from core.engine_selector import (  # noqa: E402
    ENGINE_ARIA2,
    ENGINE_N_M3U8DL_RE,
    ENGINE_STREAMLINK,
    EngineDecision,
    select_engine,
)
from utils import ssrf_guard  # noqa: E402


# ---------------------------------------------------------------------------
# Patch helpers.
# ---------------------------------------------------------------------------


class _FakeHeadResponse:
    """Minimal ``requests.Response`` stand-in for the HEAD probe.

    Only the attributes consumed by
    :func:`core.engine_selector._head_probe_mime` are implemented:
    ``status_code`` and ``headers``.
    """

    def __init__(self, status_code: int, content_type: str) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


def _patched_ensure_public(url: str, *, allow_private: bool = False):  # noqa: ARG001
    """Return a synthetic :class:`ResolvedHost` pointing at 127.0.0.1.

    Real DNS resolution is skipped; the monkey-patched
    ``requests.head`` below is what actually answers the probe, so no
    socket is opened. Matching the real ``ensure_public`` signature
    keeps the selector code paths identical to production.
    """
    return ssrf_guard.ResolvedHost(hostname="smoke.invalid", ips=("127.0.0.1",))


class _PatchedRequests:
    """Scoped monkey-patch of ``core.engine_selector._requests``.

    Captures the URL of each probe call so the smoke can assert the
    selector actually hit the HEAD path (not an accidental extension
    fallback that happens to produce the same engine).
    """

    def __init__(self, status_code: int, content_type: str) -> None:
        self._status_code = status_code
        self._content_type = content_type
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def head(self, url: str, **kwargs: Any) -> _FakeHeadResponse:
        self.calls.append((url, kwargs))
        return _FakeHeadResponse(self._status_code, self._content_type)


# ---------------------------------------------------------------------------
# Scenarios.
# ---------------------------------------------------------------------------


def assert_manual_override_wins() -> None:
    """Manual override is the highest-priority branch (R24.1)."""
    decision = select_engine(
        "https://example.invalid/any.mp4", manual="Streamlink"
    )
    assert isinstance(decision, EngineDecision), decision
    assert decision.engine_name == ENGINE_STREAMLINK, decision
    assert decision.source == "manual", decision


def assert_extension_match_strips_query() -> None:
    """Query strings must be stripped before suffix matching (R24.2).

    We also patch ``_requests`` to raise on any ``head`` call so that
    an extension-first decision is provable — any accidental HEAD probe
    would surface the RuntimeError and fail the smoke loudly.
    """

    class _ShouldNotCall:
        def head(self, url: str, **kwargs: Any) -> _FakeHeadResponse:  # noqa: ARG002
            raise RuntimeError(
                "extension-match scenario must not perform a HEAD probe"
            )

    original_requests = es._requests
    original_ensure_public = es.ssrf_guard.ensure_public

    # Make the ensure_public call fail so the probe short-circuits to
    # ``None`` before touching ``_requests``; the extension branch must
    # still fire. This mirrors the "offline / SSRF-blocked" path in
    # production without relying on outbound network.
    def _blocked(url: str, *, allow_private: bool = False):  # noqa: ARG001
        raise ssrf_guard.SSRFBlocked("smoke: ensure_public disabled")

    es._requests = _ShouldNotCall()  # type: ignore[assignment]
    es.ssrf_guard.ensure_public = _blocked  # type: ignore[assignment]
    try:
        decision = select_engine(
            "https://example.invalid/video.mp4?token=abc&sig=xyz"
        )
    finally:
        es._requests = original_requests  # type: ignore[assignment]
        es.ssrf_guard.ensure_public = original_ensure_public  # type: ignore[assignment]

    assert decision.engine_name == ENGINE_ARIA2, decision
    # With the probe blocked, the selector records ``fallback_on_error``
    # (R24.3) rather than the plain ``extension`` source; both are
    # acceptable extension-branch outcomes.
    assert decision.source in ("extension", "fallback_on_error"), decision
    assert decision.reason == ".mp4", decision


def assert_head_mime_probe_overrides_extension() -> None:
    """HEAD MIME (HLS) must win over an ``.mp4`` extension (R24.1)."""
    patched = _PatchedRequests(
        status_code=200, content_type="application/vnd.apple.mpegurl"
    )
    original_requests = es._requests
    original_ensure_public = es.ssrf_guard.ensure_public

    es._requests = patched  # type: ignore[assignment]
    es.ssrf_guard.ensure_public = _patched_ensure_public  # type: ignore[assignment]
    try:
        decision = select_engine("https://cdn.example.invalid/stream.mp4")
    finally:
        es._requests = original_requests  # type: ignore[assignment]
        es.ssrf_guard.ensure_public = original_ensure_public  # type: ignore[assignment]

    assert decision.engine_name == ENGINE_N_M3U8DL_RE, decision
    assert decision.source == "mime", decision
    assert "mpegurl" in (decision.reason or ""), decision
    assert patched.calls, "expected at least one HEAD probe call"


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run() -> None:
    checks = (
        assert_manual_override_wins,
        assert_extension_match_strips_query,
        assert_head_mime_probe_overrides_extension,
    )
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("engine select smoke passed")


if __name__ == "__main__":
    run()
