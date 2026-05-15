"""
Stop-response benchmark for Stage 2 smoke gate (task 14.1 / R14).

This smoke test spins up 20 concurrent ``DownloadTask``-style mocks driven by
``BaseEngine.read_loop``, lets them run for ~2s, then issues stop commands at
randomized times and measures the wall-clock latency from "stop requested" to
"process exited". Requirement 14.4 mandates P95 ≤ 2000ms; the script exits 0
when that holds, non-zero otherwise.

Design considerations (see design.md §2.6 / tasks.md 14.1):

* **No real network.** Each "engine" is a short-lived Python subprocess
  spawned via ``subprocess.Popen`` that prints a few hundred ticks at ~20Hz.
  Stop latency is dominated by the read_loop termination path, which is
  exactly what we want to measure.
* **No Qt.** The script avoids ``core.download_manager`` and any UI imports
  so it can run headless inside the stage gate.
* **Deterministic-ish.** Random delays are seeded through ``RANDOM_SEED``
  (env var) for CI reproducibility when needed; absent the env var we use
  system entropy and only assert the P95 threshold.
* **Reuses BaseEngine.read_loop.** The loop under test is the same one
  Stage 2 R9 shipped; benchmarking any stand-in would miss the point.
"""

from __future__ import annotations

import os
import random
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ``engines.base_engine`` / ``core.task_model`` are Qt-free; importing them
# headlessly is safe even when PyQt6 isn't installed in the stage-gate
# environment.
from core.task_model import DownloadTask  # noqa: E402
from engines.base_engine import BaseEngine, EngineResult  # noqa: E402


# ---------------------------------------------------------------------------
# Tunables — kept small so the benchmark completes quickly inside CI.
# ---------------------------------------------------------------------------

NUM_TASKS = 20
RUN_BEFORE_STOP_MIN_S = 0.5
RUN_BEFORE_STOP_MAX_S = 2.0
P95_BUDGET_S = 2.0
OVERALL_DEADLINE_S = 60.0  # absolute upper bound for the whole benchmark
TICK_COUNT = 4000  # enough to outlast the longest random stop delay
TICK_INTERVAL_S = 0.05


# ---------------------------------------------------------------------------
# Minimal BaseEngine subclass (no real download path).
# ---------------------------------------------------------------------------


class _BenchmarkEngine(BaseEngine):
    """Stub engine that satisfies the abstract contract without side effects.

    ``read_loop`` is inherited from :class:`BaseEngine` and is the actual
    subject of the benchmark.
    """

    def download(self, task, progress_callback):  # pragma: no cover - unused
        return False

    def parse_progress(self, line):  # pragma: no cover - unused
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url):  # pragma: no cover - unused
        return False

    def get_name(self):
        return "benchmark_stub"


# ---------------------------------------------------------------------------
# Per-task runner.
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    task_id: int
    stop_latency_s: Optional[float]
    status: str
    returncode: Optional[int]
    error: Optional[str] = None


