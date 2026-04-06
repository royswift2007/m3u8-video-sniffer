"""
Playwright Browser Driver (Qt Compatible)
Manages the Playwright browser instance in a separate thread.
"""
import shutil
import sys
import json
import time
from urllib.parse import urljoin, urlparse
from PyQt6.QtCore import QThread, pyqtSignal, QObject
from playwright.sync_api import sync_playwright
from core.playwright_profile import (
    create_temporary_user_data_dir,
    get_primary_user_data_dir,
    is_profile_lock_error,
)
from core.sniffer_script import SNIFFER_JS
from utils.config_manager import config
from utils.logger import logger
from utils.i18n import TR

class PlaywrightDriver(QThread):
    """Playwright 驱动线程"""
    
    # 信号定义
    browser_ready = pyqtSignal()
    page_created = pyqtSignal()
    page_closed = pyqtSignal()
    resource_detected = pyqtSignal(str, dict, str, str) # url, headers, page_url, title
    error_occurred = pyqtSignal(str)
    
    def __init__(self, headless=False):
        super().__init__()
        self.headless = headless
        self.active = True
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        # 命令队列 (简单的标志位，实际操作在 run 获取)
        self._target_url = None
        self._action_queue = [] # list of (action, args)
        self._capture_window_end = 0.0
        self._next_capture_probe_at = 0.0
        self._recent_emit_cache = {}
        self._configured_page_ids = set()
        self._context_user_data_dir = None
        self._temporary_profile_dir = None
        self._load_capture_settings()

    def _remember_page_configured(self, page) -> bool:
        """
        Return True when the page was already configured.
        Prevent duplicate event handlers on the same Playwright page.
        """
        if not page:
            return True
        page_id = id(page)
        if page_id in self._configured_page_ids:
            return True
        self._configured_page_ids.add(page_id)
        # Opportunistic cleanup to avoid unbounded growth in long sessions.
        if len(self._configured_page_ids) > 256 and self.context:
            alive_ids = {id(p) for p in self.context.pages}
            self._configured_page_ids = self._configured_page_ids.intersection(alive_ids)
        return False

    def _load_capture_settings(self):
        """Load capture window settings from feature flags."""
        features = config.get("features", {}) or {}

        def _safe_int(value, fallback):
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        self._capture_window_enabled = bool(features.get("browser_capture_window_enabled", True))
        self._capture_window_seconds = max(5, min(30, _safe_int(features.get("browser_capture_window_seconds", 12), 12)))
        self._capture_extend_on_hit_seconds = max(
            1,
            min(10, _safe_int(features.get("browser_capture_extend_on_hit_seconds", 4), 4)),
        )
        self._capture_probe_interval_ms = max(
            300,
            min(3000, _safe_int(features.get("browser_capture_probe_interval_ms", 1000), 1000)),
        )
        
    def run(self):
        """线程主入口"""
        try:
            with sync_playwright() as p:
                self.playwright = p

                # 启动持久化上下文
                # 注意：launch_persistent_context 返回的是 Context 并非 Browser
                try:
                    self.context = self._launch_persistent_context()
                    self.page = self.context.pages[0] # 持久化上下文默认会打开一个页面
                    self.browser = None # persistent_context 模式下没有单独的 browser 对象
                    
                    logger.info(f"{TR('log_pwr_started')} ({TR('label_save_path')}: {self._context_user_data_dir})")
                except Exception as e:
                    logger.error(
                        f"{TR('log_pwr_init_failed')}: {e}",
                        event="playwright_init_failed",
                        stage="launch_persistent_context",
                        error_type=type(e).__name__,
                    )
                    raise e
                
                # 配置页面 (复用逻辑)
                self._setup_page(self.page)
                
                # === 多标签页支持：监听新标签页创建 ===
                def on_new_page(new_page):
                    """当用户打开新标签页时，自动设置资源拦截"""
                    logger.info(f"[Playwright] {TR('log_pwr_new_tab')}")
                    self._setup_page(new_page)
                
                self.context.on("page", on_new_page)
                
                self.browser_ready.emit()
                
                # 事件循环
                last_detected_url = ""  # 用于检测 URL 变化
                
                while self.active:
                    # 处理待处理的动作
                    if self._target_url:
                        target = self._target_url
                        self._target_url = None  # 立即清除，避免重复导航
                        
                        logger.info(f"[Playwright] {TR('log_navigating')}: {target}")
                        self._begin_capture_window("navigate")
                        try:
                            # 使用 domcontentloaded 而不是 load（更快，不等待图片等资源）
                            # 设置 30 秒超时，避免无限等待
                            self.page.goto(
                                target, 
                                wait_until='domcontentloaded',
                                timeout=30000
                            )
                        except Exception as e:
                            error_msg = str(e).lower()
                            if 'timeout' in error_msg:
                                logger.warning(
                                    f"{TR('log_pwr_nav_timeout')}: {target[:50]}",
                                    event="playwright_navigate_timeout",
                                    stage="goto",
                                    error_type=type(e).__name__,
                                    url=target,
                                )
                            elif 'net::' in error_msg:
                                logger.error(
                                    f"{TR('log_pwr_net_error')}: {e}",
                                    event="playwright_navigate_network_error",
                                    stage="goto",
                                    error_type=type(e).__name__,
                                    url=target,
                                )
                            else:
                                logger.error(
                                    f"{TR('log_pwr_nav_failed')}: {e}",
                                    event="playwright_navigate_failed",
                                    stage="goto",
                                    error_type=type(e).__name__,
                                    url=target,
                                )
                    
                    # 简单的事件处理与保活
                    try:
                        self.page.wait_for_timeout(500)
                        
                        # 检查当前页面是否关闭
                        if self.page.is_closed():
                            # 尝试切换到其他可用页面
                            all_pages = self.context.pages
                            available_pages = [p for p in all_pages if not p.is_closed()]
                            
                            if available_pages:
                                # 切换到第一个可用页面
                                self.page = available_pages[0]
                                self._setup_page(self.page)
                                logger.info(f"{TR('log_pwr_switched_tab')} ({TR('label_remaining')} {len(available_pages)})")
                                last_detected_url = ""  # 重置 URL 检测
                                continue
                            else:
                                logger.info(TR("log_pwr_all_tabs_closed"))
                                break
                        
                        # === URL 变化检测（用于 SPA 导航）===
                        try:
                            current_url = self.page.url
                            if current_url != last_detected_url:
                                # URL 变化了，检测是否为视频页面
                                self._check_video_page(current_url)
                                self._begin_capture_window("url_change")
                                last_detected_url = current_url
                        except Exception as e:
                            logger.debug(
                                f"[PWR-LOOP] URL 变化检测异常: {e}",
                                event="playwright_loop_url_change_error",
                                stage="loop",
                                error_type=type(e).__name__,
                            )

                        # 播放后窗口内持续探测动态加载的媒体 URL
                        self._tick_capture_window()
                            
                    except Exception as e:
                        # 如果是页面关闭导致的错误，尝试切换到其他页面
                        if "closed" in str(e).lower():
                            try:
                                all_pages = self.context.pages
                                available_pages = [p for p in all_pages if not p.is_closed()]
                                if available_pages:
                                    self.page = available_pages[0]
                                    self._setup_page(self.page)
                                    logger.info(TR("log_pwr_page_closed_switched"))
                                    last_detected_url = ""
                                    continue
                            except Exception as switch_err:
                                logger.debug(
                                    f"[PWR-LOOP] 页面关闭后切换标签失败: {switch_err}",
                                    event="playwright_loop_page_switch_error",
                                    stage="loop",
                                    error_type=type(switch_err).__name__,
                                )
                            logger.info(TR("log_browser_page_closed"))
                            break
                        else:
                            logger.error(f"{TR('log_pwr_loop_error')}: {e}")
                            break
                        
                # 清理
                try:
                    if self.context:
                        self.context.close()
                    if self.browser:
                        self.browser.close()
                except Exception as e:
                    logger.debug(
                        f"[PWR-EXIT] {TR('log_pwr_shutdown_error')}: {e}",
                        event="playwright_shutdown_error",
                        stage="cleanup",
                        error_type=type(e).__name__,
                    )
                finally:
                    self._cleanup_temporary_profile_dir()
                self.page_closed.emit()
                
        except Exception as e:
            logger.error(
                f"Playwright {TR('log_error')}: {e}",
                event="playwright_thread_crash",
                stage="run",
                error_type=type(e).__name__,
            )
            self.error_occurred.emit(str(e))

    def _launch_persistent_context(self):
        """Launch the persistent browser context with safe profile fallback."""
        primary_profile_dir = get_primary_user_data_dir()
        primary_profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._context_user_data_dir = primary_profile_dir
            return self._launch_context_with_profile(primary_profile_dir)
        except Exception as exc:
            if not is_profile_lock_error(exc):
                raise

            fallback_profile_dir = create_temporary_user_data_dir()
            self._temporary_profile_dir = fallback_profile_dir
            self._context_user_data_dir = fallback_profile_dir
            logger.warning(
                f"[PWR-INIT] Browser profile is busy, falling back to isolated profile: {fallback_profile_dir}",
                event="playwright_profile_busy_fallback",
                stage="launch_persistent_context",
            )
            return self._launch_context_with_profile(fallback_profile_dir)

    def _launch_context_with_profile(self, user_data_dir):
        """Launch Chromium persistent context against the given user data dir."""
        return self.playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            channel="chrome",  # 恢复使用官方 Chrome
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-default-browser-check"
            ],
            ignore_default_args=["--enable-automation", "--disable-extensions"], # 关键：防止屏蔽扩展
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            no_viewport=True
        )

    def _cleanup_temporary_profile_dir(self):
        """Delete isolated fallback profiles created for this session."""
        if not self._temporary_profile_dir:
            return
        try:
            shutil.rmtree(self._temporary_profile_dir, ignore_errors=True)
        finally:
            self._temporary_profile_dir = None
    
    def _setup_page(self, page):
        """配置页面拦截与脚本"""
        try:
            if page.is_closed():
                return
            if self._remember_page_configured(page):
                logger.debug(
                    f"[PWR] {TR('log_pwr_page_configured')}",
                    event="playwright_page_already_configured",
                    stage="setup_page",
                )
                return

            # 1. 注入嗅探脚本
            page.add_init_script(SNIFFER_JS)
            
            # 2. 注入反检测脚本 (Stealth)
            stealth_js = """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                if (!window.chrome) { window.chrome = { runtime: {} }; }
            """
            page.add_init_script(stealth_js)
            
            # 2.5 监听页面导航事件 - 用于立即检测 yt-dlp 支持的视频网站页面
            def on_frame_navigated(frame):
                """当页面导航完成时（包括 SPA 内部导航），检测视频页面"""
                try:
                    # 只处理主 frame
                    if frame != page.main_frame:
                        return
                        
                    url = frame.url
                    url_lower = url.lower()
                    
                    # yt-dlp 支持的视频页面 URL 模式
                    # 格式: (域名关键词, URL路径特征, 标题后缀移除)
                    ytdlp_patterns = [
                        # YouTube
                        ('youtube.com/watch', None, ' - YouTube'),
                        ('youtube.com/shorts/', None, ' - YouTube'),
                        ('youtu.be/', None, ' - YouTube'),
                        # Bilibili
                        ('bilibili.com/video/', None, '_哔哩哔哩_bilibili'),
                        ('bilibili.com/bangumi/', None, '_哔哩哔哩_bilibili'),
                        ('b23.tv/', None, '_哔哩哔哩_bilibili'),
                        # TikTok / 抖音
                        ('tiktok.com/@', '/video/', ' | TikTok'),
                        ('douyin.com/video/', None, None),
                        # Twitter / X
                        ('twitter.com/', '/status/', None),
                        ('x.com/', '/status/', None),
                        # Instagram
                        ('instagram.com/p/', None, None),
                        ('instagram.com/reel/', None, None),
                        # Vimeo
                        ('vimeo.com/', None, ' on Vimeo'),
                        # Twitch
                        ('twitch.tv/videos/', None, ' - Twitch'),
                        ('twitch.tv/', None, ' - Twitch'),  # 直播
                        # 西瓜视频
                        ('ixigua.com/', None, None),
                        # 优酷
                        ('youku.com/v_show/', None, None),
                        # 爱奇艺
                        ('iqiyi.com/', None, None),
                        # 腾讯视频
                        ('v.qq.com/x/page/', None, None),
                        ('v.qq.com/x/cover/', None, None),
                        # Facebook
                        ('facebook.com/', '/videos/', None),
                        # Dailymotion
                        ('dailymotion.com/video/', None, ' - Dailymotion'),
                    ]
                    
                    matched = False
                    title_suffix = None
                    
                    for pattern in ytdlp_patterns:
                        domain_kw = pattern[0]
                        path_kw = pattern[1] if len(pattern) > 1 else None
                        suffix = pattern[2] if len(pattern) > 2 else None
                        
                        if domain_kw in url_lower:
                            # 如果有路径关键词要求，也要匹配
                            if path_kw is None or path_kw in url_lower:
                                matched = True
                                title_suffix = suffix
                                break
                    
                    if matched:
                        # 延迟等待标题加载
                        page.wait_for_timeout(500)
                        
                        try:
                            title = page.title()
                            # 移除平台后缀
                            if title_suffix and title.endswith(title_suffix):
                                title = title[:-len(title_suffix)]
                            title = title.strip()
                        except Exception as e:
                            logger.debug(
                                f"[PWR-NAV] {TR('log_pwr_read_title_fail')}: {e}",
                                event="playwright_nav_title_error",
                                stage="frame_navigated",
                                error_type=type(e).__name__,
                            )
                            title = "Video"
                        
                        if not title:
                            title = "Video"
                        
                        headers = {
                            'referer': url,
                            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        }
                        
                        # 发射页面 URL 作为资源（yt-dlp 会处理）
                        self._emit_detected_resource(
                            url=url,
                            headers=headers,
                            page_url=url,
                            title=title,
                            source="PWR-NAV",
                        )
                        self._begin_capture_window("video_page")
                        
                except Exception as e:
                    logger.debug(
                        f"[PWR-NAV] {TR('log_pwr_nav_event_error')}: {e}",
                        event="playwright_nav_event_error",
                        stage="frame_navigated",
                        error_type=type(e).__name__,
                    )
            
            page.on("framenavigated", on_frame_navigated)
            
            # 3. 网络拦截 - 同时监听 request 和 response
            
            def handle_request(request):
                """处理请求事件 - 通过 URL 模式检测"""
                url = request.url
                if self._is_video_url(url):
                    headers = dict(request.headers)
                    try:
                        page_url = page.url
                        title = page.title()
                    except Exception as e:
                        logger.debug(
                            f"[PWR-REQ] {TR('log_pwr_context_fail')}: {e}",
                            event="playwright_request_context_error",
                            stage="request",
                            error_type=type(e).__name__,
                        )
                        page_url = ""
                        title = "Unknown"
                    
                    # 补充 Referer
                    if 'referer' not in headers:
                        headers['referer'] = page_url
                    self._emit_detected_resource(
                        url=url,
                        headers=headers,
                        page_url=page_url,
                        title=title,
                        source="PWR-REQ",
                    )
                    self._maybe_extend_capture_window(url, "request_hit")
            
            def handle_response(response):
                """处理响应事件 - 通过 Content-Type 检测"""
                try:
                    url = response.url
                    # 跳过已通过 URL 检测的
                    if self._is_video_url(url):
                        return
                        
                    content_type = response.headers.get('content-type', '').lower()
                    
                    # 视频相关的 Content-Type
                    video_content_types = (
                        'application/vnd.apple.mpegurl',  # HLS
                        'application/x-mpegurl',           # HLS
                        'application/dash+xml',            # DASH
                        'video/mp4',
                        'video/webm',
                        'video/x-flv',
                        'video/x-matroska',
                        'video/mpeg',
                        'video/3gpp',
                        'video/quicktime',
                        'application/octet-stream',  # 某些视频流使用此类型
                    )
                    
                    if any(ct in content_type for ct in video_content_types):
                        # 获取原始请求的 headers
                        request = response.request
                        headers = dict(request.headers)
                        
                        try:
                            page_url = page.url
                            title = page.title()
                        except Exception as e:
                            logger.debug(
                                f"[PWR-RSP] {TR('log_pwr_context_fail')}: {e}",
                                event="playwright_response_context_error",
                                stage="response",
                                error_type=type(e).__name__,
                            )
                            page_url = ""
                            title = "Unknown"
                        
                        if 'referer' not in headers:
                            headers['referer'] = page_url
                        self._emit_detected_resource(
                            url=url,
                            headers=headers,
                            page_url=page_url,
                            title=title,
                            source="PWR-RSP",
                        )
                        self._maybe_extend_capture_window(url, "response_hit")
                except Exception as e:
                    logger.debug(
                        f"[PWR-RSP] {TR('log_pwr_response_error')}: {e}",
                        event="playwright_response_error",
                        stage="response",
                        error_type=type(e).__name__,
                    )
            
            page.on("request", handle_request)
            page.on("response", handle_response)
            
            # 4. 处理普通下载 (防止 Playwright 删除临时文件)
            def handle_download(download):
                try:
                    # 保存到系统下载文件夹
                    import os
                    home = os.path.expanduser("~")
                    download_dir = os.path.join(home, "Downloads")
                    if not os.path.exists(download_dir):
                        os.makedirs(download_dir)
                        
                    suggested_filename = download.suggested_filename
                    final_path = os.path.join(download_dir, suggested_filename)
                    
                    # 防止重名覆盖
                    base, ext = os.path.splitext(final_path)
                    counter = 1
                    while os.path.exists(final_path):
                        final_path = f"{base}_{counter}{ext}"
                        counter += 1
                    
                    download.save_as(final_path)
                    logger.info(f"[PWR] {TR('log_pwr_file_downloaded')}: {final_path}")
                    
                except Exception as e:
                    logger.error(
                        f"{TR('log_pwr_download_save_failed')}: {e}",
                        event="playwright_download_save_failed",
                        stage="download",
                        error_type=type(e).__name__,
                    )

            page.on("download", handle_download)
            
            # 监听 Console log 来自 SNIFFER_JS
            page.on("console", self._handle_console)
            
        except Exception as e:
            logger.warning(
                f"页面配置失败 (可能页面已关闭): {e}",
                event="playwright_setup_page_failed",
                stage="setup_page",
                error_type=type(e).__name__,
            )
        
    def _handle_console(self, msg):
        """处理控制台消息 - 接收来自 JS 嗅探脚本的检测结果"""
        try:
            text = msg.text
            if text.startswith("CATCATCH_PLAY:"):
                self._begin_capture_window("media_play")
                self._probe_dynamic_media_urls()
                return

            if text.startswith("CATCATCH_DETECT:"):
                # 解析 JS 脚本检测到的资源 - 格式: URL|DURATION|SOURCE
                content = text.split(":", 1)[1].strip()

                parts = content.split("|")
                url = parts[0].strip() if parts else ""
                duration_str = parts[1].strip() if len(parts) > 1 else ""
                source_tag = parts[2].strip() if len(parts) > 2 else "JS"

                # 跳过空 URL
                if not url:
                    return

                # 跳过 blob URL (需要特殊处理，暂不支持)
                if url.startswith("blob:"):
                    logger.debug(f"[PWR-JS] 跳过 Blob URL: {url}")
                    return

                # 解析时长
                duration_info = ""
                if duration_str and duration_str.isdigit():
                    seconds = int(duration_str)
                    if seconds > 0:
                        minutes, secs = divmod(seconds, 60)
                        hours, minutes = divmod(minutes, 60)
                        if hours > 0:
                            duration_info = f" [{hours:02d}:{minutes:02d}:{secs:02d}]"
                        else:
                            duration_info = f" [{minutes:02d}:{secs:02d}]"

                try:
                    page_url = self.page.url if self.page else ""
                except Exception as e:
                    logger.debug(
                        f"[PWR-JS] 读取 page.url 失败: {e}",
                        event="playwright_console_page_url_error",
                        stage="console",
                        error_type=type(e).__name__,
                    )
                    page_url = ""
                try:
                    base_title = self.page.title() if self.page else "Unknown"
                except Exception as e:
                    logger.debug(
                        f"[PWR-JS] 读取 page.title 失败: {e}",
                        event="playwright_console_page_title_error",
                        stage="console",
                        error_type=type(e).__name__,
                    )
                    base_title = "Unknown"
                title = (base_title or "Unknown") + duration_info

                self._emit_detected_resource(
                    url=url,
                    page_url=page_url,
                    title=title,
                    source=f"PWR-{source_tag or 'JS'}",
                    headers={},
                )
                self._maybe_extend_capture_window(url, "js_detect")
        except Exception as e:
            logger.debug(
                f"[PWR-JS] 处理控制台消息异常: {e}",
                event="playwright_console_handle_error",
                stage="console",
                error_type=type(e).__name__,
            )

    def _begin_capture_window(self, reason: str):
        """Start or extend post-play capture window."""
        if not self._capture_window_enabled:
            return

        now = time.monotonic()
        next_end = now + self._capture_window_seconds
        was_inactive = now >= self._capture_window_end
        if next_end > self._capture_window_end:
            self._capture_window_end = next_end
            self._next_capture_probe_at = now
            if was_inactive:
                logger.info(
                    f"[PWR-CAP] {TR('log_pwr_capture_started')}: {self._capture_window_seconds}s ({reason})"
                )
            else:
                logger.debug(f"[PWR-CAP] {TR('log_pwr_capture_extended')} ({reason})")

    def _maybe_extend_capture_window(self, url: str, reason: str):
        """Extend capture window when a media hit is observed."""
        if not self._capture_window_enabled or not url:
            return
        url_lower = url.lower()
        if (
            ".m3u8" in url_lower
            or ".mpd" in url_lower
            or "/hls/" in url_lower
            or "manifest" in url_lower
            or "playlist" in url_lower
        ):
            now = time.monotonic()
            next_end = now + self._capture_extend_on_hit_seconds
            if next_end > self._capture_window_end:
                self._capture_window_end = next_end
                logger.debug(
                    f"[PWR-CAP] 命中媒体链接，延长捕获窗口 {self._capture_extend_on_hit_seconds}s ({reason})"
                )

    def _tick_capture_window(self):
        """Run periodic probing while capture window is active."""
        if not self._capture_window_enabled:
            return
        if not self.page or self.page.is_closed():
            return
        now = time.monotonic()
        if now >= self._capture_window_end:
            return
        if now < self._next_capture_probe_at:
            return
        self._next_capture_probe_at = now + (self._capture_probe_interval_ms / 1000.0)
        self._probe_dynamic_media_urls()

    def _probe_dynamic_media_urls(self):
        """Actively scan dynamic media URLs during capture window."""
        if not self.page or self.page.is_closed():
            return
        try:
            data = self.page.evaluate(
                """
                () => {
                    const urls = new Set();
                    const pushUrl = (candidate) => {
                        if (!candidate) return;
                        try {
                            const absolute = new URL(candidate, window.location.href).href;
                            urls.add(absolute);
                        } catch (e) {}
                    };

                    document.querySelectorAll('video').forEach((video) => {
                        pushUrl(video.src);
                        pushUrl(video.currentSrc);
                        video.querySelectorAll('source').forEach((source) => pushUrl(source.src));
                    });
                    document.querySelectorAll('source').forEach((source) => pushUrl(source.src));

                    if (window.performance && performance.getEntriesByType) {
                        performance.getEntriesByType('resource').forEach((entry) => {
                            const name = (entry && entry.name) || '';
                            const lower = name.toLowerCase();
                            if (
                                lower.includes('.m3u8') ||
                                lower.includes('.mpd') ||
                                lower.includes('/hls/') ||
                                lower.includes('/dash/') ||
                                lower.includes('manifest') ||
                                lower.includes('playlist')
                            ) {
                                pushUrl(name);
                            }
                        });
                    }

                    return {
                        page_url: window.location.href,
                        title: document.title || 'Unknown',
                        urls: Array.from(urls).slice(0, 80),
                    };
                }
                """
            )
        except Exception as e:
            logger.debug(
                f"[PWR-CAP] 动态探测 evaluate 失败: {e}",
                event="playwright_capture_probe_failed",
                stage="capture_probe",
                error_type=type(e).__name__,
            )
            return

        if not isinstance(data, dict):
            return
        page_url = data.get("page_url", "")
        title = data.get("title", "Unknown")
        urls = data.get("urls", []) or []
        for url in urls:
            if not self._is_video_url(url):
                continue
            self._emit_detected_resource(
                url=url,
                page_url=page_url,
                title=title,
                source="PWR-CAP",
                headers={},
            )
            self._maybe_extend_capture_window(url, "window_probe")

    def _normalize_emit_url(self, url: str, page_url: str) -> str:
        """Normalize URL before emitting to sniffer pipeline."""
        if not url:
            return ""
        normalized = url.strip()
        if normalized.startswith("blob:"):
            return ""
        if normalized.startswith("//"):
            normalized = "https:" + normalized
        elif normalized.startswith("/"):
            normalized = urljoin(page_url or "", normalized)
        return normalized

    def _build_default_headers(self, page_url: str, resource_url: str, headers: dict | None = None) -> dict:
        """Attach stable headers for internal browser capture."""
        merged = {}
        if headers:
            merged.update(headers)
        if page_url and not merged.get("referer"):
            merged["referer"] = page_url
        if not merged.get("user-agent"):
            merged["user-agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )

        if merged.get("referer") and not merged.get("origin"):
            try:
                parsed_ref = urlparse(merged["referer"])
                if parsed_ref.scheme and parsed_ref.netloc:
                    merged["origin"] = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
            except Exception as e:
                logger.debug(
                    f"[PWR-HDR] 构建 Origin 失败: {e}",
                    event="playwright_headers_origin_failed",
                    stage="build_headers",
                    error_type=type(e).__name__,
                )

        if ".m3u8" in (resource_url or "").lower() and not merged.get("cookie"):
            try:
                if self.page and self.page.context:
                    cookies = self.page.context.cookies(page_url or resource_url)
                    cookie_str = "; ".join(
                        [f"{c.get('name', '')}={c.get('value', '')}" for c in cookies if c.get("name")]
                    )
                    if cookie_str:
                        merged["cookie"] = cookie_str
            except Exception as e:
                logger.debug(
                    f"[PWR-HDR] 获取 Cookie 失败: {e}",
                    event="playwright_headers_cookie_failed",
                    stage="build_headers",
                    error_type=type(e).__name__,
                )

        return merged

    def _is_recent_emit(self, url: str) -> bool:
        """Local short-term dedup to prevent burst duplicates."""
        now = time.monotonic()
        last_time = self._recent_emit_cache.get(url, 0.0)
        if now - last_time < 2.0:
            return True
        self._recent_emit_cache[url] = now
        if len(self._recent_emit_cache) > 800:
            expire_before = now - 30.0
            self._recent_emit_cache = {
                key: ts for key, ts in self._recent_emit_cache.items() if ts >= expire_before
            }
        return False

    def _emit_detected_resource(self, url: str, headers: dict, page_url: str, title: str, source: str):
        """Emit detected resource with normalized headers and lightweight dedup."""
        normalized_url = self._normalize_emit_url(url, page_url)
        if not normalized_url:
            return
        if self._is_recent_emit(normalized_url):
            return
        merged_headers = self._build_default_headers(page_url, normalized_url, headers)
        self.resource_detected.emit(normalized_url, merged_headers, page_url, title)
        logger.info(f"[{source}] 发现资源: {normalized_url}")

    def _is_video_url(self, url: str) -> bool:
        """判断是否为视频 URL - 增强版检测"""
        url_lower = url.lower()
        
        # 排除常见的非视频资源
        skip_patterns = (
            '.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
            '.woff', '.woff2', '.ttf', '.eot', 'google', 'facebook', 'twitter',
            'analytics', 'tracking', 'beacon', 'pixel'
        )
        if any(p in url_lower for p in skip_patterns):
            return False
        
        # 1. 常见流媒体后缀
        if '.m3u8' in url_lower or '.mpd' in url_lower:
            return True
        
        # 2. 常见视频后缀 (忽略参数)
        url_path = url_lower.split('?')[0]
        video_exts = ('.mp4', '.flv', '.mkv', '.avi', '.wmv', '.webm', '.mov', '.m4v', '.f4v', '.3gp')  # 不检测 .ts (M3U8分片)
        if any(url_path.endswith(x) for x in video_exts):
            return True
        
        # 3. URL 关键词匹配 (某些动态流 URL)
        video_keywords = (
            '/playlist.m3u8', '/master.m3u8', '/index.m3u8', '/manifest.mpd',
            '/hls/', '/dash/', '/video/', '/stream/', '/media/',
            'videoplayback', 'video_ts', 'chunk', 'segment',
            '.m3u8?', '.mpd?',  # 带参数的流地址
            'application/vnd.apple.mpegurl',
            'application/x-mpegurl',
        )
        if any(kw in url_lower for kw in video_keywords):
            return True
        
        return False
    
    def _check_video_page(self, url: str):
        """检测 URL 是否为 yt-dlp 支持的视频页面，如果是则发射资源信号"""
        if not url or not self.page:
            return
            
        url_lower = url.lower()
        
        # yt-dlp 支持的视频页面 URL 模式
        # 格式: (域名关键词, URL路径特征, 标题后缀移除)
        ytdlp_patterns = [
            # YouTube
            ('youtube.com/watch', None, ' - YouTube'),
            ('youtube.com/shorts/', None, ' - YouTube'),
            ('youtu.be/', None, ' - YouTube'),
            # Bilibili
            ('bilibili.com/video/', None, '_哔哩哔哩_bilibili'),
            ('bilibili.com/bangumi/', None, '_哔哩哔哩_bilibili'),
            ('b23.tv/', None, '_哔哩哔哩_bilibili'),
            # TikTok / 抖音
            ('tiktok.com/', '/video/', ' | TikTok'),
            ('douyin.com/video/', None, None),
            # Twitter / X
            ('twitter.com/', '/status/', None),
            ('x.com/', '/status/', None),
            # Instagram
            ('instagram.com/p/', None, None),
            ('instagram.com/reel/', None, None),
            # Vimeo
            ('vimeo.com/', None, ' on Vimeo'),
            # Twitch
            ('twitch.tv/videos/', None, ' - Twitch'),
            # 西瓜视频
            ('ixigua.com/', None, None),
            # 优酷
            ('youku.com/v_show/', None, None),
            # 爱奇艺
            ('iqiyi.com/', None, None),
            # 腾讯视频
            ('v.qq.com/x/page/', None, None),
            ('v.qq.com/x/cover/', None, None),
            # Facebook
            ('facebook.com/', '/videos/', None),
            # Dailymotion
            ('dailymotion.com/video/', None, ' - Dailymotion'),
        ]
        
        matched = False
        title_suffix = None
        
        for pattern in ytdlp_patterns:
            domain_kw = pattern[0]
            path_kw = pattern[1] if len(pattern) > 1 else None
            suffix = pattern[2] if len(pattern) > 2 else None
            
            if domain_kw in url_lower:
                # 如果有路径关键词要求，也要匹配
                if path_kw is None or path_kw in url_lower:
                    matched = True
                    title_suffix = suffix
                    break
        
        if not matched:
            return
        
        try:
            title = self.page.title() or "Video"
            # 移除平台后缀
            if title_suffix and title.endswith(title_suffix):
                title = title[:-len(title_suffix)]
            title = title.strip() or "Video"
        except Exception as e:
            logger.debug(
                f"[PWR-URL] 读取页面标题失败: {e}",
                event="playwright_video_page_title_failed",
                stage="check_video_page",
                error_type=type(e).__name__,
            )
            title = "Video"
        
        headers = {
            'referer': url,
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        # 发射页面 URL 作为资源（yt-dlp 会处理）
        self._emit_detected_resource(
            url=url,
            headers=headers,
            page_url=url,
            title=title,
            source="PWR-URL",
        )
        self._begin_capture_window("video_page_match")

    # === 公共控制方法 (线程安全调用) ===
    def navigate(self, url):
        """导航到 URL"""
        if not url:
            return
        if url == "about:blank":
            self._target_url = url
            return
        if not url.startswith('http'):
            url = 'https://' + url
        self._target_url = url
    
    def stop(self):
        """停止驱动"""
        self.active = False
        self.wait()
    
    def export_cookies_to_file(self, url: str = None, domain_filter: str = None) -> str:
        """
        Export browser cookies to a Netscape-format file (yt-dlp compatible).

        Args:
            url: Optional URL used to scope cookies by Playwright context.
            domain_filter: Optional domain keyword filter.

        Returns:
            Cookie file path on success, otherwise None.
        """
        import os
        from urllib.parse import urlparse

        try:
            if not self.context:
                logger.warning("无法导出 cookies：浏览器上下文未初始化")
                return None

            cookies = self.context.cookies(url) if url else self.context.cookies()
            if not cookies:
                logger.info("浏览器中没有 cookies")
                return None

            # If URL is provided but explicit domain filter is not, derive one from URL.
            if not domain_filter and url:
                try:
                    parsed = urlparse(url)
                    domain_filter = (parsed.hostname or "").lower()
                except Exception as e:
                    logger.debug(
                        f"[PWR-COOKIE] 解析 URL 域名失败: {e}",
                        event="playwright_cookie_domain_parse_failed",
                        stage="export_cookies",
                        error_type=type(e).__name__,
                    )
                    domain_filter = None

            if domain_filter:
                domain_filter = domain_filter.lower()
                cookies = [c for c in cookies if domain_filter in (c.get('domain', '').lower())]

            if not cookies:
                logger.info(f"没有匹配过滤条件的 cookies: {domain_filter}")
                return None

            cookie_dir = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "M3U8VideoSniffer", "cookies")
            os.makedirs(cookie_dir, exist_ok=True)
            cookie_file = os.path.join(cookie_dir, "browser_cookies.txt")

            with open(cookie_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write("# https://curl.se/docs/http-cookies.html\n")
                f.write("# This file was generated by M3U8VideoSniffer\n\n")

                for cookie in cookies:
                    domain = cookie.get('domain', '')
                    flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                    path = cookie.get('path', '/')
                    secure = 'TRUE' if cookie.get('secure', False) else 'FALSE'
                    expires_raw = cookie.get('expires', 0)
                    try:
                        expires_val = int(expires_raw)
                    except Exception as e:
                        logger.debug(
                            f"[PWR-COOKIE] Cookie 过期时间解析失败: {e}",
                            event="playwright_cookie_expires_parse_failed",
                            stage="export_cookies",
                            error_type=type(e).__name__,
                        )
                        expires_val = 0
                    expires = str(expires_val if expires_val > 0 else 0)
                    name = cookie.get('name', '')
                    value = cookie.get('value', '')

                    f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")

            logger.info(f"已导出 {len(cookies)} 个 cookies 到: {cookie_file}")
            return cookie_file

        except Exception as e:
            logger.error(
                f"导出 cookies 失败: {e}",
                event="playwright_export_cookies_failed",
                stage="export_cookies",
                error_type=type(e).__name__,
            )
            return None
