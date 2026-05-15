"""
R3 smoke check — ``BaseEngine.log_command`` scrubs sensitive argv elements.

Wires a :class:`logging.Handler` into the shared project ``logger`` (the
thin wrapper that :mod:`utils.logger` exposes under the ``"M3U8Sniffer"``
name) so we can see exactly what :meth:`BaseEngine.log_command` writes,
then issues a command line that packs a cookies file path, a token query
parameter, and a secret file name. The assertion is intentionally strict:
the emitted text MUST contain ``<redacted>`` AND MUST NOT contain any
portion of the original sensitive values.

Offline, synchronous, exits 0 on success / 1 on failure.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.task_model import DownloadTask  # noqa: E402
from engines.base_engine import BaseEngine  # noqa: E402


class _TestEngine(BaseEngine):
    """Minimal concrete :class:`BaseEngine` subclass for the smoke test.

    None of the abstract methods are exercised — only :meth:`log_command`.
    They are given trivial stubs purely to satisfy the ABC registration.
    """

    def download(self, task: DownloadTask, progress_callback: Callable) -> bool:  # noqa: ARG002
        return True

    def parse_progress(self, line: str) -> dict:  # noqa: ARG002
        return {}

    def can_handle(self, url: str) -> bool:  # noqa: ARG002
        return False

    def get_name(self) -> str:
        return "smoke_engine"


class _CaptureHandler(logging.Handler):
    """Records every formatted log record so we can scan it end-to-end."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            self.messages.append(self.format(record))
        except Exception:  # pragma: no cover - defensive
            self.messages.append(record.getMessage())


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def main() -> int:
    # Attach the capture handler to the underlying stdlib logger used by
    # :mod:`utils.logger`. Name must match ``Logger.__init__`` ("M3U8Sniffer").
    stdlib = logging.getLogger("M3U8Sniffer")
    prev_level = stdlib.level
    stdlib.setLevel(logging.DEBUG)
    capture = _CaptureHandler()
    capture.setFormatter(logging.Formatter("%(message)s"))
    stdlib.addHandler(capture)

    try:
        engine = _TestEngine(binary_path="yt-dlp")
        argv = [
            "yt-dlp",
            "--cookies",
            "/tmp/secret.txt",
            "https://example.com/path?token=abc&sign=xyz",
        ]
        engine.log_command(argv)

        joined = "\n".join(capture.messages)
        _assert(
            "<redacted>" in joined,
            f"expected <redacted> sentinel in log output, got: {joined!r}",
        )
        # The cookies file path must be entirely scrubbed.
        _assert(
            "/tmp/secret.txt" not in joined,
            f"cookies file path leaked into log: {joined!r}",
        )
        # The sensitive query values must be scrubbed.
        _assert(
            "abc" not in joined,
            f"token query value 'abc' leaked into log: {joined!r}",
        )
        _assert(
            "xyz" not in joined,
            f"sign query value 'xyz' leaked into log: {joined!r}",
        )
        # Sanity: the non-sensitive URL structure should still appear so
        # the log stays useful for diagnostics.
        _assert(
            "https://example.com/path" in joined,
            f"URL base should still be visible for diagnostics: {joined!r}",
        )

        print("PASS smoke_log_redaction: argv redaction scrubbed all secrets")
        return 0
    finally:
        stdlib.removeHandler(capture)
        stdlib.setLevel(prev_level)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL smoke_log_redaction: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"FAIL smoke_log_redaction: unexpected error {exc!r}", file=sys.stderr)
        raise SystemExit(1)