def _spawn_chatty_child() -> subprocess.Popen:
    """Spawn a Python child that emits ``TICK_COUNT`` progress lines.

    The child writes alternating stdout/stderr lines at ``TICK_INTERVAL_S``
    cadence so both pump threads are exercised. It exits naturally if we
    never stop it (safety net against runaway children).
    """

    code = (
        "import sys, time\n"
        f"for i in range({TICK_COUNT}):\n"
        "    sys.stdout.write(f'out {i}\\n')\n"
        "    sys.stdout.flush()\n"
        "    if i % 3 == 0:\n"
        "        sys.stderr.write(f'err {i}\\n')\n"
        "        sys.stderr.flush()\n"
        f"    time.sleep({TICK_INTERVAL_S})\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _run_one_task(
    task_id: int,
    engine: BaseEngine,
    stop_delay_s: float,
    barrier: threading.Barrier,
    results: List[TaskResult],
    results_lock: threading.Lock,
) -> None:
    """Spawn a child process, wait ``stop_delay_s``, request stop, measure.

    The per-task barrier ensures all 20 workers enter ``read_loop`` at roughly
    the same instant so we benchmark genuine concurrency, not serialized
    spawn overhead.
    """

    task = DownloadTask(
        url=f"https://example.invalid/benchmark/{task_id}",
        save_dir=".",
        filename=f"bench-{task_id}",
        headers={},
    )

    # Every invocation discards its on_line output; we only care about the
    # wall-clock latency between stop request and loop exit. Defining the
    # callback inline avoids a module-level name collision.
    def _noop(tag: str, text: str) -> None:  # noqa: ARG001
        return None

    result_status = "unknown"
    result_rc: Optional[int] = None
    stop_latency: Optional[float] = None
    err: Optional[str] = None

    try:
        proc = _spawn_chatty_child()
    except Exception as exc:  # pragma: no cover - defensive
        with results_lock:
            results.append(
                TaskResult(
                    task_id=task_id,
                    stop_latency_s=None,
                    status="spawn_failed",
                    returncode=None,
                    error=repr(exc),
                )
            )
        return

    # Scheduler that flips ``stop_requested`` after ``stop_delay_s`` seconds.
    stop_event = threading.Event()
    stop_at_monotonic: List[float] = []

    def _stop_later() -> None:
        # Align to the read_loop start instant signaled by the barrier below.
        stop_event.wait(timeout=OVERALL_DEADLINE_S)
        time.sleep(stop_delay_s)
        now = time.monotonic()
        stop_at_monotonic.append(now)
        # ``cancelled`` is the common case in UI: the user clicks "stop".
        # ``paused`` and ``engine_switch`` are covered by dedicated unit
        # tests; we pick ``cancelled`` here for a uniform measurement.
        task.stop_reason = "cancelled"
        task.stop_requested = True

    stopper = threading.Thread(
        target=_stop_later, name=f"stopper-{task_id}", daemon=True
    )
    stopper.start()

    try:
        barrier.wait(timeout=OVERALL_DEADLINE_S)
    except threading.BrokenBarrierError:
        err = "barrier broken"

    # Release the stop scheduler now that read_loop is about to begin.
    stop_event.set()

    try:
        engine_result: EngineResult = engine.read_loop(proc, task, _noop)
        # Wall-clock latency: "loop exit" minus "stop flag flipped". We read
        # ``stop_at_monotonic[0]`` defensively in case the stopper thread
        # raced past the barrier but hasn't actually flipped the flag yet
        # (shouldn't happen with the barrier + event handshake, but we
        # prefer a missing measurement over a bogus one).
        if stop_at_monotonic:
            stop_latency = time.monotonic() - stop_at_monotonic[0]
        result_status = engine_result.status
        result_rc = engine_result.returncode
    except Exception as exc:
        err = repr(exc)
    finally:
        # Ensure the child is reaped even if read_loop returned early.
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass

    with results_lock:
        results.append(
            TaskResult(
                task_id=task_id,
                stop_latency_s=stop_latency,
                status=result_status,
                returncode=result_rc,
                error=err,
            )
        )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def _percentile(values: List[float], pct: float) -> float:
    """Compute a simple nearest-rank percentile.

    ``statistics.quantiles`` is avoided because it requires len(values) > 1
    and behaves awkwardly at the P95 edge for exactly 20 samples. The
    ceiling-indexed nearest-rank definition is well-defined for any
    non-empty list and places P95 of 20 points on the 19th (1-indexed)
    element, matching the classical textbook definition.
    """

    if not values:
        raise ValueError("empty values")
    import math

    sorted_vals = sorted(values)
    idx = max(
        0,
        min(
            len(sorted_vals) - 1,
            math.ceil(pct / 100.0 * len(sorted_vals)) - 1,
        ),
    )
    return sorted_vals[idx]


