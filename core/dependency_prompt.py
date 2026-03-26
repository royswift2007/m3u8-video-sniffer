"""
Dependency confirmation prompt helpers.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtWidgets import QMessageBox, QWidget

from core.dependency_manifest import DependencyEntry


def build_missing_dependency_prompt_text(missing_entries: Sequence[DependencyEntry]) -> str:
    """Build user-facing text for missing required dependencies."""
    lines = [
        "检测到缺失的必须依赖，启动前需要先安装以下组件：",
        "",
    ]
    for entry in missing_entries:
        lines.append(f"- {entry.label} ({entry.relative_path})")
    lines.extend(
        [
            "",
            "点击“确定”后将立即开始下载必须依赖。",
            "下载成功后程序才会继续启动；点击“取消”将直接退出。",
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
    message.setWindowTitle("缺少必须依赖")
    message.setText("检测到启动所需组件缺失")
    message.setInformativeText("点击“确定”后将立即下载必须依赖。")
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
    detail_text = "\n".join(f"- {error}" for error in errors) if errors else "- 未提供详细错误信息"
    message = QMessageBox(parent)
    message.setIcon(QMessageBox.Icon.Critical)
    message.setWindowTitle("必须依赖安装失败")
    message.setText("启动所需组件安装失败，程序将退出。")
    message.setInformativeText("请检查网络连接或手动补齐 bin 目录中的必须依赖后重试。")
    message.setDetailedText(detail_text)
    message.setStandardButtons(QMessageBox.StandardButton.Ok)
    message.setDefaultButton(QMessageBox.StandardButton.Ok)
    message.exec()
