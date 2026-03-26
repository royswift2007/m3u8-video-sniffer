"""
Base engine class for all download engines
"""
from abc import ABC, abstractmethod
from typing import Callable
from core.task_model import DownloadTask


class BaseEngine(ABC):
    """下载引擎抽象基类"""
    
    def __init__(self, binary_path: str):
        self.binary_path = binary_path
    
    @abstractmethod
    def download(self, task: DownloadTask, progress_callback: Callable) -> bool:
        """
        执行下载任务
        
        Args:
            task: 下载任务对象
            progress_callback: 进度回调函数，接收 dict 参数
                               {"progress": float, "speed": str, "downloaded": str}
        
        Returns:
            bool: 下载是否成功
        """
        pass
    
    @abstractmethod
    def parse_progress(self, line: str) -> dict:
        """
        解析输出行，提取进度信息
        
        Args:
            line: 进程输出的一行文本
        
        Returns:
            dict: {"progress": float, "speed": str, "downloaded": str}
        """
        pass
    
    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """
        判断该引擎是否能处理此 URL
        
        Args:
            url: 资源 URL
        
        Returns:
            bool: 是否支持
        """
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """获取引擎名称"""
        pass
