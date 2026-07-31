"""
Playwright Browser Driver (Qt Compatible)
Manages the Playwright browser instance in a separate thread.
"""
import shutil
import sys
import json
import time
import threading
import weakref
from collections import OrderedDict
from urllib.parse import urljoin, urlparse
from PyQt6.QtCore import QThread, pyqtSignal, QObject
from playwright.sync_api import sync_playwright
from core.app_paths import get_temp_dir
from core.media_url_ttl import is_probably_ephemeral_media_url
from core.playwright_profile import (
    create_temporary_user_data_dir,
    get_primary_user_data_dir,
    is_profile_lock_error,
)
from core.sniffer_script import SNIFFER_JS
from engines.base_engine import kill_process_tree
from utils.config_manager import config
from utils.headers import normalized_forward_headers
from utils.logger import logger
from utils.i18n import TR

# Historical + current fallback-profile directory prefixes. ``profile_`` is
# produced by :func:`core.playwright_profile.create_temporary_user_data_dir`;
# ``temp_profile_`` is kept for backwards compatibility with older builds.
_STALE_PROFILE_PREFIXES = ("profile_", "temp_profile_")
# Retention window for stale fallback profiles (R21.AC2).
_STALE_PROFILE_MAX_AGE_SECONDS = 24 * 60 * 60
# Wall-clock budget for waiting on the browser driver process during
# cleanup before escalating to ``kill_process_tree`` (R21.AC1).
_BROWSER_PROC_WAIT_TIMEOUT = 10.0

