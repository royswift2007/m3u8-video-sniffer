"""
Network request interceptor for detecting video resources.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

from utils.config_manager import config
from utils.logger import logger


class NetworkInterceptor(QWebEngineUrlRequestInterceptor):
    """Intercept browser requests and emit candidate media URLs."""

    video_detected = pyqtSignal(str, str, str)  # url, referer, user_agent

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_page_url = ""
        self._features = config.get("features", {}) or {}
        logger.info("网络拦截器已初始化")

    def set_current_page(self, page_url: str):
        """Set current page URL from browser view."""
        self.current_page_url = page_url

    def interceptRequest(self, info):
        """Intercept every request and emit possible media entries."""
        url = info.requestUrl().toString()

        if self._features.get("sniffer_filter_noise", True) and self._is_noise_url(url):
            return
        if not self._is_video_url(url):
            return

        referer = info.firstPartyUrl().toString()
        user_agent = ""

        try:
            ua_bytes = info.requestHeader(b"User-Agent")
            user_agent = ua_bytes.data().decode("utf-8", errors="ignore") if ua_bytes else ""
        except Exception as e:
            logger.debug(f"[INTERCEPT] 读取 User-Agent 失败: {e}")

        try:
            ref_bytes = info.requestHeader(b"Referer")
            if ref_bytes:
                referer = ref_bytes.data().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.debug(f"[INTERCEPT] 读取 Referer 失败: {e}")

        if self._features.get("sniffer_rules_enabled", True):
            logger.info("[VIDEO] 检测到视频资源", event="sniffer_hit", url=url)
            logger.debug("资源请求头", referer=referer)
            self.video_detected.emit(url, referer, user_agent)

    def _is_video_url(self, url: str) -> bool:
        """Return True if URL looks like media resource."""
        url_lower = (url or "").lower()
        if not url_lower:
            return False

        if ".m3u8" in url_lower or ".mpd" in url_lower:
            return True

        video_exts = (
            ".mp4",
            ".flv",
            ".mkv",
            ".avi",
            ".mov",
            ".wmv",
            ".webm",
            ".m4v",
            ".3gp",
            ".mpg",
            ".mpeg",
            ".f4v",
            ".m4s",
            ".ts",
        )
        url_without_params = url_lower.split("?")[0]
        return url_without_params.endswith(video_exts)

    def _is_noise_url(self, url: str) -> bool:
        """Return True if URL is likely static/noise request."""
        url_lower = (url or "").lower()
        if not url_lower:
            return True

        if url_lower.startswith("data:") or url_lower.startswith("blob:"):
            return True

        static_exts = (
            ".js",
            ".css",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".ico",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".eot",
            ".json",
            ".xml",
            ".map",
        )
        url_without_params = url_lower.split("?")[0]
        if url_without_params.endswith(static_exts):
            return True

        noise_keywords = (
            "analytics",
            "track",
            "tracker",
            "beacon",
            "log",
            "metrics",
            "report",
            "collect",
            "adservice",
            "doubleclick",
            "pixel",
        )
        return any(keyword in url_lower for keyword in noise_keywords)

