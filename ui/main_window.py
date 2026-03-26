"""
Main application window integrating all components
"""
import html
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QSplitter, QLineEdit, QComboBox, QLabel, QPushButton,
                             QMessageBox, QDialog, QTextBrowser, QFrame)
from PyQt6.QtCore import Qt, QTimer
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


class MainWindow(QMainWindow):
    """主应用窗口"""
    
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
        
        # 初始化组件
        self._init_engines()
        self._init_core_components()
        self._init_ui()
        self._connect_signals()
        
        logger.info("应用启动成功")
    
    def _init_engines(self):
        """初始化下载引擎"""
        self.engines = []
        
        try:
            # N_m3u8DL-RE
            n_m3u8dl_path = config.get("engines.n_m3u8dl_re.path")
            if Path(n_m3u8dl_path).exists():
                self.engines.append(N_m3u8DL_RE_Engine(n_m3u8dl_path))
                logger.info("[OK] N_m3u8DL-RE 引擎已加载")
            else:
                logger.warning(f"[NOT FOUND] N_m3u8DL-RE 未找到: {n_m3u8dl_path}")
            
            # yt-dlp
            ytdlp_path = config.get("engines.ytdlp.path")
            if Path(ytdlp_path).exists():
                ytdlp_engine = YtdlpEngine(ytdlp_path)
                self.engines.append(ytdlp_engine)
                self.ytdlp_engine = ytdlp_engine  # 保存引用，用于后续设置 cookie_exporter
                logger.info("[OK] yt-dlp 引擎已加载")
            else:
                logger.warning(f"[NOT FOUND] yt-dlp 未找到: {ytdlp_path}")
            
            # Streamlink
            streamlink_path = config.get("engines.streamlink.path")
            if Path(streamlink_path).exists():
                self.engines.append(StreamlinkEngine(streamlink_path))
                logger.info("[OK] Streamlink 引擎已加载")
            else:
                logger.warning(f"[NOT FOUND] Streamlink 未找到: {streamlink_path}")
            
            # Aria2
            aria2_path = config.get("engines.aria2.path")
            if Path(aria2_path).exists():
                self.engines.append(Aria2Engine(aria2_path))
                logger.info("[OK] Aria2 引擎已加载")
            else:
                logger.warning(f"[NOT FOUND] Aria2 未找到: {aria2_path}")
            
            # FFmpeg (后处理)
            ffmpeg_path = config.get("engines.ffmpeg.path")
            if Path(ffmpeg_path).exists():
                self.ffmpeg = FFmpegProcessor(ffmpeg_path)
                logger.info("[OK] FFmpeg 已加载")
            else:
                self.ffmpeg = None
                logger.warning(f"[NOT FOUND] FFmpeg 未找到: {ffmpeg_path}")
            
            if not self.engines:
                QMessageBox.warning(
                    self,
                    "警告",
                    "未找到任何下载引擎！\n请将引擎文件放入 bin/ 目录"
                )
        except Exception as e:
            logger.error(f"引擎初始化失败: {e}")
    
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
        self.grab_btn = QPushButton("开始探测")
        self.grab_btn.setFixedWidth(84)
        self.grab_btn.clicked.connect(self._on_address_entered)

        # 引擎选择器
        engine_label = QLabel("下载策略")
        self.engine_selector = QComboBox()
        self.engine_selector.addItems([
            "自动选择",
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
        browser_toolbar.addWidget(engine_label)
        browser_toolbar.addWidget(self.engine_selector)
        
        # 浏览器视图
        self.browser = BrowserView(self.sniffer)
        
        # 连接 yt-dlp 的 cookie_exporter 回调到浏览器
        if hasattr(self, 'ytdlp_engine') and self.ytdlp_engine:
            self.ytdlp_engine.cookie_exporter = self.browser.export_cookies_to_file
        
        browser_layout.addWidget(browser_toolbar_card)
        browser_layout.addWidget(self.browser)
        
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
        
        settings_group = QGroupBox("下载偏好")
        settings_layout = QVBoxLayout()  # 改为垂直布局
        settings_layout.setSpacing(10)
        settings_layout.setContentsMargins(15, 15, 15, 15)
        
        # **第一行：下载路径**
        path_row = QHBoxLayout()
        path_row.setSpacing(10)
        
        path_label = QLabel("保存位置")
        self.path_display = QLabel(config.get("download_dir"))
        self.path_display.setObjectName("path_display")

        self.select_path_btn = QPushButton("更改位置")
        self.select_path_btn.setObjectName("secondary_button")
        self.select_path_btn.clicked.connect(self._on_select_path)

        self.open_folder_btn = QPushButton("打开目录")
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
        thread_label = QLabel("线程数")
        self.thread_count_spin = QSpinBox()
        self.thread_count_spin.setRange(1, 128)
        self.thread_count_spin.setValue(config.get("engines.n_m3u8dl_re.thread_count", 8))
        self.thread_count_spin.setSuffix(" 线程")
        self.thread_count_spin.setMinimumWidth(120)
        self.thread_count_spin.valueChanged.connect(self._on_thread_count_changed)
        params_row.addWidget(thread_label)
        params_row.addWidget(self.thread_count_spin)
        
        # 重试次数
        retry_label = QLabel("重试次数")
        self.retry_count_spin = QSpinBox()
        self.retry_count_spin.setRange(0, 50)
        self.retry_count_spin.setValue(config.get("engines.n_m3u8dl_re.retry_count", 5))
        self.retry_count_spin.setSuffix(" 次")
        self.retry_count_spin.setMinimumWidth(100)
        self.retry_count_spin.valueChanged.connect(self._on_retry_count_changed)
        params_row.addWidget(retry_label)
        params_row.addWidget(self.retry_count_spin)
        
        # 并发下载数
        concurrent_label = QLabel("并发任务")
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 10)
        self.concurrent_spin.setValue(config.get("max_concurrent_downloads", 2))
        self.concurrent_spin.setSuffix(" 任务")
        self.concurrent_spin.setMinimumWidth(100)
        self.concurrent_spin.valueChanged.connect(self._on_concurrent_changed)
        params_row.addWidget(concurrent_label)
        params_row.addWidget(self.concurrent_spin)
        
        # 限速（MB/s，0表示不限速）
        speed_label = QLabel("限速")
        self.speed_limit_spin = QSpinBox()
        self.speed_limit_spin.setRange(0, 100)  # 0-100MB/s
        self.speed_limit_spin.setValue(config.get("speed_limit", 3))
        self.speed_limit_spin.setSuffix(" M/s")
        self.speed_limit_spin.setSpecialValueText("不限速")
        self.speed_limit_spin.setMinimumWidth(120)
        self.speed_limit_spin.valueChanged.connect(self._on_speed_limit_changed)
        params_row.addWidget(speed_label)
        params_row.addWidget(self.speed_limit_spin)
        
        params_row.addStretch()  # 添加弹性空间
        
        settings_layout.addLayout(params_row)
        
        settings_group.setLayout(settings_layout)
        download_layout.addWidget(settings_group)
        
        
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
        
        self.log_history_tabs = QTabWidget()
        self.log_panel = LogPanel()
        self.history_panel = HistoryPanel()
        
        self.log_history_tabs.addTab(self.log_panel, "运行日志")
        self.log_history_tabs.addTab(self.history_panel, "下载历史")
        
        download_splitter.addWidget(self.log_history_tabs)
        
        # 设置初始比例（下载队列:日志 = 1:1）
        download_splitter.setSizes([300, 300])
        
        # 设置最小尺寸
        self.download_queue.setMinimumHeight(100)
        self.log_history_tabs.setMinimumHeight(100)
        
        download_layout.addWidget(download_splitter, stretch=1)
        
        # 添加所有标签页
        self.main_tabs.addTab(browser_tab, "浏览器工作台")
        self.main_tabs.addTab(detection_tab, "资源列表")
        self.main_tabs.addTab(download_tab, "下载中心")

        # 标签栏右侧入口：语言切换 + 使用手册（纯文字，保持原有角落布局）
        self.current_language = "zh"

        manual_container = QWidget()
        manual_layout = QHBoxLayout(manual_container)
        manual_layout.setContentsMargins(0, 0, 12, 8)  # 右侧留白，并与下方横线拉开距离
        manual_layout.setSpacing(8)

        self.language_link = QLabel("EN")
        self.language_link.setObjectName("manual_link")
        self.language_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.language_link.mousePressEvent = lambda ev: self._toggle_language()

        self.manual_link = QLabel("快速手册")
        self.manual_link.setObjectName("manual_link")
        self.manual_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manual_link.mousePressEvent = lambda ev: self._show_manual_dialog()

        manual_layout.addWidget(
            self.language_link,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )
        manual_layout.addWidget(
            self.manual_link,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
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
        
        # 确定将使用的引擎
        from core.engine_selector import EngineSelector
        selector = EngineSelector(self.engines)
        _, engine_name = selector.select(resource.url, user_engine)
        
        # 添加到资源面板
        self.resource_panel.add_resource(resource, engine_name)
    
    def _on_download_requested(self, resource: M3U8Resource):
        """用户请求下载 - 使用当前引擎选择器的值"""
        download_url = resource.url
        headers = resource.headers.copy() if resource.headers else {}
        
        # 获取用户当前选择的引擎（而不是资源添加时的引擎）
        user_engine = self.get_selected_engine()
        logger.info(f"用户当前选择的引擎: {user_engine or '自动选择'}")
        
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
        progress = QProgressDialog("正在获取视频格式...", "取消", 0, 0, self)
        progress.setWindowTitle("请稍候")
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
        progress = QProgressDialog("正在分析 M3U8 播放列表...", "取消", 0, 0, self)
        progress.setWindowTitle("请稍候")
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
    
    def _on_task_update(self, task: DownloadTask):
        """任务更新回调"""
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
                title=""
            )
            _, engine_name = selector.select(url, user_engine)
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

    def _toggle_language(self):
        """切换手册语言显示。"""
        self.current_language = "en" if getattr(self, "current_language", "zh") == "zh" else "zh"
        self.language_link.setText("中文" if self.current_language == "en" else "EN")
        self.manual_link.setText("Quick Manual" if self.current_language == "en" else "快速手册")

    def _show_manual_dialog(self):
        """显示使用手册"""
        dialog = QDialog(self)
        is_en = getattr(self, "current_language", "zh") == "en"
        dialog.setWindowTitle("Quick Manual" if is_en else "使用手册")
        dialog.setMinimumSize(900, 700)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)

        # 快捷脚本列表（与正文合并到同一个滚动区域）
        quick_scripts = [
            (
                "download_tools.bat",
                "Download N_m3u8DL-RE / yt-dlp and other tools into the bin directory (recommended for first-time setup)." if is_en else "下载 N_m3u8DL-RE / yt-dlp 等工具到 bin 目录（首次部署建议先运行）。",
            ),
            (
                "install_extensions.bat",
                "Launch Chrome with the fixed user profile directory for installing media-sniffing extensions." if is_en else "启动固定用户目录的 Chrome，便于安装抓流相关扩展。",
            ),
            (
                "register_protocol.bat",
                "Register the m3u8dl:// protocol so CatCatch or external browsers can send tasks back with one click." if is_en else "注册 m3u8dl:// 协议，供猫爪或外部浏览器一键回传任务。",
            ),
            (
                "uninstall_protocol.bat",
                "Unregister the m3u8dl:// protocol handler." if is_en else "卸载 m3u8dl:// 协议注册。",
            ),
            (
                "一键清理缓存.bat",
                "Run the cache cleanup script first when troubleshooting abnormal behavior." if is_en else "执行缓存清理脚本，排查异常时建议先运行。",
            ),
        ]

        if is_en:
            manual_text = """M3U8 Video Sniffer Quick Manual

This is the current minimum English manual used for language switching.

1. Main tabs
- Browser Workspace: open pages and sniff resources.
- Resource List: review detected links and start downloads.
- Download Center: monitor queue, logs, and history.

2. Basic workflow
- Open a target video page.
- Play the video in the browser.
- Wait for resources to appear in the resource list.
- Click download and choose format/quality when prompted.

3. Download engines
- Auto Select
- N_m3u8DL-RE
- yt-dlp
- Streamlink
- Aria2

4. Quick scripts
Use the links above to run helper scripts such as tool download, protocol registration, or cache cleanup.

5. Notes
- Google Chrome is still important for the browser/sniffing workflow.
- For page-based sites, yt-dlp is usually preferred.
- For m3u8 streams, N_m3u8DL-RE is usually preferred.
- If downloading fails, check the runtime log first.
"""
        else:
            manual_text = """M3U8 Video Sniffer 详细使用手册（按当前程序实际代码整理）

================================================================================
1. 程序定位与整体结构
================================================================================
本程序是一个基于 PyQt6 的桌面端视频嗅探与下载工具，核心目标是：
    1. 用独立浏览器打开真实页面，保留登录态、扩展和正常网页交互。
    2. 在播放过程中自动发现 m3u8 / mpd / mp4 / webm / 磁力链接等候选资源。
    3. 将资源放入资源列表，供用户筛选、选择清晰度、指定引擎、加入下载队列。
    4. 用多种下载引擎执行任务，并记录日志、历史和失败重试信息。

程序入口与主链路如下：
    - main.py
      负责启动 QApplication、创建主窗口、解析 --url / --headers / --filename 参数。

    - ui/main_window.py
      负责把浏览器页、资源页、下载管理页拼装成主界面，并连接所有信号。

    - core/playwright_driver.py
      负责启动持久化 Chrome、监听页面导航、网络请求、响应和前端脚本回传结果。

    - core/m3u8_sniffer.py
      负责统一接收发现到的资源、规范化 headers、应用站点规则、去重、合并上下文。

    - ui/resource_panel.py
      负责显示资源表格、过滤、批量操作、M3U8 后台解析与变体展示。

    - core/download_manager.py
      负责队列、并发、重试、回退、HLS 预探测、状态流转和下载指标。

    - engines/*.py
      负责把任务分别交给 N_m3u8DL-RE、yt-dlp、Streamlink、Aria2 执行。

    - ui/download_queue.py / ui/log_panel.py / ui/history_panel.py
      分别负责任务队列显示、关键日志显示、下载历史记录与重下。

    - core/catcatch_server.py + protocol_handler.pyw
      负责与浏览器扩展、系统协议联动，把外部请求送进程序。


================================================================================
2. 启动方式与外部参数
================================================================================
2.1 正常启动
    最常见启动方式：
        python main.py

    启动后程序会：
        1. 创建主窗口。
        2. 初始化已配置的下载引擎。
        3. 初始化下载管理器与资源嗅探器。
        4. 启动本地 CatCatch HTTP 服务。
        5. 显示浏览器 / 资源列表 / 下载中心三大页签。

2.2 命令行参数启动
    当前入口实际支持以下参数：
        --url
            外部传入的视频或页面 URL。

        --headers
            JSON 字符串，格式通常为：
            {"referer":"...","user-agent":"...","cookie":"..."}

        --filename
            传入资源的默认文件名或标题。

    实际执行逻辑：
        1. 先正常启动主界面。
        2. 如果带了 --url，则 500ms 后构造一个 M3U8Resource。
        3. 程序根据 URL 自动选择引擎。
        4. 资源会直接加入“资源列表”页。
        5. 界面自动切换到资源列表页，等待用户继续下载。

    适用场景：
        - 协议处理器启动程序时回传链接。
        - 外部脚本把解析好的链接交给程序。
        - 调试时手工向 GUI 注入一个资源。

2.3 启动时自动设置的浏览器参数
    程序会给 Qt WebEngine/浏览器环境追加：
        --disable-blink-features=AutomationControlled

    目的：
        降低网页对自动化环境的识别概率，提高登录、播放、抓流成功率。


================================================================================
3. 主界面总览
================================================================================
主界面当前分为 3 个主标签页：
    1. 浏览器工作台
    2. 资源列表
    3. 下载中心

右上角还有一个“快速手册”入口，用于打开当前这份手册。

3.1 浏览器工作台页的组成
    顶部工具栏包含：
        - 后退按钮
        - 前进按钮
        - 刷新按钮
        - 新建标签按钮
        - 地址栏
        - 开始探测按钮
        - 下载策略下拉框

    下方内容分为左右两块：
        左侧是浏览器控制卡片，提供：
            - 启动浏览器
            - 停止浏览器
            - 当前说明文字

        右侧是驱动日志区，用来显示浏览器驱动运行时状态。

3.2 资源列表页的组成
    页面主要包括：
        - 页面标题与简介
        - 下载所选 / 移除所选 / 清空列表
        - 搜索框
        - 类型过滤
        - 来源过滤
        - 清晰度过滤
        - 资源表格

    资源表格当前列为：
        1. 文件名
        2. 类型
        3. 清晰度
        4. 来源域名（当前实际显示的是来源 URL / page_url 文本）
        5. 使用引擎
        6. 检测时间
        7. 操作（下载按钮）

3.3 下载中心页的组成
    页面包含两大区域：
        A. 下载偏好
            - 保存位置
            - 更改位置
            - 打开目录
            - 线程数
            - 重试次数
            - 并发任务
            - 限速

        B. 下方可拖动分割区域
            - 上半部分：下载队列
            - 下半部分：运行日志 / 下载历史（标签页切换）


================================================================================
4. 浏览器工作台：功能、实现方法、使用方法
================================================================================
4.1 浏览器启动机制
    实现方式：
        浏览器并不是嵌在 Qt 窗口里的网页控件，而是由 Playwright 启动一个真实的持久化 Chrome。
        其核心在 core/playwright_driver.py。

    主要特性：
        - 使用持久化用户数据目录保存登录态和 Cookie。
        - 启动时尽量清理 SingletonLock 等残留锁文件。
        - 监听 page、request、response、download、console 等事件。
        - 支持多标签页，用户在 Chrome 中新开页面后会自动配置嗅探逻辑。

    使用方法：
        1. 打开“浏览器工作台”。
        2. 点击“启动浏览器”。
        3. 等待驱动日志出现“浏览器已就绪”。
        4. 再进行页面访问、登录、播放等操作。

4.2 地址栏与开始探测
    实现方式：
        地址栏回车和“开始探测”按钮都会触发同一逻辑：
            - 如果输入是普通网址，则交给 Playwright 导航。
            - 如果浏览器未启动，则先启动，再自动导航。
            - 如果输入缺少 http/https，程序会自动补成 https://。

    使用方法：
        1. 在地址栏输入站点页面地址。
        2. 回车或点“开始探测”。
        3. 页面会在外部浏览器中打开。
        4. 在页面中点击播放、切换清晰度、登录等真实操作。

4.3 后退 / 前进 / 刷新 / 新建标签
    当前代码状态：
        - “新建标签”按钮当前会调用 add_new_tab()，其实现是让浏览器加载一个新地址。
        - back() / forward() / reload() 这些兼容方法在 BrowserView 中仍是空实现占位。

    这意味着：
        - “新建标签”可以工作，但本质上是发起导航请求。
        - 后退、前进、刷新按钮的外观已存在，但当前不属于完整实现状态。

4.4 下载策略下拉框
    下拉项实际为：
        - 自动选择
        - N_m3u8DL-RE
        - yt-dlp
        - Streamlink
        - Aria2

    它的作用不是立刻下载，而是：
        在“资源点击下载”时，告诉程序优先用哪个引擎。

    具体规则：
        - 若所选引擎能处理该 URL，则优先使用用户指定引擎。
        - 若所选引擎不能处理，程序会回退到自动选择。
        - 自动选择优先级见第 8 节。

4.5 浏览器嗅探是怎么实现的
    当前代码实际使用的是 Playwright 嗅探链路，主要不是 QWebEngine 拦截器。

    PlaywrightDriver 里有 4 条主要发现路径：
        1. 页面导航命中视频页面模式
           例如 YouTube/B站/TikTok/Instagram/Twitch 等页面 URL 规则。
           命中后，程序会把页面 URL 当作一个 yt-dlp 可处理资源加入列表。

        2. request 事件
           拦截浏览器发出的网络请求，只要 URL 像视频流，就记录下来。

        3. response 事件
           当 URL 本身不像视频，但响应头 Content-Type 显示为 HLS/MP4/WebM 等时，也会记录。

        4. 前端注入脚本 + console 回传
           页面中注入 sniffer_script，在播放时主动把前端发现的媒体地址输出到控制台，
           Python 侧再接收并入库。

    此外还有“捕获窗口”机制：
        - 导航、检测到播放、命中媒体链接时，会开启一段持续探测时间。
        - 在这段时间里定期扫描 video 标签、source 标签、performance 资源列表。
        - 适合抓取延迟出现、动态注入、切清晰度后才出现的媒体链接。

4.6 浏览器页的实际使用建议
    推荐顺序：
        1. 启动浏览器。
        2. 在外部浏览器登录目标网站。
        3. 播放视频，必要时切换清晰度。
        4. 回到程序看“资源列表”是否出现候选资源。
        5. 若站点是 YouTube/B站/TikTok 等页面型站点，通常会直接出现页面资源。
        6. 若站点是 HLS 流媒体站点，通常会出现 m3u8 资源或其变体资源。

4.7 磁力链接的特殊处理
    实现方式：
        在地址栏输入 magnet:? 开头链接时，不会让浏览器导航，而是直接构造一个资源项，
        强制使用 Aria2 作为建议引擎。

    使用方法：
        1. 直接把磁力链接粘贴到地址栏。
        2. 回车。
        3. 资源会进入资源列表。
        4. 再点击下载即可。


================================================================================
5. 资源列表：功能、实现方法、使用方法
================================================================================
5.1 资源是如何进入列表的
    资源进入列表后会经过以下处理：
        1. 由 M3U8Sniffer.add_resource() 统一接收。
        2. 对 m3u8 请求头进行标准化：
            - header 名统一转小写
            - 自动补 referer
            - 自动补 user-agent
            - 尝试从 referer 推出 origin
        3. 如果启用站点规则，则用 site_rules 自动补头。
        4. 根据 URL、标题、平台特征执行去重。
        5. 计算候选分值 candidate_score，用于后续下载侧优选链接。
        6. 通过 on_resource_found 回调交给主窗口和资源表格显示。

5.2 资源去重逻辑
    当前代码的去重不是简单按 URL 一刀切，而是多层策略：
        - 同 URL 资源会合并上下文，而不是一定重复插入。
        - YouTube 等平台会按视频 ID、itag、标题做额外去重。
        - M3U8 master 和 media playlist 会分别构造键值。
        - M3U8 变体资源按 height / bandwidth / variant_url 区分。

    作用：
        尽量避免同一视频因为多个 CDN、重复请求、页面刷新而刷满列表。

5.3 搜索与过滤
    当前可用过滤项：
        - 搜索标题、URL、来源文本
        - 类型过滤
        - 来源过滤
        - 清晰度过滤

    使用方法：
        1. 在搜索框输入关键词，可搜标题、tooltip 中的完整 URL、来源文本。
        2. 通过“全部类型 / M3U8 / MPD / MP4 ...”缩小范围。
        3. 通过来源过滤定位某个页面来源。
        4. 通过 2160 / 1080 / 720 / 音频 等选项筛选清晰度。

5.4 下载所选 / 移除所选 / 清空列表
    下载所选：
        对当前选中的多行逐条执行下载逻辑。

    移除所选：
        从 UI 列表和内部资源数组中移除选中资源，并重建去重缓存。

    清空列表：
        清空资源表格、去重缓存、page_url 映射和过滤条件。

5.5 M3U8 自动解析与变体展开
    当列表里加入一个主 m3u8 资源时，ResourcePanel 会自动启动后台线程 M3U8FetchThread：
        - 下载 m3u8 内容。
        - 判断是不是 master playlist。
        - 解析 #EXT-X-STREAM-INF。
        - 递归解析嵌套 master playlist（受 m3u8_nested_depth 限制）。
        - 生成各分辨率变体。

    解析完成后会发生两件事：
        1. 更新原始那一行的“清晰度”列，例如：1080p/720p/480p。
        2. 自动把各个变体作为新的资源行加到表格中，标题会带上 [1080p] 之类后缀。

    这意味着：
        用户既可以从原始 master 入口下载，也可以直接点某个具体分辨率变体下载。

5.6 点击下载后的分流逻辑
    用户在资源列表里点“下载”后，主窗口会按实际代码执行以下判断：
        A. 如果是 yt-dlp 支持的平台页面
            - 优先使用 page_url，而不是 CDN 片段地址。
            - 弹出格式选择对话框。
            - 用户可选具体 format_id，或直接选“最佳质量”。

        B. 如果是 .m3u8
            - 弹出 M3U8 清晰度选择对话框。
            - 优先复用资源列表已经缓存的 variants，避免重复请求。
            - 若当前引擎是 N_m3u8DL-RE，会尽量传 master_url + selected_variant。
            - 若是其他引擎，直接使用选中的变体 URL 下载。

        C. 其他资源
            - 直接创建下载任务并入队。

5.7 yt-dlp 格式选择对话框怎么用
    实现方式：
        调用 yt-dlp -J 获取格式列表，再用 ui/format_dialog.py 弹窗显示。

    对话框当前特性：
        - 主要显示 720p 及以上格式。
        - 显示列：ID / 分辨率 / 格式 / 编码 / 大小。
        - 可双击一行直接确认。
        - 可点击“最佳质量”。

    使用方法：
        1. 点击支持站点资源的“下载”。
        2. 等待程序获取格式。
        3. 选择一行具体清晰度，点击“确认下载”，或直接点“最佳质量”。

5.8 M3U8 清晰度选择对话框怎么用
    使用方法：
        1. 点击 m3u8 资源的“下载”。
        2. 如果程序已缓存变体，会直接弹窗；否则后台先分析播放列表。
        3. 选定某个分辨率后，程序开始创建下载任务。


================================================================================
6. 下载中心：功能、实现方法、使用方法
================================================================================
6.1 保存位置
    对应配置项：
        download_dir

    实现方式：
        - 界面上显示当前下载目录。
        - “更改位置”会打开目录选择对话框。
        - “打开目录”会直接用系统资源管理器打开目录。
        - 修改后会立刻写回 config.json。

    使用方法：
        1. 进入下载中心。
        2. 点击“更改位置”。
        3. 选择目录。
        4. 后续新任务将默认保存到此目录。

6.2 线程数
    对应配置项：
        engines.n_m3u8dl_re.thread_count

    作用：
        主要影响 N_m3u8DL-RE 的单任务下载线程数。

    注意：
        - 这是单个任务的线程数，不是同时下载的任务数。
        - 对 yt-dlp / Streamlink 不直接等价生效。

    建议：
        - 网络正常时可适当提高。
        - 某些站点过高线程可能导致 403、限流或不稳定。

6.3 重试次数
    对应两个层面：
        A. 界面上的重试次数
            engines.n_m3u8dl_re.retry_count
            这个值会直接写给 N_m3u8DL-RE 作为引擎内部重试参数。

        B. 下载管理器总重试次数
            max_retry_attempts
            这个值控制 DownloadManager 在任务级别整体最多尝试多少轮。

    区别：
        - 引擎内部重试：某一个引擎命令内部自己重试。
        - 管理器级重试：引擎失败后，任务还可以再来一轮，甚至切换引擎。

6.4 并发任务
    对应配置项：
        max_concurrent_downloads

    实现方式：
        DownloadManager 启动多个后台工作线程，并根据这个值控制同时运行的任务数。

    使用方法：
        - 调大：多个任务可同时下载，但更占带宽和磁盘资源。
        - 调小：更稳定，适合容易 403 或网络不稳的站点。

6.5 限速
    对应配置项：
        speed_limit

    界面含义：
        单位是 MB/s，0 表示不限速。

    实际生效方式：
        - N_m3u8DL-RE：会转换成 --max-speed 参数。
        - yt-dlp：会使用 --limit-rate。
        - Aria2：会使用 --max-download-limit。
        - Streamlink 当前没有按此参数做专门限速处理。

    使用建议：
        - 遇到网络波动、掉线、证书/网关问题时，可适度限速。
        - 多任务下载时适当限速能提升整体稳定性。

6.6 下载队列显示内容
    当前列为：
        - 文件名
        - 状态
        - 进度
        - 速度
        - 引擎

    状态可能包括：
        waiting / downloading / paused / failed / completed

    说明：
        - 直播录制类任务进度可能未知，此时会显示已下载大小或“录制中...”。
        - 状态文字和颜色会随任务状态变化。

6.7 下载队列右键菜单与底部按钮
    当前支持：
        - 暂停
        - 继续
        - 停止
        - 删除
        - 重试
        - 打开位置
        - 暂停全部
        - 清除已完成
        - 按状态排序
        - 批量导入

    右键菜单支持的附加操作：
        - 复制链接
        - 已完成任务可播放文件

    删除任务的实际行为：
        - 通知 DownloadManager 移除任务。
        - 如果进程还在，会终止下载进程。
        - 3 秒后尝试清理临时文件。
        - 不会主动删除已经完成的正式成品文件。

6.8 批量导入
    支持输入：
        - http://
        - https://
        - magnet:

    使用方法：
        1. 点击“批量导入”。
        2. 每行输入一个链接。
        3. 确认后程序会过滤无效项。
        4. 有效项会批量加入资源列表，而不是直接立刻下载。


================================================================================
7. 下载管理核心机制（按实际代码）
================================================================================
7.1 任务对象里实际保存了什么
    每个 DownloadTask 主要包含：
        - url
        - save_dir
        - filename
        - headers
        - status
        - progress
        - speed
        - engine
        - error_message
        - downloaded_size
        - selected_variant
        - master_url
        - media_url
        - candidate_scores
        - retry_count / max_retries
        - stop_requested / stop_reason
        - created_at / started_at / completed_at

    这表示：
        任务不仅保存最终下载地址，还保存来源清晰度、主播放列表地址、失败状态等上下文。

7.2 引擎选择优先级
    自动选择顺序当前为：
        N_m3u8DL-RE -> Streamlink -> Aria2 -> yt-dlp

    处理倾向：
        - N_m3u8DL-RE：更偏向 m3u8 / mpd / HLS / DASH。
        - Streamlink：更偏向直播平台 URL。
        - Aria2：更偏向直链文件和磁力。
        - yt-dlp：作为通用兜底和页面型视频平台下载器。

7.3 任务入队与并发
    实现方式：
        DownloadManager 使用 Queue + 多个 worker 线程。

    流程：
        1. add_task() 把任务状态设为 waiting。
        2. 根据当前设置和用户偏好选出首选引擎。
        3. worker 线程按并发上限取任务执行。

7.4 HLS 预探测
    对应功能开关：
        features.hls_probe_enabled
        features.hls_probe_hard_fail

    实现方式：
        对 m3u8 任务先调用 HLSProbe：
            - 取 playlist
            - 如果是 master，先取第一条变体
            - 检查 key URL
            - 检查第一片 segment 是否能访问

    作用：
        提前发现“playlist 能拿到但 key/ts 拿不到”的问题。

    硬失败开关含义：
        - True：预探测失败就直接判任务失败。
        - False：预探测失败只记日志，仍继续下载流程。

7.5 候选链接优选
    对应功能开关：
        features.download_candidate_ranking_enabled

    作用：
        对 m3u8 任务的 url / media_url / master_url 做评分，选更优的地址作为主下载地址。

    打分倾向：
        - https 加分
        - 带 referer / origin / cookie / authorization 加分
        - 看起来像广告、tracker 的 URL 减分

7.6 重试与回退
    对应功能开关：
        features.download_retry_enabled
        features.download_engine_fallback
        features.download_auth_retry_first
        features.download_auth_retry_per_engine

    实际流程：
        1. 先尝试当前候选引擎。
        2. 若失败，会根据错误文本大致分类为 auth / parse / timeout / unknown。
        3. 如果是鉴权类失败，会先尝试用 site_rules 补头，再在同引擎内重试若干次。
        4. 如果允许回退，则继续尝试其他可用引擎。
        5. 如果允许任务级重试，则整轮失败后还能再来下一轮。
        6. timeout 类失败会按 backoff_seconds 做递增等待。

7.7 任务暂停、继续、取消、删除
    暂停：
        - 标记 stop_requested=True
        - stop_reason=paused
        - 若有外部进程则终止之
        - 状态进入 paused

    继续：
        - 从 paused 列表移除
        - 重新 add_task() 入队

    取消：
        - stop_reason=cancelled
        - 终止进程
        - 状态最终作为 failed 处理

    删除：
        - stop_reason=removed
        - 从管理器状态和队列中移除
        - UI 侧后续会做临时文件清理

7.8 下载指标与自动学习站点规则
    下载指标：
        DownloadManager 内部会累计 success_total / failed_total / by_engine / by_stage。

    自动学习站点规则：
        对应配置项：
            site_rules_auto.enabled
            site_rules_auto.max_rules
            site_rules_auto.allow_cookie

        当该功能开启后，成功任务中的 referer / user-agent / origin / cookie 可被抽取为自动规则，
        以后访问同站点时可自动补头。


================================================================================
8. 各下载引擎说明与使用建议
================================================================================
8.1 N_m3u8DL-RE
    适合：
        - m3u8
        - mpd
        - HLS / DASH
        - 主列表 + 变体清晰度场景

    当前实现特点：
        - 启动前会读一次 --help，探测当前二进制支持哪些参数。
        - 会构造 primary / master / media 多个候选地址依次尝试。
        - 支持 safe mode 回退，处理参数不兼容情况。
        - 支持根据 selected_variant 传 --select-video。
        - 支持限速、线程数、重试次数、输出格式等。

    推荐使用：
        - m3u8 站点优先用它。
        - 想精确选清晰度时优先用它。

8.2 yt-dlp
    适合：
        - YouTube / B站 / TikTok / Instagram / Twitter / Vimeo 等页面型站点
        - 需要页面解析、格式枚举、音视频合并的站点

    当前实现特点：
        - 可先获取格式列表，再让用户选 format_id。
        - 支持读取手工导出的 cookies 文件。
        - 格式或下载失败时会尝试回退到 Firefox cookies。
        - 遇到证书问题会自动加 --no-check-certificates 重试一次。
        - 限速可从全局 speed_limit 继承。

    Cookies 实际使用方式：
        - 程序会根据 URL 推断 cookies 文件名，例如 youtube 对应 cookies/www.youtube.com_cookies.txt。
        - 若该文件存在，会优先使用。
        - 没有时会尝试 Firefox cookies 回退。

8.3 Streamlink
    适合：
        - 直播平台
        - Twitch / Douyu / Huya / B站直播等直播 URL

    当前实现特点：
        - 输出通常保存为 .ts。
        - 没有精确总进度时，会显示已写入大小和速度。
        - 失败时会做简易原因诊断，如 401/403/超时/地理限制等。

8.4 Aria2
    适合：
        - mp4 / flv / webm / ts 等直链文件
        - magnet 磁力链接

    当前实现特点：
        - 支持多连接并行下载。
        - 继承全局限速。
        - 可附带 referer / user-agent / cookie 请求头。

8.5 FFmpeg
    当前代码中的实际定位：
        FFmpegProcessor 已加载，但主界面当前没有单独暴露“转码 / 合并 / 抽字幕 / 压缩”按钮。

    已实现的方法包括：
        - 转封装为 MP4
        - 合并音视频
        - 提取字幕
        - 压缩视频

    说明：
        它现在更像“已接入的后处理能力”，并非主界面高频入口功能。


================================================================================
9. 日志、历史记录、通知
================================================================================
9.1 运行日志
    运行日志面板只显示关键日志或 WARNING / ERROR / CRITICAL。

    显示内容包括：
        - 任务加入队列
        - 开始下载
        - 下载完成
        - 下载失败
        - 回退引擎
        - 收到猫爪请求
        - 配置变更
        - 应用启动/关闭

    使用方法：
        - 当你觉得“点了没反应”时，先看这里。
        - 当下载失败时，先看这里的关键报错，再去日志文件夹查看完整日志。

9.2 下载历史
    存储路径：
        用户目录下：.m3u8sniffer/history.json

    记录内容包括：
        - 文件名
        - URL
        - 状态
        - 大小
        - headers
        - engine
        - save_dir
        - selected_variant
        - master_url
        - media_url
        - completed_at
        - cookie_file（如果当时存在）

    右键菜单支持：
        - 重新下载
        - 打开文件位置
        - 查看相关日志
        - 从历史删除
        - 复制文件名 / 复制 URL / 复制整行

9.3 系统通知
    对应配置项：
        notification_enabled

    当前代码实际行为：
        目前通知函数主要写日志，不真正弹系统通知气泡。
        注释中保留了 plyer 方案，但默认未启用。


================================================================================
10. 外部联动：CatCatch HTTP 与 m3u8dl:// 协议
================================================================================
10.1 CatCatch HTTP 服务
    启动时主窗口会创建 CatCatchServer，并自动启动本地 HTTP 服务。

    端口策略：
        - 优先 9527
        - 若被占用，会尝试 9528 ~ 9539

    接口：
        GET /
            查看服务信息与 endpoints。

        GET /status
            返回运行状态。

        GET /download?url=...&name=...
            简单 GET 方式添加任务。

        POST /download
            可传 JSON 或 form。
            支持字段：url / headers / name / filename

    收到请求后的实际行为：
        1. 先构造一个资源对象。
        2. 加入资源列表。
        3. 切换到资源列表页。
        4. 在状态栏提示已收到下载请求。

10.2 m3u8dl:// 协议处理器
    协议处理脚本为 protocol_handler.pyw。

    它支持解析三类输入：
        1. m3u8dl:"URL" --save-dir ... --save-name ... -H "Header: Value"
        2. m3u8dl://http://example.com/xxx.m3u8
        3. m3u8dl://{"url":"...","headers":{},"name":"..."}

    实际执行流程：
        1. 解析传入协议内容。
        2. 先尝试把任务 POST 到正在运行的主程序。
        3. 若本地程序未运行，则启动 main.py 并传入 --url / --headers / --filename。
        4. 再次尝试投递给 GUI 程序。

    使用场景：
        - 浏览器扩展一键把资源发回桌面程序。
        - 外部脚本或工具调用系统协议。


================================================================================
11. config.json 全部主要设置项与使用方法
================================================================================
11.1 基础目录与全局任务设置
    download_dir
        含义：默认下载目录。
        使用方法：
            - 可在 UI 的“保存位置”直接改。
            - 也可手改 config.json。

    temp_dir
        含义：临时目录，N_m3u8DL-RE 等中间文件会使用它。
        使用方法：
            - 建议放在本地 SSD 路径。
            - 空间不足时可改到其他盘。

    max_concurrent_downloads
        含义：同时运行的任务数。
        使用方法：
            - UI 中“并发任务”会直接改这个值。

    speed_limit
        含义：全局限速，单位 MB/s，0 表示不限速。
        使用方法：
            - UI 中“限速”会直接改这个值。

    max_retry_attempts
        含义：任务级最大重试轮数。
        建议：
            - 稳定站点 1~3 即可。
            - 易掉线站点可适当加大。

    retry_backoff_seconds
        含义：任务级重试之间的等待秒数。
        说明：
            - timeout 类型失败会按此值递增退避。

11.2 site_rules 与自动学习规则
    site_rules
        含义：按域名/关键词自动补 Referer、UA、Cookie、Authorization 等头。

        单条规则常见字段：
            name
            domains
            url_keywords
            referer
            user_agent
            headers

        典型用途：
            - 某站 m3u8 必须带 referer。
            - 某站必须带固定 UA。
            - 某站需要 authorization 或 cookie。

    site_rules_auto.enabled
        含义：是否允许程序从成功任务中自动学习规则。

    site_rules_auto.max_rules
        含义：自动学习最多保留多少条规则。

    site_rules_auto.allow_cookie
        含义：自动学习时是否允许把 Cookie 一并写入规则。
        提醒：
            - 开启后更方便，但也更容易把短期 Cookie 固化进配置。

11.3 features：功能开关逐项说明
    sniffer_rules_enabled
        含义：嗅探阶段是否应用 site_rules 补头。
        建议：一般保持 true。

    sniffer_dedup_enabled
        含义：是否启用资源去重。
        建议：一般保持 true，否则列表容易爆量。

    sniffer_filter_noise
        含义：保留给网络拦截器的噪声过滤开关。
        说明：当前主抓流链路主要是 Playwright，QWebEngine 拦截器不是主路径。

    download_retry_enabled
        含义：是否允许任务级重试。

    download_engine_fallback
        含义：主引擎失败时是否自动换候选引擎再试。

    download_auth_retry_first
        含义：鉴权失败时，是否先在同一个引擎内补头重试。

    download_auth_retry_per_engine
        含义：每个引擎遇到 auth 类失败时，最多做几次同引擎鉴权重试。

    download_candidate_ranking_enabled
        含义：是否对 m3u8 候选链接做评分排序。

    hls_probe_enabled
        含义：是否在正式下载前做 HLS 预探测。

    hls_probe_hard_fail
        含义：HLS 预探测失败时是否直接判定任务失败。

    browser_capture_window_enabled
        含义：是否启用播放后持续探测窗口。

    browser_capture_window_seconds
        含义：一次捕获窗口的基础持续秒数。

    browser_capture_extend_on_hit_seconds
        含义：命中媒体后，额外延长的秒数。

    browser_capture_probe_interval_ms
        含义：捕获窗口期间主动扫描页面的间隔毫秒数。

    ui_batch_actions
        含义：是否显示资源列表的“下载所选 / 移除所选”。

    ui_filter_search
        含义：是否显示资源列表的搜索和筛选栏。

    m3u8_nested_depth
        含义：解析嵌套 master playlist 的最大深度。
        说明：虽然默认配置文件里未必显式写出，但代码支持这个 feature。

11.4 engines.n_m3u8dl_re
    path
        含义：N_m3u8DL-RE 可执行文件路径。

    thread_count
        含义：默认线程数。

    thread_min / thread_max
        含义：自适应线程范围。

    retry_count
        含义：引擎内部重试次数。

    max_retry
        含义：兼容参数，写入 N_m3u8DL-RE 的 --max-retry。

    adaptive
        含义：是否追加 --adaptive（当前二进制支持时才会生效）。

    output_format
        含义：输出格式，例如 mp4。

    force_http1
        含义：若启用，则尝试给 N_m3u8DL-RE 传 --force-http1。

    no_date_info
        含义：若启用，则尝试传 --no-date-info。

11.5 engines.ytdlp / streamlink / aria2 / ffmpeg
    engines.ytdlp.path
        yt-dlp 路径。

    engines.streamlink.path
        Streamlink 路径。

    engines.aria2.path
        Aria2 路径。

    engines.aria2.max_connection_per_server
        单服务器最大连接数。

    engines.aria2.split
        分片数。

    engines.ffmpeg.path
        FFmpeg 路径。

11.6 其他配置项
    notification_enabled
        含义：是否记录通知事件。
        说明：当前主要影响通知函数是否执行日志通知逻辑。

    auto_delete_temp
        含义：配置中存在该项。
        说明：当前主下载流程没有直接按它做统一开关判断，更多是预留/扩展项。

    proxy.enabled / proxy.http / proxy.https
        含义：代理配置项已存在于配置结构中。
        说明：当前主下载和浏览器链路未直接按这些值统一下发代理参数，更多属于预留配置。

    catcatch.port
        含义：若配置中提供，可指定 CatCatchServer 首选端口；默认是 9527。


================================================================================
12. 常见操作示例
================================================================================
12.1 抓网页视频并下载
    1. 打开浏览器工作台。
    2. 点击启动浏览器。
    3. 在地址栏输入视频网站页面 URL。
    4. 在外部 Chrome 中播放视频。
    5. 切到资源列表。
    6. 选择资源，点下载。
    7. 在下载中心查看进度和日志。

12.2 下载 YouTube / B站等页面型视频
    1. 打开对应视频页面。
    2. 等页面型资源进入资源列表。
    3. 点下载。
    4. 在格式选择框中选清晰度或点“最佳质量”。
    5. 若站点要求登录，准备对应 cookies 文件更稳。

12.3 下载 m3u8 多清晰度视频
    1. 让程序抓到 m3u8 资源。
    2. 等待资源列表清晰度列更新。
    3. 选择原始 master 或某个具体变体。
    4. 点下载。
    5. 在弹窗里确认目标清晰度。

12.4 用磁力链接下载
    1. 把 magnet 链接粘贴到地址栏。
    2. 回车。
    3. 资源进入资源列表。
    4. 点击下载，程序通常会使用 Aria2。

12.5 从历史记录重新下载
    1. 打开下载中心。
    2. 切到“下载历史”。
    3. 右键目标记录。
    4. 选择“重新下载”。

12.6 使用 CatCatch 或协议回传
    1. 保持程序运行。
    2. 浏览器扩展把 URL 发给本地 HTTP API，或通过 m3u8dl:// 调起程序。
    3. 资源进入资源列表。
    4. 再由你确认下载。


================================================================================
13. 常见问题与排查建议（结合当前代码行为）
================================================================================
13.1 点了下载没有反应
    先检查：
        - 是否真的进入下载队列。
        - 是否被弹出了格式选择框但你没确认。
        - 是否本地已存在同名文件，程序正在等待是否覆盖。
        - 是否 HLS 预探测失败并被 hls_probe_hard_fail 直接拦截。

13.2 能抓到页面但抓不到资源
    建议：
        - 确认已点击“启动浏览器”。
        - 在外部浏览器里真的播放视频，而不是只停留在详情页。
        - 等待几秒，让捕获窗口进行持续扫描。
        - 必要时切换清晰度或刷新后重试。

13.3 YouTube/B站资源不完整或无法下载
    建议：
        - 尽量让列表中出现页面型资源，再用 yt-dlp 下载。
        - 准备对应网站的 cookies 文件。
        - 若格式获取失败，程序会尝试 Firefox cookies 回退。

13.4 m3u8 下载失败
    常见原因：
        - referer / cookie 不完整
        - key 或 ts 分片无权限
        - 站点对线程数过高敏感

    建议：
        - 配置 site_rules。
        - 降低线程数、并发和限速。
        - 使用 N_m3u8DL-RE 优先尝试。
        - 查看运行日志和 logs 目录下完整日志。

13.5 下载失败后为什么会换引擎
    因为当前默认启用了：
        features.download_engine_fallback = true

    作用：
        一个引擎失败后，程序会尝试其他候选引擎，提高成功率。

13.6 为什么历史记录里还能重下旧任务
    因为历史记录不仅存了 URL，还会尽量保存 headers、engine、save_dir、selected_variant、master_url、media_url 等上下文。


================================================================================
14. 当前代码状态说明（避免误解）
================================================================================
以下内容是“代码已存在但不一定完全在主界面高频使用”的能力：
    - FFmpegProcessor 已加载，但主界面没有独立后处理按钮。
    - QWebEngine NetworkInterceptor 类存在，但当前主抓流主路径是 PlaywrightDriver。
    - auto_delete_temp / proxy 等配置项已存在，但当前主流程未统一完整接管这些项。
    - 浏览器页的后退 / 前进 / 刷新兼容接口仍是占位实现，不属于完整导航控制。

以上说明的目的：
    让你在阅读本手册时，以当前程序实际可见行为和已接入代码为准，而不是把所有预留类都当成已完整对外功能。


================================================================================
15. 建议的日常使用顺序
================================================================================
    1. 先确认 bin 目录下各下载器存在。
    2. 启动程序。
    3. 如需浏览器抓流，先点击“启动浏览器”。
    4. 打开视频页面并播放。
    5. 在资源列表筛选出目标资源。
    6. 根据站点类型选择合适引擎：
        - m3u8 / mpd：优先 N_m3u8DL-RE
        - 页面型平台：优先 yt-dlp
        - 直播：优先 Streamlink
        - 直链 / 磁力：优先 Aria2
    7. 在下载中心观察任务状态、日志和历史。
    8. 如果失败，再根据日志决定是补 cookies、补 referer、改线程数、关限速还是换引擎。
"""

        quick_links_html = []
        for script_name, script_desc in quick_scripts:
            safe_script_name = html.escape(script_name)
            safe_script_desc = html.escape(script_desc)
            quick_links_html.append(
                f"""
                <div style="margin-bottom: 12px;">
                    <a href="script:///{script_name}" style="text-decoration:none; font-weight:700; color:#1f4e79;">{safe_script_name}</a><br>
                    <span style="color:#66717d;">说明：{safe_script_desc}</span>
                </div>
                """
            )

        prereq_text = ("""## ✅ Environment Checklist

> ⚠️ Important: the built-in browser workflow currently depends on a system-installed Google Chrome.
> ⚠️ Installing `playwright install chromium` alone is not an equivalent replacement.
> ⚠️ Without Chrome, browser workspace, web sniffing, cookie/login reuse, and some site download capabilities will be significantly affected.

| Category | Name | Requirement / Version | Required | Notes |
| :--- | :--- | :--- | :--- | :--- |
| Operating System | Windows | Windows 10/11 64-bit | Required | The project is designed for Windows desktop usage. |
| Python Runtime | Python | 3.9 or higher | Required | Used to run `main.py` / `mvs.pyw`. |
| Package Manager | pip | Available and working | Required | Needed for `pip install -r requirements.txt`. |
| Python Dependencies | requirements.txt | Installed successfully | Required | Includes PyQt6 / PyQt6-WebEngine / plyer / requests / playwright. |
| Browser Environment | Google Chrome | Installed and launchable | Required (browser workflow) | The current program depends on system Chrome, not only Playwright Chromium. |
| Download Engine | bin/yt-dlp.exe | Present and executable | Required | Important for page-based and many general sites. |
| Download Engine | bin/N_m3u8DL-RE.exe | Present and executable | Required | Core engine for m3u8 / mpd / HLS / DASH downloads. |
| Download Engine | bin/ffmpeg.exe | Present and executable | Required | Needed for muxing, remuxing, and some post-processing. |
| Download Engine | bin/aria2c.exe | Present and executable | Recommended | Better for direct links, multi-connection downloads, and magnet tasks. |
| Download Engine | bin/streamlink.exe | Present and executable | Recommended | Better for livestream and replay scenarios. |
| Auxiliary Tool | bin/deno.exe | Present and executable | Optional | Not a hard dependency for the main flow, but recommended to keep. |
| Network | Access to GitHub / common video sites | Stable connection recommended | Recommended | Initial dependency install and site parsing rely on connectivity. |
| Disk Space | Local free space | At least 500MB, 2GB+ recommended | Required | Tools, cache, temp files, and merge artifacts all consume space. |
| Browser Extension | CatCatch | Install if needed | Optional | Needed only if you want one-click sending from Chrome/Edge. |

Without Chrome, you may see:
- Browser workspace failing to start
- In-page sniffing not working
- Browser cookie / login-state reuse unavailable
- Real media URLs on some sites not being captured
- Noticeably reduced overall download capability

Conclusion: Google Chrome is not optional for this workflow. It is a key prerequisite for the built-in browser, sniffing success rate, login reuse, and the overall user experience.
""" if is_en else """## ✅ 运行环境总览（请先确认）

> ⚠️ 重要：当前程序的内置浏览器实际依赖系统已安装的 Google Chrome。
> ⚠️ 不是只安装 playwright install chromium 就能替代。
> ⚠️ 如果没有安装 Chrome，浏览器工作台、网页嗅探、Cookie/登录态复用、部分站点下载能力都会明显受影响。

| 类别 | 名称 | 要求/版本 | 是否必需 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| 操作系统 | Windows | Windows 10/11 64 位 | 必需 | 当前项目按 Windows 桌面环境设计。 |
| Python 运行时 | Python | 3.9 或更高版本 | 必需 | 用于运行 main.py / mvs.pyw。 |
| Python 包管理 | pip | 可正常使用 | 必需 | 需要执行 pip install -r requirements.txt 安装依赖。 |
| Python 依赖 | requirements.txt | 需安装完成 | 必需 | 包含 PyQt6 / PyQt6-WebEngine / plyer / requests / playwright。 |
| 浏览器环境 | Google Chrome | 系统已安装且可正常启动 | 强制要求（内置浏览器场景） | 当前程序依赖系统 Chrome，不是 playwright install chromium。 |
| 下载引擎 | bin/yt-dlp.exe | 文件存在且可执行 | 必需 | 页面型站点与大量通用站点下载依赖它。 |
| 下载引擎 | bin/N_m3u8DL-RE.exe | 文件存在且可执行 | 必需 | m3u8 / mpd / HLS / DASH 下载核心引擎。 |
| 下载引擎 | bin/ffmpeg.exe | 文件存在且可执行 | 必需 | 音视频合并、转封装、部分后处理依赖它。 |
| 下载引擎 | bin/aria2c.exe | 文件存在且可执行 | 建议安装 | 直链资源、多连接下载、磁力场景更依赖它。 |
| 下载引擎 | bin/streamlink.exe | 文件存在且可执行 | 建议安装 | 直播流 / 直播回放任务更依赖它。 |
| 辅助工具 | bin/deno.exe | 文件存在且可执行 | 可选 | 当前主流程不是强依赖，但建议保留。 |
| 网络环境 | GitHub / 常见资源站点可访问 | 建议稳定联网 | 建议 | 首次安装依赖、下载工具、站点解析依赖网络连通性。 |
| 磁盘空间 | 本地可用空间 | 至少 500MB，建议 2GB 以上 | 必需 | 工具本体、缓存、临时文件、合并中间文件都会占用空间。 |
| 浏览器扩展 | CatCatch（猫爪） | 按需安装 | 可选 | 仅在你希望从 Chrome/Edge 一键发送资源到程序时需要。 |

未安装 Chrome 会导致：
- 浏览器工作台无法正常启动
- 网页内自动嗅探失效
- 浏览器 Cookie / 登录态能力失效
- 部分站点真实媒体地址无法捕获
- 整体下载能力明显降级

结论：Google Chrome 不是可有可无的附加项，而是影响内置浏览器、嗅探成功率、登录态复用和整体使用体验的关键前提。""")

        prereq_html = f"""
            <div style="background:#fff8e7; border:1px solid #f0d58c; border-radius:8px; padding:12px 14px; margin-bottom:14px;">
              <div style="font-weight:700; font-size:16px; color:#7a4b00; margin-bottom:8px;">首次运行前必看：运行环境与 Chrome 重要说明</div>
              <pre style="white-space:pre-wrap; font-family:'Consolas','Microsoft YaHei UI','Segoe UI',sans-serif; font-size:13px; line-height:1.55; margin:0;">{html.escape(prereq_text)}</pre>
              <div style="margin-top:10px; padding:10px 12px; background:#fff1f1; border:1px solid #f3b4b4; border-radius:6px;">
                <div style="color:#c62828; font-weight:800; margin-bottom:6px;">以下属于必须项目（缺一会明显影响使用，需优先满足）</div>
                <div style="color:#c62828; line-height:1.7;">
                  Windows 10/11 64 位、Python 3.9+、pip、requirements.txt 全部依赖、Google Chrome、
                  bin/yt-dlp.exe、bin/N_m3u8DL-RE.exe、bin/ffmpeg.exe
                </div>
                <div style="color:#8a4f00; margin-top:6px; line-height:1.7;">
                  建议安装：bin/aria2c.exe、bin/streamlink.exe；可选：bin/deno.exe
                </div>
              </div>
            </div>
        """

        manual_html = f"""
        <html>
          <body style="font-family:'Microsoft YaHei','Segoe UI',sans-serif; color:#243447;">
            {prereq_html}
            <div style="background:#faf7f1; border:1px solid #e4ddd0; border-radius:8px; padding:12px 14px; margin-bottom:14px;">
              <div style="font-weight:700; font-size:16px; color:#17324d; margin-bottom:6px;">快速脚本</div>
              <div style="color:#66717d; margin-bottom:12px;">点击链接可直接运行对应脚本（.bat）</div>
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
        logger.info("应用关闭中...")
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
        _, engine_name = selector.select(url, user_engine)
        self.resource_panel.add_resource(resource, engine_name)
        
        # 切换到资源标签页
        self.main_tabs.setCurrentIndex(1)
        
        # 显示通知
        self.statusBar().showMessage(f"收到猫爪下载请求: {filename or url[:50]}...", 5000)
