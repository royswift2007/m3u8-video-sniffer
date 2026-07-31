"""
Main application window integrating all components.

security-stability-hardening Task 25.2 / Requirements 26.2, 26.5:
Action/menu slots live in :mod:`ui.main_window_actions` and sniffer
coordination lives in :mod:`ui.main_window_sniff_flow`. This module
owns only the window skeleton, init chain, task-update plumbing, and
the security-critical ``_run_quick_manual_script`` entry point (R5).
"""

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMainWindow, QMessageBox

from core.app_paths import get_app_root
from core.catcatch_server import CatCatchServer
from core.download_manager import DownloadManager
from core.m3u8_sniffer import M3U8Sniffer
from core.task_model import DownloadTask, TaskSnapshot
from engines.aria2_engine import Aria2Engine
from engines.ffmpeg_processor import FFmpegProcessor
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine
from engines.streamlink_engine import StreamlinkEngine
from engines.ytdlp_engine import YtdlpEngine
from ui.component_update_worker import ComponentUpdateWorker
from ui.main_window_actions import MainWindowActionsMixin
from ui.main_window_sniff_flow import MainWindowSniffFlowMixin
from ui.main_window_postprocess import MainWindowPostprocessMixin
from utils.config_manager import config
from utils.i18n import i18n, TR
from utils.logger import logger
from utils.redact import redact_url

APP_VERSION = "0.5.0"


# ---------------------------------------------------------------------------
# security-stability-hardening R5 — `script:` protocol whitelist
# ---------------------------------------------------------------------------
# Read-only, code-level constant listing the only quick-manual scripts that
# ``MainWindow._run_quick_manual_script`` is allowed to spawn. Must NEVER be
# loaded from user-writable configuration (per R5 AC-5). Keep in sync with
# the ``quick_scripts`` list rendered inside the Quick Manual dialog.
ALLOWED_QUICK_SCRIPTS: frozenset = frozenset(
    {
        "download_tools.bat",
        "install_extensions.bat",
        "register_protocol.bat",
        "uninstall_protocol.bat",
        "clean_cache.bat",
    }
)

# URL-ish prefixes that must never reach the script spawner. Matched
# case-insensitively against the RAW ``script_name`` input before any
# path normalization. ``file://`` is explicitly called out by R5 AC-4.
_FORBIDDEN_SCRIPT_SCHEMES: tuple = (
    "file://",
    "http://",
    "https://",
    "ftp://",
    "ftps://",
    "javascript:",
    "data:",
    "vbscript:",
    "script:",
)


def build_main_window_title() -> str:
    return f"M3U8D v{APP_VERSION}"


