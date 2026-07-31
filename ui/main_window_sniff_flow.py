"""Sniff/download-coordination mixin for :class:`MainWindow`.

security-stability-hardening Task 25.2 / Requirements 26.2, 26.5.

This module carves sniffer-driven download coordination out of
``ui/main_window.py`` so the main window file stays under the
≤700-line budget mandated by R26. The mixin contains:

* The resource-discovery callback (``_on_resource_found``) that drives
  the Resources tab.
* The download-request entry point (``_on_download_requested``) and
  the title-composition helpers it leans on
  (``_resolve_download_title_source``, ``_get_variant_title_suffix``,
  ``_compose_download_title``).
* The yt-dlp format-selection dialog (``_show_format_dialog``) and
  the HLS variant-selection dialog (``_show_m3u8_variant_dialog``).
* ``_start_download`` — the single write-seam that constructs a
  :class:`DownloadTask` and hands it to :class:`DownloadManager`.
* The CatCatch browser-extension bridge (``_on_catcatch_download``).

These methods only access attributes that ``MainWindow.__init__``
already publishes on ``self`` (``self.download_manager``, ``self.engines``,
``self.resource_panel``, ``self.main_tabs``, …); they are therefore
safe to host on a mixin and participate in ``MainWindow``'s MRO.

All behaviour is preserved bit-for-bit from the pre-split
``ui/main_window.py``; splitting is purely a code-layout refactor and
is covered by the Stage 3 regression list.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
import time
import urllib.parse

from PyQt6.QtCore import QObject, Qt, pyqtSlot
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

from core.download_context import (
    build_engine_select_context,
    context_from_resource,
    RESOURCE_TYPE_HLS,
    SOURCE_CATCATCH,
    SOURCE_INTERNAL_BROWSER,
)
from core.m3u8_parser import M3U8FetchThread, analyze_playlist_diagnostics
from core.media_url_ttl import TTL_WARNING_SECONDS
from core.task_model import DownloadTask, M3U8Resource
from utils.config_manager import config
from utils.i18n import TR
from utils.logger import logger
from utils.win_path import sanitize_title

if TYPE_CHECKING:  # pragma: no cover - type-checker only
    pass


class _MainThreadListRelay(QObject):
    """Deliver worker-thread list payloads through a QObject slot on the UI thread."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    @pyqtSlot(list)
    def handle_list(self, payload):
        try:
            self._callback(payload)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            logger.error(
                f"[UI] 后台线程结果处理失败: {exc}",
                event="ui_worker_result_handler_failed",
                error_type=type(exc).__name__,
            )


