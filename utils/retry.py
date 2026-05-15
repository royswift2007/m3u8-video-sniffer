"""
Network retry / backoff primitives (Requirements R15.1, R15.3).

This module is intentionally tiny and dependency-free (standard library
only). It exposes:

- ``BACKOFF``: the canonical exponential backoff schedule used by the
  M3U8 sniffer/parser network paths (``(0.5, 1.5, 3.0)`` seconds).
- ``interruptible_sleep(total, stop_event, step=0.1)``: a cancellable
  sleep that wakes within ``step`` seconds of ``stop_event.set()``.

Jitter is intentionally *not* applied here. Callers are expected to
multiply the chosen ``BACKOFF[attempt]`` by ``random.uniform(0.8, 1.2)``
(±20%) before passing the result to :func:`interruptible_sleep`. Keeping
jitter out of this module makes the retry math deterministic under test
and lets different call sites pick different jitter policies if needed.

Example (pseudocode, see design §3.1)::

    import random
    from utils.retry import BACKOFF, interruptible_sleep

    for attempt in range(len(BACKOFF) + 1):
        if stop_event.is_set():
            return StructuredError("cancelled", ...)
        try:
            resp = session.get(url, timeout=(5, 15), verify=True)
        except RECOVERABLE as e:
            if attempt == len(BACKOFF):
                return StructuredError("fetch_failed", details={"reason": repr(e)})
            delay = BACKOFF[attempt] * random.uniform(0.8, 1.2)
            if not interruptible_sleep(delay, stop_event):
                return StructuredError("cancelled", ...)
            continue
"""

from __future__ import annotations

import threading
import time
from typing import Tuple


__all__ = ("BACKOFF", "interruptible_sleep")


#: Canonical exponential backoff schedule (seconds) for recoverable
#: network errors. Up to ``len(BACKOFF)`` retries are performed, so the
#: total number of attempts is ``len(BACKOFF) + 1`` (initial + retries).
BACKOFF: Tuple[float, float, float] = (0.5, 1.5, 3.0)


def interruptible_sleep(
    total: float,
    stop_event: threading.Event,
    step: float = 0.1,
) -> bool:
    """Sleep for ``total`` seconds, waking early if ``stop_event`` fires.

    The sleep is chopped into slices of at most ``step`` seconds so the
    thread observes ``stop_event`` within roughly one ``step`` interval
    (R15.3 requires ≤ 100 ms).

    Args:
        total: Total number of seconds to sleep. Values ``<= 0`` return
            immediately with ``True`` (nothing to wait for, no
            cancellation observed).
        stop_event: A :class:`threading.Event` used to request
            cancellation. Checked before each slice.
        step: Maximum length of a single sleep slice, in seconds.
            Defaults to ``0.1`` (100 ms). Non-positive values are
            clamped up to ``0.1`` to preserve responsiveness and to
            avoid a busy loop.

    Returns:
        ``True`` if the full duration elapsed naturally.
        ``False`` if ``stop_event`` was observed set before the
        duration elapsed (i.e. the sleep was cancelled).

    Notes:
        The return convention intentionally matches the design
        document: call sites use ``if not interruptible_sleep(...):``
        as the cancellation branch.

        This function never raises for non-negative ``total``; it only
        uses ``time.monotonic`` and ``time.sleep`` internally and
        clamps the final slice to a non-negative value to avoid a
        ``ValueError`` from ``time.sleep`` if the deadline is crossed
        mid-iteration on a loaded system.
    """

    if step <= 0:
        step = 0.1

    # Fast path: nothing to wait for. Report natural completion, and
    # still honour an already-set stop_event as cancellation so callers
    # can uniformly rely on the return value.
    if total <= 0:
        return not stop_event.is_set()

    end = time.monotonic() + total
    while True:
        if stop_event.is_set():
            return False
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        # Clamp to non-negative; min() is safe because both operands
        # are > 0 here, but the max(0.0, ...) guard keeps us correct
        # even if `step` is somehow exhausted in a degenerate future
        # refactor.
        time.sleep(max(0.0, min(step, remaining)))
