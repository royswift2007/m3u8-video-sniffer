"""
FFmpeg processor for post-download video processing.

Stage 2 / task 10.1 refactor
----------------------------
The four public methods (``convert_to_mp4`` / ``merge_video_audio`` /
``extract_subtitles`` / ``compress_video``) previously blocked on
``subprocess.run`` with ``stdout=PIPE, stderr=PIPE``, which can deadlock once
ffmpeg's stderr exceeds the PIPE buffer on long conversions (Requirement 10.1)
and has no way to honor a hard wall-clock ceiling (Requirement 10.3).

This module now spawns ffmpeg through a tiny :class:`_FFmpegBackend` adapter
that reuses :meth:`BaseEngine.spawn` (unified ``CREATE_NO_WINDOW`` /
``close_fds`` handling per task 29.1) and :meth:`BaseEngine.read_loop`
(non-blocking PIPE drain + terminate → kill_process_tree escalation per task
9.1). A per-run :class:`_FFmpegRunTask` holder duck-types the narrow subset
of :class:`~core.task_model.DownloadTask` that ``read_loop`` actually touches
(``stop_requested`` / ``stop_reason``), so ``FFmpegProcessor`` stays
decoupled from the download-task layer it never participates in.

Hard timeout enforcement is layered on top as a small watchdog thread: once
the deadline passes it flips ``run_task.stop_requested = True``, which
``BaseEngine.read_loop`` observes within ~100ms and drives through its own
terminate (500ms grace) → kill_process_tree escalation. After ``read_loop``
returns, the outcome is normalized to ``"timeout_exceeded"`` so callers can
distinguish genuine user cancels (not possible today) from wall-clock
exhaustion.

Intermediate-artifact cleanup (task 10.2, Requirement 10.5)
-----------------------------------------------------------
On any non-``ok`` outcome (``timeout_exceeded`` from the watchdog,
``stopped`` from a future cancel hook, or ``failed`` from a non-zero rc) we
delete the output artifact that ffmpeg produced during this run, plus any
``.part`` / ``.tmp`` sibling it may have written. The output path is taken
as the last positional argv entry — ffmpeg's canonical output position —
which also implicitly preserves the "previous step artifact" for
:meth:`merge_video_audio`: its ``video_file`` / ``audio_file`` are earlier
``-i`` args, so they are never in the cleanup candidate set.

Cleanup is mtime-gated (``st_mtime >= start_time``) so a pre-existing file
that ffmpeg never touched (e.g. the spawn itself failed before writing) is
left alone. Delete failures raise :class:`OSError`, which we swallow at
DEBUG level — the error path should never be masked by a cleanup hiccup.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from engines.base_engine import BaseEngine, EngineResult, kill_process_tree
from core.task_model import DownloadTask  # noqa: F401 — documents read_loop's expected shape
from utils.logger import logger


# ---------------------------------------------------------------------------
# Tunables (Requirement 10.3)
# ---------------------------------------------------------------------------

# Default hard wall-clock ceiling for a single ffmpeg invocation: 2h. Large
# compressions of 4K / high-bitrate media occasionally run past an hour on
# modest hardware, so 2h keeps false-positive timeouts out of normal use while
# still bounding worst-case lockup to a tractable window.
_DEFAULT_HARD_TIMEOUT_S: float = 7200.0

# Polling cadence for the watchdog thread. 200ms balances timeout precision
# (worst-case overshoot = one tick) against CPU load for long-running runs.
_WATCHDOG_TICK_S: float = 0.2


# ---------------------------------------------------------------------------
# Task-shaped holder consumed by BaseEngine.read_loop
# ---------------------------------------------------------------------------


@dataclass
class _FFmpegRunTask:
    """Minimal duck type consumed by :meth:`BaseEngine.read_loop`.

    ``read_loop`` polls ``task.stop_requested`` every ~100ms and, once the
    process exits, asks :func:`~engines.base_engine._classify_stop` to read
    ``task.stop_reason`` to build the final :class:`EngineResult`. A real
    :class:`DownloadTask` is not available at the ffmpeg processing layer —
    :class:`FFmpegProcessor` operates on file paths long after the download
    task has resolved — so this lightweight per-run holder satisfies the
    contract without leaking into the task-management layer.

    The fields mirror the names on :class:`DownloadTask` so future task
    10.2 cancel-hook work can plumb through a real task by passing it in
    place of the holder (every attribute read by ``read_loop`` is shared).
    """

    stop_requested: bool = False
    # ``read_loop`` passes this to :func:`_classify_stop`; empty string maps
    # to the natural-completion branch (rc==0 → ``"ok"``, else ``"failed"``).
    stop_reason: str = ""
    status: str = "downloading"
    error_message: str = ""
    # RLock to mirror :class:`DownloadTask`. Not required by ``read_loop``
    # itself, but keeps watchdog / cancel-hook call sites (task 10.2)
    # compatible with ``with task.lock:`` patterns used elsewhere.
    lock: threading.RLock = field(default_factory=threading.RLock)


# ---------------------------------------------------------------------------
# BaseEngine adapter
# ---------------------------------------------------------------------------


class _FFmpegBackend(BaseEngine):
    """Composition-only :class:`BaseEngine` adapter for :class:`FFmpegProcessor`.

    ``FFmpegProcessor`` is intentionally not a :class:`BaseEngine` subclass
    because it has no download-task / progress-callback contract to uphold —
    it operates on already-downloaded files. This adapter exists purely so
    :class:`FFmpegProcessor` can call ``self._base.spawn(...)`` and
    ``self._base.read_loop(...)`` (task 10.1 AC wording) and inherit the
    Stage 2 infrastructure:

    * :meth:`BaseEngine.spawn` — ``CREATE_NO_WINDOW`` on Windows,
      ``close_fds`` on POSIX, byte-mode PIPEs, sensitive-argv logging via
      :meth:`BaseEngine.log_command`.
    * :meth:`BaseEngine.read_loop` — two daemon pump threads, 100ms
      stop-request polling, terminate → kill_process_tree escalation.

    The abstract hooks (:meth:`download` / :meth:`parse_progress` /
    :meth:`can_handle`) are deliberately no-op stubs: callers go through
    ``spawn`` + ``read_loop`` directly.
    """

    def download(self, task, progress_callback):  # pragma: no cover - stub
        raise NotImplementedError(
            "_FFmpegBackend is a spawn/read_loop adapter, not a download engine"
        )

    def parse_progress(self, line: str) -> dict:  # pragma: no cover - stub
        return {"progress": 0.0, "speed": "", "downloaded": ""}

    def can_handle(self, url: str) -> bool:  # pragma: no cover - stub
        return False

    def get_name(self) -> str:
        return "ffmpeg"


# ---------------------------------------------------------------------------
# FFmpegProcessor
# ---------------------------------------------------------------------------


class FFmpegProcessor:
    """FFmpeg 后处理器 - 转码、合并、压缩"""

    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        # Composition: the adapter gives us ``spawn`` + ``read_loop`` without
        # forcing ``FFmpegProcessor`` itself to implement the engine
        # contract. See :class:`_FFmpegBackend` for the rationale.
        self._base = _FFmpegBackend(binary_path)

    # ------------------------------------------------------------------
    # Shared spawn + read_loop + hard_timeout driver (task 10.1)
    # ------------------------------------------------------------------

    def _run(
        self,
        cmd: list[str],
        *,
        action: str,
        hard_timeout: float,
    ) -> tuple[EngineResult, str]:
        """Spawn ``cmd`` and drive it through ``read_loop`` with a hard timeout.

        Shared implementation for the four public methods. The argv is logged
        via :meth:`BaseEngine.log_command` (Requirement 3.4 — redacts any
        sensitive query value / header that happens to slip into an ffmpeg
        argv, keeping behavior consistent with the download engines) before
        the subprocess is born; ``spawn`` is then called with
        ``sensitive=False`` so the log line is not duplicated.

        The hard_timeout is enforced by a small watchdog thread that flips
        ``run_task.stop_requested = True`` once the deadline passes. That
        flag is polled every ~100ms inside :meth:`BaseEngine.read_loop`,
        which runs its own ``terminate()`` → 500ms grace →
        :func:`kill_process_tree` escalation. Double-tapping the process tree
        after ``read_loop`` returns (when ``timed_out`` fired) guards against
        the rare case where ffmpeg re-spawned grand-children during
        shutdown.

        Returns
        -------
        tuple[EngineResult, str]
            The (normalized) :class:`EngineResult` plus a newline-joined
            concatenation of stdout+stderr for diagnostics.
        """

        run_task = _FFmpegRunTask()
        output_lines: list[str] = []
        # Capture the instant before spawn so artifact cleanup can gate on
        # ``mtime >= start_time``; using a wall-clock stamp (``time.time()``)
        # matches what :func:`os.stat` returns for ``st_mtime``.
        start_time = time.time()

        def _on_line(tag: str, text: str) -> None:
            # Both stdout and stderr flow through the same accumulator: ffmpeg
            # writes progress + errors to stderr while ``-report`` / ``-v`` can
            # push informational frames to stdout. Tag is preserved in the
            # read_loop debug log; here we just want the text for the caller's
            # error-surface blob.
            output_lines.append(text)

        try:
            self._base.log_command(cmd, action=action)
        except Exception as e:  # pragma: no cover - log_command is defensive
            logger.debug(f"[FFmpeg] log_command 异常,继续 spawn: {e}")

        # ``sensitive=False`` avoids re-logging the argv; we already did it
        # with the engine-specific ``action`` tag above.
        proc = self._base.spawn(cmd, sensitive=False)

        timed_out = threading.Event()
        stop_watchdog = threading.Event()
        deadline = time.monotonic() + float(hard_timeout)

        def _watchdog() -> None:
            while not stop_watchdog.is_set():
                if time.monotonic() >= deadline:
                    timed_out.set()
                    with run_task.lock:
                        run_task.stop_requested = True
                    return
                # ``wait`` with a short timeout gives us both early exit
                # (``stop_watchdog.set()`` after read_loop returns) and
                # bounded timeout-detection latency.
                stop_watchdog.wait(_WATCHDOG_TICK_S)

        watchdog = threading.Thread(
            target=_watchdog,
            name=f"ffmpeg.watchdog.{action}",
            daemon=True,
        )
        watchdog.start()

        try:
            result = self._base.read_loop(proc, run_task, _on_line)
        finally:
            stop_watchdog.set()
            watchdog.join(timeout=0.5)

        if timed_out.is_set():
            # Belt-and-suspenders: even if ``read_loop`` already kicked off
            # the terminate → kill_process_tree escalation via the
            # ``stop_requested`` flag, double-tap in case the child
            # re-spawned grand-children mid-shutdown.
            if proc.poll() is None:
                try:
                    kill_process_tree(proc.pid)
                except Exception as e:  # pragma: no cover - best effort
                    logger.debug(
                        f"[FFmpeg] kill_process_tree 失败 pid={proc.pid}: {e}"
                    )
            result = EngineResult(
                status="timeout_exceeded",
                returncode=result.returncode,
                resumable=False,
                error=result.error,
            )

        # Task 10.2 / Requirement 10.5: on any non-ok outcome (timeout,
        # cancel, or failed rc), wipe the output artifact this run produced
        # plus any ``.part`` / ``.tmp`` siblings ffmpeg may have written.
        # Successful runs are left alone so the existing remove_source /
        # remove_sources logic in the public methods stays in charge.
        if result.status != "ok":
            self._cleanup_artifacts(cmd, start_time=start_time, action=action)

        return result, "\n".join(output_lines)

    # ------------------------------------------------------------------
    # Intermediate-artifact cleanup (task 10.2, Requirement 10.5)
    # ------------------------------------------------------------------

    def _cleanup_artifacts(
        self,
        cmd: list[str],
        *,
        start_time: float,
        action: str,
    ) -> None:
        """Remove the partial output file this run produced, if any.

        The output path is the last positional entry in ``cmd`` (ffmpeg's
        canonical output position after all ``-i`` / flag arguments). We
        also sweep common in-flight sibling suffixes (``<out>.part``,
        ``<out>.tmp``) in case ffmpeg or a muxer staged one alongside the
        final file.

        Deletion is gated on ``st_mtime >= start_time`` so pre-existing
        files that ffmpeg never touched (e.g. the spawn itself failed
        before any write) are preserved. This also guarantees the
        ``merge_video_audio`` source files are never deleted: they're
        earlier ``-i`` args (not the last argv entry), and even if one
        happened to share a name with the output, its mtime would predate
        ``start_time``.

        All failures are swallowed at DEBUG level — a cleanup hiccup must
        not mask the real error path.
        """

        if not cmd:
            return

        output_path = Path(cmd[-1])
        # Sweep the canonical output plus the two temp-suffix patterns
        # ffmpeg/muxers commonly use during in-flight writes.
        candidates: list[Path] = [
            output_path,
            output_path.with_name(output_path.name + ".part"),
            output_path.with_name(output_path.name + ".tmp"),
        ]

        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                # mtime gate: only touch files this run actually wrote. A
                # 1-second fudge absorbs filesystems with coarse mtime
                # granularity (e.g. FAT32 at 2s) plus the small gap
                # between ``time.time()`` capture and the first ffmpeg
                # write.
                mtime = candidate.stat().st_mtime
                if mtime + 1.0 < start_time:
                    continue
                candidate.unlink()
                logger.debug(
                    f"[FFmpeg:{action}] 已清理中间产物: {candidate.name}"
                )
            except OSError as e:
                logger.debug(
                    f"[FFmpeg:{action}] 清理中间产物失败 "
                    f"path={candidate} err={e}"
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def convert_to_mp4(
        self,
        input_file: str,
        output_file: str,
        remove_source: bool = False,
        *,
        hard_timeout: float = _DEFAULT_HARD_TIMEOUT_S,
    ) -> bool:
        """
        转换为 MP4（无损转封装）

        Args:
            input_file: 输入文件路径
            output_file: 输出文件路径
            remove_source: 是否删除源文件
            hard_timeout: 硬超时（秒），默认 7200s（2h）。超时返回 False
                并以 `terminate → kill_process_tree` 两阶段终止进程树。

        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                "-i", input_file,
                "-c", "copy",  # 不重新编码，直接复制流
                "-y",          # 覆盖输出文件
                output_file,
            ]

            logger.info(f"[FFmpeg] 转换为 MP4: {Path(input_file).name}")

            result, output = self._run(
                cmd, action="convert_to_mp4", hard_timeout=hard_timeout
            )

            if result.status == "ok":
                logger.info(f"[FFmpeg] 转换成功: {Path(output_file).name}")
                if remove_source and Path(input_file).exists():
                    Path(input_file).unlink()
                    logger.debug(f"已删除源文件: {input_file}")
                return True

            if result.status == "timeout_exceeded":
                logger.error(
                    f"[FFmpeg] 转换超时(>{int(hard_timeout)}s): "
                    f"{Path(input_file).name}"
                )
                return False

            logger.error(
                f"[FFmpeg] 转换失败(rc={result.returncode}): "
                f"{_tail(output)}"
            )
            return False

        except Exception as e:
            logger.error(f"[FFmpeg] 转换异常: {e}")
            return False

    def merge_video_audio(
        self,
        video_file: str,
        audio_file: str,
        output_file: str,
        remove_sources: bool = False,
        *,
        hard_timeout: float = _DEFAULT_HARD_TIMEOUT_S,
    ) -> bool:
        """
        合并独立的视频和音频流

        Args:
            video_file: 视频文件路径
            audio_file: 音频文件路径
            output_file: 输出文件路径
            remove_sources: 是否删除源文件
            hard_timeout: 硬超时（秒），默认 7200s（2h）。

        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                "-i", video_file,
                "-i", audio_file,
                "-c", "copy",
                "-y",
                output_file,
            ]

            logger.info(f"[FFmpeg] 合并音视频: {Path(output_file).name}")

            result, output = self._run(
                cmd, action="merge_video_audio", hard_timeout=hard_timeout
            )

            if result.status == "ok":
                logger.info(f"[FFmpeg] 合并成功: {Path(output_file).name}")
                if remove_sources:
                    if Path(video_file).exists():
                        Path(video_file).unlink()
                    if Path(audio_file).exists():
                        Path(audio_file).unlink()
                    logger.debug("已删除源文件")
                return True

            if result.status == "timeout_exceeded":
                logger.error(
                    f"[FFmpeg] 合并超时(>{int(hard_timeout)}s): "
                    f"{Path(output_file).name}"
                )
                return False

            logger.error(
                f"[FFmpeg] 合并失败(rc={result.returncode}): "
                f"{_tail(output)}"
            )
            return False

        except Exception as e:
            logger.error(f"[FFmpeg] 合并异常: {e}")
            return False

    def extract_subtitles(
        self,
        input_file: str,
        output_srt: str,
        stream_index: int = 0,
        *,
        hard_timeout: float = _DEFAULT_HARD_TIMEOUT_S,
    ) -> bool:
        """
        提取内嵌字幕

        Args:
            input_file: 输入文件路径
            output_srt: 输出字幕文件路径
            stream_index: 字幕流索引（默认第一条）
            hard_timeout: 硬超时（秒），默认 7200s（2h）。

        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                "-i", input_file,
                "-map", f"0:s:{stream_index}",  # 选择字幕流
                "-y",
                output_srt,
            ]

            logger.info(f"[FFmpeg] 提取字幕: {Path(output_srt).name}")

            result, output = self._run(
                cmd, action="extract_subtitles", hard_timeout=hard_timeout
            )

            if result.status == "ok":
                logger.info("[FFmpeg] 字幕提取成功")
                return True

            if result.status == "timeout_exceeded":
                logger.error(
                    f"[FFmpeg] 字幕提取超时(>{int(hard_timeout)}s): "
                    f"{Path(output_srt).name}"
                )
                return False

            # 字幕提取失败常见于"无字幕流"，保留原 WARN 级别避免刷红。
            logger.warning(
                f"[FFmpeg] 字幕提取失败（可能无字幕）(rc={result.returncode}): "
                f"{_tail(output)}"
            )
            return False

        except Exception as e:
            logger.error(f"[FFmpeg] 字幕提取异常: {e}")
            return False

    def compress_video(
        self,
        input_file: str,
        output_file: str,
        crf: int = 23,
        *,
        hard_timeout: float = _DEFAULT_HARD_TIMEOUT_S,
    ) -> bool:
        """
        压缩视频

        Args:
            input_file: 输入文件路径
            output_file: 输出文件路径
            crf: 压缩质量 (18-28, 越小质量越好)
            hard_timeout: 硬超时（秒），默认 7200s（2h）。

        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                "-i", input_file,
                "-c:v", "libx264",
                "-crf", str(crf),
                "-c:a", "aac",
                "-y",
                output_file,
            ]

            logger.info(f"[FFmpeg] 压缩视频: {Path(input_file).name}")

            result, output = self._run(
                cmd, action="compress_video", hard_timeout=hard_timeout
            )

            if result.status == "ok":
                logger.info(f"[FFmpeg] 压缩成功: {Path(output_file).name}")
                return True

            if result.status == "timeout_exceeded":
                logger.error(
                    f"[FFmpeg] 压缩超时(>{int(hard_timeout)}s): "
                    f"{Path(input_file).name}"
                )
                return False

            logger.error(
                f"[FFmpeg] 压缩失败(rc={result.returncode}): "
                f"{_tail(output)}"
            )
            return False

        except Exception as e:
            logger.error(f"[FFmpeg] 压缩异常: {e}")
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tail(output: str, max_lines: int = 20) -> str:
    """Return the last ``max_lines`` of ``output``, preserving newline separators.

    ffmpeg is famously verbose on the error path; trimming to a tail keeps
    the log readable without losing the actionable trailing diagnostics.
    """

    if not output:
        return ""
    lines = output.splitlines()
    if len(lines) <= max_lines:
        return output
    return "\n".join(lines[-max_lines:])
