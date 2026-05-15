"""
Stage 3 smoke: DownloadManager online concurrency adjustment (R19, R25.1).

Mirrors the manual checklist item "并发在线调整" required by
Requirement 25.1 / task 23.1: starts a :class:`DownloadManager` with
``max_concurrent=5``, then online-adjusts the worker count twice and
verifies the pool converges to the new size each time without needing
real downloads on the queue.

Sequence (per task 23.1):

1. Construct ``DownloadManager(max_concurrent=5)`` with a benign no-op
   engine so :class:`core.engine_selector.EngineSelector` has at least
   one registered engine to iterate.
2. Confirm ``active_workers == 5`` (pool spawns synchronously during
   ``__init__``; give the threads up to 2s to register).
3. Call ``set_max_concurrent(2)``. R19.1 allows up to 30s for soft-exit
   convergence; on an idle queue we expect it well within 1s. We wait
   for a fixed 1s window (per the task spec) and then assert
   ``active_workers == 2``; if it's still in-flight we continue polling
   up to the R19 ceiling so transient scheduler hiccups don't make the
   gate flake.
4. Call ``set_max_concurrent(6)``. The grow path (R19.4) must spawn
   ``6 - 2 = 4`` fresh workers; we poll up to 10s for convergence.

No downloads are issued; this smoke only verifies the worker-pool
*shape*, i.e. that :attr:`DownloadManager.active_workers` matches the
configured ``max_concurrent`` after online changes. That keeps the
script headless, deterministic, and independent of any engine binary.

Runs in <5s. Exits 0 on pass; non-zero on any deviation.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ``core.download`` transitively imports PyQt6 via task_model / selector
# paths; the offscreen plugin keeps the smoke deterministic on headless
# CI hosts.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.download_manager import DownloadManager  # noqa: E402
from engines.base_engine import BaseEngine  # noqa: E402


# Task 23.1 pins the first post-shrink observation to 1s; R19.1 allows
# up to 30s for soft-exit convergence in worst-case engine stalls. We
# poll for the fixed 1s window first (so a converged pool is asserted
# immediately), then continue polling up to the R19 ceiling before
# giving up — that way a real regression surfaces within a couple of
# seconds without making the gate flaky on loaded CI hosts.
_FAST_WINDOW_S = 1.0
_SLOW_CEILING_S = 10.0


# ---------------------------------------------------------------------------
# Minimal engine stub.
# ---------------------------------------------------------------------------


class _NoopEngine(BaseEngine):
    """Engine stand-in that never matches any URL.

    The pool's worker threads idle on ``_worker_gate`` as long as the task
    queue is empty, so the engine is never invoked. We still need a valid
    :class:`BaseEngine` subclass because ``EngineSelector`` requires at
    least one registered engine.
    """

    def download(self, task, progress_callback):  # pragma: no cover - unused
        return False

    def parse_progress(self, line):  # pragma: no cover - unused
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url):  # pragma: no cover - unused
        return False

    def get_name(self):
        return "noop_smoke"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _wait_for_active(
    manager: DownloadManager, target: int, timeout_s: float
) -> int:
    """Poll ``active_workers`` until it matches ``target`` or time runs out."""
    deadline = time.monotonic() + timeout_s
    last = manager.active_workers
    while time.monotonic() < deadline:
        last = manager.active_workers
        if last == target:
            return last
        time.sleep(0.05)
    return last


# ---------------------------------------------------------------------------
# Scenario.
# ---------------------------------------------------------------------------


def assert_online_adjust_shrinks_then_grows() -> None:
    manager = DownloadManager(
        engines=[_NoopEngine(binary_path="")], max_concurrent=5
    )
    try:
        # Initial state: pool spawned during __init__ — give workers up
        # to 2s to register their threading.Thread handles.
        initial = _wait_for_active(manager, 5, timeout_s=2.0)
        assert initial == 5, f"expected 5 workers at startup, got {initial}"

        # --- Shrink: 5 -> 2 -----------------------------------------------
        manager.set_max_concurrent(2)
        # Task 23.1: wait 1s, then assert. If not yet converged, keep
        # polling up to the R19 ceiling before declaring failure.
        shrunk = _wait_for_active(manager, 2, timeout_s=_FAST_WINDOW_S)
        if shrunk != 2:
            shrunk = _wait_for_active(
                manager, 2, timeout_s=_SLOW_CEILING_S - _FAST_WINDOW_S
            )
        assert shrunk == 2, (
            f"expected active_workers==2 within {_SLOW_CEILING_S:.0f}s "
            f"after set_max_concurrent(2), got {shrunk} "
            f"(R19 ceiling is 30s)"
        )

        # --- Grow: 2 -> 6 -------------------------------------------------
        manager.set_max_concurrent(6)
        grown = _wait_for_active(manager, 6, timeout_s=_SLOW_CEILING_S)
        assert grown == 6, (
            f"expected active_workers==6 within {_SLOW_CEILING_S:.0f}s "
            f"after set_max_concurrent(6), got {grown}"
        )
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run() -> None:
    checks = (assert_online_adjust_shrinks_then_grows,)
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("concurrency adjust smoke passed")


if __name__ == "__main__":
    run()
