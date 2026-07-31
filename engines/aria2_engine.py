"""
Aria2 engine wrapper for direct link downloads with multi-threading
"""
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, unquote
from engines.base_engine import BaseEngine, EngineResult
from core.task_model import DownloadTask
from utils.headers import prepare_aria2_headers
from utils.logger import logger
from utils.config_manager import config
from utils.proxy_config import proxy_for_url


class Aria2Engine(BaseEngine):
    """Aria2 下载引擎 - 多线程加速下载"""

    # 支持的直链文件扩展名
    DIRECT_EXTENSIONS = (
        '.mp4', '.flv', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.webm',
        '.m4v', '.3gp', '.mpg', '.mpeg', '.f4v'
    )

    # 仅用于“Aria2 输出文件名补扩展名”推断；不改变 can_handle 语义。
    # 这些后缀几乎总是完整直链媒体，可安全追加到输出名。
    _DIRECT_OUTPUT_EXTENSIONS = frozenset({
        '.mp4', '.flv', '.mkv', '.avi', '.mov', '.wmv', '.webm',
        '.m4v', '.3gp', '.mpg', '.mpeg', '.f4v',
    })

    # .ts 既可能是完整 MPEG-TS，也可能是 HLS 单分片，单独处理。
    _OPTIONAL_TS_OUTPUT_EXTENSIONS = frozenset({'.ts'})

    # 绝不能作为 Aria2 最终媒体输出扩展名（交给 N_m3u8DL-RE）。
    _HLS_OR_DASH_EXTENSIONS = frozenset({'.m3u8', '.mpd'})

    # Content-Type → 输出扩展名回退映射。
    _MIME_EXTENSION_MAP = {
        'video/mp4': '.mp4',
        'video/webm': '.webm',
        'video/x-matroska': '.mkv',
        'video/mp2t': '.ts',
        'video/vnd.dlna.mpeg-tts': '.ts',
        'video/x-flv': '.flv',
        'video/quicktime': '.mov',
    }

    @staticmethod
    def _path_suffix_from_url(url: str) -> str:
        """返回 URL path 的小写后缀，忽略 query/fragment；失败返回空串。"""
        try:
            path = urlparse(url or '').path
            suffix = PurePosixPath(unquote(path)).suffix.lower()
            return suffix
        except Exception:
            return ''

    @classmethod
    def _filename_has_media_suffix(cls, filename: str) -> bool:
        """任务文件名本身是否已带媒体后缀，避免重复追加。"""
        suffix = Path(filename or '').suffix.lower()
        return (
            suffix in cls._DIRECT_OUTPUT_EXTENSIONS
            or suffix in cls._OPTIONAL_TS_OUTPUT_EXTENSIONS
        )

    @classmethod
    def _task_looks_like_hls_or_segment(cls, task: DownloadTask) -> bool:
        """判断当前任务是否不应当由 Aria2 作为最终媒体直链下载。"""
        resource_type = str(getattr(task, 'resource_type', '') or '').lower()
        if resource_type in {'hls', 'dash'}:
            return True

        if resource_type == 'segment':
            source = str(getattr(task, 'source', '') or '').lower()
            has_context_hint = bool(
                getattr(task, 'master_url', None)
                or getattr(task, 'media_url', None)
                or getattr(task, 'page_url', '')
                or source not in {'', 'unknown'}
            )
            if has_context_hint:
                return True

        mime = str(getattr(task, 'mime', '') or '').lower()
        if 'mpegurl' in mime or 'dash+xml' in mime:
            return True

        return cls._path_suffix_from_url(getattr(task, 'url', '')) in cls._HLS_OR_DASH_EXTENSIONS

    @classmethod
    def _infer_direct_output_extension(cls, task: DownloadTask) -> str:
        """仅在确认是直链媒体时推断输出扩展名；否则返回空串。"""
        if cls._task_looks_like_hls_or_segment(task):
            return ''

        suffix = cls._path_suffix_from_url(getattr(task, 'url', ''))
        if suffix in cls._DIRECT_OUTPUT_EXTENSIONS:
            return suffix
        if suffix in cls._OPTIONAL_TS_OUTPUT_EXTENSIONS:
            return suffix

        mime = str(getattr(task, 'mime', '') or '').lower().split(';', 1)[0].strip()
        if 'mpegurl' in mime or 'dash+xml' in mime:
            return ''
        return cls._MIME_EXTENSION_MAP.get(mime, '')

    @classmethod
    def _resolve_output_filename(cls, task: DownloadTask) -> str:
        """生成 Aria2 专用落盘文件名；task.filename 仍保持标题语义不变。"""
        filename = str(getattr(task, 'filename', '') or '').strip()
        if not filename:
            filename = 'download'
        if cls._filename_has_media_suffix(filename):
            return filename
        ext = cls._infer_direct_output_extension(task)
        if not ext:
            return filename
        return f'{filename}{ext}'
    
    def can_handle(self, url: str) -> bool:
        """检测是否为视频直链或磁力链接"""
        # 1. 磁力链接
        if url.startswith("magnet:?"):
            return True
             
        # 2. 直链文件
        # 去除查询参数
        url_without_params = url.split('?')[0]
        return url_without_params.lower().endswith(self.DIRECT_EXTENSIONS)
    
    def get_name(self) -> str:
        return "Aria2"

    @staticmethod
    def _safe_int(value: object, default: int) -> int:
        """Best-effort integer parser for runtime config values."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)
    
    def download(self, task: DownloadTask, progress_callback) -> bool:
        """执行多线程加速下载"""
        progress_stats = self._new_progress_stats()
        low_connection_retries = 0
        last_error_category = "none"
        try:
            cmd = self._build_command(task)
            logger.info(f"[Aria2] 开始下载: {task.filename}")
            self.log_command(cmd)

            # ``BaseEngine.spawn`` centralizes the CREATE_NO_WINDOW /
            # close_fds / byte-mode PIPE plumbing (task 29.1 / Requirement
            # 37.1). ``sensitive=False`` suppresses spawn's internal
            # ``log_command`` call because the redacted argv was already
            # emitted on the line above — otherwise every run would print
            # the same cmd twice. ``read_loop`` drains stdout and stderr
            # on separate pump threads and dispatches by ``stream_tag``
            # (task 9.2), so the legacy ``stderr=STDOUT`` merge is no
            # longer required; ``_parse_line`` treats both tags uniformly.
            process = self.spawn(cmd, sensitive=False)

            # ISS-30: bind process ownership metadata atomically so manager
            # kill paths can guard against pid reuse.
            with task.lock:
                task.process = process
                task._pid = getattr(process, "pid", None)
                task._expected_engine_name = self.get_name()

            # ``read_loop`` drives cancellation and PIPE draining;
            # ``_parse_line`` reads per-call state off the thread-local slot.
            output_lines: list[str] = []
            self._tls.progress_callback = progress_callback
            self._tls.output_lines = output_lines
            self._tls.progress_stats = progress_stats
            try:
                result: EngineResult = self.read_loop(process, task, self._parse_line)
            finally:
                self._tls.progress_callback = None
                self._tls.output_lines = None
                self._tls.progress_stats = None

            if result.status in {"stopped", "switched", "paused"}:
                # Stop requests surface as ``False`` to match the legacy
                # download() contract; DownloadManager classifies them via
                # ``task.stop_reason``.
                self._log_download_summary(
                    task,
                    status=result.status,
                    returncode=result.returncode,
                    progress_stats=progress_stats,
                    low_connection_retries=low_connection_retries,
                    last_error_category=last_error_category,
                )
                return False

            success = result.status == "ok"

            if success:
                logger.info(f"[Aria2] 下载成功: {task.filename}")
                self._log_download_summary(
                    task,
                    status=result.status,
                    returncode=result.returncode,
                    progress_stats=progress_stats,
                    low_connection_retries=low_connection_retries,
                    last_error_category=last_error_category,
                )
                return True

            returncode = (
                result.returncode if result.returncode is not None else -1
            )
            output_text = "\n".join(output_lines)
            last_error_category = self._classify_aria2_output_reason(output_text)
            logger.error(
                f"[Aria2] 下载失败: {task.filename}, 退出码: {returncode}",
                event="aria2_download_failed",
                error_category=last_error_category,
                returncode=returncode,
            )
            logger.error("[Aria2] 建议检查直链有效性或 Referer/Cookie")

            # Low-connection fallback: when output hints at rate-limit /
            # CDN friction, retry once with configurable low split / connection.
            low_connection_reason = self._low_connection_retry_reason(output_text)
            if (
                not getattr(task, "stop_requested", False)
                and self._low_connection_retry_enabled()
                and bool(low_connection_reason)
            ):
                low_connection_retries += 1
                low_connection_count = self._low_connection_count()
                logger.warning(
                    f"[Aria2] 检测到限流/弱网特征，尝试低连接重试 (split={low_connection_count}, max-conn={low_connection_count})",
                    event="aria2_low_connection_retry",
                    connection_count=low_connection_count,
                    low_connection_reason=low_connection_reason,
                )
                low_cmd = self._build_command(task, connection_override=low_connection_count)
                self.log_command(low_cmd, action="low_connection")

                process2 = self.spawn(low_cmd, sensitive=False)
                with task.lock:
                    task.process = process2
                    task._pid = getattr(process2, "pid", None)
                    task._expected_engine_name = self.get_name()

                output_lines2: list[str] = []
                self._tls.progress_callback = progress_callback
                self._tls.output_lines = output_lines2
                self._tls.progress_stats = progress_stats
                try:
                    result2: EngineResult = self.read_loop(process2, task, self._parse_line)
                finally:
                    self._tls.progress_callback = None
                    self._tls.output_lines = None
                    self._tls.progress_stats = None

                if result2.status in {"stopped", "switched", "paused"}:
                    self._log_download_summary(
                        task,
                        status=result2.status,
                        returncode=result2.returncode,
                        progress_stats=progress_stats,
                        low_connection_retries=low_connection_retries,
                        last_error_category=last_error_category,
                    )
                    return False
                if result2.status == "ok":
                    logger.info(
                        f"[Aria2] 低连接下载成功: {task.filename}",
                        event="aria2_low_connection_success",
                        connection_count=low_connection_count,
                    )
                    self._log_download_summary(
                        task,
                        status=result2.status,
                        returncode=result2.returncode,
                        progress_stats=progress_stats,
                        low_connection_retries=low_connection_retries,
                        last_error_category=last_error_category,
                    )
                    return True

                ret2 = result2.returncode if result2.returncode is not None else -1
                last_error_category = self._classify_aria2_output_reason("\n".join(output_lines2))
                logger.error(
                    f"[Aria2] 低连接重试仍失败: 退出码 {ret2}",
                    event="aria2_low_connection_failed",
                    connection_count=low_connection_count,
                    error_category=last_error_category,
                )
                task.error_message = f"Aria2 low-connection retry exit code: {ret2}"

            self._log_download_summary(
                task,
                status=result.status,
                returncode=returncode,
                progress_stats=progress_stats,
                low_connection_retries=low_connection_retries,
                last_error_category=last_error_category,
            )
            return False
            
        except Exception as e:
            last_error_category = "exception"
            logger.error(
                f"[Aria2] 下载异常: {e}",
                event="aria2_download_exception",
                error_type=type(e).__name__,
            )
            task.error_message = str(e)
            self._log_download_summary(
                task,
                status="exception",
                returncode=-1,
                progress_stats=progress_stats,
                low_connection_retries=low_connection_retries,
                last_error_category=last_error_category,
            )
            return False

    def _parse_line(self, stream_tag: str, text: str) -> None:
        """``read_loop`` callback: log + parse one aria2 output line."""
        line = text.strip()
        if not line:
            return

        # Accumulate for post-run diagnostics (low-connection fallback).
        output_lines = getattr(self._tls, "output_lines", None)
        if output_lines is not None:
            output_lines.append(line)

        logger.debug(f"[Aria2] {line}")
        progress_data = self.parse_progress(line)
        self._update_progress_stats(line, progress_data)
        if progress_data['progress'] > 0 or progress_data['speed']:
            progress_callback = getattr(self._tls, "progress_callback", None)
            if progress_callback is not None:
                progress_callback(progress_data)
    
    def _build_command(self, task: DownloadTask, connection_override: int | None = None) -> list:
        """构建下载命令

        Args:
            connection_override: non-None overrides both --max-connection-per-server
                and --split for low-connection fallback.
        """
        max_conn = self._safe_int(config.get("engines.aria2.max_connection_per_server", 2), 2)
        split = self._safe_int(config.get("engines.aria2.split", 2), 2)
        rate_limit_hint = self._task_has_rate_limit_hint(task)
        low_connection_mode = connection_override is not None
        if (
            connection_override is None
            and self._low_connection_retry_enabled()
            and rate_limit_hint
        ):
            low_connection_count = self._low_connection_count()
            max_conn = min(max_conn, low_connection_count)
            split = min(split, low_connection_count)
            low_connection_mode = True
        # Task 27.3: speed_limit unit is MB/s (mebibytes per second).
        # A value of 0 means "no limit" — do NOT add the flag at all.
        speed_limit = config.get("speed_limit", 0)
        # ISS-24: 使用 aria2 自身的重试配置，而非误引用 n_m3u8dl_re 的配置。
        retry_count = config.get("engines.aria2.retry_count", 5)
        min_split_size = '1M'

        if connection_override is not None:
            override_value = self._safe_int(connection_override, 1)
            max_conn = max(1, override_value)
            split = max(1, override_value)
            low_connection_mode = True

        # Aria2 直链补扩展名：task.filename 通常是标题 stem（不带媒体后缀），
        # 这里仅构造 Aria2 专用输出名，不修改 task.filename 本身。
        output_filename = self._resolve_output_filename(task)
        try:
            with task.lock:
                task._aria2_output_filename = output_filename
        except Exception:
            pass
        if output_filename != task.filename:
            logger.info(
                "[Aria2] 输出文件名已补扩展名",
                event="aria2_output_filename_resolved",
                task_filename=task.filename,
                output_filename=output_filename,
                url_suffix=self._path_suffix_from_url(task.url),
                resource_type=str(getattr(task, "resource_type", "") or ""),
                mime=str(getattr(task, "mime", "") or ""),
            )

        cmd = [
            self.binary_path,
            task.url,
            '-d', task.save_dir,
            '-o', output_filename,
            '--max-connection-per-server', str(max_conn),
            '--split', str(split),
            '--min-split-size', min_split_size,
            '--max-tries', str(retry_count),
            '--retry-wait', '3',
            '--continue=true',
            '--allow-overwrite=true',
            '--auto-file-renaming=false',
            '--connect-timeout=10',
            '--timeout=30',
            '--summary-interval', '1',  # 每秒输出进度
            '--console-log-level', 'notice',
        ]
        
        # Task 27.3: speed_limit unit: MB/s (aria2 accepts N[M|K];
        # the ``M`` suffix is mebibytes/s, matching the unified UI semantic).
        # Use ``--max-overall-download-limit`` so the cap applies across all
        # concurrent connections (``--max-download-limit`` is per-download
        # and would let the aggregate exceed the user's configured ceiling).
        try:
            speed_limit_mb = int(speed_limit)
        except (TypeError, ValueError):
            speed_limit_mb = 0
        if speed_limit_mb > 0:
            cmd.extend([
                '--max-overall-download-limit',
                f'{speed_limit_mb}M',
            ])

        logger.info(
            "[Aria2] 命令参数摘要",
            event="aria2_command_summary",
            aria2_max_connection_per_server=max_conn,
            aria2_split=split,
            aria2_min_split_size=min_split_size,
            aria2_retry_count=retry_count,
            speed_limit_enabled=bool(speed_limit_mb > 0),
            speed_limit_mb=speed_limit_mb if speed_limit_mb > 0 else 0,
            low_connection_mode=bool(low_connection_mode),
            rate_limit_hint=bool(rate_limit_hint),
        )
        
        # 添加请求头（Aria2 专属 Range 策略：默认剥离浏览器捕获的 Range）
        self._append_headers(cmd, task.headers)

        proxy = proxy_for_url(task.url)
        if proxy:
            cmd.extend(['--all-proxy', proxy])
        
        return cmd

    def _append_headers(self, cmd: list[str], headers: dict) -> dict[str, object]:
        """Append safe HTTP headers to aria2c argv.

        User-Agent / Referer use dedicated flags; everything else uses
        ``--header Name: value``.  Browser-captured ``Range`` is deliberately
        removed so aria2c can manage segmented Range requests itself.
        """
        safe, summary = prepare_aria2_headers(
            headers,
            include_authorization=bool((headers or {}).get("_allow_authorization_header")),
        )
        if safe.get("user-agent"):
            cmd.extend(["--user-agent", safe["user-agent"]])
        if safe.get("referer"):
            cmd.extend(["--referer", safe["referer"]])

        # Generic headers forwarded via --header.  Range is intentionally absent.
        generic = {
            "origin": "Origin",
            "cookie": "Cookie",
            "accept": "Accept",
            "accept-language": "Accept-Language",
            "authorization": "Authorization",
            "sec-fetch-site": "Sec-Fetch-Site",
            "sec-fetch-mode": "Sec-Fetch-Mode",
            "sec-fetch-dest": "Sec-Fetch-Dest",
            "sec-ch-ua": "Sec-Ch-Ua",
            "sec-ch-ua-mobile": "Sec-Ch-Ua-Mobile",
            "sec-ch-ua-platform": "Sec-Ch-Ua-Platform",
        }
        for lower_key, canonical_name in generic.items():
            value = safe.get(lower_key)
            if value:
                cmd.extend(["--header", f"{canonical_name}: {value}"])

        logger.info(
            "[Aria2] 请求头摘要",
            event="aria2_header_summary",
            **summary,
        )
        if summary.get("range_dropped"):
            logger.info(
                "[Aria2] Range 请求头已按策略剥离",
                event="aria2_range_policy",
                original_range_present=True,
                range_kind=summary.get("range_kind"),
                action=summary.get("range_action"),
                reason=summary.get("range_drop_reason"),
            )
        return summary

    @staticmethod
    def _low_connection_retry_enabled() -> bool:
        """Return whether engine-level low-connection recovery is enabled."""
        features = config.get("features", {}) or {}
        return bool(features.get("rate_limit_low_concurrency_enabled", True))

    @staticmethod
    def _low_connection_count() -> int:
        """Return configured low-connection retry split/connection count."""
        value = Aria2Engine._safe_int(config.get("engines.aria2.low_connection_count", 1), 1)
        return max(1, min(value, 4))

    @staticmethod
    def _task_rate_limit_hint_reason(task: DownloadTask) -> str:
        """Return a non-sensitive reason for prior rate-limit hints."""
        if bool(getattr(task, "rate_limit_retry_hint", False)):
            return "task_rate_limit_retry_hint"
        probe_result = getattr(task, "probe_result", None)
        if not isinstance(probe_result, dict):
            return ""
        if str(probe_result.get("status_code")) == "429":
            return "probe_status_429"
        diagnostics = probe_result.get("stage_diagnostics", [])
        if not isinstance(diagnostics, list):
            return ""
        for item in diagnostics:
            if isinstance(item, dict) and str(item.get("status_code")) == "429":
                return "probe_diagnostic_429"
        return ""

    @staticmethod
    def _task_has_rate_limit_hint(task: DownloadTask) -> bool:
        """Return True when prior probe/download diagnostics suggest CDN rate limiting."""
        reason = Aria2Engine._task_rate_limit_hint_reason(task)
        if reason:
            logger.info(
                "[Aria2] 检测到任务级限流提示",
                event="aria2_task_rate_limit_hint",
                reason=reason,
            )
        return bool(reason)

    @staticmethod
    def _classify_aria2_output_reason(output_text: str) -> str:
        """Classify aria2 output without logging the raw sensitive text."""
        if not output_text:
            return "none"
        lower = output_text.lower()
        if re.search(r"\b429\b", lower) or "too many" in lower:
            return "http_429"
        if re.search(r"\b416\b", lower):
            return "http_416"
        if re.search(r"\b403\b", lower):
            return "http_403"
        if "rate limit" in lower or "ratelimit" in lower:
            return "rate_limit"
        if "reset" in lower:
            return "connection_reset"
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "text/html" in lower or "<html" in lower or "<!doctype html" in lower:
            return "html_error_page"
        return "unknown"

    @staticmethod
    def _low_connection_retry_reason(output_text: str) -> str:
        """Return low-connection retry trigger category, or empty string."""
        reason = Aria2Engine._classify_aria2_output_reason(output_text)
        if reason in {"http_429", "rate_limit", "connection_reset", "timeout"}:
            return reason
        return ""

    def _should_retry_low_connection(self, output_text: str) -> bool:
        """Return True when aria2 output suggests rate-limit or CDN connection friction."""
        return bool(self._low_connection_retry_reason(output_text))

    @staticmethod
    def _new_progress_stats() -> dict[str, object]:
        """Create a per-download aria2 progress statistics accumulator."""
        return {
            "speed_samples": 0,
            "speed_total_bps": 0.0,
            "peak_speed_bps": 0.0,
            "cn_min": None,
            "cn_max": None,
            "cn_last": None,
            "downloaded_last": "",
        }

    @staticmethod
    def _speed_to_bytes_per_second(speed_text: str) -> float:
        """Best-effort parser for aria2 speed strings such as ``1.2MiB/s``."""
        if not speed_text:
            return 0.0
        match = re.search(
            r"([0-9.]+)\s*([KMG]?i?B|[KMG]?B|B)(?:/s|ps)?",
            speed_text,
            re.IGNORECASE,
        )
        if not match:
            return 0.0
        try:
            value = float(match.group(1))
        except ValueError:
            return 0.0
        unit = match.group(2).lower()
        factors = {
            "b": 1,
            "kb": 1000,
            "mb": 1000 ** 2,
            "gb": 1000 ** 3,
            "kib": 1024,
            "mib": 1024 ** 2,
            "gib": 1024 ** 3,
        }
        return value * factors.get(unit, 1)

    @staticmethod
    def _format_bytes_per_second(value: float) -> str:
        """Format bytes/s for compact diagnostic logs."""
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = 0.0
        for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
            if amount < 1024 or unit == "GiB/s":
                if unit == "B/s":
                    return f"{amount:.0f}{unit}"
                return f"{amount:.2f}{unit}"
            amount /= 1024
        return "0B/s"

    def _update_progress_stats(self, line: str, progress_data: dict) -> None:
        """Sample CN/DL/SPD data for the eventual download summary."""
        tls = getattr(self, "_tls", None)
        stats = getattr(tls, "progress_stats", None) if tls is not None else None
        if not isinstance(stats, dict):
            return

        cn_match = re.search(r"CN:(\d+)", line, re.IGNORECASE)
        if cn_match:
            try:
                cn_value = int(cn_match.group(1))
            except ValueError:
                cn_value = None
            if cn_value is not None:
                stats["cn_last"] = cn_value
                stats["cn_min"] = cn_value if stats.get("cn_min") is None else min(int(stats["cn_min"]), cn_value)
                stats["cn_max"] = cn_value if stats.get("cn_max") is None else max(int(stats["cn_max"]), cn_value)

        speed_text = str(progress_data.get("speed", "") or "")
        speed_bps = self._speed_to_bytes_per_second(speed_text)
        if speed_bps > 0:
            stats["speed_samples"] = int(stats.get("speed_samples", 0)) + 1
            stats["speed_total_bps"] = float(stats.get("speed_total_bps", 0.0)) + speed_bps
            stats["peak_speed_bps"] = max(float(stats.get("peak_speed_bps", 0.0)), speed_bps)

        downloaded = str(progress_data.get("downloaded", "") or "")
        if downloaded:
            stats["downloaded_last"] = downloaded

    def _log_download_summary(
        self,
        task: DownloadTask,
        *,
        status: str,
        returncode: int | None,
        progress_stats: dict[str, object],
        low_connection_retries: int,
        last_error_category: str,
    ) -> None:
        """Log one non-sensitive aria2 result summary."""
        speed_samples = int(progress_stats.get("speed_samples", 0) or 0)
        total_bps = float(progress_stats.get("speed_total_bps", 0.0) or 0.0)
        avg_bps = total_bps / speed_samples if speed_samples else 0.0
        peak_bps = float(progress_stats.get("peak_speed_bps", 0.0) or 0.0)
        logger.info(
            "[Aria2] 下载摘要",
            event="aria2_download_summary",
            filename=str(getattr(task, "filename", "") or ""),
            status=status,
            returncode=returncode if returncode is not None else -1,
            avg_speed=self._format_bytes_per_second(avg_bps),
            peak_speed=self._format_bytes_per_second(peak_bps),
            speed_samples=speed_samples,
            cn_min=progress_stats.get("cn_min"),
            cn_max=progress_stats.get("cn_max"),
            cn_last=progress_stats.get("cn_last"),
            downloaded_last=progress_stats.get("downloaded_last", ""),
            low_connection_retry_count=low_connection_retries,
            last_error_category=last_error_category,
        )
    
    def parse_progress(self, line: str) -> dict:
        """
        解析进度输出
        示例: [#1 SIZE:123.45MiB/567.89MiB(21%) CN:16 DL:12.3MiB SPD:1.23MiB/s]
        """
        result = {'progress': 0.0, 'speed': '', 'downloaded': ''}
        
        # 匹配百分比
        progress_match = re.search(r'\((\d+)%\)', line)
        if progress_match:
            try:
                result['progress'] = float(progress_match.group(1))
            except ValueError:
                pass
        
        # 匹配速度 (SPD: 或 DL:)
        # Log 示例: DL:361KiB 或 SPD:1.23MiB/s
        speed_match = re.search(r'(?:SPD|DL):([0-9.]+[KMG]?i?B(?:/s)?)', line, re.IGNORECASE)
        if speed_match:
            speed_str = speed_match.group(1)
            # 如果缺少 /s 后缀，自动补全以便统一显示
            if not speed_str.endswith('/s') and not speed_str.endswith('ps'):
                speed_str += '/s'
            result['speed'] = speed_str
        
        # 匹配已下载大小
        # 优先匹配当前下载量: 15MiB/1.5GiB
        downloaded_match = re.search(r'([0-9.]+[KMG]?i?B)/[0-9.]+[KMG]?i?B', line, re.IGNORECASE)
        if downloaded_match:
            result['downloaded'] = downloaded_match.group(1)
        else:
            # 备用匹配 SIZE:
            size_match = re.search(r'SIZE:([0-9.]+[KMG]?i?B)', line, re.IGNORECASE)
            if size_match:
                result['downloaded'] = size_match.group(1)
        
        return result
