"""
Internationalization (i18n) Manager for the application.
"""

from PyQt6.QtCore import QObject, pyqtSignal

from utils.i18n_data import TRANSLATIONS
from utils.logger import logger

# Default fallback language used when a key is missing in the current
# language. See Requirement 28 / task 26.2 for the lookup chain:
#   current language -> DEFAULT_LANG -> humanised key.
DEFAULT_LANG = "en"


def _humanize(key: str) -> str:
    """Return a human-readable form of a key.

    ``"log_queue_added"`` -> ``"Log Queue Added"``.

    Guaranteed not to raise; on any unexpected failure the raw key is
    returned unchanged so callers never see an exception from a lookup.
    """
    try:
        return key.replace("_", " ").title()
    except Exception:  # pragma: no cover - defensive, extremely unlikely
        return key


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
        """Translate ``key`` into the current language.

        Lookup chain (Requirement 28 / task 26.2):

        1. ``TRANSLATIONS[current_language][key]`` — return if present.
        2. ``TRANSLATIONS[DEFAULT_LANG][key]`` — return if present.
        3. ``key.replace("_", " ").title()`` — synthesised display string.

        Whenever we fall through step 1 (i.e. the key is missing in the
        current language), emit a single structured ``i18n_missing`` WARN
        so missing translations are observable regardless of whether the
        default-language fallback covered them.

        When ``kwargs`` are supplied, ``str.format`` is applied to the
        resolved template. Any formatting error (bad format spec, missing
        placeholder, non-string template) is swallowed and the title-case
        synthesised form is returned instead.

        This function never raises. Only the key and language are logged;
        no kwargs are echoed so sensitive values cannot leak via the i18n
        path.

        Usage: ``TR("start_grab")`` or ``TR("log_engine_loaded", name="yt-dlp")``.
        """
        try:
            lang = self.current_language
        except Exception:  # pragma: no cover - defensive
            lang = DEFAULT_LANG

        template = TRANSLATIONS.get(lang, {}).get(key)
        missed_current = template is None

        if template is None and lang != DEFAULT_LANG:
            template = TRANSLATIONS.get(DEFAULT_LANG, {}).get(key)

        if template is None:
            template = _humanize(key)

        if missed_current:
            # Structured warning; deliberately omits kwargs so user-supplied
            # values (filenames, URLs, error details) never leak via i18n.
            logger.warning(
                "i18n_missing",
                event="i18n_missing",
                lang=lang,
                key=key,
            )

        if not kwargs:
            return template

        try:
            return template.format(**kwargs)
        except Exception as exc:
            logger.warning(
                "i18n_format_error",
                event="i18n_format_error",
                lang=lang,
                key=key,
                error=str(exc),
            )
            return _humanize(key)

# Global singleton instance
i18n = I18nManager()
TR = i18n.TR
