"""
Browser View - Playwright Controller
Controls the external Chrome window via PlaywrightDriver
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QLabel, 
                             QHBoxLayout, QTextEdit, QFrame)
from PyQt6.QtCore import pyqtSignal, QUrl, Qt
from core.m3u8_sniffer import M3U8Sniffer
from core.playwright_driver import PlaywrightDriver
from utils.logger import logger

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
        self._init_driver()
        
    def _init_ui(self):
        """初始化控制面板 UI - 左右布局"""
        layout = QHBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # === 左侧：状态卡片 ===
        status_card = QFrame()
        status_card.setObjectName("card")
        card_layout = QVBoxLayout(status_card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)
        
        # 标题
        title = QLabel("浏览器控制台")
        title.setObjectName("page_title")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft)
        card_layout.addWidget(title)

        # 说明
        desc = QLabel(
            "浏览器会在独立窗口中运行，适合登录站点、打开扩展并完成真实页面交互。"
        )
        desc.setAlignment(Qt.AlignmentFlag.AlignLeft)
        desc.setWordWrap(True)
        desc.setObjectName("muted_text")
        card_layout.addWidget(desc)

        status_chip = QLabel("建议先启动浏览器，再打开目标页面。")
        status_chip.setObjectName("status_chip")
        status_chip.setAlignment(Qt.AlignmentFlag.AlignLeft)
        card_layout.addWidget(status_chip)
        
        # 填充中间空间
        card_layout.addStretch()
        
        # 控制按钮
        btn_layout = QHBoxLayout()
        
        self.launch_btn = QPushButton("启动浏览器")
        self.launch_btn.setMinimumHeight(34)
        self.launch_btn.setObjectName("success_button")
        self.launch_btn.clicked.connect(self.start_browser)

        self.stop_btn = QPushButton("停止浏览器")
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
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(8)
        
        log_label = QLabel("驱动日志")
        log_label.setObjectName("log_label")
        log_layout.addWidget(log_label)
        
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setObjectName("log_area")
        log_layout.addWidget(self.log_area)
        
        layout.addWidget(log_frame, stretch=2)

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
            self.log("正在启动浏览器...")
            self.launch_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            
    def stop_browser(self):
        """停止"""
        if self.driver.isRunning():
            self.driver.stop()
            self.log("正在停止浏览器...")
            
    def _on_browser_ready(self):
        self._browser_ready = True
        self.log("✅ 浏览器已就绪")
        
        # 如果有待导航的 URL，立即执行
        if self._pending_url:
            self.log(f"执行待导航: {self._pending_url}")
            self.driver.navigate(self._pending_url)
            self._pending_url = None
        
    def _on_page_closed(self):
        self._browser_ready = False
        self.log("❌ 浏览器页面已关闭")
        self.launch_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        
    def _on_driver_error(self, msg):
        self.log(f"⚠️ 错误: {msg}")
        self.log("请先安装 Google Chrome，再重新启动浏览器工作台")
        self.log("若已安装 Chrome，请确认系统可正常找到 chrome.exe")
        
    def _on_resource_detected(self, url, headers, page_url, title):
        """资源回调"""
        self.log(f"捕获: {url[:50]}...")
        
        # 1. 直接添加到嗅探器核心 (触发 MainWindow 的 on_resource_found)
        if self.sniffer:
            self.sniffer.add_resource(url, headers, page_url, title)
            
        # 2. 兼容信号 (可选，保留以防有其他组件连接)
        self.resource_detected.emit(url, headers, page_url)
        self.format_found.emit(url, headers, page_url, title)

    def log(self, msg):
        self.log_area.append(msg)
        
    # === 外部接口兼容 ===
    def load_url(self, url):
        self.log(f"导航: {url}")
        
        if self._browser_ready and self.driver.isRunning():
            # 浏览器已就绪，直接导航
            self.driver.navigate(url)
        elif self.driver.isRunning():
            # 驱动在运行但还没就绪（正在启动），缓存 URL
            self._pending_url = url
            self.log("浏览器启动中，已缓存导航请求...")
        else:
            # 驱动未运行，启动并缓存 URL
            self._pending_url = url
            self.start_browser()
            self.log("正在启动浏览器，稍后自动导航...")

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