class MainWindow(MainWindowActionsMixin, MainWindowSniffFlowMixin, MainWindowPostprocessMixin, QMainWindow):
    """主应用窗口"""

    # ---------------------------------------------------------------
    # security-stability-hardening R11.7 / R29 — task update channels
    # ---------------------------------------------------------------
    #
    # ``_task_raw_thread_hop`` is the legacy Qt signal used purely as a
    # thread-hop primitive: workers in ``core/download_manager.py`` call
    # ``self._on_task_update`` (a non-Qt callback) which ``emit()``s the
    # raw ``DownloadTask`` so the UI slot runs on the main thread. Queue
    # rendering now uses immutable ``TaskSnapshot`` values; the raw task
    # channel remains for control handles and terminal history records.
    #
    # ``_task_snapshot_thread_hop`` is the new R11.7 / R29 channel: the
    # manager builds an immutable :class:`TaskSnapshot` under
    # ``task.lock`` and the signal delivers it to
    # :meth:`task_update_received` on the main thread. The slot below
    # is strictly read-only and MUST NOT call any mutating method on
    # the snapshot.
    _task_raw_thread_hop = pyqtSignal(object)
    _task_snapshot_thread_hop = pyqtSignal(object)
    _postprocess_finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(build_main_window_title())
        self.setGeometry(100, 100, 1400, 900)

        # 应用现代样式
        from ui.styles import MODERN_STYLE
        self.setStyleSheet(MODERN_STYLE)

        # 设置应用图标
        icon_path = str(Path(__file__).parent.parent / "resources" / "mvs.ico")
        if Path(icon_path).exists():
            self.setWindowIcon(QIcon(icon_path))

        # 初始化系统托盘通知服务（在主线程创建 QSystemTrayIcon）
        from utils.notification_service import NotificationService
        NotificationService.instance().initialize(self)

        # 初始化语言环境
        i18n.set_language(config.get("language", "zh"))
        i18n.language_changed.connect(self.retranslate_ui)

        # 初始化组件
        self._init_engines()
        self._init_core_components()
        self._init_ui()
        self.retranslate_ui()  # 触发首次渲染
        self._connect_signals()
        # Legacy raw-task thread hop: keeps raw ``DownloadTask`` control
        # handles and terminal history writes on the main thread. Queue
        # display itself is driven by ``TaskSnapshot`` below.
        self._task_raw_thread_hop.connect(
            self._handle_task_update_on_main_thread,
            Qt.ConnectionType.QueuedConnection,
        )
        # R11.7 / R29 snapshot channel: delivers an immutable
        # ``TaskSnapshot`` to the read-only ``task_update_received``
        # slot below. Use explicit queued connections for both task-update
        # signals so queue rendering never runs re-entrantly inside
        # DownloadManager.add_task or directly on a download worker thread.
        self._task_snapshot_thread_hop.connect(
            self.task_update_received,
            Qt.ConnectionType.QueuedConnection,
        )
        self._init_component_startup_check()

        logger.info(TR("log_ready"))

    # ------------------------------------------------------------------
    # Engines / core components / UI init
    # ------------------------------------------------------------------
    def _init_engines(self):
        """初始化下载引擎"""
        self.engines = []

        try:
            # N_m3u8DL-RE
            n_m3u8dl_path = config.get("engines.n_m3u8dl_re.path")
            if Path(n_m3u8dl_path).exists():
                n_m3u8dl_engine = N_m3u8DL_RE_Engine(n_m3u8dl_path)
                self.engines.append(n_m3u8dl_engine)
                self.n_m3u8dl_engine = n_m3u8dl_engine
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
        # Raw DownloadTask callback (legacy) — keeps control handles and
        # history persistence on the main thread; rendering uses snapshots.
        self.download_manager.on_task_update = self._on_task_update
        # R11.7 / R29 snapshot callback — read-only path into the UI.
        self.download_manager.on_task_snapshot = self._on_task_snapshot

        # 启动猫爪 HTTP 服务
        self.catcatch_server = CatCatchServer(port=config.get("catcatch.port", 9527))
        self.catcatch_server.download_requested.connect(self._on_catcatch_download)
        self.catcatch_server.start()

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
        self.download_queue.task_postprocess_requested.connect(self._on_postprocess_requested)
        self._postprocess_finished.connect(self._on_postprocess_finished)

        # 历史记录操作
        self.history_panel.record_download_requested.connect(self._on_history_download_requested)
        self.history_panel.record_deleted.connect(self._on_history_record_deleted)

    def _init_component_startup_check(self):
        """Schedule a delayed read-only component update check after the main window is ready."""
        self.component_startup_worker = ComponentUpdateWorker(parent=self)
        self._component_startup_check_running = False
        self._component_update_badge_counts = {"updates": 0, "missing": 0, "failed": 0, "total": 0}
        self.component_startup_worker.operation_started.connect(self._on_component_startup_operation_started)
        self.component_startup_worker.operation_finished.connect(self._on_component_startup_operation_finished)
        self.component_startup_worker.updates_checked.connect(self._on_component_startup_updates_checked)
        self.component_startup_worker.operation_failed.connect(self._on_component_startup_failed)
        self._update_component_manager_entry_text()
        QTimer.singleShot(2500, self._run_component_startup_readonly_check)

    # ------------------------------------------------------------------
    # Task-update plumbing (R11.7 / R29)
    # ------------------------------------------------------------------
    def _on_task_update(self, task: DownloadTask) -> None:
        """Legacy raw-task callback: forwarded via Qt signal to the UI thread.

        The queue panel keeps the raw :class:`DownloadTask` as a control
        handle, and this path still records terminal-state history on the
        UI thread. Display state is delivered separately as immutable
        snapshots by :meth:`_on_task_snapshot` below.
        """
        self._task_raw_thread_hop.emit(task)

    def _on_task_snapshot(self, snapshot: TaskSnapshot) -> None:
        """R11.7 / R29 snapshot callback: thread-hop to the main slot.

        Routes through :meth:`_emit_task_snapshot` so the only statically
        typed path into the snapshot signal accepts ``TaskSnapshot``
        exclusively. ``mypy --strict`` / ``pyright`` treat passing a raw
        :class:`DownloadTask` here as a type error (R29.2).
        """
        self._emit_task_snapshot(snapshot)

    def _emit_task_snapshot(self, snapshot: TaskSnapshot) -> None:
        """Typed alias for the snapshot thread-hop signal (R29.2).

        ``pyqtSignal(object)`` accepts any Python object, which means a
        bare ``self._task_snapshot_thread_hop.emit(task)`` with a raw
        :class:`DownloadTask` compiles and only fails at runtime inside
        :meth:`task_update_received`'s defensive ``isinstance`` check.
        Routing every emit through this thin wrapper gives static
        checkers a single choke point to enforce the ``TaskSnapshot``
        contract — callers that try to pass a :class:`DownloadTask`
        fail ``mypy --strict`` / ``pyright`` at authoring time, which
        is the lint behaviour required by Requirement 29 AC-2.
        """
        self._task_snapshot_thread_hop.emit(snapshot)

    @pyqtSlot(object)
    def _handle_task_update_on_main_thread(self, task: DownloadTask) -> None:
        """Main-thread bridge for the legacy raw-task channel.

        Responsible for (a) refreshing the queue panel's raw
        :class:`DownloadTask` control handle, and (b) recording history
        once the task reaches a terminal state. Queue display fields are
        refreshed by :meth:`task_update_received` from immutable
        ``TaskSnapshot`` values.
        """
        filename = getattr(task, "filename", "")
        status = getattr(task, "status", "")
        # R41 — stale late snapshots for a removed task must not
        # resurrect deleted QTreeWidgetItems; ``add_or_update_task``
        # already rejects ``status == "removed"``, but also avoid
        # the history recording code below touching a dead task.
        # The kill-window race means ``status`` can still be
        # ``"downloading"`` while ``stop_reason`` is already
        # ``"removed"``.  Check both.
        if status == "removed" or getattr(task, "stop_reason", "") == "removed":
            return
        logger.debug(
            "主线程任务更新开始",
            event="ui_task_update_start",
            task_id=id(task),
            filename=filename,
            status=status,
        )
        try:
            self.download_queue.add_or_update_task(task)
            logger.debug(
                "主线程下载队列控制句柄刷新完成",
                event="ui_task_update_handle_done",
                task_id=id(task),
                filename=filename,
                status=getattr(task, "status", status),
            )

            # 如果任务完成或失败，记录到历史
            current_status = getattr(task, "status", "")
            if current_status in ['completed', 'failed']:
                if getattr(task, "_history_recorded_status", None) == current_status:
                    logger.debug(
                        "主线程任务更新完成（历史已记录）",
                        event="ui_task_update_done",
                        task_id=id(task),
                        filename=filename,
                        status=current_status,
                    )
                    return
                size = task.downloaded_size if task.downloaded_size else 'N/A'
                self.history_panel.add_record(
                    filename=task.filename,
                    url=task.url,
                    status=current_status,
                    size=size,
                    headers=task.headers,
                    engine=task.engine,
                    save_dir=task.save_dir,
                    selected_variant=getattr(task, 'selected_variant', None),
                    master_url=getattr(task, 'master_url', None),
                    media_url=getattr(task, 'media_url', None)
                )
                setattr(task, "_history_recorded_status", current_status)
                logger.debug(
                    "主线程历史记录写入完成",
                    event="ui_task_update_history_done",
                    task_id=id(task),
                    filename=filename,
                    status=current_status,
                )

            logger.debug(
                "主线程任务更新完成",
                event="ui_task_update_done",
                task_id=id(task),
                filename=filename,
                status=getattr(task, "status", status),
            )
        except RuntimeError as exc:
            logger.warning(
                f"主线程任务更新跳过: {exc}",
                event="ui_task_update_runtime_error",
                task_id=id(task),
                filename=filename,
                status=getattr(task, "status", status),
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                f"主线程任务更新失败: {exc}",
                event="ui_task_update_failed",
                task_id=id(task),
                filename=filename,
                status=getattr(task, "status", status),
                error_type=type(exc).__name__,
            )

    @pyqtSlot(object)
    def task_update_received(self, snapshot: TaskSnapshot) -> None:
        """Read-only main-thread slot for :class:`TaskSnapshot` updates.

        Contract (see task 11.3 / design 2.3 / R11.7):

        * The incoming ``snapshot`` is an immutable
          :class:`core.task_model.TaskSnapshot`; callers MUST treat it
          as read-only and MUST NOT attempt to mutate any field.
        * This slot MUST NOT reach into the owning :class:`DownloadTask`
          (e.g. ``stop_requested`` / ``process``); those fields are
          owned by the worker thread and reading them from the UI
          thread would reintroduce the very race that the snapshot is
          designed to avoid.
        * Queue tree rendering is driven here from the immutable snapshot.
          The legacy raw-task path
          (:meth:`_handle_task_update_on_main_thread`) remains only for
          control handles and terminal-state history persistence.
        """
        # Defensive shape check: if a caller accidentally emits a raw
        # ``DownloadTask`` through this channel we'd rather log and
        # ignore than crash the UI thread. The Stage 4 lint rule
        # (R29 AC-2) will make this impossible at authoring time.
        if not isinstance(snapshot, TaskSnapshot):
            logger.warning(
                "task_update_received: expected TaskSnapshot, got %s",
                type(snapshot).__name__,
                event="ui_task_snapshot_type_mismatch",
            )
            return
        # 下载队列显示现在由不可变 TaskSnapshot 驱动；raw DownloadTask
        # 仅保留给控制按钮和历史记录，避免 queued signal 晚到时读取同一
        # 可变对象的最终状态并覆盖实时进度。
        try:
            self.download_queue.add_or_update_snapshot(snapshot)
            logger.debug(
                "主线程下载队列快照刷新完成",
                event="ui_task_snapshot_queue_done",
                task_id=snapshot.task_id,
                filename=snapshot.title,
                status=snapshot.status,
                progress=snapshot.progress,
            )
        except RuntimeError as exc:
            logger.warning(
                f"主线程下载队列快照刷新跳过: {exc}",
                event="ui_task_snapshot_queue_runtime_error",
                task_id=snapshot.task_id,
                filename=snapshot.title,
                status=snapshot.status,
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                f"主线程下载队列快照刷新失败: {exc}",
                event="ui_task_snapshot_queue_failed",
                task_id=snapshot.task_id,
                filename=snapshot.title,
                status=snapshot.status,
                error_type=type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # security-stability-hardening R5 — quick-manual script entry point
    # ------------------------------------------------------------------
    # NOTE: kept on ``MainWindow`` (not on the actions mixin) so the
    # entire R5 call site — whitelist constant, scheme list, spawn
    # logic, and security-alert emitter — stays in a single file and
    # is trivially auditable.
    def _run_quick_manual_script(self, script_name: str):
        """Spawn one whitelisted quick-manual script.

        Security hardening (R5):
            1. Coerce ``script_name`` via ``Path(...).name`` to strip any
               directory component (``..``, ``/``, ``\\``, drive letter, UNC).
            2. Reject empty names, names containing ASCII control characters,
               and names carrying a scheme prefix such as ``file://``.
            3. Reject anything not in the ``ALLOWED_QUICK_SCRIPTS`` code-level
               whitelist. The whitelist is a frozenset constant and is NEVER
               read from user-writable configuration.
            4. On any rejection, log a redacted security alert via
               ``_emit_security_alert`` and return WITHOUT spawning any
               subprocess.
        """
        # --- 0. Type guard (defensive; caller already narrows to str) ---
        if not isinstance(script_name, str):
            self._emit_security_alert("script_name_not_string", script_name)
            return

        # --- 1. Reject URL-ish schemes BEFORE ``Path.name`` collapses them ---
        # ``Path("file:///c:/x").name`` would yield ``"x"`` which is deceptively
        # innocent; screen the raw input first to catch protocol smuggling.
        stripped = script_name.strip()
        lowered = stripped.lower()
        for scheme in _FORBIDDEN_SCRIPT_SCHEMES:
            if lowered.startswith(scheme):
                self._emit_security_alert("script_name_scheme", script_name)
                return

        # --- 2. Reject absolute / UNC / drive-letter / relative-escape forms ---
        # These would otherwise be reduced by ``Path.name`` to an apparently
        # safe basename, masking the attacker intent. R5 AC-4 forbids any of
        # these from reaching the whitelist check at all.
        if (
            stripped.startswith("/")
            or stripped.startswith("\\")
            or stripped.startswith("~")
            or ".." in stripped
            or (len(stripped) >= 2 and stripped[1] == ":")  # e.g. "C:\\"
        ):
            self._emit_security_alert("script_name_path_escape", script_name)
            return

        # --- 3. Path.name normalization (R5 AC-1) ---
        try:
            sanitized = Path(script_name).name
        except (ValueError, OSError):
            self._emit_security_alert("script_name_unparseable", script_name)
            return

        # --- 4. Empty / control-character rejection (R5 AC-3) ---
        if not sanitized:
            self._emit_security_alert("script_name_empty", script_name)
            return
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in sanitized):
            self._emit_security_alert("script_name_control_char", script_name)
            return
        # Path separators surviving ``.name`` would indicate a very exotic
        # platform; treat defensively.
        if "/" in sanitized or "\\" in sanitized:
            self._emit_security_alert("script_name_separator", script_name)
            return

        # --- 5. Whitelist check (R5 AC-2) ---
        if sanitized not in ALLOWED_QUICK_SCRIPTS:
            self._emit_security_alert("script_name_not_whitelisted", script_name)
            return

        # --- 6. Resolve under the built-in scripts directory only ---
        app_root = get_app_root()
        scripts_dir = (app_root / "scripts").resolve()
        try:
            script_path = (scripts_dir / sanitized).resolve()
        except (OSError, RuntimeError):
            self._emit_security_alert("script_path_unresolvable", script_name)
            return
        # Containment check: resolved target MUST stay under ``scripts/``.
        try:
            script_path.relative_to(scripts_dir)
        except ValueError:
            self._emit_security_alert("script_path_escape_resolved", script_name)
            return
        if not script_path.is_file():
            QMessageBox.warning(self, "文件不存在", f"未找到脚本：\n{script_path}")
            return

        # --- 7. Spawn the (now-validated) script ---
        try:
            import subprocess
            import platform

            if platform.system() == "Windows":
                subprocess.Popen(
                    ["cmd.exe", "/K", str(script_path)],
                    cwd=str(script_path.parent),
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            else:
                subprocess.Popen([str(script_path)], cwd=str(script_path.parent))

            logger.info(f"[MANUAL] 已启动脚本: {sanitized}")
            self.statusBar().showMessage(f"已启动脚本: {sanitized}", 3000)
        except Exception as e:
            logger.error(f"[MANUAL] 启动脚本失败: {sanitized}, 错误: {e}")
            QMessageBox.critical(self, "启动失败", f"脚本启动失败：\n{e}")

    def _emit_security_alert(self, reason: str, raw_input) -> None:
        """Log a redacted security alert for a rejected quick-script request.

        Never echo the raw user-provided ``script_name`` (R5 AC-3 + R3 log
        redaction). ``redact_url`` handles URL-shaped inputs; for free-form
        inputs we additionally length-cap and strip control characters so
        a crafted payload cannot break log parsing or leak token-shaped
        query values.
        """
        try:
            if isinstance(raw_input, str):
                redacted = redact_url(raw_input)
            else:
                redacted = f"<non-string:{type(raw_input).__name__}>"
        except Exception:
            redacted = "<redact_failed>"

        # Sanitize for safe log output: strip control chars, cap length.
        if isinstance(redacted, str):
            redacted = "".join(
                c if 0x20 <= ord(c) < 0x7F or ord(c) > 0xA0 else "?"
                for c in redacted
            )
            if len(redacted) > 120:
                redacted = redacted[:117] + "..."

        logger.warning(
            "[SECURITY] quick_manual_script rejected: reason=%s raw=%s",
            reason,
            redacted,
        )
        try:
            self.statusBar().showMessage(f"脚本被拒绝 ({reason})", 3000)
        except (RuntimeError, AttributeError):
            # Status bar unavailable during teardown (Qt object deleted or
            # main window partially torn down); the security warning already
            # went through logger.warning above, so this is purely UX.
            logger.debug("main_window: statusBar unavailable for rejection hint")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        """窗口关闭事件"""
        logger.info(TR("log_closing"))
        self.catcatch_server.stop()
        self.download_manager.shutdown()
        # ISS-34: 释放 BrowserView 的 Playwright driver
        try:
            if hasattr(self, "browser") and self.browser is not None:
                self.browser.stop_browser()
        except Exception:
            logger.debug("main_window: browser.stop_browser() failed during closeEvent", exc_info=True)
        # ISS-34: 释放组件更新 worker 线程
        try:
            if hasattr(self, "component_startup_worker") and self.component_startup_worker is not None:
                self.component_startup_worker.shutdown()
        except Exception:
            logger.debug("main_window: component_startup_worker.shutdown() failed during closeEvent", exc_info=True)
        event.accept()


def summarize_component_entry_statuses(statuses: list) -> dict[str, int]:
    """Summarize read-only component statuses for the main-window entry badge."""
    updates = 0
    missing = 0
    failed = 0
    total = 0
    for status in statuses or []:
        total += 1
        raw_status = str(getattr(status, "status", "") or "").strip().lower()
        if bool(getattr(status, "update_available", False)) or raw_status == "update_available" or raw_status == "remote_is_prerelease":
            updates += 1
        if raw_status == "missing":
            missing += 1
        if raw_status in {"failed", "remote_check_failed", "asset_missing"}:
            failed += 1
    return {"updates": updates, "missing": missing, "failed": failed, "total": total}
