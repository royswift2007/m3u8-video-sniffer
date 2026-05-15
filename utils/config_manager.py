"""
Configuration manager for loading and saving application settings.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.app_paths import get_app_root, get_bin_path, get_temp_dir, is_frozen, resolve_app_path
from utils.engine_paths import (
    find_engine_on_path,
    reconcile_trusted_registry,
    validate_engine_exe,
)
from utils.errors import StructuredError
from utils.json_store import backup_path_for, corrupt_path_for, write_json_atomic
from utils.logger import logger


#: Current configuration schema version. Bump when introducing a new
#: ``_migrate_vN_to_v{N+1}`` step. Readers that don't know about this key
#: still see the rest of the config unchanged because the field is opaque
#: to them; writers always stamp this version so repeated loads are
#: idempotent.
CONFIG_SCHEMA_VERSION = 1


class ConfigManager:
    """Application config manager."""

    ENGINE_BIN_MAP = {
        "n_m3u8dl_re": "N_m3u8DL-RE.exe",
        "ytdlp": "yt-dlp.exe",
        "streamlink": "streamlink.exe",
        "aria2": "aria2c.exe",
        "ffmpeg": "ffmpeg.exe",
    }

    def __init__(self, config_path: str = "config.json"):
        self.base_dir = get_app_root()
        self.config_path = str(resolve_app_path(config_path))
        self.config: Dict[str, Any] = {}
        # Per-process reentrant lock; ``set`` may call ``save`` which also
        # acquires the lock, so it must be reentrant (RLock, not Lock).
        self._lock = threading.RLock()
        # Last persistence failure; exposed for callers that want to surface
        # structured errors without changing the legacy ``save()`` signature.
        self._last_save_error: Optional[StructuredError] = None
        # Reconcile the user-approved engine paths registry once per
        # process start. Entries whose on-disk sha256 no longer matches
        # are evicted so that a later ``validate_engine_exe`` falls back
        # to the default bundled binary. Best-effort — failures are
        # logged by :func:`reconcile_trusted_registry` itself.
        try:
            reconcile_trusted_registry()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"reconcile_trusted_registry 失败（已忽略）: {exc}")
        self.load()

    def load(self):
        """Load config from disk and merge with defaults."""
        with self._lock:
            self._load_locked()

    def _load_locked(self):
        """Load config from disk; caller must hold ``self._lock``."""
        default_config = self._build_default_config()
        config_file = Path(self.config_path)
        backup_file = backup_path_for(config_file)

        if not config_file.exists() and not backup_file.exists():
            logger.info("配置文件不存在，使用默认配置")
            self.config = default_config
            runtime_changed = self._sanitize_runtime_paths()
            self._ensure_directories()
            if runtime_changed:
                logger.info("首次运行已完成运行时路径初始化")
            self.save()
            return

        try:
            loaded_config, recovered_from_backup = self._load_saved_config()
            if isinstance(loaded_config, dict):
                loaded_config.pop("_说明", None)
            else:
                raise ValueError("配置文件顶层必须是对象")
            loaded_config, migrated = self._apply_migrations(loaded_config)
            merged_config, changed = self._merge_with_defaults(default_config, loaded_config)
            self.config = merged_config
            runtime_changed = self._sanitize_runtime_paths()
            changed = (
                changed
                or runtime_changed
                or recovered_from_backup
                or migrated
                or not backup_file.exists()
            )
            self._ensure_directories()
            if changed:
                if migrated:
                    logger.info("检测到旧版本配置，已完成迁移并保存")
                else:
                    logger.info("检测到缺省配置项或运行时路径异常，已自动修正并保存")
                self.save()
            else:
                logger.info("配置已加载")
        except Exception as e:
            logger.error(f"加载配置失败: {e}，回退到默认配置")
            self.config = default_config
            runtime_changed = self._sanitize_runtime_paths()
            self._ensure_directories()
            if runtime_changed:
                logger.info("回退默认配置后已完成运行时路径修正")
            self.save()

    def save(self) -> Optional[StructuredError]:
        """Save runtime config to disk atomically under a per-process lock.

        Returns
        -------
        Optional[StructuredError]
            ``None`` on success. On failure, returns a ``StructuredError``
            with ``code="config_write_failed"``; the in-memory config and
            the previous on-disk ``config.json`` are left untouched. The
            same error is also cached in ``self._last_save_error`` for
            legacy call sites that ignore the return value.
        """
        with self._lock:
            try:
                config_with_comments = copy.deepcopy(self.config)
                config_with_comments["_说明"] = self._build_comment_map()
            except Exception as exc:
                # Snapshotting the in-memory config must never throw in
                # practice; if it does we still want a structured error
                # rather than a raw exception propagating to the UI thread.
                err = StructuredError(
                    code="config_write_failed",
                    reason=repr(exc),
                    stage="fs",
                    details={"path": self.config_path, "phase": "snapshot"},
                )
                self._last_save_error = err
                logger.error(f"保存配置失败(快照阶段): {exc}")
                return err

            target_path = Path(self.config_path)
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                err = StructuredError(
                    code="config_write_failed",
                    reason=repr(exc),
                    stage="fs",
                    details={"path": self.config_path, "phase": "mkdir"},
                )
                self._last_save_error = err
                logger.error(f"保存配置失败(创建目录): {exc}")
                return err

            tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
            try:
                # Write to sibling .tmp, flush + fsync, then atomic replace.
                with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(
                        config_with_comments,
                        handle,
                        indent=4,
                        ensure_ascii=False,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, target_path)
            except OSError as exc:
                # Write failed: drop the partial tmp, keep the in-memory
                # values and the existing on-disk config intact, and
                # return a structured error.
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError as cleanup_exc:
                    logger.warning(
                        f"清理临时配置文件失败: {cleanup_exc} ({tmp_path})"
                    )
                err = StructuredError(
                    code="config_write_failed",
                    reason=repr(exc),
                    stage="fs",
                    details={"path": self.config_path, "phase": "write"},
                )
                self._last_save_error = err
                logger.error(f"保存配置失败: {exc}")
                return err
            except Exception as exc:  # pragma: no cover - defensive
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                err = StructuredError(
                    code="config_write_failed",
                    reason=repr(exc),
                    stage="fs",
                    details={"path": self.config_path, "phase": "serialize"},
                )
                self._last_save_error = err
                logger.error(f"保存配置失败(序列化): {exc}")
                return err

            # Refresh sibling .bak so recovery still has a good copy.
            try:
                write_json_atomic(
                    backup_path_for(target_path),
                    config_with_comments,
                    indent=4,
                    ensure_ascii=False,
                    write_backup=False,
                )
            except OSError as exc:
                # Backup refresh is best-effort; a failed .bak must not
                # turn a successful save into a reported failure.
                logger.warning(f"刷新配置备份失败(已忽略): {exc}")

            self._last_save_error = None
            logger.debug(f"配置已保存: {self.config_path}")
            return None

    def _load_saved_config(self) -> Tuple[Dict[str, Any], bool]:
        """Load config JSON from the primary file or its backup."""
        config_file = Path(self.config_path)
        backup_file = backup_path_for(config_file)
        primary_error: Exception | None = None

        if config_file.exists():
            try:
                return self._load_json_file(config_file), False
            except json.JSONDecodeError as exc:
                primary_error = exc
                logger.error(f"Primary config is corrupted: {exc}")
                self._quarantine_corrupted_config(config_file)
            except Exception as exc:
                primary_error = exc
                logger.warning(f"Primary config read failed, trying backup: {exc}")

        if backup_file.exists():
            logger.warning(f"Recovered config from backup: {backup_file}")
            return self._load_json_file(backup_file), True

        if primary_error is not None:
            raise primary_error
        raise FileNotFoundError(self.config_path)

    def _load_json_file(self, path: Path) -> Dict[str, Any]:
        """Load a JSON object from disk."""
        with open(path, "r", encoding="utf-8") as handle:
            loaded_config = json.load(handle)
        if isinstance(loaded_config, dict):
            return loaded_config
        raise ValueError("Config file top-level value must be an object")

    def _quarantine_corrupted_config(self, path: Path):
        """Move a corrupted config file aside before recovery."""
        try:
            if path.exists():
                os.replace(path, corrupt_path_for(path))
        except Exception as exc:
            logger.error(f"Failed to quarantine corrupted config: {exc}")

    def get(self, key: str, default: Any = None) -> Any:
        """Read config by dotted key."""
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            self.config = self._build_default_config()
            self._ensure_directories()
            self.save()

    def get_last_save_error(self) -> Optional[StructuredError]:
        """Return the last persistence error, or ``None`` if the last save succeeded."""
        with self._lock:
            return self._last_save_error

    def _build_default_config(self) -> Dict[str, Any]:
        """Build default runtime config."""
        return {
            "version": CONFIG_SCHEMA_VERSION,
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
                "network_verify_tls": True,
                "hls_probe_enabled": True,
                "hls_probe_hard_fail": True,
                "allow_segment_probe_soft_fail": True,
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
            "language": "zh",
            "proxy": {
                "enabled": False,
                "http": "",
                "https": "",
            },
        }

    # ------------------------------------------------------------------
    # Migration chain
    # ------------------------------------------------------------------
    def _apply_migrations(self, loaded: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Run the ``_migrate_vN_to_v{N+1}`` chain until ``loaded`` matches
        ``CONFIG_SCHEMA_VERSION``.

        The read path calls this before merging with defaults. Each step
        receives and returns the loaded dict, so future migrations can add
        keys, rename fields, or drop legacy entries. Missing/invalid
        ``version`` is treated as ``0``. The routine is idempotent: once
        the stamped version already matches, no step runs.

        Returns
        -------
        (migrated_config, migrated)
            ``migrated`` is ``True`` when at least one step fired so the
            caller can trigger an atomic write-back.
        """
        raw_version = loaded.get("version")
        try:
            current_version = int(raw_version)
        except (TypeError, ValueError):
            current_version = 0

        if current_version >= CONFIG_SCHEMA_VERSION:
            # Already at target version (or newer from a future client).
            # Ensure the stamp is an int for forward-compat readers and
            # skip the chain entirely so repeated loads don't churn.
            if raw_version != current_version:
                loaded["version"] = current_version
                return loaded, True
            return loaded, False

        migrations = {
            0: self._migrate_v0_to_v1,
            # Future: 1: self._migrate_v1_to_v2, ...
        }

        migrated = False
        while current_version < CONFIG_SCHEMA_VERSION:
            step = migrations.get(current_version)
            if step is None:
                logger.warning(
                    f"未找到 config 迁移步骤 v{current_version} -> v{current_version + 1}，停止迁移"
                )
                break
            try:
                loaded = step(loaded)
            except Exception as exc:
                logger.error(
                    f"config 迁移 v{current_version} -> v{current_version + 1} 失败: {exc}"
                )
                break
            current_version += 1
            loaded["version"] = current_version
            migrated = True
            logger.info(f"已将 config 迁移至 v{current_version}")

        return loaded, migrated

    def _migrate_v0_to_v1(self, loaded: Dict[str, Any]) -> Dict[str, Any]:
        """Stamp legacy (unversioned) configs as v1.

        v0 is the implicit schema used by every config.json shipped before
        versioning was introduced. Beyond caller-supplied version stamping,
        this step is a no-op: the existing merge with defaults already
        reconciles missing keys. Future migrations should mutate and
        return ``loaded`` in place with whatever field renames / key
        moves are needed.
        """
        return loaded

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

    def _sanitize_runtime_paths(self) -> bool:
        """Repair polluted runtime paths and enforce engine exe safety.

        Integrates Requirement 7 (engine exe path hardening):

        * Each ``engines.<name>.path`` value is run through
          :func:`utils.engine_paths.validate_engine_exe`.
        * Paths that fail validation are cleared and replaced with the
          default ``bin/<engine>.exe`` path; an ``engine_path_tampered``
          warning is emitted so operators can investigate.
        * When the default bundled binary is missing we fall back to
          :func:`utils.engine_paths.find_engine_on_path`, which only
          accepts ``.exe`` hits on ``PATH``.
        """
        changed = False

        temp_dir = self.config.get("temp_dir")
        expected_temp_dir = str(get_temp_dir())
        if not temp_dir or self._is_invalid_runtime_temp_dir(temp_dir):
            self.config["temp_dir"] = expected_temp_dir
            changed = True
            logger.warning(
                f"检测到 temp_dir 非法或不可用，已修正为运行时目录: {expected_temp_dir}"
            )

        engines = self.config.get("engines")
        if not isinstance(engines, dict):
            return changed

        for engine_key, executable_name in self.ENGINE_BIN_MAP.items():
            engine_config = engines.get(engine_key)
            if not isinstance(engine_config, dict):
                continue

            current_path = engine_config.get("path")
            expected_path = str(get_bin_path(executable_name))

            # Fast path: when the default bundled binary is present and
            # the current value looks sane, keep the legacy repair logic.
            if self._should_repair_engine_path(current_path, expected_path):
                engine_config["path"] = expected_path
                changed = True
                logger.warning(
                    f"检测到 {engine_key} 引擎路径失效或已污染，已修正为: {expected_path}"
                )
                current_path = expected_path

            # Security-hardened validation (R7). Only applied to real
            # string values; the repair above already normalized empty /
            # missing fields. A failure here means the path is outside
            # the allowed roots *and* not trusted — reset it.
            if isinstance(current_path, str) and current_path:
                validated = validate_engine_exe(current_path)
                if validated is None:
                    # Structured "engine_path_tampered" warning. We avoid
                    # echoing the full offending string at INFO; a debug
                    # level log keeps the detail available when opted in.
                    logger.warning(
                        "engine_path_tampered: "
                        f"engine={engine_key} reason=validation_failed"
                    )
                    logger.debug(
                        f"engine_path_tampered 详情: engine={engine_key} "
                        f"offending={current_path!r}"
                    )
                    fallback = self._resolve_engine_fallback(executable_name)
                    if fallback is None:
                        # No safe default available. Clear the field so
                        # downstream loaders treat the engine as missing
                        # rather than attempting to launch a bad binary.
                        if "path" in engine_config:
                            engine_config["path"] = ""
                            changed = True
                    else:
                        if engine_config.get("path") != fallback:
                            engine_config["path"] = fallback
                            changed = True

        return changed

    def _resolve_engine_fallback(self, executable_name: str) -> str | None:
        """Return the safe default path for an engine, or ``None`` if absent.

        Preference order matches Requirement 7.5:

        1. The bundled ``bin/<executable>`` binary, when present.
        2. ``shutil.which(executable_name)`` via
           :func:`utils.engine_paths.find_engine_on_path` — but only if
           the match is an ``.exe`` outside UNC/symlink-escape territory.
        3. ``None`` — caller clears the field so the engine is reported
           as unavailable rather than launched from an unsafe location.
        """
        default_path = get_bin_path(executable_name)
        if default_path.exists():
            validated = validate_engine_exe(default_path)
            if validated is not None:
                return str(validated)

        path_hit = find_engine_on_path(executable_name)
        if path_hit is not None:
            validated = validate_engine_exe(path_hit)
            if validated is not None:
                return str(validated)

        return None

    def _should_repair_engine_path(self, current_path: Any, expected_path: str) -> bool:
        """Return True if engine path should be reset to current runtime bin path."""
        expected = Path(expected_path)
        if not expected.exists():
            return False
        if not current_path or not isinstance(current_path, str):
            return True

        current = Path(current_path)
        if current == expected and current.exists():
            return False
        if not current.exists():
            return True
        if is_frozen() and current.resolve() != expected.resolve():
            return True
        return False

    def _is_invalid_runtime_temp_dir(self, temp_dir: Any) -> bool:
        """Return True if temp directory should be reset for current runtime."""
        if not temp_dir or not isinstance(temp_dir, str):
            return True

        expected = get_temp_dir()
        current = Path(temp_dir)
        if current == expected:
            return False
        if is_frozen():
            try:
                current.resolve().relative_to(self.base_dir.resolve())
                return True
            except Exception:
                return False
        return False

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
            "version": "配置文件 schema 版本，勿手动修改，升级时自动迁移",
            "max_concurrent_downloads": "同时下载任务数，建议 2-3",
            "speed_limit": "下载限速(MB/s)，0 表示不限速",
            "max_retry_attempts": "任务最大重试次数（含引擎切换）",
            "retry_backoff_seconds": "重试等待秒数",
            "site_rules": "站点规则数组，字段示例: name/domains/url_keywords/referer/user_agent/headers",
            "features": "功能开关集合(sniffer/download/ui)",
            "network_verify_tls": "是否校验 HTTPS/TLS 证书，默认开启；仅在证书异常且确认可信时关闭",
            "thread_count": "单任务下载线程数，建议 8-16",
            "thread_min": "自适应线程下限",
            "thread_max": "自适应线程上限",
            "retry_count": "引擎内部重试次数",
            "max_retry": "引擎最大重试次数(兼容参数)",
            "adaptive": "弱网自适应开关(引擎支持时生效)",
            "output_format": "输出格式，建议 mp4",
            "language": "界面语言: zh (中文), en (英文)",
        }


# 全局配置实例
config = ConfigManager()