def main(argv: Optional[List[str]] = None) -> int:
    # ``argv`` is accepted for symmetry with other smoke scripts; the
    # benchmark has no flags today but future tuning knobs (override
    # NUM_TASKS, P95_BUDGET_S) will land here without a CLI breakage.
    del argv

    seed_env = os.environ.get("RANDOM_SEED")
    if seed_env is not None:
        try:
            random.seed(int(seed_env))
        except ValueError:
            random.seed(seed_env)

    engine = _BenchmarkEngine(binary_path="")

    results: List[TaskResult] = []
    results_lock = threading.Lock()
    # ``parties=NUM_TASKS`` — every task must reach the barrier before the
    # benchmark clock starts. Add +1 so the main thread can also wait on
    # the barrier and start its observation window in lock-step.
    barrier = threading.Barrier(NUM_TASKS + 1, timeout=OVERALL_DEADLINE_S)

    threads: List[threading.Thread] = []
    for i in range(NUM_TASKS):
        # Randomize stop delay so we exercise "early stop" (child still
        # mid-output) and "late stop" (child nearly idle) paths.
        delay = random.uniform(RUN_BEFORE_STOP_MIN_S, RUN_BEFORE_STOP_MAX_S)
        t = threading.Thread(
            target=_run_one_task,
            name=f"bench-{i}",
            args=(i, engine, delay, barrier, results, results_lock),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Sync with the workers: once every thread has spawned its child we
    # release the barrier and let the concurrent phase begin.
    try:
        barrier.wait(timeout=OVERALL_DEADLINE_S)
    except threading.BrokenBarrierError:
        print(
            "[stop_response_benchmark] FAIL: barrier broken — not all tasks "
            "reached the synchronized start",
            file=sys.stderr,
        )
        return 1

    t_start = time.monotonic()
    for t in threads:
        remaining = OVERALL_DEADLINE_S - (time.monotonic() - t_start)
        if remaining <= 0:
            break
        t.join(timeout=remaining)

    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        print(
            f"[stop_response_benchmark] FAIL: threads still alive after "
            f"{OVERALL_DEADLINE_S}s: {alive}",
            file=sys.stderr,
        )
        return 1

    latencies_ms: List[float] = []
    stopped_count = 0
    for r in results:
        if r.stop_latency_s is None:
            continue
        latencies_ms.append(r.stop_latency_s * 1000.0)
        if r.status == "stopped":
            stopped_count += 1

    if len(latencies_ms) < NUM_TASKS:
        print(
            f"[stop_response_benchmark] FAIL: only {len(latencies_ms)} of "
            f"{NUM_TASKS} tasks produced a measurement",
            file=sys.stderr,
        )
        for r in results:
            if r.stop_latency_s is None:
                print(f"  task {r.task_id}: status={r.status} error={r.error}", file=sys.stderr)
        return 1

    p50 = statistics.median(latencies_ms)
    p95 = _percentile(latencies_ms, 95)
    p99 = _percentile(latencies_ms, 99)
    maxv = max(latencies_ms)
    minv = min(latencies_ms)

    print(
        "[stop_response_benchmark] "
        f"n={len(latencies_ms)} stopped_status={stopped_count} "
        f"min={minv:.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms "
        f"max={maxv:.1f}ms budget_p95={P95_BUDGET_S * 1000:.0f}ms"
    )

    if p95 > P95_BUDGET_S * 1000:
        print(
            f"[stop_response_benchmark] FAIL: P95 {p95:.1f}ms > budget "
            f"{P95_BUDGET_S * 1000:.0f}ms (Requirement 14.4)",
            file=sys.stderr,
        )
        return 1

    # Sanity check: every task should have been classified as ``stopped``
    # (we flipped ``stop_reason="cancelled"``); a mismatch indicates the
    # benchmark scenario drifted and future runs should re-verify.
    if stopped_count != NUM_TASKS:
        print(
            f"[stop_response_benchmark] WARN: expected {NUM_TASKS} stopped "
            f"results, got {stopped_count}. Individual statuses:",
            file=sys.stderr,
        )
        for r in results:
            print(f"  task {r.task_id}: status={r.status} rc={r.returncode}", file=sys.stderr)
        # Status drift is informative but not a hard failure; the P95
        # measurement above is the authoritative gate per R14.4.

    print("[stop_response_benchmark] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
