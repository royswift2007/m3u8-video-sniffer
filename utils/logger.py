"""
Logging utility for the application
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

from core.app_paths import get_logs_dir
from utils.log_retention import CapacityManagedFileHandler


class DailyRolloverMixin:
    """Mixin that rolls the underlying log file over on each new calendar day.

    Contract (Task 28.3 / Requirement 35):

    * Tracks ``_current_day: datetime.date`` set in ``__init__`` via
      ``date.today()``.
    * On each :meth:`emit`, BEFORE delegating to ``super().emit``, compares
      ``date.today()`` with ``_current_day`` and, if different, invokes
      :meth:`doRollover` (which archives the active file as
      ``<base>_YYYYMMDD<ext>`` using the day being archived) and updates
      ``_current_day``.
    * Is fully independent of the R13 capacity-based ``_maybe_rotate`` path:
      both execute within the same ``emit`` in order — daily check first,
      then ``super().emit`` (which writes and performs the throttled capacity
      check). Each path is idempotent.
    * When the new file cannot be created (rename failure, OS error), emits
      a one-line stderr warning and keeps writing to the existing open
      stream; ``_current_day`` is still advanced so the rollover is not
      retried on every subsequent emit within the same day.

    Preserves the underlying handler's constructor signature; all ``*args``
    and ``**kwargs`` are forwarded to ``super().__init__``.
    """

    # These attributes are populated by the base handler (FileHandler) and
    # ``CapacityManagedFileHandler`` respectively. Declared here only as
    # documentation for readers of the mixin.
    baseFilename: str
    stream: object  # type: ignore[assignment]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._current_day: date = date.today()

    # ------------------------------------------------------------------ public
    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        today = date.today()
        if today != self._current_day:
            try:
                self.doRollover()
            except Exception as exc:  # pragma: no cover - defensive
                # Keep writing to the existing stream on failure; advance the
                # day marker so we don't retry on every subsequent emit.
                try:
                    sys.stderr.write(
                        f"DailyRolloverMixin rollover failed: {exc}\n"
                    )
                except Exception:  # NOSONAR: stderr itself may be closed during interpreter shutdown; logging here would recurse into the broken handler.
                    pass
            # Always advance the marker. Even on failure, retrying every emit
            # within the same day would spam stderr and risk wedging the
            # handler. A subsequent day change will get another attempt.
            self._current_day = today
        super().emit(record)

    # ----------------------------------------------------------------- private
    def doRollover(self) -> None:
        """Archive ``baseFilename`` to ``<base>_YYYYMMDD<ext>`` and reopen.

        Uses ``_current_day`` — the day whose content is being archived —
        for the date suffix so that yesterday's log always lives under a
        filename containing yesterday's date. The active ``baseFilename``
        stays stable across rollovers so capacity-based rotation and
        downstream consumers continue to see the same on-disk path.

        If ``baseFilename`` does not exist, no rename is performed; the
        stream is simply (re)opened.

        If the archive destination already exists (for instance after a
        same-day restart that already archived yesterday), a numeric
        counter suffix is appended to avoid overwriting prior content.
        """
        base = Path(self.baseFilename)
        archive_dir = base.parent
        suffix = self._current_day.strftime("%Y%m%d")
        archive_path = archive_dir / f"{base.stem}_{suffix}{base.suffix}"

        # Close the current stream first — Windows cannot rename an open file.
        stream = getattr(self, "stream", None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                # Stream may already be closed or detached; rollover continues
                # regardless and we cannot safely log here without recursing
                # into this very handler.
                pass
            self.stream = None  # type: ignore[assignment]

        try:
            if base.exists():
                dest = archive_path
                if dest.exists():
                    counter = 1
                    while True:
                        dest = archive_dir / (
                            f"{base.stem}_{suffix}.{counter}{base.suffix}"
                        )
                        if not dest.exists():
                            break
                        counter += 1
                os.replace(str(base), str(dest))
        finally:
            # Reopen the (now absent) baseFilename so logging continues.
            # ``FileHandler._open()`` honours mode ``'a'`` / encoding set at
            # construction time.
            try:
                self.stream = self._open()  # type: ignore[attr-defined]
            except OSError:
                # Leave ``self.stream`` None; ``FileHandler.emit`` will try
                # to re-open on next write via ``_open``. Report once.
                try:
                    sys.stderr.write(
                        "DailyRolloverMixin reopen failed for "
                        f"{self.baseFilename}\n"
                    )
                except Exception:  # NOSONAR: stderr may be closed; we must not recurse into this handler while it is mid-reopen.
                    pass

        # Invalidate any cached directory listing from
        # CapacityManagedFileHandler so the freshly created archive is
        # counted on the next capacity check.
        if hasattr(self, "_cached_listing"):
            self._cached_listing = None  # type: ignore[attr-defined]
            self._cached_listing_mtime = -1.0  # type: ignore[attr-defined]


class DailyCapacityManagedFileHandler(
    DailyRolloverMixin, CapacityManagedFileHandler
):
    """CapacityManagedFileHandler with cross-midnight daily rollover.

    Inherits the throttled capacity-based rotation from
    :class:`CapacityManagedFileHandler` and adds date-driven rollover from
    :class:`DailyRolloverMixin`. Both paths execute within ``emit`` in
    order: daily rollover first, then capacity check inside the parent
    ``emit``. Each is independently idempotent.
    """


class Logger:
    """日志管理器"""

    def __init__(self, name: str = "M3U8Sniffer", log_file: str | Path | None = None):
        self._ensure_utf8_console()
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        # 避免重复添加 handler
        if not self.logger.handlers:
            # 控制台输出
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_format = logging.Formatter(
                '[%(levelname)s] %(message)s'
            )
            console_handler.setFormatter(console_format)
            self.logger.addHandler(console_handler)

            # 文件输出:使用稳定 baseFilename,按日期切换由 DailyRolloverMixin 负责。
            # 历史上这里把当日日期写死进文件名,导致进程跨零点时不会切换文件(R35)。
            if log_file is None:
                log_dir = get_logs_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                log_file = log_dir / "m3u8sniffer.log"

            file_handler = DailyCapacityManagedFileHandler(
                log_file, encoding='utf-8'
            )
            file_handler.setLevel(logging.DEBUG)
            file_format = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(file_format)
            self.logger.addHandler(file_handler)

    @staticmethod
    def _ensure_utf8_console():
        """Best-effort UTF-8 console output on Windows to reduce mojibake."""
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is None:
                continue
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError, AttributeError):
                    # Keep default console settings if reconfigure is unavailable.
                    pass

    def _format_kv(self, **kwargs) -> str:
        if not kwargs:
            return ""
        parts = []
        for key in sorted(kwargs):
            value = kwargs.get(key)
            if value is None:
                continue
            text = str(value).replace("\n", " ").replace("\r", " ").replace("\t", " ")
            parts.append(f"{key}={text}")
        return " " + " ".join(parts) if parts else ""

    def debug(self, message: str, **kwargs):
        self.logger.debug(f"{message}{self._format_kv(**kwargs)}")

    def info(self, message: str, **kwargs):
        self.logger.info(f"{message}{self._format_kv(**kwargs)}")

    def warning(self, message: str, **kwargs):
        self.logger.warning(f"{message}{self._format_kv(**kwargs)}")

    def error(self, message: str, **kwargs):
        self.logger.error(f"{message}{self._format_kv(**kwargs)}")

    def critical(self, message: str, **kwargs):
        self.logger.critical(f"{message}{self._format_kv(**kwargs)}")


# 全局日志实例
logger = Logger()
