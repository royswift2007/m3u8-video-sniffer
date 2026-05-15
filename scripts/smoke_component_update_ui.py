"""
Smoke checks for component manager UI batch update and startup badge phase.

This script avoids real network access and real installation. It uses fake worker
bridges and direct signal emissions to exercise UI state transitions. No real
component update/download/install method is called by this smoke test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from core.component_update_models import (  # noqa: E402
    ComponentBatchUpdateResult,
    ComponentUpdateProgressEvent,
    ComponentUpdateResult,
)
from core.component_update_service import ComponentUpdateService  # noqa: E402
from ui.component_manager_dialog import ComponentManagerDialog, make_fake_status, summarize_component_entry_rows, summarize_rows  # noqa: E402
from ui.component_update_worker import ComponentUpdateWorker, status_to_ui_state  # noqa: E402
from ui.main_window import summarize_component_entry_statuses  # noqa: E402


class FakeWorker(ComponentUpdateWorker):
    component_update_progress = pyqtSignal(object)
    component_update_completed = pyqtSignal(object)
    components_update_completed = pyqtSignal(object)
    operation_started = pyqtSignal(str)
    operation_finished = pyqtSignal(str)

    def __init__(self):
        super().__init__(service_factory=None)
        self.list_called = False
        self.check_called = False
        self.update_calls: list[tuple[str, bool]] = []
        self.batch_update_calls: list[tuple[tuple[str, ...] | None, bool]] = []
        self.running_operation: str | None = None

    def is_running(self, operation=None):
        if operation is None:
            return self.running_operation is not None
        return self.running_operation == operation

    def list_components(self, component_ids=None):
        self.list_called = True
        return False

    def check_updates(self, component_ids=None, force=True):
        self.check_called = True
        return False

    def update_component(self, component_id, force=False):
        self.update_calls.append((component_id, force))
        self.running_operation = "update_component"
        self.operation_started.emit("update_component")
        return True

    def update_components(self, component_ids=None, force=False):
        self.batch_update_calls.append((tuple(component_ids) if component_ids is not None else None, force))
        self.running_operation = "update_components"
        self.operation_started.emit("update_components")
        return True

    def emit_progress(self, component_id, event, percent=None, detail="fake progress"):
        self.component_update_progress.emit(
            ComponentUpdateProgressEvent(
                event=event,
                component_id=component_id,
                label=component_id,
                detail=detail,
                percent=percent,
            )
        )

    def make_success(self, component_id, new_version="7.0", skipped=False):
        return ComponentUpdateResult(
            component_id=component_id,
            success=True,
            status="latest" if skipped else "updated",
            code="skipped" if skipped else "ok",
            label=component_id,
            old_version="6.1",
            new_version=new_version,
            remote_version=new_version,
            skipped=skipped,
            message="fake skipped" if skipped else "fake success",
        )

    def make_failure(self, component_id, error="fake failure"):
        return ComponentUpdateResult(
            component_id=component_id,
            success=False,
            status="failed",
            code="fake_failed",
            label=component_id,
            error=error,
            message=error,
        )

    def emit_success(self, component_id, new_version="7.0"):
        self.component_update_completed.emit(self.make_success(component_id, new_version))
        self.running_operation = None
        self.operation_finished.emit("update_component")

    def emit_failure(self, component_id, error="fake failure"):
        self.component_update_completed.emit(self.make_failure(component_id, error))
        self.running_operation = None
        self.operation_finished.emit("update_component")

    def emit_batch_finished(self, results):
        self.components_update_completed.emit(ComponentBatchUpdateResult(results=results))
        self.running_operation = None
        self.operation_finished.emit("update_components")

    def shutdown(self):
        self.running_operation = None


class FakeMainWindow:
    def __init__(self):
        self.component_manager_btn = QPushButton("")
        self._component_update_badge_counts = {"updates": 0, "missing": 0, "failed": 0, "total": 0}

    def apply_component_entry_statuses(self, statuses):
        counts = summarize_component_entry_statuses(statuses)
        return self.apply_component_entry_summary(counts)

    def apply_component_entry_summary(self, counts):
        normalized_counts = {
            "updates": int(counts.get("updates", 0)),
            "missing": int(counts.get("missing", 0)),
            "failed": int(counts.get("failed", 0)),
            "total": int(counts.get("total", 0)),
        }
        self._component_update_badge_counts = normalized_counts
        self._update_component_manager_entry_text()
        return normalized_counts

    def _update_component_manager_entry_text(self):
        from utils.i18n import TR

        counts = self._component_update_badge_counts
        updates = counts.get("updates", 0)
        missing = counts.get("missing", 0)
        failed = counts.get("failed", 0)
        if updates or missing:
            self.component_manager_btn.setText(f"{TR('component_manager_entry')} {TR('component_entry_badge', updates=updates, missing=missing)}")
        else:
            self.component_manager_btn.setText(TR("component_manager_entry"))
        self.component_manager_btn.setToolTip(TR("component_entry_tooltip", updates=updates, missing=missing, failed=failed))


def button_text(dialog: ComponentManagerDialog, row_index: int) -> str:
    button = dialog.table.cellWidget(row_index, 7)
    assert isinstance(button, QPushButton)
    return button.text()


def button_enabled(dialog: ComponentManagerDialog, row_index: int) -> bool:
    button = dialog.table.cellWidget(row_index, 7)
    assert isinstance(button, QPushButton)
    return button.isEnabled()


def row_by_id(dialog: ComponentManagerDialog, component_id: str):
    row = dialog._find_row(component_id)
    assert row is not None, component_id
    return row


def wait_for_dialog_rows(app, dialog: ComponentManagerDialog, minimum_rows: int, timeout_ms: int) -> bool:
    deadline = QTimer()
    deadline.setSingleShot(True)
    loop = QEventLoop()

    def poll() -> None:
        if dialog.table.rowCount() >= minimum_rows or not dialog.worker.is_running("list_components"):
            loop.quit()

    poll_timer = QTimer()
    poll_timer.timeout.connect(poll)
    deadline.timeout.connect(loop.quit)
    poll_timer.start(25)
    deadline.start(timeout_ms)
    poll()
    if dialog.table.rowCount() < minimum_rows and dialog.worker.is_running("list_components"):
        loop.exec()
    poll_timer.stop()
    app.processEvents()
    return dialog.table.rowCount() >= minimum_rows


def assert_real_service_dialog_populates_quickly(app) -> None:
    worker = ComponentUpdateWorker(service_factory=ComponentUpdateService)
    dialog = ComponentManagerDialog(worker=worker)
    try:
        assert wait_for_dialog_rows(app, dialog, 1, 1500), dialog.log_text.toPlainText()
        assert dialog.table.rowCount() >= 1, dialog.table.rowCount()
        assert dialog.summary_label.text() and "总数 0" not in dialog.summary_label.text(), dialog.summary_label.text()
        logs = dialog.log_text.toPlainText()
        assert "manifest" in logs.lower() or "组件列表" in logs, logs
        wait_for_dialog_rows(app, dialog, 9999, 12000)
    finally:
        dialog.close()
        app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    original_question = QMessageBox.question
    original_information = QMessageBox.information
    info_messages: list[str] = []

    def fake_question(*args, **kwargs):
        return QMessageBox.StandardButton.Yes

    def fake_information(parent, title, text, buttons=QMessageBox.StandardButton.Ok, defaultButton=QMessageBox.StandardButton.NoButton):
        info_messages.append(str(text or ""))
        return QMessageBox.StandardButton.Ok

    try:
        QMessageBox.question = fake_question
        QMessageBox.information = fake_information

        worker = FakeWorker()
        dialog = ComponentManagerDialog(worker=worker)

        statuses = [
            make_fake_status("yt_dlp", "yt-dlp", "required", "latest", "2026.01.01", "2026.01.01"),
            make_fake_status("ffmpeg", "FFmpeg", "required", "update_available", "6.1", "7.0", True),
            make_fake_status("aria2", "Aria2", "recommended", "missing", None, "1.37.0"),
        ]
        dialog.apply_statuses(statuses)
        app.processEvents()

        long_url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        long_path = "C:\\Users\\qinghua\\Documents\\M3U8D\\sandbox_bin\\very\\deep\\nested\\toolchains\\ffmpeg\\bin\\ffmpeg.exe"
        long_local_version = "ffmpeg version 6.1.1-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers built with gcc 13.2.0"
        dialog.apply_statuses([
            make_fake_status(
                "ffmpeg",
                "FFmpeg",
                "required",
                "update_available",
                long_local_version,
                long_url,
                True,
                path=long_path,
            ),
        ])
        app.processEvents()
        long_local_item = dialog.table.item(0, 2)
        long_remote_item = dialog.table.item(0, 3)
        long_path_item = dialog.table.item(0, 5)
        assert long_local_item is not None
        assert long_remote_item is not None
        assert long_path_item is not None
        assert dialog.table.wordWrap() is False
        assert dialog.table.rowHeight(0) <= ComponentManagerDialog.TABLE_ROW_HEIGHT + 2, dialog.table.rowHeight(0)
        assert long_remote_item.toolTip() == long_url, long_remote_item.toolTip()
        assert long_path_item.toolTip() == long_path, long_path_item.toolTip()
        assert long_local_item.toolTip() == long_local_version, long_local_item.toolTip()
        assert long_remote_item.text() != long_url, long_remote_item.text()
        assert long_remote_item.text() == "ffmpeg-release-essentials.zip", long_remote_item.text()
        assert long_path_item.text() != long_path, long_path_item.text()
        assert long_path_item.text().endswith("bin\\ffmpeg.exe"), long_path_item.text()
        assert long_local_item.text() != long_local_version, long_local_item.text()
        assert "\n" not in long_path_item.text()
        action_btn = dialog.table.cellWidget(0, 7)
        assert isinstance(action_btn, QPushButton)
        assert action_btn.maximumHeight() == ComponentManagerDialog.ACTION_BUTTON_HEIGHT, action_btn.maximumHeight()
        assert dialog.table.columnWidth(3) <= ComponentManagerDialog.VERSION_COLUMN_WIDTH + 80, dialog.table.columnWidth(3)
        assert dialog.table.columnWidth(5) >= ComponentManagerDialog.PATH_COLUMN_MIN_WIDTH, dialog.table.columnWidth(5)

        dialog.apply_statuses(statuses)
        app.processEvents()

        assert dialog.width() >= ComponentManagerDialog.MINIMUM_SIZE[0], dialog.width()
        assert dialog.minimumWidth() >= ComponentManagerDialog.MINIMUM_SIZE[0], dialog.minimumWidth()
        assert dialog.table.columnCount() == 8, dialog.table.columnCount()
        assert dialog.table.rowCount() == 3, dialog.table.rowCount()
        assert dialog.table.columnWidth(6) >= ComponentManagerDialog.PROGRESS_COLUMN_WIDTH, dialog.table.columnWidth(6)
        assert dialog.table.columnWidth(7) >= ComponentManagerDialog.ACTION_COLUMN_WIDTH, dialog.table.columnWidth(7)
        assert dialog.refresh_btn is not None
        assert dialog.check_updates_btn is not None
        assert dialog.update_all_btn is not None
        assert dialog.close_btn is not None
        assert status_to_ui_state("missing") == ("error", "component_status_missing")
        assert status_to_ui_state("update_available") == ("update", "component_status_update_available")
        assert status_to_ui_state("latest") == ("ok", "component_status_latest")
        assert status_to_ui_state("local_check_failed") == ("ok", "component_status_installed")
        assert summarize_rows(dialog._rows) == {"ok": 1, "update": 1, "error": 1}

        ffmpeg_action_btn = dialog.table.cellWidget(1, 7)
        aria2_action_btn = dialog.table.cellWidget(2, 7)
        assert isinstance(ffmpeg_action_btn, QPushButton)
        assert isinstance(aria2_action_btn, QPushButton)
        assert ffmpeg_action_btn.minimumWidth() >= ComponentManagerDialog.ACTION_BUTTON_MIN_WIDTH, ffmpeg_action_btn.minimumWidth()
        assert aria2_action_btn.minimumWidth() >= ComponentManagerDialog.ACTION_BUTTON_MIN_WIDTH, aria2_action_btn.minimumWidth()
        assert button_text(dialog, 1) == "更新"
        assert button_enabled(dialog, 1)
        assert button_text(dialog, 2) == "安装"
        assert button_enabled(dialog, 2)
        assert not button_enabled(dialog, 0)
        assert dialog.update_all_btn.isEnabled(), "bulk update should be enabled when update/missing rows exist"
        assert len(dialog.collect_bulk_update_rows()) == 2
        assert worker.list_called, "dialog should request local refresh through fake worker only"
        assert not worker.check_called, "dialog construction must not start remote check"
        assert not worker.update_calls
        assert not worker.batch_update_calls

        assert dialog.confirm_and_update_all()
        app.processEvents()
        assert worker.batch_update_calls == [(("ffmpeg", "aria2"), False)]
        assert not dialog.refresh_btn.isEnabled()
        assert not dialog.check_updates_btn.isEnabled()
        assert not dialog.update_all_btn.isEnabled()
        assert not dialog.close_btn.isEnabled()
        assert not button_enabled(dialog, 0)
        assert not button_enabled(dialog, 1)
        assert not button_enabled(dialog, 2)

        worker.emit_progress("ffmpeg", "downloading", 42, "fake downloading")
        worker.emit_progress("aria2", "installing", 80, "fake installing")
        app.processEvents()
        ffmpeg_row = row_by_id(dialog, "ffmpeg")
        aria2_row = row_by_id(dialog, "aria2")
        assert ffmpeg_row.progress == "42%", ffmpeg_row.progress
        assert ffmpeg_row.status == "downloading", ffmpeg_row.status
        assert aria2_row.progress == "80%", aria2_row.progress
        assert aria2_row.status == "installing", aria2_row.status

        worker.emit_batch_finished([
            worker.make_success("ffmpeg", "7.0"),
            worker.make_failure("aria2", "fake install failed"),
            worker.make_success("yt_dlp", "2026.01.01", skipped=True),
        ])
        app.processEvents()
        ffmpeg_row = row_by_id(dialog, "ffmpeg")
        aria2_row = row_by_id(dialog, "aria2")
        yt_dlp_row = row_by_id(dialog, "yt_dlp")
        assert ffmpeg_row.status == "updated", ffmpeg_row.status
        assert ffmpeg_row.local_version == "7.0", ffmpeg_row.local_version
        assert aria2_row.status == "failed", aria2_row.status
        assert "fake install failed" in aria2_row.message
        assert yt_dlp_row.status == "latest", yt_dlp_row.status
        assert any("成功 1" in message and "失败 1" in message and "跳过 1" in message for message in info_messages), info_messages
        assert dialog.refresh_btn.isEnabled()
        assert dialog.check_updates_btn.isEnabled()
        assert dialog.close_btn.isEnabled()
        assert dialog.update_all_btn.isEnabled(), "failed row should remain retryable by bulk update"

        dialog.apply_statuses([
            make_fake_status("streamlink", "Streamlink", "recommended", "local_check_failed", None, None, False, "version command timed out")
        ])
        streamlink_row = row_by_id(dialog, "streamlink")
        assert streamlink_row.status == "local_check_failed", streamlink_row.status
        assert streamlink_row.status_class == "ok", streamlink_row.status_class
        assert streamlink_row.status_key == "component_status_installed", streamlink_row.status_key
        assert button_text(dialog, 0) == "不可操作", button_text(dialog, 0)
        assert not button_enabled(dialog, 0), "local version probe failure should not show retry"
        assert not dialog.update_all_btn.isEnabled(), "local version probe failure should not be bulk retryable"

        dialog.apply_statuses([make_fake_status("yt_dlp", "yt-dlp", "required", "latest", "2026.01.01", "2026.01.01")])
        assert not dialog.update_all_btn.isEnabled(), "bulk update should be disabled with no eligible rows"

        dialog.apply_statuses([
            make_fake_status("ffmpeg", "FFmpeg", "required", "update_available", "6.1", "7.1", True),
            make_fake_status("aria2", "Aria2", "recommended", "missing", None, "1.37.0"),
        ])
        assert dialog.start_component_update("aria2")
        worker.emit_progress("aria2", "installing", 80, "fake installing")
        worker.emit_failure("aria2", "fake install failed")
        app.processEvents()
        aria2_row = row_by_id(dialog, "aria2")
        assert aria2_row.status == "failed", aria2_row.status
        assert aria2_row.status_key == "component_status_failed", aria2_row.status_key
        assert "fake install failed" in aria2_row.message
        assert button_text(dialog, 1) == "重试", button_text(dialog, 1)
        assert button_enabled(dialog, 1)

        fake_main = FakeMainWindow()
        local_probe_failed_status = make_fake_status("streamlink", "Streamlink", "recommended", "local_check_failed", None, None, False, "version command timed out")
        local_probe_failed_counts = fake_main.apply_component_entry_statuses([local_probe_failed_status])
        assert local_probe_failed_counts == {"updates": 0, "missing": 0, "failed": 0, "total": 1}, local_probe_failed_counts
        counts = fake_main.apply_component_entry_statuses(statuses)
        assert counts == {"updates": 1, "missing": 1, "failed": 0, "total": 3}, counts
        assert "更新 1" in fake_main.component_manager_btn.text(), fake_main.component_manager_btn.text()
        assert "缺失 1" in fake_main.component_manager_btn.text(), fake_main.component_manager_btn.text()
        assert "可更新 1" in fake_main.component_manager_btn.toolTip(), fake_main.component_manager_btn.toolTip()
        assert "缺失 1" in fake_main.component_manager_btn.toolTip(), fake_main.component_manager_btn.toolTip()

        badge_worker = FakeWorker()
        badge_dialog = ComponentManagerDialog(worker=badge_worker)
        try:
            fake_main.apply_component_entry_statuses([
                make_fake_status("aria2", "Aria2", "recommended", "missing", None, "1.37.0"),
            ])
            assert "缺失 1" in fake_main.component_manager_btn.text(), fake_main.component_manager_btn.text()
            badge_dialog.component_status_summary_changed.connect(fake_main.apply_component_entry_summary)
            badge_dialog.apply_statuses([
                make_fake_status("aria2", "Aria2", "recommended", "missing", None, "1.37.0"),
            ])
            assert "缺失 1" in fake_main.component_manager_btn.text(), fake_main.component_manager_btn.text()
            assert summarize_component_entry_rows(badge_dialog._rows) == {"updates": 0, "missing": 1, "failed": 0, "total": 1}
            assert badge_dialog.start_component_update("aria2")
            badge_worker.emit_success("aria2", "1.37.0")
            app.processEvents()
            assert badge_dialog.current_entry_summary() == {"updates": 0, "missing": 0, "failed": 0, "total": 1}, badge_dialog.current_entry_summary()
            assert fake_main._component_update_badge_counts == {"updates": 0, "missing": 0, "failed": 0, "total": 1}, fake_main._component_update_badge_counts
            assert fake_main.component_manager_btn.text() == "组件管理", fake_main.component_manager_btn.text()
            assert "缺失 0" in fake_main.component_manager_btn.toolTip(), fake_main.component_manager_btn.toolTip()
            assert "可更新 0" in fake_main.component_manager_btn.toolTip(), fake_main.component_manager_btn.toolTip()
        finally:
            badge_dialog.close()
            app.processEvents()

        assert not worker.check_called, "fake construction and startup badge checks must not auto-download or auto-update"
        assert not any(call[0] is None for call in worker.batch_update_calls), "bulk update should use explicit confirmed component ids"

        dialog.close()
        app.processEvents()

        assert_real_service_dialog_populates_quickly(app)
    finally:
        QMessageBox.question = original_question
        QMessageBox.information = original_information

    print("component manager UI batch/startup smoke passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
