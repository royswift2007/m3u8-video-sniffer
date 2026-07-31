"""
N_m3u8DL-RE engine wrapper for HLS (m3u8) video downloads.
"""

import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from core.task_model import DownloadTask
from engines.base_engine import BaseEngine, EngineResult
from utils.config_manager import config
from utils.headers import iter_engine_headers
from utils.logger import logger
from utils.proxy_config import proxy_for_url
from utils.redact import REDACTED, SENSITIVE_QUERY_KEYS, redact_url


class N_m3u8DL_RE_Engine(BaseEngine):
    """N_m3u8DL-RE 下载引擎（HLS/MPD）。"""

    _supported_options_cache: dict[str, set[str]] = {}
    _warned_unsupported_options: set[str] = set()
    _rate_limited_hosts: dict[str, float] = {}
    # These are legacy/compatibility flags that are intentionally attempted
    # only when a bundled or user-provided binary supports them.  Current
    # N_m3u8DL-RE 0.6.x builds no longer expose them, and a warning on every
    # download is noisy but not actionable because the command builder already
    # has supported alternatives (notably ``--download-retry-count``).
    _silent_unsupported_options: set[str] = {"--resume", "--max-retry"}

    # N_m3u8DL-RE refreshes progress with CR and, in 0.6.0-beta builds, can
    # concatenate timestamped log rows and progress rows without CR/LF.  Keep
    # these markers narrow so ETA values such as ``00:08:14`` are not mistaken
    # for log starts.
    _ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    _TIMESTAMP_LOG_START_RE = re.compile(
        r"(?=\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\s+"
        r"(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\s*:)",
        re.IGNORECASE,
    )
    _PROGRESS_ROW_START_RE = re.compile(
        r"(?=(?:Vid|Aud|Sub)\s+Kbps\s+-{3,}\s*"
        r"\d+/\d+\s+\d+(?:\.\d+)?\s*%)",
        re.IGNORECASE,
    )
    _URL_IN_TEXT_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    _SENSITIVE_KV_RE = re.compile(
        r"(?i)\b("
        + "|".join(re.escape(str(key)) for key in sorted(SENSITIVE_QUERY_KEYS, key=str))
        + r")\s*=\s*[^&\s]+"
    )

    def __init__(self, binary_path: str):
        super().__init__(binary_path)
        self.cookie_exporter = None
        self._low_concurrency_success_hosts: dict[str, float] = {}

    @staticmethod
    def _log_failure(message: str, *, recoverable: bool, **kwargs):
        """Emit recoverable/non-recoverable failure logs with chosen level."""
        (logger.warning if recoverable else logger.error)(message, **kwargs)

    @staticmethod
    def _safe_int(value: object, default: int) -> int:
        """Best-effort integer parser for runtime config values."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _has_cookie_header(headers: dict | None) -> bool:
        """Return True when a header bag already has a non-empty Cookie."""
        if not isinstance(headers, dict):
            return False
        return any(
            str(name).lower() == "cookie" and bool(value)
            for name, value in headers.items()
        )

    @staticmethod
    def _domain_matches(host: str, cookie_domain: str) -> bool:
        """Return whether a Netscape-cookie domain applies to host."""
        host = (host or "").lower().strip(".")
        domain = (cookie_domain or "").lower().strip().lstrip(".")
        if not host or not domain:
            return False
        return host == domain or host.endswith(f".{domain}")

    @staticmethod
    def _path_matches(request_path: str, cookie_path: str) -> bool:
        """Conservative RFC6265-style path match for cookie replay."""
        request_path = request_path or "/"
        cookie_path = cookie_path or "/"
        if not cookie_path.startswith("/"):
            cookie_path = "/"
        if cookie_path == "/":
            return True
        if request_path == cookie_path:
            return True
        return request_path.startswith(cookie_path.rstrip("/") + "/")

    @classmethod
    def _validate_cookie_file_path(cls, cookie_file: str) -> str:
        """Allow N_m3u8DL-RE to read only app-owned browser cookie files."""
        if not cookie_file or not isinstance(cookie_file, str):
            return ""
        try:
            candidate = Path(os.path.realpath(cookie_file))
        except (OSError, ValueError):
            logger.warning(
                "[N_m3u8DL-RE] cookie file path rejected (unresolvable)",
                event="nm3u8dlre_cookie_file_rejected",
                stage="cookie_header_prepare",
            )
            return ""

        try:
            from core.app_paths import get_data_root
            trusted_roots = [
                (get_data_root() / "cookies").resolve(),
                (Path.home() / ".m3u8d").resolve(),
            ]
        except (OSError, RuntimeError):
            trusted_roots = []

        for root in trusted_roots:
            try:
                candidate.relative_to(root)
                return str(candidate)
            except ValueError:
                continue

        logger.warning(
            "[N_m3u8DL-RE] cookie file outside trusted root; dropping",
            event="nm3u8dlre_cookie_file_rejected",
            stage="cookie_header_prepare",
            cookie_file=cookie_file,
        )
        return ""

    @classmethod
    def _cookie_header_from_netscape_file(cls, cookie_file: str, url: str) -> str:
        """Build a Cookie header for url from a Netscape-format cookie file."""
        if not cookie_file or not url:
            return ""
        try:
            parsed = urlparse(url)
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        request_path = parsed.path or "/"
        scheme = (parsed.scheme or "").lower()
        if not host:
            return ""

        pairs: dict[str, str] = {}
        now = int(time.time())
        try:
            with open(cookie_file, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        parts = line.split(None, 6)
                    if len(parts) < 7:
                        continue
                    domain, _flag, cookie_path, secure, expires, name, value = parts[:7]
                    name = name.strip()
                    if not name:
                        continue
                    if (secure or "").strip().upper() == "TRUE" and scheme != "https":
                        continue
                    if not cls._domain_matches(host, domain):
                        continue
                    if not cls._path_matches(request_path, cookie_path):
                        continue
                    try:
                        expires_value = int(float(str(expires or "0")))
                    except (TypeError, ValueError):
                        expires_value = 0
                    if expires_value > 0 and expires_value < now:
                        continue
                    pairs[name] = value
        except (OSError, ValueError) as exc:
            logger.debug(
                f"[N_m3u8DL-RE] 读取 Netscape cookie 文件失败: {exc}",
                event="nm3u8dlre_cookie_file_read_failed",
                stage="cookie_header_prepare",
                error_type=type(exc).__name__,
            )
            return ""

        return "; ".join(f"{name}={value}" for name, value in pairs.items())

    def _prepare_cookie_header(
        self,
        task: DownloadTask,
        source_url: str,
        *,
        cookie_file: str | None = None,
    ) -> bool:
        """Fill task.headers['cookie'] from Playwright-exported cookies when missing."""
        if not isinstance(task.headers, dict):
            task.headers = {}
        if self._has_cookie_header(task.headers):
            return False

        candidate_file = cookie_file or str(task.headers.get("_cookie_file") or "")
        if not candidate_file:
            exporter = getattr(self, "cookie_exporter", None)
            if callable(exporter):
                try:
                    candidate_file = exporter(source_url) or ""
                except Exception as exc:
                    logger.warning(
                        f"[N_m3u8DL-RE] 浏览器 Cookie 导出失败: {exc}",
                        event="nm3u8dlre_cookie_export_failed",
                        stage="cookie_header_prepare",
                        error_type=type(exc).__name__,
                    )
                    candidate_file = ""

        candidate_file = self._validate_cookie_file_path(candidate_file)
        if not candidate_file or not os.path.exists(candidate_file):
            return False

        cookie_header = self._cookie_header_from_netscape_file(candidate_file, source_url)
        if not cookie_header:
            logger.info(
                "[N_m3u8DL-RE] 浏览器 cookie 文件中没有匹配当前 HLS 域名的 cookie",
                event="nm3u8dlre_cookie_file_no_match",
                stage="cookie_header_prepare",
            )
            return False

        task.headers["cookie"] = cookie_header
        logger.info(
            "[N_m3u8DL-RE] 已从浏览器 cookies 补齐 Cookie header",
            event="nm3u8dlre_cookie_header_prepared",
            stage="cookie_header_prepare",
            cookie_len=len(cookie_header),
        )
        return True

    @classmethod
    def _redact_progress_line(cls, line: str) -> str:
        """Return a safe debug representation of one raw progress fragment."""
        text = cls._ANSI_ESCAPE_RE.sub("", str(line or ""))
        text = cls._URL_IN_TEXT_RE.sub(lambda match: redact_url(match.group(0)), text)
        return cls._SENSITIVE_KV_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", text)

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
            # Probe-only path: ``--help`` returns in <1s and we need the
            # captured text synchronously, so we keep ``subprocess.run``
            # here rather than routing through ``BaseEngine.spawn`` +
            # ``read_loop``. ``getattr`` keeps the one-liner portable to
            # non-Windows hosts where ``CREATE_NO_WINDOW`` is undefined
            # (task 29.2).
            result = subprocess.run(
                [self.binary_path, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
            help_text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            options.update(re.findall(r"--[a-z0-9][a-z0-9-]*", help_text))
        except Exception as e:
            logger.warning(f"[N_m3u8DL-RE] 读取 --help 失败，跳过参数探测: {e}")

        # ISS-34: when --help parsing yields zero options (e.g. the binary
        # changed its help format or is a prerelease with different flags),
        # log a warning so operators can correlate engine failures with the
        # option probe result. The empty set still gets cached to avoid
        # re-probing on every invocation, but the warning is emitted once
        # per binary path.
        if not options:
            logger.warning(
                "[N_m3u8DL-RE] --help 参数探测结果为空；"
                "所有可选参数将被跳过，仅保留基础命令。"
                "请检查 N_m3u8DL-RE 版本是否与引擎封装兼容。",
                event="nm3u8dlre_empty_options",
            )

        self._supported_options_cache[cache_key] = options
        return options

    def _probe_version(self) -> str | None:
        """Probe the N_m3u8DL-RE binary version via ``--version``.

        Returns the stripped version string (e.g. ``0.5.1``) or ``None``
        when the probe fails. This is a best-effort diagnostic helper; it
        does not cache and is only called when the engine encounters an
        option-parse failure so the version can be logged for correlation.
        """
        try:
            result = subprocess.run(
                [self.binary_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
            raw = (result.stdout or "").strip() or (result.stderr or "").strip()
            if raw:
                return raw
        except Exception:
            pass
        return None

    def _warn_unsupported_option(self, option: str):
        """同一参数仅提示一次，避免刷屏。"""
        option_normalized = option.lower()
        if option_normalized in self._silent_unsupported_options:
            return
        key = f"{str(self.binary_path).lower()}::{option_normalized}"
        if key in self._warned_unsupported_options:
            return
        self._warned_unsupported_options.add(key)
        logger.warning(f"[N_m3u8DL-RE] 当前版本不支持参数，已自动跳过: {option}")

    def download(self, task: DownloadTask, progress_callback) -> bool:
        """Execute download with master/media fallback chain."""
        try:
            logger.info(f"[N_m3u8DL-RE] 开始下载: {task.filename}")
            if not isinstance(task.headers, dict):
                task.headers = {}
            headers = task.headers
            cookie_len = len(headers.get("cookie", "") or "")
            referer = headers.get("referer") or ""
            origin = headers.get("origin") or ""
            user_agent = headers.get("user-agent") or ""
            logger.info(
                f"[N_m3u8DL-RE] 请求头摘要: has_referer={bool(referer)} "
                f"has_origin={bool(origin)} has_user_agent={bool(user_agent)} "
                f"ua_len={len(user_agent)} has_cookie={cookie_len > 0} "
                f"cookie_len={cookie_len}",
                event="nm3u8dlre_final_headers_summary",
                has_referer=bool(referer),
                has_origin=bool(origin),
                has_user_agent=bool(user_agent),
                has_cookie=cookie_len > 0,
                cookie_len=cookie_len,
            )

            url_candidates = self._build_url_candidates(task)
            last_error = ""
            low_concurrency_attempted: set[str] = set()

            for index, (source_url, source_label, allow_select_video) in enumerate(url_candidates):
                # 引擎/source 级失败仍属于可恢复诊断信息；
                # 真正的最终失败由 DownloadManager 统一记为 error。
                recoverable = True
                self._prepare_cookie_header(task, source_url)
                start_low_concurrency = self._should_start_low_concurrency(task, source_url)
                cmd = self._build_command(
                    task,
                    source_url=source_url,
                    safe_mode=False,
                    allow_select_video=allow_select_video,
                    thread_override=self._low_concurrency_thread_count() if start_low_concurrency else None,
                )
                logger.info(
                    f"[N_m3u8DL-RE] 尝试地址: {source_label} -> {redact_url(source_url)}",
                    event="nm3u8dlre_source_try",
                )
                self.log_command(cmd)
                if start_low_concurrency:
                    low_concurrency_attempted.add(source_url)

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
                    if start_low_concurrency:
                        self._remember_rate_limited_host(source_url, "initial_low_concurrency_success")
                    return True

                last_error = tail_text or last_error

                # Backward compatibility: if option parse failed, retry a safe command.
                if tail_text and ("show help" in tail_text.lower() or "usage information" in tail_text.lower()):
                    # ISS-34: log the N_m3u8DL-RE version so operators can
                    # correlate option-parse failures with a specific binary
                    # release (e.g. a prerelease that changed CLI flags).
                    engine_version = self._probe_version()
                    logger.warning(
                        "[N_m3u8DL-RE] 检测到参数解析失败，尝试安全模式命令",
                        event="nm3u8dlre_option_parse_failed",
                        engine_version=engine_version,
                    )
                    safe_cmd = self._build_command(
                        task,
                        source_url=source_url,
                        safe_mode=True,
                        allow_select_video=allow_select_video,
                    )
                    logger.info(f"[N_m3u8DL-RE] 安全模式命令")
                    self.log_command(safe_cmd, action="safe_mode")
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

                # Low-concurrency fallback after normal failure:
                # when the output hints at rate-limit / unstable CDN, retry
                # once with a configurable low thread count (default: 1).
                if (
                    tail_text
                    and self._low_concurrency_retry_enabled()
                    and self._should_retry_low_concurrency(tail_text)
                    and source_url not in low_concurrency_attempted
                ):
                    if getattr(task, "stop_requested", False):
                        break
                    low_concurrency_attempted.add(source_url)
                    low_thread_count = self._low_concurrency_thread_count()
                    logger.warning(
                        f"[N_m3u8DL-RE] 检测到限流/弱网特征，尝试低并发重试 (--thread-count {low_thread_count})",
                        event="nm3u8dlre_low_concurrency_retry",
                        source=source_label,
                        thread_count=low_thread_count,
                    )
                    low_cmd = self._build_command(
                        task,
                        source_url=source_url,
                        safe_mode=True,
                        allow_select_video=allow_select_video,
                        thread_override=low_thread_count,
                    )
                    self.log_command(low_cmd, action="low_concurrency")
                    ok_low, tail_low = self._run_command(
                        task,
                        low_cmd,
                        progress_callback,
                        f"{source_label}-lowcon",
                        recoverable=recoverable,
                    )
                    if ok_low:
                        logger.info(
                            f"[N_m3u8DL-RE] 低并发下载完成: {task.filename}",
                            event="nm3u8dlre_source_ok",
                            source=f"{source_label}-lowcon",
                            thread_count=low_thread_count,
                        )
                        self._remember_rate_limited_host(source_url, "low_concurrency_retry_success")
                        return True
                    logger.warning(
                        "[N_m3u8DL-RE] 低并发重试仍失败",
                        event="nm3u8dlre_low_concurrency_failed",
                        source=source_label,
                        thread_count=low_thread_count,
                    )
                    last_error = tail_low or last_error

                if len(url_candidates) > 1:
                    logger.warning(
                        "[N_m3u8DL-RE] 当前地址下载失败，尝试下一个候选地址",
                        event="nm3u8dlre_source_failed",
                        source=source_label,
                    )

            task.error_message = last_error or task.error_message or "N_m3u8DL-RE all source urls failed"
            if self._is_user_stop_disposition(task):
                logger.info(
                    "[N_m3u8DL-RE] 任务已停止/移除，跳过 all-sources-failed 鉴权提示",
                    event="nm3u8dlre_stopped_no_all_sources_failed",
                    stop_reason=getattr(task, "stop_reason", ""),
                )
                return False
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
        # ``BaseEngine.spawn`` (task 29.1) unifies CREATE_NO_WINDOW /
        # close_fds / byte-mode PIPE handling. The caller (``download`` /
        # safe-mode branch) already emitted a redacted argv via
        # ``self.log_command(cmd)`` before we were invoked, so we pass
        # ``sensitive=False`` to avoid duplicating that line. ``read_loop``
        # drains stdout and stderr on independent pump threads (task 9.2);
        # ``_parse_line`` is tag-agnostic — progress scraping and tail
        # accumulation both apply uniformly — so the legacy
        # ``stderr=STDOUT`` merge is redundant.
        process = self.spawn(cmd, sensitive=False)

        # ISS-30: bind process ownership metadata atomically so manager
        # kill paths can guard against pid reuse.
        with task.lock:
            task.process = process
            task._pid = getattr(process, "pid", None)
            task._expected_engine_name = self.get_name()

        # Per-call state consumed by the local read-loop callback. Do not rely
        # on ``threading.local`` here: N_m3u8DL-RE emits CR-refreshed progress
        # through BaseEngine's queue/drain path, and keeping the callback in a
        # closure guarantees parsed progress reaches DownloadManager even if
        # the read-loop implementation changes thread boundaries.
        output_lines: list[str] = []

        progress_state = {
            "total_segments": 0,
            "last_progress": float(getattr(task, "progress", 0.0) or 0.0),
        }
        progress_state_lock = threading.Lock()
        monitor_stop = threading.Event()

        def source_progress_callback(payload: dict) -> None:
            if progress_callback is None:
                return
            normalized = dict(payload or {})
            normalized.setdefault("source", source_label)
            normalized.setdefault("source_label", source_label)
            progress_callback(normalized)

        monitor_thread = self._start_temp_progress_monitor(
            task=task,
            progress_callback=source_progress_callback,
            temp_dir=self._extract_tmp_dir_from_cmd(cmd),
            state=progress_state,
            state_lock=progress_state_lock,
            stop_event=monitor_stop,
        )

        def on_engine_line(stream_tag: str, text: str) -> None:
            for fragment in self._split_progress_fragments(text):
                progress_data = self._parse_progress_fragment(
                    fragment,
                    progress_callback=source_progress_callback,
                    output_lines=output_lines,
                )
                self._update_progress_state_from_fragment(
                    fragment,
                    progress_data,
                    progress_state,
                    progress_state_lock,
                )

        try:
            result: EngineResult = self.read_loop(process, task, on_engine_line)
        finally:
            monitor_stop.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1.0)

        # Stop-request dispositions propagate to the caller as failure; the
        # DownloadManager inspects ``task.stop_reason`` to distinguish user
        # cancels from real errors, so we don't need to synthesize anything
        # here.
        if result.status in {"stopped", "switched", "paused"}:
            return False, ""

        if result.status == "ok":
            return True, ""

        returncode = result.returncode if result.returncode is not None else -1
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

    def _parse_line(self, stream_tag: str, text: str) -> None:
        """``read_loop`` callback: accumulate output and push progress events."""
        for fragment in self._split_progress_fragments(text):
            self._parse_progress_fragment(fragment)

    @classmethod
    def _split_progress_fragments(cls, text: str) -> list[str]:
        """Split N_m3u8DL-RE CR-refreshed/progress-concatenated output.

        N_m3u8DL-RE 0.5.1 generally emits one CR-delimited progress row at a
        time, while 0.6.0-beta can concatenate rows like
        ``...Start downloading...Vid Kbps ... 0.29%...00:06:00.807 INFO :`` in
        one pipe chunk.  The read pump already slices on CR/LF; this second
        pass separates timestamped log starts and repeated progress-row starts
        that arrive without any delimiter.
        """
        if text is None:
            return []

        normalized = cls._ANSI_ESCAPE_RE.sub("", str(text))
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        parts: list[str] = []

        for line in normalized.split("\n"):
            line = line.strip()
            if not line:
                continue

            boundaries = {0}
            for pattern in (cls._TIMESTAMP_LOG_START_RE, cls._PROGRESS_ROW_START_RE):
                for match in pattern.finditer(line):
                    boundaries.add(match.start())

            ordered = sorted(index for index in boundaries if 0 <= index < len(line))
            if not ordered:
                ordered = [0]
            if ordered[0] != 0:
                ordered.insert(0, 0)

            for index, start in enumerate(ordered):
                end = ordered[index + 1] if index + 1 < len(ordered) else len(line)
                fragment = line[start:end].strip()
                if fragment:
                    parts.append(fragment)

        return parts

    def _parse_progress_fragment(
        self,
        line: str,
        progress_callback=None,
        output_lines: list[str] | None = None,
    ) -> dict:
        """Accumulate one output fragment, push progress events, and return parsed data."""
        if output_lines is None:
            output_lines = getattr(self._tls, "output_lines", None)
        if output_lines is not None:
            output_lines.append(line)

        if os.environ.get("M3U8D_PROGRESS_DEBUG") == "1" and (
            "%" in line or "B/s" in line or "iB/s" in line or "Kbps" in line or "Mbps" in line
        ):
            logger.debug(f"[N_m3u8DL-RE RAW] {self._redact_progress_line(line)}")

        progress_data = self.parse_progress(line)
        if progress_data["progress"] > 0 or progress_data["speed"]:
            logger.debug(
                f"[N_m3u8DL-RE PARSED] 进度={progress_data['progress']}% 速度={progress_data['speed']}"
            )
            if progress_callback is None:
                progress_callback = getattr(self._tls, "progress_callback", None)
            if progress_callback is not None:
                try:
                    progress_callback(progress_data)
                    logger.debug(
                        "[N_m3u8DL-RE CALLBACK] progress callback delivered",
                        event="nm3u8dlre_progress_callback_delivered",
                        progress=progress_data.get("progress"),
                        speed_present=bool(progress_data.get("speed")),
                    )
                except Exception as exc:
                    logger.warning(
                        f"[N_m3u8DL-RE CALLBACK] progress callback failed: {exc}",
                        event="nm3u8dlre_progress_callback_failed",
                        progress=progress_data.get("progress"),
                        error_type=type(exc).__name__,
                    )
            else:
                logger.warning(
                    "[N_m3u8DL-RE CALLBACK] progress callback missing after parse",
                    event="nm3u8dlre_progress_callback_missing",
                    progress=progress_data.get("progress"),
                    speed_present=bool(progress_data.get("speed")),
                )
        return progress_data

    def _build_command(
        self,
        task: DownloadTask,
        source_url: str | None = None,
        safe_mode: bool = False,
        allow_select_video: bool = True,
        thread_override: int | None = None,
    ) -> list:
        """构建下载命令。

        Args:
            thread_override: 非 None 时覆盖自动计算的线程数（最低 1）。
                用于 low-concurrency fallback 场景。
        """
        thread_default = config.get("engines.n_m3u8dl_re.thread_count", 32)
        thread_min = config.get("engines.n_m3u8dl_re.thread_min", 4)
        thread_max = config.get("engines.n_m3u8dl_re.thread_max", 32)
        thread_count = self._auto_thread_count(task, thread_default, thread_min, thread_max)
        if thread_override is not None:
            thread_count = max(1, self._safe_int(thread_override, 1))
        retry_count = config.get("engines.n_m3u8dl_re.retry_count", 10)
        max_retry = config.get("engines.n_m3u8dl_re.max_retry", retry_count)
        adaptive = config.get("engines.n_m3u8dl_re.adaptive", False)
        output_format = config.get("engines.n_m3u8dl_re.output_format", "mp4")
        # Task 27.3: speed_limit unit is MB/s (mebibytes/s); a value of 0 means
        # "no limit" and is not forwarded as a flag.
        speed_limit = config.get("speed_limit", 0)
        force_http1 = config.get("engines.n_m3u8dl_re.force_http1", False)
        no_date_info = config.get("engines.n_m3u8dl_re.no_date_info", False)
        # 0.4.1 可实时刷新的路径没有强制 ANSI/控制台参数：它让
        # N_m3u8DL-RE 在 stdout/stderr=PIPE 的普通非交互模式下输出可解析
        # 进度。当前版本曾默认追加 ``--force-ansi-console`` +
        # ``--no-ansi-color``，这组新参数会改变 N_m3u8DL-RE 的进度刷新形态，
        # 在部分版本中表现为下载期间无实时日志/进度，退出时才集中吐出。
        # 因此默认回到旧版思路；仅在用户显式配置时再启用该实验参数。
        force_ansi_console = config.get("engines.n_m3u8dl_re.force_ansi_console", False)

        temp_dir = self._build_task_temp_dir(task)
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
            # ISS-33: safe mode only drops aggressive merge/resume flags.  Keep
            # the shared base command and append every cross-mode option below
            # (headers, speed limit, mux format, HTTP flags, selected variant)
            # so fallback downloads do not silently lose authentication or
            # throttling context.
            append_option("--no-log")

        if force_http1:
            append_option("--force-http1")
        if no_date_info:
            append_option("--no-date-info")
        if force_ansi_console:
            append_option("--force-ansi-console")
            append_option("--no-ansi-color")
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
            # F-04: the resolution originates from a parsed m3u8 master
            # playlist (an attacker-controlled blob) and is interpolated
            # into the N_m3u8DL-RE argv as ``res="<resolution>"``.
            # Although BaseEngine.spawn uses a parameter array (no shell),
            # a resolution containing ``"`` or control chars could still
            # break N_m3u8DL-RE's own parser or smuggle extra tokens.
            # Fail-closed: only allow the canonical ``WxH`` / ``WxH-``
            # shape; anything else is dropped + logged + falls back to
            # ``--auto-select``.
            if isinstance(resolution, str) and re.fullmatch(r"[0-9]+x[0-9]+(?:[-+][0-9]+)?", resolution):
                append_option("--select-video", f'res="{resolution}"')
                logger.info(f"[N_m3u8DL-RE] 使用指定分辨率: {resolution}")
            else:
                logger.warning(
                    "[N_m3u8DL-RE] variant resolution rejected (invalid chars), falling back to --auto-select",
                    event="nm3u8dlre_resolution_rejected",
                    stage="build_command",
                    resolution=resolution if isinstance(resolution, str) else type(resolution).__name__,
                )
                append_option("--auto-select")
        else:
            append_option("--auto-select")

        try:
            speed_limit = float(speed_limit)
        except (TypeError, ValueError):
            speed_limit = 0

        if speed_limit > 0:
            # ISS-31 / Task 27.3: unify speed-limit semantics across engines.
            # The UI value is interpreted as N MB/s (mebibytes/s), not Mbps.
            # N_m3u8DL-RE treats explicit byte-rate suffixes (``M`` / ``MB``)
            # as byte-scale limits, so we must not do the legacy ``* 8`` bit
            # conversion. Prefer the clearer newer ``--max-download-speed``
            # spelling when supported; otherwise use ``--max-speed`` with the
            # same mebibyte suffix.
            if float(speed_limit).is_integer():
                limit_value = str(int(speed_limit))
            else:
                limit_value = str(speed_limit)
            logger.info(
                f"[N_m3u8DL-RE] 应用限速: {speed_limit} MB/s -> {limit_value}M (mebibytes/s)"
            )
            if "--max-download-speed" in supported_options:
                append_option("--max-download-speed", f"{limit_value}MB")
            else:
                append_option("--max-speed", f"{limit_value}M")

        if output_format.lower() == "mp4":
            append_option("--mux-after-done", "format=mp4")

        self._append_headers(cmd, task.headers)

        proxy = proxy_for_url(source_url or task.url)
        if proxy:
            append_option("--proxy", proxy)

        return cmd

    def _append_headers(self, cmd: list[str], headers: dict) -> None:
        """Append safe HTTP headers as repeated ``-H`` arguments for N_m3u8DL-RE.

        Uses :func:`utils.headers.iter_engine_headers` to produce canonical
        ``(Name, value)`` pairs from the allowlisted + sanitized subset.
        """
        pairs = iter_engine_headers(
            headers,
            include_authorization=bool((headers or {}).get("_allow_authorization_header")),
        )
        cookie_len = 0
        for name, value in pairs:
            if name.lower() == "cookie":
                cookie_len = len(value or "")
            cmd.extend(["-H", f"{name}: {value}"])
        if cookie_len:
            logger.info(
                "[N_m3u8DL-RE] Cookie header 已转发给引擎",
                event="nm3u8dlre_cookie_forwarded",
                cookie_len=cookie_len,
                header_count=len(pairs),
            )
        else:
            logger.info(
                "[N_m3u8DL-RE] 未检测到 Cookie header，按无 Cookie 上下文启动引擎",
                event="nm3u8dlre_cookie_missing",
                header_count=len(pairs),
            )

    def _build_task_temp_dir(self, task: DownloadTask) -> Path:
        """Return a per-task temp directory to avoid same-URL retry collisions."""
        base_dir = Path(config.get("temp_dir")) / "n_m3u8dl"
        task_marker = getattr(task, "task_id", None) or getattr(task, "_idempotency_key", None) or str(id(task))
        digest = hashlib.sha1(f"{task.url}|{task.filename}|{task_marker}".encode("utf-8", errors="ignore")).hexdigest()[:12]
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", (task.filename or "task"))[:48].strip("._") or "task"
        return base_dir / f"{safe_stem}-{digest}"

    @staticmethod
    def _extract_tmp_dir_from_cmd(cmd: list[str]) -> Path | None:
        """Extract the ``--tmp-dir`` value from a built N_m3u8DL-RE command."""
        try:
            index = cmd.index("--tmp-dir")
        except ValueError:
            return None
        try:
            value = cmd[index + 1]
        except IndexError:
            return None
        if not value:
            return None
        return Path(str(value))

    @staticmethod
    def _extract_total_segments_from_text(text: str) -> int:
        """Extract total segment count from a N_m3u8DL-RE progress/log fragment."""
        text = text or ""
        progress_row_total = re.search(r"\b\d+\s*/\s*(\d+)\b", text)
        if progress_row_total:
            try:
                return int(progress_row_total.group(1))
            except ValueError:
                return 0

        segments_total = re.search(r"\b(\d+)\s+Segments\b", text, re.IGNORECASE)
        if segments_total:
            try:
                return int(segments_total.group(1))
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _update_progress_state_from_fragment(
        fragment: str,
        progress_data: dict,
        state: dict,
        state_lock: threading.Lock,
    ) -> None:
        """Track segment totals and the latest real engine progress."""
        text = fragment or ""
        total = N_m3u8DL_RE_Engine._extract_total_segments_from_text(text)

        try:
            progress = float((progress_data or {}).get("progress") or 0.0)
        except (TypeError, ValueError):
            progress = 0.0

        with state_lock:
            if total > 0:
                state["total_segments"] = max(int(state.get("total_segments") or 0), total)
            if progress > 0:
                state["last_progress"] = max(float(state.get("last_progress") or 0.0), progress)

    def _start_temp_progress_monitor(
        self,
        *,
        task: DownloadTask,
        progress_callback,
        temp_dir: Path | None,
        state: dict,
        state_lock: threading.Lock,
        stop_event: threading.Event,
        interval: float | None = None,
    ) -> threading.Thread | None:
        """Start a fallback monitor that derives progress from temp segment files.

        Some N_m3u8DL-RE builds buffer stdout/stderr for long periods even while
        segment files are being downloaded.  This monitor is intentionally a
        fallback: it only emits when a segment total has been learned from real
        engine output and temp-file progress advances beyond the last parsed
        engine progress.
        """
        if progress_callback is None or temp_dir is None:
            return None
        if not bool(config.get("engines.n_m3u8dl_re.temp_progress_fallback", True)):
            return None
        try:
            poll_interval = float(
                interval
                if interval is not None
                else config.get("engines.n_m3u8dl_re.temp_progress_interval", 1.0)
            )
        except (TypeError, ValueError):
            poll_interval = 1.0
        poll_interval = max(0.2, poll_interval)
        unknown_total_enabled = bool(
            config.get("engines.n_m3u8dl_re.temp_progress_unknown_total_enabled", True)
        )

        def _monitor() -> None:
            last_count = -1
            last_total = 0
            while not stop_event.wait(poll_interval):
                if getattr(task, "stop_requested", False):
                    return
                with state_lock:
                    total = int(state.get("total_segments") or 0)
                    last_progress = float(state.get("last_progress") or 0.0)

                downloaded_count = self._estimate_downloaded_segments(temp_dir)
                if downloaded_count <= 0:
                    continue
                if downloaded_count == last_count and total == last_total:
                    continue

                if total <= 0:
                    if not unknown_total_enabled:
                        continue
                    payload = {
                        "progress": -1.0,
                        "speed": "",
                        "downloaded": f"{downloaded_count} segments",
                    }
                else:
                    progress = min(99.0, max(0.0, (downloaded_count / total) * 100.0))
                    if progress <= last_progress + 0.05:
                        last_count = downloaded_count
                        last_total = total
                        continue
                    payload = {
                        "progress": round(progress, 2),
                        "speed": "",
                        "downloaded": f"{downloaded_count}/{total} segments",
                    }

                last_count = downloaded_count
                last_total = total
                try:
                    progress_callback(payload)
                    with state_lock:
                        payload_progress = float(payload["progress"])
                        if payload_progress >= 0:
                            state["last_progress"] = max(
                                float(state.get("last_progress") or 0.0),
                                payload_progress,
                            )
                    logger.debug(
                        "[N_m3u8DL-RE FALLBACK] temp segment progress callback delivered",
                        event="nm3u8dlre_temp_progress_callback_delivered",
                        filename=getattr(task, "filename", ""),
                        progress=payload["progress"],
                        downloaded=payload["downloaded"],
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        f"[N_m3u8DL-RE FALLBACK] temp progress callback failed: {exc}",
                        event="nm3u8dlre_temp_progress_callback_failed",
                        filename=getattr(task, "filename", ""),
                        error_type=type(exc).__name__,
                    )

        thread = threading.Thread(
            target=_monitor,
            daemon=True,
            name="n_m3u8dl_re.temp_progress_monitor",
        )
        thread.start()
        return thread

    @staticmethod
    def _estimate_downloaded_segments(temp_dir: Path | None) -> int:
        """Best-effort count of downloaded media segment files in N_m3u8DL temp dir."""
        if temp_dir is None:
            return 0
        try:
            root = Path(temp_dir)
            if not root.exists():
                return 0
            count = 0
            ignored_suffixes = {".json", ".log", ".txt", ".aria2", ".tmp"}
            ignored_names = {"meta.json", "raw.json", "mediainfo.json"}
            for path in root.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    name = path.name.lower()
                    if name in ignored_names or path.suffix.lower() in ignored_suffixes:
                        continue
                    if path.stat().st_size <= 0:
                        continue
                    count += 1
                except OSError:
                    continue
            return count
        except OSError:
            return 0

    @staticmethod
    def _is_user_stop_disposition(task: DownloadTask) -> bool:
        """True when a failure is caused by user/system stop rather than source/auth failure."""
        reason = getattr(task, "stop_reason", "") or ""
        reason_value = getattr(reason, "value", str(reason))
        return bool(getattr(task, "stop_requested", False)) or reason_value in {
            "removed",
            "shutdown",
            "cancelled",
            "canceled",
            "paused",
            "engine_switch",
        }

    def _should_retry_low_concurrency(self, tail_text: str) -> bool:
        """Return True when output suggests rate-limit or unstable CDN."""
        if not tail_text:
            return False
        lower = tail_text.lower()
        terminal_keywords = (
            "drm",
            "widevine",
            "fairplay",
            "playready",
            "unsupported",
            "invalid url",
            "404",
            "not found",
            "no such host",
            "name resolution",
        )
        if any(kw in lower for kw in terminal_keywords):
            return False
        if "403" in lower and any(
            kw in lower
            for kw in ("segment", "fragment", ".ts", ".m4s", "retry")
        ):
            return True
        keywords = (
            "429",
            "too many requests",
            "too many",
            "rate limit",
            "ratelimit",
            "throttled",
            "connection reset",
            "reset by peer",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "segment retry exhausted",
            "download speed too low",
            "failed to read",
        )
        return any(kw in lower for kw in keywords)

    @classmethod
    def _remember_rate_limited_host(cls, url: str, reason: str = "") -> None:
        """Remember that a host benefited from low-concurrency recovery."""
        try:
            host = (urlparse(url or "").hostname or "").lower()
        except ValueError:
            host = ""
        if not host:
            return
        cls._rate_limited_hosts[host] = time.time()
        logger.info(
            "[N_m3u8DL-RE] 已记录 host 低并发成功状态",
            event="nm3u8dlre_rate_limited_host_remembered",
            host=host,
            reason=reason,
        )

    @classmethod
    def _host_has_recent_rate_limit(cls, url: str) -> bool:
        """Return whether url host recently succeeded via low concurrency."""
        try:
            host = (urlparse(url or "").hostname or "").lower()
        except ValueError:
            return False
        if not host:
            return False
        now = time.time()
        ttl = max(
            60,
            cls._safe_int(
                config.get("engines.n_m3u8dl_re.rate_limited_host_ttl_seconds", 3600),
                3600,
            ),
        )
        stale = [saved_host for saved_host, ts in cls._rate_limited_hosts.items() if now - ts > ttl]
        for saved_host in stale:
            cls._rate_limited_hosts.pop(saved_host, None)
        return bool(host in cls._rate_limited_hosts)

    @staticmethod
    def _low_concurrency_retry_enabled() -> bool:
        """Return whether engine-level low-concurrency recovery is enabled."""
        features = config.get("features", {}) or {}
        return bool(features.get("rate_limit_low_concurrency_enabled", True))

    @staticmethod
    def _low_concurrency_thread_count() -> int:
        """Return configured low-concurrency retry thread count."""
        value = N_m3u8DL_RE_Engine._safe_int(
            config.get("engines.n_m3u8dl_re.low_concurrency_thread_count", 1),
            1,
        )
        return max(1, min(value, 4))

    @staticmethod
    def _task_has_rate_limit_hint(task: DownloadTask) -> bool:
        """Return True when prior probe/download diagnostics suggest CDN rate limiting."""
        if bool(getattr(task, "rate_limit_retry_hint", False)):
            return True
        probe_result = getattr(task, "probe_result", None)
        if not isinstance(probe_result, dict):
            return False
        if str(probe_result.get("status_code")) == "429":
            return True
        diagnostics = probe_result.get("stage_diagnostics", [])
        if not isinstance(diagnostics, list):
            return False
        return any(isinstance(item, dict) and str(item.get("status_code")) == "429" for item in diagnostics)

    def _should_start_low_concurrency(self, task: DownloadTask, source_url: str) -> bool:
        """Return True when this host recently required low-concurrency recovery."""
        if not self._low_concurrency_retry_enabled():
            return False
        features = config.get("features", {}) or {}
        if not bool(features.get("adaptive_low_concurrency_enabled", True)):
            return False
        if self._task_has_rate_limit_hint(task):
            return True
        return self._host_has_recent_rate_limit(source_url)

    def _auto_thread_count(
        self, task: DownloadTask, default_value: int, min_value: int, max_value: int
    ) -> int:
        """根据分辨率、探测结果和限流历史自适应线程数。"""
        safe_min = max(1, self._safe_int(min_value, 1))
        safe_max = max(safe_min, self._safe_int(max_value, max(safe_min, 1)))
        if self._low_concurrency_retry_enabled() and self._task_has_rate_limit_hint(task):
            return min(safe_max, self._low_concurrency_thread_count())
        if self._low_concurrency_retry_enabled() and self._host_has_recent_rate_limit(getattr(task, "url", "")):
            return min(safe_max, self._low_concurrency_thread_count())

        height = 0
        if task.selected_variant:
            height = int(task.selected_variant.get("height") or 0)
        if height >= 1080:
            return safe_max
        if height >= 720:
            return max(safe_min, int((safe_min + safe_max) / 2))
        if height > 0:
            return safe_min
        try:
            default_threads = int(default_value)
        except (TypeError, ValueError):
            default_threads = safe_min
        return min(safe_max, max(safe_min, default_threads))

    def _convert_speed_to_mbs(self, speed_str: str) -> str:
        """将 N_m3u8DL-RE 速度格式转换成更友好的 B/s、KB/s、M/s。"""
        try:
            raw = str(speed_str).strip()
            # N_m3u8DL-RE 0.6.0-beta can emit negative-looking Kbps tokens in
            # glued progress rows.  They are not reliable speeds; keep the raw
            # token instead of converting it into a misleading positive value.
            if raw.startswith("-"):
                return raw

            match = re.search(r"(\d+\.?\d*)\s*([KMGT]?i?[Bb]ps)", raw, re.IGNORECASE)
            if not match:
                return raw

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
            speed_match = re.search(r"(-?\d+\.?\d*[KMG]?i?[Bb]ps)", scope)
            if not speed_match:
                continue
            speed_val = speed_match.group(1).strip()
            if speed_val.startswith("0.00") or speed_val.startswith("0Bps"):
                continue
            result["speed"] = self._convert_speed_to_mbs(speed_val)
            break

        size_match = re.search(
            r"([0-9.]+\s*[KMGT]?i?B)\s*/\s*([0-9.]+\s*[KMGT]?i?B)",
            line,
            re.IGNORECASE,
        )
        if size_match:
            result["downloaded"] = f"{size_match.group(1)}/{size_match.group(2)}"
        else:
            seg_match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", line)
            if seg_match:
                result["downloaded"] = f"{seg_match.group(1)}/{seg_match.group(2)} segments"

        return result
