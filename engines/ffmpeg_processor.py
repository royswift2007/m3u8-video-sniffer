"""
FFmpeg processor for post-download video processing
"""
import subprocess
from pathlib import Path
from utils.logger import logger


class FFmpegProcessor:
    """FFmpeg 后处理器 - 转码、合并、压缩"""
    
    def __init__(self, binary_path: str):
        self.binary_path = binary_path
    
    def convert_to_mp4(self, input_file: str, output_file: str, remove_source: bool = False) -> bool:
        """
        转换为 MP4（无损转封装）
        
        Args:
            input_file: 输入文件路径
            output_file: 输出文件路径
            remove_source: 是否删除源文件
        
        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                '-i', input_file,
                '-c', 'copy',  # 不重新编码，直接复制流
                '-y',  # 覆盖输出文件
                output_file
            ]
            
            logger.info(f"[FFmpeg] 转换为 MP4: {Path(input_file).name}")
            
            # Windows 下隐藏 cmd 窗口
            creation_flags = 0
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                creationflags=creation_flags
            )
            
            if process.returncode == 0:
                logger.info(f"[FFmpeg] 转换成功: {Path(output_file).name}")
                if remove_source and Path(input_file).exists():
                    Path(input_file).unlink()
                    logger.debug(f"已删除源文件: {input_file}")
                return True
            else:
                logger.error(f"[FFmpeg] 转换失败: {process.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"[FFmpeg] 转换异常: {e}")
            return False
    
    def merge_video_audio(self, video_file: str, audio_file: str, output_file: str, remove_sources: bool = False) -> bool:
        """
        合并独立的视频和音频流
        
        Args:
            video_file: 视频文件路径
            audio_file: 音频文件路径
            output_file: 输出文件路径
            remove_sources: 是否删除源文件
        
        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                '-i', video_file,
                '-i', audio_file,
                '-c', 'copy',
                '-y',
                output_file
            ]
            
            logger.info(f"[FFmpeg] 合并音视频: {Path(output_file).name}")
            
            # Windows 下隐藏 cmd 窗口
            creation_flags = 0
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                creationflags=creation_flags
            )
            
            if process.returncode == 0:
                logger.info(f"[FFmpeg] 合并成功: {Path(output_file).name}")
                if remove_sources:
                    if Path(video_file).exists():
                        Path(video_file).unlink()
                    if Path(audio_file).exists():
                        Path(audio_file).unlink()
                    logger.debug("已删除源文件")
                return True
            else:
                logger.error(f"[FFmpeg] 合并失败: {process.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"[FFmpeg] 合并异常: {e}")
            return False
    
    def extract_subtitles(self, input_file: str, output_srt: str, stream_index: int = 0) -> bool:
        """
        提取内嵌字幕
        
        Args:
            input_file: 输入文件路径
            output_srt: 输出字幕文件路径
            stream_index: 字幕流索引（默认第一条）
        
        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                '-i', input_file,
                '-map', f'0:s:{stream_index}',  # 选择字幕流
                '-y',
                output_srt
            ]
            
            logger.info(f"[FFmpeg] 提取字幕: {Path(output_srt).name}")
            
            # Windows 下隐藏 cmd 窗口
            creation_flags = 0
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                creationflags=creation_flags
            )
            
            if process.returncode == 0:
                logger.info(f"[FFmpeg] 字幕提取成功")
                return True
            else:
                logger.warning(f"[FFmpeg] 字幕提取失败（可能无字幕）: {process.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"[FFmpeg] 字幕提取异常: {e}")
            return False
    
    def compress_video(self, input_file: str, output_file: str, crf: int = 23) -> bool:
        """
        压缩视频
        
        Args:
            input_file: 输入文件路径
            output_file: 输出文件路径
            crf: 压缩质量 (18-28, 越小质量越好)
        
        Returns:
            bool: 是否成功
        """
        try:
            cmd = [
                self.binary_path,
                '-i', input_file,
                '-c:v', 'libx264',
                '-crf', str(crf),
                '-c:a', 'aac',
                '-y',
                output_file
            ]
            
            logger.info(f"[FFmpeg] 压缩视频: {Path(input_file).name}")
            
            # Windows 下隐藏 cmd 窗口
            creation_flags = 0
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                creationflags=creation_flags
            )
            
            if process.returncode == 0:
                logger.info(f"[FFmpeg] 压缩成功: {Path(output_file).name}")
                return True
            else:
                logger.error(f"[FFmpeg] 压缩失败: {process.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"[FFmpeg] 压缩异常: {e}")
            return False
