"""
Streamlink engine wrapper for live streaming downloads
"""
import re
from pathlib import Path
from urllib.parse import quote as _urlquote
from engines.base_engine import BaseEngine, EngineResult
from core.task_model import DownloadTask
from utils.headers import normalized_forward_headers
from utils.logger import logger
from utils.proxy_config import proxy_for_url


class StreamlinkEngine(BaseEngine):
    """Streamlink 下载引擎 - 直播流专用"""
    
    # 支持的直播平台
    LIVE_PLATFORMS = [
        'twitch.tv',
        'douyu.com',
        'huya.com',
        'youtube.com/live',
        # 'youtube.com/watch',  # 移除：避免抢占 yt-dlp，普通视频由 yt-dlp 处理
        'bilibili.com/live',
        'afreecatv.com',
        'mixer.com',
        'facebook.com/live'
    ]
    
    def can_handle(self, url: str) -> bool:
        """检测是否为直播平台 URL"""
        return any(platform in url.lower() for platform in self.LIVE_PLATFORMS)
    
    def get_name(self) -> str:
        return "Streamlink"
    
    def _should_stop(self, task: DownloadTask) -> bool:
        """检查直播任务是否已收到暂停/取消/切换等停止信号。"""
        return bool(getattr(task, "stop_requested", False))

    def download(self, task: DownloadTask, progress_callback) -> bool:
        """执行直播流录制，失败时执行短暂重试与质量降级。"""
        try:
            last_output_lines: list[str] = []
            last_output_text = ""
            last_returncode = -1
            qualities = self._quality_fallback_chain(task)

            for quality_index, quality in enumerate(qualities):
                transient_retry_used = False
                attempt = 1

                while True:
                    if self._should_stop(task):
                        return False

                    result, output_lines = self._run_attempt(
                        task,
                        progress_callback,
                        quality=quality,
                        attempt=attempt,
                    )

                    if result.status in {"stopped", "switched", "paused"}:
                        return False

                    success = result.status == "ok"
                    returncode = result.returncode if result.returncode is not None else -1
                    output_text = "\n".join(output_lines)
                    lower_output = output_text.lower()

                    if success:
                        logger.info(
                            f"[Streamlink] 录制完成: {task.filename} quality={quality}"
                        )
                        return True

                    last_output_lines = output_lines
                    last_output_text = output_text
                    last_returncode = returncode
                    logger.warning(
                        f"[Streamlink] 录制尝试失败: {task.filename}, "
                        f"quality={quality}, attempt={attempt}, 退出码: {returncode}"
                    )

                    if (not transient_retry_used) and self._should_retry_transient(lower_output):
                        transient_retry_used = True
                        attempt += 1
                        logger.warning(
                            f"[Streamlink] 检测到临时网络/限流失败，短暂重试一次: "
                            f"quality={quality}",
                            event="streamlink_transient_retry",
                            quality=quality,
                            attempt=attempt,
                        )
                        continue

                    break

                if (
                    quality_index < len(qualities) - 1
                    and self._should_fallback_quality(last_output_text)
                ):
                    next_quality = qualities[quality_index + 1]
                    logger.warning(
                        f"[Streamlink] 当前清晰度可能不可用，切换质量重试: "
                        f"{quality} -> {next_quality}",
                        event="streamlink_quality_fallback",
                        from_quality=quality,
                        to_quality=next_quality,
                    )
                    continue

                break

            logger.error(
                f"[Streamlink] 录制失败: {task.filename}, 退出码: {last_returncode}"
            )
            tail_lines = last_output_lines[-20:] if last_output_lines else []
            if tail_lines:
                logger.error("[Streamlink] 输出尾部(20行):\n" + "\n".join(tail_lines))
                task.error_message = "\n".join(tail_lines)
            else:
                task.error_message = last_output_text[-1000:] if last_output_text else (
                    f"Streamlink exit code: {last_returncode}"
                )

            reason, suggestions = self._diagnose_failure(last_output_text)
            if reason:
                logger.warning(f"[Streamlink] 失败原因: {reason}")
            if suggestions:
                logger.warning("[Streamlink] 建议: " + "；".join(suggestions))
            return False
            
        except Exception as e:
            logger.error(f"[Streamlink] 录制异常: {e}")
            task.error_message = str(e)
            return False

    def _run_attempt(
        self,
        task: DownloadTask,
        progress_callback,
        *,
        quality: str,
        attempt: int,
    ) -> tuple[EngineResult, list[str]]:
        """Run one Streamlink subprocess attempt and return result plus output."""
        cmd = self._build_command(task, quality=quality)
        logger.info(
            f"[Streamlink] 开始录制直播: {task.filename} "
            f"quality={quality} attempt={attempt}"
        )
        self.log_command(cmd)

        # ``BaseEngine.spawn`` centralizes the CREATE_NO_WINDOW /
        # close_fds / byte-mode PIPE plumbing (task 29.1). Passing
        # ``sensitive=False`` avoids the duplicate ``log_command`` that
        # would otherwise re-emit the same redacted argv. ``read_loop``
        # drains stdout and stderr independently (task 9.2) so the old
        # ``stderr=STDOUT`` merge is unnecessary — ``_parse_line``
        # already accumulates lines regardless of stream tag for the
        # failure-diagnosis tail.
        process = self.spawn(cmd, sensitive=False)

        # ISS-30: bind process ownership metadata atomically so manager
        # kill paths can guard against pid reuse.
        with task.lock:
            task.process = process
            task._pid = getattr(process, "pid", None)
            task._expected_engine_name = self.get_name()
        output_lines: list[str] = []

        # ``_parse_line`` reads per-call state from the thread-local slot.
        self._tls.progress_callback = progress_callback
        self._tls.output_lines = output_lines
        try:
            result: EngineResult = self.read_loop(process, task, self._parse_line)
        finally:
            self._tls.progress_callback = None
            self._tls.output_lines = None

        return result, output_lines

    def _quality_fallback_chain(self, task: DownloadTask) -> list[str]:
        """Return deterministic Streamlink quality fallback order."""
        raw_quality = getattr(task, "streamlink_quality", "") or getattr(task, "quality", "")
        initial = self._sanitize_quality(str(raw_quality)) if raw_quality else "best"
        chain = [initial]
        for fallback in ("720p", "480p"):
            if fallback not in chain:
                chain.append(fallback)
        return chain

    @staticmethod
    def _sanitize_quality(quality: str) -> str:
        """Keep quality as a single safe argv token, falling back to best."""
        quality = (quality or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.+:-]+", quality):
            return quality
        return "best"

    @staticmethod
    def _is_terminal_auth_geo_drm_failure(output_text: str) -> bool:
        """Return True for failures where retrying another quality is not useful."""
        text = (output_text or "").lower()
        terminal_keywords = (
            "401",
            "unauthorized",
            "login",
            "log in",
            "sign in",
            "authentication required",
            "subscriber",
            "subscriber-only",
            "sub-only",
            "members-only",
            "members only",
            "geo",
            "geo-restricted",
            "not available in your country",
            "blocked in your country",
            "not available from your location",
            "drm",
            "widevine",
            "license server",
            "protected content",
            "no plugin can handle url",
            "no plugin",
            "unsupported url",
            "no streams found",
            "no playable streams",
            "could not find any playable streams",
            "stream is offline",
            "channel is offline",
            "currently offline",
            "livestream has ended",
        )
        return any(keyword in text for keyword in terminal_keywords)

    def _should_retry_transient(self, output_text: str) -> bool:
        """Return True for one same-quality retry on transient network failures."""
        text = (output_text or "").lower()
        if not text or self._is_terminal_auth_geo_drm_failure(text):
            return False
        transient_keywords = (
            "429",
            "too many requests",
            "rate limit",
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "temporarily unavailable",
            "temporary failure",
            "try again later",
            "read timed out",
            "incomplete read",
            "chunked encoding",
            "connection pool is full",
            "502",
            "503",
            "504",
        )
        return any(keyword in text for keyword in transient_keywords)

    def _should_fallback_quality(self, output_text: str) -> bool:
        """Return True when lowering live quality may bypass a bad variant/CDN."""
        text = (output_text or "").lower()
        if not text or self._is_terminal_auth_geo_drm_failure(text):
            return False
        quality_keywords = (
            "failed to open stream",
            "unable to open stream",
            "could not open stream",
            "error opening stream",
            "failed to read data from stream",
            "no data returned",
            "stream ended",
            "failed to fetch segment",
            "segment",
            "hlsplaylistreloadtimeouterror",
            "403",
            "forbidden",
            "404",
            "not found",
            "timeout",
            "timed out",
            "connection reset",
        )
        return any(keyword in text for keyword in quality_keywords)

    def _parse_line(self, stream_tag: str, text: str) -> None:
        """``read_loop`` callback: accumulate output and push progress events."""
        line = text.strip()
        if not line:
            return

        output_lines = getattr(self._tls, "output_lines", None)
        if output_lines is not None:
            output_lines.append(line)

        logger.debug(f"[Streamlink] {line}")
        progress_data = self.parse_progress(line)
        if progress_data['downloaded'] or progress_data['speed']:
            progress_callback = getattr(self._tls, "progress_callback", None)
            if progress_callback is not None:
                progress_callback(progress_data)
    
    def _diagnose_failure(self, output_text: str) -> tuple:
        """根据输出日志推断失败原因并给出建议"""
        if not output_text:
            return "无输出", ["检查网络连接或直播间是否可访问"]

        text = output_text.lower()
        suggestions = []
        reason = ""

        if "429" in text or "too many requests" in text or "rate limit" in text:
            reason = "429/限流（直播平台或 CDN 临时限制）"
            suggestions.extend(["稍后重试", "降低录制并发", "必要时更换网络出口"])
        elif any(keyword in text for keyword in ("drm", "widevine", "license server", "protected", "protected content")):
            reason = "DRM/订阅授权限制"
            suggestions.extend(["该直播可能不支持 Streamlink 录制", "确认账号/订阅权限"])
        elif any(keyword in text for keyword in (
            "401",
            "unauthorized",
            "login",
            "log in",
            "sign in",
            "authentication required",
            "subscriber",
            "subscriber-only",
            "sub-only",
            "members-only",
            "members only",
            "paid content",
        )):
            reason = "需要登录/账号权限不足"
            suggestions.extend(["导出 cookies 并放入 cookies 目录", "使用已登录账号", "确认账号具备观看权限"])
        elif "403" in text or "forbidden" in text:
            reason = "403/Forbidden（可能鉴权/防盗链）"
            suggestions.extend(["检查 Referer/UA 是否正确", "导出并配置 cookies", "尝试降低直播质量"])
        elif any(keyword in text for keyword in (
            "geo",
            "not available in your country",
            "blocked in your country",
            "not available from your location",
            "region restriction",
            "geo-restricted",
            "geo restricted",
        )):
            reason = "地理限制/地区不可用"
            suggestions.extend(["使用代理/VPN", "更换节点后重试"])
        elif any(keyword in text for keyword in (
            "no streams found",
            "no playable streams",
            "could not find any playable streams",
            "stream is offline",
            "channel is offline",
            "currently offline",
            "livestream has ended",
            "live has ended",
        )):
            reason = "未发现可用直播流或直播已下播"
            suggestions.extend(["确认直播间仍在直播", "刷新页面重新嗅探", "稍后重试"])
        elif "404" in text or "not found" in text:
            reason = "直播地址不存在或已下播"
            suggestions.extend(["确认直播间仍在直播", "刷新页面重新嗅探"])
        elif any(keyword in text for keyword in (
            "plugin error",
            "no plugin",
            "no plugin can handle url",
            "unsupported url",
            "unable to validate plugin",
        )):
            reason = "无法识别平台插件或 Streamlink 版本过旧"
            suggestions.extend(["更新 Streamlink", "确认链接为直播页面", "等待上游插件适配站点变更"])
        elif any(keyword in text for keyword in (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "temporarily unavailable",
            "temporary failure",
            "read timed out",
            "incomplete read",
            "chunked encoding",
        )):
            reason = "网络超时或连接被重置"
            suggestions.extend(["检查网络稳定性", "稍后重试", "降低直播质量"])
        elif any(keyword in text for keyword in (
            "failed to open stream",
            "unable to open stream",
            "could not open stream",
            "error opening stream",
            "failed to fetch segment",
        )):
            reason = "直播流打开失败（可能清晰度/CDN 节点异常）"
            suggestions.extend(["尝试降低直播质量", "稍后重试", "保留失败日志用于定位"])

        return reason, suggestions

    @staticmethod
    def build_cookie_args(cookie_str: str) -> list[str]:
        """Split a ``Cookie`` header into repeated ``--http-cookie`` pairs.

        Task 27.2 / Requirement 31.1-31.3: streamlink's ``--http-cookie`` flag
        accepts a single ``name=value`` cookie per occurrence. The browser
        cookie header may contain many pairs separated by ``;``. This helper
        splits them, URL-encodes each value with ``safe=''`` so any ``;``,
        ``=``, whitespace or non-ASCII byte inside a value survives the
        CLI round-trip, and emits ``["--http-cookie", f"{name}={value}"]``
        pairs.

        - Pieces missing ``=`` (including trailing empties) are discarded.
        - ``name`` is whitespace-stripped; empty-name pieces are discarded.
        - ``value`` is NOT stripped before encoding (cookie values may legally
          carry surrounding whitespace); trailing ``\\r\\n`` is quoted as well.
        - The return order preserves input order.
        - The raw cookie values never appear in engine logs: command logging
          goes through :meth:`BaseEngine.log_command`, which applies the R3
          redaction rules to ``--http-cookie name=value`` argv pairs.
        """

        if not cookie_str or not isinstance(cookie_str, str):
            return []
        args: list[str] = []
        for raw in cookie_str.split(";"):
            if "=" not in raw:
                continue
            name, _, value = raw.partition("=")
            name = name.strip()
            if not name:
                continue
            encoded = _urlquote(value, safe="")
            args.append("--http-cookie")
            args.append(f"{name}={encoded}")
        return args

    def _build_command(self, task: DownloadTask, quality: str | None = None) -> list:
        """构建录制命令"""
        # 直播流通常保存为 .ts 或 .flv 格式
        output_file = Path(task.save_dir) / f'{task.filename}.ts'
        if quality is None:
            raw_quality = getattr(task, "streamlink_quality", "") or getattr(task, "quality", "")
            quality = str(raw_quality) if raw_quality else "best"
        quality = self._sanitize_quality(quality)
        
        cmd = [
            self.binary_path,
            task.url,
            quality,
            '-o', str(output_file),
            '--force',  # 覆盖现有文件
        ]
        
        # 添加请求头
        safe_headers = normalized_forward_headers(
            task.headers,
            include_authorization=bool((task.headers or {}).get("_allow_authorization_header")),
        )
        if safe_headers.get('user-agent'):
            cmd.extend(['--http-header', f'User-Agent={safe_headers["user-agent"]}'])
        
        if safe_headers.get('cookie'):
            # Task 27.2: split the Cookie header on ``;`` and forward each
            # ``name=value`` pair as its own ``--http-cookie`` argument so
            # streamlink's CLI parser (which treats the value as a single
            # cookie) receives well-formed pairs. Values are percent-encoded
            # with ``safe=''`` to survive ``;`` / `` `` / `=`` inside values;
            # empty-name pieces and pieces without ``=`` are discarded.
            cmd.extend(self.build_cookie_args(safe_headers['cookie']))
        
        if safe_headers.get('referer'):
            cmd.extend(['--http-header', f'Referer={safe_headers["referer"]}'])
        if safe_headers.get('origin'):
            cmd.extend(['--http-header', f'Origin={safe_headers["origin"]}'])
        if safe_headers.get('accept'):
            cmd.extend(['--http-header', f'Accept={safe_headers["accept"]}'])
        if safe_headers.get('accept-language'):
            cmd.extend(['--http-header', f'Accept-Language={safe_headers["accept-language"]}'])
        if safe_headers.get('authorization'):
            cmd.extend(['--http-header', f'Authorization={safe_headers["authorization"]}'])

        proxy = proxy_for_url(task.url)
        if proxy:
            cmd.extend(['--http-proxy', proxy])
        
        return cmd
    
    def parse_progress(self, line: str) -> dict:
        """
        解析进度输出
        示例: [cli][info] Written 123.45 MB (1h 23m 45s @ 1.23 MB/s)
        """
        result = {'progress': 0.0, 'speed': '', 'downloaded': ''}
        
        # 匹配已下载大小和速度
        match = re.search(
            r'Written\s+([0-9.]+\s*[KMG]?B).*?@\s+([0-9.]+\s*[KMG]?B/s)',
            line,
            re.IGNORECASE
        )
        if match:
            result['downloaded'] = match.group(1)
            result['speed'] = match.group(2)
        
        # 直播流无确定进度，使用已下载大小表示进度
        # 这里设置为 -1 表示未知进度
        result['progress'] = -1
        
        return result
