"""
M3U8 Video Sniffer application entry.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow
from utils.logger import logger

# ---------------------------------------------------------------------------
# R33 — CLI validation constants (Stage 4 / P3-9)
# ---------------------------------------------------------------------------

#: Maximum accepted length for ``--url``. Matches the R6 value-byte ceiling
#: so a CLI URL never exceeds what a downstream header / argv pipeline can
#: carry without truncation.
MAX_CLI_URL_LEN: int = 4096

#: Per R33.1 only HTTP(S) URLs may be handed to the engine pipeline. Any
#: other scheme (``file://``, ``javascript:``, ``data:``, ``ftp://`` …)
#: is refused with ``parser.error`` which exits with code 2.
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _validate_url(parser: argparse.ArgumentParser, url: str) -> str:
    """Enforce R33.1 scheme / length + SSRF defence-in-depth on ``--url``.

    The checks run in this order so the cheapest predicate fails first
    and we never feed unbounded input to :func:`urllib.parse.urlsplit`:

    1. Length ≤ :data:`MAX_CLI_URL_LEN` (R33.1).
    2. Parseable as a URL (any ``ValueError`` from ``urlsplit`` rejects).
    3. Scheme in ``{http, https}`` (R33.1 — blocks ``file://``,
       ``javascript:`` etc.).
    4. :func:`utils.ssrf_guard.ensure_public` resolves and vets every IP
       (defence-in-depth — also the design §1.2 hook for ``main.py``).

    Any failure routes through :meth:`ArgumentParser.error`, which prints
    a short ``prog: error: <msg>`` line to stderr and calls
    ``sys.exit(2)`` before QApplication is ever constructed — satisfying
    R33.3 ("不启动 UI").
    """

    if len(url) > MAX_CLI_URL_LEN:
        # Deliberately do NOT echo the URL; it may itself be a DoS attempt.
        parser.error(
            f"--url too long (len={len(url)}, max={MAX_CLI_URL_LEN})"
        )

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        parser.error(f"--url invalid: {exc}")

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        # Only the scheme token is echoed — never the path/query, so a
        # ``javascript:alert(document.cookie)`` payload does not land in
        # the shell history of whoever is reading stderr.
        parser.error(f"--url scheme must be http/https (got {scheme!r})")

    # SSRF guard is imported lazily so plain ``python main.py --help``
    # does not pay the socket-module import cost (and so unit tests can
    # monkey-patch ``utils.ssrf_guard`` before the first call).
    from utils import ssrf_guard

    try:
        ssrf_guard.ensure_public(url)
    except ssrf_guard.SSRFBlocked as exc:
        # ``exc.reason`` is a short machine-readable tag
        # (``scheme_not_allowed``, ``ip_in_blocklist`` …) — safe for
        # stderr. The full URL is already logged by the guard internals.
        parser.error(f"--url rejected by SSRF guard: {exc.reason}")

    return url


def _parse_and_sanitize_headers(raw: str | None) -> dict[str, str]:
    """R33.2 — decode ``--headers`` JSON and filter through the R6 allowlist.

    Mirrors the CatCatch HTTP ``/download`` pipeline (drop + warn) so the
    two entry points agree on which headers are forwardable. Anything
    that does not survive :func:`utils.headers.sanitize_headers` is
    dropped silently (with a structured warning emitted by the helper
    itself) rather than rejected via ``parser.error`` — matching the
    user-facing behaviour of the extension path.

    Returns an empty dict on missing / malformed / non-object payloads.
    """

    if not raw:
        return {}

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Do not echo ``raw`` — it may contain a deliberately malformed
        # payload with sensitive substrings (cookies a user pasted).
        logger.warning(
            f"[CLI] --headers JSON parse failed, ignoring: {exc}",
            event="cli_headers_json_error",
            stage="parse_args",
            error_type=type(exc).__name__,
        )
        return {}

    if not isinstance(decoded, dict):
        logger.warning(
            "[CLI] --headers must decode to a JSON object, ignoring",
            event="cli_headers_not_object",
            stage="parse_args",
            type=type(decoded).__name__,
        )
        return {}

    # Imported lazily so ``utils.headers`` (which pulls in ``utils.logger``)
    # can be monkey-patched in tests that exercise ``parse_args`` in
    # isolation.
    from utils.headers import sanitize_headers

    return sanitize_headers(decoded)


def parse_args():
    """Parse command line arguments.

    Beyond the raw argparse wiring this also enforces R33:

    * ``--url`` is scheme- / length- / SSRF-validated via
      :func:`_validate_url`; failure exits with code 2 before any UI
      module is initialised.
    * ``--headers`` is JSON-decoded and filtered through the R6
      forwardable-header allowlist (see
      :func:`_parse_and_sanitize_headers`); the resulting ``args.headers``
      is a ``dict[str, str]`` rather than the raw JSON string, so
      downstream callers can use it directly.
    """

    parser = argparse.ArgumentParser(description="M3U8 Video Sniffer")
    parser.add_argument("--url", type=str, help="Video URL from external handlers")
    parser.add_argument("--headers", type=str, help="Request headers in JSON")
    parser.add_argument("--filename", type=str, help="Output filename")
    args = parser.parse_args()

    # R33.1 / R33.3: scheme + length + SSRF guard on --url.
    if args.url:
        args.url = _validate_url(parser, args.url)

    # R33.2: allowlist + length-check headers via utils.headers.sanitize_headers.
    # args.headers is replaced with a cleaned dict so main() no longer
    # needs its own json.loads path.
    args.headers = _parse_and_sanitize_headers(args.headers)

    return args


def _merge_chromium_flags():
    """Add anti-automation chromium flag without overwriting existing flags."""
    chromium_flag = "--disable-blink-features=AutomationControlled"
    existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if chromium_flag not in existing_flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{existing_flags} {chromium_flag}".strip()


def main():
    """Application main."""
    args = parse_args()
    _merge_chromium_flags()

    app = QApplication(sys.argv)
    app.setApplicationName("M3U8 Video Sniffer")
    app.setOrganizationName("M3U8VideoSniffer")

    icon_path = Path(__file__).parent / "resources" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    try:
        from utils.i18n import TR, i18n
        from utils.config_manager import config
        
        # Ensure initial logs use the configured language
        i18n.set_language(config.get("language", "zh"))
        
        logger.info("=" * 60)
        logger.info(TR("log_ready"))
        logger.info("=" * 60)

        window = MainWindow()
        window.show()

        if args.url:
            logger.info(f"[CLI] {TR('log_cli_received_url')}: {args.url}")
            # R33.2: args.headers is already sanitized by parse_args()
            # (dict[str, str] filtered through the R6 allowlist). No extra
            # json.loads here — malformed payloads were logged upstream.
            headers = args.headers

            def add_external_resource():
                from core.engine_selector import EngineSelector
                from core.task_model import M3U8Resource

                resource = M3U8Resource(
                    url=args.url,
                    headers=headers,
                    page_url=args.url,
                    title=args.filename or TR("label_ext_download"),
                )

                selector = EngineSelector(window.engines)
                _, engine_name = selector.select(args.url, None)
                window.resource_panel.add_resource(resource, engine_name)
                window.main_tabs.setCurrentIndex(1)
                logger.info(f"[CLI] {TR('log_cli_resource_added')}: {args.filename or args.url}")

            QTimer.singleShot(500, add_external_resource)

        sys.exit(app.exec())

    except Exception as e:
        from utils.i18n import TR
        logger.critical(
            f"{TR('msg_init_failed', error=str(e))}",
            event="app_start_failed",
            stage="main",
            error_type=type(e).__name__,
        )
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
