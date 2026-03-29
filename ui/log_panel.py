"""
Log panel for displaying real-time application logs
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout, QLabel, QFrame
from PyQt6.QtCore import pyqtSlot, pyqtSignal, QObject
from PyQt6.QtGui import QTextCursor, QColor
from core.task_model import DownloadTask # Although not used here, checking consistency
from utils.logger import logger
from utils.i18n import i18n, TR


class LogPanel(QWidget):
    """日志显示面板"""
    
    # 信号：用于线程安全的日志添加
    log_signal = pyqtSignal(str, str)
    
    def __init__(self):
        super().__init__()
        self._init_ui()
        self.retranslate_ui()
        
        # 连接信号
        self.log_signal.connect(self._append_log_internal)
        
        self._setup_logger_handler()
    
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
 
        self.intro_label = QLabel("")
        self.intro_label.setObjectName("panel_intro")
        header_layout.addWidget(self.intro_label)
 
        panel_layout.addLayout(header_layout)
# 日志文本框
        self.log_text = QTextEdit()
        self.log_text.setObjectName("download_center_log_text")
        self.log_text.setReadOnly(True)
        
        # 底部工具栏
        toolbar = QHBoxLayout()
        
        self.clear_btn = QPushButton("")
        self.clear_btn.setObjectName("secondary_button")
        self.clear_btn.clicked.connect(self.clear_logs)
        
        self.auto_scroll_btn = QPushButton("")
        self.auto_scroll_btn.setObjectName("secondary_button")
        self.auto_scroll_btn.setCheckable(True)
        self.auto_scroll_btn.setChecked(True)
        
        toolbar.addWidget(self.clear_btn)
        toolbar.addWidget(self.auto_scroll_btn)
        toolbar.addStretch()
        
        panel_layout.addWidget(self.log_text)
        panel_layout.addLayout(toolbar)
        layout.addWidget(panel_card)

    def retranslate_ui(self):
        """翻译 UI 文字"""
        self.intro_label.setText(TR("intro_log_panel"))
        self.log_text.setPlaceholderText(TR("placeholder_log_panel"))
        self.clear_btn.setText(TR("btn_clear_logs"))
        self.auto_scroll_btn.setText(TR("btn_auto_scroll"))

        # 尝试翻译现有的静态关键日志文本，满足用户界面语言切换时的体验预期
        from utils.i18n_data import TRANSLATIONS
        from utils.i18n import i18n
        current_lang = i18n.current_language
        
        doc = self.log_text.document()
        
        # 只取主要的静态启动/关闭日志做界面热替换
        static_keys = ["log_ready", "log_closing_dl_mgr", "log_dl_mgr_closed", "log_browser_ready"]
        
        for lang, trans in TRANSLATIONS.items():
            if lang == current_lang:
                continue
            for key in static_keys:
                old_str = trans.get(key, "")
                new_str = TRANSLATIONS.get(current_lang, {}).get(key, "")
                if old_str and new_str and old_str != new_str:
                    cursor = doc.find(old_str)
                    while not cursor.isNull():
                        cursor.insertText(new_str)
                        cursor = doc.find(old_str, cursor)
    
    def _setup_logger_handler(self):
        """设置日志处理器"""
        # 创建一个自定义的日志处理器
        import logging
        
        class QtLogHandler(logging.Handler):
            def __init__(self, widget):
                super().__init__()
                self.widget = widget
                
                # 关键日志关键词（显示在 UI）- 增加英文匹配项以防英文模式日志被吞
                self.key_patterns = [
                    '任务已加入队列', '任务已添加', '已添加下载', 'Added',
                    '开始下载', '开始录制', '下载成功', '下载完成', 'Downloading', 'successfully', 'completed', 'started',
                    '下载失败', '任务失败', '任务完成', '任务取消', 'failed', 'cancelled', 'canceled',
                    '任务已取消', '任务已暂停', '回退引擎', 'Paused', 'Fallback',
                    '引擎已加载', '应用启动', '应用关闭', 'loaded',
                    '资源已添加', '已发现资源', '收到下载请求', 'Resource', 'request',
                    '收到猫爪', '并发数调整', '限速已设置', 'CatCatch', 'Concurrent', 'Speed limit',
                    '线程数已更新', '重试次数已更新', 'Thread', 'Retry',
                    '下载路径已更新', '配置已加载', 'Download path', 'Config', 'Application',
                    '[OK]', '[FAILED]', '[RETRY]',
                    'ERROR', 'WARNING', 'CRITICAL',
                ]
                
                # 排除的关键词（不显示在 UI，但记录到文件）
                self.exclude_patterns = [
                    '%', '进度', 'progress',
                    'MiB/s', 'KiB/s', 'MB/s', 'KB/s',
                    'Vid ', 'Aud ',  # N_m3u8DL-RE 进度输出
                    '[download]',  # yt-dlp 进度输出
                    'SPD:', 'DL:', 'SIZE:',  # Aria2 进度输出
                    'Written',  # Streamlink 进度输出
                ]
            
            def emit(self, record):
                try:
                    msg = self.format(record)
                    
                    # 检查是否应该排除（进度信息）
                    msg_lower = msg.lower()
                    for pattern in self.exclude_patterns:
                        if pattern.lower() in msg_lower:
                            return  # 跳过进度类日志
                    
                    # 只显示关键日志 或 WARNING/ERROR 级别
                    is_key_log = any(kw in msg for kw in self.key_patterns)
                    is_important_level = record.levelno >= logging.WARNING
                    
                    if is_key_log or is_important_level:
                        # 使用信号发射（线程安全）
                        self.widget.log_signal.emit(msg, record.levelname)
                except Exception:
                    # 忽略日志处理器中的异常，避免递归
                    pass
        
        # 添加处理器到全局 logger
        handler = QtLogHandler(self)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logging.getLogger().addHandler(handler)
    
    
    @pyqtSlot(str, str)
    def _append_log_internal(self, message: str, level: str = "INFO"):
        """添加日志（内部方法，由信号调用，线程安全）"""
        try:
            # 根据级别设置颜色
            color_map = {
                'DEBUG': '#888888',
                'INFO': '#000000',
                'WARNING': '#FF8C00',
                'ERROR': '#FF0000',
                'CRITICAL': '#8B0000'
            }
            
            color = color_map.get(level, '#000000')
            self.log_text.setTextColor(QColor(color))
            self.log_text.append(message)
            
            # 自动滚动
            if self.auto_scroll_btn.isChecked():
                self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            # 忽略 UI 更新异常
            pass
    
    def clear_logs(self):
        """清空日志"""
        self.log_text.clear()
        logger.info("日志已清空")
