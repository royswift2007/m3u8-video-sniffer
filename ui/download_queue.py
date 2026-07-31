"""
Download queue panel for displaying active, completed, and failed downloads
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QTreeWidget,
                             QTreeWidgetItem, QPushButton, QHBoxLayout,
                             QProgressBar, QHeaderView, QMenu, QMessageBox,
                             QComboBox, QFrame, QInputDialog)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QAction, QCursor, QBrush
from core.task_model import DownloadTask, TaskSnapshot
from utils.logger import logger
from utils.i18n import i18n, TR
import subprocess
import os


def _task_owns_artifact(task: DownloadTask, artifact_name: str) -> bool:
    """Return True only for filenames that clearly belong to the task output."""
    if not task.filename or not artifact_name:
        return False
    return artifact_name == task.filename or artifact_name.startswith(f"{task.filename}.")


def _cleanup_temp_files_for_task(snapshot: dict) -> None:
    """清理任务临时文件（模块级纯函数，不依赖任何 Qt 对象）。
    
    ISS-38: 从 threading.Timer 调用，完全隔离于 Qt 事件循环
    和 widget 生命周期。即使 DownloadQueuePanel 已销毁，此
    函数仍安全执行纯文件系统操作。
    """
    import shutil
    from pathlib import Path
    from utils.config_manager import config

    filename = snapshot.get("filename", "")
    save_dir = snapshot.get("save_dir", "")
    status = snapshot.get("status", "")

    cleaned_count = 0
    try:
        # 1. 清理 N_m3u8DL-RE 临时目录
        temp_dir = Path(config.get("temp_dir", "temp")) / "n_m3u8dl"
        if temp_dir.exists():
            for item in temp_dir.iterdir():
                _name = item.name
                if filename and (_name == filename or _name.startswith(f"{filename}.")):
                    try:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                        cleaned_count += 1
                        logger.debug(f"已删除 N_m3u8DL-RE 临时文件: {item.name}")
                    except Exception as e:
                        logger.warning(f"无法删除临时文件 {item.name}: {e}")

        # 2. 清理下载目录中的 .part, .ytdl, .aria2 等文件
        download_dir = Path(save_dir) if save_dir else None
        if download_dir and download_dir.exists():
            for item in download_dir.iterdir():
                if not item.is_file():
                    continue

                name = item.name
                if not filename or not (name == filename or name.startswith(f"{filename}.")):
                    continue

                is_temp = False
                lower_name = name.lower()

                if lower_name.endswith(('.part', '.ytdl', '.temp', '.download', '.aria2')):
                    is_temp = True
                elif '.f' in lower_name and ('.mp4.part' in lower_name or '.webm.part' in lower_name or '.m4a.part' in lower_name):
                    is_temp = True
                elif name == filename and status != 'completed':
                    is_temp = True

                if is_temp:
                    try:
                        item.unlink(missing_ok=True)
                        cleaned_count += 1
                        logger.debug(f"已删除下载临时文件: {name}")
                    except Exception as e:
                        logger.warning(f"无法删除文件 {name}: {e}")

        logger.info(f"已清理 {cleaned_count} 个临时文件: {filename}")
    except Exception as e:
        logger.error(f"清理临时文件失败: {e}")


class DownloadQueuePanel(QWidget):
    """下载队列面板"""
    
    # 信号
    task_paused = pyqtSignal(DownloadTask)
    task_resumed = pyqtSignal(DownloadTask)
    task_cancelled = pyqtSignal(DownloadTask)
    task_retried = pyqtSignal(DownloadTask)
    task_removed = pyqtSignal(DownloadTask)  # 新增：彻底移除任务
    task_batch_imported = pyqtSignal(list)
    task_postprocess_requested = pyqtSignal(DownloadTask, str)
    
    def __init__(self):
        super().__init__()
        self.task_items = {}  # task_id -> QTreeWidgetItem
        self.tasks = {}  # task_id -> DownloadTask（控制句柄）
        self.task_snapshots = {}  # task_id -> TaskSnapshot（显示状态源）
        self._selected_task_id = None
        self._init_ui()
        self.retranslate_ui()
    
    def _init_ui(self):
        """初始化 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        panel_card = QFrame()
        panel_card.setObjectName("panel_card")
        panel_layout = QVBoxLayout(panel_card)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(8)
        
        # 标题
        title_layout = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(2)

        self.title_label = QLabel("")
        self.title_label.setObjectName("section_title")
        title_block.addWidget(self.title_label)

        self.intro_label = QLabel("")
        self.intro_label.setObjectName("panel_intro")
        title_block.addWidget(self.intro_label)

        title_layout.addLayout(title_block)

        title_layout.addStretch()

        # 状态过滤
        self.status_filter = QComboBox()
        self.status_filter.setMinimumWidth(140)
        self.status_filter.currentIndexChanged.connect(self._apply_status_filter)
        title_layout.addWidget(self.status_filter)

        panel_layout.addLayout(title_layout)
        
        # 下载列表
        self.tree = QTreeWidget()
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(2, 120)
        self.tree.setColumnWidth(3, 100)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame) # 去除边框
        
        # 启用右键菜单
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        
        panel_layout.addWidget(self.tree)
        
        # 控制按钮
        btn_layout = QHBoxLayout()
        
        self.pause_btn = QPushButton("")
        self.pause_btn.setObjectName("queue_pause_button")
        self.pause_btn.clicked.connect(self._on_pause_selected)
        btn_layout.addWidget(self.pause_btn)
        
        self.resume_btn = QPushButton("")
        self.resume_btn.setObjectName("queue_resume_button")
        self.resume_btn.clicked.connect(self._on_resume_selected)
        btn_layout.addWidget(self.resume_btn)
        
        self.stop_btn = QPushButton("")
        self.stop_btn.setObjectName("queue_stop_button")
        self.stop_btn.clicked.connect(self._on_stop_selected)
        btn_layout.addWidget(self.stop_btn)
        
        self.delete_btn = QPushButton("")
        self.delete_btn.setObjectName("queue_delete_button")
        self.delete_btn.clicked.connect(self._on_delete_selected)
        btn_layout.addWidget(self.delete_btn)
        
        self.retry_btn = QPushButton("")
        self.retry_btn.setObjectName("queue_retry_button")
        self.retry_btn.clicked.connect(self._on_retry_selected)
        btn_layout.addWidget(self.retry_btn)
        
        self.open_btn = QPushButton("")
        self.open_btn.setObjectName("queue_open_button")
        self.open_btn.clicked.connect(self._on_open_selected)
        btn_layout.addWidget(self.open_btn)
        
        self.pause_all_btn = QPushButton("")
        self.pause_all_btn.setObjectName("queue_pauseall_button")
        self.pause_all_btn.clicked.connect(self._on_pause_all)
        btn_layout.addWidget(self.pause_all_btn)
        
        self.clear_completed_btn = QPushButton("")
        self.clear_completed_btn.setObjectName("queue_clear_button")
        self.clear_completed_btn.clicked.connect(self._on_clear_completed)
        btn_layout.addWidget(self.clear_completed_btn)
        
        self.sort_status_btn = QPushButton("")
        self.sort_status_btn.setObjectName("queue_sort_button")
        self.sort_status_btn.clicked.connect(self._on_sort_by_status)
        btn_layout.addWidget(self.sort_status_btn)
        
        self.import_btn = QPushButton("")
        self.import_btn.setObjectName("queue_import_button")
        self.import_btn.clicked.connect(self._on_batch_import)
        btn_layout.addWidget(self.import_btn)
        
        btn_layout.addStretch()
        panel_layout.addLayout(btn_layout)
        layout.addWidget(panel_card)
        self._refresh_task_action_buttons()

    def retranslate_ui(self):
        """翻译 UI 文字"""
        self.title_label.setText(TR("tab_downloading"))
        self.intro_label.setText(TR("intro_download_queue"))
        
        # 过滤器
        self.status_filter.blockSignals(True)
        status_idx = self.status_filter.currentIndex()
        self.status_filter.clear()
        self.status_filter.addItems([
            TR("status_all"), TR("status_downloading"), TR("status_waiting"), 
            TR("status_paused"), TR("status_failed"), TR("status_completed")
        ])
        self.status_filter.setCurrentIndex(max(0, status_idx))
        self.status_filter.blockSignals(False)
        
        # 列表头
        self.tree.setHeaderLabels([
            TR("col_filename"), TR("col_status"), TR("col_progress"), 
            TR("col_speed"), TR("col_engine")
        ])
        
        # 按钮
        self.pause_btn.setText(TR("btn_pause"))
        self.resume_btn.setText(TR("btn_resume"))
        self.stop_btn.setText(TR("btn_stop"))
        self.delete_btn.setText(TR("btn_delete"))
        self.retry_btn.setText(TR("btn_retry"))
        self.open_btn.setText(TR("btn_open_folder"))
        self.pause_all_btn.setText(TR("btn_pause_all"))
        self.clear_completed_btn.setText(TR("btn_clear_completed"))
        self.sort_status_btn.setText(TR("btn_sort_status"))
        self.import_btn.setText(TR("btn_batch_import"))
        
        # 刷新所有任务项目的状态文字。显示状态优先使用不可变 snapshot，
        # raw task 仅作为尚未收到 snapshot 时的兼容回退。
        for task_id, item in self.task_items.items():
            snapshot = self.task_snapshots.get(task_id)
            if snapshot is not None:
                self._update_item_from_snapshot(item, snapshot)
                continue
            task = self.tasks.get(task_id)
            if task is not None:
                self._update_item(item, task)
    
    def _show_context_menu(self, position):
        """显示右键菜单"""
        item = self.tree.itemAt(position)
        if not item:
            return
        
        # 获取对应的任务
        task = None
        for task_id, tree_item in self.task_items.items():
            if tree_item == item:
                task = self.tasks.get(task_id)
                break
        
        if not task:
            return
        
        # 创建菜单
        menu = QMenu(self)
        
        # 根据任务状态添加不同的菜单项
        if task.status == "downloading":
            pause_action = QAction(f"⏸️ {TR('btn_pause')}", self)
            pause_action.triggered.connect(lambda: self._pause_task(task))
            menu.addAction(pause_action)
            
            stop_action = QAction(f"⏹️ {TR('btn_stop')}", self)
            stop_action.triggered.connect(lambda: self._stop_task(task))
            menu.addAction(stop_action)
            
        elif task.status == "paused":
            resume_action = QAction(f"▶️ {TR('btn_resume')}", self)
            resume_action.triggered.connect(lambda: self._resume_task(task))
            menu.addAction(resume_action)
            
        elif task.status == "failed":
            retry_action = QAction(f"🔄 {TR('btn_retry')}", self)
            retry_action.triggered.connect(lambda: self._retry_task(task))
            menu.addAction(retry_action)
            
        elif task.status == "completed":
            open_action = QAction(f"📂 {TR('btn_open_file_location')}", self)
            open_action.triggered.connect(lambda: self._open_file_location(task))
            menu.addAction(open_action)
            
            play_action = QAction(f"▶️ {TR('btn_play')}", self)
            play_action.triggered.connect(lambda: self._play_file(task))
            menu.addAction(play_action)

            postprocess_menu = menu.addMenu(f"🧰 {TR('btn_postprocess')}")
            postprocess_actions = [
                ("convert_mp4", "btn_postprocess_remux_mp4", "🎞️"),
                ("merge_audio", "btn_postprocess_merge_audio", "🔀"),
                ("extract_subtitles", "btn_postprocess_extract_subtitles", "💬"),
                ("compress_video", "btn_postprocess_compress_video", "🗜️"),
            ]
            for action_key, label_key, icon in postprocess_actions:
                action = QAction(f"{icon} {TR(label_key)}", self)
                action.triggered.connect(
                    lambda _checked=False, key=action_key: self._request_postprocess(task, key)
                )
                postprocess_menu.addAction(action)
        
        menu.addSeparator()
        
        # 通用菜单项
        copy_url_action = QAction(f"📋 {TR('btn_copy_link')}", self)
        copy_url_action.triggered.connect(lambda: self._copy_url(task))
        menu.addAction(copy_url_action)
        
        # 删除任务（所有状态都可以删除）
        delete_action = QAction(f"🗑️ {TR('btn_delete')}", self)
        delete_action.triggered.connect(lambda: self._delete_task(task))
        menu.addAction(delete_action)
        
        # 显示菜单
        menu.exec(QCursor.pos())

    def _request_postprocess(self, task: DownloadTask, action: str):
        """Request FFmpeg post-processing for a completed task."""
        self.task_postprocess_requested.emit(task, action)

    def _get_task_by_item(self, item: QTreeWidgetItem | None):
        """根据列表项获取任务"""
        if not item:
            return None
        for task_id, tree_item in self.task_items.items():
            if tree_item == item:
                self._selected_task_id = task_id
                return self.tasks.get(task_id)
        return None

    def _get_selected_task(self):
        """获取当前选中的任务"""
        item = self.tree.currentItem()
        return self._get_task_by_item(item)

    def _on_current_item_changed(self, current, previous):
        """切换选中任务时刷新底部控制按钮"""
        self._get_task_by_item(current)
        self._refresh_task_action_buttons()

    def _refresh_task_action_buttons(self):
        """根据当前选中任务状态刷新按钮可用性"""
        task = self._get_selected_task()

        self.pause_btn.setEnabled(bool(task and task.status == "downloading"))
        self.resume_btn.setEnabled(bool(task and task.status == "paused"))
        self.stop_btn.setEnabled(bool(task and task.status == "downloading"))
        self.retry_btn.setEnabled(bool(task and task.status == "failed"))
        self.open_btn.setEnabled(bool(task and task.status == "completed"))
        self.delete_btn.setEnabled(task is not None)

    def _on_pause_selected(self):
        task = self._get_selected_task()
        if task:
            self._pause_task(task)

    def _on_resume_selected(self):
        task = self._get_selected_task()
        if task:
            self._resume_task(task)

    def _on_stop_selected(self):
        task = self._get_selected_task()
        if task:
            self._stop_task(task)

    def _on_retry_selected(self):
        task = self._get_selected_task()
        if task:
            self._retry_task(task)

    def _on_open_selected(self):
        task = self._get_selected_task()
        if task:
            self._open_file_location(task)

    def _on_delete_selected(self):
        task = self._get_selected_task()
        if task:
            self._delete_task(task)
    
    def _pause_task(self, task: DownloadTask):
        """暂停任务"""
        logger.info(f"用户请求暂停任务: {task.filename}")
        self.task_paused.emit(task)
    
    def _resume_task(self, task: DownloadTask):
        """继续任务"""
        logger.info(f"用户请求继续任务: {task.filename}")
        self.task_resumed.emit(task)
    
    def _stop_task(self, task: DownloadTask):
        """停止任务"""
        reply = QMessageBox.question(
            self, TR("dialog_confirm_stop"),
            TR("msg_confirm_stop_task", filename=task.filename),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            logger.info(f"用户请求停止任务: {task.filename}")
            self.task_cancelled.emit(task)
    
    def _retry_task(self, task: DownloadTask):
        """重试任务"""
        logger.info(f"用户请求重试任务: {task.filename}")
        self.task_retried.emit(task)
    
    def _open_file_location(self, task: DownloadTask):
        """打开文件所在位置"""
        from pathlib import Path
        file_path = Path(task.save_dir) / task.filename
        
        # 尝试找到带扩展名的文件
        possible_files = list(Path(task.save_dir).glob(f"{task.filename}.*"))
        if possible_files:
            file_path = possible_files[0]
        
        try:
            if file_path.exists():
                # Windows: 选中文件
                subprocess.run(['explorer', '/select,', str(file_path)])
            else:
                # 打开目录
                os.startfile(task.save_dir)
        except Exception as e:
            logger.error(f"打开文件位置失败: {e}")
    
    def _play_file(self, task: DownloadTask):
        """播放文件"""
        from pathlib import Path
        
        # 尝试找到带扩展名的文件
        possible_files = list(Path(task.save_dir).glob(f"{task.filename}.*"))
        if possible_files:
            file_path = possible_files[0]
            try:
                os.startfile(str(file_path))
            except Exception as e:
                logger.error(f"播放文件失败: {e}")
        else:
            QMessageBox.warning(self, TR("dialog_file_not_found"), TR("msg_file_not_found", filename=task.filename))
    
    def _copy_url(self, task: DownloadTask):
        """复制链接"""
        from PyQt6.QtWidgets import QApplication
        from utils.redact import redact_url
        clipboard = QApplication.clipboard()
        clipboard.setText(task.url)
        # ISS-53: 完整记录被复制链接（脱敏后），避免截断丢失上下文
        logger.info(f"已复制链接: {redact_url(task.url)}")
    
    def _delete_task(self, task: DownloadTask):
        """删除任务"""
        if task.status == "downloading":
            msg = TR("msg_confirm_delete_downloading", filename=task.filename)
        else:
            msg = TR("msg_confirm_delete_task", filename=task.filename)
        
        reply = QMessageBox.question(
            self, TR("dialog_confirm_delete"),
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # 1. 告诉 Manager 删除任务（它会负责停止进程）
            self.task_removed.emit(task)
            
            # 2. 从 UI 队列立即移除，提升体验
            self.remove_task(task)
            
            # 3. 延迟清理临时文件 (3秒后)，确保文件句柄释放。
            #    ISS-38: 使用 threading.Timer 而非 QTimer.singleShot，
            #    彻底隔离 Qt 对象生命周期 — QTimer 的回调即使在
            #    parented 模式下仍可能在 widget 析构竞态中触发
            #    Qt6Core.dll STATUS_FAIL_FAST_EXCEPTION (0xc0000409)。
            #    摘取任务属性快照后传给模块级纯文件清理函数。
            logger.info(f"已删除任务: {task.filename} (临时文件将在3秒后自动清理)")
            from threading import Timer
            _cleanup_snapshot = {
                "filename": task.filename,
                "save_dir": task.save_dir,
                "status": task.status,
            }
            Timer(3.0, _cleanup_temp_files_for_task, args=(_cleanup_snapshot,)).start()
    
    def add_or_update_task(self, task: DownloadTask):
        """添加或更新任务控制句柄。

        显示状态优先由 :class:`TaskSnapshot` 驱动，避免 Qt queued signal
        晚处理同一个可变 ``DownloadTask`` 引用时读到最终状态而丢失进度。
        raw task 仍保存为暂停/删除/打开目录等操作的控制句柄，并在尚未收到
        snapshot 的初始阶段用于创建行。
        """
        task_id = str(id(task))
        filename = getattr(task, "filename", "")
        status = getattr(task, "status", "")
        logger.debug(
            "下载队列 UI 控制句柄刷新开始",
            event="ui_download_queue_task_handle_update_start",
            task_id=task_id,
            filename=filename,
            status=status,
            known_item=task_id in self.task_items,
        )

        if status == "removed" or getattr(task, "stop_reason", "") == "removed":
            return

        try:
            self.tasks[task_id] = task
            snapshot = self.task_snapshots.get(task_id)
            if snapshot is not None:
                # 已经进入 snapshot 显示通道后，不再用 raw task 重绘队列。
                # raw queued update 可能晚到并读取到同一可变对象的最终状态，
                # 覆盖已经到达 UI 线程的不可变进度快照。
                self._refresh_task_action_buttons()
                return

            if task_id in self.task_items:
                item = self.task_items[task_id]
                self._update_item(item, task)
            else:
                item = QTreeWidgetItem(self.tree)
                self.task_items[task_id] = item
                self._update_item(item, task)

            logger.debug(
                "下载队列 UI 列表项刷新完成",
                event="ui_download_queue_item_update_done",
                task_id=task_id,
                filename=filename,
                status=getattr(task, "status", status),
            )
            self._apply_status_filter()
            logger.debug(
                "下载队列 UI 状态过滤刷新完成",
                event="ui_download_queue_filter_done",
                task_id=task_id,
                filename=filename,
                status=getattr(task, "status", status),
            )
            self._refresh_task_action_buttons()
            logger.debug(
                "下载队列 UI 刷新完成",
                event="ui_download_queue_update_done",
                task_id=task_id,
                filename=filename,
                status=getattr(task, "status", status),
            )
        except RuntimeError as exc:
            logger.warning(
                f"下载队列 UI 刷新跳过: {exc}",
                event="ui_download_queue_update_runtime_error",
                task_id=task_id,
                filename=filename,
                status=getattr(task, "status", status),
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                f"下载队列 UI 刷新失败: {exc}",
                event="ui_download_queue_update_failed",
                task_id=task_id,
                filename=filename,
                status=getattr(task, "status", status),
                error_type=type(exc).__name__,
            )

    def add_or_update_snapshot(self, snapshot: TaskSnapshot):
        """使用不可变快照刷新下载队列显示状态。"""
        task_id = snapshot.task_id
        filename = snapshot.title or ""
        status = snapshot.status or ""
        logger.debug(
            "下载队列 UI 快照刷新开始",
            event="ui_download_queue_snapshot_update_start",
            task_id=task_id,
            filename=filename,
            status=status,
            progress=snapshot.progress,
            known_item=task_id in self.task_items,
        )

        if status == "removed" or (snapshot.stop_reason or "") == "removed":
            return

        try:
            self.task_snapshots[task_id] = snapshot
            if task_id in self.task_items:
                item = self.task_items[task_id]
            else:
                item = QTreeWidgetItem(self.tree)
                self.task_items[task_id] = item
            self._update_item_from_snapshot(item, snapshot)

            logger.debug(
                "下载队列 UI 快照列表项刷新完成",
                event="ui_download_queue_snapshot_item_update_done",
                task_id=task_id,
                filename=filename,
                status=status,
                progress=snapshot.progress,
            )
            self._apply_status_filter()
            self._refresh_task_action_buttons()
        except RuntimeError as exc:
            logger.warning(
                f"下载队列 UI 快照刷新跳过: {exc}",
                event="ui_download_queue_snapshot_update_runtime_error",
                task_id=task_id,
                filename=filename,
                status=status,
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                f"下载队列 UI 快照刷新失败: {exc}",
                event="ui_download_queue_snapshot_update_failed",
                task_id=task_id,
                filename=filename,
                status=status,
                error_type=type(exc).__name__,
            )
    
    def _update_item(self, item: QTreeWidgetItem, task: DownloadTask):
        """更新列表项"""
        filename = getattr(task, "filename", "") or ""
        status = getattr(task, "status", "") or ""
        save_path = getattr(task, "save_path", "") or getattr(task, "save_dir", "") or ""
        progress = getattr(task, "progress", -1)
        downloaded_size = getattr(task, "downloaded_size", "") or ""
        speed = getattr(task, "speed", "") or ""
        engine = getattr(task, "engine", "") or ""

        logger.debug(
            "下载队列 UI 列表项渲染开始",
            event="ui_download_queue_item_render_start",
            task_id=id(task),
            filename=filename,
            status=status,
        )

        # 文件名
        item.setText(0, filename)
        filename_tip = filename
        if save_path:
            filename_tip = f"{filename}\n{TR('label_save_path')}: {save_path}"
        item.setToolTip(0, filename_tip)
        
        # 状态
        try:
            status_text = task.get_status_display()
        except Exception:
            status_text = TR(f"status_{status}") if status else ""
        item.setText(1, status_text)
        
        # 进度
        try:
            progress_value = float(progress)
        except (TypeError, ValueError):
            progress_value = -1.0
        if progress_value >= 0:
            item.setText(2, f"{progress_value:.1f}%")
        else:
            # 直播流等无进度的任务
            item.setText(2, downloaded_size or TR("status_recording"))
        item.setToolTip(2, item.text(2))

        if progress_value > 0 and status == "downloading":
            logger.debug(
                "下载队列 UI 进度已应用",
                event="ui_download_queue_progress_apply",
                task_id=id(task),
                filename=filename,
                status=status,
                progress=progress_value,
            )
        
        # 速度
        item.setText(3, speed)
        item.setToolTip(3, speed)
        
        # 引擎
        item.setText(4, engine)
        item.setToolTip(4, engine)
        
        # 根据状态设置颜色
        default_brush = QBrush()
        for col in range(5):
            item.setForeground(col, default_brush)
        if status == "completed":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.darkGreen)
        elif status == "failed":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.red)
        elif status == "downloading":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.blue)
        elif status == "paused":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.darkYellow)

        logger.debug(
            "下载队列 UI 列表项渲染完成",
            event="ui_download_queue_item_render_done",
            task_id=id(task),
            filename=filename,
            status=status,
        )

    def _update_item_from_snapshot(self, item: QTreeWidgetItem, snapshot: TaskSnapshot):
        """用不可变快照更新列表项显示字段。"""
        task_id = snapshot.task_id
        filename = snapshot.title or ""
        status = snapshot.status or ""
        progress = snapshot.progress
        speed = getattr(snapshot, "speed_text", "") or ""
        if not speed and snapshot.speed_bps > 0:
            speed = f"{snapshot.speed_bps / (1024 * 1024):.2f} MB/s"
        save_path = getattr(snapshot, "save_dir", "") or ""
        downloaded_size = getattr(snapshot, "downloaded_text", "") or ""

        logger.debug(
            "下载队列 UI 快照列表项渲染开始",
            event="ui_download_queue_snapshot_item_render_start",
            task_id=task_id,
            filename=filename,
            status=status,
            progress=progress,
        )

        item.setText(0, filename)
        filename_tip = filename
        if save_path:
            filename_tip = f"{filename}\n{TR('label_save_path')}: {save_path}"
        item.setToolTip(0, filename_tip)

        item.setText(1, TR(f"status_{status}") if status else "")

        try:
            progress_value = float(progress)
        except (TypeError, ValueError):
            progress_value = -1.0
        if progress_value >= 0:
            item.setText(2, f"{progress_value:.1f}%")
        else:
            item.setText(2, downloaded_size or TR("status_recording"))
        item.setToolTip(2, item.text(2))

        if progress_value > 0 and status == "downloading":
            logger.debug(
                "下载队列 UI 快照进度已应用",
                event="ui_download_queue_snapshot_progress_apply",
                task_id=task_id,
                filename=filename,
                status=status,
                progress=progress_value,
            )

        item.setText(3, speed)
        item.setToolTip(3, speed)
        item.setText(4, snapshot.engine or "")
        item.setToolTip(4, snapshot.engine or "")

        default_brush = QBrush()
        for col in range(5):
            item.setForeground(col, default_brush)
        if status == "completed":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.darkGreen)
        elif status == "failed":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.red)
        elif status == "downloading":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.blue)
        elif status == "paused":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.darkYellow)

        logger.debug(
            "下载队列 UI 快照列表项渲染完成",
            event="ui_download_queue_snapshot_item_render_done",
            task_id=task_id,
            filename=filename,
            status=status,
            progress=progress_value,
        )
    
    def remove_task(self, task: DownloadTask):
        """移除任务"""
        task_id = str(id(task))
        if task_id in self.task_items:
            item = self.task_items[task_id]
            index = self.tree.indexOfTopLevelItem(item)
            self.tree.takeTopLevelItem(index)
            del self.task_items[task_id]
        if task_id in self.tasks:
            del self.tasks[task_id]
        if task_id in self.task_snapshots:
            del self.task_snapshots[task_id]
        if self._selected_task_id == task_id:
            self._selected_task_id = None
        self._refresh_task_action_buttons()
    
    def _on_pause_all(self):
        """暂停所有任务"""
        logger.info("用户请求暂停所有任务")
        for task in self.tasks.values():
            if task.status == "downloading":
                self.task_paused.emit(task)
    
    def _on_clear_completed(self):
        """Clear completed tasks from queue without deleting local files."""
        to_remove = []
        for task in self.tasks.values():
            if task and task.status == "completed":
                to_remove.append(task)

        for task in to_remove:
            # Sync DownloadManager state first, then remove from UI.
            self.task_removed.emit(task)
            self.remove_task(task)

        logger.info(f"已清除 {len(to_remove)} 个已完成任务")

    def clear(self):
        """清空列表"""
        self.tree.clear()
        self.task_items.clear()
        self.tasks.clear()
        self.task_snapshots.clear()
        if hasattr(self, "status_filter"):
            self.status_filter.setCurrentIndex(0)

    def _apply_status_filter(self):
        """按状态过滤队列显示"""
        if not hasattr(self, "status_filter"):
            return
        
        selected_text = self.status_filter.currentText()
        if not selected_text or selected_text == TR("status_all"):
            target_status = "all"
        else:
            # 根据翻译反查状态键
            mapping = {
                TR("status_downloading"): "downloading",
                TR("status_waiting"): "waiting",
                TR("status_paused"): "paused",
                TR("status_failed"): "failed",
                TR("status_completed"): "completed",
            }
            target_status = mapping.get(selected_text, "all")

        snapshots = getattr(self, "task_snapshots", {})
        for task_id, item in self.task_items.items():
            snapshot = snapshots.get(task_id)
            if snapshot is not None:
                visible = (target_status == "all" or snapshot.status == target_status)
            else:
                task = self.tasks.get(task_id)
                if not task:
                    continue
                visible = (target_status == "all" or task.status == target_status)
            item.setHidden(not visible)
        self._refresh_task_action_buttons()

    def _on_sort_by_status(self):
        """按状态排序（下载中/等待/暂停/失败/完成）"""
        snapshots = getattr(self, "task_snapshots", {})

        def status_rank(t: DownloadTask) -> int:
            mapping = {
                "downloading": 0,
                "waiting": 1,
                "paused": 2,
                "failed": 3,
                "completed": 4
            }
            task_id = str(id(t))
            snapshot = snapshots.get(task_id)
            status = snapshot.status if snapshot is not None else getattr(t, "status", "")
            return mapping.get(status, 99)

        tasks_sorted = sorted(self.tasks.values(), key=lambda t: (status_rank(t), t.created_at))
        self._rebuild_tree(tasks_sorted)
        logger.info("已按状态排序")

    def _rebuild_tree(self, tasks_list):
        """根据任务列表重建树"""
        self.tree.clear()
        self.task_items.clear()
        self._selected_task_id = None
        snapshots = getattr(self, "task_snapshots", {})

        for task in tasks_list:
            task_id = str(id(task))
            item = QTreeWidgetItem(self.tree)
            self.task_items[task_id] = item
            snapshot = snapshots.get(task_id)
            if snapshot is not None:
                self._update_item_from_snapshot(item, snapshot)
            else:
                self._update_item(item, task)
        self._refresh_task_action_buttons()

    def _on_batch_import(self):
        """批量导入 URL 列表"""
        text, ok = QInputDialog.getMultiLineText(
            self,
            TR("dialog_batch_import"),
            TR("msg_batch_import_hint")
        )
        if not ok or not text.strip():
            return

        urls = [line.strip() for line in text.splitlines() if line.strip()]
        if not urls:
            return

        # 仅支持 http(s)/magnet
        valid_urls = [u for u in urls if u.startswith("http://") or u.startswith("https://") or u.startswith("magnet:")]
        if not valid_urls:
            QMessageBox.warning(self, TR("dialog_no_valid_urls"), TR("msg_no_valid_urls"))
            return

        self.task_batch_imported.emit(valid_urls)
