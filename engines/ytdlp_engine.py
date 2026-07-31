"""
yt-dlp engine wrapper for universal video downloads
"""
import os
import subprocess
import re
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

from engines.base_engine import BaseEngine, EngineResult
from core.app_paths import get_data_root
from core.playwright_profile import get_primary_user_data_dir
from core.task_model import DownloadTask
from utils.headers import normalized_forward_headers
from utils.logger import logger
from utils.proxy_config import proxy_for_url


# ---------------------------------------------------------------------------
# cookies 域名 → cookies 文件主域 的映射（Requirement 36.1 / 36.2 / 36.3）
#
# - 精确键(如 ``www.youtube.com``)用于命中特定主机；
# - 通配键(``*.youtube.com``)用于匹配该主域下未显式列出的任意子域,
#   与 Requirement 36.2 "可扩展至 *.youtube.com / *.bilibili.com, 配置化"
#   保持一致。
#
# 该常量暴露为模块级,后续可由配置文件或测试通过 ``override_map`` 覆盖。
# ---------------------------------------------------------------------------
COOKIE_DOMAIN_MAP: dict[str, str] = {
    # YouTube
    'youtube.com': 'www.youtube.com',
    'www.youtube.com': 'www.youtube.com',
    'youtu.be': 'www.youtube.com',
    'm.youtube.com': 'www.youtube.com',
    '*.youtube.com': 'www.youtube.com',

    # Bilibili
    'bilibili.com': 'www.bilibili.com',
    'www.bilibili.com': 'www.bilibili.com',
    '*.bilibili.com': 'www.bilibili.com',

    # TikTok
    'tiktok.com': 'www.tiktok.com',
    'www.tiktok.com': 'www.tiktok.com',
    '*.tiktok.com': 'www.tiktok.com',

    # Twitter / X
    'twitter.com': 'www.x.com',
    'x.com': 'www.x.com',
    'www.twitter.com': 'www.x.com',
    'www.x.com': 'www.x.com',
    '*.twitter.com': 'www.x.com',
    '*.x.com': 'www.x.com',

    # Instagram
    'instagram.com': 'www.instagram.com',
    'www.instagram.com': 'www.instagram.com',
    '*.instagram.com': 'www.instagram.com',
    # Common page/video sites where users may explicitly export cookies.
    'vimeo.com': 'vimeo.com',
    'www.vimeo.com': 'vimeo.com',
    '*.vimeo.com': 'vimeo.com',
    'dailymotion.com': 'www.dailymotion.com',
    'www.dailymotion.com': 'www.dailymotion.com',
    '*.dailymotion.com': 'www.dailymotion.com',
    'twitch.tv': 'www.twitch.tv',
    'www.twitch.tv': 'www.twitch.tv',
    '*.twitch.tv': 'www.twitch.tv',
    'facebook.com': 'www.facebook.com',
    'www.facebook.com': 'www.facebook.com',
    'fb.watch': 'www.facebook.com',
    '*.facebook.com': 'www.facebook.com',
    'douyin.com': 'www.douyin.com',
    'www.douyin.com': 'www.douyin.com',
    '*.douyin.com': 'www.douyin.com',
    'kuaishou.com': 'www.kuaishou.com',
    'www.kuaishou.com': 'www.kuaishou.com',
    '*.kuaishou.com': 'www.kuaishou.com',
}


def _normalize_host(url: str) -> str:
    """从 URL 中解析出 host（小写、去端口）。失败返回空串。"""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception as e:  # pragma: no cover - urlparse extremely rarely raises
        logger.debug(f"[yt-dlp] URL 解析失败: url={url} error={e}")
        return ""
    if not netloc:
        return ""
    # 去掉用户信息(user:pass@)与端口
    if '@' in netloc:
        netloc = netloc.rsplit('@', 1)[1]
    if ':' in netloc:
        netloc = netloc.split(':', 1)[0]
    return netloc


def _lookup_target_domain(
    host: str,
    mapping: Mapping[str, str],
) -> Optional[str]:
    """按 Requirement 36 的规则在映射中查找目标 cookie 主域。

    规则:
      1. 精确匹配优先(host 直接命中映射键)。
      2. 否则按 ``.`` 边界切分 host, 从左向右依次剥离前缀,
         得到逐步缩短的后缀;对每个后缀依次检查:
           a. ``<suffix>`` 精确键
           b. ``*.<suffix>`` 通配键
         任一命中即返回对应主域。
      3. 后缀最短保留 2 段(避免匹配到单一 TLD 如 ``com``)。
    """
    if not host:
        return None

    # 1. 精确匹配
    if host in mapping:
        return mapping[host]
    # 同一 host 同时接受 *.host 写法的精确命中(罕见但配置上合法)
    exact_wild = f"*.{host}"
    if exact_wild in mapping:
        return mapping[exact_wild]

    # 2. 后缀匹配
    parts = host.split('.')
    # i 从 1 开始(剥离最左一段);suffix 最短保留 2 段
    for i in range(1, len(parts) - 1):
        suffix = '.'.join(parts[i:])
        if suffix in mapping:
            return mapping[suffix]
        wild = f"*.{suffix}"
        if wild in mapping:
            return mapping[wild]
    return None


