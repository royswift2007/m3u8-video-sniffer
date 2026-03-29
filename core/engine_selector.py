"""
Engine selector for intelligently choosing the best download engine
"""
from typing import Optional

from engines.base_engine import BaseEngine
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine
from engines.ytdlp_engine import YtdlpEngine
from engines.streamlink_engine import StreamlinkEngine
from engines.aria2_engine import Aria2Engine
from utils.logger import logger
from utils.i18n import TR


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
            YtdlpEngine,  # 万能兜底
        ]

    def _safe_can_handle(self, engine: BaseEngine, url: str) -> bool:
        """Safely evaluate whether an engine can clearly handle the current URL."""
        try:
            return bool(engine.can_handle(url))
        except Exception as exc:
            logger.warning(
                f"{TR('log_engine_handle_exception')}: {engine.get_name()} - {exc}"
            )
            return False

    def get_candidates(self, url: str) -> list[tuple[BaseEngine, str]]:
        """按优先级返回可用引擎列表"""
        if not self.engines:
            return []
        candidates = []
        priority_order = self._get_priority_order()
        for engine_class in priority_order:
            for engine in self.engines:
                if isinstance(engine, engine_class) and self._safe_can_handle(engine, url):
                    engine_name = engine.get_name()
                    candidates.append((engine, engine_name))
        if not candidates and self.engines:
            fallback = self.engines[0]
            candidates.append((fallback, fallback.get_name()))
        return candidates

    def predict(
        self,
        url: str,
        user_preference: Optional[str] = None,
    ) -> tuple[BaseEngine, str]:
        """
        预测探测阶段应显示的引擎。

        设计目标：
        - 用户显式指定时，优先反映该选择；
        - 只有在 can_handle 已明确返回 False 时，才不继续显示该显式引擎；
        - URL 信息不足、识别不完整时，不因为缺少候选就武断改成别的引擎。
        """
        if user_preference and user_preference in self._engine_map:
            preferred_engine = self._engine_map[user_preference]
            if self._safe_can_handle(preferred_engine, url):
                logger.info(f"{TR('log_engine_predict_user_pref')}: {user_preference}")
                return preferred_engine, user_preference

            auto_candidates = self.get_candidates(url)
            auto_names = {name for _, name in auto_candidates}
            if auto_candidates and user_preference not in auto_names:
                engine, engine_name = auto_candidates[0]
                logger.info(
                    TR("log_engine_predict_overridden"),
                    event="predict_engine_overridden",
                    preferred_engine=user_preference,
                    predicted_engine=engine_name,
                    url=url,
                )
                return engine, engine_name

            logger.info(
                f"{TR('log_engine_predict_keep_user')}: {user_preference}",
                event="predict_engine_keep_user_preference",
                preferred_engine=user_preference,
                url=url,
            )
            return preferred_engine, user_preference

        candidates = self.get_candidates(url)
        if not candidates:
            raise RuntimeError("无可用下载引擎，请检查引擎配置或二进制文件")
        engine, engine_name = candidates[0]
        logger.info(f"{TR('log_engine_predict_auto')}: {engine_name}")
        return engine, engine_name

    def select(self, url: str, user_preference: Optional[str] = None) -> tuple[BaseEngine, str]:
        """
        智能选择引擎

        Args:
            url: 资源 URL
            user_preference: 用户在全局 UI 中指定的引擎名称（None = 自动选择）

        Returns:
            (engine, engine_name) 元组
        """
        # 1️⃣ 真正入队/执行前仍优先使用用户指定的引擎
        if user_preference and user_preference in self._engine_map:
            preferred_engine = self._engine_map[user_preference]
            logger.info(f"{TR('log_engine_use_user_pref')}: {user_preference}")
            return preferred_engine, user_preference

        # 2️⃣ 自动选择：按优先级匹配
        candidates = self.get_candidates(url)
        if not candidates:
            raise RuntimeError(TR("msg_engine_not_found_text"))
        engine, engine_name = candidates[0]
        logger.info(f"自动选择引擎: {engine_name}")
        return engine, engine_name

    def get_engine_by_name(self, name: str) -> Optional[BaseEngine]:
        """根据名称获取引擎"""
        return self._engine_map.get(name)

    def list_available_engines(self) -> list[str]:
        """列出所有可用引擎"""
        return list(self._engine_map.keys())