class MainWindowSniffFlowMixin:
    """Sniffer-coordination slots for :class:`MainWindow`.

    Mixed in ahead of :class:`PyQt6.QtWidgets.QMainWindow` so standard
    attribute lookups on ``self`` reach ``MainWindow``'s instance
    attributes (``download_manager``, ``engines``, ``resource_panel``
    …) that :meth:`MainWindow.__init__` assigns before any slot fires.
    """

    # ------------------------------------------------------------------
    # Resource discovery
    # ------------------------------------------------------------------
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

        # 仅在首次进入资源列表时绑定当前引擎选择；重复资源刷新保留既有选择
        is_existing_resource = any(
            current_resource is resource
            for current_resource, _engine_name in getattr(self.resource_panel, "resources", [])
        )
        if not is_existing_resource:
            resource.selected_engine = self.get_selected_engine()
        user_engine = getattr(resource, "selected_engine", None)

        # 预测探测阶段将优先尝试的引擎，用于资源列表展示
        from core.engine_selector import EngineSelector
        selector = EngineSelector(self.engines)
        selector_ctx = context_from_resource(resource)
        _, engine_name = selector.predict(resource.url, user_engine, context=selector_ctx)

        # 添加到资源面板
        self.resource_panel.add_resource(resource, engine_name)

    # ------------------------------------------------------------------
    # Title composition helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_download_title_source(title_source) -> str:
        """在创建下载任务前解析最新标题。"""
        if isinstance(title_source, M3U8Resource):
            if getattr(title_source, "is_variant", False):
                parent_resource = getattr(title_source, "variant_parent_resource", None)
                if parent_resource is not None:
                    parent_title = M3U8Resource._sanitize_title(getattr(parent_resource, "title", ""))
                    if not parent_title:
                        parent_title = M3U8Resource._sanitize_title(getattr(parent_resource, "page_title", ""))
                    if not parent_title:
                        parent_title = parent_resource._extract_title()

                    quality_label = M3U8Resource._sanitize_title(getattr(title_source, "quality_label", ""))
                    variant_title = parent_title
                    if quality_label and quality_label not in variant_title:
                        variant_title = f"{variant_title} [{quality_label}]"
                    title_source.title = variant_title
                    if getattr(parent_resource, "page_title", ""):
                        title_source.page_title = parent_resource.page_title

            latest_title = M3U8Resource._sanitize_title(getattr(title_source, "title", ""))
            if latest_title:
                return latest_title

            latest_page_title = M3U8Resource._sanitize_title(getattr(title_source, "page_title", ""))
            if latest_page_title:
                return latest_page_title

            return title_source._extract_title()

        normalized_title = M3U8Resource._sanitize_title(str(title_source or ""))
        return normalized_title or "untitled_video"

    @staticmethod
    def _get_variant_title_suffix(variant: dict) -> str:
        """从变体信息提取用于文件名的清晰度后缀。"""
        if not variant:
            return ""

        resolution = str(variant.get('resolution') or "").strip()
        if resolution:
            return resolution

        height = variant.get('height', 0)
        if height:
            return f"{height}p"

        return ""

    def _compose_download_title(self, title_source, title_suffix: str = "") -> str:
        """基于最新标题来源拼接最终下载文件名。"""
        title = self._resolve_download_title_source(title_source)
        suffix = M3U8Resource._sanitize_title(title_suffix or "")
        if suffix and suffix not in title:
            title = f"{title} [{suffix}]"
        # Final filename-construction point: run through the strong
        # Windows-safe sanitizer from utils.win_path so reserved names,
        # trailing dot/space, and >240-byte UTF-8 are all handled at the
        # single boundary between title resolution and DownloadTask.filename.
        # See tasks.md 12.2.
        return sanitize_title(title)

    # ------------------------------------------------------------------
    # Download request entry point
    # ------------------------------------------------------------------
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
                        QMessageBox.warning(self, "无效链接", "这不是一个有效的 YouTube 视频链接。\n请选择包含具体视频的资源。")
                        return

                # 显示分辨率选择对话框（yt-dlp 会自动使用 Firefox cookies 作为回退）
                self._show_format_dialog(download_url, resource, headers, None, user_engine)
                return

            # M3U8/HLS 资源：尝试解析多码率。不能只看 URL 后缀，
            # 一些站点会用无 .m3u8 后缀的 API 地址返回 mpegurl MIME。
            resource_type_hint = (getattr(resource, "resource_type", "") or "").strip().lower()
            mime_hint = (getattr(resource, "mime", "") or "").strip().lower()
            is_m3u8 = (
                '.m3u8' in download_url.lower()
                or resource_type_hint == RESOURCE_TYPE_HLS
                or 'mpegurl' in mime_hint
            )
            if is_m3u8:
                # 变体子资源已有明确分辨率和 URL，无需再次分析 M3U8 播放列表
                if getattr(resource, "is_variant", False) and getattr(resource, "variant_info", None):
                    variant_info = resource.variant_info
                    variant_url = variant_info.get('url', download_url)
                    parent = getattr(resource, "variant_parent_resource", None)
                    master_url = (
                        getattr(resource, "master_url", None)
                        or (getattr(parent, "master_url", None) if parent else None)
                        or (parent.url if parent else download_url)
                    )
                    # 补齐变体资源缺失的 master_url / media_url
                    if not getattr(resource, "master_url", None):
                        resource.master_url = master_url
                    if not getattr(resource, "media_url", None):
                        resource.media_url = variant_url
                    context_kwargs = self._resource_context_kwargs(resource)
                    logger.info(
                        "[QUEUE] 变体资源直接下载，跳过 M3U8 分析",
                        event="ui_variant_direct_download",
                        title=getattr(resource, "title", ""),
                        quality=getattr(resource, "quality_label", ""),
                    )
                    self._start_download(
                        variant_url,
                        resource,
                        headers,
                        user_engine,
                        **context_kwargs,
                    )
                    return

                # 复用资源面板已缓存的 variants（避免重复网络请求）
                cached_variants = resource.variants if hasattr(resource, 'variants') else []
                m3u8_url = getattr(resource, "master_url", None) or download_url
                self._show_m3u8_variant_dialog(m3u8_url, resource, headers, user_engine, cached_variants)
                return

            # 其他资源，直接下载
            context_kwargs = self._resource_context_kwargs(resource)
            self._start_download(download_url, resource, headers, user_engine, **context_kwargs)
        except Exception as e:
            logger.error(
                f"[QUEUE] 添加到下载队列主链路异常: {e}",
                event="ui_download_request_failed",
                title=getattr(resource, "title", ""),
                url=getattr(resource, "url", ""),
            )
            QMessageBox.critical(self, "添加失败", f"添加到下载队列失败：\n{e}")

    # ------------------------------------------------------------------
    # Format / variant selection dialogs
    # ------------------------------------------------------------------
    def _show_format_dialog(self, url: str, title_source, headers: dict, cookie_file=None, user_engine=None):
        """显示分辨率选择对话框（支持 yt-dlp 所有平台）- 异步获取格式避免卡顿"""
        from ui.format_dialog import FormatSelectionDialog
        from PyQt6.QtCore import QThread, pyqtSignal

        # 获取 yt-dlp 引擎
        ytdlp_engine = None
        for engine in self.download_manager.engines:
            if engine.get_name() == 'yt-dlp':
                ytdlp_engine = engine
                break

        if not ytdlp_engine:
            logger.warning("未找到 yt-dlp 引擎，使用最佳质量下载")
            self._start_download(url, title_source, headers, user_engine)
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

            # ISS-35/36: 清理已完成的后台线程，防止线程泄漏
            _cleanup_format_thread()

            if not formats:
                logger.warning("无法获取视频格式，将使用最佳质量下载")
                # 将 cookie 文件路径添加到 headers 中
                if cookie_file:
                    headers['_cookie_file'] = cookie_file
                self._start_download(url, title_source, headers, user_engine)
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
                    self._start_download(download_url, title_source, headers, user_engine)
                else:
                    logger.info("用户选择最佳质量下载")
                    if cookie_file:
                        headers['_cookie_file'] = cookie_file
                    self._start_download(url, title_source, headers, user_engine)
            else:
                logger.info("用户取消了下载")

        def _cleanup_format_thread():
            """ISS-35: 安全清理 FormatFetchThread，防止 QThread 泄漏。"""
            t = getattr(self, "_format_thread", None)
            if t is None:
                return
            try:
                if t.isRunning():
                    t.quit()
                    t.wait(2000)
            except RuntimeError:
                pass
            try:
                t.deleteLater()
            except RuntimeError:
                pass
            self._format_thread = None

        self._format_thread.finished.connect(on_formats_ready)
        self._format_thread.start()

        logger.info("正在获取视频格式...")

    def _show_m3u8_variant_dialog(self, url: str, title_source, headers: dict, user_engine=None, cached_variants=None):
        """显示 M3U8 清晰度选择对话框"""
        from ui.format_dialog import FormatSelectionDialog

        def _selector_context():
            if isinstance(title_source, M3U8Resource):
                return context_from_resource(title_source)
            return build_engine_select_context(url=url, headers=headers)

        def _handle_variants(variants):
            """处理解析完成的 variants"""
            if not variants:
                logger.info("未找到多码率变体，直接下载原始链接")
                self._start_download(url, title_source, headers, user_engine, master_url=url, media_url=url)
                return

            # 显示选择对话框
            dialog = FormatSelectionDialog(variants, self)
            if dialog.exec():
                selected = dialog.get_selected_format()
                if selected:
                    resolution = selected.get('resolution', '')
                    height = selected.get('height', 0)
                    logger.info(f"用户选择了变体: {resolution}")

                    title_suffix = self._get_variant_title_suffix(selected)

                    # 判断当前是否使用 N_m3u8DL-RE 引擎
                    effective_engine = user_engine
                    if not effective_engine:
                        from core.engine_selector import EngineSelector
                        selector = EngineSelector(self.engines)
                        _, effective_engine = selector.select(url, None, context=_selector_context())

                    if effective_engine == 'N_m3u8DL-RE':
                        # N_m3u8DL-RE: 传递 master playlist URL + 选中的变体信息，让引擎原生处理
                        logger.info(f"N_m3u8DL-RE: 传递 master URL + --select-video 参数")
                        self._start_download(
                            url,
                            title_source,
                            headers,
                            user_engine,
                            title_suffix=title_suffix,
                            selected_variant=selected,
                            master_url=url,
                            media_url=selected.get('url', url),
                        )
                    else:
                        # 其他引擎: 直接使用变体 URL
                        variant_url = selected.get('url', url)
                        self._start_download(
                            variant_url,
                            title_source,
                            headers,
                            user_engine,
                            title_suffix=title_suffix,
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
                        _, effective_engine = selector.select(url, None, context=_selector_context())

                    if effective_engine == 'N_m3u8DL-RE':
                        # N_m3u8DL-RE: 传递 master URL + 最佳变体
                        self._start_download(
                            url,
                            title_source,
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
                            title_source,
                            headers,
                            user_engine,
                            master_url=url,
                            media_url=variant_url,
                        )
            else:
                logger.info("用户取消下载")

        # 如果已有缓存的 variants，直接使用，无需再次网络请求。
        if cached_variants:
            logger.info(f"使用缓存的 M3U8 变体列表 ({len(cached_variants)} 个)")
            _handle_variants(cached_variants)
            return

        # 如果资源列表后台解析已经尝试过且没有拿到 variants，不要在点击下载时
        # 再启动第二个 QThread 等同样的重试链路。直接交给下载引擎尝试，既减少
        # Windows/PyQt 线程收尾风险，也避免用户点击后继续等待 6~10 秒。
        if isinstance(title_source, M3U8Resource):
            parse_attempted = bool(getattr(title_source, "variants_parse_attempted", False))
            parse_failed = bool(getattr(title_source, "variants_parse_failed", False))
            parse_in_progress = bool(getattr(title_source, "variants_parse_in_progress", False))
            last_parse_url = getattr(title_source, "variants_parse_url", "") or getattr(title_source, "url", "")
            same_parse_target = not last_parse_url or last_parse_url == url
            if parse_attempted and (parse_failed or parse_in_progress) and same_parse_target:
                logger.info(
                    "M3U8 资源列表解析无可用变体，跳过下载前重复分析，直接入队",
                    event="ui_m3u8_variant_skip_reparse",
                    url=url,
                    parse_failed=parse_failed,
                    parse_in_progress=parse_in_progress,
                )
                _handle_variants([])
                return

        # 显示加载对话框
        progress = QProgressDialog(TR("msg_analyzing_m3u8"), TR("btn_cancel"), 0, 0, self)
        progress.setWindowTitle(TR("dialog_please_wait"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        # 启动后台线程解析。不要把 QThread 的结果直接连到普通 Python 回调；
        # 在 PyQt 中这类回调可能运行在工作线程里，随后创建对话框/更新队列会触发
        # 原生 Qt 崩溃。使用带父对象的 QObject relay，强制把结果切回主线程处理。
        thread = M3U8FetchThread(url, headers)
        self._m3u8_thread = thread  # 兼容旧调试入口，仅指向最近一次分析线程
        if not hasattr(self, "_m3u8_threads"):
            self._m3u8_threads = []
        self._m3u8_threads.append(thread)
        cancelled = {"value": False}

        def _request_cancel():
            cancelled["value"] = True
            try:
                thread.request_stop()
            except RuntimeError:
                pass
            logger.info("用户取消 M3U8 播放列表分析")

        progress.canceled.connect(_request_cancel)

        def _cleanup_m3u8_thread():
            """安全清理本次 M3U8FetchThread。

            不在 finished 回调链路里立即 deleteLater()/释放 QThread。
            Windows + PyQt 对已完成 QThread 的即时销毁偶发原生崩溃，且日志显示
            崩溃发生在任务入队后、回调收尾阶段。这里仅从活跃列表移出，并把线程
            与 relay 保留到窗口生命周期结束，让 Qt 自己在进程退出时统一回收。
            """
            logger.debug(
                "M3U8 下载前分析线程清理开始",
                event="ui_m3u8_thread_cleanup_start",
                url=url,
            )
            try:
                if thread.isRunning():
                    thread.request_stop()
                    thread.wait(2000)
            except RuntimeError:
                pass

            threads = getattr(self, "_m3u8_threads", None)
            if isinstance(threads, list):
                try:
                    threads.remove(thread)
                except ValueError:
                    pass

            if getattr(self, "_m3u8_thread", None) is thread:
                self._m3u8_thread = None

            if not hasattr(self, "_m3u8_retired_threads"):
                self._m3u8_retired_threads = []
            self._m3u8_retired_threads.append(thread)
            logger.debug(
                "M3U8 下载前分析线程清理完成",
                event="ui_m3u8_thread_cleanup_done",
                retired_count=len(self._m3u8_retired_threads),
                url=url,
            )

        def on_variants_ready(variants):
            diagnostics = getattr(thread, "playlist_diagnostics", {}) or {}
            if isinstance(title_source, M3U8Resource) and isinstance(diagnostics, dict) and diagnostics:
                self._apply_playlist_diagnostics_to_resource(title_source, diagnostics)

            try:
                progress.blockSignals(True)
                progress.close()
            except RuntimeError:
                pass
            finally:
                try:
                    progress.blockSignals(False)
                except RuntimeError:
                    pass

            try:
                if cancelled["value"]:
                    logger.info("M3U8 播放列表分析已取消，未添加下载任务")
                    return
                logger.debug(
                    "M3U8 变体处理开始",
                    event="ui_m3u8_variant_handle_start",
                    variant_count=len(variants or []),
                    url=url,
                )
                _handle_variants(variants)
                logger.debug(
                    "M3U8 变体处理完成",
                    event="ui_m3u8_variant_handle_done",
                    variant_count=len(variants or []),
                    url=url,
                )
            except Exception as exc:
                logger.error(
                    f"[QUEUE] M3U8 变体处理异常: {exc}",
                    event="ui_m3u8_variant_handle_failed",
                    url=url,
                    error_type=type(exc).__name__,
                )
                QMessageBox.critical(self, "添加失败", f"解析 M3U8 后添加下载任务失败：\n{exc}")
            finally:
                _cleanup_m3u8_thread()

        relay = _MainThreadListRelay(on_variants_ready, self)
        thread._ui_relay = relay
        thread.finished.connect(relay.handle_list, Qt.ConnectionType.QueuedConnection)
        thread.start()

    # ------------------------------------------------------------------
    # Task creation + manager hand-off
    # ------------------------------------------------------------------
    def _resource_context_kwargs(self, resource: M3U8Resource) -> dict:
        """Extract download-context kwargs from a M3U8Resource for _start_download."""
        return {
            "page_url": getattr(resource, "page_url", "") or "",
            "page_title": getattr(resource, "page_title", "") or "",
            "source": getattr(resource, "source", "unknown") or "unknown",
            "resource_type": getattr(resource, "resource_type", "") or "",
            "mime": getattr(resource, "mime", "") or "",
            "master_url": getattr(resource, "master_url", None),
            "media_url": getattr(resource, "media_url", None),
            "temporary_cookie_allowed": bool(getattr(resource, "temporary_cookie_allowed", False)),
            "cookie_policy_source": getattr(resource, "cookie_policy_source", "") or "",
            "auth_policy_source": getattr(resource, "auth_policy_source", "") or "",
            "playlist_diagnostics": dict(getattr(resource, "playlist_diagnostics", {}) or {}),
            "detected_at": getattr(resource, "detected_at", None),
            "expires_at": getattr(resource, "expires_at", None),
            "ephemeral_url": bool(getattr(resource, "ephemeral_url", False)),
            "ttl_warning": bool(getattr(resource, "ttl_warning", False)),
        }

    @staticmethod
    def _apply_playlist_diagnostics_to_resource(resource: M3U8Resource, diagnostics: dict | None) -> None:
        """Attach playlist diagnostics to a resource without exposing signed URL values in logs."""
        if not isinstance(diagnostics, dict) or not diagnostics:
            return
        resource.playlist_diagnostics = dict(diagnostics)
        try:
            resource.detected_at = float(diagnostics.get("detected_at") or resource.detected_at)
        except (TypeError, ValueError):
            pass
        try:
            expires_at = diagnostics.get("expires_at")
            resource.expires_at = float(expires_at) if expires_at is not None else None
        except (TypeError, ValueError):
            resource.expires_at = None
        resource.ephemeral_url = bool(diagnostics.get("ephemeral_url", False))
        resource.ttl_warning = bool(diagnostics.get("ttl_warning", False))

    @staticmethod
    def _playlist_diagnostics_from_url(url: str) -> dict:
        """Build URL-only diagnostics for signed/ephemeral URLs when no playlist was parsed."""
        try:
            diagnostics = analyze_playlist_diagnostics(url or "", "").to_dict()
        except Exception:
            return {}
        if not diagnostics.get("ephemeral_url"):
            return {}
        return diagnostics

    def _show_playlist_diagnostics_warning(self, diagnostics: dict | None, title: str) -> None:
        """Log one concise warning for DRM/cross-domain/signed-URL playlist risks."""
        features = config.get("features", {}) or {}
        if not bool(features.get("playlist_diagnostics_enabled", True)):
            return
        if not isinstance(diagnostics, dict) or not diagnostics:
            return

        warning_items = [str(item) for item in diagnostics.get("warnings", []) if item]
        risk_level = str(diagnostics.get("risk_level", "none") or "none")
        if risk_level == "none" and not warning_items:
            return

        key = "|".join(
            [
                str(diagnostics.get("playlist_url") or ""),
                risk_level,
                str(bool(diagnostics.get("is_drm"))),
                str(bool(diagnostics.get("key_cross_domain"))),
                str(bool(diagnostics.get("segment_cross_domain"))),
                str(bool(diagnostics.get("ttl_warning"))),
            ]
        )
        shown = getattr(self, "_playlist_diagnostics_warning_keys", None)
        if not isinstance(shown, set):
            shown = set()
            self._playlist_diagnostics_warning_keys = shown
        if key in shown:
            return
        shown.add(key)

        details: list[str] = []
        if diagnostics.get("is_drm"):
            systems = ", ".join(diagnostics.get("drm_systems") or []) or "未知 DRM"
            details.append(f"• DRM/许可证加密：{systems}")
        if diagnostics.get("key_cross_domain"):
            hosts = ", ".join((diagnostics.get("key_cross_domain_hosts") or [])[:3])
            details.append(f"• Key 跨域：{hosts}")
        if diagnostics.get("segment_cross_domain"):
            hosts = ", ".join((diagnostics.get("segment_cross_domain_hosts") or [])[:3])
            details.append(f"• 分片/CDN 跨域：{hosts}")
        if diagnostics.get("variant_cross_domain"):
            hosts = ", ".join((diagnostics.get("variant_cross_domain_hosts") or [])[:3])
            details.append(f"• 子播放列表跨域：{hosts}")
        if diagnostics.get("ephemeral_url"):
            expires_in = diagnostics.get("expires_in_seconds")
            if isinstance(expires_in, (int, float)):
                if expires_in <= 0:
                    details.append("• 签名 URL：已过期，建议重新嗅探")
                else:
                    details.append(f"• 签名 URL：约 {max(1, int(expires_in // 60))} 分钟后过期")
            else:
                details.append("• 签名 URL：检测到临时 token/signature 参数")
        existing_normalized = {item[2:] if item.startswith("• ") else item for item in details}
        for item in warning_items:
            if item not in existing_normalized:
                details.append(f"• {item}")

        message = (
            f"播放列表风险提示 | 任务：{title}\n"
            "已检测到可能影响下载成功率的播放列表特征：\n"
            + "\n".join(details[:8])
            + "\n程序会继续加入下载队列；如因过期或 DRM 失败，请重新嗅探后再试。"
        )
        logger.warning(
            message,
            event="ui_playlist_diagnostics_warning_logged",
            risk_level=risk_level,
            title=title,
        )

    @staticmethod
    def _headers_have_cookie(headers: dict | None) -> bool:
        """Return True when the request header bag currently carries Cookie."""
        if not isinstance(headers, dict):
            return False
        return any(str(name).lower() == "cookie" and bool(value) for name, value in headers.items())

    def _resolve_temporary_cookie_authorization(
        self,
        *,
        headers: dict,
        temporary_cookie_allowed: bool,
        cookie_policy_source: str,
        title: str,
        page_url: str,
    ) -> tuple[bool, str]:
        """Prompt once for task-local Cookie forwarding when the feature is enabled."""
        features = config.get("features", {}) or {}
        if not bool(features.get("temporary_cookie_forwarding_enabled", False)):
            return temporary_cookie_allowed, cookie_policy_source
        if temporary_cookie_allowed or not self._headers_have_cookie(headers):
            return temporary_cookie_allowed, cookie_policy_source

        reply = QMessageBox.question(
            self,
            "Cookie 临时授权",
            (
                "检测到该资源携带 Cookie。\n\n"
                "是否仅本次下载使用当前站点 Cookie？\n"
                "Cookie 不会写入历史记录、站点规则或普通日志；下载阶段仍会限制为同站或明确匹配的规则。\n\n"
                f"任务：{title}\n"
                f"页面：{page_url or '未知'}"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            return True, cookie_policy_source or "ui_prompt"
        return False, cookie_policy_source or "ui_declined"

    @staticmethod
    def _signed_url_refresh_is_high_risk(
        diagnostics: dict | None,
        *,
        expires_at: float | None = None,
        ttl_warning: bool = False,
    ) -> bool:
        """Return whether a signed URL is stale enough to justify refresh/re-capture."""
        if isinstance(diagnostics, dict):
            if bool(diagnostics.get("ttl_warning")):
                return True
            candidate = diagnostics.get("expires_at", expires_at)
        else:
            candidate = expires_at
        try:
            expires_at_float = float(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            expires_at_float = None
        if expires_at_float is not None:
            return expires_at_float <= time.time() + TTL_WARNING_SECONDS
        return bool(ttl_warning)

    @staticmethod
    def _merge_refreshed_url_diagnostics(previous: dict | None, refreshed: dict | None) -> dict:
        """Preserve structural playlist diagnostics while replacing signed-URL timing."""
        if not isinstance(previous, dict) or not previous:
            return dict(refreshed or {}) if isinstance(refreshed, dict) else {}
        if not isinstance(refreshed, dict) or not refreshed:
            return dict(previous)

        merged = dict(previous)
        for key in (
            "playlist_url",
            "playlist_host",
            "detected_at",
            "signed_query_params",
            "ephemeral_url",
            "expires_at",
            "expires_in_seconds",
            "ttl_warning",
        ):
            if key in refreshed:
                merged[key] = refreshed[key]

        structural_warnings = [
            str(item)
            for item in (previous.get("warnings") or [])
            if item and "签名 URL" not in str(item) and "过期" not in str(item)
        ]
        for item in refreshed.get("warnings") or []:
            if item and item not in structural_warnings:
                structural_warnings.append(str(item))
        merged["warnings"] = structural_warnings

        if merged.get("is_drm"):
            merged["risk_level"] = "error"
        elif (
            merged.get("key_cross_domain")
            or merged.get("segment_cross_domain")
            or merged.get("variant_cross_domain")
            or merged.get("ttl_warning")
        ):
            merged["risk_level"] = "warning"
        else:
            merged["risk_level"] = refreshed.get("risk_level", previous.get("risk_level", "none"))
        return merged

    def _maybe_refresh_ephemeral_download_context(
        self,
        *,
        url: str,
        headers: dict,
        page_url: str,
        page_title: str,
        source: str,
        resource_type: str,
        mime: str,
        master_url,
        media_url,
        selected_variant,
        title_source,
        playlist_diagnostics: dict,
        detected_at,
        expires_at,
        ephemeral_url: bool,
        ttl_warning: bool,
        title: str,
    ) -> dict:
        """Optionally refresh an internal-browser short-lived URL before task creation."""
        base = {
            "url": url,
            "headers": headers,
            "page_url": page_url,
            "page_title": page_title,
            "source": source,
            "resource_type": resource_type,
            "mime": mime,
            "master_url": master_url,
            "media_url": media_url,
            "selected_variant": selected_variant,
            "playlist_diagnostics": playlist_diagnostics,
            "detected_at": detected_at,
            "expires_at": expires_at,
            "ephemeral_url": ephemeral_url,
            "ttl_warning": ttl_warning,
        }

        features = config.get("features", {}) or {}
        if not bool(features.get("ephemeral_m3u8_refresh_enabled", False)):
            return base
        if not bool(ephemeral_url or (playlist_diagnostics or {}).get("ephemeral_url")):
            return base
        if (source or "").lower() != SOURCE_INTERNAL_BROWSER:
            return base
        if not self._signed_url_refresh_is_high_risk(
            playlist_diagnostics,
            expires_at=expires_at,
            ttl_warning=ttl_warning,
        ):
            return base

        browser = getattr(self, "browser", None)
        if browser is None or not hasattr(browser, "refresh_and_capture_current_page"):
            return base

        expires_in = (playlist_diagnostics or {}).get("expires_in_seconds")
        if isinstance(expires_in, (int, float)) and expires_in <= 0:
            risk_text = "当前签名 URL 已过期。"
        elif isinstance(expires_in, (int, float)):
            risk_text = f"当前签名 URL 约 {max(1, int(expires_in // 60))} 分钟后过期。"
        else:
            risk_text = "当前 URL 携带短期签名参数。"

        reply = QMessageBox.question(
            self,
            "刷新短期链接",
            (
                f"任务：{title}\n\n"
                f"{risk_text}\n"
                "是否刷新内置浏览器当前页面并等待新的媒体链接？\n\n"
                "成功后将自动用新 URL 和新 headers 创建下载任务；失败时仍可手动重新播放后再嗅探。"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return base

        result = browser.refresh_and_capture_current_page(
            page_url=page_url or "",
            previous_resource_url=url or "",
            timeout_ms=int(features.get("ephemeral_m3u8_refresh_timeout_ms", 15000) or 15000),
        )
        context = result.get("context") if isinstance(result, dict) else None
        if not (isinstance(context, dict) and result.get("ok") and context.get("url")):
            QMessageBox.warning(
                self,
                "刷新短期链接失败",
                "刷新页面后未捕获到新的媒体链接。请在内置浏览器中重新播放视频后再下载。",
            )
            return base

        old_url = url
        refreshed_url = str(context.get("url") or "")
        refreshed_headers = dict(context.get("headers") or headers or {})
        refreshed_source = context.get("source") or source
        refreshed_resource_type = context.get("resource_type") or resource_type
        refreshed_mime = context.get("mime") or mime
        refreshed_page_url = context.get("page_url") or page_url
        refreshed_page_title = context.get("page_title") or page_title
        refreshed_master_url = context.get("master_url") or master_url
        refreshed_media_url = context.get("media_url") or media_url

        if refreshed_resource_type == RESOURCE_TYPE_HLS:
            if not refreshed_master_url or refreshed_master_url == old_url:
                refreshed_master_url = refreshed_url
            if not refreshed_media_url or refreshed_media_url == old_url:
                refreshed_media_url = refreshed_url

        refreshed_selected_variant = selected_variant
        if isinstance(selected_variant, dict) and selected_variant.get("url") == old_url:
            refreshed_selected_variant = dict(selected_variant)
            refreshed_selected_variant["url"] = refreshed_url

        refreshed_diagnostics = self._merge_refreshed_url_diagnostics(
            playlist_diagnostics,
            self._playlist_diagnostics_from_url(refreshed_url),
        )
        refreshed_detected_at = refreshed_diagnostics.get("detected_at", time.time()) if refreshed_diagnostics else time.time()
        refreshed_expires_at = refreshed_diagnostics.get("expires_at") if refreshed_diagnostics else None
        refreshed_ephemeral = bool(refreshed_diagnostics.get("ephemeral_url", False)) if refreshed_diagnostics else False
        refreshed_ttl_warning = bool(refreshed_diagnostics.get("ttl_warning", False)) if refreshed_diagnostics else False

        if isinstance(title_source, M3U8Resource):
            title_source.url = refreshed_url
            title_source.headers = dict(refreshed_headers)
            title_source.page_url = refreshed_page_url
            title_source.page_title = refreshed_page_title
            title_source.source = refreshed_source
            title_source.resource_type = refreshed_resource_type
            title_source.mime = refreshed_mime
            title_source.master_url = refreshed_master_url
            title_source.media_url = refreshed_media_url
            self._apply_playlist_diagnostics_to_resource(title_source, refreshed_diagnostics)

        logger.info(
            "[PWR-REFRESH] 短期 URL 已刷新并替换下载上下文",
            event="ui_ephemeral_url_refreshed",
            old_url=old_url,
            new_url=refreshed_url,
            source=refreshed_source,
            resource_type=refreshed_resource_type,
            has_headers=bool(refreshed_headers),
        )

        return {
            "url": refreshed_url,
            "headers": refreshed_headers,
            "page_url": refreshed_page_url,
            "page_title": refreshed_page_title,
            "source": refreshed_source,
            "resource_type": refreshed_resource_type,
            "mime": refreshed_mime,
            "master_url": refreshed_master_url,
            "media_url": refreshed_media_url,
            "selected_variant": refreshed_selected_variant,
            "playlist_diagnostics": refreshed_diagnostics,
            "detected_at": refreshed_detected_at,
            "expires_at": refreshed_expires_at,
            "ephemeral_url": refreshed_ephemeral,
            "ttl_warning": refreshed_ttl_warning,
        }

    def _start_download(
        self,
        url: str,
        title_source,
        headers: dict,
        user_engine=None,
        title_suffix: str = "",
        selected_variant=None,
        save_dir=None,
        master_url=None,
        media_url=None,
        page_url: str = "",
        page_title: str = "",
        source: str = "unknown",
        resource_type: str = "",
        mime: str = "",
        temporary_cookie_allowed: bool = False,
        cookie_policy_source: str = "",
        auth_policy_source: str = "",
        playlist_diagnostics: dict | None = None,
        detected_at: float | None = None,
        expires_at: float | None = None,
        ephemeral_url: bool = False,
        ttl_warning: bool = False,
    ):
        """开始下载任务"""
        title = self._compose_download_title(title_source, title_suffix)
        if isinstance(title_source, M3U8Resource):
            context_defaults = self._resource_context_kwargs(title_source)
            page_url = page_url or context_defaults["page_url"]
            page_title = page_title or context_defaults["page_title"]
            if not source or source == "unknown":
                source = context_defaults["source"]
            resource_type = resource_type or context_defaults["resource_type"]
            mime = mime or context_defaults["mime"]
            master_url = master_url or context_defaults["master_url"]
            media_url = media_url or context_defaults["media_url"]
            temporary_cookie_allowed = bool(
                temporary_cookie_allowed or context_defaults["temporary_cookie_allowed"]
            )
            cookie_policy_source = cookie_policy_source or context_defaults["cookie_policy_source"]
            auth_policy_source = auth_policy_source or context_defaults["auth_policy_source"]
            if not playlist_diagnostics:
                playlist_diagnostics = context_defaults["playlist_diagnostics"]
            detected_at = detected_at if detected_at is not None else context_defaults["detected_at"]
            expires_at = expires_at if expires_at is not None else context_defaults["expires_at"]
            ephemeral_url = bool(ephemeral_url or context_defaults["ephemeral_url"])
            ttl_warning = bool(ttl_warning or context_defaults["ttl_warning"])

        if not isinstance(playlist_diagnostics, dict):
            playlist_diagnostics = {}
        if not playlist_diagnostics:
            playlist_diagnostics = self._playlist_diagnostics_from_url(url)
        if playlist_diagnostics:
            detected_at = detected_at if detected_at is not None else playlist_diagnostics.get("detected_at")
            expires_at = expires_at if expires_at is not None else playlist_diagnostics.get("expires_at")
            ephemeral_url = bool(ephemeral_url or playlist_diagnostics.get("ephemeral_url", False))
            ttl_warning = bool(ttl_warning or playlist_diagnostics.get("ttl_warning", False))

        try:
            refreshed_context = self._maybe_refresh_ephemeral_download_context(
                url=url,
                headers=headers,
                page_url=page_url,
                page_title=page_title,
                source=source,
                resource_type=resource_type,
                mime=mime,
                master_url=master_url,
                media_url=media_url,
                selected_variant=selected_variant,
                title_source=title_source,
                playlist_diagnostics=playlist_diagnostics,
                detected_at=detected_at,
                expires_at=expires_at,
                ephemeral_url=ephemeral_url,
                ttl_warning=ttl_warning,
                title=title,
            )
            url = refreshed_context["url"]
            headers = refreshed_context["headers"]
            page_url = refreshed_context["page_url"]
            page_title = refreshed_context["page_title"]
            source = refreshed_context["source"]
            resource_type = refreshed_context["resource_type"]
            mime = refreshed_context["mime"]
            master_url = refreshed_context["master_url"]
            media_url = refreshed_context["media_url"]
            selected_variant = refreshed_context["selected_variant"]
            playlist_diagnostics = refreshed_context["playlist_diagnostics"]
            detected_at = refreshed_context["detected_at"]
            expires_at = refreshed_context["expires_at"]
            ephemeral_url = refreshed_context["ephemeral_url"]
            ttl_warning = refreshed_context["ttl_warning"]

            temporary_cookie_allowed, cookie_policy_source = self._resolve_temporary_cookie_authorization(
                headers=headers,
                temporary_cookie_allowed=temporary_cookie_allowed,
                cookie_policy_source=cookie_policy_source,
                title=title,
                page_url=page_url,
            )
            self._show_playlist_diagnostics_warning(playlist_diagnostics, title)

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
                temporary_cookie_allowed=temporary_cookie_allowed,
                cookie_policy_source=cookie_policy_source or "none",
            )

            # 若本地已存在同名文件，提示是否覆盖
            from pathlib import Path

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

            task_kwargs = {
                "url": url,
                "save_dir": save_dir or config.get("download_dir"),
                "filename": title,
                "headers": headers,
                "page_url": page_url,
                "page_title": page_title,
                "source": source,
                "resource_type": resource_type,
                "mime": mime,
                "temporary_cookie_allowed": temporary_cookie_allowed,
                "cookie_policy_source": cookie_policy_source,
                "auth_policy_source": auth_policy_source,
                "playlist_diagnostics": dict(playlist_diagnostics or {}),
                "ephemeral_url": bool(ephemeral_url),
                "ttl_warning": bool(ttl_warning),
            }
            try:
                if detected_at is not None:
                    task_kwargs["detected_at"] = float(detected_at)
            except (TypeError, ValueError):
                pass
            try:
                if expires_at is not None:
                    task_kwargs["expires_at"] = float(expires_at)
            except (TypeError, ValueError):
                pass
            task = DownloadTask(**task_kwargs)

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
            # Audit-finding High #4: consume the AddResult that
            # DownloadManager.add_task returns. Previously the UI
            # logged "已添加下载任务" unconditionally even when the
            # task was merged, rejected for disk space, or blocked —
            # creating a silent divergence between what the user saw
            # and what actually reached the queue.
            result = self.download_manager.add_task(task, user_engine)
            status = getattr(result, "status", "queued")
            reason = getattr(result, "reason", None) or ""

            if status == "queued":
                logger.info(
                    f"已添加下载任务: {task.filename}",
                    event="ui_start_download_queued",
                    title=title,
                )
            elif status == "merged":
                logger.info(
                    f"任务已存在,已合并到现有任务: {task.filename} ({reason})",
                    event="ui_start_download_merged",
                    title=title,
                    reason=reason,
                )
                self.statusBar().showMessage(
                    f"任务已在队列中,未重复添加: {task.filename}",
                    5000,
                )
            elif status == "needs_confirmation":
                # Offer the user an explicit choice: bypass the disk
                # precheck or abort the add. Matches the ``AddResult``
                # contract in ``core/download/manager.py`` which expects
                # the caller to call ``add_task(..., bypass_disk_check=True)``
                # on approval.
                reply = QMessageBox.warning(
                    self,
                    "磁盘空间预检",
                    (
                        f"目标磁盘可用空间可能不足以保存该任务\n"
                        f"({task.filename}).\n\n"
                        f"原因: {reason}\n\n"
                        "点击「确定」仍然加入下载队列(可能因磁盘满而失败);\n"
                        "点击「取消」放弃这次添加."
                    ),
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if reply == QMessageBox.StandardButton.Ok:
                    self.download_manager.add_task(
                        task, user_engine, bypass_disk_check=True
                    )
                    logger.warning(
                        f"磁盘预检被用户绕过,任务仍加入队列: {task.filename}",
                        event="ui_start_download_disk_bypass",
                        title=title,
                    )
                else:
                    logger.info(
                        f"用户因磁盘预检放弃添加任务: {task.filename}",
                        event="ui_start_download_disk_cancelled",
                        title=title,
                    )
                    return
            else:
                # "failed" or any future status: surface it so the user
                # never sees a stale success message.
                logger.warning(
                    f"任务入队返回异常状态: {status} ({reason})",
                    event="ui_start_download_unexpected_status",
                    status=status,
                    reason=reason,
                    title=title,
                )
                QMessageBox.warning(
                    self,
                    "添加失败",
                    f"任务未能加入下载队列: {task.filename}\n状态: {status}\n原因: {reason}",
                )
        except Exception as e:
            logger.error(
                f"[QUEUE] 创建或入队下载任务失败: {title} - {e}",
                event="ui_start_download_failed",
                title=title,
                url=url,
            )
            QMessageBox.critical(self, "添加失败", f"添加到下载队列失败：\n{e}")

    # ------------------------------------------------------------------
    # CatCatch browser-extension bridge
    # ------------------------------------------------------------------
    def _on_catcatch_download(self, url: str, headers: dict, filename: str):
        """处理来自猫爪插件的下载请求

        Audit-finding High #2: route CatCatch-delivered URLs through
        ``M3U8Sniffer.add_resource`` instead of hand-constructing a
        ``M3U8Resource``. The sniffer applies the same SSRF filter,
        header normalization, site-rule augmentation, and dedup logic
        that the Playwright sniffing pipeline already uses, so there is
        exactly one trust boundary for "URL that landed in the resource
        list" regardless of whether it came from the browser or the
        extension. A SSRF rejection here is surfaced as a status-bar
        notice; the resource is simply not added.
        """

        logger.info(
            "[CatCatch] 收到下载请求",
            event="catcatch_ui_received",
            title=filename or "",
        )

        # Prefer Referer as page_url to keep site-rule matching and
        # title / origin derivation accurate to the real browsing context.
        headers = headers or {}
        page_url = (
            headers.get("referer")
            or headers.get("Referer")
            or url
        )
        page_title = filename or "CatCatch Download"

        context = build_engine_select_context(
            url=url,
            headers=headers,
            page_url=page_url,
            page_title=page_title,
            source=SOURCE_CATCATCH,
        )

        resource = self.sniffer.add_resource(
            context.url,
            dict(context.headers or {}),
            context.page_url,
            context.page_title,
            source=context.source,
            resource_type=context.resource_type,
            mime=context.mime,
            master_url=context.master_url,
            media_url=context.media_url,
        )
        if resource is None:
            # Either the URL was SSRF-blocked or dedup collapsed it into
            # an existing entry. Tell the user something happened so the
            # click never feels silently ignored.
            self.statusBar().showMessage(
                "猫爪请求已过滤 (重复或被 SSRF 防线拦截)，请查看运行日志",
                5000,
            )
            return

        # Honour the user's pre-selected engine just like the pre-existing
        # implementation did; EngineSelector.select falls back to auto
        # when the preference cannot serve the URL.
        from core.engine_selector import EngineSelector

        selector = EngineSelector(self.engines)
        user_engine = self.get_selected_engine()
        resource.selected_engine = user_engine

        selector_ctx = context_from_resource(resource)
        _, engine_name = selector.select(resource.url, user_engine, context=selector_ctx)
        self.resource_panel.add_resource(resource, engine_name)

        # 切换到资源标签页
        self.main_tabs.setCurrentIndex(1)

        # 显示通知
        self.statusBar().showMessage(
            f"收到猫爪下载请求: {filename or url[:50]}...", 5000
        )
