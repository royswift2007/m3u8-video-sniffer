"""
Aria2 engine wrapper for direct link downloads with multi-threading
"""
import re
from pathlib import Path
from engines.base_engine import BaseEngine, EngineResult
from core.task_model import DownloadTask
from utils.logger import logger
from utils.config_manager import config


class Aria2Engine(BaseEngine):
    """Aria2 下载引擎 - 多线程加速下载"""
    
    # 支持的直链文件扩展名
    DIRECT_EXTENSIONS = (
        '.mp4', '.flv', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.webm',
        '.m4v', '.3gp', '.mpg', '.mpeg', '.f4v'
    )
    
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
    
    def download(self, task: DownloadTask, progress_callback) -> bool:
        """执行多线程加速下载"""
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

            task.process = process

            # ``read_loop`` drives cancellation and PIPE draining; ``_parse_line``
            # reads per-call state off the thread-local slot.
            self._tls.progress_callback = progress_callback
            try:
                result: EngineResult = self.read_loop(process, task, self._parse_line)
            finally:
                self._tls.progress_callback = None

            if result.status in {"stopped", "switched", "paused"}:
                # Stop requests surface as ``False`` to match the legacy
                # download() contract; DownloadManager classifies them via
                # ``task.stop_reason``.
                return False

            success = result.status == "ok"

            if success:
                logger.info(f"[Aria2] 下载成功: {task.filename}")
            else:
                returncode = (
                    result.returncode if result.returncode is not None else -1
                )
                logger.error(f"[Aria2] 下载失败: {task.filename}, 退出码: {returncode}")
                logger.error("[Aria2] 建议检查直链有效性或 Referer/Cookie")
            
            return success
            
        except Exception as e:
            logger.error(f"[Aria2] 下载异常: {e}")
            task.error_message = str(e)
            return False

    def _parse_line(self, stream_tag: str, text: str) -> None:
        """``read_loop`` callback: log + parse one aria2 output line."""
        line = text.strip()
        if not line:
            return
        logger.debug(f"[Aria2] {line}")
        progress_data = self.parse_progress(line)
        if progress_data['progress'] > 0 or progress_data['speed']:
            progress_callback = getattr(self._tls, "progress_callback", None)
            if progress_callback is not None:
                progress_callback(progress_data)
    
    def _build_command(self, task: DownloadTask) -> list:
        """构建下载命令"""
        max_conn = config.get("engines.aria2.max_connection_per_server", 16)
        split = config.get("engines.aria2.split", 16)
        # Task 27.3: speed_limit unit is MB/s (mebibytes per second).
        # A value of 0 means "no limit" — do NOT add the flag at all.
        speed_limit = config.get("speed_limit", 0)
        retry_count = config.get("engines.n_m3u8dl_re.retry_count", 5)  # 使用统一的重试次数配置
        
        cmd = [
            self.binary_path,
            task.url,
            '-d', task.save_dir,
            '-o', task.filename,
            '--max-connection-per-server', str(max_conn),
            '--split', str(split),
            '--min-split-size', '1M',
            '--max-tries', str(retry_count),
            '--retry-wait', '3',
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
        
        # 添加请求头
        if task.headers.get('user-agent'):
            cmd.extend(['--user-agent', task.headers['user-agent']])
        
        if task.headers.get('referer'):
            cmd.extend(['--referer', task.headers['referer']])
        
        if task.headers.get('cookie'):
            cmd.extend(['--header', f'Cookie: {task.headers["cookie"]}'])
        
        return cmd
    
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
