"""
Qt system-tray notification service (thread-safe singleton).

Replaces the plyer-based ``utils.notification.notify()`` with
``QSystemTrayIcon.showMessage``, avoiding CMD flash on Windows while
keeping notifications functional across worker threads via Qt signals.
"""
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QSystemTrayIcon, QApplication
from pathlib import Path
from typing import Optional

from utils.logger import logger


class _NotificationBridge(QObject):
    """Internal signal bridge that enqueues notify requests onto the Qt main-thread event loop."""

    _notify_requested = pyqtSignal(str, str, int)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)


class NotificationService(QObject):
    """Thread-safe singleton wrapping a ``QSystemTrayIcon``.

    Inherits ``QObject`` so ``@pyqtSlot`` on ``_on_notify`` is recognised
    by PyQt6's signal/slot type system, allowing ``Qt.QueuedConnection``
    to marshal cross-thread ``emit()`` calls safely onto the main thread.

    Usage::

        NotificationService.instance().notify("Title", "Message")
    """

    _instance: Optional["NotificationService"] = None

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._tray: Optional[QSystemTrayIcon] = None
        self._bridge: Optional[_NotificationBridge] = None
        self._initialized = False

    # ---- singleton access -------------------------------------------------
    @classmethod
    def instance(cls) -> "NotificationService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---- initialization (call once from the Qt main thread) ---------------
    def initialize(self, parent: Optional[QObject] = None) -> None:
        """Create the system-tray icon and wire the signal bridge.

        Must be called from the Qt main thread **after** ``QApplication``
        exists.  Safe to call multiple times (idempotent).
        """
        if self._initialized:
            return

        app = QApplication.instance()
        if app is None:
            logger.warning(
                "NotificationService: QApplication not running; "
                "notifications will be logged only.",
                event="notif_service_no_qapp",
            )
            self._initialized = True
            return

        # Resolve icon path relative to this project
        icon_path = str(
            Path(__file__).parent.parent / "resources" / "mvs.ico"
        )
        icon = QIcon(icon_path) if Path(icon_path).exists() else QIcon()

        self._tray = QSystemTrayIcon(icon, parent)
        self._tray.setToolTip("M3U8 Video Sniffer")

        # Signal bridge – delivers notify() calls onto the main thread
        self._bridge = _NotificationBridge(self)
        self._bridge._notify_requested.connect(
            self._on_notify, Qt.ConnectionType.QueuedConnection
        )

        # Show the tray icon so it is visible
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()
        else:
            logger.info(
                "NotificationService: system tray unavailable; "
                "notifications will be logged only.",
                event="notif_service_tray_unavailable",
            )

        self._initialized = True

    # ---- public API (thread-safe) -----------------------------------------
    def notify(self, title: str, message: str, timeout_ms: int = 5000) -> None:
        """Enqueue a notification to be shown via the system tray.

        Thread-safe – can be called from any thread.
        Falls back to ``logger.info`` when the tray is not available.
        """
        if not self._initialized or self._bridge is None:
            logger.info(f"通知: {title} - {message}")
            return

        try:
            self._bridge._notify_requested.emit(title, message, timeout_ms)
        except Exception:
            logger.info(f"通知: {title} - {message}")

    # ---- internal slot (runs on main thread) ------------------------------
    @pyqtSlot(str, str, int)
    def _on_notify(self, title: str, message: str, timeout_ms: int) -> None:
        tray = self._tray
        if tray is None or not tray.supportsMessages():
            logger.info(f"通知: {title} - {message}")
            return

        try:
            tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information,
                             timeout_ms)
        except Exception as exc:
            logger.debug(
                f"NotificationService: showMessage failed ({exc})",
                event="notif_service_showmessage_failed",
                error_type=type(exc).__name__,
            )
            logger.info(f"通知: {title} - {message}")

    # ---- cleanup ----------------------------------------------------------
    def shutdown(self) -> None:
        """Hide the tray icon (call on app quit)."""
        if self._tray is not None:
            try:
                self._tray.hide()
            except Exception:
                pass
        self._initialized = False
