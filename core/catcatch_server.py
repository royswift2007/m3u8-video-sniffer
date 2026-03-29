"""
HTTP API server for receiving download requests from CatCatch extension.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QObject, pyqtSignal

from utils.logger import logger
from utils.i18n import TR


class DownloadRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for `/download` endpoint."""

    on_download_request = None

    def log_message(self, fmt, *args):
        try:
            message = fmt % args if args else fmt
        except (TypeError, ValueError) as e:
            message = str(args[0]) if args else fmt
            logger.debug(
                f"[HTTP] log format fallback: {e}",
                event="catcatch_http_log_format_fallback",
                stage="http_log",
                error_type=type(e).__name__,
            )
        logger.debug(f"[HTTP] {message}")

    def _send_response(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self._send_response(200, {"status": "ok"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_response(
                200,
                {
                    "status": "running",
                    "name": "M3U8VideoSniffer API",
                    "endpoints": {
                        "/download": "POST - add download task",
                        "/status": "GET - server status",
                    },
                },
            )
            return

        if parsed.path == "/status":
            self._send_response(200, {"status": "running"})
            return

        if parsed.path == "/download":
            params = parse_qs(parsed.query)
            url = params.get("url", [""])[0]
            filename = params.get("name", [""])[0] or params.get("filename", [""])[0]
            if url:
                self._handle_download_request(url, {}, filename)
            else:
                self._send_response(400, {"error": "Missing 'url' parameter"})
            return

        self._send_response(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/download":
            self._send_response(404, {"error": "Not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        body_text = raw_body.decode("utf-8", errors="ignore")
        content_type = (self.headers.get("Content-Type", "") or "").lower()

        data = {}
        if body_text:
            if "application/json" in content_type:
                try:
                    data = json.loads(body_text)
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"[HTTP] invalid json body: {e}",
                        event="catcatch_http_invalid_json",
                        stage="http_parse_body",
                        error_type=type(e).__name__,
                    )
                    self._send_response(400, {"error": f"Invalid JSON: {e}"})
                    return
            else:
                parsed_form = parse_qs(body_text, keep_blank_values=True)
                data = {k: (v[0] if isinstance(v, list) and v else "") for k, v in parsed_form.items()}

        url = data.get("url", "")
        headers = data.get("headers", {})
        filename = data.get("name", "") or data.get("filename", "")

        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError as e:
                logger.warning(
                    f"[HTTP] invalid headers json: {e}",
                    event="catcatch_http_invalid_headers_json",
                    stage="http_parse_headers",
                    error_type=type(e).__name__,
                )
                headers = {}
        if not isinstance(headers, dict):
            headers = {}

        if url:
            self._handle_download_request(url, headers, filename)
        else:
            self._send_response(400, {"error": "Missing 'url' in request body"})

    def _handle_download_request(self, url: str, headers: dict, filename: str):
        logger.info(f"[HTTP] {TR('log_http_request_received')}: {url}")
        logger.debug(f"[HTTP] Headers: {headers}")
        logger.debug(f"[HTTP] Filename: {filename}")

        if not DownloadRequestHandler.on_download_request:
            self._send_response(500, {"error": "No handler registered"})
            return

        try:
            DownloadRequestHandler.on_download_request(url, headers, filename)
            self._send_response(
                200,
                {
                    "status": "success",
                    "message": TR("log_cli_resource_added"),
                    "url": url,
                },
            )
        except Exception as e:
            logger.error(
                f"[HTTP] {TR('log_http_handle_failed')}: {e}",
                event="catcatch_handle_download_failed",
                stage="http_handle_download",
                error_type=type(e).__name__,
                url=url,
            )
            self._send_response(500, {"error": str(e)})


class CatCatchServer(QObject):
    """Local HTTP API server used by CatCatch extension."""

    download_requested = pyqtSignal(str, dict, str)  # url, headers, filename

    def __init__(self, port: int = 9527):
        super().__init__()
        self.port = port
        self.server = None
        self.thread = None
        self._running = False
        self._lock = threading.Lock()
        self._start_event = threading.Event()
        self._start_error = ""
        DownloadRequestHandler.on_download_request = self._on_request

    def _on_request(self, url: str, headers: dict, filename: str):
        self.download_requested.emit(url, headers, filename)

    def start(self):
        """Start server in a background thread."""
        with self._lock:
            if self._running:
                return
            self._start_event.clear()
            self._start_error = ""

        self.thread = threading.Thread(target=self._run_server, daemon=True, name="CatCatchHTTPServer")
        self.thread.start()

        self._start_event.wait(timeout=3.0)

        with self._lock:
            running = self._running
            current_port = self.port
            start_error = self._start_error

        if running:
            logger.info(f"{TR('log_catcatch_started').replace('{url}', f'http://localhost:{current_port}')}")
        else:
            if not start_error:
                start_error = "startup timeout"
            logger.error(
                f"[CatCatch] {TR('log_catcatch_start_failed')}: {start_error}",
                event="catcatch_start_failed",
                stage="server_start",
            )

    def _run_server(self):
        requested_port = self.port
        candidate_ports = [requested_port] + [p for p in range(9528, 9540) if p != requested_port]

        for port in candidate_ports:
            try:
                server = HTTPServer(("127.0.0.1", port), DownloadRequestHandler)
            except OSError as e:
                if port == requested_port:
                    logger.warning(f"[CatCatch] {TR('log_catcatch_port_occupied').replace('{port}', str(port))}: {e}")
                else:
                    logger.debug(f"[CatCatch] {TR('log_catcatch_port_unavailable').replace('{port}', str(port))}: {e}")
                continue
            except Exception as e:
                logger.error(
                    f"[CatCatch] {TR('log_catcatch_create_failed').replace('{port}', str(port))}: {e}",
                    event="catcatch_create_server_failed",
                    stage="server_create",
                    error_type=type(e).__name__,
                    port=port,
                )
                continue

            with self._lock:
                self.server = server
                self.port = port
                self._running = True
                self._start_error = ""
            self._start_event.set()

            try:
                server.serve_forever()
            except Exception as e:
                logger.error(
                    f"[CatCatch] {TR('log_catcatch_runtime_exception').replace('{port}', str(port))}: {e}",
                    event="catcatch_server_runtime_failed",
                    stage="serve_forever",
                    error_type=type(e).__name__,
                    port=port,
                )
            finally:
                try:
                    server.server_close()
                except Exception as e:
                    logger.debug(
                        f"[CatCatch] server_close 异常(port={port}): {e}",
                        event="catcatch_server_close_error",
                        stage="server_close",
                        error_type=type(e).__name__,
                        port=port,
                    )
                with self._lock:
                    self.server = None
                    self._running = False
            return

        with self._lock:
            self._running = False
            self.server = None
            self._start_error = "no available ports in 9527-9539"
        self._start_event.set()

    def stop(self):
        """Stop server and wait for thread exit."""
        with self._lock:
            server = self.server

        if server:
            try:
                server.shutdown()
            except Exception as e:
                logger.warning(
                    f"[CatCatch] shutdown 失败: {e}",
                    event="catcatch_shutdown_failed",
                    stage="server_shutdown",
                    error_type=type(e).__name__,
                )
            try:
                server.server_close()
            except Exception as e:
                logger.debug(
                    f"[CatCatch] stop/server_close 异常: {e}",
                    event="catcatch_stop_server_close_error",
                    stage="server_close",
                    error_type=type(e).__name__,
                )

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)

        with self._lock:
            self.server = None
            self._running = False

        logger.info(f"[CatCatch] {TR('log_catcatch_stopped')}")

    def is_running(self) -> bool:
        with self._lock:
            return self._running and self.server is not None

    def get_url(self) -> str:
        return f"http://localhost:{self.port}/download"