class PlaywrightDriver(QThread):
    """Playwright 驱动线程"""
    
    # 信号定义
    browser_ready = pyqtSignal()
    page_created = pyqtSignal()
    page_closed = pyqtSignal()
    resource_detected = pyqtSignal(str, dict, str, str) # url, headers, page_url, title
    resource_context_detected = pyqtSignal(dict)  # unified context dict (download-context unification)
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
        self._recent_media_request_headers = OrderedDict()
        self._recent_media_contexts = OrderedDict()
        # Track configured pages by weak reference so garbage-collected pages
        # drop out automatically (R21.AC3). Fall back to ``page.guid`` string
        # tracking when a Playwright Page object cannot be weakly referenced.
        self._configured_pages = weakref.WeakSet()
        self._configured_page_guids: set[str] = set()
        self._context_user_data_dir = None
        self._temporary_profile_dir = None
        self._load_capture_settings()
        # Clean stale fallback profile directories from prior sessions
        # (R21.AC2). Failures must not block startup.
        self._cleanup_stale_profile_dirs()

    def _page_guid(self, page) -> str | None:
        """Return Playwright's stable page guid when available."""
        guid = getattr(page, "guid", None)
        if isinstance(guid, str) and guid:
            return guid
        impl = getattr(page, "_impl_obj", None)
        if impl is not None:
            inner = getattr(impl, "_guid", None)
            if isinstance(inner, str) and inner:
                return inner
        return None

    def _remember_page_configured(self, page) -> bool:
        """
        Return True when the page was already configured.
        Prevent duplicate event handlers on the same Playwright page.
        """
        if not page:
            return True

        # Prefer WeakSet identity so entries vanish when pages are GC'd.
        try:
            if page in self._configured_pages:
                return True
        except TypeError:
            # Unhashable page object (extremely unlikely) — fall through.
            pass

        guid = self._page_guid(page)
        if guid is not None and guid in self._configured_page_guids:
            return True

        try:
            self._configured_pages.add(page)
        except TypeError:
            # Page object does not support weak references on this
            # Playwright version; rely on guid tracking instead.
            pass
        if guid is not None:
            self._configured_page_guids.add(guid)

        # Opportunistically prune guid cache to avoid unbounded growth in
        # long-lived sessions where pages are closed frequently.
        if len(self._configured_page_guids) > 256 and self.context:
            try:
                alive_guids = {
                    g
                    for g in (self._page_guid(p) for p in self.context.pages)
                    if g is not None
                }
                if alive_guids:
                    self._configured_page_guids &= alive_guids
            except (AttributeError, RuntimeError):
                # Context enumeration can race with teardown (RuntimeError from
                # the Playwright loop, AttributeError when ``context`` is torn
                # down mid-call). The cap is a best-effort hygiene measure.
                logger.debug("playwright_driver: guid-cache prune skipped during teardown")
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
        self._exact_request_header_replay_enabled = bool(
            features.get("exact_request_header_replay_enabled", True)
        )
        self._resource_domain_cookie_lookup_enabled = bool(
            features.get("resource_domain_cookie_lookup_enabled", True)
        )
        self._media_header_cache_ttl_seconds = max(
            60,
            min(900, _safe_int(features.get("media_header_cache_ttl_seconds", 600), 600)),
        )
        self._media_header_cache_max_items = max(
            100,
            min(2000, _safe_int(features.get("media_header_cache_max_items", 800), 800)),
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
                    """当用户打开新标签页时，自动设置资源拦截并切换为活动页"""
                    logger.info(f"[Playwright] {TR('log_pwr_new_tab')}")
                    self.page = new_page
                    self._last_detected_url = ""
                    self._setup_page(new_page)
                
                self.context.on("page", on_new_page)
                
                self.browser_ready.emit()
                
                # 事件循环
                self._last_detected_url = ""  # 用于检测 URL 变化
                
                while self.active:
                    # 处理待处理的动作队列（back/forward/reload/new_tab 等）
                    while self._action_queue:
                        action, args = self._action_queue.pop(0)
                        self._handle_action(action, args)
                    
                    # 处理待处理的 URL 导航
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
                                self._last_detected_url = ""  # 重置 URL 检测
                                continue
                            else:
                                logger.info(TR("log_pwr_all_tabs_closed"))
                                break
                        
                        # === URL 变化检测（用于 SPA 导航）===
                        try:
                            current_url = self.page.url
                            if current_url != self._last_detected_url:
                                # URL 变化了，检测是否为视频页面
                                self._check_video_page(current_url)
                                self._begin_capture_window("url_change")
                                self._last_detected_url = current_url
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
                                    self._last_detected_url = ""
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
        """Delete isolated fallback profiles created for this session.

        R21.AC1 + R21.AC4:
        1. Ensure the browser context/process is torn down before ``rmtree``
           to avoid Windows file-in-use errors (Chromium keeps Singleton
           sockets/locks open until the driver exits).
        2. Wait up to ``_BROWSER_PROC_WAIT_TIMEOUT`` seconds, escalating to
           ``kill_process_tree`` on timeout.
        3. Run ``shutil.rmtree`` with ``ignore_errors=False`` so we can log
           ``profile_cleanup_denied`` on ``PermissionError`` and keep going
           instead of crashing the main flow.
        """
        if not self._temporary_profile_dir:
            return

        profile_dir = self._temporary_profile_dir
        try:
            self._shutdown_browser_for_cleanup()
            try:
                shutil.rmtree(profile_dir, ignore_errors=False)
            except PermissionError as exc:
                logger.warning(
                    f"[PWR-CLEAN] 临时 profile 清理无权限: {profile_dir} ({exc})",
                    event="profile_cleanup_denied",
                    stage="cleanup_temporary_profile_dir",
                    error_type=type(exc).__name__,
                    path=str(profile_dir),
                )
            except FileNotFoundError:
                # Already gone (possibly cleaned by stale scan). Not an error.
                pass
            except OSError as exc:
                logger.warning(
                    f"[PWR-CLEAN] 临时 profile 清理失败: {profile_dir} ({exc})",
                    event="profile_cleanup_failed",
                    stage="cleanup_temporary_profile_dir",
                    error_type=type(exc).__name__,
                    path=str(profile_dir),
                )
        finally:
            self._temporary_profile_dir = None

    def _shutdown_browser_for_cleanup(self) -> None:
        """Best-effort browser shutdown + process wait prior to rmtree."""
        browser = self.browser
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug(
                    f"[PWR-CLEAN] browser.close() 失败: {exc}",
                    event="playwright_browser_close_error",
                    stage="cleanup_temporary_profile_dir",
                    error_type=type(exc).__name__,
                )

        # Playwright's ``browser`` attribute is ``None`` under persistent
        # context mode — the driver child process is owned by the
        # ``_browser_type`` connection. Look up the backing Popen handle
        # across known attribute names so we can wait and escalate.
        proc_handle = self._resolve_browser_process_handle()
        if proc_handle is None:
            return

        pid = getattr(proc_handle, "pid", None)
        try:
            proc_handle.wait(timeout=_BROWSER_PROC_WAIT_TIMEOUT)
        except Exception as exc:
            # ``TimeoutExpired`` is the expected escalation trigger; any
            # other failure likewise means we cannot confirm exit, so
            # escalate too rather than leaking a child into rmtree.
            logger.warning(
                f"[PWR-CLEAN] 浏览器进程未在 {_BROWSER_PROC_WAIT_TIMEOUT}s 内退出, "
                f"升级到 kill_process_tree pid={pid}: {exc}",
                event="playwright_browser_wait_timeout",
                stage="cleanup_temporary_profile_dir",
                error_type=type(exc).__name__,
                pid=pid,
            )
            if isinstance(pid, int) and pid > 0:
                try:
                    kill_process_tree(pid)
                except Exception as kill_exc:  # pragma: no cover - defensive
                    logger.debug(
                        f"[PWR-CLEAN] kill_process_tree 失败 pid={pid}: {kill_exc}",
                        event="playwright_kill_process_tree_failed",
                        stage="cleanup_temporary_profile_dir",
                        error_type=type(kill_exc).__name__,
                    )

    def _resolve_browser_process_handle(self):
        """Return the ``subprocess.Popen``-like handle behind Playwright."""
        # Public attribute first (rare), then known internal locations.
        candidates = (
            getattr(self, "_browser_proc", None),
            getattr(self.browser, "_process", None) if self.browser is not None else None,
        )
        if self.playwright is not None:
            try:
                chromium = getattr(self.playwright, "chromium", None)
                impl = getattr(chromium, "_impl_obj", None) if chromium is not None else None
                conn = getattr(impl, "_connection", None) if impl is not None else None
                transport = getattr(conn, "_transport", None) if conn is not None else None
                proc = getattr(transport, "_proc", None) if transport is not None else None
                candidates = candidates + (proc,)
            except AttributeError:
                # Playwright internals moved between releases; fall through
                # to the other candidate slots silently.
                logger.debug("playwright_driver: chromium impl handle unavailable")
        for candidate in candidates:
            if candidate is not None and hasattr(candidate, "wait"):
                return candidate
        return None

    def _cleanup_stale_profile_dirs(self) -> None:
        """Scan the fallback profile root for ``profile_*`` / ``temp_profile_*``
        directories older than 24h and ``rmtree`` them (R21.AC2).

        Failures are logged as ``profile_cleanup_denied`` and do not abort
        startup (R21.AC4).
        """
        try:
            profile_root = get_temp_dir() / "playwright_profiles"
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                f"[PWR-CLEAN] 解析 profile 根目录失败: {exc}",
                event="playwright_profile_root_resolve_failed",
                stage="cleanup_stale_profile_dirs",
                error_type=type(exc).__name__,
            )
            return

        if not profile_root.exists():
            return

        now = time.time()
        cutoff = now - _STALE_PROFILE_MAX_AGE_SECONDS
        removed = 0
        scanned = 0
        try:
            entries = list(profile_root.iterdir())
        except PermissionError as exc:
            logger.warning(
                f"[PWR-CLEAN] 无权限列出 profile 根目录: {profile_root} ({exc})",
                event="profile_cleanup_denied",
                stage="cleanup_stale_profile_dirs",
                error_type=type(exc).__name__,
                path=str(profile_root),
            )
            return
        except OSError as exc:
            logger.debug(
                f"[PWR-CLEAN] 列出 profile 根目录失败: {profile_root} ({exc})",
                event="playwright_profile_scan_failed",
                stage="cleanup_stale_profile_dirs",
                error_type=type(exc).__name__,
            )
            return

        for entry in entries:
            if not entry.is_dir():
                continue
            if not entry.name.startswith(_STALE_PROFILE_PREFIXES):
                continue
            scanned += 1
            try:
                mtime = entry.stat().st_mtime
            except OSError as exc:
                logger.debug(
                    f"[PWR-CLEAN] 读取 mtime 失败: {entry} ({exc})",
                    event="playwright_profile_stat_failed",
                    stage="cleanup_stale_profile_dirs",
                    error_type=type(exc).__name__,
                )
                continue
            if mtime >= cutoff:
                continue
            try:
                shutil.rmtree(entry, ignore_errors=False)
                removed += 1
            except PermissionError as exc:
                logger.warning(
                    f"[PWR-CLEAN] 清理旧 profile 无权限: {entry} ({exc})",
                    event="profile_cleanup_denied",
                    stage="cleanup_stale_profile_dirs",
                    error_type=type(exc).__name__,
                    path=str(entry),
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.debug(
                    f"[PWR-CLEAN] 清理旧 profile 失败: {entry} ({exc})",
                    event="playwright_profile_rm_failed",
                    stage="cleanup_stale_profile_dirs",
                    error_type=type(exc).__name__,
                )

        if scanned or removed:
            logger.info(
                f"[PWR-CLEAN] 旧 profile 清理: 扫描 {scanned} 个, 删除 {removed} 个",
                event="playwright_profile_stale_cleanup",
                stage="cleanup_stale_profile_dirs",
                scanned=scanned,
                removed=removed,
            )
    
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
                    self._remember_media_request_headers(
                        url,
                        headers,
                        page_url=page_url,
                        source="PWR-REQ",
                    )
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
                    
                    is_video_url = self._is_video_url(url)
                    is_video_mime = any(ct in content_type for ct in video_content_types)

                    # Request 阶段只能根据 URL 猜测，拿不到响应 Content-Type。
                    # 对 .m3u8 或 /hls/ 等已命中过的 URL，如果响应 MIME 是 HLS/DASH，
                    # 仍然再发一次“上下文升级”事件，让 Sniffer 合并 resource_type/mime；
                    # 否则无后缀的 HLS API 或已先由 request 命中的资源会丢失 HLS 上下文。
                    if is_video_url and not is_video_mime:
                        return

                    if is_video_mime:
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
                        self._remember_media_request_headers(
                            url,
                            headers,
                            page_url=page_url,
                            source="PWR-RSP",
                            mime=content_type,
                        )
                        self._emit_detected_resource(
                            url=url,
                            headers=headers,
                            page_url=page_url,
                            title=title,
                            source="PWR-RSP",
                            mime=content_type,
                            force_emit=is_video_url,
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

    @staticmethod
    def _looks_like_playlist_url(url: str) -> bool:
        """Return True for HLS/DASH playlist or manifest URLs."""
        lower = (url or "").lower()
        return any(
            marker in lower
            for marker in (
                ".m3u8",
                ".mpd",
                "/hls/",
                "/dash/",
                "manifest",
                "playlist",
            )
        )

    @staticmethod
    def _site_key(url: str) -> str:
        """Best-effort site key for deciding safe same-site cookie fallback."""
        try:
            host = (urlparse(url or "").hostname or "").lower().strip(".")
        except ValueError:
            return ""
        if not host:
            return ""
        labels = [label for label in host.split(".") if label]
        if len(labels) >= 3 and labels[-2] in {"co", "com", "net", "org", "edu", "gov"}:
            return ".".join(labels[-3:])
        if len(labels) >= 3:
            return ".".join(labels[-2:])
        return host

    @classmethod
    def _is_same_site(cls, left_url: str, right_url: str) -> bool:
        """Return True when two URLs are likely same-site."""
        left = cls._site_key(left_url)
        right = cls._site_key(right_url)
        return bool(left and right and left == right)

    @staticmethod
    def _has_short_lived_signature_params(url: str) -> bool:
        """Detect signed/ephemeral query parameters that should disable fuzzy matching."""
        try:
            return is_probably_ephemeral_media_url(url or "")
        except Exception:
            return True

    @staticmethod
    def _score_forward_headers(headers: dict | None) -> int:
        """Score header completeness for replay/merge decisions."""
        if not isinstance(headers, dict):
            return 0
        normalized = {str(key).lower(): value for key, value in headers.items() if value}
        score = 0
        if normalized.get("cookie"):
            score += 50
        if normalized.get("referer"):
            score += 20
        if normalized.get("origin"):
            score += 15
        if normalized.get("user-agent"):
            score += 15
        if normalized.get("authorization"):
            score += 12
        if normalized.get("accept"):
            score += 4
        if normalized.get("accept-language"):
            score += 5
        for key in normalized:
            if key.startswith("sec-fetch-") or key.startswith("sec-ch-ua"):
                score += 2
        return score + min(len(normalized), 12)

    def _media_header_cache(self) -> OrderedDict:
        """Return the media header cache, creating it for unit-test instances."""
        cache = getattr(self, "_recent_media_request_headers", None)
        if cache is None:
            cache = OrderedDict()
            self._recent_media_request_headers = cache
        return cache

    def _media_context_cache(self) -> OrderedDict:
        """Return recently emitted media contexts for refresh/re-capture matching."""
        cache = getattr(self, "_recent_media_contexts", None)
        if cache is None:
            cache = OrderedDict()
            self._recent_media_contexts = cache
        return cache

    @staticmethod
    def _url_host_path_key(url: str) -> tuple[str, str]:
        try:
            parsed = urlparse(url or "")
        except ValueError:
            return "", ""
        return (parsed.hostname or "").lower(), parsed.path or ""

    def _remember_emitted_media_context(self, context: dict) -> None:
        """Record an emitted media context so refresh-and-capture can return it synchronously."""
        if not isinstance(context, dict):
            return
        url = str(context.get("url") or "")
        if not url:
            return
        cache = self._media_context_cache()
        record = dict(context)
        record["captured_at_monotonic"] = time.monotonic()
        cache[url] = record
        cache.move_to_end(url)
        while len(cache) > 200:
            cache.popitem(last=False)

    def _select_refresh_media_context(
        self,
        *,
        page_url: str = "",
        previous_resource_url: str = "",
        since_monotonic: float = 0.0,
    ) -> dict:
        """Pick the best recently captured media context for a refreshed page."""
        cache = self._media_context_cache()
        if not cache:
            return {}

        previous_host, previous_path = self._url_host_path_key(previous_resource_url)
        best: tuple[int, float, dict] | None = None
        for record in cache.values():
            try:
                captured_at = float(record.get("captured_at_monotonic", 0.0) or 0.0)
            except (TypeError, ValueError):
                captured_at = 0.0
            if captured_at < since_monotonic:
                continue
            url = str(record.get("url") or "")
            resource_type = str(record.get("resource_type") or "").lower()
            mime = str(record.get("mime") or "").lower()
            if not url:
                continue
            looks_like_media = (
                self._is_video_url(url)
                or resource_type in {"hls", "dash", "direct_video"}
                or "mpegurl" in mime
                or "dash+xml" in mime
                or mime.startswith("video/")
            )
            if not looks_like_media:
                continue

            host, path = self._url_host_path_key(url)
            score = 0
            if previous_host and host == previous_host:
                score += 40
                if previous_path and path == previous_path:
                    score += 60
            elif previous_resource_url and self._is_same_site(url, previous_resource_url):
                score += 25
            if page_url and str(record.get("page_url") or "") == page_url:
                score += 25
            elif page_url and self._is_same_site(str(record.get("page_url") or ""), page_url):
                score += 10
            if resource_type in {"hls", "dash"} or "mpegurl" in mime or "dash+xml" in mime:
                score += 30
            elif self._looks_like_playlist_url(url):
                score += 20
            source = str(record.get("capture_source") or record.get("source") or "")
            if source.endswith("RSP"):
                score += 8
            elif source.endswith("REQ"):
                score += 6
            elif source.endswith("CAP"):
                score += 2
            if previous_resource_url and url != previous_resource_url:
                score += 5

            if best is None or score > best[0] or (score == best[0] and captured_at > best[1]):
                best = (score, captured_at, dict(record))

        if best is None or best[0] <= 0:
            return {}
        result = dict(best[2])
        result.pop("captured_at_monotonic", None)
        return result

    def _purge_recent_media_request_headers(self, now: float | None = None) -> None:
        """Expire old cached media request headers and enforce capacity."""
        cache = self._media_header_cache()
        if not cache:
            return
        now = time.monotonic() if now is None else now
        ttl = float(getattr(self, "_media_header_cache_ttl_seconds", 600) or 600)
        max_items = int(getattr(self, "_media_header_cache_max_items", 800) or 800)
        expired = [
            url
            for url, record in cache.items()
            if now - float(record.get("captured_at", 0.0) or 0.0) > ttl
        ]
        for url in expired:
            cache.pop(url, None)
        while len(cache) > max_items:
            cache.popitem(last=False)

    def _remember_media_request_headers(
        self,
        url: str,
        headers: dict | None,
        *,
        page_url: str = "",
        source: str = "",
        resource_type: str = "",
        mime: str = "",
    ) -> bool:
        """Cache sanitized real browser request headers for later PWR-CAP replay."""
        if not url or not headers:
            return False
        features = config.get("features", {}) or {}
        if not bool(features.get("exact_request_header_replay_enabled", True)):
            return False
        include_cookie = bool(features.get("forward_cookie_headers", True))
        include_authorization = bool(features.get("forward_authorization_headers", False))
        normalized = normalized_forward_headers(
            headers,
            include_cookie=include_cookie,
            include_authorization=include_authorization,
        )
        if include_authorization and normalized.get("authorization"):
            normalized["_allow_authorization_header"] = True
        if not normalized:
            return False

        now = time.monotonic()
        self._purge_recent_media_request_headers(now)
        cache = self._media_header_cache()
        score = self._score_forward_headers(normalized)
        existing = cache.get(url)
        if existing:
            old_score = int(existing.get("score", 0) or 0)
            if old_score > score:
                if mime and not existing.get("mime"):
                    existing["mime"] = mime
                if resource_type and not existing.get("resource_type"):
                    existing["resource_type"] = resource_type
                return False
            cache.pop(url, None)

        cache[url] = {
            "headers": dict(normalized),
            "page_url": page_url or "",
            "source": source or "unknown",
            "resource_type": resource_type or "",
            "mime": mime or "",
            "captured_at": now,
            "score": score,
        }
        logger.debug(
            "[PWR-HDR] 已缓存真实媒体请求头",
            event="playwright_media_headers_cached",
            source=source or "unknown",
            resource_type=resource_type or "unknown",
            mime=mime or "",
            score=score,
            has_cookie=bool(normalized.get("cookie")),
            cookie_len=len(normalized.get("cookie", "") or ""),
            has_referer=bool(normalized.get("referer")),
            has_origin=bool(normalized.get("origin")),
            has_user_agent=bool(normalized.get("user-agent")),
        )
        return True

    def _lookup_recent_media_request_headers(self, url: str) -> dict:
        """Return recently captured real headers for url without mutating cache records."""
        if not url:
            return {}
        features = config.get("features", {}) or {}
        if not bool(features.get("exact_request_header_replay_enabled", True)):
            return {}
        self._purge_recent_media_request_headers()
        cache = self._media_header_cache()
        record = cache.get(url)
        if record:
            cache.move_to_end(url)
            logger.debug(
                "[PWR-HDR] 真实请求头缓存精确命中",
                event="playwright_media_headers_replay_hit",
                match_type="exact",
                source=record.get("source", "unknown"),
                score=record.get("score", 0),
            )
            return dict(record.get("headers") or {})

        try:
            target = urlparse(url)
        except ValueError:
            return {}
        if target.query or self._has_short_lived_signature_params(url):
            return {}
        target_host = (target.hostname or "").lower()
        target_path = target.path or ""
        if not target_host or not target_path:
            return {}

        for cached_url, cached_record in reversed(list(cache.items())):
            if self._has_short_lived_signature_params(cached_url):
                continue
            try:
                cached = urlparse(cached_url)
            except ValueError:
                continue
            if (
                (cached.hostname or "").lower() == target_host
                and (cached.path or "") == target_path
            ):
                cache.move_to_end(cached_url)
                logger.debug(
                    "[PWR-HDR] 真实请求头缓存路径命中",
                    event="playwright_media_headers_replay_hit",
                    match_type="host_path",
                    source=cached_record.get("source", "unknown"),
                    score=cached_record.get("score", 0),
                )
                return dict(cached_record.get("headers") or {})
        return {}

    @staticmethod
    def _cookie_header_from_cookie_items(cookies: list[dict] | tuple[dict, ...]) -> str:
        """Build and sanitize a Cookie header from Playwright cookie dictionaries."""
        pairs: list[str] = []
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            value = cookie.get("value")
            if value is None:
                value = ""
            pairs.append(f"{name}={value}")
        if not pairs:
            return ""
        normalized = normalized_forward_headers(
            {"Cookie": "; ".join(pairs)},
            include_cookie=True,
            include_authorization=False,
        )
        return normalized.get("cookie", "")

    def _cookie_header_for_resource(self, page_url: str, resource_url: str) -> str:
        """Prefer resource-domain cookies and only fall back to page cookies for same-site URLs."""
        context = getattr(self, "context", None)
        page = getattr(self, "page", None)
        if context is None and page is not None:
            context = getattr(page, "context", None)
        if context is None:
            return ""

        features = config.get("features", {}) or {}
        resource_cookie_lookup_enabled = bool(
            features.get(
                "resource_domain_cookie_lookup_enabled",
                getattr(self, "_resource_domain_cookie_lookup_enabled", True),
            )
        )
        if not resource_cookie_lookup_enabled:
            try:
                return self._cookie_header_from_cookie_items(context.cookies(page_url or resource_url))
            except Exception as e:
                logger.debug(
                    f"[PWR-HDR] 获取 Cookie 失败: {e}",
                    event="playwright_headers_cookie_failed",
                    stage="build_headers",
                    error_type=type(e).__name__,
                )
                return ""

        try:
            resource_cookies = context.cookies(resource_url) if resource_url else []
        except Exception as e:
            logger.debug(
                f"[PWR-HDR] 获取资源域 Cookie 失败: {e}",
                event="playwright_resource_cookie_failed",
                stage="build_headers",
                error_type=type(e).__name__,
            )
            resource_cookies = []

        same_site = bool(page_url and resource_url and self._is_same_site(page_url, resource_url))
        page_cookies = []
        if same_site and page_url:
            try:
                page_cookies = context.cookies(page_url)
            except Exception as e:
                logger.debug(
                    f"[PWR-HDR] 获取页面域 Cookie 失败: {e}",
                    event="playwright_page_cookie_failed",
                    stage="build_headers",
                    error_type=type(e).__name__,
                )
                page_cookies = []

        merged_by_name: dict[str, dict] = {}
        if same_site:
            for cookie in page_cookies:
                name = str((cookie or {}).get("name") or "")
                if name:
                    merged_by_name[name] = cookie
        for cookie in resource_cookies:
            name = str((cookie or {}).get("name") or "")
            if name:
                merged_by_name[name] = cookie

        cookie_header = self._cookie_header_from_cookie_items(list(merged_by_name.values()))
        logger.debug(
            "[PWR-COOKIE] 资源域 Cookie 查询摘要",
            event="playwright_resource_cookie_lookup",
            same_site=same_site,
            resource_cookie_count=len(resource_cookies or []),
            page_cookie_count=len(page_cookies or []),
            cookie_len=len(cookie_header),
        )
        return cookie_header

    def _build_default_headers(self, page_url: str, resource_url: str, headers: dict | None = None) -> dict:
        """Attach stable headers for internal browser capture."""
        features = config.get("features", {}) or {}
        include_cookie = bool(features.get("forward_cookie_headers", True))
        include_authorization = bool(features.get("forward_authorization_headers", False))
        merged = normalized_forward_headers(
            headers or {},
            include_cookie=include_cookie,
            include_authorization=include_authorization,
        )
        if include_authorization and merged.get("authorization"):
            merged["_allow_authorization_header"] = True
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

        if include_cookie and self._looks_like_playlist_url(resource_url) and not merged.get("cookie"):
            cookie_str = self._cookie_header_for_resource(page_url, resource_url)
            if cookie_str:
                merged["cookie"] = cookie_str

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

    def _emit_detected_resource(
        self,
        url: str,
        headers: dict,
        page_url: str,
        title: str,
        source: str,
        *,
        mime: str = "",
        resource_type: str = "",
        master_url: str | None = None,
        media_url: str | None = None,
        force_emit: bool = False,
    ):
        """Emit detected resource with normalized headers and lightweight dedup."""
        normalized_url = self._normalize_emit_url(url, page_url)
        if not normalized_url:
            return
        if not force_emit and self._is_recent_emit(normalized_url):
            return
        if force_emit:
            self._recent_emit_cache[normalized_url] = time.monotonic()
        replay_headers = self._lookup_recent_media_request_headers(normalized_url)
        input_headers = headers or {}
        header_seed = {}
        if replay_headers:
            header_seed.update(replay_headers)
        if input_headers:
            header_seed.update(input_headers)
        merged_headers = self._build_default_headers(page_url, normalized_url, header_seed)
        cookie_len = len(merged_headers.get("cookie", "") or "")
        logger.info(
            "[PWR-HDR] resource header 摘要: "
            f"source={source} header_source="
            f"{'explicit' if input_headers else ('replay' if replay_headers else 'default')} "
            f"has_cookie={cookie_len > 0} cookie_len={cookie_len} "
            f"has_referer={bool(merged_headers.get('referer'))} "
            f"has_origin={bool(merged_headers.get('origin'))} "
            f"has_user_agent={bool(merged_headers.get('user-agent'))} "
            f"resource_type={resource_type or 'unknown'} mime={mime or ''}",
            event="playwright_resource_headers_summary",
            source=source,
            header_source="explicit" if input_headers else ("replay" if replay_headers else "default"),
            has_cookie=cookie_len > 0,
            cookie_len=cookie_len,
            has_referer=bool(merged_headers.get("referer")),
            has_origin=bool(merged_headers.get("origin")),
            has_user_agent=bool(merged_headers.get("user-agent")),
            resource_type=resource_type or "unknown",
            mime=mime or "",
        )

        # Emit unified context signal (download-context unification).
        from core.download_context import build_engine_select_context, SOURCE_INTERNAL_BROWSER

        context = build_engine_select_context(
            url=normalized_url,
            headers=merged_headers,
            page_url=page_url,
            page_title=title,
            source=SOURCE_INTERNAL_BROWSER if (source or "").startswith("PWR") else source,
            resource_type=resource_type,
            mime=mime,
            master_url=master_url,
            media_url=media_url,
        )
        context_payload = {
            "url": context.url,
            "headers": dict(context.headers or {}),
            "page_url": context.page_url,
            "page_title": context.page_title,
            "source": context.source,
            "resource_type": context.resource_type,
            "mime": context.mime,
            "master_url": context.master_url,
            "media_url": context.media_url,
            "capture_source": source,
        }
        self._remember_emitted_media_context(context_payload)
        self.resource_context_detected.emit(context_payload)

        # Preserve old signal for backward compatibility.
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

    def go_back(self):
        """浏览器后退（线程安全）"""
        self._action_queue.append(("back", None))

    def go_forward(self):
        """浏览器前进（线程安全）"""
        self._action_queue.append(("forward", None))

    def reload(self):
        """浏览器刷新（线程安全）"""
        self._action_queue.append(("reload", None))

    def new_tab(self, url: str = "about:blank"):
        """新建标签页（线程安全），可选在新标签页导航到指定 URL"""
        self._action_queue.append(("new_tab", url))

    def refresh_and_wait_for_media(
        self,
        *,
        page_url: str = "",
        previous_resource_url: str = "",
        timeout_ms: int = 15000,
    ) -> dict:
        """Refresh/reopen the current page and wait for a matching media URL."""
        timeout_ms = max(3000, min(60000, int(timeout_ms or 15000)))
        done = threading.Event()
        holder: dict = {}
        self._action_queue.append(
            (
                "refresh_and_wait_for_media",
                {
                    "page_url": page_url or "",
                    "previous_resource_url": previous_resource_url or "",
                    "timeout_ms": timeout_ms,
                    "done": done,
                    "holder": holder,
                },
            )
        )
        if not done.wait((timeout_ms / 1000.0) + 5.0):
            return {"ok": False, "reason": "timeout", "context": {}}
        return holder.get("result") or {"ok": False, "reason": "empty_result", "context": {}}

    @staticmethod
    def _finish_action_result(args, result) -> None:
        if not isinstance(args, dict):
            return
        holder = args.get("holder")
        if isinstance(holder, dict):
            holder["result"] = result
        done = args.get("done")
        if hasattr(done, "set"):
            done.set()

    def _handle_refresh_and_wait_for_media_action(self, page, args: dict) -> None:
        """Implementation for refresh_and_wait_for_media, executed on the Playwright thread."""
        previous_resource_url = str(args.get("previous_resource_url") or "")
        page_url = str(args.get("page_url") or "")
        try:
            timeout_ms = max(3000, min(60000, int(args.get("timeout_ms") or 15000)))
        except (TypeError, ValueError):
            timeout_ms = 15000

        started = time.monotonic()
        deadline = started + (timeout_ms / 1000.0)
        try:
            if previous_resource_url:
                self._recent_emit_cache.pop(previous_resource_url, None)
            target_url = page_url or getattr(page, "url", "") or ""
            self._begin_capture_window("ephemeral_refresh")
            if target_url and target_url != "about:blank" and getattr(page, "url", "") != target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
            else:
                page.reload(wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
            self._last_detected_url = ""
        except Exception as exc:
            logger.warning(
                f"[PWR-REFRESH] 页面刷新/重开失败: {exc}",
                event="playwright_refresh_media_navigation_failed",
                stage="refresh_and_wait_for_media",
                error_type=type(exc).__name__,
            )

        while time.monotonic() < deadline:
            context = self._select_refresh_media_context(
                page_url=page_url,
                previous_resource_url=previous_resource_url,
                since_monotonic=started,
            )
            if context:
                self._finish_action_result(
                    args,
                    {"ok": True, "reason": "captured", "context": context},
                )
                return
            try:
                self._probe_dynamic_media_urls()
                page.wait_for_timeout(250)
            except Exception as exc:
                logger.debug(
                    f"[PWR-REFRESH] 等待媒体资源时出错: {exc}",
                    event="playwright_refresh_media_wait_error",
                    stage="refresh_and_wait_for_media",
                    error_type=type(exc).__name__,
                )
                break

        self._finish_action_result(
            args,
            {"ok": False, "reason": "no_media_captured", "context": {}},
        )

    def _handle_action(self, action: str, args):
        """在事件循环线程中执行浏览器操作"""
        # Cookie export only needs BrowserContext and must stay on the Playwright
        # thread; it should not depend on a live page object.
        if action == "export_cookies":
            args_dict = args if isinstance(args, dict) else {}
            try:
                path = self._export_cookies_to_file_impl(
                    url=args_dict.get("url"),
                    domain_filter=args_dict.get("domain_filter"),
                )
                self._finish_action_result(args_dict, path)
            except Exception as e:
                logger.error(
                    f"导出 cookies 失败: {e}",
                    event="playwright_export_cookies_failed",
                    stage="export_cookies",
                    error_type=type(e).__name__,
                )
                self._finish_action_result(args_dict, None)
            return

        try:
            if action == "new_tab":
                url = args if args else "about:blank"
                new_page = self.context.new_page()
                # 新建标签页后必须立即成为驱动层活动页；后续地址栏导航、刷新、
                # 后退/前进都通过 self.page 分发，否则会继续命中旧视频页。
                self.page = new_page
                self._last_detected_url = ""
                self._setup_page(new_page)
                try:
                    new_page.bring_to_front()
                except AttributeError:
                    pass
                except Exception as e:
                    logger.debug(
                        f"[PWR-ACTION] 新标签页置前失败: {e}",
                        event="playwright_new_tab_bring_to_front_failed",
                        stage="handle_action",
                        error_type=type(e).__name__,
                    )
                logger.info(
                    f"[Playwright] + 新标签页: {url}",
                    event="playwright_new_tab",
                    url=str(url),
                )
                if url != "about:blank":
                    new_page.goto(
                        url if url.startswith("http") else "https://" + url,
                        wait_until='domcontentloaded',
                        timeout=30000,
                    )
                else:
                    self._begin_capture_window("new_tab")
                return

            page = self.page
            if not page or page.is_closed():
                logger.warning(
                    f"[PWR-ACTION] 无法执行 {action}：当前页面不可用",
                    event="playwright_action_no_page",
                    stage="handle_action",
                    action=action,
                )
                if action == "refresh_and_wait_for_media":
                    self._finish_action_result(args, {"ok": False, "reason": "no_page", "context": {}})
                return

            if action == "back":
                if page.go_back(wait_until='domcontentloaded', timeout=15000):
                    logger.info("[Playwright] ← 后退", event="playwright_nav_back")
                    self._last_detected_url = ""
                    self._begin_capture_window("back")
            elif action == "forward":
                if page.go_forward(wait_until='domcontentloaded', timeout=15000):
                    logger.info("[Playwright] → 前进", event="playwright_nav_forward")
                    self._last_detected_url = ""
                    self._begin_capture_window("forward")
            elif action == "reload":
                page.reload(wait_until='domcontentloaded', timeout=15000)
                logger.info("[Playwright] ↻ 刷新", event="playwright_nav_reload")
                self._last_detected_url = ""
                self._begin_capture_window("reload")
            elif action == "refresh_and_wait_for_media":
                self._handle_refresh_and_wait_for_media_action(page, args if isinstance(args, dict) else {})
        except Exception as e:
            err_msg = str(e).lower()
            if 'timeout' in err_msg:
                logger.warning(
                    f"[PWR-ACTION] {action} 超时",
                    event="playwright_action_timeout",
                    stage="handle_action",
                    action=action,
                )
            else:
                logger.debug(
                    f"[PWR-ACTION] {action} 执行异常: {e}",
                    event="playwright_action_error",
                    stage="handle_action",
                    action=action,
                    error_type=type(e).__name__,
                )
            if action == "refresh_and_wait_for_media":
                self._finish_action_result(args, {"ok": False, "reason": "action_error", "context": {}})

    def stop(self):
        """停止驱动"""
        self.active = False
        self.wait()

    def export_cookies_to_file(self, url: str = None, domain_filter: str = None) -> str | None:
        """
        Export browser cookies to a Netscape-format file (yt-dlp compatible).

        Playwright sync APIs are greenlet-bound to the driver thread. Callers
        (download workers / UI) must not touch ``context.cookies()`` directly;
        this method marshals the export onto the Playwright thread via the
        action queue, matching ``refresh_and_wait_for_media``.

        Args:
            url: Optional URL used to scope cookies by Playwright context.
            domain_filter: Optional domain keyword filter.

        Returns:
            Cookie file path on success, otherwise None.
        """
        if not self.active:
            logger.warning(
                "无法导出 cookies：浏览器驱动未运行",
                event="playwright_export_cookies_inactive",
                stage="export_cookies",
            )
            return None

        # Already on the Playwright thread — run inline to avoid deadlock.
        try:
            if QThread.currentThread() is self:
                return self._export_cookies_to_file_impl(url=url, domain_filter=domain_filter)
        except RuntimeError:
            # QThread identity may be unavailable outside a Qt app; fall through
            # to the queue path used by worker threads.
            pass

        done = threading.Event()
        holder: dict = {}
        self._action_queue.append(
            (
                "export_cookies",
                {
                    "url": url,
                    "domain_filter": domain_filter,
                    "done": done,
                    "holder": holder,
                },
            )
        )
        if not done.wait(10.0):
            logger.error(
                "导出 cookies 超时：Playwright 线程未响应",
                event="playwright_export_cookies_timeout",
                stage="export_cookies",
            )
            return None
        return holder.get("result")

    def _export_cookies_to_file_impl(self, url: str = None, domain_filter: str = None) -> str | None:
        """
        Perform cookie export on the Playwright driver thread.

        Must only be invoked from the driver event loop (or via
        ``export_cookies_to_file`` which guarantees that affinity).
        """
        from core.app_paths import get_data_root

        try:
            if not self.context:
                logger.warning("无法导出 cookies：浏览器上下文未初始化")
                return None

            cookies = self.context.cookies(url) if url else self.context.cookies()
            if not cookies:
                logger.info("浏览器中没有 cookies")
                return None

            # If URL is provided but explicit domain filter is not, derive one from URL.
            export_target_host = ""
            if url:
                try:
                    export_target_host = (urlparse(url).hostname or "").lower()
                except Exception:
                    export_target_host = ""
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

            cookie_dir = get_data_root() / "cookies"
            cookie_dir.mkdir(parents=True, exist_ok=True)
            cookie_file = cookie_dir / "browser_cookies.txt"

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

            matched_domains = sorted({str(c.get('domain', '')).lower() for c in cookies if c.get('domain')})
            logger.info(
                f"已导出 {len(cookies)} 个 cookies 到: {cookie_file}",
                event="playwright_cookies_exported",
                stage="export_cookies",
                cookie_count=len(cookies),
                matched_domain_count=len(matched_domains),
                target_host=export_target_host,
                domain_filter=(domain_filter or ""),
            )
            return str(cookie_file)

        except Exception as e:
            logger.error(
                f"导出 cookies 失败: {e}",
                event="playwright_export_cookies_failed",
                stage="export_cookies",
                error_type=type(e).__name__,
            )
            return None
