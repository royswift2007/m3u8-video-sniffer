"""
Configuration manager for loading and saving application settings.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from core.app_paths import get_app_root, get_bin_path, get_temp_dir, resolve_app_path
from utils.logger import logger


class ConfigManager:
    """Application config manager."""

    def __init__(self, config_path: str = "config.json"):
        self.base_dir = get_app_root()
        self.config_path = str(resolve_app_path(config_path))
        self.config: Dict[str, Any] = {}
        self.load()

    def load(self):
        """Load config from disk and merge with defaults."""
        default_config = self._build_default_config()
        config_file = Path(self.config_path)

        if not config_file.exists():
            logger.info("配置文件不存在，使用默认配置")
            self.config = default_config
            self._ensure_directories()
            self.save()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded_config = json.load(f)
            if isinstance(loaded_config, dict):
                loaded_config.pop("_说明", None)
            else:
                raise ValueError("配置文件顶层必须是对象")
            merged_config, changed = self._merge_with_defaults(default_config, loaded_config)
            self.config = merged_config
            self._ensure_directories()
            if changed:
                logger.info("检测到缺省配置项，已自动补齐并保存")
                self.save()
            else:
                logger.info("配置已加载")
        except Exception as e:
            logger.error(f"加载配置失败: {e}，回退到默认配置")
            self.config = default_config
            self._ensure_directories()
            self.save()

    def save(self):
        """Save runtime config to disk with a lightweight help section."""
        try:
            config_with_comments = copy.deepcopy(self.config)
            config_with_comments["_说明"] = self._build_comment_map()
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config_with_comments, f, indent=4, ensure_ascii=False)
            logger.debug(f"配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """Read config by dotted key."""
        keys = key.split(".")
        value: Any = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default
        if value == "" or value is None:
            return default
        return value

    def set(self, key: str, value: Any):
        """Set config by dotted key and persist to disk."""
        keys = key.split(".")
        config = self.config
        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value
        self._ensure_directories()
        self.save()

    def _load_defaults(self):
        """
        Backward-compatible helper.
        Keep this method for old call sites; it now means reset-to-default and save.
        """
        self.config = self._build_default_config()
        self._ensure_directories()
        self.save()

    def _build_default_config(self) -> Dict[str, Any]:
        """Build default runtime config."""
        return {
            "download_dir": str(Path.home() / "Downloads"),
            "temp_dir": str(get_temp_dir()),
            "max_concurrent_downloads": 2,
            "speed_limit": 3,
            "max_retry_attempts": 3,
            "retry_backoff_seconds": 2,
            "site_rules": [],
            "site_rules_auto": {
                "enabled": False,
                "max_rules": 50,
                "allow_cookie": False,
            },
            "features": {
                "sniffer_rules_enabled": True,
                "sniffer_dedup_enabled": True,
                "sniffer_filter_noise": True,
                "download_retry_enabled": True,
                "download_engine_fallback": True,
                "download_auth_retry_first": True,
                "download_auth_retry_per_engine": 1,
                "download_candidate_ranking_enabled": True,
                "hls_probe_enabled": True,
                "hls_probe_hard_fail": True,
                "browser_capture_window_enabled": True,
                "browser_capture_window_seconds": 12,
                "browser_capture_extend_on_hit_seconds": 4,
                "browser_capture_probe_interval_ms": 1000,
                "ui_batch_actions": True,
                "ui_filter_search": True,
            },
            "engines": {
                "n_m3u8dl_re": {
                    "path": str(get_bin_path("N_m3u8DL-RE.exe")),
                    "thread_count": 8,
                    "thread_min": 4,
                    "thread_max": 32,
                    "retry_count": 5,
                    "max_retry": 5,
                    "adaptive": False,
                    "output_format": "mp4",
                },
                "ytdlp": {
                    "path": str(get_bin_path("yt-dlp.exe")),
                },
                "streamlink": {
                    "path": str(get_bin_path("streamlink.exe")),
                },
                "aria2": {
                    "path": str(get_bin_path("aria2c.exe")),
                    "max_connection_per_server": 16,
                    "split": 16,
                },
                "ffmpeg": {
                    "path": str(get_bin_path("ffmpeg.exe")),
                },
            },
            "notification_enabled": True,
            "auto_delete_temp": True,
            "proxy": {
                "enabled": False,
                "http": "",
                "https": "",
            },
        }

    def _merge_with_defaults(
        self,
        defaults: Dict[str, Any],
        loaded: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        """Deep-merge loaded config into defaults. Returns (merged, changed)."""
        changed = False
        merged: Dict[str, Any] = {}

        for key, default_value in defaults.items():
            if key not in loaded or loaded[key] is None:
                merged[key] = copy.deepcopy(default_value)
                changed = True
                continue

            loaded_value = loaded[key]
            if isinstance(default_value, dict) and isinstance(loaded_value, dict):
                nested_merged, nested_changed = self._merge_with_defaults(default_value, loaded_value)
                merged[key] = nested_merged
                changed = changed or nested_changed
            else:
                merged[key] = loaded_value

        # Keep unknown keys for forward compatibility.
        for key, value in loaded.items():
            if key not in merged:
                merged[key] = value

        return merged, changed

    def _ensure_directories(self):
        """Ensure download/temp directories exist."""
        try:
            download_dir = self.config.get("download_dir")
            temp_dir = self.config.get("temp_dir")
            if download_dir:
                os.makedirs(download_dir, exist_ok=True)
            if temp_dir:
                os.makedirs(temp_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"创建目录失败: {e}")

    def _build_comment_map(self) -> Dict[str, str]:
        """Build writable help text shown in config.json."""
        return {
            "max_concurrent_downloads": "同时下载任务数，建议 2-3",
            "speed_limit": "下载限速(MB/s)，0 表示不限速",
            "max_retry_attempts": "任务最大重试次数（含引擎切换）",
            "retry_backoff_seconds": "重试等待秒数",
            "site_rules": "站点规则数组，字段示例: name/domains/url_keywords/referer/user_agent/headers",
            "features": "功能开关集合(sniffer/download/ui)",
            "thread_count": "单任务下载线程数，建议 8-16",
            "thread_min": "自适应线程下限",
            "thread_max": "自适应线程上限",
            "retry_count": "引擎内部重试次数",
            "max_retry": "引擎最大重试次数(兼容参数)",
            "adaptive": "弱网自适应开关(引擎支持时生效)",
            "output_format": "输出格式，建议 mp4",
        }


# 全局配置实例
config = ConfigManager()

