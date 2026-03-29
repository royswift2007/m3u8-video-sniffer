"""
Logging utility for the application
"""
import logging
import sys
from datetime import datetime

from core.app_paths import get_logs_dir
from utils.log_retention import CapacityManagedFileHandler


class Logger:
    """日志管理器"""
    
    def __init__(self, name: str = "M3U8Sniffer", log_file: str = None):
        self._ensure_utf8_console()
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        
        # 避免重复添加 handler
        if not self.logger.handlers:
            # 控制台输出
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_format = logging.Formatter(
                '[%(levelname)s] %(message)s'
            )
            console_handler.setFormatter(console_format)
            self.logger.addHandler(console_handler)
            
            # 文件输出
            if log_file is None:
                log_dir = get_logs_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                log_file = log_dir / f"m3u8sniffer_{datetime.now().strftime('%Y%m%d')}.log"
            
            file_handler = CapacityManagedFileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_format = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(file_format)
            self.logger.addHandler(file_handler)

    @staticmethod
    def _ensure_utf8_console():
        """Best-effort UTF-8 console output on Windows to reduce mojibake."""
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is None:
                continue
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    # Keep default console settings if reconfigure is unavailable.
                    pass
    
    def _format_kv(self, **kwargs) -> str:
        if not kwargs:
            return ""
        parts = []
        for key in sorted(kwargs):
            value = kwargs.get(key)
            if value is None:
                continue
            text = str(value).replace("\n", " ").replace("\r", " ").replace("\t", " ")
            parts.append(f"{key}={text}")
        return " " + " ".join(parts) if parts else ""

    def debug(self, message: str, **kwargs):
        self.logger.debug(f"{message}{self._format_kv(**kwargs)}")
    
    def info(self, message: str, **kwargs):
        self.logger.info(f"{message}{self._format_kv(**kwargs)}")
    
    def warning(self, message: str, **kwargs):
        self.logger.warning(f"{message}{self._format_kv(**kwargs)}")
    
    def error(self, message: str, **kwargs):
        self.logger.error(f"{message}{self._format_kv(**kwargs)}")
    
    def critical(self, message: str, **kwargs):
        self.logger.critical(f"{message}{self._format_kv(**kwargs)}")


# 全局日志实例
logger = Logger()
