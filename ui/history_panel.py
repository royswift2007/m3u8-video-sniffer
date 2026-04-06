"""
History panel for managing download history
"""
import json
from pathlib import Path
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
                             QPushButton, QHBoxLayout, QHeaderView, QMessageBox, QMenu,
                             QLabel, QFrame)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from utils.json_store import backup_path_for, corrupt_path_for, write_json_atomic
from utils.logger import logger
from utils.i18n import i18n, TR


class HistoryPanel(QWidget):
    """下载历史面板"""
    
    record_download_requested = pyqtSignal(dict)
    record_deleted = pyqtSignal(dict)
    
    def __init__(self):
        super().__init__()
        self.history_file = Path.home() / ".m3u8sniffer" / "history.json"
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history_data = []  # 存储完整的历史记录数据
        self._init_ui()
        self.retranslate_ui()
        self._load_history()
    
    def _init_ui(self):
        """初始化 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        panel_card = QFrame()
        panel_card.setObjectName("panel_card")
        panel_layout = QVBoxLayout(panel_card)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(8)

        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)

        self.title_label = QLabel("")
        self.title_label.setObjectName("section_title")
        header_layout.addWidget(self.title_label)

        self.intro_label = QLabel("")
        self.intro_label.setObjectName("panel_intro")
        header_layout.addWidget(self.intro_label)

        panel_layout.addLayout(header_layout)
        
        # 历史记录表格
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(5)
        
        # 设置列宽
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        # 启用右键菜单
        self.history_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_table.customContextMenuRequested.connect(self._show_context_menu)
        
        # 工具栏
        toolbar = QHBoxLayout()
        
        self.refresh_btn = QPushButton("")
        self.refresh_btn.setObjectName("secondary_button")
        self.refresh_btn.clicked.connect(self._load_history)
        
        self.clear_btn = QPushButton("")
        self.clear_btn.setObjectName("secondary_button")
        self.clear_btn.clicked.connect(self._clear_history)
        
        toolbar.addWidget(self.refresh_btn)
        toolbar.addWidget(self.clear_btn)
        toolbar.addStretch()
        
        panel_layout.addWidget(self.history_table)
        panel_layout.addLayout(toolbar)
        layout.addWidget(panel_card)

    def retranslate_ui(self):
        """翻译 UI 文字"""
        self.title_label.setText(TR("tab_history"))
        self.intro_label.setText(TR("intro_history_panel"))
        
        self.refresh_btn.setText(TR("btn_refresh"))
        self.clear_btn.setText(TR("btn_clear_history"))
        
        self.history_table.setHorizontalHeaderLabels([
            TR("col_filename"), "URL", TR("col_status"), TR("col_time"), TR("col_size")
        ])
        
        # 刷新表格中翻译的状态列
        for row in range(self.history_table.rowCount()):
            status_item = self.history_table.item(row, 2)
            if status_item:
                raw_status = status_item.data(Qt.ItemDataRole.UserRole) or status_item.text()
                # 兼容旧数据中的中文状态
                status_key = f"status_{raw_status.lower()}"
                if raw_status == "已完成": status_key = "status_completed"
                elif raw_status == "失败": status_key = "status_failed"
                
                self.history_table.item(row, 2).setText(TR(status_key))
    
    def _show_context_menu(self, position):
        """显示右键菜单"""
        # 获取当前选中的行
        current_row = self.history_table.currentRow()
        if current_row < 0:
            return
        
        # 创建菜单
        menu = QMenu(self)
        
        retry_action = QAction(f"🔄 {TR('btn_redownload')}", self)
        retry_action.triggered.connect(lambda: self._retry_download(current_row))
        menu.addAction(retry_action)
        
        open_action = QAction(f"📂 {TR('btn_open_file_location')}", self)
        open_action.triggered.connect(lambda: self._open_file_location(current_row))
        menu.addAction(open_action)
        
        view_log_action = QAction(f"📄 {TR('btn_view_related_log')}", self)
        view_log_action.triggered.connect(lambda: self._view_related_log(current_row))
        menu.addAction(view_log_action)
        
        delete_action = QAction(f"🗑️ {TR('btn_delete_from_history')}", self)
        delete_action.triggered.connect(lambda: self._delete_record(current_row))
        menu.addAction(delete_action)
        
        menu.addSeparator()
        
        copy_filename_action = QAction(TR("btn_copy_filename"), self)
        copy_filename_action.triggered.connect(lambda: self._copy_cell_data(current_row, 0))
        menu.addAction(copy_filename_action)
        
        copy_url_action = QAction(TR("btn_copy_url"), self)
        copy_url_action.triggered.connect(lambda: self._copy_full_url(current_row))
        menu.addAction(copy_url_action)
        
        menu.addSeparator()
        
        copy_row_action = QAction(TR("btn_copy_row"), self)
        copy_row_action.triggered.connect(lambda: self._copy_row_data(current_row))
        menu.addAction(copy_row_action)
        
        # 显示菜单
        menu.exec(self.history_table.viewport().mapToGlobal(position))
    
    def _copy_cell_data(self, row: int, col: int):
        """复制单元格数据"""
        from PyQt6.QtWidgets import QApplication
        item = self.history_table.item(row, col)
        if item:
            QApplication.clipboard().setText(item.text())
            logger.info(f"已复制: {item.text()}")
    
    def _copy_full_url(self, row: int):
        """复制完整 URL（不截断）"""
        from PyQt6.QtWidgets import QApplication
        if 0 <= row < len(self.history_data):
            url = self.history_data[row].get('url', '')
            QApplication.clipboard().setText(url)
            logger.info(f"已复制完整 URL: {url[:50]}...")
    
    def _copy_row_data(self, row: int):
        """复制整行数据"""
        from PyQt6.QtWidgets import QApplication
        if 0 <= row < len(self.history_data):
            record = self.history_data[row]
            text = f"文件名: {record.get('filename', 'N/A')}\nURL: {record.get('url', 'N/A')}\n状态: {record.get('status', 'N/A')}\n时间: {record.get('completed_at', 'N/A')}\n大小: {record.get('size', 'N/A')}"
            QApplication.clipboard().setText(text)
            logger.info("已复制整行数据")
    
    def _load_history(self):
        """加载历史记录"""
        self.history_table.setRowCount(0)
        self.history_data = []  # 清空并重新加载
        
        backup_file = self._history_backup_file()
        if not self.history_file.exists() and not backup_file.exists():
            return
        
        try:
            history, recovered_from_backup = self._load_history_entries()
            
            # 按时间倒序排列
            history.sort(key=lambda x: x.get('completed_at', ''), reverse=True)
            
            # 保存完整数据并显示
            self.history_data = history
            for record in history:
                self._add_history_row(record)
            if recovered_from_backup or not backup_file.exists():
                self._save_history_entries(history)
                
        except Exception as e:
            logger.error(f"加载历史记录失败: {e}")

    def _history_backup_file(self) -> Path:
        """Return the history backup path."""
        return backup_path_for(self.history_file)

    def _load_history_entries(self):
        """Load history entries from the primary file or its backup."""
        backup_file = self._history_backup_file()
        primary_error: Exception | None = None

        if self.history_file.exists():
            try:
                return self._load_json_file(self.history_file), False
            except json.JSONDecodeError as exc:
                primary_error = exc
                logger.error(f"Primary history is corrupted: {exc}")
                self._quarantine_corrupted_history()
            except Exception as exc:
                primary_error = exc
                logger.warning(f"Primary history read failed, trying backup: {exc}")

        if backup_file.exists():
            logger.warning(f"Recovered history from backup: {backup_file}")
            return self._load_json_file(backup_file), True

        if primary_error is not None:
            raise primary_error
        raise FileNotFoundError(self.history_file)

    def _load_json_file(self, path: Path):
        """Load a JSON array from disk."""
        with open(path, 'r', encoding='utf-8') as handle:
            history = json.load(handle)
        if isinstance(history, list):
            return history
        raise ValueError("History file top-level value must be an array")

    def _save_history_entries(self, history: list):
        """Persist history entries with atomic replace and backup refresh."""
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.history_file, history, indent=2, ensure_ascii=False)

    def _quarantine_corrupted_history(self):
        """Move a corrupted history file aside before recovery."""
        try:
            if self.history_file.exists():
                self.history_file.replace(corrupt_path_for(self.history_file))
        except Exception as exc:
            logger.error(f"Failed to quarantine corrupted history: {exc}")

    def _add_history_row(self, record: dict):
        """添加历史记录行"""
        row = self.history_table.rowCount()
        self.history_table.insertRow(row)
        
        # 文件名
        self.history_table.setItem(row, 0, QTableWidgetItem(record.get('filename', 'N/A')))
        
        # URL (截断显示)
        url = record.get('url', 'N/A')
        url_display = url[:50] + '...' if len(url) > 50 else url
        self.history_table.setItem(row, 1, QTableWidgetItem(url_display))
        
        # 状态
        status = record.get('status', 'unknown')
        status_key = f"status_{status.lower()}"
        # 兼容旧数据中的中文状态
        if status == "已完成": status_key = "status_completed"
        elif status == "失败": status_key = "status_failed"
        
        status_item = QTableWidgetItem(TR(status_key))
        status_item.setData(Qt.ItemDataRole.UserRole, status) # 存储原始状态用于翻译
        if status in ['completed', '已完成']:
            status_item.setForeground(Qt.GlobalColor.darkGreen)
        elif status in ['failed', '失败']:
            status_item.setForeground(Qt.GlobalColor.red)
        self.history_table.setItem(row, 2, status_item)
        
        # 完成时间
        completed_at = record.get('completed_at', 'N/A')
        self.history_table.setItem(row, 3, QTableWidgetItem(completed_at))
        
        # 大小
        size = record.get('size', 'N/A')
        self.history_table.setItem(row, 4, QTableWidgetItem(size))
    
    def add_record(
        self,
        filename: str,
        url: str,
        status: str,
        size: str = 'N/A',
        headers: dict = None,
        engine: str = '',
        save_dir: str = '',
        selected_variant: dict = None,
        master_url: str = None,
        media_url: str = None,
    ):
        """添加新的历史记录"""
        try:
            # 加载现有历史
            history = []
            backup_file = self._history_backup_file()
            if self.history_file.exists() or backup_file.exists():
                try:
                    history, _ = self._load_history_entries()
                except (json.JSONDecodeError, ValueError):
                    logger.warning("历史记录文件损坏，将创建新文件")
                    history = []
            
            # 添加新记录
            record = {
                'filename': filename,
                'url': url,
                'status': status,
                'size': size,
                'headers': headers or {},
                'engine': engine or '',
                'save_dir': save_dir or '',
                'selected_variant': selected_variant or None,
                'master_url': master_url or None,
                'media_url': media_url or None,
                'completed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            if headers and isinstance(headers, dict) and headers.get('_cookie_file'):
                record['cookie_file'] = headers.get('_cookie_file')
            history.append(record)
            
            # 原子写入历史及其备份
            self._save_history_entries(history)
            
            # 刷新显示
            self._load_history()
            
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")
    
    def _clear_history(self):
        """清空历史记录"""
        reply = QMessageBox.question(
            self,
            TR("dialog_confirm_clear"),
            TR("msg_confirm_clear_history"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            try:
                for path in (self.history_file, self._history_backup_file()):
                    if path.exists():
                        path.unlink()
                self.history_table.setRowCount(0)
                self.history_data = []
                logger.info("历史记录已清空")
            except Exception as e:
                logger.error(f"清空历史记录失败: {e}")

    def _retry_download(self, row: int):
        """重新下载"""
        if 0 <= row < len(self.history_data):
            record = self.history_data[row]
            self.record_download_requested.emit(record)
            logger.info(f"历史记录请求重新下载: {record.get('filename', '')}")

    def _delete_record(self, row: int):
        """从历史删除记录"""
        if not (0 <= row < len(self.history_data)):
            return

        record = self.history_data[row]
        reply = QMessageBox.question(
            self,
            TR("dialog_confirm_delete"),
            TR("msg_confirm_delete_history", filename=record.get('filename', 'N/A')),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.history_data.pop(row)
            self._save_history_entries(self.history_data)
            self._load_history()
            self.record_deleted.emit(record)
            logger.info("已从历史记录中删除")
        except Exception as e:
            logger.error(f"删除历史记录失败: {e}")

    def _open_file_location(self, row: int):
        """打开文件所在位置"""
        if not (0 <= row < len(self.history_data)):
            return

        record = self.history_data[row]
        filename = record.get('filename', '')
        save_dir = record.get('save_dir', '')
        if not filename or not save_dir:
            QMessageBox.warning(self, TR("dialog_cannot_open"), TR("msg_history_no_path"))
            return

        from pathlib import Path
        import subprocess
        import os

        file_path = Path(save_dir) / filename
        possible_files = list(Path(save_dir).glob(f"{filename}.*"))
        if possible_files:
            file_path = possible_files[0]

        try:
            if file_path.exists():
                subprocess.run(['explorer', '/select,', str(file_path)])
            else:
                os.startfile(save_dir)
        except Exception as e:
            logger.error(f"打开文件位置失败: {e}")

    def _view_related_log(self, row: int):
        """查看与历史记录相关的日志"""
        if not (0 <= row < len(self.history_data)):
            return

        record = self.history_data[row]
        filename = record.get('filename', '')
        if not filename:
            QMessageBox.warning(self, TR("dialog_cannot_view"), TR("msg_history_no_filename"))
            return

        import os
        import glob
        import subprocess
        from pathlib import Path

        logs_dir = Path(__file__).parent.parent / 'logs'
        if not logs_dir.exists():
            QMessageBox.warning(self, TR("dialog_cannot_view"), TR("msg_logs_dir_not_exists"))
            return

        log_files = sorted(glob.glob(str(logs_dir / '*.log')))
        if not log_files:
            QMessageBox.warning(self, TR("dialog_cannot_view"), TR("msg_no_log_files"))
            return

        latest_log = log_files[-1]
        try:
            subprocess.run(['notepad', latest_log])
            logger.info(f"已打开日志: {latest_log}")
        except Exception as e:
            logger.error(f"打开日志失败: {e}")
