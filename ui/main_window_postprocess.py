"""FFmpeg post-processing UI integration for :class:`MainWindow`.

F-12 exposes the already-loaded :class:`engines.ffmpeg_processor.FFmpegProcessor`
through the completed-task context menu without blocking the Qt UI thread.
"""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Optional, TYPE_CHECKING

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from core.task_model import DownloadTask
from utils.i18n import TR
from utils.logger import logger

if TYPE_CHECKING:  # pragma: no cover - type-checker only
    from engines.ffmpeg_processor import FFmpegProcessor


class MainWindowPostprocessMixin:
    """FFmpeg post-processing slots used by ``ui.download_queue`` actions."""

    def _find_task_output_file(self, task: DownloadTask) -> Optional[Path]:
        """Resolve the primary downloaded artifact for a completed task.

        Precedence:
        1. ``task._aria2_output_filename`` (the actual filename Aria2 wrote).
        2. Media-suffixed glob matches (e.g. ``movie.mp4``).
        3. Exact stem-only file (legacy fallback, e.g. ``movie``).
        4. Any remaining glob matches.
        """
        save_dir = Path(str(getattr(task, "save_dir", "") or ""))
        filename = str(getattr(task, "filename", "") or "").strip()
        if not save_dir.exists() or not filename:
            return None

        # 1. Aria2 实际输出文件名（由 `Aria2Engine._build_command` 写入）。
        actual_name = str(getattr(task, "_aria2_output_filename", "") or "").strip()
        if actual_name:
            actual = save_dir / actual_name
            if actual.is_file():
                return actual

        # 2. Media-suffixed matches preferred over bare stem.
        preferred_suffixes = (".mp4", ".mkv", ".ts", ".m4v", ".mov", ".webm", ".flv")
        matches = [p for p in save_dir.glob(f"{filename}.*") if p.is_file()]
        media_matches = [
            p for p in matches if p.suffix.lower() in preferred_suffixes
        ]
        if media_matches:
            media_matches.sort(key=lambda p: p.name.lower())
            return media_matches[0]

        # 3. Exact stem-only file (legacy — could be suffixless Aria2 output).
        direct = save_dir / filename
        if direct.is_file():
            return direct

        # 4. Remaining non-media glob matches.
        if matches:
            matches.sort(
                key=lambda p: (
                    0 if p.suffix.lower() in preferred_suffixes else 1,
                    p.name.lower(),
                )
            )
            return matches[0]

        return None

    @staticmethod
    def _unique_postprocess_output(source: Path, suffix: str, extension: str) -> Path:
        """Build a non-overwriting output path beside ``source``."""
        base = source.with_name(f"{source.stem}{suffix}{extension}")
        if not base.exists():
            return base
        for idx in range(2, 1000):
            candidate = source.with_name(f"{source.stem}{suffix}_{idx}{extension}")
            if not candidate.exists():
                return candidate
        return source.with_name(f"{source.stem}{suffix}_{threading.get_ident()}{extension}")

    def _on_postprocess_requested(self, task: DownloadTask, action: str) -> None:
        """Start FFmpeg post-processing from the completed-task context menu."""
        ffmpeg = getattr(self, "ffmpeg", None)
        if ffmpeg is None:
            QMessageBox.warning(self, TR("msg_warning_title"), TR("msg_postprocess_ffmpeg_missing"))
            return

        source = self._find_task_output_file(task)
        if source is None:
            QMessageBox.warning(
                self,
                TR("dialog_file_not_found"),
                TR("msg_file_not_found", filename=getattr(task, "filename", "")),
            )
            return

        audio_file: Optional[Path] = None
        output: Path
        if action == "convert_mp4":
            output = self._unique_postprocess_output(source, "_remux", ".mp4")
        elif action == "compress_video":
            output = self._unique_postprocess_output(source, "_compressed", ".mp4")
        elif action == "extract_subtitles":
            output = self._unique_postprocess_output(source, "_subtitle", ".srt")
        elif action == "merge_audio":
            audio_path, _selected_filter = QFileDialog.getOpenFileName(
                self,
                TR("dialog_postprocess_select_audio"),
                str(source.parent),
                TR("filter_media_files"),
            )
            if not audio_path:
                try:
                    self.statusBar().showMessage(TR("status_postprocess_cancelled"), 3000)
                except (RuntimeError, AttributeError):
                    pass
                return
            audio_file = Path(audio_path)
            if not audio_file.is_file():
                QMessageBox.warning(self, TR("dialog_file_not_found"), str(audio_file))
                return
            output = self._unique_postprocess_output(source, "_merged", ".mp4")
        else:
            logger.warning(f"Unknown FFmpeg postprocess action ignored: {action}")
            return

        try:
            self.statusBar().showMessage(
                TR("status_postprocess_started", filename=source.name),
                5000,
            )
        except (RuntimeError, AttributeError):
            pass

        worker = threading.Thread(
            target=self._run_postprocess_worker,
            args=(action, source, output, audio_file),
            name=f"ffmpeg-postprocess-{action}",
            daemon=True,
        )
        worker.start()

    def _run_postprocess_worker(
        self,
        action: str,
        source: Path,
        output: Path,
        audio_file: Optional[Path],
    ) -> None:
        """Run the blocking FFmpeg call off the UI thread and report back."""
        ok = False
        error = ""
        try:
            ffmpeg = getattr(self, "ffmpeg", None)
            if ffmpeg is None:
                error = "FFmpeg is not available"
            elif action == "convert_mp4":
                ok = ffmpeg.convert_to_mp4(str(source), str(output))
            elif action == "compress_video":
                ok = ffmpeg.compress_video(str(source), str(output))
            elif action == "extract_subtitles":
                ok = ffmpeg.extract_subtitles(str(source), str(output))
            elif action == "merge_audio" and audio_file is not None:
                ok = ffmpeg.merge_video_audio(str(source), str(audio_file), str(output))
            else:
                error = f"Unsupported postprocess action: {action}"
        except Exception as exc:
            error = str(exc)
            logger.error(f"FFmpeg postprocess failed: action={action}, source={source.name}, error={error}")

        self._postprocess_finished.emit(
            {
                "ok": ok,
                "action": action,
                "source": str(source),
                "output": str(output),
                "error": error,
            }
        )

    @pyqtSlot(object)
    def _on_postprocess_finished(self, result: object) -> None:
        """Notify the user after an FFmpeg post-processing job finishes."""
        data = result if isinstance(result, dict) else {}
        ok = bool(data.get("ok"))
        output = str(data.get("output") or "")
        error = str(data.get("error") or "")
        if ok:
            message = TR("status_postprocess_finished", output=output)
            try:
                self.statusBar().showMessage(message, 8000)
            except (RuntimeError, AttributeError):
                pass
            QMessageBox.information(self, TR("dialog_postprocess_finished"), message)
            return

        message = TR("status_postprocess_failed", error=error or "FFmpeg returned non-zero exit")
        try:
            self.statusBar().showMessage(message, 8000)
        except (RuntimeError, AttributeError):
            pass
        QMessageBox.warning(self, TR("dialog_postprocess_failed"), message)
