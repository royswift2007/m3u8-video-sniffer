"""
Main application window integrating all components
"""
import html
import threading
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QSplitter, QLineEdit, QComboBox, QLabel, QPushButton,
                             QMessageBox, QDialog, QTextBrowser, QFrame, QMenu)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from pathlib import Path

from ui.browser_view import BrowserView
from ui.resource_panel import ResourcePanel
from ui.download_queue import DownloadQueuePanel
from core.m3u8_sniffer import M3U8Sniffer
from core.download_manager import DownloadManager
from core.task_model import DownloadTask, M3U8Resource
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine
from engines.ytdlp_engine import YtdlpEngine
from engines.streamlink_engine import StreamlinkEngine
from engines.aria2_engine import Aria2Engine
from engines.ffmpeg_processor import FFmpegProcessor
from core.catcatch_server import CatCatchServer
from utils.config_manager import config
from utils.logger import logger
from core.m3u8_parser import M3U8FetchThread
from utils.i18n import i18n, TR


class MainWindow(QMainWindow):
    """主应用窗口"""

    task_update_received = pyqtSignal(object)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("M3U8 Video Sniffer - 全能视频下载工具")
        self.setGeometry(100, 100, 1400, 900)
        
        # 应用现代样式
        from ui.styles import MODERN_STYLE
        self.setStyleSheet(MODERN_STYLE)
        
        # 设置应用图标
        icon_path = str(Path(__file__).parent.parent / "resources" / "icon.png")
        if Path(icon_path).exists():
            self.setWindowIcon(QIcon(icon_path))
        
        # 初始化语言环境
        i18n.set_language(config.get("language", "zh"))
        i18n.language_changed.connect(self.retranslate_ui)
        
        # 初始化组件
        self._init_engines()
        self._init_core_components()
        self._init_ui()
        self.retranslate_ui()  # 触发首次渲染
        self._connect_signals()
        self.task_update_received.connect(self._handle_task_update_on_main_thread)
        
        logger.info(TR("log_ready"))
    
    def _init_engines(self):
        """初始化下载引擎"""
        self.engines = []
        
        try:
            # N_m3u8DL-RE
            n_m3u8dl_path = config.get("engines.n_m3u8dl_re.path")
            if Path(n_m3u8dl_path).exists():
                self.engines.append(N_m3u8DL_RE_Engine(n_m3u8dl_path))
                logger.info(TR("log_engine_loaded").format(name="N_m3u8DL-RE"))
            else:
                logger.warning(TR("log_engine_not_found").format(name="N_m3u8DL-RE", path=n_m3u8dl_path))
            
            # yt-dlp
            ytdlp_path = config.get("engines.ytdlp.path")
            if Path(ytdlp_path).exists():
                ytdlp_engine = YtdlpEngine(ytdlp_path)
                self.engines.append(ytdlp_engine)
                self.ytdlp_engine = ytdlp_engine  # 保存引用，用于后续设置 cookie_exporter
                logger.info(TR("log_engine_loaded").format(name="yt-dlp"))
            else:
                logger.warning(TR("log_engine_not_found").format(name="yt-dlp", path=ytdlp_path))
            
            # Streamlink
            streamlink_path = config.get("engines.streamlink.path")
            if Path(streamlink_path).exists():
                self.engines.append(StreamlinkEngine(streamlink_path))
                logger.info(TR("log_engine_loaded").format(name="Streamlink"))
            else:
                logger.warning(TR("log_engine_not_found").format(name="Streamlink", path=streamlink_path))
            
            # Aria2
            aria2_path = config.get("engines.aria2.path")
            if Path(aria2_path).exists():
                self.engines.append(Aria2Engine(aria2_path))
                logger.info(TR("log_engine_loaded").format(name="Aria2"))
            else:
                logger.warning(TR("log_engine_not_found").format(name="Aria2", path=aria2_path))
            
            # FFmpeg (后处理)
            ffmpeg_path = config.get("engines.ffmpeg.path")
            if Path(ffmpeg_path).exists():
                self.ffmpeg = FFmpegProcessor(ffmpeg_path)
                logger.info(TR("log_ffmpeg_loaded"))
            else:
                self.ffmpeg = None
                logger.warning(TR("log_ffmpeg_not_found").format(path=ffmpeg_path))
            
            if not self.engines:
                QMessageBox.warning(
                    self,
                    TR("msg_warning_title"),
                    TR("msg_no_engines")
                )
        except Exception as e:
            logger.error(TR("log_engine_init_failed").format(error=str(e)))
    
    def _init_core_components(self):
        """初始化核心组件"""
        self.sniffer = M3U8Sniffer()
        self.download_manager = DownloadManager(
            self.engines,
            max_concurrent=config.get("max_concurrent_downloads", 3)
        )
        
        # 设置回调
        self.download_manager.on_task_update = self._on_task_update
        
        # 启动猫爪 HTTP 服务
        self.catcatch_server = CatCatchServer(port=config.get("catcatch.port", 9527))
        self.catcatch_server.download_requested.connect(self._on_catcatch_download)
        self.catcatch_server.start()
    
    def _init_ui(self):
        """初始化 UI"""
        # 中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # === 主标签页 ===
        from PyQt6.QtWidgets import QTabWidget
        self.main_tabs = QTabWidget()
        
        # === 标签页 1: 浏览器 ===
        browser_tab = QWidget()
        browser_layout = QVBoxLayout(browser_tab)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.setSpacing(8)

        # 浏览器工具栏
        browser_toolbar_card = QFrame()
        browser_toolbar_card.setObjectName("toolbar_card")
        browser_toolbar = QHBoxLayout(browser_toolbar_card)
        browser_toolbar.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        browser_toolbar.setContentsMargins(10, 8, 10, 8)
        browser_toolbar.setSpacing(6)

        # 导航按钮
        self.back_btn = QPushButton("")
        self.back_btn.setObjectName("nav_back")
        self.back_btn.setFixedSize(20, 20)
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.setStyleSheet("QPushButton#nav_back { image: url(resources/nav_back_v2.svg); background-size: 8px 8px; background-repeat: no-repeat; background-position: center; }")
        self.back_btn.clicked.connect(self._on_back)

        self.forward_btn = QPushButton("")
        self.forward_btn.setObjectName("nav_forward")
        self.forward_btn.setFixedSize(20, 20)
        self.forward_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forward_btn.setStyleSheet("QPushButton#nav_forward { image: url(resources/nav_forward_v2.svg); background-size: 8px 8px; background-repeat: no-repeat; background-position: center; }")
        self.forward_btn.clicked.connect(self._on_forward)

        self.refresh_btn = QPushButton("")
        self.refresh_btn.setObjectName("nav_refresh")
        self.refresh_btn.setFixedSize(20, 20)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setStyleSheet("QPushButton#nav_refresh { image: url(resources/nav_refresh_v2.svg); background-size: 8px 8px; background-repeat: no-repeat; background-position: center; }")
        self.refresh_btn.clicked.connect(self._on_refresh)

        # 新建标签页按钮
        self.new_tab_btn = QPushButton("")
        self.new_tab_btn.setObjectName("nav_new_tab")
        self.new_tab_btn.setFixedSize(20, 20)
        self.new_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_tab_btn.setStyleSheet("QPushButton#nav_new_tab { image: url(resources/nav_new_tab_v2.svg); background-size: 8px 8px; background-repeat: no-repeat; background-position: center; }")
        self.new_tab_btn.clicked.connect(lambda: self.browser.add_new_tab())
        
        # 地址栏
        self.address_bar = QLineEdit()
        self.address_bar.setPlaceholderText("输入页面地址后按回车，例如视频网站详情页")
        self.address_bar.returnPressed.connect(self._on_address_entered)

        # 抓取按钮
        self.grab_btn = QPushButton("")
        self.grab_btn.setMinimumWidth(84)
        self.grab_btn.clicked.connect(self._on_address_entered)

        # 引擎选择器
        self.engine_label = QLabel("")
        self.engine_selector = QComboBox()
        self.engine_selector.addItems([
            TR("strategy_auto"),
            "N_m3u8DL-RE",
            "yt-dlp",
            "Streamlink",
            "Aria2"
        ])
        self.engine_selector.setCurrentIndex(0)
        self.engine_selector.setMinimumWidth(140)
        
        browser_toolbar.addWidget(self.back_btn)
        browser_toolbar.addWidget(self.forward_btn)
        browser_toolbar.addWidget(self.refresh_btn)
        browser_toolbar.addWidget(self.new_tab_btn)  # 添加新建标签页按钮
        browser_toolbar.addWidget(self.address_bar)
        browser_toolbar.addWidget(self.grab_btn)
        browser_toolbar.addWidget(self.engine_label)
        browser_toolbar.addWidget(self.engine_selector)
        
        # 浏览器视图
        self.browser = BrowserView(self.sniffer)
        
        # 连接 yt-dlp 的 cookie_exporter 回调到浏览器
        if hasattr(self, 'ytdlp_engine') and self.ytdlp_engine:
            self.ytdlp_engine.cookie_exporter = self.browser.export_cookies_to_file
        
        browser_layout.addWidget(browser_toolbar_card)
        browser_layout.addWidget(self.browser, stretch=1)
        
        # === 标签页 2: 资源检测 ===
        detection_tab = QWidget()
        detection_layout = QVBoxLayout(detection_tab)
        detection_layout.setContentsMargins(0, 0, 0, 0)

        self.resource_panel = ResourcePanel()
        detection_layout.addWidget(self.resource_panel)
        
        # === 标签页 3: 下载管理 ===
        download_tab = QWidget()
        download_layout = QVBoxLayout(download_tab)
        download_layout.setContentsMargins(0, 0, 0, 0)
        download_layout.setSpacing(10)
        
        
        # **下载参数设置面板**
        from PyQt6.QtWidgets import QSpinBox, QGroupBox, QGridLayout
        
        self.pref_group = QGroupBox("下载偏好")
        settings_layout = QVBoxLayout()  # 改为垂直布局
        settings_layout.setSpacing(10)
        settings_layout.setContentsMargins(15, 15, 15, 15)
        
        # **第一行：下载路径**
        path_row = QHBoxLayout()
        path_row.setSpacing(10)
        
        path_label = QLabel("")
        self.path_label = path_label
        self.path_display = QLabel(config.get("download_dir"))
        self.path_display.setObjectName("path_display")

        self.select_path_btn = QPushButton("")
        self.select_path_btn.setObjectName("secondary_button")
        self.select_path_btn.clicked.connect(self._on_select_path)

        self.open_folder_btn = QPushButton("")
        self.open_folder_btn.setObjectName("secondary_button")
        self.open_folder_btn.clicked.connect(self._on_open_folder)
        
        path_row.addWidget(path_label)
        path_row.addWidget(self.path_display, stretch=1)
        path_row.addWidget(self.select_path_btn)
        path_row.addWidget(self.open_folder_btn)
        
        settings_layout.addLayout(path_row)
        
        # **第二行：下载参数**
        params_row = QHBoxLayout()
        params_row.setSpacing(20)
        
        
        # 线程数
        self.thread_label = QLabel("")
        self.thread_count_spin = QSpinBox()
        self.thread_count_spin.setRange(1, 128)
        self.thread_count_spin.setValue(config.get("engines.n_m3u8dl_re.thread_count", 8))
        self.thread_count_spin.setMinimumWidth(120)
        self.thread_count_spin.valueChanged.connect(self._on_thread_count_changed)
        params_row.addWidget(self.thread_label)
        params_row.addWidget(self.thread_count_spin)
        
        # 重试次数
        self.retry_label = QLabel("")
        self.retry_count_spin = QSpinBox()
        self.retry_count_spin.setRange(0, 50)
        self.retry_count_spin.setValue(config.get("engines.n_m3u8dl_re.retry_count", 5))
        self.retry_count_spin.setMinimumWidth(100)
        self.retry_count_spin.valueChanged.connect(self._on_retry_count_changed)
        params_row.addWidget(self.retry_label)
        params_row.addWidget(self.retry_count_spin)
        
        # 并发下载数
        self.concurrent_label = QLabel("")
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 10)
        self.concurrent_spin.setValue(config.get("max_concurrent_downloads", 2))
        self.concurrent_spin.setSuffix(TR("suffix_tasks"))
        self.concurrent_spin.setMinimumWidth(100)
        self.concurrent_spin.valueChanged.connect(self._on_concurrent_changed)
        params_row.addWidget(self.concurrent_label)
        params_row.addWidget(self.concurrent_spin)
        
        # 限速（MB/s，0表示不限速）
        self.speed_label = QLabel("")
        self.speed_limit_spin = QSpinBox()
        self.speed_limit_spin.setRange(0, 100)  # 0-100MB/s
        self.speed_limit_spin.setValue(config.get("speed_limit", 3))
        self.speed_limit_spin.setMinimumWidth(120)
        self.speed_limit_spin.valueChanged.connect(self._on_speed_limit_changed)
        params_row.addWidget(self.speed_label)
        params_row.addWidget(self.speed_limit_spin)
        
        params_row.addStretch()  # 添加弹性空间
        
        settings_layout.addLayout(params_row)
        
        self.pref_group.setLayout(settings_layout)
        download_layout.addWidget(self.pref_group)
        
        
        # 使用 QSplitter 让下载队列和日志区域可拖动调整
        from PyQt6.QtWidgets import QSplitter
        from PyQt6.QtCore import Qt as QtCore_Qt
        
        download_splitter = QSplitter(QtCore_Qt.Orientation.Vertical)
        
        # 下载队列
        self.download_queue = DownloadQueuePanel()
        download_splitter.addWidget(self.download_queue)
        
        # 日志/历史标签页
        from ui.log_panel import LogPanel
        from ui.history_panel import HistoryPanel
        
        self.bottom_tabs = QTabWidget()
        self.log_panel = LogPanel()
        self.history_panel = HistoryPanel()
        
        self.bottom_tabs.addTab(self.log_panel, "运行日志")
        self.bottom_tabs.addTab(self.history_panel, "下载历史")
        
        download_splitter.addWidget(self.bottom_tabs)
        
        # 设置初始比例（下载队列:日志 = 1:1）
        download_splitter.setSizes([300, 300])
        
        # 设置最小尺寸
        self.download_queue.setMinimumHeight(100)
        self.bottom_tabs.setMinimumHeight(100)
        
        download_layout.addWidget(download_splitter, stretch=1)
        
        # 添加所有标签页
        self.main_tabs.addTab(browser_tab, "浏览器工作台")
        self.main_tabs.addTab(detection_tab, "资源列表")
        self.main_tabs.addTab(download_tab, "下载中心")

        # 标签栏右侧入口：语言切换 + 使用手册（纯文字，保持原有角落布局）
        self.current_language = "zh"

        manual_container = QWidget()
        manual_layout = QHBoxLayout(manual_container)
        manual_layout.setContentsMargins(0, 4, 12, 0)  # 移除底部边距，增加顶部微调
        manual_layout.setSpacing(8)

        # 语言切换按钮 + 菜单
        self.language_btn = QPushButton("")
        self.language_btn.setObjectName("language_button")
        self.language_btn.setMinimumWidth(80)
        self.language_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        lang_menu = QMenu(self)
        lang_menu.setObjectName("language_menu")
        action_zh = lang_menu.addAction("中文")
        action_en = lang_menu.addAction("English")
        
        action_zh.triggered.connect(lambda: i18n.set_language("zh"))
        action_en.triggered.connect(lambda: i18n.set_language("en"))
        self.language_btn.setMenu(lang_menu)

        self.manual_link = QLabel("")
        self.manual_link.setObjectName("manual_link")
        self.manual_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manual_link.mousePressEvent = lambda ev: self._show_manual_dialog()

        manual_layout.addWidget(
            self.language_btn,
            0,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        manual_layout.addWidget(
            self.manual_link,
            0,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        self.main_tabs.setCornerWidget(manual_container, Qt.Corner.TopRightCorner)
        
        # === 添加到主布局 ===
        main_layout.addWidget(self.main_tabs)


    
    def _connect_signals(self):
        """连接信号"""
        # 资源发现
        self.sniffer.on_resource_found = self._on_resource_found
        
        # 下载请求
        self.resource_panel.download_requested.connect(self._on_download_requested)
        
        # 浏览器 URL 变化
        self.browser.url_changed.connect(self._on_url_changed)
        
        # 下载队列控制信号
        self.download_queue.task_paused.connect(self.download_manager.pause_task)
        self.download_queue.task_resumed.connect(self.download_manager.resume_task)
        self.download_queue.task_cancelled.connect(self.download_manager.cancel_task)
        self.download_queue.task_retried.connect(self.download_manager.resume_task)  # 重试 = 继续
        self.download_queue.task_removed.connect(self.download_manager.remove_task)  # 移除任务
        self.download_queue.task_batch_imported.connect(self._on_batch_import_requested)
        
        # 历史记录操作
        self.history_panel.record_download_requested.connect(self._on_history_download_requested)
        self.history_panel.record_deleted.connect(self._on_history_record_deleted)

    def _on_url_changed(self, url: QUrl):
        """URL 变化时更新地址栏"""
        url_str = url.toString()
        if url_str == "about:blank":
            url_str = ""
        self.address_bar.setText(url_str)
        self.address_bar.setCursorPosition(0)
    
    def _on_resource_found(self, resource: M3U8Resource):
        """资源发现回调"""
        # 过滤掉无效的 YouTube 资源（只有首页 URL，没有具体视频）
        page_url = resource.page_url or ''
        
        if ('youtube.com' in page_url or 'youtu.be' in page_url):
            if not ('watch?v=' in page_url or '/shorts/' in page_url or 'youtu.be/' in page_url):
                # 这是 YouTube 首页或频道页的资源，跳过
                return
            
            # 如果标题只是 "YouTube"，尝试从 URL 提取视频 ID 来区分
            if resource.title.strip() == "YouTube":
                import re
                # 匹配 11 位视频 ID
                video_id_match = re.search(r'(?:v=|\/shorts\/|\/)([\w-]{11})', page_url)
                if video_id_match:
                    video_id = video_id_match.group(1)
                    resource.title = f"YouTube Video [{video_id}]"
        
        # 获取用户选择的引擎
        user_engine = self.get_selected_engine()
        resource.selected_engine = user_engine

        # 预测探测阶段将优先尝试的引擎，用于资源列表展示
        from core.engine_selector import EngineSelector
        selector = EngineSelector(self.engines)
        _, engine_name = selector.predict(resource.url, user_engine)

        # 添加到资源面板
        self.resource_panel.add_resource(resource, engine_name)
    
    def _on_download_requested(self, resource: M3U8Resource):
        """用户请求下载 - 使用当前引擎选择器的值"""
        try:
            download_url = resource.url
            headers = resource.headers.copy() if resource.headers else {}

            # 优先使用资源被探测/加入列表时记录的引擎；仅在 auto 时才回退到当前选择
            user_engine = getattr(resource, "selected_engine", None)
            if user_engine is None:
                user_engine = self.get_selected_engine()
            logger.info(
                "[QUEUE] 收到添加到下载队列请求",
                event="ui_download_request_received",
                title=getattr(resource, "title", ""),
                url=getattr(resource, "url", ""),
                page_url=getattr(resource, "page_url", ""),
                user_engine=user_engine or "auto",
            )

            # 判断是否为 yt-dlp 支持的平台（需要使用页面 URL 和分辨率选择）
            ytdlp_sites = [
                'youtube.com', 'youtu.be', 'googlevideo.com',  # YouTube
                'tiktok.com', 'tiktokv.com',  # TikTok
                'bilibili.com', 'bilivideo.com',  # Bilibili
                'twitter.com', 'x.com', 'twimg.com',  # Twitter/X
                'instagram.com', 'cdninstagram.com',  # Instagram
                'facebook.com', 'fbcdn.net',  # Facebook
                'vimeo.com',  # Vimeo
                'dailymotion.com',  # Dailymotion
                'twitch.tv',  # Twitch
                'nicovideo.jp',  # Niconico
            ]

            is_ytdlp_site = any(site in resource.url.lower() or site in (resource.page_url or '').lower()
                               for site in ytdlp_sites)

            if is_ytdlp_site:
                # 优先使用页面 URL（yt-dlp 需要页面 URL 而不是 CDN 流地址）
                if resource.page_url:
                    download_url = resource.page_url
                    logger.info(f"yt-dlp 资源：使用页面 URL 下载: {download_url}")

                # 检查 YouTube URL 是否有效（必须包含视频 ID）
                if 'youtube.com' in download_url or 'youtu.be' in download_url:
                    if not ('watch?v=' in download_url or '/shorts/' in download_url or 'youtu.be/' in download_url):
                        from PyQt6.QtWidgets import QMessageBox
                        QMessageBox.warning(self, "无效链接", "这不是一个有效的 YouTube 视频链接。\n请选择包含具体视频的资源。")
                        return

                # 显示分辨率选择对话框（yt-dlp 会自动使用 Firefox cookies 作为回退）
                self._show_format_dialog(download_url, resource.title, headers, None, user_engine)
                return

            # M3U8 资源：尝试解析多码率
            is_m3u8 = '.m3u8' in download_url.lower()
            if is_m3u8:
                # 复用资源面板已缓存的 variants（避免重复网络请求）
                cached_variants = resource.variants if hasattr(resource, 'variants') else []
                self._show_m3u8_variant_dialog(download_url, resource.title, headers, user_engine, cached_variants)
                return

            # 其他资源，直接下载
            self._start_download(download_url, resource.title, headers, user_engine)
        except Exception as e:
            logger.error(
                f"[QUEUE] 添加到下载队列主链路异常: {e}",
                event="ui_download_request_failed",
                title=getattr(resource, "title", ""),
                url=getattr(resource, "url", ""),
            )
            QMessageBox.critical(self, "添加失败", f"添加到下载队列失败：\n{e}")
    
    def _show_format_dialog(self, url: str, title: str, headers: dict, cookie_file: str = None, user_engine: str = None):
        """显示分辨率选择对话框（支持 yt-dlp 所有平台）- 异步获取格式避免卡顿"""
        from ui.format_dialog import FormatSelectionDialog
        from PyQt6.QtWidgets import QProgressDialog
        from PyQt6.QtCore import QThread, pyqtSignal
        
        # 获取 yt-dlp 引擎
        ytdlp_engine = None
        for engine in self.download_manager.engines:
            if engine.get_name() == 'yt-dlp':
                ytdlp_engine = engine
                break
        
        if not ytdlp_engine:
            logger.warning("未找到 yt-dlp 引擎，使用最佳质量下载")
            self._start_download(url, title, headers, user_engine)
            return
        
        # 创建后台线程获取格式
        class FormatFetchThread(QThread):
            finished = pyqtSignal(list)
            
            def __init__(self, engine, url, cookie_file=None):
                super().__init__()
                self.engine = engine
                self.url = url
                self.cookie_file = cookie_file
            
            def run(self):
                # 使用预导出的 cookie 文件
                formats = self.engine.get_formats(self.url, cookie_file=self.cookie_file)
                self.finished.emit(formats or [])
        
        # 显示加载对话框
        progress = QProgressDialog(TR("msg_fetching_formats"), TR("btn_cancel"), 0, 0, self)
        progress.setWindowTitle(TR("dialog_please_wait"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)  # 立即显示
        progress.setCancelButton(None)  # 不可取消（避免复杂状态处理）
        progress.show()
        
        # 创建并启动线程（传入预导出的 cookie 文件）
        self._format_thread = FormatFetchThread(ytdlp_engine, url, cookie_file)
        
        def on_formats_ready(formats):
            progress.close()
            
            if not formats:
                logger.warning("无法获取视频格式，将使用最佳质量下载")
                # 将 cookie 文件路径添加到 headers 中
                if cookie_file:
                    headers['_cookie_file'] = cookie_file
                self._start_download(url, title, headers, user_engine)
                return
            
            # 显示格式选择对话框
            dialog = FormatSelectionDialog(formats, self)
            if dialog.exec():
                selected_format = dialog.get_selected_format()
                
                if selected_format:
                    format_id = selected_format.get('format_id', '')
                    height = selected_format.get('height', 0)
                    fps = selected_format.get('fps', 30)
                    logger.info(f"用户选择格式: {format_id} ({height}p{fps if fps > 30 else ''})")
                    
                    download_url = f"{url}#format={format_id}"
                    # 将 cookie 文件路径添加到 headers 中供下载时使用
                    if cookie_file:
                        headers['_cookie_file'] = cookie_file
                    self._start_download(download_url, title, headers, user_engine)
                else:
                    logger.info("用户选择最佳质量下载")
                    if cookie_file:
                        headers['_cookie_file'] = cookie_file
                    self._start_download(url, title, headers, user_engine)
            else:
                logger.info("用户取消了下载")
        
        self._format_thread.finished.connect(on_formats_ready)
        self._format_thread.start()
        
        logger.info("正在获取视频格式...")
    
    def _show_m3u8_variant_dialog(self, url: str, title: str, headers: dict, user_engine: str = None, cached_variants: list = None):
        """显示 M3U8 清晰度选择对话框"""
        from ui.format_dialog import FormatSelectionDialog
        from PyQt6.QtWidgets import QProgressDialog
        
        def _handle_variants(variants):
            """处理解析完成的 variants"""
            if not variants:
                logger.info("未找到多码率变体，直接下载原始链接")
                self._start_download(url, title, headers, user_engine, master_url=url, media_url=url)
                return
                
            # 显示选择对话框
            dialog = FormatSelectionDialog(variants, self)
            if dialog.exec():
                selected = dialog.get_selected_format()
                if selected:
                    resolution = selected.get('resolution', '')
                    height = selected.get('height', 0)
                    logger.info(f"用户选择了变体: {resolution}")
                    
                    # 更新标题以包含清晰度信息
                    new_title = title
                    if resolution and resolution not in title:
                        new_title = f"{title} [{resolution}]"
                    
                    # 判断当前是否使用 N_m3u8DL-RE 引擎
                    effective_engine = user_engine
                    if not effective_engine:
                        from core.engine_selector import EngineSelector
                        selector = EngineSelector(self.engines)
                        _, effective_engine = selector.select(url, None)
                    
                    if effective_engine == 'N_m3u8DL-RE':
                        # N_m3u8DL-RE: 传递 master playlist URL + 选中的变体信息，让引擎原生处理
                        logger.info(f"N_m3u8DL-RE: 传递 master URL + --select-video 参数")
                        self._start_download(
                            url,
                            new_title,
                            headers,
                            user_engine,
                            selected_variant=selected,
                            master_url=url,
                            media_url=selected.get('url', url),
                        )
                    else:
                        # 其他引擎: 直接使用变体 URL
                        variant_url = selected.get('url', url)
                        self._start_download(
                            variant_url,
                            new_title,
                            headers,
                            user_engine,
                            master_url=url,
                            media_url=variant_url,
                        )
                else:
                    # 用户选择“最佳质量”
                    best_variant = variants[0]
                    
                    effective_engine = user_engine
                    if not effective_engine:
                        from core.engine_selector import EngineSelector
                        selector = EngineSelector(self.engines)
                        _, effective_engine = selector.select(url, None)
                    
                    if effective_engine == 'N_m3u8DL-RE':
                        # N_m3u8DL-RE: 传递 master URL + 最佳变体
                        self._start_download(
                            url,
                            title,
                            headers,
                            user_engine,
                            selected_variant=best_variant,
                            master_url=url,
                            media_url=best_variant.get('url', url),
                        )
                    else:
                        variant_url = best_variant.get('url', url)
                        logger.info("用户选择自动/最佳质量")
                        self._start_download(
                            variant_url,
                            title,
                            headers,
                            user_engine,
                            master_url=url,
                            media_url=variant_url,
                        )
            else:
                logger.info("用户取消下载")
        
        # 如果已有缓存的 variants，直接使用，无需再次网络请求
        if cached_variants:
            logger.info(f"使用缓存的 M3U8 变体列表 ({len(cached_variants)} 个)")
            _handle_variants(cached_variants)
            return
        
        # 显示加载对话框
        progress = QProgressDialog(TR("msg_analyzing_m3u8"), TR("btn_cancel"), 0, 0, self)
        progress.setWindowTitle(TR("dialog_please_wait"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        
        # 启动后台线程解析
        self._m3u8_thread = M3U8FetchThread(url, headers)
        
        def on_variants_ready(variants):
            progress.close()
            _handle_variants(variants)
        
        self._m3u8_thread.finished.connect(on_variants_ready)
        self._m3u8_thread.start()
    
    def _start_download(
        self,
        url: str,
        title: str,
        headers: dict,
        user_engine: str = None,
        selected_variant: dict = None,
        save_dir: str = None,
        master_url: str = None,
        media_url: str = None,
    ):
        """开始下载任务"""
        try:
            logger.info(
                "[QUEUE] 开始构建下载任务",
                event="ui_start_download",
                title=title,
                url=url,
                save_dir=save_dir or config.get("download_dir"),
                user_engine=user_engine or "auto",
                has_headers=bool(headers),
                has_selected_variant=bool(selected_variant),
                master_url=master_url or "",
                media_url=media_url or "",
            )

            # 若本地已存在同名文件，提示是否覆盖
            from pathlib import Path
            from PyQt6.QtWidgets import QMessageBox

            target_dir = Path(save_dir or config.get("download_dir"))
            exact_file = target_dir / title
            possible_files = list(target_dir.glob(f"{title}.*")) if target_dir.exists() else []
            file_exists = exact_file.exists() or len(possible_files) > 0

            if file_exists:
                reply = QMessageBox.question(
                    self,
                    "文件已存在",
                    f"本地已存在同名文件：\n{title}\n是否重新下载并覆盖？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    logger.info(f"用户取消覆盖下载: {title}")
                    return

            task = DownloadTask(
                url=url,
                save_dir=save_dir or config.get("download_dir"),
                filename=title,
                headers=headers
            )

            # 设置用户选择的分辨率变体（供 N_m3u8DL-RE 使用）
            if selected_variant:
                task.selected_variant = selected_variant
            if master_url:
                task.master_url = master_url
            if media_url:
                task.media_url = media_url

            # 使用传入的引擎或获取当前选择的引擎
            if user_engine is None:
                user_engine = self.get_selected_engine()

            # 添加到下载队列
            self.download_manager.add_task(task, user_engine)
            logger.info(f"已添加下载任务: {task.filename}")
        except Exception as e:
            logger.error(
                f"[QUEUE] 创建或入队下载任务失败: {title} - {e}",
                event="ui_start_download_failed",
                title=title,
                url=url,
            )
            QMessageBox.critical(self, "添加失败", f"添加到下载队列失败：\n{e}")
    
    def _on_task_update(self, task: DownloadTask):
        """任务更新回调：可能来自下载工作线程，统一投递回主线程。"""
        self.task_update_received.emit(task)

    @pyqtSlot(object)
    def _handle_task_update_on_main_thread(self, task: DownloadTask):
        """在 Qt 主线程中刷新下载队列和历史记录。"""
        # 取消高频 DEBUG 日志，防止刷屏
        # logger.debug(
        #     f"[UI-THREAD] 处理任务更新: {task.filename}",
        #     status=getattr(task, 'status', ''),
        #     thread_name=threading.current_thread().name,
        # )
        self.download_queue.add_or_update_task(task)
        
        # 如果任务完成或失败，记录到历史
        if task.status in ['completed', 'failed']:
            if getattr(task, "_history_recorded_status", None) == task.status:
                return
            size = task.downloaded_size if task.downloaded_size else 'N/A'
            self.history_panel.add_record(
                filename=task.filename,
                url=task.url,
                status=task.status,
                size=size,
                headers=task.headers,
                engine=task.engine,
                save_dir=task.save_dir,
                selected_variant=getattr(task, 'selected_variant', None),
                master_url=getattr(task, 'master_url', None),
                media_url=getattr(task, 'media_url', None)
            )
            setattr(task, "_history_recorded_status", task.status)
    
    def _on_back(self):
        """后退"""
        self.browser.back()
    
    def _on_forward(self):
        """前进"""
        self.browser.forward()
    
    def _on_refresh(self):
        """刷新"""
        self.browser.reload()
    
    def _on_address_entered(self):
        """地址栏回车"""
        url = self.address_bar.text().strip()
        if not url:
            return
            
        # 拦截磁力链接
        if url.startswith("magnet:?"):
            logger.info(f"检测到磁力链接: {url}")
            from datetime import datetime
            import urllib.parse
            
            # 尝试从 magnet 链接中提取 dn (Display Name)
            try:
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                name = params.get('dn', ['磁力下载任务'])[0]
            except Exception:
                name = "磁力下载任务"
            
            resource = M3U8Resource(
                url=url,
                headers={},  # 本地输入，无 headers
                page_url="", # 无来源页面
                title=name,
                timestamp=datetime.now()
            )
            # 添加到资源列表，强制指定 Aria2 引擎
            self.resource_panel.add_resource(resource, engine_name='Aria2')
            
            # 提示用户 (改为状态栏消息，不弹窗)
            # QMessageBox.information(self, "提示", "已捕捉磁力链接，请在资源列表中点击下载。")
            logger.info(f"已捕捉磁力链接: {name}")
            self.statusBar().showMessage(f"已捕捉磁力链接: {name}", 3000)
            self.address_bar.clear()
            return
            
        self.browser.load_url(url)
    
    def get_selected_engine(self) -> str:
        """获取用户选择的引擎"""
        index = self.engine_selector.currentIndex()
        if index == 0:  # 自动选择
            return None
        engines = [None, 'N_m3u8DL-RE', 'yt-dlp', 'Streamlink', 'Aria2']
        return engines[index]

    def _on_history_download_requested(self, record: dict):
        """从历史记录重新下载"""
        url = record.get('url', '')
        if not url:
            QMessageBox.warning(self, "无法下载", "历史记录缺少 URL")
            return

        title = record.get('filename', '未命名视频')
        headers = record.get('headers', {}) or {}
        cookie_file = record.get('cookie_file')
        if cookie_file and isinstance(headers, dict) and not headers.get('_cookie_file'):
            headers['_cookie_file'] = cookie_file
        user_engine = record.get('engine') or None
        selected_variant = record.get('selected_variant', None)
        save_dir = record.get('save_dir') or None

        # 兼容旧记录：如果没有 headers/save_dir 等信息仍可下载
        self._start_download(
            url,
            title,
            headers,
            user_engine,
            selected_variant=selected_variant,
            save_dir=save_dir,
            master_url=record.get('master_url', None),
            media_url=record.get('media_url', None),
        )
        logger.info(f"已从历史记录重新下载: {title}")

    def _on_history_record_deleted(self, record: dict):
        """历史记录删除回调（预留扩展）"""
        logger.info(f"历史记录已删除: {record.get('filename', '')}")
    
    def _on_batch_import_requested(self, urls: list):
        """处理批量导入 URL"""
        from core.engine_selector import EngineSelector
        from datetime import datetime
        import urllib.parse

        selector = EngineSelector(self.engines)
        user_engine = self.get_selected_engine()

        for url in urls:
            if url.startswith("magnet:"):
                try:
                    parsed = urllib.parse.urlparse(url)
                    params = urllib.parse.parse_qs(parsed.query)
                    name = params.get('dn', ['磁力下载任务'])[0]
                except Exception:
                    name = "磁力下载任务"

                resource = M3U8Resource(
                    url=url,
                    headers={},
                    page_url="",
                    title=name,
                    timestamp=datetime.now()
                )
                self.resource_panel.add_resource(resource, engine_name='Aria2')
                continue

            resource = M3U8Resource(
                url=url,
                headers={},
                page_url=url,
                title="",
                selected_engine=user_engine,
            )
            _, engine_name = selector.predict(url, user_engine)
            self.resource_panel.add_resource(resource, engine_name)

        self.main_tabs.setCurrentIndex(1)
        self.statusBar().showMessage(f"已批量导入 {len(urls)} 条链接", 3000)

    def _on_select_path(self):
        """选择下载路径"""
        from PyQt6.QtWidgets import QFileDialog
        
        current_dir = config.get("download_dir")
        new_dir = QFileDialog.getExistingDirectory(
            self,
            "选择下载路径",
            current_dir
        )
        
        if new_dir:
            config.set("download_dir", new_dir)
            self.path_display.setText(new_dir)
            logger.info(f"下载路径已更新: {new_dir}")
    
    def _on_open_folder(self):
        """打开下载文件夹"""
        import os
        import subprocess
        import platform
        
        download_dir = config.get("download_dir")
        
        # 确保目录存在
        os.makedirs(download_dir, exist_ok=True)
        
        # 根据操作系统打开文件夹
        try:
            if platform.system() == 'Windows':
                os.startfile(download_dir)
            elif platform.system() == 'Darwin':  # macOS
                subprocess.run(['open', download_dir])
            else:  # Linux
                subprocess.run(['xdg-open', download_dir])
            logger.info(f"已打开下载文件夹: {download_dir}")
        except Exception as e:
            logger.error(f"打开文件夹失败: {e}")
    
    
    def _on_thread_count_changed(self, value):
        """线程数变更"""
        config.set("engines.n_m3u8dl_re.thread_count", value)
        logger.info(f"线程数已更新: {value}")
    
    def _on_retry_count_changed(self, value):
        """重试次数变更"""
        config.set("engines.n_m3u8dl_re.retry_count", value)
        logger.info(f"重试次数已更新: {value}")
    
    def _on_concurrent_changed(self, value):
        """并发下载数变更"""
        config.set("max_concurrent_downloads", value)
        self.download_manager.set_max_concurrent(value)
    
    def _on_speed_limit_changed(self, value):
        """限速变更"""
        config.set("speed_limit", value)
        if value == 0:
            logger.info("已取消限速")
        else:
            logger.info(f"限速已设置: {value} KB/s")

    def retranslate_ui(self):
        """刷新界面文字"""
        self.setWindowTitle(TR("app_title"))
        
        # 标签页
        self.main_tabs.setTabText(0, TR("tab_browser"))
        self.main_tabs.setTabText(1, TR("tab_resources"))
        self.main_tabs.setTabText(2, TR("tab_downloading"))
        
        # 工具栏
        self.address_bar.setPlaceholderText(TR("placeholder_url"))
        self.grab_btn.setText(TR("btn_start_grab"))
        self.engine_label.setText(TR("strategy_label"))
        
        # 引擎选择器选项
        current_idx = self.engine_selector.currentIndex()
        self.engine_selector.clear()
        self.engine_selector.addItems([
            TR("strategy_auto"),
            "N_m3u8DL-RE",
            "yt-dlp",
            "Streamlink",
            "Aria2"
        ])
        self.engine_selector.setCurrentIndex(current_idx)
        
        # 下载设置面板
        self.pref_group.setTitle(TR("group_download_pref"))
        self.path_label.setText(TR("label_save_path"))
        self.select_path_btn.setText(TR("btn_change_path"))
        self.open_folder_btn.setText(TR("btn_open_folder"))
        
        self.thread_label.setText(TR("label_threads"))
        self.thread_count_spin.setSuffix(TR("suffix_threads"))
        
        self.retry_label.setText(TR("label_retries"))
        self.retry_count_spin.setSuffix(TR("suffix_retries"))
        
        self.concurrent_label.setText(TR("label_concurrent"))
        self.concurrent_spin.setSuffix(TR("suffix_tasks"))
        
        self.speed_label.setText(TR("label_speed_limit"))
        self.speed_limit_spin.setSuffix(TR("suffix_speed"))
        self.speed_limit_spin.setSpecialValueText(TR("speed_unlimited"))
        
        # 底部标签页
        self.bottom_tabs.setTabText(0, TR("tab_logs"))
        self.bottom_tabs.setTabText(1, TR("tab_history"))
        
        # 顶部按钮
        self.language_btn.setText(TR("lang_name"))
        self.manual_link.setText(TR("quick_manual"))
        
        # 保存语言配置
        lang = i18n.get_language()
        if config.get("language") != lang:
            config.set("language", lang)
            logger.info(TR("log_lang_changed", lang=lang))
            
        # 通知子组件
        if hasattr(self, "resource_panel"):
            self.resource_panel.retranslate_ui()
        if hasattr(self, "download_queue"):
            self.download_queue.retranslate_ui()
        if hasattr(self, "log_panel"):
            self.log_panel.retranslate_ui()
        if hasattr(self, "history_panel"):
            self.history_panel.retranslate_ui()
        if hasattr(self, "browser"):
            self.browser.retranslate_ui()

    def _show_manual_dialog(self):
        """显示使用手册 (从外部 Markdown 加载)"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser
        from pathlib import Path
        import html
        
        dialog = QDialog(self)
        lang = i18n.get_language()
        dialog.setWindowTitle(TR("quick_manual"))
        dialog.setMinimumSize(900, 700)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)

        # 快捷脚本列表 (按当前语言翻译说明)
        is_en = (lang == "en")
        quick_scripts = [
            (
                "download_tools.bat",
                "Download tools (N_m3u8DL-RE / yt-dlp) into bin/" if is_en else "下载 N_m3u8DL-RE / yt-dlp 等工具到 bin 目录",
            ),
            (
                "install_extensions.bat",
                "Open Chrome for extension installation" if is_en else "启动固定用户目录的 Chrome，用于安装扩展",
            ),
            (
                "register_protocol.bat",
                "Register m3u8dl:// protocol" if is_en else "注册 m3u8dl:// 协议（猫爪一键回传）",
            ),
            (
                "uninstall_protocol.bat",
                "Unregister protocol" if is_en else "卸载 m3u8dl:// 协议注册",
            ),
            (
                "clean_cache.bat",
                "Clean cache files" if is_en else "执行缓存清理脚本",
            ),
        ]

        quick_links_html = []
        for script_name, script_desc in quick_scripts:
            quick_links_html.append(
                f"""
                <div style="margin-bottom: 12px;">
                    <a href="script:///{script_name}" style="text-decoration:none; font-weight:700; color:#1f4e79;">{html.escape(script_name)}</a><br>
                    <span style="color:#66717d;">{"Description" if is_en else "说明"}：{html.escape(script_desc)}</span>
                </div>
                """
            )

        # 加载外部文本
        manual_path = Path(__file__).parent.parent / "resources" / f"manual_{lang}.md"
        manual_text = ""
        if manual_path.exists():
            try:
                with open(manual_path, "r", encoding="utf-8") as f:
                    manual_text = f.read()
            except Exception as e:
                manual_text = f"Error loading manual: {e}"
        else:
            manual_text = f"Manual file not found: {manual_path}"

        manual_html = f"""
        <html>
          <body style="font-family:'Microsoft YaHei','Segoe UI',sans-serif; color:#243447;">
            <div style="background:#faf7f1; border:1px solid #e4ddd0; border-radius:8px; padding:12px 14px; margin-bottom:14px;">
              <div style="font-weight:700; font-size:16px; color:#17324d; margin-bottom:6px;">{TR("quick_manual")} - Scripts</div>
              {''.join(quick_links_html)}
            </div>
            <pre style="white-space:pre-wrap; font-family:'Consolas','Microsoft YaHei UI','Segoe UI',sans-serif; font-size:13px; line-height:1.55; margin:0;">{html.escape(manual_text)}</pre>
          </body>
        </html>
        """

        manual_view = QTextBrowser()
        manual_view.setReadOnly(True)
        manual_view.setOpenLinks(False)
        manual_view.setOpenExternalLinks(False)
        manual_view.setHtml(manual_html)
        manual_view.anchorClicked.connect(self._handle_manual_browser_link)
        layout.addWidget(manual_view, 1)

        dialog.setLayout(layout)
        dialog.exec()

    def _handle_manual_browser_link(self, url):
        """处理手册中的可点击链接。"""
        if url.scheme() == "script":
            script_name = url.fileName() or url.path().lstrip("/")
            if script_name:
                self._run_quick_manual_script(script_name)

    def _run_quick_manual_script(self, script_name: str):
        """Run one .bat script from scripts/ (fallback to project root)."""
        project_root = Path(__file__).parent.parent
        script_path = project_root / "scripts" / script_name
        if not script_path.exists():
            script_path = project_root / script_name
        if not script_path.exists():
            QMessageBox.warning(self, "文件不存在", f"未找到脚本：\n{script_path}")
            return

        try:
            import os
            import subprocess
            import platform

            if platform.system() == "Windows":
                os.startfile(str(script_path))
            else:
                subprocess.Popen([str(script_path)], cwd=str(script_path.parent))

            logger.info(f"[MANUAL] 已启动脚本: {script_name}")
            self.statusBar().showMessage(f"已启动脚本: {script_name}", 3000)
        except Exception as e:
            logger.error(f"[MANUAL] 启动脚本失败: {script_name}, 错误: {e}")
            QMessageBox.critical(self, "启动失败", f"脚本启动失败：\n{e}")
    
    def closeEvent(self, event):
        """窗口关闭事件"""
        logger.info(TR("log_closing"))
        self.catcatch_server.stop()
        self.download_manager.shutdown()
        event.accept()
    
    def _on_catcatch_download(self, url: str, headers: dict, filename: str):
        """处理来自猫爪插件的下载请求"""
        logger.info(f"[CatCatch] 收到下载请求: {url}")
        
        # 创建资源对象
        resource = M3U8Resource(
            url=url,
            headers=headers,
            page_url=url,
            title=filename or "CatCatch Download"
        )
        
        # 添加到资源面板 - 使用用户预选的引擎
        from core.engine_selector import EngineSelector
        selector = EngineSelector(self.engines)
        user_engine = self.get_selected_engine()  # 获取用户预选的引擎
        resource.selected_engine = user_engine
        _, engine_name = selector.select(url, user_engine)
        self.resource_panel.add_resource(resource, engine_name)
        
        # 切换到资源标签页
        self.main_tabs.setCurrentIndex(1)
        
        # 显示通知
        self.statusBar().showMessage(f"收到猫爪下载请求: {filename or url[:50]}...", 5000)
