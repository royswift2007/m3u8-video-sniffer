"""
Streamlink engine wrapper for live streaming downloads
"""
import subprocess
import re
from pathlib import Path
from engines.base_engine import BaseEngine
from core.task_model import DownloadTask
from utils.logger import logger


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
    
    def download(self, task: DownloadTask, progress_callback) -> bool:
        """执行直播流录制"""
        try:
            cmd = self._build_command(task)
            logger.info(f"[Streamlink] 开始录制直播: {task.filename}")
            logger.debug(f"命令: {' '.join(cmd)}")
            
            # Windows 下隐藏 cmd 窗口
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
            output_lines = []
            
            for line in process.stdout:
                line = line.strip()
                if line:
                    output_lines.append(line)
                    logger.debug(f"[Streamlink] {line}")
                    progress_data = self.parse_progress(line)
                    if progress_data['downloaded'] or progress_data['speed']:
                        progress_callback(progress_data)
            
            returncode = process.wait()
            success = returncode == 0
            
            if success:
                logger.info(f"[Streamlink] 录制完成: {task.filename}")
            else:
                logger.error(f"[Streamlink] 录制失败: {task.filename}, 退出码: {returncode}")
                tail_lines = output_lines[-20:] if output_lines else []
                if tail_lines:
                    logger.error("[Streamlink] 输出尾部(20行):\n" + "\n".join(tail_lines))
                reason, suggestions = self._diagnose_failure("\n".join(output_lines).lower())
                if reason:
                    logger.warning(f"[Streamlink] 失败原因: {reason}")
                if suggestions:
                    logger.warning("[Streamlink] 建议: " + "；".join(suggestions))
            
            return success
            
        except Exception as e:
            logger.error(f"[Streamlink] 录制异常: {e}")
            task.error_message = str(e)
            return False
    
    def _diagnose_failure(self, output_text: str) -> tuple:
        """根据输出日志推断失败原因并给出建议"""
        if not output_text:
            return "无输出", ["检查网络连接或直播间是否可访问"]

        text = output_text.lower()
        suggestions = []
        reason = ""

        if "404" in text or "not found" in text:
            reason = "直播地址不存在或已下播"
            suggestions.extend(["确认直播间仍在直播", "稍后重试"])
        elif "403" in text or "forbidden" in text:
            reason = "403/Forbidden（可能鉴权/防盗链）"
            suggestions.extend(["检查 Referer/UA 是否正确", "导出并配置 cookies"])
        elif "401" in text or "unauthorized" in text:
            reason = "401/Unauthorized（需要登录）"
            suggestions.extend(["导出 cookies 并放入 cookies 目录", "使用已登录账号"])
        elif "geo" in text or "not available" in text or "blocked" in text:
            reason = "地理限制/地区不可用"
            suggestions.extend(["使用代理/VPN", "更换节点后重试"])
        elif "plugin error" in text or "no plugin" in text:
            reason = "无法识别平台插件"
            suggestions.extend(["更新 streamlink", "确认链接为直播页面"])
        elif "timeout" in text or "timed out" in text:
            reason = "网络超时"
            suggestions.extend(["检查网络稳定性", "稍后重试"])

        return reason, suggestions

    def _build_command(self, task: DownloadTask) -> list:
        """构建录制命令"""
        # 直播流通常保存为 .ts 或 .flv 格式
        output_file = Path(task.save_dir) / f'{task.filename}.ts'
        
        cmd = [
            self.binary_path,
            task.url,
            'best',  # 最佳质量
            '-o', str(output_file),
            '--force',  # 覆盖现有文件
        ]
        
        # 添加请求头
        if task.headers.get('user-agent'):
            cmd.extend(['--http-header', f'User-Agent={task.headers["user-agent"]}'])
        
        if task.headers.get('cookie'):
            cmd.extend(['--http-cookie', task.headers['cookie']])
        
        if task.headers.get('referer'):
            cmd.extend(['--http-header', f'Referer={task.headers["referer"]}'])
        
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
