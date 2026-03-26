"""
System notification utility
"""
from utils.config_manager import config
from utils.logger import logger


def notify(title: str, message: str, timeout: int = 10):
    """
    发送系统通知
    
    Args:
        title: 通知标题
        message: 通知内容
        timeout: 显示时长（秒）
    """
    if not config.get("notification_enabled", True):
        return
    
    # 注意：plyer 在 Windows 上会导致 CMD 窗口闪现，暂时禁用
    # 只记录日志
    logger.info(f"通知: {title} - {message}")
    
    # 如果需要启用系统通知，可以取消下面的注释
    # try:
    #     from plyer import notification as plyer_notify
    #     plyer_notify.notify(
    #         title=title,
    #         message=message,
    #         app_name='M3U8 Video Sniffer',
    #         timeout=timeout
    #     )
    # except Exception as e:
    #     logger.error(f"发送通知失败: {e}")


def notify_resource_found(resource_title: str):
    """资源发现通知"""
    notify("发现新资源", f"检测到视频: {resource_title}")


def notify_download_started(filename: str, engine: str):
    """下载开始通知"""
    notify("开始下载", f"{filename}\n使用引擎: {engine}")


def notify_download_completed(filename: str):
    """下载完成通知"""
    notify("下载完成", f"{filename}\n点击打开文件夹")


def notify_download_failed(filename: str, error: str):
    """下载失败通知"""
    notify("下载失败", f"{filename}\n错误: {error}")
