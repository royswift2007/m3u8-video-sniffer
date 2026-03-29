"""
Dependency confirmation prompt helpers.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtWidgets import QMessageBox, QWidget

from core.dependency_manifest import DependencyEntry
from utils.i18n import TR


def build_missing_dependency_prompt_text(missing_entries: Sequence[DependencyEntry]) -> str:
    """Build user-facing text for missing required dependencies."""
    lines = [
        TR("msg_dep_missing_title_text"),
        "",
    ]
    for entry in missing_entries:
        lines.append(f"- {entry.label} ({entry.relative_path})")
    lines.extend(
        [
            "",
            TR("msg_dep_click_ok_to_install"),
            TR("msg_dep_cancel_to_exit"),
        ]
    )
    return "\n".join(lines)


def show_missing_dependency_confirmation(
    missing_entries: Sequence[DependencyEntry],
    parent: QWidget | None = None,
) -> bool:
    """Show a blocking confirmation dialog for missing required dependencies."""
    message = QMessageBox(parent)
    message.setIcon(QMessageBox.Icon.Warning)
    message.setWindowTitle(TR("msg_dep_missing_window_title"))
    message.setText(TR("msg_dep_missing_main_text"))
    message.setInformativeText(TR("msg_dep_click_ok_to_install"))
    message.setDetailedText(build_missing_dependency_prompt_text(missing_entries))
    message.setStandardButtons(
        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
    )
    message.setDefaultButton(QMessageBox.StandardButton.Ok)
    return message.exec() == QMessageBox.StandardButton.Ok


def show_dependency_install_failure(
    errors: Sequence[str],
    parent: QWidget | None = None,
) -> None:
    """Show a blocking error dialog after dependency installation fails."""
    detail_text = "\n".join(f"- {error}" for error in errors) if errors else f"- {TR('log_dep_no_error_provided')}"
    message = QMessageBox(parent)
    message.setIcon(QMessageBox.Icon.Critical)
    message.setWindowTitle(TR("msg_dep_install_failed_title"))
    message.setText(TR("msg_dep_install_failed_main"))
    message.setInformativeText(TR("msg_dep_install_failed_hint"))
    message.setDetailedText(detail_text)
    message.setStandardButtons(QMessageBox.StandardButton.Ok)
    message.setDefaultButton(QMessageBox.StandardButton.Ok)
    message.exec()
