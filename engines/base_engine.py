"""
Base engine class for all download engines.

This module provides:

* :class:`BaseEngine` — the abstract base that concrete engines (n_m3u8dl_re,
  aria2, streamlink, yt-dlp) inherit from. Existing ``download()`` and
  ``parse_progress()`` contracts are preserved so that in-flight engine
  implementations keep working during the Stage 2 migration (see task 9.2).

* :class:`EngineResult` — structured engine outcome used by
  :meth:`BaseEngine.read_loop`. The shape matches design 2.1 / 3.1 and task 9.1.

* :meth:`BaseEngine.read_loop` — non-blocking read/terminate/kill loop built on
  top of two daemon :func:`_pump` threads and a bounded :class:`queue.Queue`.
  Concrete engines adopt it in task 9.2 to unify the stop-request handling and
  PIPE draining that Requirement 9 / 10 demand.

The read loop itself keeps the decoding logic minimal: ``utf-8 → mbcs →
latin-1`` fallback with throttled telemetry (``decode=mbcs`` /
``decode=lossy`` tagging, emitted once per ``_DECODE_TELEMETRY_INTERVAL``
occurrences) added in task 20.1.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Optional, Sequence, Tuple

from core.task_model import DownloadTask
from utils.errors import StopReason, StructuredError
from utils.logger import logger
from utils.redact import redact_argv


# ---------------------------------------------------------------------------
# Public result contract (design 2.1 / 3.1, task 9.1 AC)
# ---------------------------------------------------------------------------

EngineStatus = Literal[
    "ok",
    "failed",
    "paused",
    "stopped",
    "switched",
    "timeout_exceeded",
]


@dataclass(frozen=True)
class EngineResult:
    """Structured outcome returned by engine read loops.

    Attributes
    ----------
    status:
        High-level disposition of the run. ``ok`` / ``failed`` cover natural
        process completion; ``paused`` / ``stopped`` / ``switched`` cover
        user-driven stop requests (see :class:`utils.errors.StopReason`);
        ``timeout_exceeded`` is reserved for the FFmpeg hard timeout path
        introduced in Requirement 10.
    returncode:
        The subprocess return code when available. ``None`` is permitted for
        engine results that short-circuit before the process is spawned.
    resumable:
        ``True`` when the task can be resumed without re-downloading existing
        artifacts (currently only ``status="paused"``).
    error:
        Optional structured error payload for ``failed`` / ``timeout_exceeded``
        / stop-reason-derived failures.
    """

    status: EngineStatus
    returncode: Optional[int] = None
    resumable: bool = False
    error: Optional[StructuredError] = None


# ---------------------------------------------------------------------------
# Internal tunables
# ---------------------------------------------------------------------------

# Queue max size per design 2.1 pseudo-code.
_QUEUE_MAXSIZE = 1024

# Main-loop queue.get timeout — keeps stop-request latency < 100ms.
_GET_TIMEOUT = 0.1

# Grace period between terminate() and escalating to kill_process_tree.
# 0.5s grace per task AC, 1.5s total wall-clock budget.
_TERMINATE_GRACE = 0.5
_KILL_DEADLINE = 1.5

# Maximum wait for the _pump threads to flush after proc exits. The pumps
# already hit EOF the moment the child process closes its pipes; 0.25s is
# comfortably more than enough on a loaded Windows host.
_PUMP_JOIN_TIMEOUT = 0.25

# Maximum time a pump thread waits to enqueue a line before dropping it.
# Defends against a wedged ``on_line`` callback from permanently blocking the
# producer.
_PUT_TIMEOUT = 5.0


# Decode telemetry throttle (task 20.1). Emitting a DEBUG line for every GBK /
# lossy line would flood the log on long noisy runs; instead we emit the
# first occurrence immediately and then once per N further hits. The counters
# are global because ``_decode_line`` is a module-level helper shared across
# every engine instance and every read_loop thread.
_DECODE_TELEMETRY_INTERVAL = 256
_decode_telemetry_lock = threading.Lock()
_decode_mbcs_count = 0
_decode_lossy_count = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_line(raw: bytes) -> str:
    """Best-effort byte→text decode for engine output.

    3-step ladder per Requirement 20.1 / task 20.1:

    * ``utf-8`` — engines that honor ``--encoding utf-8`` or set it by default.
    * ``mbcs`` — Windows ANSI code page (CP936 in zh-CN); n_m3u8dl_re and
      legacy aria2 builds emit progress lines in CP936. Tagged
      ``decode=mbcs`` in a throttled DEBUG telemetry line.
    * ``latin-1`` — last-resort lossy decode so bytes survive as codepoints.
      Tagged ``decode=lossy`` in a throttled DEBUG telemetry line.

    The returned text preserves any trailing ``\\r\\n`` / ``\\r`` bytes the
    source stream produced; the :meth:`BaseEngine.read_loop` consumer is
    responsible for trimming the line-terminator for display while keeping
    ``\\r`` progress markers intact (Requirement 20.3).
    """

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    try:
        text = raw.decode("mbcs")
    except (UnicodeDecodeError, LookupError):
        # ``LookupError`` fires on non-Windows hosts where the ``mbcs`` codec
        # is unavailable; fall through to latin-1.
        pass
    else:
        _record_decode_telemetry("mbcs")
        return text

    # latin-1 can't raise — every byte has a defined codepoint. ``errors=
    # "replace"`` is belt-and-suspenders in case a future Python release
    # tightens the codec behavior.
    _record_decode_telemetry("lossy")
    return raw.decode("latin-1", errors="replace")


def _record_decode_telemetry(tag: str) -> None:
    """Emit a throttled DEBUG line for a non-UTF-8 decode outcome.

    The first occurrence is always logged; subsequent occurrences are logged
    once per :data:`_DECODE_TELEMETRY_INTERVAL` hits so high-volume CP936
    output (e.g. a 4000-segment download) doesn't swamp the main log.
    """

    global _decode_mbcs_count, _decode_lossy_count

    with _decode_telemetry_lock:
        if tag == "mbcs":
            _decode_mbcs_count += 1
            count = _decode_mbcs_count
        elif tag == "lossy":
            _decode_lossy_count += 1
            count = _decode_lossy_count
        else:  # pragma: no cover - defensive
            return

    if count == 1 or count % _DECODE_TELEMETRY_INTERVAL == 0:
        try:
            logger.debug(
                f"[base_engine] decode={tag} count={count}"
            )
        except (OSError, ValueError, AttributeError):  # pragma: no cover - logger is defensive
            # We are already inside a telemetry emitter for the logging
            # subsystem; any further logger call here risks recursion.
            pass


def _engine_name_matches(actual: str | None, expected: str) -> bool:
    """Case-insensitive, ``.exe``-insensitive name comparison.

    security-stability-hardening R30.1 (tasks.md 27.1): ``kill_process_tree``
    must refuse to terminate a pid whose image name does not match the
    engine the caller believes is running there. Pid reuse is the concrete
    risk — the OS may have recycled the pid for an unrelated process after
    the engine exited but before the manager's cleanup ran.

    The comparison is intentionally lenient so callers don't have to care
    about the ``.exe`` suffix convention:

    * ``"yt-dlp"``      matches ``"yt-dlp.exe"``
    * ``"python.exe"``  matches ``"python"``
    * ``"ffmpeg.EXE"``  matches ``"FFmpeg"`` (case folded)

    Implementation strips a trailing ``.exe`` (case-insensitive) from both
    sides and compares for equality, OR accepts a substring match when one
    side cleanly embeds the other (e.g. a hypothetical
    ``"n-m3u8dl-re_x64"`` vs ``"n_m3u8dl_re"``). Substring matching stays
    guarded by a minimum length of 3 so accidental one-letter collisions
    cannot bypass the check.
    """

    if actual is None or not expected:
        return False

    def _norm(s: str) -> str:
        s = s.strip().lower()
        if s.endswith(".exe"):
            s = s[:-4]
        return s

    a = _norm(actual)
    e = _norm(expected)
    if not a or not e:
        return False
    if a == e:
        return True
    # Allow lenient substring match in either direction — see docstring.
    if len(a) >= 3 and len(e) >= 3 and (a in e or e in a):
        return True
    return False


def kill_process_tree(
    pid: int, *, expected_name: Optional[str] = None
) -> str:
    """Best-effort process-tree termination helper.

    Mirrors the semantics of ``DownloadManager._kill_process_tree`` but lives
    at module scope so the :meth:`BaseEngine.read_loop` escalation path can
    call it without an instance of ``DownloadManager``. Safe to call when the
    process has already exited; all exceptions are swallowed and logged.

    Parameters
    ----------
    pid:
        Process id to terminate.
    expected_name:
        Optional engine image name (e.g. ``"yt-dlp"``, ``"ffmpeg.exe"``).
        When provided, :func:`psutil.Process.name` is consulted first and the
        kill is skipped entirely if the actual image name does not match
        (case-insensitive, ``.exe``-insensitive — see
        :func:`_engine_name_matches`). This guards against pid-reuse races
        in the engine-switch path, where the manager may still hold a pid
        referencing a long-dead engine whose slot has been recycled by the
        OS (R30.1, design §4.5).

    Returns
    -------
    str
        * ``"ok"``              — process tree terminated (or already gone).
        * ``"pid_mismatch"``    — ``expected_name`` did not match; nothing
          killed. The caller should treat this as a successful no-op for
          safety (the "real" engine process, if any, is already dead).
        * ``"no_such_process"`` — pid not found before the kill attempt;
          treated as success by callers that ignore the return value.

    Notes
    -----
    All historical callers pass positional ``pid`` only and ignore the
    return value, so the new string return is fully backwards compatible.
    """

    # ------------------------------------------------------------------
    # Optional pid-ownership guard (R30.1)
    # ------------------------------------------------------------------
    if expected_name:
        try:
            import psutil  # type: ignore

            try:
                actual_name = psutil.Process(pid).name()
            except psutil.NoSuchProcess:
                # Target is already gone — nothing to kill, nothing to
                # verify. Callers treat this as success.
                logger.debug(
                    f"[base_engine] kill_process_tree pid={pid} "
                    f"expected_name={expected_name!r}: process already exited"
                )
                return "no_such_process"
            except psutil.AccessDenied as exc:
                # We couldn't verify ownership — prefer to proceed with the
                # kill because the caller explicitly requested it, but log
                # loudly so pid-reuse bugs are still traceable.
                logger.warning(
                    f"[base_engine] kill_process_tree pid={pid} "
                    f"expected_name={expected_name!r}: psutil access denied "
                    f"({exc}); proceeding without name-match guard"
                )
            else:
                if not _engine_name_matches(actual_name, expected_name):
                    logger.warning(
                        f"[base_engine] kill_process_tree skipped: pid={pid} "
                        f"actual_name={actual_name!r} does not match "
                        f"expected_name={expected_name!r} (likely pid reuse)"
                    )
                    return "pid_mismatch"
        except ImportError:
            # psutil unavailable — cannot verify. Fall through to the kill
            # path to preserve the legacy best-effort behavior on systems
            # where psutil isn't installed.
            logger.debug(
                f"[base_engine] kill_process_tree pid={pid}: psutil missing, "
                f"skipping expected_name={expected_name!r} guard"
            )

    # ------------------------------------------------------------------
    # Platform-specific termination
    # ------------------------------------------------------------------
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.debug(f"[base_engine] taskkill 终止进程树: {pid}")
            return "ok"
        except Exception as e:  # pragma: no cover - best effort
            logger.warning(f"[base_engine] taskkill 失败 pid={pid}: {e}")

    try:
        import psutil  # type: ignore

        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            logger.debug(
                f"[base_engine] psutil: pid={pid} already gone before kill"
            )
            return "no_such_process"
        try:
            children = proc.children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                continue
            except Exception:
                continue
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            return "no_such_process"
        except OSError as exc:
            # Process already exited between children enumeration and kill.
            logger.debug(
                "[base_engine] psutil proc.kill() skipped pid=%s (%s)",
                pid,
                type(exc).__name__,
            )
        logger.debug(
            f"[base_engine] psutil 终止进程树 pid={pid} children={len(children)}"
        )
        return "ok"
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[base_engine] psutil 终止失败 pid={pid}: {e}")
        return "ok"


def _classify_stop(task: DownloadTask, returncode: Optional[int]) -> EngineResult:
    """Map ``task.stop_reason`` + ``returncode`` to an :class:`EngineResult`.

    Kept as a module function so unit tests can exercise it independently of
    any live subprocess.
    """

    raw = getattr(task, "stop_reason", "") or ""
    # ``StopReason`` is a ``str`` enum, so string equality with its ``.value``
    # works transparently regardless of whether callers assign the enum or a
    # plain literal ("paused", etc.).
    reason = raw.value if isinstance(raw, StopReason) else str(raw)

    if reason == StopReason.PAUSED.value:
        return EngineResult(status="paused", returncode=returncode, resumable=True)
    if reason in {
        StopReason.CANCELLED.value,
        StopReason.REMOVED.value,
        StopReason.SHUTDOWN.value,
    }:
        return EngineResult(status="stopped", returncode=returncode)
    if reason == StopReason.ENGINE_SWITCH.value:
        return EngineResult(status="switched", returncode=returncode)

    # Natural completion: returncode 0 → ok, anything else → failed.
    status: EngineStatus = "ok" if returncode == 0 else "failed"
    return EngineResult(status=status, returncode=returncode)


# ---------------------------------------------------------------------------
# Sensitive (debug) logger — lazy, isolated from CapacityManagedFileHandler
# ---------------------------------------------------------------------------

# Name is module-scoped so the standard ``logging`` cache keeps it a singleton
# across engine instances. Keeping this logger detached from the engine /
# root logger tree guarantees that CapacityManagedFileHandler never observes
# the raw (unredacted) argv lines, per design 1.6 and Requirement 3.5.
_SENSITIVE_LOGGER_NAME = "m3u8d.sensitive"

# Rotating sizes per design 1.6: independent 5 MB × 3 rotation that is not
# subject to the main log capacity manager.
_SENSITIVE_LOG_MAX_BYTES = 5 * 1024 * 1024
_SENSITIVE_LOG_BACKUP_COUNT = 3
_SENSITIVE_LOG_FILENAME = "debug.sensitive.log"

# Process-wide guard so the module never attaches more than one
# RotatingFileHandler even if multiple engine subclasses race on first call.
_sensitive_logger_lock = threading.Lock()
_sensitive_logger_ready = False


def _get_sensitive_logger() -> Optional[logging.Logger]:
    """Return a lazily-initialized logger that writes to ``debug.sensitive.log``.

    The logger is created on first use so importing ``base_engine`` has no
    filesystem side effects. It does NOT propagate to the root logger (so the
    main ``CapacityManagedFileHandler`` never sees the raw argv) and it
    installs exactly one :class:`logging.handlers.RotatingFileHandler`.

    Returns ``None`` when the logs directory cannot be prepared — the caller
    is expected to skip the sensitive write silently rather than crash an
    engine command.
    """

    global _sensitive_logger_ready

    sensitive = logging.getLogger(_SENSITIVE_LOGGER_NAME)

    # Fast path: already initialized on a previous call.
    if _sensitive_logger_ready:
        return sensitive

    with _sensitive_logger_lock:
        if _sensitive_logger_ready:
            return sensitive

        # Lazy import so app_paths isn't pulled in for code paths that don't
        # touch the sensitive log (keeps ``base_engine`` import lightweight).
        try:
            from core.app_paths import get_logs_dir

            log_dir = get_logs_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / _SENSITIVE_LOG_FILENAME
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[base_engine] 敏感日志目录不可用,跳过: {e}")
            # Mark ready so we don't retry on every log_command call; the
            # redacted INFO line still lands in the main logger.
            _sensitive_logger_ready = True
            return None

        try:
            handler = logging.handlers.RotatingFileHandler(
                filename=str(log_path),
                maxBytes=_SENSITIVE_LOG_MAX_BYTES,
                backupCount=_SENSITIVE_LOG_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[base_engine] 敏感日志 handler 构建失败: {e}")
            _sensitive_logger_ready = True
            return None

        sensitive.setLevel(logging.DEBUG)
        # Detach from the root logger tree so CapacityManagedFileHandler never
        # receives the raw argv bytes.
        sensitive.propagate = False
        # Guard against duplicate handlers if something external seeded one.
        for existing in list(sensitive.handlers):
            sensitive.removeHandler(existing)
        sensitive.addHandler(handler)

        _sensitive_logger_ready = True
        return sensitive


# ---------------------------------------------------------------------------
# Pump worker (module-scope so it can be tested without a BaseEngine subclass)
# ---------------------------------------------------------------------------

# Sentinel placed on the queue when a pump thread finishes. Separate from a
# ``None`` line so the consumer can easily distinguish "drained" from "EOF".
_PUMP_DONE: Tuple[str, Any] = ("__pump_done__", None)


def _pump(
    stream, tag: str, q: "queue.Queue[Tuple[str, Any]]"
) -> None:
    """Read ``stream`` line-by-line and push ``(tag, bytes)`` onto ``q``.

    The pump reads raw bytes so encoding decisions stay in the main loop
    where they are tagged for telemetry by :func:`_decode_line` (task 20.1).
    Each enqueued line preserves its trailing ``\\r\\n`` /  ``\\n`` so the
    consumer can honor the newline-boundary guarantee in Requirement 20.3.
    Terminates when the stream reaches EOF, closes, or raises (e.g. handle
    was force-closed during process kill). Always enqueues the
    ``_PUMP_DONE`` sentinel on exit.
    """

    try:
        if stream is None:
            return
        # ``iter(readline, b"")`` yields one line at a time and stops at EOF.
        # Engines are spawned with ``stdout=PIPE, stderr=PIPE`` and no
        # ``text=True``, so ``readline()`` returns ``bytes``.
        for raw in iter(stream.readline, b""):
            if not raw:
                break
            try:
                q.put((tag, raw), timeout=_PUT_TIMEOUT)
            except queue.Full:
                # Consumer is stuck — dropping is safer than deadlocking the
                # child process (PIPE back-pressure would eventually hang it).
                logger.warning(
                    f"[base_engine] read_loop 队列已满,丢弃一行 {tag}"
                )
    except ValueError:
        # readline() on a closed pipe raises ValueError on some platforms
        # (e.g. after kill_process_tree). Treat as EOF.
        pass
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[base_engine] _pump({tag}) 异常: {e}")
    finally:
        try:
            q.put_nowait(_PUMP_DONE)
        except queue.Full:  # pragma: no cover - consumer already torn down
            pass


# ---------------------------------------------------------------------------
# BaseEngine
# ---------------------------------------------------------------------------


class BaseEngine(ABC):
    """下载引擎抽象基类。"""

    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        # Per-call state (progress callback, output accumulator, …) consumed
        # by ``_parse_line``. Using ``threading.local`` keeps each worker
        # thread's view isolated while still allowing direct
        # ``self.read_loop(proc, task, self._parse_line)`` wiring required by
        # task 9.2.
        self._tls = threading.local()

    @abstractmethod
    def download(self, task: DownloadTask, progress_callback: Callable) -> bool:
        """
        执行下载任务

        Args:
            task: 下载任务对象
            progress_callback: 进度回调函数,接收 dict 参数
                               ``{"progress": float, "speed": str, "downloaded": str}``

        Returns:
            bool: 下载是否成功
        """

    @abstractmethod
    def parse_progress(self, line: str) -> dict:
        """
        解析输出行,提取进度信息

        Args:
            line: 进程输出的一行文本

        Returns:
            dict: ``{"progress": float, "speed": str, "downloaded": str}``
        """

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """
        判断该引擎是否能处理此 URL

        Args:
            url: 资源 URL

        Returns:
            bool: 是否支持
        """

    @abstractmethod
    def get_name(self) -> str:
        """获取引擎名称"""

    # ------------------------------------------------------------------
    # Shared command-logging helper (task 4.1)
    # ------------------------------------------------------------------

    def log_command(
        self,
        argv: Sequence[str] | Iterable[str],
        *,
        action: str = "run",
    ) -> None:
        """Emit a scrubbed engine command line to the main logger.

        Requirement 3.4–3.6 mandate that every engine goes through this helper
        instead of hand-rolled ``logger.info("命令: " + " ".join(cmd))`` sites.
        The helper:

        1. Redacts sensitive headers / query params via
           :func:`utils.redact.redact_argv`.
        2. Joins the result with :func:`shlex.quote` so the log line remains
           cut-and-pastable into a shell for reproduction.
        3. When ``SECURITY_DEBUG=1`` is set in the environment, additionally
           appends the *raw* (unredacted) joined command to a dedicated
           ``debug.sensitive.log`` held by an independent
           :class:`logging.handlers.RotatingFileHandler` — it never shares
           the main :class:`CapacityManagedFileHandler` and does not
           propagate to the root logger.

        Parameters
        ----------
        argv:
            The subprocess argv list/iterable about to be spawned (or just
            was).
        action:
            Short verb describing the operation, logged as a structured tag
            (``action=run`` / ``action=probe`` / etc.). Defaults to ``"run"``.
        """

        # Materialize once so both the redacted log line and the optional
        # sensitive write see the same sequence, even if ``argv`` is a
        # generator.
        items: list[str] = [
            a if isinstance(a, str) else str(a) for a in (argv or [])
        ]

        try:
            safe = redact_argv(items)
        except Exception as e:  # pragma: no cover - redact_argv is total
            logger.debug(f"[base_engine] redact_argv 异常,记录降级占位: {e}")
            safe = ["<redact_failed>"]

        try:
            joined_safe = " ".join(shlex.quote(s) for s in safe)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[base_engine] shlex.quote 异常: {e}")
            joined_safe = " ".join(safe)

        try:
            name = self.get_name()
        except Exception:
            name = type(self).__name__

        logger.info(
            f"[{name}] engine={name} action={action} cmd={joined_safe}"
        )

        # Sensitive raw-command stream (opt-in only). The env var must be
        # explicitly set to "1" — any other value (including unset / "0" /
        # "true") is treated as disabled to avoid accidental activation.
        if os.environ.get("SECURITY_DEBUG") != "1":
            return

        sensitive = _get_sensitive_logger()
        if sensitive is None:
            return
        try:
            joined_raw = " ".join(shlex.quote(s) for s in items)
        except Exception:
            joined_raw = " ".join(items)
        try:
            sensitive.debug(
                f"engine={name} action={action} cmd={joined_raw}"
            )
        except Exception as e:  # pragma: no cover - handler I/O failure
            logger.debug(f"[base_engine] 敏感日志写入失败: {e}")

    # ------------------------------------------------------------------
    # Shared subprocess spawn helper (task 29.1, Requirement 37.1/37.3)
    # ------------------------------------------------------------------

    def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: Any = None,
        env: Optional[Any] = None,
        cwd: Optional[str] = None,
        sensitive: bool = True,
    ) -> subprocess.Popen:
        """Unified :class:`subprocess.Popen` wrapper for engine invocations.

        Centralizes the boilerplate every concrete engine otherwise duplicates
        (Requirement 37.1):

        * Windows: attaches ``CREATE_NO_WINDOW`` so the child never flashes a
          console window. ``DETACHED_PROCESS`` is intentionally *not* applied
          by default — callers that genuinely need a detached child (rare)
          should build their own flag set rather than pay that cost on every
          engine run. Escalate via a dedicated extension if the need arises.
        * POSIX: ``creationflags=0`` (the parameter is ignored but we still
          pass ``0`` for symmetry).
        * ``stdout`` / ``stderr`` are always pipes so :meth:`read_loop` can
          drain them, and they are kept in byte mode (no ``text=True`` /
          ``encoding=``) so :func:`_decode_line` controls the decode ladder.
        * ``close_fds`` follows the platform default — ``True`` on POSIX,
          ``False`` on Windows. Historically passing ``close_fds=True`` on
          Windows together with redirected handles raised ``ValueError`` on
          older Python releases; the platform-gated value keeps behavior
          consistent across the supported matrix.
        * When ``sensitive=True`` (default), :meth:`log_command` runs first
          so Requirement 37.3 — every engine command lands in the scrubbed
          log before the process is born — holds unconditionally. Callers
          that already emitted their own log line (e.g. a custom probe
          helper) can opt out by passing ``sensitive=False``.

        Parameters
        ----------
        argv:
            Argv sequence for the child process.
        stdin:
            Passed through to :class:`subprocess.Popen`. Typically ``None``
            or :data:`subprocess.DEVNULL`; set explicitly when the engine
            needs to feed stdin (e.g. an FFmpeg concat list).
        env:
            Optional environment mapping; ``None`` inherits the parent env.
        cwd:
            Optional working directory for the child.
        sensitive:
            When ``True`` (default) the argv is logged via
            :meth:`log_command` immediately before spawn. When ``False`` the
            caller is responsible for whatever logging it considers
            appropriate.

        Returns
        -------
        subprocess.Popen
            The freshly-spawned process. The caller owns the lifetime and
            should hand the handle to :meth:`read_loop` to drain pipes and
            honor ``task.stop_requested``.
        """

        # Materialize so ``log_command`` and ``Popen`` observe the exact same
        # sequence even if ``argv`` is a generator.
        items: list[str] = [
            a if isinstance(a, str) else str(a) for a in (argv or [])
        ]

        if sensitive:
            try:
                self.log_command(items)
            except Exception as e:  # pragma: no cover - log_command is defensive
                logger.debug(f"[base_engine] log_command 异常,继续 spawn: {e}")

        if os.name == "nt":
            # ``CREATE_NO_WINDOW`` is always defined on Windows Python builds
            # but we go through getattr to stay robust against stripped /
            # embedded interpreters that occasionally omit the constant.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        else:
            creationflags = 0

        return subprocess.Popen(
            items,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            close_fds=(os.name != "nt"),
            creationflags=creationflags,
        )

    # ------------------------------------------------------------------
    # Shared non-blocking read loop (task 9.1)
    # ------------------------------------------------------------------

    def read_loop(
        self,
        proc: subprocess.Popen,
        task: DownloadTask,
        on_line: Callable[[str, str], None],
    ) -> EngineResult:
        """Consume ``proc`` stdout/stderr while honoring ``task.stop_requested``.

        The process *must* be spawned with ``stdout=PIPE`` and ``stderr=PIPE``
        (i.e. byte mode — do not pass ``text=True`` / ``encoding=``) so the
        pump threads can read raw lines and the main loop controls decoding.

        Parameters
        ----------
        proc:
            Running subprocess, produced by ``subprocess.Popen``.
        task:
            Owning :class:`DownloadTask`. The loop polls
            ``task.stop_requested`` every ``~100ms`` and terminates ``proc``
            within ``500ms`` of a request; if the child is still alive at the
            ``1.5s`` mark it escalates to :func:`kill_process_tree`.
        on_line:
            Callback invoked for each output line, receiving ``(stream_tag,
            text)`` where ``stream_tag`` is ``"stdout"`` or ``"stderr"``.
            Decoding follows the ``utf-8 → mbcs → latin-1`` fallback. Trailing
            ``\\r\\n`` / ``\\n`` is stripped; a lone ``\\r`` (common for
            progress lines) is preserved. The callback is invoked on the main
            loop thread; exceptions are logged at DEBUG and do not break the
            loop.

        Returns
        -------
        EngineResult
            See :func:`_classify_stop` for the stop-reason → status mapping.
            ``returncode`` mirrors :attr:`subprocess.Popen.returncode` at the
            time the loop exited.
        """

        q: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)

        t_out = threading.Thread(
            target=_pump,
            args=(proc.stdout, "stdout", q),
            daemon=True,
            name="base_engine.read_loop.stdout",
        )
        t_err = threading.Thread(
            target=_pump,
            args=(proc.stderr, "stderr", q),
            daemon=True,
            name="base_engine.read_loop.stderr",
        )
        t_out.start()
        t_err.start()

        terminated = False
        killed = False
        terminate_deadline: Optional[float] = None
        kill_deadline: Optional[float] = None

        def _emit(tag: str, raw: bytes) -> None:
            try:
                text = _decode_line(raw)
            except Exception:  # pragma: no cover - _decode_line falls back
                text = raw.decode("latin-1", errors="replace")

            # Strip the terminating newline but keep carriage returns for
            # progress-bar style lines (yt-dlp emits ``\r`` between updates).
            if text.endswith("\r\n"):
                text = text[:-2]
            elif text.endswith("\n"):
                text = text[:-1]

            try:
                on_line(tag, text)
            except Exception as e:
                logger.debug(
                    f"[base_engine] on_line 回调异常 tag={tag}: {e}"
                )

        try:
            while True:
                # 1) Honor stop request before blocking on the queue. Capture
                #    a local snapshot so a concurrent flip between the two
                #    checks below can't confuse the deadline bookkeeping.
                if (
                    not terminated
                    and getattr(task, "stop_requested", False)
                ):
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                    except Exception as e:
                        logger.debug(
                            f"[base_engine] proc.terminate() 失败: {e}"
                        )
                    terminated = True
                    now = time.monotonic()
                    terminate_deadline = now + _TERMINATE_GRACE
                    kill_deadline = now + _KILL_DEADLINE

                # 2) Pull at most one item per tick (0.1s timeout).
                try:
                    item = q.get(timeout=_GET_TIMEOUT)
                except queue.Empty:
                    item = None

                if item is not None and item is not _PUMP_DONE:
                    tag, raw = item
                    _emit(tag, raw)

                # 3) Escalate to kill_process_tree if the child ignored our
                #    terminate() for more than the grace + kill window.
                if (
                    kill_deadline is not None
                    and not killed
                    and proc.poll() is None
                    and time.monotonic() >= kill_deadline
                ):
                    logger.warning(
                        f"[base_engine] 子进程未在 1.5s 内退出,升级到 "
                        f"kill_process_tree pid={proc.pid}"
                    )
                    try:
                        kill_process_tree(proc.pid)
                    except Exception as e:  # pragma: no cover
                        logger.debug(
                            f"[base_engine] kill_process_tree 失败: {e}"
                        )
                    killed = True

                # 4) Drain and exit once the child process is gone.
                if proc.poll() is not None:
                    # Give the pumps a brief chance to flush any in-flight
                    # lines now that the pipes are closing.
                    t_out.join(timeout=_PUMP_JOIN_TIMEOUT)
                    t_err.join(timeout=_PUMP_JOIN_TIMEOUT)
                    while True:
                        try:
                            drained = q.get_nowait()
                        except queue.Empty:
                            break
                        if drained is _PUMP_DONE:
                            continue
                        tag, raw = drained
                        _emit(tag, raw)
                    break
        finally:
            # Best-effort cleanup so the pumps don't outlive the loop with
            # open pipe handles. ``daemon=True`` means they won't block
            # interpreter shutdown, but closing the streams unblocks any
            # readline() still in flight.
            for stream in (proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    # Stream may already be closed by the pump thread; nothing
                    # to log here because the engine logger may itself be tearing
                    # down if we are in interpreter shutdown.
                    pass

        return _classify_stop(task, proc.returncode)
