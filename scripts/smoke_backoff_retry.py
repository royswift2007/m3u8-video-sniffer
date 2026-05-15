"""
Stage 3 smoke: M3U8FetchThread backoff schedule (R15.1, R15.3, R25.1).

Simulates the R15 recoverable-failure path by patching
``core.m3u8_parser.requests.get`` with an in-memory transport that returns
``(429 → 500 → Timeout → 200)``. The script calls
:meth:`core.m3u8_parser.M3U8FetchThread._fetch_with_retry` directly (no Qt
event loop, no real network) and asserts:

* The retry loop performs exactly ``len(BACKOFF) + 1 = 4`` attempts before
  succeeding on the final try.
* The cumulative sleep time observed via a fake ``time.sleep`` lines up
  with ``sum(BACKOFF)`` within the ±20 % jitter budget (R15.3).
* ``ensure_public`` is invoked once per attempt (defence in depth against
  DNS rebinding between retries — design §3.1).
* Cancellation via ``stop_event`` inside the backoff sleep returns a
  ``StructuredError(code="cancelled")`` within one sleep step (R15.5).

Runs headless in <2s. Exits 0 on pass; non-zero with an explanatory
message on any deviation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# M3U8FetchThread inherits QThread. The Qt plugin is not required for
# pure Python method calls, but setting ``offscreen`` keeps the import
# deterministic on CI hosts without a display server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402

import requests  # noqa: E402

from core import m3u8_parser as mp  # noqa: E402
from utils.errors import StructuredError  # noqa: E402
from utils.retry import BACKOFF  # noqa: E402


# ---------------------------------------------------------------------------
# Fake HTTP transport.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in used by ``_fetch_once``.

    Only the attributes consumed by :meth:`M3U8FetchThread._fetch_once` are
    implemented: ``status_code``, ``url``, ``headers``, ``text``, and
    ``raise_for_status``. Keeping the surface small avoids accidentally
    exercising real HTTP behaviour via a leaked attribute access.
    """

    def __init__(self, status_code: int, body: str = "", url: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.headers: dict[str, str] = {}
        self.text = body

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            # ``requests`` wraps the response on HTTPError so the retry
            # loop can introspect ``e.response.status_code``. We mirror
            # that contract.
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err


def _build_scripted_transport(
    scripted: List[Any],
) -> Tuple[Callable[..., _FakeResponse], List[str]]:
    """Return a ``requests.get`` stand-in that yields ``scripted`` events.

    Each event in ``scripted`` is either:

    * ``int`` — an HTTP status code to return (``200`` success, ``429`` /
      ``5xx`` recoverable).
    * ``Exception`` instance — raised synchronously (use
      ``requests.Timeout`` / ``requests.ConnectionError`` to exercise
      the recoverable branch without network I/O).
    """

    calls: List[str] = []

    def _get(url: str, **_kwargs: Any) -> _FakeResponse:
        idx = len(calls)
        calls.append(url)
        if idx >= len(scripted):
            # Unexpected extra attempt — force a loud failure so the
            # smoke doesn't silently paper over a regression where the
            # retry count grows beyond the BACKOFF schedule.
            raise AssertionError(
                f"transport exhausted; unexpected attempt #{idx + 1}"
            )
        event = scripted[idx]
        if isinstance(event, Exception):
            raise event
        return _FakeResponse(int(event), body="#EXTM3U\n", url=url)

    return _get, calls


# ---------------------------------------------------------------------------
# Test scenarios.
# ---------------------------------------------------------------------------


def _install_patches(
    scripted: List[Any],
    *,
    sleep_log: List[float],
    ensure_calls: List[str],
    cancel_on_sleep: bool = False,
    stop_event: threading.Event | None = None,
) -> Tuple[Callable[..., _FakeResponse], List[str]]:
    """Monkey-patch ``requests.get``, ``interruptible_sleep`` and ``ensure_public``.

    ``core.m3u8_parser`` imports :mod:`requests` and
    :func:`interruptible_sleep` at module scope, so we patch those module
    references directly rather than the global
    ``utils.retry.interruptible_sleep``. Replacing
    ``interruptible_sleep`` (instead of ``time.sleep``) keeps the smoke
    instant without having to busy-wait for ``time.monotonic`` to
    advance inside the real interruptible-sleep loop.

    When ``cancel_on_sleep`` is True the patched sleep sets
    ``stop_event`` before returning ``False``, simulating a cancellation
    during backoff (R15.5).
    """

    transport, calls = _build_scripted_transport(scripted)
    mp.requests.get = transport  # type: ignore[assignment]

    # ``ensure_public`` must be called once per attempt (design §3.1).
    # The real function resolves DNS, which we cannot do offline; a
    # no-op recorder keeps the defence-in-depth contract observable
    # without touching the network.
    def _ensure_public(url: str, **_kwargs: Any) -> None:
        ensure_calls.append(url)

    mp.ensure_public = _ensure_public  # type: ignore[assignment]

    # Replace ``interruptible_sleep`` on the ``m3u8_parser`` module so
    # the retry loop returns instantly while we record the requested
    # duration for assertions. The real contract is preserved:
    #
    #   * ``True``  → full duration elapsed, continue retrying.
    #   * ``False`` → stop_event observed, loop should return cancelled.
    def _fake_interruptible_sleep(duration: float, event: threading.Event, step: float = 0.1) -> bool:
        sleep_log.append(duration)
        if cancel_on_sleep:
            if stop_event is not None:
                stop_event.set()
            return False
        return not event.is_set()

    mp.interruptible_sleep = _fake_interruptible_sleep  # type: ignore[assignment]

    return transport, calls


def _restore_patches(
    original_get: Callable[..., Any],
    original_ensure: Callable[..., Any],
    original_sleep: Callable[..., Any],
) -> None:
    mp.requests.get = original_get  # type: ignore[assignment]
    mp.ensure_public = original_ensure  # type: ignore[assignment]
    mp.interruptible_sleep = original_sleep  # type: ignore[assignment]


def _new_thread(url: str = "https://example.invalid/playlist.m3u8") -> mp.M3U8FetchThread:
    """Construct an ``M3U8FetchThread`` without starting its Qt event loop.

    We want the class's retry methods, not its thread. Instantiating is
    enough; ``run()`` is never invoked.
    """
    return mp.M3U8FetchThread(url=url, headers={})


def assert_backoff_sequence_four_attempts() -> None:
    """``429 → 500 → Timeout → 200`` drains BACKOFF and succeeds on attempt 4."""

    original_get = mp.requests.get
    original_ensure = mp.ensure_public
    original_sleep = mp.interruptible_sleep

    scripted: List[Any] = [
        429,
        500,
        requests.Timeout("read timeout"),
        200,
    ]
    sleep_log: List[float] = []
    ensure_calls: List[str] = []

    try:
        _, calls = _install_patches(
            scripted, sleep_log=sleep_log, ensure_calls=ensure_calls
        )
        thread = _new_thread()
        result = thread._fetch_with_retry(thread.url, dict(thread.headers))
    finally:
        _restore_patches(original_get, original_ensure, original_sleep)

    assert isinstance(result, str), f"expected success body, got {type(result).__name__}: {result!r}"
    assert result.startswith("#EXTM3U"), f"unexpected body: {result!r}"
    # Initial attempt + 3 retries == 4 transport calls.
    assert len(calls) == 4, f"expected 4 attempts, got {len(calls)}: {calls}"
    assert len(ensure_calls) == 4, (
        f"ensure_public must fire once per attempt (got {len(ensure_calls)})"
    )

    # Exactly ``len(BACKOFF)`` sleeps fire before the final success. Each
    # sleep is ``BACKOFF[i] * random.uniform(0.8, 1.2)``; summing and
    # comparing against the jittered envelope keeps the assertion tight
    # without making it flaky.
    assert len(sleep_log) == len(BACKOFF), (
        f"expected {len(BACKOFF)} sleeps, got {len(sleep_log)}: {sleep_log}"
    )
    lower = sum(BACKOFF) * 0.8 - 1e-6
    upper = sum(BACKOFF) * 1.2 + 1e-6
    total = sum(sleep_log)
    assert lower <= total <= upper, (
        f"total sleep {total:.3f}s outside jitter envelope "
        f"[{lower:.3f}s, {upper:.3f}s] (log={sleep_log})"
    )
    # Per-attempt sanity: each sleep falls in its own ±20% envelope.
    for i, slept in enumerate(sleep_log):
        expected = BACKOFF[i]
        assert expected * 0.8 - 1e-6 <= slept <= expected * 1.2 + 1e-6, (
            f"sleep[{i}] {slept:.3f}s outside BACKOFF[{i}]={expected}s jitter"
        )


def assert_backoff_exhaustion_returns_structured_error() -> None:
    """Four recoverable failures in a row → ``StructuredError(fetch_failed)``."""

    original_get = mp.requests.get
    original_ensure = mp.ensure_public
    original_sleep = mp.interruptible_sleep

    scripted: List[Any] = [
        requests.ConnectionError("reset"),
        500,
        requests.Timeout("read"),
        429,
    ]
    sleep_log: List[float] = []
    ensure_calls: List[str] = []

    try:
        _install_patches(
            scripted, sleep_log=sleep_log, ensure_calls=ensure_calls
        )
        thread = _new_thread()
        result = thread._fetch_with_retry(thread.url, dict(thread.headers))
    finally:
        _restore_patches(original_get, original_ensure, original_sleep)

    assert isinstance(result, StructuredError), (
        f"expected StructuredError, got {type(result).__name__}: {result!r}"
    )
    assert result.code == "fetch_failed", result
    assert result.reason == "max_retries_exhausted", result
    # All 4 attempts fired and all 3 sleeps ran before the loop gave up.
    assert len(sleep_log) == len(BACKOFF), sleep_log
    assert len(ensure_calls) == 4, ensure_calls


def assert_cancel_during_backoff_returns_cancelled() -> None:
    """``stop_event.set()`` during a backoff sleep → ``cancelled`` StructuredError."""

    original_get = mp.requests.get
    original_ensure = mp.ensure_public
    original_sleep = mp.interruptible_sleep

    stop_event = threading.Event()
    # Fail the first attempt so the loop enters the backoff branch,
    # then the patched ``interruptible_sleep`` trips ``stop_event`` and
    # returns False, short-circuiting into the ``cancelled`` branch.
    scripted: List[Any] = [requests.Timeout("first")]
    sleep_log: List[float] = []
    ensure_calls: List[str] = []

    try:
        _install_patches(
            scripted,
            sleep_log=sleep_log,
            ensure_calls=ensure_calls,
            cancel_on_sleep=True,
            stop_event=stop_event,
        )
        thread = _new_thread()
        thread.stop_event = stop_event
        result = thread._fetch_with_retry(thread.url, dict(thread.headers))
    finally:
        _restore_patches(original_get, original_ensure, original_sleep)

    assert isinstance(result, StructuredError), result
    assert result.code == "cancelled", result
    assert result.reason == "stop_event set during backoff", result
    # Only the first attempt runs; the stop fires during the first sleep.
    assert len(sleep_log) == 1, sleep_log


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run() -> None:
    checks = (
        assert_backoff_sequence_four_attempts,
        assert_backoff_exhaustion_returns_structured_error,
        assert_cancel_during_backoff_returns_cancelled,
    )
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("backoff retry smoke passed")


if __name__ == "__main__":
    run()
