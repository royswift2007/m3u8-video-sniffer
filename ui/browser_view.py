"""
Browser View - Playwright Controller
Controls the external Chrome window via PlaywrightDriver
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QLabel,
                             QHBoxLayout, QTextEdit, QFrame, QSizePolicy)
from PyQt6.QtCore import pyqtSignal, QUrl, Qt
from PyQt6.QtGui import QFont, QTextCursor, QTextOption
from core.m3u8_sniffer import M3U8Sniffer
from core.playwright_driver import PlaywrightDriver
from utils.logger import logger
from utils.i18n import i18n, TR


class _LogTextEdit(QTextEdit):
    """QTextEdit 子类：修复 Qt 样式表与 WidgetWidth 换行宽度的计算 bug。
    Qt 在 QTextEdit 应用了 border-radius / padding 样式后，会把多余的值计入
    内部 frame width，导致 document.textWidth 比实际 viewport 宽度小很多。
    这里每次 resizeEvent 都手动把 textWidth 同步为 viewport 宽度。
    """
    def resizeEvent(self, event):
        super().resizeEvent(event)
        vw = self.viewport().width()
        if vw > 0:
            self.document().setTextWidth(vw)

class BrowserView(QWidget):
    """浏览器控制视图 (Playwright 版)"""
    
    # 信号兼容
    resource_detected = pyqtSignal(str, dict, str)
    url_changed = pyqtSignal(QUrl)  
    load_finished = pyqtSignal(bool)
    format_found = pyqtSignal(str, dict, str, str)
    
    def __init__(self, sniffer: M3U8Sniffer):
        super().__init__()
        self.sniffer = sniffer
        self.driver = None
        self._browser_ready = False  # 浏览器是否已就绪
        self._pending_url = None     # 待导航的 URL（浏览器未就绪时缓存）
        
        self._init_ui()
        self.retranslate_ui()
        self._init_driver()
        
    def _init_ui(self):
        """初始化控制面板 UI - 左右布局"""
        layout = QHBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)
        # BrowserView 本身要撑满父容器
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # === 左侧：状态卡片 ===
        status_card = QFrame()
        status_card.setObjectName("card")
        status_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        card_layout = QVBoxLayout(status_card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)
        
        # 标题
        self.title_label = QLabel("")
        self.title_label.setObjectName("page_title")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        card_layout.addWidget(self.title_label)

        # 说明
        self.desc_label = QLabel("")
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.desc_label.setWordWrap(True)
        self.desc_label.setObjectName("muted_text")
        card_layout.addWidget(self.desc_label)

        self.status_chip = QLabel("")
        self.status_chip.setObjectName("status_chip")
        self.status_chip.setAlignment(Qt.AlignmentFlag.AlignLeft)
        card_layout.addWidget(self.status_chip)
        
        # 填充中间空间
        card_layout.addStretch()
        
        # 控制按钮
        btn_layout = QHBoxLayout()
        
        self.launch_btn = QPushButton("")
        self.launch_btn.setMinimumHeight(34)
        self.launch_btn.setObjectName("success_button")
        self.launch_btn.clicked.connect(self.start_browser)

        self.stop_btn = QPushButton("")
        self.stop_btn.setMinimumHeight(34)
        self.stop_btn.setObjectName("danger_button")
        self.stop_btn.clicked.connect(self.stop_browser)
        self.stop_btn.setEnabled(False)
        
        btn_layout.addWidget(self.launch_btn)
        btn_layout.addWidget(self.stop_btn)
        card_layout.addLayout(btn_layout)
        
        layout.addWidget(status_card, stretch=1)
        
        # === 右侧：日志区域 ===
        log_frame = QFrame()
        log_frame.setObjectName("dark_card")
        log_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(8)
        
        self.log_label = QLabel("")
        self.log_label.setObjectName("log_label")
        log_layout.addWidget(self.log_label)
        
        self.log_area = _LogTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setObjectName("log_area")
        self.log_area.setAcceptRichText(False)
        self.log_area.setFont(QFont("Microsoft YaHei UI", 9))
        self.log_area.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.log_area.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.log_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_area.setMinimumHeight(180)
        self.log_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        log_layout.addWidget(self.log_area, stretch=1)
        
        layout.addWidget(log_frame, stretch=2)

    def retranslate_ui(self):
        """翻译 UI 文字"""
        self.title_label.setText(TR("title_browser_console"))
        self.desc_label.setText(TR("desc_browser_console"))
        self.status_chip.setText(TR("chip_browser_suggestion"))
        self.launch_btn.setText(TR("btn_start_browser"))
        self.stop_btn.setText(TR("btn_stop_browser"))
        self.log_label.setText(TR("label_driver_logs"))

    def _init_driver(self):
        """初始化驱动线程"""
        self.driver = PlaywrightDriver(headless=False)
        self.driver.browser_ready.connect(self._on_browser_ready)
        self.driver.page_closed.connect(self._on_page_closed)
        self.driver.resource_detected.connect(self._on_resource_detected)
        self.driver.error_occurred.connect(self._on_driver_error)
        
        # 不再自动启动浏览器，用户需要时点击 "启动浏览器" 按钮
        
    def start_browser(self):
        """启动浏览器"""
        if not self.driver.isRunning():
            self.driver.active = True
            self.driver.start()
            self.log(TR("log_starting_browser"))
            self.launch_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            
    def stop_browser(self):
        """停止"""
        if self.driver.isRunning():
            self.driver.stop()
            self.log(TR("log_stopping_browser"))
            
    def _on_browser_ready(self):
        self._browser_ready = True
        self.log(f"✅ {TR('log_browser_ready')}")
        
        # 如果有待导航的 URL，立即执行
        if self._pending_url:
            self.log(f"{TR('log_nav_pending')}: {self._pending_url}")
            self.driver.navigate(self._pending_url)
            self._pending_url = None
        
    def _on_page_closed(self):
        self._browser_ready = False
        self.log(f"❌ {TR('log_browser_page_closed')}")
        self.launch_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        
    def _on_driver_error(self, msg):
        self.log(f"⚠️ {TR('log_error')}: {msg}")
        self.log(TR("msg_install_chrome"))
        self.log(TR("msg_check_chrome_exe"))
        
    def _on_resource_detected(self, url, headers, page_url, title):
        """资源回调"""
        self.log(f"{TR('log_captured')}: {url}")
        
        # 1. 直接添加到嗅探器核心 (触发 MainWindow 的 on_resource_found)
        if self.sniffer:
            self.sniffer.add_resource(url, headers, page_url, title)
            
        # 2. 兼容信号 (可选，保留以防有其他组件连接)
        self.resource_detected.emit(url, headers, page_url)
        self.format_found.emit(url, headers, page_url, title)

    def log(self, msg):
        msg = str(msg)
        # 动态获取当前翻译的前缀，以便在 log 中高亮
        prefixes = [
            TR("log_nav_pending"), 
            TR("log_captured"), 
            TR("log_navigating"), 
            f"✅ {TR('log_browser_ready')}", 
            f"❌ {TR('log_browser_page_closed')}", 
            f"⚠️ {TR('log_error')}", 
            TR("log_starting_browser"), 
            TR("log_stopping_browser")
        ]
        
        formatted_msg = msg
        for p in prefixes:
            if p and msg.startswith(p):
                # 将前缀设为深红色加粗
                prefix_html = f'<b><font color="#8B0000">{p}</font></b>'
                remaining_text = msg[len(p):]
                formatted_msg = prefix_html + remaining_text
                break

        cursor = self.log_area.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log_area.document().isEmpty():
            cursor.insertBlock()
        
        # 插入 HTML 内容
        cursor.insertHtml(formatted_msg.replace("\n", "<br>"))
        self.log_area.setTextCursor(cursor)
        # 确保自动滚动到底部
        self.log_area.ensureCursorVisible()
        self.log_area.ensureCursorVisible()
        
    # === 外部接口兼容 ===
    def load_url(self, url):
        self.log(f"{TR('log_navigating')}: {url}")
        
        if self._browser_ready and self.driver.isRunning():
            # 浏览器已就绪，直接导航
            self.driver.navigate(url)
        elif self.driver.isRunning():
            # 驱动在运行但还没就绪（正在启动），缓存 URL
            self._pending_url = url
            self.log(TR("log_browser_starting_cached"))
        else:
            # 驱动未运行，启动并缓存 URL
            self._pending_url = url
            self.start_browser()
            self.log(TR("log_starting_nav_later"))

    def back(self): pass
    def forward(self): pass
    def reload(self): pass
    def add_new_tab(self, url="about:blank"): 
        self.load_url(url) # 简化：PW 模式下暂不支持多标签控制

    def export_cookies_to_file(self, url: str = None) -> str:
        """
        导出当前浏览器的 Cookies 为 Netscape 格式文件
        委托给 PlaywrightDriver.export_cookies_to_file
        
        Returns:
            str: cookie 文件的路径，失败返回空字符串
        """
        if self.driver and self._browser_ready:
            return self.driver.export_cookies_to_file(url)
        return ""
