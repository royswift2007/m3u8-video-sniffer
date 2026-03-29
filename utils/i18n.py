"""
Internationalization (i18n) Manager for the application.
"""

from PyQt6.QtCore import QObject, pyqtSignal
from utils.i18n_data import TRANSLATIONS
from utils.logger import logger

class I18nManager(QObject):
    """
    Singleton manager for translations.
    """
    language_changed = pyqtSignal(str)
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(I18nManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self.current_language = "zh"  # Default
        self._initialized = True

    def set_language(self, lang: str):
        """Set current language and emit signal."""
        if lang not in TRANSLATIONS:
            logger.warning(f"Unsupported language: {lang}")
            return
            
        if self.current_language != lang:
            self.current_language = lang
            logger.info(f"Language changed to: {lang}")
            self.language_changed.emit(lang)

    def get_language(self) -> str:
        return self.current_language

    def TR(self, key: str, **kwargs) -> str:
        """
        Translate a key to the current language.
        Usage: TR("start_grab") or TR("log_engine_loaded", name="yt-dlp")
        """
        lang = self.current_language
        translated = TRANSLATIONS.get(lang, {}).get(key, key)
        
        if kwargs:
            try:
                return translated.format(**kwargs)
            except Exception as e:
                logger.error(f"I18n format error for key '{key}': {e}")
                return translated
                
        return translated

# Global singleton instance
i18n = I18nManager()
TR = i18n.TR
