"""Action/menu-slot mixin for :class:`MainWindow`.

security-stability-hardening Task 25.2 / Requirements 26.2, 26.5.

This module hosts the batch-operation / menu / toolbar slots that used
to live inline in ``ui/main_window.py``. Splitting them keeps the main
window file under the ≤700-line budget mandated by R26 while leaving
behavioural surface untouched (covered by Stage 3 regressions).

The mixin intentionally does NOT own:

* Security-critical entry points — ``_run_quick_manual_script``,
  ``_emit_security_alert`` and the ``ALLOWED_QUICK_SCRIPTS`` whitelist
  stay in ``ui/main_window.py`` per R5 AC-5.
* The sniffer coordination chain — see
  :mod:`ui.main_window_sniff_flow`.
* Task-update plumbing and Qt thread-hop signals — those live on
  :class:`MainWindow` directly because they touch the snapshot channel
  contract (R11.7 / R29).

The split is structural: every method below was relocated verbatim
from ``ui/main_window.py``; any behavioural change here would be a
regression and must go through its own spec task.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import html
import urllib.parse

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.task_model import M3U8Resource
from ui.browser_view import BrowserView
from ui.download_queue import DownloadQueuePanel
from ui.resource_panel import ResourcePanel
from utils.config_manager import config
from utils.i18n import i18n, TR
from utils.logger import logger

if TYPE_CHECKING:  # pragma: no cover - type-checker only
    from PyQt6.QtCore import QUrl  # noqa: F401


class MainWindowActionsMixin:
    """Batch-operation / menu / config-slot surface for :class:`MainWindow`."""

    # ------------------------------------------------------------------
    # UI skeleton construction (relocated from MainWindow._init_ui to keep
    # ui/main_window.py under the ≤700-line budget mandated by R26.2 /
    # R26.5; behaviour is preserved bit-for-bit).
    # ------------------------------------------------------------------
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
        from PyQt6.QtWidgets import QSpinBox, QGroupBox

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

        # security-stability-hardening Task 28.2: explicit "Clear Temp Files"
        # entry in the Download Center. add_task no longer wipes the shared
        # temp dir on its own (so paused-task fragments survive); users must
        # opt in here when they want to reclaim disk space.
        self.clear_temp_btn = QPushButton("")
        self.clear_temp_btn.setObjectName("secondary_button")
        self.clear_temp_btn.clicked.connect(self._on_clear_temp_clicked)

        path_row.addWidget(path_label)
        path_row.addWidget(self.path_display, stretch=1)
        path_row.addWidget(self.select_path_btn)
        path_row.addWidget(self.open_folder_btn)
        path_row.addWidget(self.clear_temp_btn)

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

        # 标签栏右侧入口：组件管理 + 语言切换 + 使用手册（保持原有角落布局）
        self.current_language = "zh"

        manual_container = QWidget()
        manual_layout = QHBoxLayout(manual_container)
        manual_layout.setContentsMargins(0, 4, 12, 0)  # 移除底部边距，增加顶部微调
        manual_layout.setSpacing(8)

        self.component_manager_btn = QPushButton("")
        self.component_manager_btn.setObjectName("component_manager_button")
        self.component_manager_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.component_manager_btn.clicked.connect(self._show_component_manager_dialog)

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
            self.component_manager_btn,
            0,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
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

    # ------------------------------------------------------------------
    # Browser navigation slots
    # ------------------------------------------------------------------
    def _on_back(self):
        """后退"""
        self.browser.back()

    def _on_forward(self):
        """前进"""
        self.browser.forward()

    def _on_refresh(self):
        """刷新"""
        self.browser.reload()

    def _on_url_changed(self, url):
        """URL 变化时更新地址栏"""
        url_str = url.toString()
        if url_str == "about:blank":
            url_str = ""
        self.address_bar.setText(url_str)
        self.address_bar.setCursorPosition(0)

    def _on_address_entered(self):
        """地址栏回车"""
        url = self.address_bar.text().strip()
        if not url:
            return

        # 拦截磁力链接
        if url.startswith("magnet:?"):
            logger.info(f"检测到磁力链接: {url}")
            from datetime import datetime

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
                page_url="",  # 无来源页面
                title=name,
                timestamp=datetime.now()
            )
            # 添加到资源列表，强制指定 Aria2 引擎
            self.resource_panel.add_resource(resource, engine_name='Aria2')

            # 提示用户 (改为状态栏消息，不弹窗)
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

    # ------------------------------------------------------------------
    # History-panel slots
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Batch import (resource list)
    # ------------------------------------------------------------------
    def _on_batch_import_requested(self, urls: list):
        """处理批量导入 URL"""
        from core.engine_selector import EngineSelector
        from datetime import datetime

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

    # ------------------------------------------------------------------
    # Download preferences (path / spinboxes / clear temp)
    # ------------------------------------------------------------------
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

    def _on_clear_temp_clicked(self):
        """Handler for the "清空临时文件 / Clear Temp Files" toolbar button.

        security-stability-hardening Task 28.2 / Requirements 34.3, 34.4.

        Shows a confirmation dialog warning the user that this may discard
        already-downloaded fragments of paused/running tasks. On confirm,
        delegates to :func:`utils.cache_cleaner.clean_temp_cache`, passing
        the filenames of any currently running/paused tasks as a skip list
        so their fragment scratch dirs survive. Outcome is surfaced via a
        transient status-bar message and the log.
        """

        from utils.cache_cleaner import clean_temp_cache

        reply = QMessageBox.question(
            self,
            TR("clear_temp_confirm_title"),
            TR("clear_temp_confirm_body"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Collect filenames of tasks we must not wipe out — running,
        # paused, and queued all keep partial fragments worth preserving.
        active_filenames: list[str] = []
        dm = getattr(self, "download_manager", None)
        if dm is not None:
            try:
                with dm._lock:  # type: ignore[attr-defined]
                    buckets = (
                        getattr(dm, "active_tasks", []),
                        getattr(dm, "paused_tasks", []),
                    )
                    for bucket in buckets:
                        for t in bucket:
                            name = getattr(t, "filename", "")
                            if name:
                                active_filenames.append(name)
                # Queued tasks live in the Queue; take a best-effort snapshot.
                try:
                    queued = dm._snapshot_queued_tasks()  # type: ignore[attr-defined]
                    for t in queued:
                        name = getattr(t, "filename", "")
                        if name:
                            active_filenames.append(name)
                except Exception as snap_err:  # pragma: no cover - defensive
                    logger.debug(f"[CLEAR-TEMP] snapshot queued failed: {snap_err}")
            except Exception as dm_err:  # pragma: no cover - defensive
                logger.warning(f"[CLEAR-TEMP] gather active filenames failed: {dm_err}")

        logger.info(
            "[CLEAR-TEMP] 用户触发清空临时文件",
            event="clear_temp_triggered",
            skip_count=len(active_filenames),
        )

        try:
            result = clean_temp_cache(skip_filenames=active_filenames)
        except Exception as e:
            logger.error(f"[CLEAR-TEMP] 清理失败: {e}", event="clear_temp_failed")
            try:
                self.statusBar().showMessage(
                    TR("clear_temp_status_error", count=1), 5000,
                )
            except (RuntimeError, AttributeError):
                pass
            return

        if not result.existed:
            logger.info(
                "[CLEAR-TEMP] 临时目录为空或未配置",
                event="clear_temp_nothing",
                temp_dir=result.temp_dir,
            )
            try:
                self.statusBar().showMessage(TR("clear_temp_status_empty"), 5000)
            except (RuntimeError, AttributeError):
                pass
            return

        logger.info(
            "[CLEAR-TEMP] 清理完成",
            event="clear_temp_done",
            temp_dir=result.temp_dir,
            files_removed=result.files_removed,
            bytes_removed=result.bytes_removed,
            skipped=len(result.skipped),
            errors=len(result.errors),
        )
        for err in result.errors:
            logger.warning(f"[CLEAR-TEMP] 清理异常: {err}", event="clear_temp_error")

        status_msg = TR(
            "clear_temp_status_ok",
            count=result.files_removed,
            size=result.human_size(),
            skipped=len(result.skipped),
        )
        if result.errors:
            status_msg = status_msg + " | " + TR(
                "clear_temp_status_error", count=len(result.errors),
            )
        try:
            self.statusBar().showMessage(status_msg, 5000)
        except (RuntimeError, AttributeError):
            # Status bar unavailable during teardown; log-only fallback.
            logger.debug("main_window: statusBar unavailable for clear-temp hint")

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

    # ------------------------------------------------------------------
    # Quick Manual dialog (menu slot)
    # ------------------------------------------------------------------
    def _show_manual_dialog(self):
        """显示使用手册 (从外部 Markdown 加载)"""

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
                # ``_run_quick_manual_script`` stays on MainWindow to keep
                # the security-hardened entry point co-located with
                # ``ALLOWED_QUICK_SCRIPTS`` (R5 AC-5).
                self._run_quick_manual_script(script_name)

    # ------------------------------------------------------------------
    # Component manager entry / startup-check slots
    # ------------------------------------------------------------------
    def _show_component_manager_dialog(self):
        """打开组件管理对话框。"""
        from ui.component_manager_dialog import ComponentManagerDialog

        dialog = ComponentManagerDialog(self)
        dialog.component_status_summary_changed.connect(self.apply_component_entry_summary)
        self.apply_component_entry_summary(dialog.current_entry_summary())
        dialog.exec()

    def _run_component_startup_readonly_check(self):
        """Run only read-only startup checks; never install or update components automatically."""
        worker = getattr(self, "component_startup_worker", None)
        if worker is None or worker.is_running():
            return False
        logger.info(TR("component_startup_check_started"))
        started = worker.check_updates(force=False)
        if not started:
            logger.info(TR("component_startup_check_skipped"))
        return started

    @pyqtSlot(str)
    def _on_component_startup_operation_started(self, operation: str):
        if operation == "check_updates":
            self._component_startup_check_running = True
            self.component_manager_btn.setToolTip(TR("component_entry_tooltip_checking"))

    @pyqtSlot(str)
    def _on_component_startup_operation_finished(self, operation: str):
        if operation == "check_updates":
            self._component_startup_check_running = False
            self._update_component_manager_entry_text()
            logger.info(TR("component_startup_check_finished"))

    @pyqtSlot(list)
    def _on_component_startup_updates_checked(self, statuses: list):
        self.apply_component_entry_statuses(statuses)

    @pyqtSlot(str, str)
    def _on_component_startup_failed(self, operation: str, detail: str):
        if operation != "check_updates":
            return
        self._component_startup_check_running = False
        error = detail.splitlines()[0] if detail else ""
        self.component_manager_btn.setToolTip(TR("component_entry_tooltip_failed", error=error))
        logger.warning(TR("component_startup_check_failed", error=error))

    def apply_component_entry_statuses(self, statuses: list):
        """Update the component manager entry badge/tooltip from read-only status results."""
        from ui.main_window import summarize_component_entry_statuses

        counts = summarize_component_entry_statuses(statuses)
        return self.apply_component_entry_summary(counts)

    def apply_component_entry_summary(self, counts: dict):
        """Update the component manager entry badge/tooltip from precomputed summary counts."""
        normalized_counts = {
            "updates": int(counts.get("updates", 0)),
            "missing": int(counts.get("missing", 0)),
            "failed": int(counts.get("failed", 0)),
            "total": int(counts.get("total", 0)),
        }
        self._component_update_badge_counts = normalized_counts
        self._update_component_manager_entry_text()
        return normalized_counts

    def _update_component_manager_entry_text(self):
        counts = getattr(self, "_component_update_badge_counts", {"updates": 0, "missing": 0, "failed": 0, "total": 0})
        updates = int(counts.get("updates", 0))
        missing = int(counts.get("missing", 0))
        failed = int(counts.get("failed", 0))
        if updates or missing:
            badge = TR("component_entry_badge", updates=updates, missing=missing)
            self.component_manager_btn.setText(f"{TR('component_manager_entry')} {badge}")
            self.component_manager_btn.setObjectName("component_manager_button_badged")
        else:
            self.component_manager_btn.setText(TR("component_manager_entry"))
            self.component_manager_btn.setObjectName("component_manager_button")
        self.component_manager_btn.setToolTip(
            TR("component_entry_tooltip", updates=updates, missing=missing, failed=failed)
        )
        self.component_manager_btn.style().unpolish(self.component_manager_btn)
        self.component_manager_btn.style().polish(self.component_manager_btn)

    # ------------------------------------------------------------------
    # Language / window-chrome retranslation
    # ------------------------------------------------------------------
    def retranslate_ui(self):
        """刷新界面文字"""
        from ui.main_window import build_main_window_title

        self.setWindowTitle(build_main_window_title())

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
        self.clear_temp_btn.setText(TR("btn_clear_temp"))

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
        self._update_component_manager_entry_text()
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
