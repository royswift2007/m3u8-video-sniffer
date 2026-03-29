"""
yt-dlp engine wrapper for universal video downloads
"""
import subprocess
import re
import time
from pathlib import Path
from engines.base_engine import BaseEngine
from core.task_model import DownloadTask
from utils.logger import logger


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

    def _terminate_process_if_running(self, task: DownloadTask):
        """在检测到停止请求时尽快终止当前 yt-dlp 进程。"""
        process = getattr(task, "process", None)
        if not process:
            return
        try:
            if process.poll() is None:
                process.kill()
        except Exception as e:
            logger.debug(f"[yt-dlp] 终止进程时忽略异常: {e}")
    
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
        """根据 URL 获取对应的 cookies 文件路径
        
        支持的网站及对应文件名：
        - YouTube: www.youtube.com_cookies.txt
        - Bilibili: www.bilibili.com_cookies.txt
        - TikTok: www.tiktok.com_cookies.txt
        - Twitter/X: www.x.com_cookies.txt
        - Instagram: www.instagram.com_cookies.txt
        等等...
        """
        import os
        from urllib.parse import urlparse
        
        # 解析域名
        try:
            domain = urlparse(url).netloc.lower()
        except Exception as e:
            logger.debug(f"[yt-dlp] URL 解析失败: url={url} error={e}")
            domain = ""
        
        # 域名映射（处理子域名和别名）
        domain_map = {
            'youtube.com': 'www.youtube.com',
            'www.youtube.com': 'www.youtube.com',
            'youtu.be': 'www.youtube.com',
            'm.youtube.com': 'www.youtube.com',
            
            'bilibili.com': 'www.bilibili.com',
            'www.bilibili.com': 'www.bilibili.com',
            
            'tiktok.com': 'www.tiktok.com',
            'www.tiktok.com': 'www.tiktok.com',
            
            'twitter.com': 'www.x.com',
            'x.com': 'www.x.com',
            'www.twitter.com': 'www.x.com',
            'www.x.com': 'www.x.com',
            
            'instagram.com': 'www.instagram.com',
            'www.instagram.com': 'www.instagram.com',
        }
        
        # 查找匹配的域名
        target_domain = None
        for key, value in domain_map.items():
            if key in domain:
                target_domain = value
                break
        
        if not target_domain:
            return ""
        
        # 构建文件路径
        cookies_file = os.path.join(cls.get_cookies_base_path(), f"{target_domain}_cookies.txt")
        return cookies_file
    
    @classmethod
    def get_youtube_cookies_file(cls) -> str:
        """获取 YouTube cookies 文件路径（向后兼容）"""
        import os
        return os.path.join(cls.get_cookies_base_path(), "www.youtube.com_cookies.txt")
    
    def can_handle(self, url: str) -> bool:
        """yt-dlp 是万能兜底，总是返回 True"""
        return True
    
    def get_name(self) -> str:
        return "yt-dlp"
    
    def download(self, task: DownloadTask, progress_callback) -> bool:
        """执行通用视频下载"""
        import os
        
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
            allow_insecure_tls=False
        )
        
        if success or self._should_stop(task):
            if self._should_stop(task):
                self._mark_stopped(task)
            return success
        
        # Bilibili 某些视频在未登录时不会直接提示 sign in，而是返回 No video formats found
        # 但使用浏览器 cookies 可以正常拿到格式，因此这里做一次站点定向降级重试。
        if is_bilibili and not has_cookies_file:
            if self._should_stop(task):
                self._mark_stopped(task)
                return False
            logger.warning("[yt-dlp] Bilibili 无 cookies 首次下载失败，怀疑为登录态/站点限制导致的空格式")
            logger.info("[yt-dlp] 尝试使用 Firefox cookies 作为 Bilibili 备用认证...")
            success, _ = self._do_download(
                task,
                progress_callback,
                use_browser_cookies='firefox',
                allow_insecure_tls=False
            )
            if success or self._should_stop(task):
                if self._should_stop(task):
                    self._mark_stopped(task)
                return success
            logger.warning("[yt-dlp] Firefox cookies 备用认证仍失败，问题更可能是 yt-dlp/Bilibili 站点变更或账号权限限制")
        
        # 如果需要登录
        if need_login:
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
                
                logger.warning(f"[yt-dlp] ⚠️ 需要登录但未找到 cookies 文件！")
                logger.warning(f"[yt-dlp] 💡 请导出 {expected_file} 并放到程序目录")
            
            if self._should_stop(task):
                self._mark_stopped(task)
                return False
            # 回退到 Firefox cookies
            logger.info(f"[yt-dlp] 尝试使用 Firefox cookies 作为备用...")
            # 清除可能失效的 cookie 文件路径
            task.headers.pop('_cookie_file', None)
            success, _ = self._do_download(
                task,
                progress_callback,
                use_browser_cookies='firefox',
                allow_insecure_tls=False
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
        allow_insecure_tls: bool = False
    ) -> tuple:
        """
        执行下载
        Args:
            use_browser_cookies: None=只用任务自带cookies, 'chromium'=使用Chromium, 'firefox'=使用Firefox
        Returns: (success: bool, need_login: bool)
        """
        process = None
        output_lines = []
        try:
            if self._should_stop(task):
                self._mark_stopped(task)
                return False, False

            cmd = self._build_command(
                task,
                use_browser_cookies=use_browser_cookies,
                allow_insecure_tls=allow_insecure_tls
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
            logger.debug(f"命令: {' '.join(cmd)}")
            
            # 隐藏 CMD 窗口
            creation_flags = 0
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                creationflags=creation_flags
            )
            
            task.process = process
            stdout = process.stdout
            
            while True:
                if self._should_stop(task):
                    self._mark_stopped(task)
                    self._terminate_process_if_running(task)
                    break

                if stdout is None:
                    break

                line = stdout.readline()
                if line == "":
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue

                line = line.strip()
                if line:
                    output_lines.append(line)
                    logger.debug(f"[yt-dlp] {line}")
                    progress_data = self.parse_progress(line)
                    if progress_data['progress'] > 0 or progress_data['speed']:
                        progress_callback(progress_data)
            
            if self._should_stop(task):
                self._mark_stopped(task)
                self._terminate_process_if_running(task)

            returncode = process.poll()
            if returncode is None:
                if self._should_stop(task):
                    self._mark_stopped(task)
                    self._terminate_process_if_running(task)
                returncode = process.wait()
            success = returncode == 0 and not self._should_stop(task)
            
            # 检测是否需要登录
            need_login = False
            full_output = '\n'.join(output_lines).lower()
            login_keywords = ['sign in', 'login', 'private video', 'members-only', 'subscriber', 'age-restricted']
            if not success and any(kw in full_output for kw in login_keywords):
                need_login = True

            if self._should_stop(task):
                self._mark_stopped(task)
                return False, False

            if (not success) and (not allow_insecure_tls) and self._is_certificate_error(full_output):
                if self._should_stop(task):
                    self._mark_stopped(task)
                    return False, False
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
        finally:
            if self._should_stop(task):
                self._terminate_process_if_running(task)
    
    def _build_command(
        self,
        task: DownloadTask,
        use_browser_cookies=None,
        allow_insecure_tls: bool = False
    ) -> list:
        """构建下载命令
        
        Args:
            use_browser_cookies: None=不使用浏览器cookies, 'chromium'=使用Chromium, 'firefox'=使用Firefox
        """
        from utils.config_manager import config
        import os
        
        # 从 URL 末尾的 fragment 中提取内部格式选择标记（如果有）
        # 这里只认程序自己附加的 `#format=`，避免把站点原本合法的 fragment 误当作内部参数。
        url = task.url
        format_id = None

        if '#format=' in url:
            base_url, format_param = url.rsplit('#format=', 1)
            if format_param.isdigit():
                url = base_url
                format_id = format_param
                logger.info(f"[yt-dlp] 使用指定格式: {format_id}")
        
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
        
        # 使用浏览器 cookies（需要浏览器关闭或支持读取）
        if use_browser_cookies == 'chromium':
            # 使用 Playwright 的 Chromium 用户数据目录
            chromium_data_dir = os.path.join(
                os.environ.get('APPDATA', ''),
                'M3U8VideoSniffer',
                'chromium_user_data'
            )
            if os.path.exists(chromium_data_dir):
                cmd.extend(['--cookies-from-browser', f'chromium:{chromium_data_dir}'])
                logger.info(f"[yt-dlp] 尝试使用 Chromium cookies: {chromium_data_dir}")
            else:
                # 回退到默认 Chrome
                cmd.extend(['--cookies-from-browser', 'chrome'])
                logger.info("[yt-dlp] 尝试使用系统 Chrome cookies")
        elif use_browser_cookies == 'firefox':
            cmd.extend(['--cookies-from-browser', 'firefox'])
        
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
        
        # 添加请求头（仅在不使用浏览器 cookies 时）
        if not use_browser_cookies:
            if task.headers.get('user-agent'):
                cmd.extend(['--user-agent', task.headers['user-agent']])
            
            if task.headers.get('referer'):
                cmd.extend(['--referer', task.headers['referer']])
            
            # 对于 YouTube，--add-header Cookie 方式效果不佳，跳过
            # if task.headers.get('cookie'):
            #     cmd.extend(['--add-header', f'Cookie: {task.headers["cookie"]}'])
        
        # 使用 cookie 文件（从浏览器导出的）
        if task.headers.get('_cookie_file'):
            cookie_file = task.headers['_cookie_file']
            if os.path.exists(cookie_file):
                cmd.extend(['--cookies', cookie_file])
                logger.info(f"[yt-dlp] 使用导出的 cookies: {cookie_file}")

        if allow_insecure_tls:
            cmd.append('--no-check-certificates')
        
        return cmd

    def _is_certificate_error(self, output_text: str) -> bool:
        if not output_text:
            return False
        text = output_text.lower()
        return (
            "certificate_verify_failed" in text
            or "unable to get local issuer certificate" in text
            or "[ssl: certificate_verify_failed]" in text
        )
    
    def _diagnose_failure(self, output_text: str) -> tuple:
        """根据输出日志推断失败原因并给出建议"""
        if not output_text:
            return "无输出", ["检查网络连接或站点是否可访问"]

        text = output_text.lower()
        suggestions = []
        reason = ""

        if "403" in text or "http error 403" in text or "forbidden" in text:
            reason = "403/Forbidden（可能鉴权/防盗链）"
            suggestions.extend(["检查 Referer/UA 是否正确", "导出并配置 cookies", "尝试使用浏览器 cookies"])
        elif "401" in text or "http error 401" in text or "unauthorized" in text:
            reason = "401/Unauthorized（需要登录）"
            suggestions.extend(["导出 cookies 并放入 cookies 目录", "尝试使用浏览器 cookies"])
        elif "geo" in text or "not available in your country" in text or "blocked in your country" in text:
            reason = "地理限制/地区不可用"
            suggestions.extend(["使用代理/VPN", "更换节点后重试"])
        elif "signature" in text or "nsig" in text or "signature extraction" in text:
            reason = "签名/解析失败（可能需要更新 yt-dlp）"
            suggestions.extend(["更新 yt-dlp 到最新版本", "稍后重试"])
        elif "private video" in text or "members-only" in text or "subscriber" in text:
            reason = "访问权限受限"
            suggestions.extend(["确认账号有权限", "使用已登录的 cookies"])
        elif "timed out" in text or "timeout" in text or "connection reset" in text:
            reason = "网络超时或连接被重置"
            suggestions.extend(["降低并发/限速", "检查网络稳定性", "稍后重试"])
        elif "certificate_verify_failed" in text or "unable to get local issuer certificate" in text:
            reason = "SSL 证书校验失败"
            suggestions.extend(["检查系统证书链", "允许引擎在失败时自动禁用证书校验重试"])
        elif "unable to download" in text or "no video formats" in text:
            reason = "无法解析视频格式"
            suggestions.extend(["尝试切换引擎", "检查链接是否为有效页面"])

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
            # 隐藏 CMD 窗口
            creation_flags = 0
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creation_flags = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=30,
                creationflags=creation_flags
            )
            
            if result.returncode != 0:
                error_output = result.stderr or result.stdout or ''
                error_msg = error_output[:500]
                logger.error(f"[yt-dlp] 获取格式失败: {error_msg}")
                
                # 如果使用了 cookie 文件但失败，提示可能过期
                if cookie_file and ('sign in' in error_output.lower() or 'login' in error_output.lower()):
                    logger.warning("[yt-dlp] ⚠️ cookies 可能已过期，请重新导出对应站点的 cookies 文件")
                
                # Bilibili 常见场景：未登录时不明确报登录，而是直接空格式；此时自动回退浏览器 cookies
                if (not use_browser_cookies) and ('bilibili.com' in (url or '').lower()):
                    logger.info("[yt-dlp] Bilibili 获取格式失败，尝试使用 Firefox cookies 重试...")
                    return self.get_formats(url, None, use_browser_cookies=True)
                
                # 失败时尝试用 Firefox cookies 重试
                if not use_browser_cookies:
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
            # 超时时也尝试用 cookies 重试
            if not use_browser_cookies and not cookie:
                return self.get_formats(url, None, use_browser_cookies=True)
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
