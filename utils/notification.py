"""
System notification utility.

Delegates to :class:`utils.notification_service.NotificationService` which
uses ``QSystemTrayIcon.showMessage`` for native, thread-safe desktop
notifications without CMD flash.
"""
from utils.config_manager import config


def notify(title: str, message: str, timeout: int = 10):
    """
    发送系统通知

    Args:
        title: 通知标题
        message: 通知内容
        timeout: 显示时长（秒），仅对 tray 通知有效
    """
    if not config.get("notification_enabled", True):
        return

    # Lazy import to avoid circular dependency during early startup
    # (NotificationService requires QApplication which may not exist yet).
    from utils.notification_service import NotificationService

    try:
        NotificationService.instance().notify(title, message,
                                              timeout_ms=timeout * 1000)
    except Exception:
        # Never let a notification failure propagate to caller.
        pass


def notify_resource_found(resource_title: str):
    """资源发现通知"""
    from utils.i18n import TR
    notify(TR("notif_resource_found"), TR("msg_detected_video", title=resource_title))


def notify_download_started(filename: str, engine: str):
    """下载开始通知"""
    from utils.i18n import TR
    notify(TR("notif_download_started"), TR("msg_using_engine", filename=filename, engine=engine))


def notify_download_completed(filename: str):
    """下载完成通知"""
    from utils.i18n import TR
    notify(TR("notif_download_completed"), TR("msg_click_open_folder", filename=filename))


def notify_download_failed(filename: str, error: str):
    """下载失败通知"""
    from utils.i18n import TR
    notify(TR("notif_download_failed"), TR("msg_error_detail", filename=filename, error=error))
