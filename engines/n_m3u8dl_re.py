"""
N_m3u8DL-RE engine wrapper for HLS (m3u8) video downloads.
"""

import re
import subprocess
from pathlib import Path

from core.task_model import DownloadTask
from engines.base_engine import BaseEngine
from utils.config_manager import config
from utils.logger import logger


class N_m3u8DL_RE_Engine(BaseEngine):
    """N_m3u8DL-RE 下载引擎（HLS/MPD）。"""

    _supported_options_cache: dict[str, set[str]] = {}
    _warned_unsupported_options: set[str] = set()

    @staticmethod
    def _log_failure(message: str, *, recoverable: bool, **kwargs):
        """Emit recoverable/non-recoverable failure logs with chosen level."""
        (logger.warning if recoverable else logger.error)(message, **kwargs)

    def can_handle(self, url: str) -> bool:
        """检测是否适合交给 N_m3u8DL-RE 处理。"""
        url_lower = url.lower()
        if ".m3u8" in url_lower:
            return True
        if ".urlset/" in url_lower or "index-f" in url_lower:
            return True
        if ".mpd" in url_lower:
            return True
        return False

    def get_name(self) -> str:
        return "N_m3u8DL-RE"

    def _load_supported_options(self) -> set[str]:
        """读取并缓存当前二进制支持的长参数。"""
        cache_key = str(self.binary_path).lower()
        cached = self._supported_options_cache.get(cache_key)
        if cached is not None:
            return cached

        options: set[str] = set()
        try:
            creation_flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation_flags = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                [self.binary_path, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=creation_flags,
                timeout=10,
            )
            help_text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            options.update(re.findall(r"--[a-z0-9][a-z0-9-]*", help_text))
        except Exception as e:
            logger.warning(f"[N_m3u8DL-RE] 读取 --help 失败，跳过参数探测: {e}")

        self._supported_options_cache[cache_key] = options
        return options

    def _warn_unsupported_option(self, option: str):
        """同一参数仅提示一次，避免刷屏。"""
        key = f"{str(self.binary_path).lower()}::{option.lower()}"
        if key in self._warned_unsupported_options:
            return
        self._warned_unsupported_options.add(key)
        logger.warning(f"[N_m3u8DL-RE] 当前版本不支持参数，已自动跳过: {option}")

    def download(self, task: DownloadTask, progress_callback) -> bool:
        """Execute download with master/media fallback chain."""
        try:
            logger.info(f"[N_m3u8DL-RE] 开始下载: {task.filename}")
            cookie_len = len(task.headers.get("cookie", "") or "")
            logger.info(
                f"[N_m3u8DL-RE] 请求头摘要: referer={task.headers.get('referer')} "
                f"origin={task.headers.get('origin')} ua={task.headers.get('user-agent')} "
                f"cookie_len={cookie_len}"
            )

            url_candidates = self._build_url_candidates(task)
            last_error = ""

            for index, (source_url, source_label, allow_select_video) in enumerate(url_candidates):
                # 引擎/source 级失败仍属于可恢复诊断信息；
                # 真正的最终失败由 DownloadManager 统一记为 error。
                recoverable = True
                cmd = self._build_command(
                    task,
                    source_url=source_url,
                    safe_mode=False,
                    allow_select_video=allow_select_video,
                )
                logger.info(
                    f"[N_m3u8DL-RE] 尝试地址: {source_label} -> {source_url}",
                    event="nm3u8dlre_source_try",
                )
                logger.info(f"[N_m3u8DL-RE] 完整命令: {' '.join(cmd)}")
                logger.info(f"[N_m3u8DL-RE] 参数列表: {cmd}")

                ok, tail_text = self._run_command(
                    task,
                    cmd,
                    progress_callback,
                    source_label,
                    recoverable=recoverable,
                )
                if ok:
                    logger.info(
                        f"[N_m3u8DL-RE] 下载完成: {task.filename}",
                        event="nm3u8dlre_source_ok",
                        source=source_label,
                    )
                    return True

                last_error = tail_text or last_error

                # Backward compatibility: if option parse failed, retry a safe command.
                if tail_text and ("show help" in tail_text.lower() or "usage information" in tail_text.lower()):
                    logger.warning("[N_m3u8DL-RE] 检测到参数解析失败，尝试安全模式命令")
                    safe_cmd = self._build_command(
                        task,
                        source_url=source_url,
                        safe_mode=True,
                        allow_select_video=allow_select_video,
                    )
                    logger.info(f"[N_m3u8DL-RE] 安全模式命令: {' '.join(safe_cmd)}")
                    ok_safe, tail_safe = self._run_command(
                        task,
                        safe_cmd,
                        progress_callback,
                        f"{source_label}-safe",
                        recoverable=recoverable,
                    )
                    if ok_safe:
                        logger.info(
                            f"[N_m3u8DL-RE] 安全模式下载完成: {task.filename}",
                            event="nm3u8dlre_source_ok",
                            source=f"{source_label}-safe",
                        )
                        return True
                    last_error = tail_safe or last_error

                if len(url_candidates) > 1:
                    logger.warning(
                        "[N_m3u8DL-RE] 当前地址下载失败，尝试下一个候选地址",
                        event="nm3u8dlre_source_failed",
                        source=source_label,
                    )

            task.error_message = last_error or task.error_message or "N_m3u8DL-RE all source urls failed"
            logger.warning(
                "[N_m3u8DL-RE] 建议检查 Referer/Cookie 或尝试切换引擎",
                event="nm3u8dlre_all_sources_failed",
            )
            return False
        except Exception as e:
            logger.error(f"[N_m3u8DL-RE] 下载异常: {e}")
            task.error_message = str(e)
            return False

    def _build_url_candidates(self, task: DownloadTask) -> list[tuple[str, str, bool]]:
        """Build primary/master/media fallback chain for one task."""
        candidates: list[tuple[str, str, bool]] = []
        seen = set()

        primary_url = (task.url or "").strip()
        master_url = (getattr(task, "master_url", None) or "").strip()
        media_url = (getattr(task, "media_url", None) or "").strip()

        def add(url: str, label: str, allow_select_video: bool):
            if not url or url in seen:
                return
            seen.add(url)
            candidates.append((url, label, allow_select_video))

        if primary_url:
            allow_primary_select = not media_url or primary_url != media_url
            add(primary_url, "primary", allow_primary_select)
        add(master_url, "master", True)
        add(media_url, "media", False)

        return candidates or [(task.url, "primary", True)]

    def _run_command(
        self,
        task: DownloadTask,
        cmd: list[str],
        progress_callback,
        source_label: str,
        recoverable: bool = False,
    ) -> tuple[bool, str]:
        """Run command once and return (ok, tail_text)."""
        creation_flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=creation_flags,
        )

        task.process = process
        output_lines = []
        stdout = process.stdout
        if stdout is not None:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                output_lines.append(line)
                if "%" in line or "B/s" in line or "iB/s" in line or "Kbps" in line or "Mbps" in line:
                    logger.info(f"[N_m3u8DL-RE RAW] {line}")
                progress_data = self.parse_progress(line)
                if progress_data["progress"] > 0 or progress_data["speed"]:
                    logger.debug(
                        f"[N_m3u8DL-RE PARSED] 进度={progress_data['progress']}% 速度={progress_data['speed']}"
                    )
                    progress_callback(progress_data)

        returncode = process.wait()
        if returncode == 0:
            return True, ""

        self._log_failure(
            f"[N_m3u8DL-RE] 下载失败: {task.filename}, 退出码: {returncode}",
            recoverable=recoverable,
            event="nm3u8dlre_exit_nonzero",
            source=source_label,
        )
        tail_text = ""
        if output_lines:
            tail_text = "\n".join(output_lines[-20:])
            self._log_failure(
                "[N_m3u8DL-RE] 输出尾部(20行):\n" + tail_text,
                recoverable=recoverable,
            )
        task.error_message = tail_text or f"N_m3u8DL-RE exit code: {returncode}"
        return False, tail_text

    def _build_command(
        self,
        task: DownloadTask,
        source_url: str | None = None,
        safe_mode: bool = False,
        allow_select_video: bool = True,
    ) -> list:
        """构建下载命令。"""
        thread_default = config.get("engines.n_m3u8dl_re.thread_count", 32)
        thread_min = config.get("engines.n_m3u8dl_re.thread_min", 4)
        thread_max = config.get("engines.n_m3u8dl_re.thread_max", 32)
        thread_count = self._auto_thread_count(task, thread_default, thread_min, thread_max)
        retry_count = config.get("engines.n_m3u8dl_re.retry_count", 10)
        max_retry = config.get("engines.n_m3u8dl_re.max_retry", retry_count)
        adaptive = config.get("engines.n_m3u8dl_re.adaptive", False)
        output_format = config.get("engines.n_m3u8dl_re.output_format", "mp4")
        speed_limit = config.get("speed_limit", 0)  # MB/s
        force_http1 = config.get("engines.n_m3u8dl_re.force_http1", False)
        no_date_info = config.get("engines.n_m3u8dl_re.no_date_info", False)

        temp_dir = Path(config.get("temp_dir")) / "n_m3u8dl"
        temp_dir.mkdir(parents=True, exist_ok=True)

        supported_options = self._load_supported_options()
        cmd = [
            self.binary_path,
            source_url or task.url,
            "--save-dir",
            task.save_dir,
            "--save-name",
            task.filename,
            "--tmp-dir",
            str(temp_dir),
            "--thread-count",
            str(thread_count),
            "--download-retry-count",
            str(retry_count),
        ]

        def append_option(flag: str, value: str | None = None):
            if flag.lower() not in supported_options:
                self._warn_unsupported_option(flag)
                return
            cmd.append(flag)
            if value is not None:
                cmd.append(value)

        if not safe_mode:
            append_option("--binary-merge")
            append_option("--del-after-done")
            append_option("--no-log")
            append_option("--resume")
        else:
            cmd = [
                self.binary_path,
                source_url or task.url,
                "--save-dir",
                task.save_dir,
                "--save-name",
                task.filename,
                "--tmp-dir",
                str(temp_dir),
                "--thread-count",
                str(thread_count),
                "--download-retry-count",
                str(retry_count),
            ]
            if "--no-log" in supported_options:
                cmd.append("--no-log")

        if force_http1:
            append_option("--force-http1")
        if no_date_info:
            append_option("--no-date-info")
        if adaptive:
            append_option("--adaptive")

        if max_retry is not None:
            try:
                max_retry_int = int(max_retry)
                if max_retry_int >= 0:
                    append_option("--max-retry", str(max_retry_int))
            except (TypeError, ValueError):
                logger.debug(f"[N_m3u8DL-RE] 忽略非法 max_retry 配置: {max_retry}")

        if allow_select_video and task.selected_variant and task.selected_variant.get("resolution"):
            resolution = task.selected_variant["resolution"]
            append_option("--select-video", f'res="{resolution}"')
            logger.info(f"[N_m3u8DL-RE] 使用指定分辨率: {resolution}")
        else:
            append_option("--auto-select")

        try:
            speed_limit = float(speed_limit)
        except (TypeError, ValueError):
            speed_limit = 0

        if speed_limit > 0:
            speed_mbps = speed_limit * 8
            limit_str = f"{int(speed_mbps)}M" if speed_mbps.is_integer() else f"{speed_mbps}M"
            logger.info(f"[N_m3u8DL-RE] 应用限速: {speed_limit} MB/s -> {limit_str}bps")
            append_option("--max-speed", limit_str)

        if output_format.lower() == "mp4":
            append_option("--mux-after-done", "format=mp4")

        if task.headers.get("user-agent"):
            cmd.extend(["-H", f'User-Agent: {task.headers["user-agent"]}'])
        if task.headers.get("referer"):
            cmd.extend(["-H", f'Referer: {task.headers["referer"]}'])
        if task.headers.get("origin"):
            cmd.extend(["-H", f'Origin: {task.headers["origin"]}'])
        if task.headers.get("cookie"):
            cmd.extend(["-H", f'Cookie: {task.headers["cookie"]}'])

        return cmd

    def _auto_thread_count(
        self, task: DownloadTask, default_value: int, min_value: int, max_value: int
    ) -> int:
        """根据分辨率自适应线程数。"""
        height = 0
        if task.selected_variant:
            height = int(task.selected_variant.get("height") or 0)
        if height >= 1080:
            return max(min_value, max_value)
        if height >= 720:
            return max(min_value, int((min_value + max_value) / 2))
        if height > 0:
            return max(min_value, int(min_value))
        return int(default_value)

    def _convert_speed_to_mbs(self, speed_str: str) -> str:
        """将 N_m3u8DL-RE 速度格式转换成更友好的 B/s、KB/s、M/s。"""
        try:
            match = re.search(r"(\d+\.?\d*)\s*([KMGT]?i?[Bb]ps)", speed_str, re.IGNORECASE)
            if not match:
                return speed_str

            value = float(match.group(1))
            unit = match.group(2).lower()

            if "k" in unit:
                bits = value * 1024
            elif "m" in unit:
                bits = value * 1024 * 1024
            elif "g" in unit:
                bits = value * 1024 * 1024 * 1024
            else:
                bits = value

            bytes_val = bits / 8
            if bytes_val >= 1024 * 1024:
                return f"{bytes_val / (1024 * 1024):.2f} M/s"
            if bytes_val >= 1024:
                return f"{bytes_val / 1024:.2f} KB/s"
            return f"{bytes_val:.2f} B/s"
        except Exception:
            return speed_str

    def parse_progress(self, line: str) -> dict:
        """解析进度输出。"""
        result = {"progress": 0.0, "speed": "", "downloaded": ""}

        progress_match = re.search(r"(\d+\.?\d*)\s*%", line)
        if progress_match:
            try:
                result["progress"] = float(progress_match.group(1))
            except ValueError:
                result["progress"] = 0.0

        # 优先在百分比之后找速度，避免匹配到前面的码率字段
        percent_pos = line.find("%")
        scopes = [line[percent_pos:]] if percent_pos > 0 else []
        scopes.append(line)

        for scope in scopes:
            speed_match = re.search(r"(\d+\.?\d*[KMG]?i?[Bb]ps)", scope)
            if not speed_match:
                continue
            speed_val = speed_match.group(1).strip()
            if speed_val.startswith("0.00") or speed_val.startswith("0Bps"):
                continue
            result["speed"] = self._convert_speed_to_mbs(speed_val)
            break

        return result
