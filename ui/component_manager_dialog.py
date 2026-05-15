"""
Component management dialog for component update checks and user-confirmed component updates.

The dialog lists update-manageable external components, supports local refresh and
read-only remote checks, and exposes only manual install/update/retry actions
protected by confirmation dialogs and worker-thread execution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.component_update_models import (
    ComponentBatchUpdateResult,
    ComponentUpdateProgressEvent,
    ComponentUpdateResult,
    ComponentUpdateStatus,
    ComponentVersionInfo,
    RemoteReleaseInfo,
)
from ui.component_update_worker import ComponentUpdateWorker, status_to_ui_state
from utils.i18n import TR, i18n


@dataclass(frozen=True)
class ComponentTableRow:
    """UI-friendly row data for a component status."""

    component_id: str
    label: str
    category: str
    local_version: str
    remote_version: str
    status: str
    status_key: str
    status_class: str
    path: str
    progress: str
    message: str
    update_available: bool = False


STATUS_CLASS_TO_OBJECT = {
    "ok": "component_status_ok",
    "update": "component_status_update",
    "pending": "component_status_pending",
    "error": "component_status_error",
    "unknown": "component_status_unknown",
}

RUNNING_STATUSES = {"started", "checking", "downloading", "staged", "installing", "running"}
FAILURE_STATUSES = {"failed", "remote_check_failed", "asset_missing"}


class ComponentManagerDialog(QDialog):
    """Component management dialog."""

    component_status_summary_changed = pyqtSignal(dict)
    component_statuses_changed = pyqtSignal(list)

    DEFAULT_SIZE = (1300, 740)
    MINIMUM_SIZE = (1250, 660)
    TABLE_ROW_HEIGHT = 34
    COMPONENT_COLUMN_WIDTH = 132
    CATEGORY_COLUMN_WIDTH = 92
    VERSION_COLUMN_WIDTH = 154
    STATUS_COLUMN_WIDTH = 104
    PATH_COLUMN_MIN_WIDTH = 360
    PROGRESS_COLUMN_WIDTH = 82
    ACTION_COLUMN_WIDTH = 132
    ACTION_BUTTON_MIN_WIDTH = 96
    ACTION_BUTTON_HEIGHT = 26

    TABLE_COLUMNS = [
        "component_col_component",
        "component_col_category",
        "component_col_local_version",
        "component_col_latest_version",
        "component_col_status",
        "component_col_path",
        "component_col_progress",
        "component_col_action",
    ]

    def __init__(self, parent: QWidget | None = None, worker: ComponentUpdateWorker | None = None):
        super().__init__(parent)
        self.setObjectName("component_manager_dialog")
        self.resize(*self.DEFAULT_SIZE)
        self.setMinimumSize(*self.MINIMUM_SIZE)
        self.worker = worker or ComponentUpdateWorker(parent=self)
        self._rows: list[ComponentTableRow] = []
        self._status_counts: dict[str, int] = {}
        self._active_update_component_id: str | None = None
        self._active_batch_component_ids: list[str] = []
        self._batch_running = False
        self._last_failed_component_id: str | None = None

        self._init_ui()
        self._connect_signals()
        self.retranslate_ui()
        self._append_log(TR("component_log_dialog_opened"))
        self.refresh_local_status()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        header_card = QFrame()
        header_card.setObjectName("panel_card")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(14, 12, 14, 12)
        header_layout.setSpacing(8)

        title_row = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setObjectName("page_title")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("component_summary_label")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.title_label, 1)
        title_row.addWidget(self.summary_label, 2)
        header_layout.addLayout(title_row)

        self.intro_label = QLabel("")
        self.intro_label.setObjectName("panel_intro")
        self.intro_label.setWordWrap(True)
        header_layout.addWidget(self.intro_label)
        main_layout.addWidget(header_card)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.refresh_btn = QPushButton("")
        self.refresh_btn.setObjectName("secondary_button")
        self.check_updates_btn = QPushButton("")
        self.check_updates_btn.setObjectName("component_primary_button")
        self.update_all_btn = QPushButton("")
        self.update_all_btn.setObjectName("component_batch_update_button")
        self.close_btn = QPushButton("")
        self.close_btn.setObjectName("secondary_button")
        action_row.addWidget(self.refresh_btn)
        action_row.addWidget(self.check_updates_btn)
        action_row.addWidget(self.update_all_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.close_btn)
        main_layout.addLayout(action_row)

        self.table = QTableWidget(0, len(self.TABLE_COLUMNS))
        self.table.setObjectName("component_manager_table")
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(self.TABLE_ROW_HEIGHT)
        self.table.verticalHeader().setMinimumSectionSize(self.TABLE_ROW_HEIGHT)
        self.table.horizontalHeader().setStretchLastSection(False)
        self._configure_table_columns()
        main_layout.addWidget(self.table, 1)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)

        detail_group = QGroupBox("")
        self.detail_group = detail_group
        detail_layout = QGridLayout(detail_group)
        detail_layout.setContentsMargins(12, 16, 12, 12)
        detail_layout.setHorizontalSpacing(8)
        detail_layout.setVerticalSpacing(6)
        self.detail_component_label = QLabel("")
        self.detail_component_value = QLabel("-")
        self.detail_status_label = QLabel("")
        self.detail_status_value = QLabel("-")
        self.detail_path_label = QLabel("")
        self.detail_path_value = QLabel("-")
        self.detail_message_label = QLabel("")
        self.detail_message_value = QLabel("-")
        self.detail_path_value.setWordWrap(True)
        self.detail_message_value.setWordWrap(True)
        detail_layout.addWidget(self.detail_component_label, 0, 0)
        detail_layout.addWidget(self.detail_component_value, 0, 1)
        detail_layout.addWidget(self.detail_status_label, 1, 0)
        detail_layout.addWidget(self.detail_status_value, 1, 1)
        detail_layout.addWidget(self.detail_path_label, 2, 0)
        detail_layout.addWidget(self.detail_path_value, 2, 1)
        detail_layout.addWidget(self.detail_message_label, 3, 0)
        detail_layout.addWidget(self.detail_message_value, 3, 1)
        detail_layout.setColumnStretch(1, 1)

        log_group = QGroupBox("")
        self.log_group = log_group
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(12, 16, 12, 12)
        self.log_text = QTextEdit()
        self.log_text.setObjectName("component_manager_log_text")
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(120)
        log_layout.addWidget(self.log_text)

        bottom_row.addWidget(detail_group, 1)
        bottom_row.addWidget(log_group, 1)
        main_layout.addLayout(bottom_row)

    def _connect_signals(self) -> None:
        self.refresh_btn.clicked.connect(self.refresh_local_status)
        self.check_updates_btn.clicked.connect(self.check_updates)
        self.update_all_btn.clicked.connect(self.confirm_and_update_all)
        self.close_btn.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._sync_detail_from_selection)
        self.worker.operation_started.connect(self._on_operation_started)
        self.worker.operation_finished.connect(self._on_operation_finished)
        self.worker.components_listed.connect(self._on_components_listed)
        self.worker.updates_checked.connect(self._on_updates_checked)
        self.worker.component_update_progress.connect(self._on_component_update_progress)
        self.worker.component_update_completed.connect(self._on_component_update_completed)
        self.worker.components_update_completed.connect(self._on_components_update_completed)
        self.worker.operation_failed.connect(self._on_operation_failed)
        self.worker.log_message.connect(self._on_worker_log_message)
        i18n.language_changed.connect(self.retranslate_ui)

    def refresh_local_status(self) -> None:
        """Start local-only refresh."""
        if self.worker.list_components():
            self._append_log(TR("component_log_refresh_started"))

    def check_updates(self) -> None:
        """Start read-only remote update check in worker thread."""
        if self.worker.check_updates(force=True):
            self._append_log(TR("component_log_check_started"))

    def retranslate_ui(self) -> None:
        self.setWindowTitle(TR("component_dialog_title"))
        self.title_label.setText(TR("component_dialog_title"))
        self.intro_label.setText(TR("component_dialog_intro"))
        self.refresh_btn.setText(TR("component_btn_refresh_local"))
        self.check_updates_btn.setText(TR("component_btn_check_updates"))
        self.update_all_btn.setText(TR("component_btn_update_all"))
        self.close_btn.setText(TR("btn_close"))
        self.detail_group.setTitle(TR("component_detail_title"))
        self.log_group.setTitle(TR("component_log_title"))
        self.detail_component_label.setText(TR("component_detail_component"))
        self.detail_status_label.setText(TR("component_detail_status"))
        self.detail_path_label.setText(TR("component_detail_path"))
        self.detail_message_label.setText(TR("component_detail_message"))
        self._set_headers()
        self._render_rows()
        self._update_summary()

    def apply_statuses(self, statuses: list[ComponentUpdateStatus]) -> None:
        """Public refresh helper used by worker callbacks and smoke tests."""
        self._rows = [component_status_to_row(status) for status in statuses]
        self._status_counts = summarize_rows(self._rows)
        self._render_rows()
        self._update_summary()
        if self.table.rowCount() > 0 and not self.table.selectedItems():
            self.table.selectRow(0)
        else:
            self._sync_detail_from_selection()
        self._emit_component_status_changed()

    def current_entry_summary(self) -> dict[str, int]:
        """Return the main-window entry badge summary for the current dialog rows."""
        return summarize_component_entry_rows(self._rows)

    def _emit_component_status_changed(self) -> None:
        """Notify owners that component status counts may have changed."""
        rows = list(self._rows)
        self.component_status_summary_changed.emit(summarize_component_entry_rows(rows))
        self.component_statuses_changed.emit(rows)

    def _set_headers(self) -> None:
        self.table.setHorizontalHeaderLabels([TR(key) for key in self.TABLE_COLUMNS])
        self._configure_table_columns()

    def _configure_table_columns(self) -> None:
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        widths = {
            0: self.COMPONENT_COLUMN_WIDTH,
            1: self.CATEGORY_COLUMN_WIDTH,
            2: self.VERSION_COLUMN_WIDTH,
            3: self.VERSION_COLUMN_WIDTH,
            4: self.STATUS_COLUMN_WIDTH,
            6: self.PROGRESS_COLUMN_WIDTH,
            7: self.ACTION_COLUMN_WIDTH,
        }
        for column, width in widths.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(48)
        self.table.setColumnWidth(5, self.PATH_COLUMN_MIN_WIDTH)
        self.table.setColumnWidth(6, self.PROGRESS_COLUMN_WIDTH)
        self.table.setColumnWidth(7, self.ACTION_COLUMN_WIDTH)

    def _create_table_item(self, row: ComponentTableRow, column: int, value: str) -> QTableWidgetItem:
        text = display_text_for_column(column, value)
        item = QTableWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, row)
        item.setToolTip(value if value and value != "-" else "")
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if column == 4:
            item.setData(Qt.ItemDataRole.UserRole + 1, row.status_class)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        elif column == 6:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def _row_values(self, row: ComponentTableRow) -> list[str]:
        return [
            row.label,
            TR(f"component_category_{row.category}"),
            row.local_version,
            row.remote_version,
            TR(row.status_key),
            row.path,
            row.progress,
        ]

    def _stabilize_table_rows(self) -> None:
        for row_index in range(self.table.rowCount()):
            self.table.setRowHeight(row_index, self.TABLE_ROW_HEIGHT)

    def _render_rows(self) -> None:
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            for column, value in enumerate(self._row_values(row)):
                self.table.setItem(row_index, column, self._create_table_item(row, column, value))
            action_btn = self._make_action_button(row)
            self.table.setCellWidget(row_index, 7, action_btn)
        self._stabilize_table_rows()
        self._sync_detail_from_selection()
        self._update_bulk_button_state()

    def _make_action_button(self, row: ComponentTableRow) -> QPushButton:
        action_key = action_key_for_row(row)
        action_btn = QPushButton(TR(action_key or "component_action_not_available"))
        action_btn.setMinimumWidth(self.ACTION_BUTTON_MIN_WIDTH)
        action_btn.setMaximumHeight(self.ACTION_BUTTON_HEIGHT)
        action_btn.setMinimumHeight(self.ACTION_BUTTON_HEIGHT)
        action_btn.setProperty("component_id", row.component_id)
        running = self._active_update_component_id is not None or self._batch_running or self.worker.is_running()
        enabled = action_key is not None and not running and row.status not in RUNNING_STATUSES
        action_btn.setEnabled(enabled)
        action_btn.setObjectName("component_action_button" if enabled else "component_action_disabled")
        if action_key is not None:
            action_btn.clicked.connect(lambda _checked=False, component_id=row.component_id: self.confirm_and_update_component(component_id))
        return action_btn

    def _update_summary(self) -> None:
        total = len(self._rows)
        update_count = self._status_counts.get("update", 0)
        error_count = self._status_counts.get("error", 0)
        ok_count = self._status_counts.get("ok", 0)
        self.summary_label.setText(
            TR("component_summary_template", total=total, ok=ok_count, updates=update_count, errors=error_count)
        )
        self._update_bulk_button_state()

    def _sync_detail_from_selection(self) -> None:
        selected = self.table.selectedItems()
        row = selected[0].data(Qt.ItemDataRole.UserRole) if selected else None
        if not isinstance(row, ComponentTableRow):
            self.detail_component_value.setText("-")
            self.detail_status_value.setText("-")
            self.detail_path_value.setText("-")
            self.detail_message_value.setText("-")
            return
        self.detail_component_value.setText(f"{row.label} ({row.component_id})")
        self.detail_status_value.setText(TR(row.status_key))
        self.detail_status_value.setProperty("component_status_class", STATUS_CLASS_TO_OBJECT.get(row.status_class, "component_status_unknown"))
        self.detail_status_value.style().unpolish(self.detail_status_value)
        self.detail_status_value.style().polish(self.detail_status_value)
        self.detail_path_value.setText(row.path or "-")
        self.detail_message_value.setText(row.message or "-")

    def _append_log(self, message: str) -> None:
        self.log_text.append(message)

    def collect_bulk_update_rows(self) -> list[ComponentTableRow]:
        """Collect rows eligible for bulk update/install/retry from the current table state."""
        eligible: list[ComponentTableRow] = []
        for row in self._rows:
            if row.status in RUNNING_STATUSES:
                continue
            if row.update_available or row.status in {"update_available", "missing"} or row.status in FAILURE_STATUSES:
                eligible.append(row)
        return eligible

    def confirm_and_update_all(self, force: bool = False) -> bool:
        """Confirm and start a batch update for all eligible rows."""
        rows = self.collect_bulk_update_rows()
        if self.worker.is_running() or self._active_update_component_id is not None or self._batch_running or not rows:
            return False
        reply = QMessageBox.question(
            self,
            TR("component_confirm_title"),
            TR("component_confirm_update_all_message", count=len(rows)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._append_log(TR("component_log_batch_update_cancelled"))
            return False
        return self.start_batch_update([row.component_id for row in rows], force=force)

    def start_batch_update(self, component_ids: list[str], force: bool = False) -> bool:
        """Start batch worker update after confirmation; exposed for fake smoke tests."""
        if not component_ids or self.worker.is_running() or self._active_update_component_id is not None or self._batch_running:
            return False
        rows = [row for row in self._rows if row.component_id in set(component_ids)]
        if not rows:
            return False
        self._batch_running = True
        self._active_batch_component_ids = list(component_ids)
        for row in rows:
            self._set_row(row.component_id, self._row_with_status(row, "started", progress="0%", message=TR("component_status_running")))
        self._append_log(TR("component_log_batch_update_started", count=len(component_ids)))
        started = self.worker.update_components(component_ids=component_ids, force=force)
        if not started:
            self._batch_running = False
            self._active_batch_component_ids = []
            self._append_log(TR("component_log_update_start_rejected"))
            self._set_busy(False)
            return False
        self._set_busy(True)
        return True

    def confirm_and_update_component(self, component_id: str, force: bool = False) -> bool:
        """Confirm and start a single-component update/install operation."""
        row = self._find_row(component_id)
        if row is None or self.worker.is_running() or self._active_update_component_id is not None or self._batch_running:
            return False
        action_key = action_key_for_row(row)
        if action_key is None:
            return False
        reply = QMessageBox.question(
            self,
            TR("component_confirm_title"),
            TR("component_confirm_update_message", action=TR(action_key), component=row.label),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._append_log(TR("component_log_update_cancelled", component=row.label))
            return False
        return self.start_component_update(component_id, force=force)

    def start_component_update(self, component_id: str, force: bool = False) -> bool:
        """Start worker update after confirmation; exposed for fake smoke tests."""
        row = self._find_row(component_id)
        if row is None or self._batch_running:
            return False
        self._active_update_component_id = component_id
        self._set_row(component_id, self._row_with_status(row, "started", progress="0%", message=TR("component_status_running")))
        self._append_log(TR("component_log_update_started", component=row.label))
        started = self.worker.update_component(component_id, force=force)
        if not started:
            self._active_update_component_id = None
            self._set_row(component_id, replace(row, message=TR("component_log_update_start_rejected")))
            self._append_log(TR("component_log_update_start_rejected"))
            return False
        self._set_busy(True)
        return True

    def _set_busy(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.check_updates_btn.setEnabled(not busy)
        self.close_btn.setEnabled(not busy)
        self._update_bulk_button_state()
        self._render_rows_without_bulk_recursion()

    def _render_rows_without_bulk_recursion(self) -> None:
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            for column, value in enumerate(self._row_values(row)):
                self.table.setItem(row_index, column, self._create_table_item(row, column, value))
            self.table.setCellWidget(row_index, 7, self._make_action_button(row))
        self._stabilize_table_rows()
        self._sync_detail_from_selection()

    def _update_bulk_button_state(self) -> None:
        busy = self.worker.is_running() or self._active_update_component_id is not None or self._batch_running
        eligible_count = len(self.collect_bulk_update_rows())
        self.update_all_btn.setEnabled((not busy) and eligible_count > 0)
        self.update_all_btn.setToolTip(TR("component_update_all_tooltip", count=eligible_count))

    @pyqtSlot(str)
    def _on_operation_started(self, operation: str) -> None:
        if operation == "update_components":
            self._batch_running = True
        self._set_busy(True)

    @pyqtSlot(str)
    def _on_operation_finished(self, operation: str) -> None:
        busy = self.worker.is_running()
        if operation == "update_component" and not busy:
            self._active_update_component_id = None
        if operation == "update_components" and not busy:
            self._batch_running = False
            self._active_batch_component_ids = []
        self._set_busy(busy)
        if operation == "check_updates":
            self._append_log(TR("component_log_check_finished"))
        elif operation == "list_components":
            self._append_log(TR("component_log_refresh_finished"))

    @pyqtSlot(list)
    def _on_components_listed(self, statuses: list[ComponentUpdateStatus]) -> None:
        self.apply_statuses(statuses)

    @pyqtSlot(list)
    def _on_updates_checked(self, statuses: list[ComponentUpdateStatus]) -> None:
        self.apply_statuses(statuses)

    @pyqtSlot(object)
    def _on_component_update_progress(self, event: ComponentUpdateProgressEvent | Any) -> None:
        component_id = str(getattr(event, "component_id", "") or "")
        if not component_id:
            return
        current = self._find_row(component_id)
        if current is None:
            return
        event_name = str(getattr(event, "event", "running") or "running")
        detail = str(getattr(event, "detail", "") or TR(f"component_status_{event_name}") if event_name else "")
        percent = getattr(event, "percent", None)
        progress = f"{percent}%" if percent is not None else current.progress
        updated = self._row_with_status(current, event_name, progress=progress, message=detail)
        self._set_row(component_id, updated)
        self._append_log(TR("component_log_update_progress", component=updated.label, event=TR(updated.status_key), detail=detail))

    @pyqtSlot(object)
    def _on_component_update_completed(self, result: ComponentUpdateResult | Any) -> None:
        self._apply_component_result(result)
        self._active_update_component_id = None
        self._set_busy(self.worker.is_running())

    @pyqtSlot(object)
    def _on_components_update_completed(self, result: ComponentBatchUpdateResult | Any) -> None:
        results = list(getattr(result, "results", []) or [])
        for item in results:
            self._apply_component_result(item)
        success = int(getattr(result, "success_count", sum(1 for item in results if bool(getattr(item, "success", False)) and not bool(getattr(item, "skipped", False)))))
        failed = int(getattr(result, "failure_count", sum(1 for item in results if not bool(getattr(item, "success", False)) and not bool(getattr(item, "skipped", False)))))
        skipped = int(getattr(result, "skipped_count", sum(1 for item in results if bool(getattr(item, "skipped", False)))))
        summary = TR("component_batch_summary", success=success, failed=failed, skipped=skipped)
        self._append_log(summary)
        QMessageBox.information(self, TR("component_batch_summary_title"), summary)
        self._batch_running = False
        self._active_batch_component_ids = []
        self._set_busy(self.worker.is_running())

    def _apply_component_result(self, result: ComponentUpdateResult | Any) -> None:
        component_id = str(getattr(result, "component_id", "") or "")
        if not component_id:
            return
        current = self._find_row(component_id)
        label = str(getattr(result, "label", None) or (current.label if current else component_id))
        success = bool(getattr(result, "success", False))
        skipped = bool(getattr(result, "skipped", False))
        if success:
            status = "latest" if skipped else "updated"
            message = str(getattr(result, "message", "") or TR("component_update_success"))
            new_version = _display(getattr(result, "new_version", None) or getattr(result, "remote_version", None))
            row = current or ComponentTableRow(component_id, label, "unknown", "-", new_version, status, "component_status_latest", "ok", "", "100%", message)
            row = replace(
                row,
                local_version=new_version if new_version != "-" else row.local_version,
                remote_version=new_version if new_version != "-" else row.remote_version,
                update_available=False,
            )
            self._set_row(component_id, self._row_with_status(row, status, progress="100%", message=message))
            self._append_log(TR("component_log_update_success", component=label))
        else:
            error = str(getattr(result, "error", "") or getattr(result, "message", "") or TR("component_update_failed"))
            row = current or ComponentTableRow(component_id, label, "unknown", "-", "-", "failed", "component_status_failed", "error", "", "-", error)
            self._set_row(component_id, self._row_with_status(row, "failed", progress="-", message=error))
            self._last_failed_component_id = component_id
            self._append_log(TR("component_log_update_failed", component=label, error=error))

    @pyqtSlot(str, str)
    def _on_operation_failed(self, operation: str, detail: str) -> None:
        error = detail.splitlines()[0] if detail else ""
        self._append_log(TR("component_log_operation_failed", operation=operation, error=error))
        if operation == "update_component" and self._active_update_component_id:
            component_id = self._active_update_component_id
            current = self._find_row(component_id)
            if current:
                self._set_row(component_id, self._row_with_status(current, "failed", progress="-", message=error))
                self._last_failed_component_id = component_id
            self._active_update_component_id = None
        if operation == "update_components":
            for component_id in self._active_batch_component_ids:
                current = self._find_row(component_id)
                if current and current.status in RUNNING_STATUSES:
                    self._set_row(component_id, self._row_with_status(current, "failed", progress="-", message=error))
            self._batch_running = False
            self._active_batch_component_ids = []
        self._set_busy(self.worker.is_running())

    @pyqtSlot(str)
    def _on_worker_log_message(self, key: str) -> None:
        self._append_log(TR(key))

    def _find_row(self, component_id: str) -> ComponentTableRow | None:
        return next((row for row in self._rows if row.component_id == component_id), None)

    def _set_row(self, component_id: str, row: ComponentTableRow) -> None:
        for index, existing in enumerate(self._rows):
            if existing.component_id == component_id:
                self._rows[index] = row
                break
        else:
            self._rows.append(row)
        self._status_counts = summarize_rows(self._rows)
        selected_component = self._selected_component_id()
        self._render_rows()
        self._update_summary()
        if selected_component:
            self._select_component(selected_component)
        self._emit_component_status_changed()

    def _row_with_status(self, row: ComponentTableRow, status: str, progress: str | None = None, message: str | None = None) -> ComponentTableRow:
        status_class, status_key = status_to_ui_state(status, update_available=False)
        return replace(
            row,
            status=status,
            status_key=status_key,
            status_class=status_class,
            progress=progress if progress is not None else row.progress,
            message=message if message is not None else row.message,
            update_available=False if status in {"latest", "updated", "failed"} | RUNNING_STATUSES else row.update_available,
        )

    def _selected_component_id(self) -> str | None:
        selected = self.table.selectedItems()
        row = selected[0].data(Qt.ItemDataRole.UserRole) if selected else None
        return row.component_id if isinstance(row, ComponentTableRow) else None

    def _select_component(self, component_id: str) -> None:
        for index, row in enumerate(self._rows):
            if row.component_id == component_id:
                self.table.selectRow(index)
                break

    def closeEvent(self, event) -> None:
        if self._batch_running or self.worker.is_running():
            reply = QMessageBox.question(
                self,
                TR("component_close_busy_title"),
                TR("component_close_busy_message"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.worker.shutdown()
        try:
            i18n.language_changed.disconnect(self.retranslate_ui)
        except TypeError:
            pass
        super().closeEvent(event)


def action_key_for_row(row: ComponentTableRow) -> str | None:
    """Return the translation key for a row-level action, or None when disabled."""
    if row.status in RUNNING_STATUSES:
        return None
    if row.status == "missing":
        return "component_action_install"
    if row.update_available or row.status == "update_available":
        return "component_action_update"
    if row.status in FAILURE_STATUSES:
        return "component_action_retry"
    return None


def component_status_to_row(status: ComponentUpdateStatus | Any) -> ComponentTableRow:
    """Map backend status object to table row data."""
    component_id = str(getattr(status, "component_id", ""))
    label = str(getattr(status, "label", component_id) or component_id)
    category = str(getattr(status, "category", "unknown") or "unknown")
    local = getattr(status, "local", None)
    remote = getattr(status, "remote", None)
    local_version = _display(getattr(local, "version", None))
    remote_version = _display(getattr(remote, "latest_version", None))
    path = str(getattr(local, "path", "") or "")
    raw_status = str(getattr(status, "status", "unknown") or "unknown")
    update_available = bool(getattr(status, "update_available", False))
    status_class, status_key = status_to_ui_state(raw_status, update_available=update_available)
    message = str(getattr(status, "message", "") or getattr(local, "error", "") or getattr(remote, "error", "") or "")
    progress = "100%" if raw_status in {"latest", "update_available", "local_checked", "updated"} else "-"
    return ComponentTableRow(
        component_id=component_id,
        label=label,
        category=category,
        local_version=local_version,
        remote_version=remote_version,
        status=raw_status,
        status_key=status_key,
        status_class=status_class,
        path=path,
        progress=progress,
        message=message,
        update_available=update_available,
    )


def summarize_rows(rows: list[ComponentTableRow]) -> dict[str, int]:
    """Count row status classes for summary labels and smoke assertions."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status_class] = counts.get(row.status_class, 0) + 1
    return counts


