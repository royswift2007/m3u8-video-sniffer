"""
Qt worker bridge for component update service operations.

This module keeps potentially slow component update operations outside the Qt main
thread. It exposes local listing, read-only remote checks, and user-confirmed
single-component or batch update/install execution through Qt signals.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any, Literal

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.component_update_models import ComponentUpdateProgressEvent
from core.component_update_service import ComponentUpdateService

OperationName = Literal["list_components", "check_updates", "update_component", "update_components"]
ServiceFactory = Callable[[], ComponentUpdateService]


class _ComponentUpdateTask(QObject):
    """One-shot background task for a component update service operation."""

    succeeded = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)
    progress_event = pyqtSignal(object)
    partial_statuses = pyqtSignal(list)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(
        self,
        operation: OperationName,
        service_factory: ServiceFactory,
        component_ids: list[str] | None = None,
        force: bool = False,
    ):
        super().__init__()
        self.operation = operation
        self.service_factory = service_factory
        self.component_ids = component_ids
        self.force = force

    @pyqtSlot()
    def run(self) -> None:
        """Execute the selected operation in the owning QThread."""
        try:
            service = self.service_factory()
            if self.operation == "list_components":
                self.log_message.emit("component_update_worker_list_started")
                manifest_statuses = service.get_manifest_statuses(component_ids=self.component_ids)
                self.partial_statuses.emit(manifest_statuses)
                self.log_message.emit("component_update_worker_manifest_listed")
                result = service.refresh_local_status(component_ids=self.component_ids)
            elif self.operation == "check_updates":
                self.log_message.emit("component_update_worker_check_started")
                result = service.check_updates(component_ids=self.component_ids, force=self.force)
            elif self.operation == "update_component":
                component_id = (self.component_ids or [""])[0]
                if not component_id:
                    raise ValueError("component_id is required for update_component")
                self.log_message.emit("component_update_worker_update_started")
                self.progress_event.emit(
                    ComponentUpdateProgressEvent(
                        event="started",
                        component_id=component_id,
                        label=component_id,
                        detail="component update started",
                        percent=0,
                    )
                )
                result = service.update_component(
                    component_id,
                    force=self.force,
                    progress_callback=self.progress_event.emit,
                )
            elif self.operation == "update_components":
                self.log_message.emit("component_update_worker_batch_update_started")
                result = service.update_components(
                    component_ids=self.component_ids,
                    force=self.force,
                    progress_callback=self.progress_event.emit,
                )
            else:
                raise ValueError(f"Unsupported component update worker operation: {self.operation}")
            self.succeeded.emit(self.operation, result)
        except Exception as exc:  # pragma: no cover - defensive UI bridge
            detail = f"{exc}\n{traceback.format_exc()}"
            self.failed.emit(self.operation, detail)
        finally:
            self.finished.emit(self.operation)


class ComponentUpdateWorker(QObject):
    """Qt signal bridge for ComponentUpdateService operations."""

    operation_started = pyqtSignal(str)
    operation_finished = pyqtSignal(str)
    components_listed = pyqtSignal(list)
    updates_checked = pyqtSignal(list)
    component_update_progress = pyqtSignal(object)
    component_update_completed = pyqtSignal(object)
    components_update_completed = pyqtSignal(object)
    operation_failed = pyqtSignal(str, str)
    log_message = pyqtSignal(str)

    def __init__(self, service_factory: ServiceFactory | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.service_factory = service_factory or ComponentUpdateService
        self._threads: list[QThread] = []
        self._tasks: list[_ComponentUpdateTask] = []
        self._running_operations: set[str] = set()

    def is_running(self, operation: str | None = None) -> bool:
        """Return whether any operation or a specific operation is active."""
        if operation is None:
            return bool(self._running_operations)
        return operation in self._running_operations

    def list_components(self, component_ids: list[str] | None = None) -> bool:
        """Asynchronously refresh local component status without remote network."""
        return self._start_operation("list_components", component_ids=component_ids, force=False)

    def check_updates(self, component_ids: list[str] | None = None, force: bool = True) -> bool:
        """Asynchronously run read-only remote update checks."""
        return self._start_operation("check_updates", component_ids=component_ids, force=force)

    def update_component(self, component_id: str, force: bool = False) -> bool:
        """Asynchronously update/install one component after the UI has confirmed user intent."""
        return self._start_operation("update_component", component_ids=[component_id], force=force)

    def update_components(self, component_ids: list[str] | None = None, force: bool = False) -> bool:
        """Asynchronously update/install multiple components after explicit UI confirmation."""
        return self._start_operation("update_components", component_ids=component_ids, force=force)

    def shutdown(self) -> None:
        """Ask active worker threads to quit; used when the dialog closes."""
        for thread in list(self._threads):
            thread.quit()
            thread.wait(1500)
        self._threads.clear()
        self._tasks.clear()
        self._running_operations.clear()

    def _start_operation(self, operation: OperationName, component_ids: list[str] | None, force: bool) -> bool:
        if operation in self._running_operations:
            self.log_message.emit("component_update_worker_operation_already_running")
            return False
        update_operations = {"update_component", "update_components"}
        if operation in update_operations and self._running_operations:
            self.log_message.emit("component_update_worker_operation_already_running")
            return False
        if operation not in update_operations and self._running_operations.intersection(update_operations):
            self.log_message.emit("component_update_worker_update_running")
            return False

        thread = QThread(self)
        task = _ComponentUpdateTask(operation, self.service_factory, component_ids=component_ids, force=force)
        task.moveToThread(thread)

        thread.started.connect(task.run)
        task.succeeded.connect(self._handle_success)
        task.failed.connect(self._handle_failure)
        task.progress_event.connect(self.component_update_progress)
        task.partial_statuses.connect(self.components_listed)
        task.log_message.connect(self.log_message)
        task.finished.connect(self._handle_finished)
        task.finished.connect(thread.quit)
        task.finished.connect(task.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda thread=thread: self._remove_thread(thread))
        thread.finished.connect(lambda task=task: self._remove_task(task))

        self._threads.append(thread)
        self._tasks.append(task)
        self._running_operations.add(operation)
        self.operation_started.emit(operation)
        thread.start()
        return True

    @pyqtSlot(str, object)
    def _handle_success(self, operation: str, result: Any) -> None:
        statuses = list(result or []) if operation not in {"update_component", "update_components"} else []
        if operation == "list_components":
            self.components_listed.emit(statuses)
        elif operation == "check_updates":
            self.updates_checked.emit(statuses)
        elif operation == "update_component":
            self.component_update_completed.emit(result)
        elif operation == "update_components":
            self.components_update_completed.emit(result)

    @pyqtSlot(str, str)
    def _handle_failure(self, operation: str, detail: str) -> None:
        self.operation_failed.emit(operation, detail)

    @pyqtSlot(str)
    def _handle_finished(self, operation: str) -> None:
        self._running_operations.discard(operation)
        self.operation_finished.emit(operation)

    def _remove_thread(self, thread: QThread) -> None:
        try:
            self._threads.remove(thread)
        except ValueError:
            pass

    def _remove_task(self, task: _ComponentUpdateTask) -> None:
        try:
            self._tasks.remove(task)
        except ValueError:
            pass


def status_to_ui_state(status: str, update_available: bool = False) -> tuple[str, str]:
    """Return a coarse UI class and translation key for a component status."""
    normalized = (status or "unknown").strip().lower()
    if update_available or normalized == "update_available":
        return "update", "component_status_update_available"
    if normalized in {"latest", "local_checked", "installed", "updated", "manifest_listed", "local_check_failed"}:
        return "ok", "component_status_latest" if normalized in {"latest", "updated"} else "component_status_installed"
    if normalized in {"started", "checking", "downloading", "staged", "installing", "running"}:
        key = "component_status_running" if normalized in {"started", "running"} else f"component_status_{normalized}"
        return "pending", key
    if normalized in {"missing", "remote_check_failed", "asset_missing", "failed"}:
        key = "component_status_failed" if normalized != "missing" else "component_status_missing"
        return "error", key
    return "unknown", "component_status_unknown"
