"""
M3U8 Video Sniffer application entry.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow
from utils.logger import logger


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="M3U8 Video Sniffer")
    parser.add_argument("--url", type=str, help="Video URL from external handlers")
    parser.add_argument("--headers", type=str, help="Request headers in JSON")
    parser.add_argument("--filename", type=str, help="Output filename")
    return parser.parse_args()


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
            headers = {}
            if args.headers:
                try:
                    headers = json.loads(args.headers)
                except json.JSONDecodeError:
                    logger.warning(f"[CLI] {TR('log_cli_headers_error')}: {args.headers}")

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
