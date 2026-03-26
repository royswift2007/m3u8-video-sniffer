"""
Engine selector for intelligently choosing the best download engine
"""
from engines.base_engine import BaseEngine
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine
from engines.ytdlp_engine import YtdlpEngine
from engines.streamlink_engine import StreamlinkEngine
from engines.aria2_engine import Aria2Engine
from utils.logger import logger


class EngineSelector:
    """智能引擎选择器"""
    
    def __init__(self, engines: list[BaseEngine]):
        self.engines = engines
        self._engine_map = {engine.get_name(): engine for engine in engines}

    def _get_priority_order(self) -> list[type[BaseEngine]]:
        """引擎优先级顺序"""
        return [
            N_m3u8DL_RE_Engine,
            StreamlinkEngine,
            Aria2Engine,
            YtdlpEngine  # 万能兜底
        ]

    def get_candidates(self, url: str) -> list[tuple[BaseEngine, str]]:
        """按优先级返回可用引擎列表"""
        if not self.engines:
            return []
        candidates = []
        priority_order = self._get_priority_order()
        for engine_class in priority_order:
            for engine in self.engines:
                if isinstance(engine, engine_class) and engine.can_handle(url):
                    engine_name = engine.get_name()
                    candidates.append((engine, engine_name))
        if not candidates and self.engines:
            fallback = self.engines[0]
            candidates.append((fallback, fallback.get_name()))
        return candidates
    
    def select(self, url: str, user_preference: str = None) -> tuple[BaseEngine, str]:
        """
        智能选择引擎
        
        Args:
            url: 资源 URL
            user_preference: 用户在全局 UI 中指定的引擎名称（None = 自动选择）
        
        Returns:
            (engine, engine_name) 元组
        """
        # 1️⃣ 优先使用用户指定的引擎
        if user_preference and user_preference in self._engine_map:
            preferred_engine = self._engine_map[user_preference]
            # 检查是否能处理该 URL
            if preferred_engine.can_handle(url):
                logger.info(f"使用用户指定的引擎: {user_preference}")
                return preferred_engine, user_preference
            else:
                # ⚠️ 用户指定的引擎无法处理，回退到自动选择
                logger.warning(f"{user_preference} 无法处理该 URL，自动选择引擎...")
        
        # 2️⃣ 自动选择：按优先级匹配
        candidates = self.get_candidates(url)
        if not candidates:
            raise RuntimeError("无可用下载引擎，请检查引擎配置或二进制文件")
        engine, engine_name = candidates[0]
        logger.info(f"自动选择引擎: {engine_name}")
        return engine, engine_name
    
    def get_engine_by_name(self, name: str) -> BaseEngine:
        """根据名称获取引擎"""
        return self._engine_map.get(name)
    
    def list_available_engines(self) -> list[str]:
        """列出所有可用引擎"""
        return list(self._engine_map.keys())
