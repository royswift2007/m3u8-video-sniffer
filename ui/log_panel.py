"""
Log panel for displaying real-time application logs
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout, QLabel, QFrame
from PyQt6.QtCore import pyqtSlot, pyqtSignal, QObject
from PyQt6.QtGui import QTextCursor, QColor
from utils.logger import logger


class LogPanel(QWidget):
    """日志显示面板"""
    
    # 信号：用于线程安全的日志添加
    log_signal = pyqtSignal(str, str)
    
    def __init__(self):
        super().__init__()
        self._init_ui()
        
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

        title = QLabel("运行日志")
        title.setObjectName("section_title")
        header_layout.addWidget(title)

        intro = QLabel("只显示关键事件和异常。")
        intro.setObjectName("panel_intro")
        header_layout.addWidget(intro)

        panel_layout.addLayout(header_layout)
        
        # 日志文本框
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("任务启动、失败重试和关键告警会显示在这里。")
        
        # 底部工具栏
        toolbar = QHBoxLayout()
        
        self.clear_btn = QPushButton("清空日志")
        self.clear_btn.setObjectName("secondary_button")
        self.clear_btn.clicked.connect(self.clear_logs)
        
        self.auto_scroll_btn = QPushButton("自动滚动")
        self.auto_scroll_btn.setObjectName("secondary_button")
        self.auto_scroll_btn.setCheckable(True)
        self.auto_scroll_btn.setChecked(True)
        
        toolbar.addWidget(self.clear_btn)
        toolbar.addWidget(self.auto_scroll_btn)
        toolbar.addStretch()
        
        panel_layout.addWidget(self.log_text)
        panel_layout.addLayout(toolbar)
        layout.addWidget(panel_card)
    
    def _setup_logger_handler(self):
        """设置日志处理器"""
        # 创建一个自定义的日志处理器
        import logging
        
        class QtLogHandler(logging.Handler):
            def __init__(self, widget):
                super().__init__()
                self.widget = widget
                
                # 关键日志关键词（显示在 UI）
                self.key_patterns = [
                    '任务已加入队列', '任务已添加', '已添加下载',
                    '开始下载', '开始录制', '下载成功', '下载完成',
                    '下载失败', '任务失败', '任务完成', '任务取消',
                    '任务已取消', '任务已暂停', '回退引擎',
                    '引擎已加载', '应用启动', '应用关闭',
                    '资源已添加', '已发现资源', '收到下载请求',
                    '收到猫爪', '并发数调整', '限速已设置',
                    '线程数已更新', '重试次数已更新',
                    '下载路径已更新', '配置已加载',
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