def resolve_cookie_file(
    url: str,
    *,
    cookies_base_path: Optional[str] = None,
    override_map: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """根据 URL 解析对应的 cookies 文件路径(Requirement 36)。

    Args:
        url: 目标视频页 URL。
        cookies_base_path: 可选;cookies 目录。默认取
            ``YtdlpEngine.get_cookies_base_path()``。
        override_map: 可选;替换默认的 ``COOKIE_DOMAIN_MAP``。
            便于配置化覆盖与测试注入(Requirement 36.2)。

    Returns:
        映射命中时返回 ``<base>/<target_domain>_cookies.txt``;
        未命中返回 ``None``。注意返回的路径不保证文件存在 ——
        调用方仍需用 ``os.path.exists`` 校验。
    """
    host = _normalize_host(url)
    if not host:
        return None

    mapping = override_map if override_map is not None else COOKIE_DOMAIN_MAP
    target = _lookup_target_domain(host, mapping)
    if not target:
        return None

    base = cookies_base_path if cookies_base_path is not None \
        else YtdlpEngine.get_cookies_base_path()
    return os.path.join(base, f"{target}_cookies.txt")


class YtdlpEngine(BaseEngine):
    """yt-dlp 下载引擎 - 通用视频"""
    
    # cookies 文件基础目录（缓存）
    _cookies_base_path = None

    def _should_stop(self, task: DownloadTask) -> bool:
        """检查任务是否已收到停止/暂停/删除信号。"""
        return bool(getattr(task, "stop_requested", False))

    def _mark_stopped(self, task: DownloadTask):
        """统一设置停止类任务的错误信息，便于状态机收敛。"""
        stop_reason = getattr(task, "stop_reason", "")
        if stop_reason == "paused":
            task.error_message = "用户暂停"
        elif stop_reason == "cancelled":
            task.error_message = "用户取消"
        elif stop_reason == "removed":
            task.error_message = "用户删除任务"
        elif stop_reason == "shutdown":
            task.error_message = "应用关闭"

    # Note: explicit process-kill helper removed in task 9.2 migration —
    # ``BaseEngine.read_loop`` now owns the terminate→kill escalation.
    
    @classmethod
    def get_cookies_base_path(cls) -> str:
        """获取 cookies 文件所在目录"""
        if cls._cookies_base_path is None:
            import os
            # 放在程序目录下的 cookies 子目录
            base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cls._cookies_base_path = os.path.join(base_path, "cookies")
            # 确保目录存在
            os.makedirs(cls._cookies_base_path, exist_ok=True)
        return cls._cookies_base_path
    
    @classmethod
    def get_cookies_file_for_url(cls, url: str) -> str:
        """根据 URL 获取对应的 cookies 文件路径(Requirement 36)。

        支持精确匹配与后缀/通配匹配,具体规则见
        :func:`resolve_cookie_file`。若未命中任何映射键,
        返回空串以保持历史调用方契约。

        支持的网站(默认映射)：
        - YouTube: www.youtube.com_cookies.txt
        - Bilibili: www.bilibili.com_cookies.txt
        - TikTok: www.tiktok.com_cookies.txt
        - Twitter/X: www.x.com_cookies.txt
        - Instagram: www.instagram.com_cookies.txt
        """
        path = resolve_cookie_file(url, cookies_base_path=cls.get_cookies_base_path())
        return path or ""
    
    @classmethod
    def get_youtube_cookies_file(cls) -> str:
        """获取 YouTube cookies 文件路径（向后兼容）"""
        import os
        return os.path.join(cls.get_cookies_base_path(), "www.youtube.com_cookies.txt")

    def _is_youtube_url(self, url: str) -> bool:
        """Return True for YouTube / youtu.be URLs."""
        host = ""
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return False
        host = host.lower()
        return (
            host == "youtu.be"
            or host.endswith("youtube.com")
            or host.endswith("youtube-nocookie.com")
        )

    @staticmethod
    def _browser_cookie_sources() -> list[str]:
        """Return browser-cookie sources ordered by reliability for retries."""
        sources: list[str] = []
        try:
            profile = get_primary_user_data_dir()
            if profile.exists():
                sources.append(f"chromium:{profile}")
        except (OSError, RuntimeError) as exc:
            logger.debug(
                f"[yt-dlp] Playwright Chromium profile 检测失败: {exc}",
                event="ytdlp_chromium_profile_probe_failed",
                stage="browser_cookie_sources",
                error_type=type(exc).__name__,
            )
        sources.extend(["chrome", "firefox"])
        deduped: list[str] = []
        for source in sources:
            if source and source not in deduped:
                deduped.append(source)
        return deduped

    def _resolve_browser_cookie_source(self, use_browser_cookies) -> str:
        """Map retry mode to a yt-dlp --cookies-from-browser source string."""
        if not use_browser_cookies:
            return ""
        if isinstance(use_browser_cookies, str) and use_browser_cookies.startswith("chromium:"):
            return use_browser_cookies
        if use_browser_cookies == "chromium":
            sources = self._browser_cookie_sources()
            for source in sources:
                if source.startswith("chromium:"):
                    return source
            return "chrome"
        if use_browser_cookies == "firefox":
            return "firefox"
        if isinstance(use_browser_cookies, str):
            return use_browser_cookies
        return ""

    def _build_browser_cookie_retry_plan(self) -> list[str]:
        """Return retry cookie sources after removing duplicates."""
        return self._browser_cookie_sources()

    def _try_browser_cookie_retries(
        self,
        task: DownloadTask,
        progress_callback,
        *,
        reason: str,
        generic_impersonate: bool = False,
    ) -> tuple[bool, bool]:
        """Try yt-dlp with real browser cookies for bot/auth failures."""
        need_login = False
        task.headers.pop('_cookie_file', None)
        for source in self._build_browser_cookie_retry_plan():
            if self._should_stop(task):
                self._mark_stopped(task)
                return False, need_login
            suffix = " + generic impersonate" if generic_impersonate else ""
            logger.info(f"[yt-dlp] 尝试使用浏览器 cookies 作为备用: {source}{suffix} ({reason})")
            success, source_need_login = self._do_download(
                task,
                progress_callback,
                use_browser_cookies=source,
                allow_insecure_tls=False,
                generic_impersonate=generic_impersonate,
            )
            need_login = need_login or source_need_login
            if success or self._should_stop(task):
                if self._should_stop(task):
                    self._mark_stopped(task)
                return success, need_login
        return False, need_login

    def _append_browser_context_headers(
        self,
        cmd: list[str],
        task: DownloadTask,
        *,
        include_cookie: bool,
    ) -> None:
        """Append sanitized browser-context headers to yt-dlp argv."""
        safe = normalized_forward_headers(
            task.headers,
            include_cookie=include_cookie,
            include_authorization=bool((task.headers or {}).get('_allow_authorization_header')),
        )
        if safe.get('user-agent'):
            cmd.extend(['--user-agent', safe['user-agent']])
        if safe.get('referer'):
            cmd.extend(['--referer', safe['referer']])

        for lower_key, header_name in (
            ('origin', 'Origin'),
            ('accept', 'Accept'),
            ('accept-language', 'Accept-Language'),
            ('sec-fetch-site', 'Sec-Fetch-Site'),
            ('sec-fetch-mode', 'Sec-Fetch-Mode'),
            ('sec-fetch-dest', 'Sec-Fetch-Dest'),
            ('sec-ch-ua', 'Sec-Ch-Ua'),
            ('sec-ch-ua-mobile', 'Sec-Ch-Ua-Mobile'),
            ('sec-ch-ua-platform', 'Sec-Ch-Ua-Platform'),
            ('authorization', 'Authorization'),
        ):
            value = safe.get(lower_key)
            if value:
                cmd.extend(['--add-header', f'{header_name}: {value}'])

        cookie = safe.get('cookie')
        if cookie and include_cookie and not self._is_youtube_url(task.url):
            cmd.extend(['--add-header', f'Cookie: {cookie}'])

    @classmethod
    def _validate_cookie_file_path(cls, cookie_file: str) -> str:
        """F-03: reject ``_cookie_file`` paths outside trusted roots.

        A malicious ``m3u8dl://`` JSON payload can inject
        ``headers._cookie_file`` with an arbitrary local path. Even
        though the protocol handler now strips ``_``-prefixed keys and
        CatCatch repeats the strip server-side, this final check at the
        engine boundary ensures that if a value ever reaches
        ``_build_command`` it is confined to:

          * the app ``cookies/`` directory, OR
          * the user's ``~/.m3u8d`` directory (portable profile root).

        Both roots are realpath-compared against
        ``os.path.realpath(cookie_file)`` so symlinks / ``..`` traversal
        cannot escape. A rejected path is logged as
        ``cookie_file_outside_trusted_root`` and the cookie file is
        silently dropped (yt-dlp then falls back to its normal cookie
        discovery flow).
        """
        if not cookie_file or not isinstance(cookie_file, str):
            return ""
        import os
        from pathlib import Path

        try:
            candidate = Path(os.path.realpath(cookie_file))
        except (OSError, ValueError):
            logger.warning(
                "[yt-dlp] cookie file path rejected (unresolvable)",
                event="cookie_file_outside_trusted_root",
                stage="build_command",
            )
            return ""

        trusted_roots = []
        for root_candidate in (
            Path(cls.get_cookies_base_path()),
            get_data_root() / "cookies",
            Path.home() / ".m3u8d",
        ):
            try:
                trusted_roots.append(root_candidate.resolve())
            except (OSError, RuntimeError):
                # A root may be unresolvable in frozen/test envs; skip it
                # and keep the check fail-closed when no trusted root works.
                pass

        for root in trusted_roots:
            try:
                candidate.relative_to(root)
                return str(candidate)
            except ValueError:
                continue

        logger.warning(
            "[yt-dlp] cookie file outside trusted root; dropping",
            event="cookie_file_outside_trusted_root",
            stage="build_command",
            cookie_file=cookie_file,
        )
        return ""

    def can_handle(self, url: str) -> bool:
        """yt-dlp 是万能兜底，总是返回 True"""
        return True
    
    def get_name(self) -> str:
        return "yt-dlp"
    
    def download(self, task: DownloadTask, progress_callback) -> bool:
        """执行通用视频下载（含策略链增强回退）。

        策略顺序:
        1. 默认模式（cookie 文件或嗅探器 cookies）
        2. 非 YouTube URL 有 Cookie header 时允许 --add-header Cookie
        3. generic impersonate（浏览器伪装绕过 Cloudflare）
        4. Playwright/Chrome/Firefox 浏览器 cookies
        5. 证书错误时 --no-check-certificates（最后一次尝试）
        """
        import os

        if not isinstance(task.headers, dict):
            task.headers = {}

        # 根据 URL 查找对应的 cookies 文件
        cookies_file = self.get_cookies_file_for_url(task.url)
        has_cookies_file = cookies_file and os.path.exists(cookies_file)
        is_bilibili = 'bilibili.com' in (task.url or '').lower()

        # 第一次尝试：使用手动导出的 cookies 文件（如果存在）
        if has_cookies_file:
            task.headers['_cookie_file'] = cookies_file
            logger.info(f"[yt-dlp] 使用手动导出的 cookies: {cookies_file}")

        if self._should_stop(task):
            self._mark_stopped(task)
            return False

        success, need_login = self._do_download(
            task,
            progress_callback,
            use_browser_cookies=None,
            allow_insecure_tls=False,
        )

        if success or self._should_stop(task):
            if self._should_stop(task):
                self._mark_stopped(task)
            return success

        # Bilibili 某些视频在未登录时不会直接提示 sign in，而是返回 No video formats found
        # 但使用浏览器 cookies 可以正常拿到格式。M6 起仅在失败文本符合登录态/空格式类
        # 可恢复场景时重试，避免 DRM/地区/已下架等终态错误盲目启动浏览器 cookie 子进程。
        if (
            is_bilibili
            and not has_cookies_file
            and self._should_try_browser_cookie_retry(task.error_message or "", task.url)
        ):
            logger.warning("[yt-dlp] Bilibili 无 cookies 首次下载失败，怀疑为登录态/站点限制导致的空格式")
            success, browser_need_login = self._try_browser_cookie_retries(
                task,
                progress_callback,
                reason="bilibili_login_or_empty_formats",
            )
            need_login = need_login or browser_need_login
            if success or self._should_stop(task):
                if self._should_stop(task):
                    self._mark_stopped(task)
                return success
            logger.warning("[yt-dlp] 浏览器 cookies 备用认证仍失败，问题更可能是 yt-dlp/Bilibili 站点变更或账号权限限制")

        # 非 YouTube 且有 Cookie header：首次命令已传 Cookie header。
        if task.headers.get('cookie') and not self._is_youtube_url(task.url):
            pass

        # Generic impersonate fallback: only use a browser-like fingerprint
        # for failures that commonly benefit from it (bot checks, Cloudflare,
        # hotlink 403, or empty formats). Avoid blind retries for DRM/geo or
        # plainly unsupported URLs.
        generic_failed_for_browser_state = False
        if not self._should_stop(task) and self._should_try_generic_impersonate(task.error_message or ""):
            logger.info("[yt-dlp] 尝试 generic impersonate 浏览器伪装...")
            success, generic_need_login = self._do_download(
                task,
                progress_callback,
                use_browser_cookies=None,
                allow_insecure_tls=False,
                generic_impersonate=True,
            )
            need_login = need_login or generic_need_login
            if success or self._should_stop(task):
                if self._should_stop(task):
                    self._mark_stopped(task)
                return success
            generic_failed_for_browser_state = self._should_try_browser_cookie_retry(
                task.error_message or "",
                task.url,
            )

        # 如果需要登录，或失败文本符合可由真实浏览器 cookies 恢复的页面类问题。
        if need_login or generic_failed_for_browser_state or self._should_try_browser_cookie_retry(task.error_message or "", task.url):
            if has_cookies_file:
                # cookies 文件存在但可能已过期
                cookies_filename = os.path.basename(cookies_file)
                logger.warning(f"[yt-dlp] ⚠️ cookies 可能已过期，请重新导出 {cookies_filename}")
            else:
                # cookies 文件不存在，提示用户导出
                if cookies_file:
                    expected_file = os.path.basename(cookies_file)
                else:
                    # 尝试从 URL 推断期望的文件名
                    from urllib.parse import urlparse
                    try:
                        domain = urlparse(task.url).netloc
                        expected_file = f"www.{domain.replace('www.', '')}_cookies.txt"
                    except Exception as e:
                        logger.debug(f"[yt-dlp] 站点域名推断失败: url={task.url} error={e}")
                        expected_file = "对应网站的 cookies 文件"

                logger.warning(f"[yt-dlp] ⚠️ 需要登录/浏览器验证但未找到可用 cookies 文件！")
                logger.warning(f"[yt-dlp] 💡 请先在内置浏览器或系统浏览器完成验证，或导出 {expected_file}")

            need_impersonate_for_cookie = generic_failed_for_browser_state or self._should_try_generic_impersonate(
                task.error_message or ""
            )
            success, _ = self._try_browser_cookie_retries(
                task,
                progress_callback,
                reason="auth_or_bot_check",
                generic_impersonate=need_impersonate_for_cookie,
            )
            if self._should_stop(task):
                self._mark_stopped(task)
                return False
            return success

        return False
    
    def _do_download(
        self,
        task: DownloadTask,
        progress_callback,
        use_browser_cookies=None,
        allow_insecure_tls: bool = False,
        generic_impersonate: bool = False,
    ) -> tuple:
        """
        执行下载
        Args:
            use_browser_cookies: None=只用任务自带cookies, 'chromium'=使用Chromium, 'firefox'=使用Firefox
            generic_impersonate: True 时启用 yt-dlp 浏览器伪装绕过 Cloudflare 等反爬
        Returns: (success: bool, need_login: bool)
        """
        output_lines: list[str] = []
        try:
            if self._should_stop(task):
                self._mark_stopped(task)
                return False, False

            cmd = self._build_command(
                task,
                use_browser_cookies=use_browser_cookies,
                allow_insecure_tls=allow_insecure_tls,
                generic_impersonate=generic_impersonate,
            )
            
            # 日志显示使用的 cookie 来源
            cookie_source = ""
            if use_browser_cookies:
                cookie_source = f" (使用 {use_browser_cookies} cookies)"
            elif task.headers.get('cookie'):
                cookie_source = " (使用嗅探器 cookies)"
            if allow_insecure_tls:
                cookie_source += " (禁用证书校验重试)"
            
            logger.info(f"[yt-dlp] 开始下载: {task.filename}{cookie_source}")
            self.log_command(cmd)

            # ``BaseEngine.spawn`` (task 29.1) owns the CREATE_NO_WINDOW /
            # close_fds / byte-mode PIPE boilerplate. ``sensitive=False``
            # suppresses spawn's own ``log_command`` call since we already
            # emitted a redacted line above. ``read_loop`` drains stdout
            # and stderr on separate pump threads (task 9.2), so the old
            # ``stderr=STDOUT`` merge is no longer needed — ``_parse_line``
            # accumulates every line for post-run login-keyword diagnosis
            # regardless of tag.
            process = self.spawn(cmd, sensitive=False)

            # ISS-30: bind process ownership metadata atomically so manager
            # kill paths can guard against pid reuse.
            with task.lock:
                task.process = process
                task._pid = getattr(process, "pid", None)
                task._expected_engine_name = self.get_name()

            # ``read_loop`` drives PIPE draining, stop-request handling, and
            # the terminate→kill escalation. ``_parse_line`` accumulates
            # output on the thread-local slot for post-run diagnostics.
            self._tls.progress_callback = progress_callback
            self._tls.output_lines = output_lines
            try:
                result: EngineResult = self.read_loop(process, task, self._parse_line)
            finally:
                self._tls.progress_callback = None
                self._tls.output_lines = None

            # Stop requests (paused / cancelled / removed / shutdown /
            # engine_switch) are already reflected on ``task.stop_reason``;
            # surface them as a non-login failure so callers don't escalate
            # into browser-cookie retries.
            if result.status in {"stopped", "switched", "paused"}:
                self._mark_stopped(task)
                return False, False

            returncode = result.returncode
            success = result.status == "ok"

            # 检测是否需要登录/浏览器态重试。部分站点（尤其 YouTube/Bilibili）
            # 会用 bot-check、403 或空格式替代直白的 login required 文案。
            full_output = '\n'.join(output_lines).lower()
            need_login = (not success) and self._is_login_required_failure(full_output)

            if (not success) and (not allow_insecure_tls) and self._is_certificate_error(full_output):
                # ISS-17: 重试前检查 stop 信号，避免在任务已被停止后仍
                # spawn 新子进程，造成进程泄漏与资源浪费。
                if self._should_stop(task):
                    self._mark_stopped(task)
                    return False, False
                # 清理上一次尝试残留的 process 引用（进程已由
                # read_loop 终止并关闭管道），避免下游代码误判存活状态。
                task.process = None
                logger.warning("[yt-dlp] 检测到证书校验失败，自动启用 --no-check-certificates 重试一次")
                return self._do_download(
                    task,
                    progress_callback,
                    use_browser_cookies=use_browser_cookies,
                    allow_insecure_tls=True
                )
            
            if success:
                logger.info(f"[yt-dlp] 下载成功: {task.filename}")
            else:
                logger.error(f"[yt-dlp] 下载失败: {task.filename}, 退出码: {returncode}")
                tail_lines = output_lines[-20:] if output_lines else []
                if tail_lines:
                    logger.error("[yt-dlp] 输出尾部(20行):\n" + "\n".join(tail_lines))
                    task.error_message = "\n".join(tail_lines)
                else:
                    task.error_message = full_output[-1000:] if full_output else f"yt-dlp exit code: {returncode}"
                reason, suggestions = self._diagnose_failure(full_output)
                if reason:
                    logger.warning(f"[yt-dlp] 失败原因: {reason}")
                if suggestions:
                    logger.warning("[yt-dlp] 建议: " + "；".join(suggestions))
            
            return success, need_login
            
        except Exception as e:
            logger.error(f"[yt-dlp] 下载异常: {e}")
            task.error_message = str(e)
            return False, False

    def _parse_line(self, stream_tag: str, text: str) -> None:
        """``read_loop`` callback: accumulate output and push progress events."""
        line = text.strip()
        if not line:
            return

        output_lines = getattr(self._tls, "output_lines", None)
        if output_lines is not None:
            output_lines.append(line)

        logger.debug(f"[yt-dlp] {line}")
        progress_data = self.parse_progress(line)
        if progress_data['progress'] > 0 or progress_data['speed']:
            progress_callback = getattr(self._tls, "progress_callback", None)
            if progress_callback is not None:
                progress_callback(progress_data)
    
    def _build_command(
        self,
        task: DownloadTask,
        use_browser_cookies=None,
        allow_insecure_tls: bool = False,
        generic_impersonate: bool = False,
    ) -> list:
        """构建下载命令
        
        Args:
            use_browser_cookies: None=不使用浏览器cookies, 'chromium'=使用Chromium, 'firefox'=使用Firefox
            generic_impersonate: True 时拼装 yt-dlp 浏览器伪装参数
                (``--extractor-args generic:impersonate``)，用于绕过 Cloudflare 等
                基于浏览器指纹的防护；该场景下还会以 ``--add-header`` 形式补传
                Origin / Cookie，恢复 ISS-02 中被删除的 Cloudflare 绕过能力。
        """
        from utils.config_manager import config
        
        # 从 URL 末尾的 fragment 中提取内部格式选择标记（如果有）
        # 这里只认程序自己附加的 `#format=`，避免把站点原本合法的 fragment 误当作内部参数。
        url = task.url
        format_id = None

        if '#format=' in url:
            base_url, format_param = url.rsplit('#format=', 1)
            # Audit-finding Medium #5: yt-dlp accepts non-numeric format
            # ids such as ``bestvideo+bestaudio``, ``137+140``, ``hls-720``.
            # The previous ``isdigit()`` gate silently fell back to
            # best-quality for every non-numeric selection the UI made.
            # Widen to a conservative safe-character allowlist that
            # covers real-world format ids while still refusing shell
            # metacharacters (space, ``;``, ``&``, ``|``, ``$``, backticks,
            # quotes, newlines) so the value stays safe to hand to the
            # engine as a plain argv token.
            if re.fullmatch(r"[A-Za-z0-9_.+:\-]+", format_param or ""):
                url = base_url
                format_id = format_param
                logger.info(f"[yt-dlp] 使用指定格式: {format_id}")
            else:
                logger.warning(
                    f"[yt-dlp] 忽略不安全的 #format= 取值,回退到默认选择",
                    event="ytdlp_format_rejected",
                    stage="format_parse",
                )
        
        output_template = str(Path(task.save_dir) / f'{task.filename}.%(ext)s')
        
        cmd = [
            self.binary_path,
            url,
            '-o', output_template,
            '--newline',  # 每次进度单独一行
            '--no-warnings',
            '--no-playlist',  # 只下载单个视频，不下载整个播放列表
            '--merge-output-format', 'mp4',
        ]
        
        browser_cookie_source = self._resolve_browser_cookie_source(use_browser_cookies)
        if browser_cookie_source:
            cmd.extend(['--cookies-from-browser', browser_cookie_source])
            logger.info(f"[yt-dlp] 尝试使用浏览器 cookies: {browser_cookie_source}")
        
        # 指定格式
        if format_id:
            # 如果指定了格式ID，优先使用该格式+最佳音频
            cmd.extend(['--format', f'{format_id}+bestaudio/best'])
        else:
            # 否则使用最佳质量
            cmd.extend(['--format', 'bestvideo+bestaudio/best'])
        
        # 限速（从配置读取）
        speed_limit = config.get("speed_limit", 0)
        if speed_limit > 0:
            # yt-dlp 的 --limit-rate 参数，单位可以是 K, M
            cmd.extend(['--limit-rate', f'{speed_limit}M'])
            logger.info(f"[yt-dlp] 限速: {speed_limit}M/s")
        
        # 添加请求头：即使用浏览器 cookies，也保留 Referer/Origin/UA 等
        # 浏览器上下文；Cookie header 仅在不使用 --cookies-from-browser 时透传，
        # 避免与 yt-dlp 自己的 cookie jar 合并策略冲突。
        self._append_browser_context_headers(
            cmd,
            task,
            include_cookie=not bool(browser_cookie_source),
        )
        
        # 使用 cookie 文件（从浏览器导出的）
        # F-03: validate the resolved path stays inside a trusted root
        # (the app ``cookies/`` dir or ``~/.m3u8d``) so a malicious
        # ``_cookie_file`` injected via the protocol-handler JSON path
        # cannot point yt-dlp's ``--cookies`` at an arbitrary local
        # file (e.g. C:\Windows\System32\config\SAM).
        if task.headers.get('_cookie_file'):
            cookie_file = task.headers['_cookie_file']
            cookie_file = self._validate_cookie_file_path(cookie_file)
            if cookie_file and os.path.exists(cookie_file):
                cmd.extend(['--cookies', cookie_file])
                logger.info(f"[yt-dlp] 使用导出的 cookies: {cookie_file}")

        # Cloudflare 等基于浏览器指纹的防护绕过（ISS-02 恢复）：
        # ``--extractor-args generic:impersonate`` 让 yt-dlp 用真实浏览器
        # 指纹发起请求。该场景下 Origin / Cookie 必须随请求头带上去，否则
        # 站点会因鉴权/防盗链缺失而拒绝；此分支独立于默认的 YouTube cookie
        # 跳过逻辑，避免影响既有认证流程。
        if generic_impersonate:
            cmd.extend(['--extractor-args', 'generic:impersonate'])

        if allow_insecure_tls:
            cmd.append('--no-check-certificates')

        proxy = proxy_for_url(url)
        if proxy:
            cmd.extend(['--proxy', proxy])

        return cmd

    def _is_login_required_failure(self, output_text: str) -> bool:
        """Return True when a failed run is likely recoverable by browser cookies."""
        if not output_text:
            return False
        text = output_text.lower()
        login_or_auth_keywords = (
            "sign in",
            "login",
            "log in",
            "please log in",
            "authentication required",
            "requires authentication",
            "account required",
            "private video",
            "members-only",
            "members only",
            "subscriber",
            "age-restricted",
            "cookies are required",
            "cookie is required",
            "http error 401",
            "unauthorized",
            "http error 403",
            "forbidden",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "not a bot",
            "automated requests",
            "unusual traffic",
            "captcha",
            "no video formats",
            "no formats found",
        )
        return any(keyword in text for keyword in login_or_auth_keywords)

    def _is_certificate_error(self, output_text: str) -> bool:
        if not output_text:
            return False
        text = output_text.lower()
        return (
            "certificate_verify_failed" in text
            or "unable to get local issuer certificate" in text
            or "[ssl: certificate_verify_failed]" in text
        )

    @staticmethod
    def _is_terminal_non_retryable_failure(output_text: str) -> bool:
        """Return True when yt-dlp output indicates retries are unlikely to help."""
        text = (output_text or "").lower()
        terminal_keywords = (
            "drm",
            "widevine",
            "license server",
            "protected by drm",
            "not available in your country",
            "blocked in your country",
            "not available from your location",
            "region restriction",
            "country restriction",
            "unsupported url",
            "no suitable extractor",
            "this video has been removed",
            "video has been removed",
            "copyright claim",
            "not available anymore",
            "livestream has ended",
            "premiere has not begun",
        )
        return any(keyword in text for keyword in terminal_keywords)

    def _should_try_generic_impersonate(self, output_text: str) -> bool:
        """Return True when browser fingerprint impersonation is a useful fallback."""
        text = (output_text or "").lower()
        if not text or self._is_terminal_non_retryable_failure(text):
            return False
        impersonate_keywords = (
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "not a bot",
            "automated requests",
            "unusual traffic",
            "captcha",
            "checking your browser",
            "cloudflare",
            "ddos-guard",
            "akamai",
            "403",
            "http error 403",
            "forbidden",
            "no video formats",
            "no formats found",
            "requested format is not available",
        )
        return any(keyword in text for keyword in impersonate_keywords)

    def _should_try_browser_cookie_retry(self, output_text: str, url: str = "") -> bool:
        """Return True when browser cookies may recover a page-class failure."""
        text = (output_text or "").lower()
        if not text or self._is_terminal_non_retryable_failure(text):
            return False
        if "bilibili.com" in (url or "").lower() and any(
            keyword in text for keyword in ("no video formats", "no formats found", "requested format")
        ):
            return True
        cookie_keywords = (
            "sign in",
            "login",
            "log in",
            "please log in",
            "authentication required",
            "requires authentication",
            "account required",
            "private video",
            "members-only",
            "members only",
            "subscriber",
            "age-restricted",
            "cookies are required",
            "cookie is required",
            "http error 401",
            "unauthorized",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "no video formats",
            "no formats found",
        )
        return any(keyword in text for keyword in cookie_keywords)

    def _should_retry_formats_with_browser_cookies(self, output_text: str, url: str = "") -> bool:
        """Return True when format probing should retry with browser cookies."""
        return self._should_try_browser_cookie_retry(output_text, url) or self._should_try_generic_impersonate(output_text)
    
    def _diagnose_failure(self, output_text: str) -> tuple:
        """根据输出日志推断失败原因并给出建议"""
        if not output_text:
            return "无输出", ["检查网络连接或站点是否可访问"]

        text = output_text.lower()
        suggestions = []
        reason = ""

        if "429" in text or "too many requests" in text or "rate limit" in text or "http error 429" in text:
            reason = "429/限流（请求过快或站点临时限制）"
            suggestions.extend(["稍后重试", "降低并发/限速", "必要时更换网络出口"])
        elif any(keyword in text for keyword in ("drm", "widevine", "license server", "protected by drm", "copyright protection")):
            reason = "DRM/加密授权限制"
            suggestions.extend(["该内容可能不支持下载", "确认资源是否需要官方客户端播放"])
        elif any(keyword in text for keyword in (
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "not a bot",
            "automated requests",
            "unusual traffic",
            "captcha",
            "checking your browser",
            "cloudflare",
            "ddos-guard",
            "checking if the site connection is secure",
        )):
            reason = "反爬/机器人校验（需要真实浏览器态）"
            suggestions.extend(["使用浏览器 cookies 重试", "更新 yt-dlp", "必要时在浏览器中完成验证后重新嗅探"])
        elif any(keyword in text for keyword in (
            "401",
            "http error 401",
            "unauthorized",
            "sign in",
            "login required",
            "please log in",
            "authentication required",
            "private video",
            "members-only",
            "members only",
            "subscriber",
            "age-restricted",
            "cookies are required",
            "cookie is required",
            "join this channel",
            "paid content",
        )):
            reason = "需要登录/账号权限不足"
            suggestions.extend(["导出并配置 cookies", "尝试使用浏览器 cookies", "确认账号具备观看权限"])
        elif "403" in text or "http error 403" in text or "forbidden" in text:
            reason = "403/Forbidden（可能鉴权/防盗链）"
            suggestions.extend(["检查 Referer/UA 是否正确", "导出并配置 cookies", "尝试使用浏览器 cookies"])
        elif any(keyword in text for keyword in (
            "geo",
            "not available in your country",
            "blocked in your country",
            "not available from your location",
            "region restriction",
            "country restriction",
            "geo-restricted",
            "geo restricted",
        )):
            reason = "地理限制/地区不可用"
            suggestions.extend(["使用代理/VPN", "更换节点后重试"])
        elif any(keyword in text for keyword in (
            "signature extraction",
            "nsig",
            "n-sig",
            "player response",
            "signature cipher",
            "unable to extract uploader id",
            "unable to extract initial player response",
        )):
            reason = "签名/播放器解析失败（可能需要更新 yt-dlp）"
            suggestions.extend(["更新 yt-dlp 到最新版本", "稍后重试", "尝试使用浏览器 cookies"])
        elif any(keyword in text for keyword in (
            "extractor error",
            "unable to extract",
            "please report this issue",
            "make sure you are using the latest version",
            "this version of yt-dlp is outdated",
            "update to the latest version",
            "unsupported url",
            "no suitable extractor",
        )):
            reason = "站点解析器失效或 yt-dlp 版本过旧"
            suggestions.extend(["更新 yt-dlp", "确认链接为视频详情页", "等待上游适配站点变更"])
        elif any(keyword in text for keyword in (
            "no video formats",
            "no formats found",
            "requested format is not available",
            "this video is unavailable",
            "video unavailable",
            "selected format is not available",
        )):
            reason = "无法解析可下载格式（可能登录态不足或资源不可用）"
            suggestions.extend(["使用浏览器 cookies 重试", "切换引擎/格式", "检查链接是否仍可播放"])
        elif any(keyword in text for keyword in (
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "temporarily unavailable",
            "temporary failure in name resolution",
            "incomplete read",
            "read timed out",
        )):
            reason = "网络超时或连接被重置"
            suggestions.extend(["降低并发/限速", "检查网络稳定性", "稍后重试"])
        elif any(keyword in text for keyword in (
            "certificate_verify_failed",
            "unable to get local issuer certificate",
            "ssl: certificate",
            "tlsv1 alert",
        )):
            reason = "SSL/TLS 证书校验失败"
            suggestions.extend(["检查系统证书链", "允许引擎在失败时自动禁用证书校验重试"])
        elif any(keyword in text for keyword in (
            "this video has been removed",
            "video has been removed",
            "not available anymore",
            "livestream has ended",
            "premiere has not begun",
        )):
            reason = "资源已失效或直播尚不可用"
            suggestions.extend(["确认链接仍可播放", "刷新页面重新嗅探", "稍后重试"])
        elif "unable to download" in text:
            reason = "下载阶段失败"
            suggestions.extend(["检查网络与站点可访问性", "尝试切换引擎", "保留失败日志用于定位"])

        return reason, suggestions

    def get_formats(self, url: str, cookie: str | None = None, use_browser_cookies: bool = False, cookie_file: str | None = None) -> list:
        """获取可用格式列表
        
        Args:
            url: 视频 URL
            cookie: Cookie 字符串（可选，已废弃）
            use_browser_cookies: 是否使用 Firefox cookies（自动回退时设为 True）
            cookie_file: 预导出的 cookie 文件路径（优先使用）
        
        Returns:
            list: [{'format_id': '137', 'ext': 'mp4', 'height': 1080, 'vcodec': 'avc1', 'fps': 30}, ...]
        """
        import json
        import os
        
        cmd = [self.binary_path, url, '-J', '--no-warnings', '--no-playlist']
        
        # 根据 URL 查找对应的 cookies 文件（如果没有指定 cookie_file）
        if not cookie_file and not use_browser_cookies:
            manual_cookies = self.get_cookies_file_for_url(url)
            if manual_cookies and os.path.exists(manual_cookies):
                cookie_file = manual_cookies
        
        # 优先使用预导出的 cookie 文件
        if cookie_file and os.path.exists(cookie_file):
            cmd.extend(['--cookies', cookie_file])
            logger.info(f"[yt-dlp] 使用 cookies 获取格式: {cookie_file}")
        elif use_browser_cookies:
            cmd.extend(['--cookies-from-browser', 'firefox'])
            logger.info("[yt-dlp] 使用 Firefox cookies 获取格式...")
        elif cookie:
            cmd.extend(['--add-header', f'Cookie: {cookie}'])
        
        try:
            # Probe-only path: yt-dlp ``-J`` dumps the full format metadata
            # as a JSON blob we need synchronously, so we stay on
            # ``subprocess.run`` rather than routing through
            # ``BaseEngine.spawn`` + ``read_loop``. The single-line
            # ``getattr`` keeps CREATE_NO_WINDOW attachment consistent
            # with the other probe sites (task 29.2).
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            
            if result.returncode != 0:
                error_output = result.stderr or result.stdout or ''
                error_msg = error_output[:500]
                logger.error(f"[yt-dlp] 获取格式失败: {error_msg}")
                reason, suggestions = self._diagnose_failure(error_output)
                if reason:
                    logger.warning(f"[yt-dlp] 获取格式失败原因: {reason}")
                if suggestions:
                    logger.warning("[yt-dlp] 获取格式建议: " + "；".join(suggestions))
                
                # 如果使用了 cookie 文件但失败，提示可能过期
                if cookie_file and self._is_login_required_failure(error_output):
                    logger.warning("[yt-dlp] ⚠️ cookies 可能已过期，请重新导出对应站点的 cookies 文件")
                
                # Bilibili 常见场景：未登录时不明确报登录，而是直接空格式；
                # 仅在诊断链路认为 cookies/浏览器态可能恢复时回退，跳过 DRM/地区/已下架终态失败。
                if (
                    (not use_browser_cookies)
                    and ('bilibili.com' in (url or '').lower())
                    and self._should_retry_formats_with_browser_cookies(error_output, url)
                ):
                    logger.info("[yt-dlp] Bilibili 获取格式失败，尝试使用 Firefox cookies 重试...")
                    return self.get_formats(url, None, use_browser_cookies=True)
                
                # 仅对登录态/反爬/空格式类失败尝试 Firefox cookies，避免 DRM/地区/过期资源盲试。
                if (not use_browser_cookies) and self._should_retry_formats_with_browser_cookies(error_output, url):
                    return self.get_formats(url, None, use_browser_cookies=True)
                return []
            
            # 解析 JSON 输出
            data = json.loads(result.stdout)
            formats = []
            
            if 'formats' in data:
                for fmt in data['formats']:
                    # 只要包含视频流的格式
                    if fmt.get('vcodec') and fmt.get('vcodec') != 'none':
                        # 计算分辨率字符串
                        width = fmt.get('width', 0)
                        height = fmt.get('height', 0)
                        resolution = f"{width}x{height}" if width and height else ""
                        
                        # 格式化文件大小
                        filesize = fmt.get('filesize') or fmt.get('filesize_approx', 0)
                        if filesize:
                            if filesize > 1024 * 1024 * 1024:
                                filesize_str = f"{filesize / 1024 / 1024 / 1024:.2f}GiB"
                            elif filesize > 1024 * 1024:
                                filesize_str = f"{filesize / 1024 / 1024:.2f}MiB"
                            else:
                                filesize_str = f"{filesize / 1024:.0f}KiB"
                        else:
                            filesize_str = ""
                        
                        # 码率
                        tbr = fmt.get('tbr', 0)
                        tbr_str = f"{int(tbr)}k" if tbr else ""
                        
                        formats.append({
                            'format_id': fmt.get('format_id', ''),
                            'ext': fmt.get('ext', 'mp4'),
                            'resolution': resolution,
                            'height': height,
                            'width': width,
                            'fps': fmt.get('fps') or 30,
                            'filesize_str': filesize_str,
                            'tbr': tbr_str,
                            'vcodec': fmt.get('vcodec', ''),
                            'acodec': fmt.get('acodec', ''),
                            'protocol': fmt.get('protocol', ''),
                        })
            
            # 获取到空列表时也尝试用 cookies 重试
            if not formats and not use_browser_cookies and not cookie:
                logger.info("[yt-dlp] 未获取到格式，使用 Firefox cookies 重试...")
                return self.get_formats(url, None, use_browser_cookies=True)
            
            logger.info(f"[yt-dlp] 获取到 {len(formats)} 个视频格式")
            return formats
            
        except subprocess.TimeoutExpired:
            logger.error("[yt-dlp] 获取格式超时")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"[yt-dlp] JSON 解析失败: {e}")
            return []
        except Exception as e:
            logger.error(f"[yt-dlp] 获取格式异常: {e}")
            return []
    
    def parse_progress(self, line: str) -> dict:
        """
        解析进度输出
        示例: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:15
        """
        result = {'progress': 0.0, 'speed': '', 'downloaded': ''}
        
        # 检测是否为下载进度行
        if '[download]' in line:
            # 匹配百分比
            progress_match = re.search(r'(\d+\.?\d*)\s*%', line)
            if progress_match:
                try:
                    result['progress'] = float(progress_match.group(1))
                except ValueError:
                    result['progress'] = 0.0
            
            # 匹配速度
            speed_match = re.search(r'at\s+([0-9.]+[KMG]?iB/s)', line, re.IGNORECASE)
            if speed_match:
                result['speed'] = speed_match.group(1)
        
        return result
