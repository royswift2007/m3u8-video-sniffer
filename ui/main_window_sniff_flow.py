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
import urllib.parse

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

from core.m3u8_parser import M3U8FetchThread
from core.task_model import DownloadTask, M3U8Resource
from utils.config_manager import config
from utils.i18n import TR
from utils.logger import logger
from utils.win_path import sanitize_title

if TYPE_CHECKING:  # pragma: no cover - type-checker only
    pass


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
        _, engine_name = selector.predict(resource.url, user_engine)

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

            # M3U8 资源：尝试解析多码率
            is_m3u8 = '.m3u8' in download_url.lower()
            if is_m3u8:
                # 复用资源面板已缓存的 variants（避免重复网络请求）
                cached_variants = resource.variants if hasattr(resource, 'variants') else []
                self._show_m3u8_variant_dialog(download_url, resource, headers, user_engine, cached_variants)
                return

            # 其他资源，直接下载
            self._start_download(download_url, resource, headers, user_engine)
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

        self._format_thread.finished.connect(on_formats_ready)
        self._format_thread.start()

        logger.info("正在获取视频格式...")

    def _show_m3u8_variant_dialog(self, url: str, title_source, headers: dict, user_engine=None, cached_variants=None):
        """显示 M3U8 清晰度选择对话框"""
        from ui.format_dialog import FormatSelectionDialog

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
                        _, effective_engine = selector.select(url, None)

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
                        _, effective_engine = selector.select(url, None)

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

    # ------------------------------------------------------------------
    # Task creation + manager hand-off
    # ------------------------------------------------------------------
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
    ):
        """开始下载任务"""
        title = self._compose_download_title(title_source, title_suffix)
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

        page_url = url  # CatCatch does not carry a separate page URL
        page_title = filename or "CatCatch Download"
        resource = self.sniffer.add_resource(url, headers or {}, page_url, page_title)
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
        _, engine_name = selector.select(url, user_engine)
        self.resource_panel.add_resource(resource, engine_name)

        # 切换到资源标签页
        self.main_tabs.setCurrentIndex(1)

        # 显示通知
        self.statusBar().showMessage(
            f"收到猫爪下载请求: {filename or url[:50]}...", 5000
        )