def summarize_component_entry_rows(rows: list[ComponentTableRow]) -> dict[str, int]:
    """Summarize dialog rows for the main-window component entry badge."""
    updates = 0
    missing = 0
    failed = 0
    total = 0
    for row in rows or []:
        total += 1
        raw_status = (row.status or "").strip().lower()
        if row.update_available or raw_status == "update_available":
            updates += 1
        if raw_status == "missing":
            missing += 1
        if raw_status in FAILURE_STATUSES:
            failed += 1
    return {"updates": updates, "missing": missing, "failed": failed, "total": total}


def _display(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text or "-"


def display_text_for_column(column: int, value: str) -> str:
    """Return compact single-line table text while preserving full value in tooltips."""
    text = _display(value)
    if text == "-":
        return text
    if column == 5:
        return compact_path_text(text)
    if column in {2, 3}:
        if _looks_like_url(text):
            return compact_url_text(text)
        return middle_elide_text(text, 42)
    return middle_elide_text(text, 36)


def _looks_like_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def compact_url_text(text: str, limit: int = 42) -> str:
    parsed = urlparse(text)
    filename = unquote(PureWindowsPath(parsed.path).name)
    if filename:
        return middle_elide_text(filename, limit)
    host = parsed.netloc or text
    return middle_elide_text(host, limit)


def compact_path_text(text: str, limit: int = 48) -> str:
    normalized = text.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    if len(parts) >= 2:
        compact = "...\\" + "\\".join(parts[-2:])
    elif parts:
        compact = parts[-1]
    else:
        compact = normalized
    return middle_elide_text(compact, limit)


def middle_elide_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return "." * limit
    head = max(1, (limit - 3) // 2)
    tail = max(1, limit - 3 - head)
    return f"{text[:head]}...{text[-tail:]}"



def make_fake_status(
    component_id: str,
    label: str,
    category: str,
    status: str,
    local_version: str | None = None,
    remote_version: str | None = None,
    update_available: bool = False,
    message: str | None = None,
    path: str | None = None,
) -> ComponentUpdateStatus:
    """Small factory used by UI smoke tests without real service/network access."""
    local = ComponentVersionInfo(
        component_id=component_id,
        label=label,
        path=path if path is not None else f"sandbox_bin/{component_id}.exe",
        exists=status != "missing",
        version=local_version,
    )
    remote = RemoteReleaseInfo(component_id=component_id, latest_version=remote_version)
    return ComponentUpdateStatus(
        component_id=component_id,
        label=label,
        category=category,
        local=local,
        remote=remote,
        update_available=update_available,
        status=status,
        message=message,
    )
