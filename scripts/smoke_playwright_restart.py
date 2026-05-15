"""
Stage 3 smoke: PlaywrightDriver repeated start/stop cleanup (R21, R25.1).

Spins up :class:`core.playwright_driver.PlaywrightDriver` a handful of times
(3, not 20 — 20 is listed as a manual-only item in tasks.md 23.1 because it
is too slow for CI) and after each iteration verifies no
``temp_profile_*`` / ``profile_*`` directories leak under the fallback
profile root (R21.AC2 / R21.AC3).

Design notes:

* **Real Playwright required.** If ``playwright`` is not installed OR the
  browser binaries are missing, the smoke exits 0 with a clear
  ``SKIP`` message so the Stage 3 gate can still complete on hosts
  without a full browser stack. The manual checklist covers the
  "actually starts a real browser" case.
* **Graceful shutdown.** Each iteration calls
  :meth:`PlaywrightDriver.quit` (inherited Qt thread teardown) and waits
  for the thread to finish before counting leftover profile dirs.
* **Leak heuristic.** Between iterations we record the profile-root
  directory count; the count must not grow across iterations. We allow
  equal counts because :meth:`_cleanup_stale_profile_dirs` runs at
  start-up and may prune historical leftovers on the first boot.

Runs headless in <15s when Playwright is installed. Exits 0 on pass or
skip; non-zero only when a real leak is observed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Number of start/stop iterations. 3 is enough to catch linear growth; the
# manual checklist covers the full 20-iteration stress from R25.1.
ITERATIONS = 3
# Per-iteration wall-clock budget before we force-stop the driver.
PER_ITERATION_BUDGET_S = 10.0


def _skip(reason: str) -> None:
    """Exit 0 with a ``SKIP`` banner the stage gate can grep for."""
    print(f"[playwright_restart] SKIP: {reason}", flush=True)
    sys.exit(0)


def _count_profile_leftovers(profile_root: Path) -> int:
    """Count ``temp_profile_*`` / ``profile_*`` dirs still on disk."""
    if not profile_root.exists():
        return 0
    prefixes = ("profile_", "temp_profile_")
    return sum(
        1
        for child in profile_root.iterdir()
        if child.is_dir() and child.name.startswith(prefixes)
    )


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        _skip("playwright package not installed")
        return 0  # unreachable; _skip exits

    # Probe that the browser binaries are actually installed. If they are
    # missing, ``sync_playwright`` raises deep inside the driver; skip
    # early rather than churning through 3 failed launches.
    try:
        from playwright.sync_api import sync_playwright as _sp

        with _sp() as p:
            try:
                browser_path = p.chromium.executable_path
            except Exception:  # pragma: no cover - defensive
                browser_path = None
            if browser_path and not Path(browser_path).exists():
                _skip(
                    f"playwright chromium executable missing at {browser_path}"
                )
    except Exception as exc:
        _skip(f"playwright sanity check failed: {exc}")
        return 0

    # Delay these imports so the skip path above can exit without
    # touching QThread or the driver module.
    from PyQt6.QtCore import QCoreApplication  # noqa: E402
    from core import playwright_profile  # noqa: E402
    from core.playwright_driver import PlaywrightDriver  # noqa: E402

    app = QCoreApplication.instance() or QCoreApplication([])

    profile_root = Path(playwright_profile.get_profile_root()).resolve() \
        if hasattr(playwright_profile, "get_profile_root") else None
    if profile_root is None:
        # Fall back to the same path the driver's cleanup uses.
        try:
            from core.app_paths import get_temp_dir

            profile_root = Path(get_temp_dir()).resolve()
        except ImportError:
            _skip("cannot resolve playwright profile root")
            return 0

    # Record the baseline count so we only flag *new* leftovers.
    baseline = _count_profile_leftovers(profile_root)

    for i in range(ITERATIONS):
        driver = PlaywrightDriver(headless=True)
        driver.start()
        # Give the thread a moment to initialise; we don't need the
        # browser to actually navigate for the cleanup contract to be
        # exercised.
        deadline = time.monotonic() + PER_ITERATION_BUDGET_S
        while time.monotonic() < deadline and not driver.isRunning():
            QCoreApplication.processEvents()
            time.sleep(0.05)
        # Ask the driver to shut down; the QThread teardown path runs
        # ``_cleanup_temporary_profile_dir`` inside its own ``finally``.
        driver.active = False
        driver.quit()
        driver.wait(int(PER_ITERATION_BUDGET_S * 1000))

        current = _count_profile_leftovers(profile_root)
        # R21 invariant: leftover count must not grow linearly with
        # iteration count. Equality with baseline is fine; growth is a
        # leak.
        if current > baseline:
            print(
                f"[playwright_restart] FAIL: iteration {i + 1} leaked "
                f"temp_profile_* dirs (baseline={baseline}, now={current})",
                file=sys.stderr,
            )
            return 1
        print(
            f"[playwright_restart] iter {i + 1}/{ITERATIONS} "
            f"leftovers={current} (baseline={baseline})"
        )

    # Keep ``app`` referenced until the end so Python doesn't GC it
    # while worker threads are still winding down.
    del app

    print("[playwright_restart] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
