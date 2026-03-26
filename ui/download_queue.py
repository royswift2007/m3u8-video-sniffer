"""
Download queue panel for displaying active, completed, and failed downloads
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QTreeWidget,
                             QTreeWidgetItem, QPushButton, QHBoxLayout,
                             QProgressBar, QHeaderView, QMenu, QMessageBox,
                             QComboBox, QFrame)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QAction, QCursor
from core.task_model import DownloadTask
from utils.logger import logger
import subprocess
import os


class DownloadQueuePanel(QWidget):
    """下载队列面板"""
    
    # 信号
    task_paused = pyqtSignal(DownloadTask)
    task_resumed = pyqtSignal(DownloadTask)
    task_cancelled = pyqtSignal(DownloadTask)
    task_retried = pyqtSignal(DownloadTask)
    task_removed = pyqtSignal(DownloadTask)  # 新增：彻底移除任务
    task_batch_imported = pyqtSignal(list)
    
    def __init__(self):
        super().__init__()
        self.task_items = {}  # task_id -> QTreeWidgetItem
        self.tasks = {}  # task_id -> DownloadTask
        self._selected_task_id = None
        self._init_ui()
    
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

        title = QLabel("下载队列")
        title.setObjectName("section_title")
        title_block.addWidget(title)

        intro = QLabel("查看任务状态、下载速度和执行引擎。")
        intro.setObjectName("panel_intro")
        title_block.addWidget(intro)

        title_layout.addLayout(title_block)

        title_layout.addStretch()

        # 状态过滤
        self.status_filter = QComboBox()
        self.status_filter.addItems(["全部状态", "下载中", "等待中", "已暂停", "失败", "已完成"])
        self.status_filter.currentIndexChanged.connect(self._apply_status_filter)
        title_layout.addWidget(self.status_filter)

        panel_layout.addLayout(title_layout)
        
        # 下载列表
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件名", "状态", "进度", "速度", "引擎"])
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 120)
        self.tree.setColumnWidth(2, 120)
        self.tree.setColumnWidth(3, 100)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame) # 去除边框
        # 样式统一由全局样式控制
        
        # 表头样式交由全局样式统一管理
        header = self.tree.header()
        
        # 启用右键菜单
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        
        panel_layout.addWidget(self.tree)
        
        # 控制按钮
        btn_layout = QHBoxLayout()
        
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setObjectName("queue_pause_button")
        self.pause_btn.clicked.connect(self._on_pause_selected)
        btn_layout.addWidget(self.pause_btn)
        
        self.resume_btn = QPushButton("继续")
        self.resume_btn.setObjectName("queue_resume_button")
        self.resume_btn.clicked.connect(self._on_resume_selected)
        btn_layout.addWidget(self.resume_btn)
        
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("queue_stop_button")
        self.stop_btn.clicked.connect(self._on_stop_selected)
        btn_layout.addWidget(self.stop_btn)
        
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setObjectName("queue_delete_button")
        self.delete_btn.clicked.connect(self._on_delete_selected)
        btn_layout.addWidget(self.delete_btn)
        
        self.retry_btn = QPushButton("重试")
        self.retry_btn.setObjectName("queue_retry_button")
        self.retry_btn.clicked.connect(self._on_retry_selected)
        btn_layout.addWidget(self.retry_btn)
        
        self.open_btn = QPushButton("打开位置")
        self.open_btn.setObjectName("queue_open_button")
        self.open_btn.clicked.connect(self._on_open_selected)
        btn_layout.addWidget(self.open_btn)
        
        pause_all_btn = QPushButton("暂停全部")
        pause_all_btn.setObjectName("queue_pauseall_button")
        pause_all_btn.clicked.connect(self._on_pause_all)
        btn_layout.addWidget(pause_all_btn)
        
        clear_completed_btn = QPushButton("清除已完成")
        clear_completed_btn.setObjectName("queue_clear_button")
        clear_completed_btn.clicked.connect(self._on_clear_completed)
        btn_layout.addWidget(clear_completed_btn)
        
        sort_status_btn = QPushButton("按状态排序")
        sort_status_btn.setObjectName("queue_sort_button")
        sort_status_btn.clicked.connect(self._on_sort_by_status)
        btn_layout.addWidget(sort_status_btn)
        
        import_btn = QPushButton("批量导入")
        import_btn.setObjectName("queue_import_button")
        import_btn.clicked.connect(self._on_batch_import)
        btn_layout.addWidget(import_btn)
        
        btn_layout.addStretch()
        panel_layout.addLayout(btn_layout)
        layout.addWidget(panel_card)
        self._refresh_task_action_buttons()
    
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
            pause_action = QAction("⏸️ 暂停", self)
            pause_action.triggered.connect(lambda: self._pause_task(task))
            menu.addAction(pause_action)
            
            stop_action = QAction("⏹️ 停止", self)
            stop_action.triggered.connect(lambda: self._stop_task(task))
            menu.addAction(stop_action)
            
        elif task.status == "paused":
            resume_action = QAction("▶️ 继续", self)
            resume_action.triggered.connect(lambda: self._resume_task(task))
            menu.addAction(resume_action)
            
        elif task.status == "failed":
            retry_action = QAction("🔄 重试", self)
            retry_action.triggered.connect(lambda: self._retry_task(task))
            menu.addAction(retry_action)
            
        elif task.status == "completed":
            open_action = QAction("📂 打开文件位置", self)
            open_action.triggered.connect(lambda: self._open_file_location(task))
            menu.addAction(open_action)
            
            play_action = QAction("▶️ 播放", self)
            play_action.triggered.connect(lambda: self._play_file(task))
            menu.addAction(play_action)
        
        menu.addSeparator()
        
        # 通用菜单项
        copy_url_action = QAction("📋 复制链接", self)
        copy_url_action.triggered.connect(lambda: self._copy_url(task))
        menu.addAction(copy_url_action)
        
        # 删除任务（所有状态都可以删除）
        delete_action = QAction("🗑️ 删除任务", self)
        delete_action.triggered.connect(lambda: self._delete_task(task))
        menu.addAction(delete_action)
        
        # 显示菜单
        menu.exec(QCursor.pos())

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
            self, "确认停止",
            f"确定要停止下载 \"{task.filename}\" 吗？",
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
            QMessageBox.warning(self, "文件未找到", f"未找到文件: {task.filename}")
    
    def _copy_url(self, task: DownloadTask):
        """复制链接"""
        from PyQt6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        clipboard.setText(task.url)
        logger.info(f"已复制链接: {task.url[:50]}...")
    
    def _delete_task(self, task: DownloadTask):
        """删除任务"""
        msg = f"确定要删除任务 \"{task.filename}\" 吗？\n"
        if task.status == "downloading":
            msg += "(将停止下载并清理临时文件)"
        else:
            msg += "(不会删除已下载的文件)"
        
        reply = QMessageBox.question(
            self, "确认删除",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # 1. 告诉 Manager 删除任务（它会负责停止进程）
            self.task_removed.emit(task)
            
            # 2. 从 UI 队列立即移除，提升体验
            self.remove_task(task)
            
            # 3. 延迟清理临时文件 (3秒后)，确保文件句柄释放
            # 借鉴 JJH_download.pyw 的成熟方案
            from PyQt6.QtCore import QTimer
            logger.info(f"已删除任务: {task.filename} (临时文件将在3秒后自动清理)")
            
            # 使用 lambda 捕获 task 对象
            QTimer.singleShot(3000, lambda: self._cleanup_temp_files_safe(task))
            
    def _cleanup_temp_files_safe(self, task: DownloadTask):
        """安全的清理临时文件（带异常处理）"""
        try:
            self._cleanup_temp_files(task)
        except Exception as e:
            logger.warning(f"延迟清理临时文件失败: {e}")

    def _cleanup_temp_files(self, task: DownloadTask):
        """清理任务的临时文件"""
        import shutil
        from pathlib import Path
        from utils.config_manager import config
        
        try:
            cleaned_count = 0
            # 1. 清理 N_m3u8DL-RE 临时目录
            temp_dir = Path(config.get("temp_dir", "temp")) / "n_m3u8dl"
            if temp_dir.exists():
                # 查找与任务相关的临时文件
                for item in temp_dir.iterdir():
                    if task.filename in item.name:
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
            download_dir = Path(task.save_dir)
            if download_dir.exists():
                # 遍历目录而不是使用 glob，避免此时文件名中包含 [] 等特殊字符导致 glob 失败
                for item in download_dir.iterdir():
                    if not item.is_file():
                        continue
                        
                    name = item.name
                    # 只有当文件名包含任务名时才处理 (避免误删)
                    # 注意：task.filename 应该是已经去除非法字符的
                    if task.filename not in name:
                        continue
                        
                    is_temp = False
                    lower_name = name.lower()
                    
                    # 检查后缀
                    if lower_name.endswith(('.part', '.ytdl', '.temp', '.download', '.aria2')):
                        is_temp = True
                    # 检查 yt-dlp 中间临时文件格式 (如 video.f137.mp4.part)
                    elif '.f' in lower_name and ('.mp4.part' in lower_name or '.webm.part' in lower_name or '.m4a.part' in lower_name):
                        is_temp = True
                    # 关键修复：如果任务未完成（下载中/暂停/失败），删除预分配的主文件 (针对 Aria2)
                    elif name == task.filename and task.status != 'completed':
                        is_temp = True
                    
                    if is_temp:
                        try:
                            item.unlink(missing_ok=True)
                            cleaned_count += 1
                            logger.debug(f"已删除下载临时文件: {name}")
                        except Exception as e:
                            logger.warning(f"无法删除文件 {name}: {e}")
            
            logger.info(f"已清理 {cleaned_count} 个临时文件: {task.filename}")
        except Exception as e:
            logger.error(f"清理临时文件失败: {e}")
    
    def add_or_update_task(self, task: DownloadTask):
        """添加或更新任务"""
        task_id = id(task)
        
        # 保存任务引用
        self.tasks[task_id] = task
        
        if task_id in self.task_items:
            # 更新现有任务
            item = self.task_items[task_id]
            self._update_item(item, task)
        else:
            # 添加新任务
            item = QTreeWidgetItem(self.tree)
            self.task_items[task_id] = item
            self._update_item(item, task)

        self._apply_status_filter()
        self._refresh_task_action_buttons()
    
    def _update_item(self, item: QTreeWidgetItem, task: DownloadTask):
        """更新列表项"""
        # 文件名
        item.setText(0, task.filename)
        
        # 状态
        item.setText(1, task.get_status_display())
        
        # 进度
        if task.progress >= 0:
            item.setText(2, f"{task.progress:.1f}%")
        else:
            # 直播流等无进度的任务
            item.setText(2, task.downloaded_size or "录制中...")
        
        # 速度
        item.setText(3, task.speed)
        
        # 引擎
        item.setText(4, task.engine)
        
        # 根据状态设置颜色
        if task.status == "completed":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.darkGreen)
        elif task.status == "failed":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.red)
        elif task.status == "downloading":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.blue)
        elif task.status == "paused":
            for col in range(5):
                item.setForeground(col, Qt.GlobalColor.darkYellow)
    
    def remove_task(self, task: DownloadTask):
        """移除任务"""
        task_id = id(task)
        if task_id in self.task_items:
            item = self.task_items[task_id]
            index = self.tree.indexOfTopLevelItem(item)
            self.tree.takeTopLevelItem(index)
            del self.task_items[task_id]
        if task_id in self.tasks:
            del self.tasks[task_id]
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
        if hasattr(self, "status_filter"):
            self.status_filter.setCurrentIndex(0)

    def _apply_status_filter(self):
        """按状态过滤队列显示"""
        if not hasattr(self, "status_filter"):
            return
        selected = self.status_filter.currentText()
        mapping = {
            "下载中": "downloading",
            "等待中": "waiting",
            "已暂停": "paused",
            "失败": "failed",
            "已完成": "completed",
        }
        for task_id, item in self.task_items.items():
            task = self.tasks.get(task_id)
            if not task:
                continue
            visible = True
            if selected != "全部状态":
                visible = task.status == mapping.get(selected)
            item.setHidden(not visible)
        self._refresh_task_action_buttons()

    def _on_sort_by_status(self):
        """按状态排序（下载中/等待/暂停/失败/完成）"""
        def status_rank(t: DownloadTask) -> int:
            mapping = {
                "downloading": 0,
                "waiting": 1,
                "paused": 2,
                "failed": 3,
                "completed": 4
            }
            return mapping.get(t.status, 99)

        tasks_sorted = sorted(self.tasks.values(), key=lambda t: (status_rank(t), t.created_at))
        self._rebuild_tree(tasks_sorted)
        logger.info("已按状态排序")

    def _rebuild_tree(self, tasks_list):
        """根据任务列表重建树"""
        self.tree.clear()
        self.task_items.clear()
        self._selected_task_id = None

        for task in tasks_list:
            task_id = id(task)
            item = QTreeWidgetItem(self.tree)
            self.task_items[task_id] = item
            self._update_item(item, task)
        self._refresh_task_action_buttons()

    def _on_batch_import(self):
        """批量导入 URL 列表"""
        from PyQt6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getMultiLineText(
            self,
            "批量导入",
            "请输入下载链接（每行一个）："
        )
        if not ok or not text.strip():
            return

        urls = [line.strip() for line in text.splitlines() if line.strip()]
        if not urls:
            return

        # 仅支持 http(s)/magnet
        valid_urls = [u for u in urls if u.startswith("http://") or u.startswith("https://") or u.startswith("magnet:")]
        if not valid_urls:
            QMessageBox.warning(self, "无有效链接", "未检测到可用链接（仅支持 http/https/magnet）")
            return

        self.task_batch_imported.emit(valid_urls)

