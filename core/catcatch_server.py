"""
HTTP API server for receiving download requests from CatCatch extension.

Security hardening (Requirement 2 of ``security-stability-hardening``):
    1. The server generates a fresh ``session_token`` (192 bit,
       ``secrets.token_urlsafe(24)``) on every start and writes it to
       ``~/.m3u8d/session.token`` with owner-restricted permissions.
    2. The server binds to ``127.0.0.1`` only, via ``ThreadingHTTPServer``.
    3. Every request's ``Origin`` / ``Referer`` must match an entry in the
       code-level ``allowed_origins`` whitelist (augmented by optional
       extension origins passed to the constructor).
    4. ``POST /download`` additionally requires a matching ``X-Session-Token``
       header; mismatch returns 401.
    5. CORS responses echo the requesting Origin (never ``*``) and are emitted
       only when the Origin is whitelisted.
    6. Non-whitelisted ``OPTIONS`` preflights return 403 with NO
       ``Access-Control-Allow-*`` headers.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse, urlsplit

from PyQt6.QtCore import QObject, pyqtSignal

from utils.headers import sanitize_headers
from utils.logger import logger
from utils.i18n import TR
from utils import ssrf_guard


# Built-in allowed origins. Loopback variants only; specific browser
# extension origins (e.g. ``chrome-extension://<id>``) are layered on top
# via the ``extra_allowed_origins`` constructor argument and/or config.
DEFAULT_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    {
        "http://127.0.0.1",
        "http://localhost",
        "https://127.0.0.1",
        "https://localhost",
    }
)

# Location of the on-disk session token. The browser extension / trusted
# local clients read this file to learn the current token. The file is
# overwritten on every server start and removed on stop.
SESSION_TOKEN_FILE: Path = Path.home() / ".m3u8d" / "session.token"

# Default candidate port list for :class:`CatCatchServer`. Per R22.3 the
# default count (13) MUST be preserved when callers do not pass an
# explicit ``candidate_ports`` argument. 9527 is the primary port and
# 9528–9539 are the fallback range; together that is 13 slots.
DEFAULT_CATCATCH_PORT: int = 9527
DEFAULT_FALLBACK_PORTS: tuple[int, ...] = tuple(range(9528, 9540))

# Timeout (seconds) that :meth:`CatCatchServer.start` waits for the
# background serve thread to either bind a socket or exhaust every
# candidate port. Per R22.4 a miss of this deadline is recorded as
# ``bind_timeout`` and treated as a failed start.
START_BIND_TIMEOUT_S: float = 5.0

# Audit-finding High #2: upper bound for ``POST /download`` body size.
# The extension's normal payload is a compact JSON object (URL + headers
# + filename) that easily fits in a few KiB; any caller shipping more
# than 64 KiB is either misconfigured or hostile, so we refuse with 413
# before spending memory on ``self.rfile.read()``.
MAX_CATCATCH_BODY_BYTES: int = 64 * 1024


class PortExhausted(Exception):
    """Raised internally when every candidate port fails to bind.

    Surfaced to :meth:`CatCatchServer.start` callers via the existing
    ``_start_error`` string (not re-raised to the UI) so that a socket
    contention situation does not crash the Qt main thread. The exception
    is public so callers and tests can assert on the failure mode.
    """


# ---------------------------------------------------------------------------
# Origin / token helpers (pure functions)
# ---------------------------------------------------------------------------


def _redact_url_for_log(url: str) -> str:
    """Return ``url`` with sensitive query values scrubbed, for logging.

    Audit-finding High #3 (sensitive data in logs): ``_handle_download_request``
    used to write the raw CatCatch URL straight into the runtime log and
    echo it back in the 200 success body. Route the same value through
    :func:`utils.redact.redact_url` so tokens / signatures / auth keys in
    the query string never land on disk or in the HTTP response.
    """

    try:
        from utils.redact import redact_url

        return redact_url(url)
    except Exception:
        # Pure-function helper must never raise into the HTTP path; if
        # redaction imports fail for any reason just drop the query
        # entirely (safer than emitting the raw URL).
        head, _, _ = (url or "").partition("?")
        return head or url


def _normalize_origin(raw: str | None) -> str | None:
    """Return the ``scheme://host[:port]`` form of ``raw`` or ``None``.

    Accepts either a bare Origin value or a Referer URL. Returns ``None``
    when the input is missing/blank so callers can distinguish "no header"
    from "header present but invalid".
    """

    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return value.rstrip("/")
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    # Extensions sometimes send bare tokens such as ``null``; pass through
    # untouched so the whitelist comparison can decide.
    return value.rstrip("/")


def _build_allowed_origins(extra: Iterable[str] | None) -> frozenset[str]:
    """Return the combined built-in + user-supplied origin set."""

    base = set(DEFAULT_ALLOWED_ORIGINS)
    if not extra:
        return frozenset(base)
    for origin in extra:
        if not isinstance(origin, str):
            continue
            
        normalized = _normalize_origin(origin)
        if normalized:
            base.add(normalized)
    return frozenset(base)


def _write_session_token(path: Path, token: str) -> bool:
    """Write ``token`` to ``path`` with owner-only permissions.

    Returns ``True`` on success. Best-effort permission tightening is
    applied via ``os.chmod`` on POSIX and ``icacls`` on Windows. All
    errors are logged and swallowed so a permission hardening failure
    never prevents the server from starting.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            f"[CatCatch] 创建 session token 目录失败: {e}",
            event="catcatch_token_dir_failed",
            stage="token_write",
            error_type=type(e).__name__,
        )
        return False

    # ``O_CREAT | O_TRUNC | O_WRONLY`` with mode 0o600 ensures the file is
    # created with restrictive permissions on POSIX from the start (rather
    # than race-prone "create then chmod"). On Windows the mode is ignored
    # but the subsequent ``icacls`` call locks the DACL down.
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as e:
        logger.warning(
            f"[CatCatch] 打开 session token 文件失败: {e}",
            event="catcatch_token_open_failed",
            stage="token_write",
            error_type=type(e).__name__,
        )
        return False

    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token)
    except OSError as e:
        logger.warning(
            f"[CatCatch] 写入 session token 失败: {e}",
            event="catcatch_token_write_failed",
            stage="token_write",
            error_type=type(e).__name__,
        )
        return False

    # Best-effort permission tightening (POSIX -> chmod, Windows -> icacls).
    try:
        os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError as e:
        logger.debug(
            f"[CatCatch] chmod session token 失败: {e}",
            event="catcatch_token_chmod_failed",
            stage="token_write",
            error_type=type(e).__name__,
        )

    if sys.platform == "win32":
        _apply_windows_owner_only_dacl(path)

    return True


def _apply_windows_owner_only_dacl(path: Path) -> None:
    """Best-effort: strip ACL inheritance and leave only owner access.

    Uses ``icacls`` via subprocess. Timeouts / failures are logged at
    DEBUG and swallowed, because a DACL tightening failure should not
    prevent the server from starting.
    """

    username = os.environ.get("USERNAME") or ""
    if not username:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        subprocess.run(
            ["icacls", str(path), "/grant:r", f"{username}:(R,W)"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug(
            f"[CatCatch] icacls 限制 session token 失败: {e}",
            event="catcatch_token_icacls_failed",
            stage="token_write",
            error_type=type(e).__name__,
        )


def _remove_session_token(path: Path) -> None:
    """Delete the on-disk session token. Missing file is not an error."""

    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.debug(
            f"[CatCatch] 删除 session token 失败: {e}",
            event="catcatch_token_unlink_failed",
            stage="token_cleanup",
            error_type=type(e).__name__,
        )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class DownloadRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for the CatCatch download API.

    Class-level attributes are populated by :class:`CatCatchServer` before
    the server starts accepting connections. They are treated as read-only
    at request time (:mod:`http.server` instantiates a new handler per
    request on its own thread).
    """

    # Injected by CatCatchServer.
    on_download_request = None
    allowed_origins: frozenset[str] = DEFAULT_ALLOWED_ORIGINS
    session_token: str = ""

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

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _request_origin(self) -> str | None:
        """Return the normalized request Origin, falling back to Referer."""

        origin = _normalize_origin(self.headers.get("Origin"))
        if origin:
            return origin
        return _normalize_origin(self.headers.get("Referer"))

    def _origin_is_allowed(self, origin: str | None) -> bool:
        """True when no Origin/Referer was sent or the origin is whitelisted.

        Loopback binding is the outer gate for the "no header at all" case
        (e.g. curl / health checks). For cross-origin browser requests at
        least one of Origin/Referer will be present and is checked here.
        """

        if origin is None:
            return True
        return origin in self.allowed_origins

    def _send_response_raw(
        self,
        code: int,
        data: dict,
        *,
        origin_to_echo: str | None = None,
        include_cors_preflight: bool = False,
    ) -> None:
        """Send a JSON response.

        CORS headers are emitted only when ``origin_to_echo`` is non-empty
        (i.e. the request Origin was whitelisted). The wildcard ``*`` is
        never sent.
        """

        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if origin_to_echo:
            self.send_header("Access-Control-Allow-Origin", origin_to_echo)
            self.send_header("Vary", "Origin")
            if include_cors_preflight:
                self.send_header(
                    "Access-Control-Allow-Methods", "GET, POST, OPTIONS"
                )
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-Session-Token",
                )
                self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        self.wfile.write(payload)

    def _send_response(self, code: int, data: dict) -> None:
        """Default JSON response that echoes the Origin iff whitelisted."""

        origin = self._request_origin()
        echo = origin if origin and origin in self.allowed_origins else None
        self._send_response_raw(code, data, origin_to_echo=echo)

    def _reject_bad_origin(self) -> bool:
        """Return True (after sending 403) if the request origin fails the check."""

        origin = self._request_origin()
        if self._origin_is_allowed(origin):
            return False
        logger.warning(
            f"[HTTP] Origin 不在白名单: {origin!r}",
            event="catcatch_origin_rejected",
            stage="http_origin_check",
            method=self.command,
            path=self.path,
        )
        self._send_response_raw(
            403,
            {"error": "Origin not allowed"},
            origin_to_echo=None,
        )
        return True

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    def do_OPTIONS(self):
        """CORS preflight.

        - Whitelisted Origin  -> 200 with Allow-* headers echoing the Origin.
        - Non-whitelisted     -> 403 with NO Allow-* headers.
        """

        origin = self._request_origin()
        if origin is None or origin not in self.allowed_origins:
            self._send_response_raw(
                403,
                {"error": "Origin not allowed"},
                origin_to_echo=None,
            )
            return
        self._send_response_raw(
            200,
            {"status": "ok"},
            origin_to_echo=origin,
            include_cors_preflight=True,
        )

    def do_GET(self):
        if self._reject_bad_origin():
            return

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
            # Audit-finding High #2: ``GET /download`` used to be a write
            # mutator that bypassed the Origin + session-token gates that
            # protect ``POST /download``. Refuse it outright — the
            # browser extension and the protocol handler both use POST,
            # so no supported caller is affected. Keeping a 405 here also
            # nudges future integrations onto the authenticated path.
            logger.warning(
                "[HTTP] GET /download is not allowed; use POST with X-Session-Token",
                event="catcatch_get_download_blocked",
                stage="http_method_check",
                method=self.command,
                path=self.path,
            )
            self._send_response(405, {"error": "Method not allowed; use POST"})
            return

        self._send_response(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/download":
            if self._reject_bad_origin():
                return
            self._send_response(404, {"error": "Not found"})
            return

        # POST /download is the sensitive mutator: Origin/Referer MUST be
        # both present AND in the whitelist (bare "no Origin" is rejected,
        # which is stricter than the GET path).
        origin = self._request_origin()
        if origin is None or origin not in self.allowed_origins:
            logger.warning(
                f"[HTTP] POST /download Origin 校验失败: {origin!r}",
                event="catcatch_origin_rejected",
                stage="http_origin_check",
                method=self.command,
                path=self.path,
            )
            self._send_response_raw(
                403,
                {"error": "Origin not allowed"},
                origin_to_echo=None,
            )
            return

        # POST /download additionally requires a matching X-Session-Token.
        provided = self.headers.get("X-Session-Token", "") or ""
        expected = self.session_token
        if (
            not expected
            or not provided
            or not secrets.compare_digest(provided, expected)
        ):
            logger.warning(
                "[HTTP] X-Session-Token 缺失或不匹配",
                event="catcatch_session_token_denied",
                stage="http_auth",
                method=self.command,
                path=self.path,
                has_token=bool(provided),
            )
            # Do NOT echo CORS headers to an unauthenticated caller — even
            # though the Origin was in the whitelist, a missing token means
            # the client cannot prove it is the trusted extension.
            self._send_response_raw(
                401,
                {"error": "Unauthorized"},
                origin_to_echo=None,
            )
            return

        # Audit-finding High #2: cap the request body before ``rfile.read``.
        # An unbounded ``Content-Length`` used to let a malicious local
        # caller trigger an arbitrarily large allocation.
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length < 0:
            self._send_response(400, {"error": "Invalid Content-Length"})
            return
        if content_length > MAX_CATCATCH_BODY_BYTES:
            logger.warning(
                f"[HTTP] POST /download body too large: {content_length} bytes",
                event="catcatch_body_too_large",
                stage="http_body_limit",
                content_length=content_length,
                limit=MAX_CATCATCH_BODY_BYTES,
            )
            self._send_response(
                413,
                {
                    "error": "Request body too large",
                    "limit": MAX_CATCATCH_BODY_BYTES,
                },
            )
            return
        raw_body = self.rfile.read(content_length) if content_length else b""
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
                data = {
                    k: (v[0] if isinstance(v, list) and v else "")
                    for k, v in parsed_form.items()
                }

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
        logger.info(
            f"[HTTP] {TR('log_http_request_received')}: {_redact_url_for_log(url)}",
            event="catcatch_request_received",
            stage="http_handle_download",
            url_redacted=_redact_url_for_log(url),
        )
        # Audit-finding High #2: gate the URL through the SSRF / scheme
        # filter before handing it to the UI layer. The protocol handler
        # and ``main.py --url`` both apply ``ensure_public`` up front;
        # doing the same here closes the last remaining loopback where a
        # local (or smuggled-Origin) caller could route the desktop app
        # to a private-network target via CatCatch.
        try:
            ssrf_guard.ensure_public(url)
        except ssrf_guard.SSRFBlocked as exc:
            logger.warning(
                f"[HTTP] URL blocked by SSRF guard: reason={exc.reason}",
                event="catcatch_url_ssrf_blocked",
                stage="http_handle_download",
                reason=exc.reason,
            )
            self._send_response(
                400,
                {"error": "URL rejected", "reason": exc.reason},
            )
            return

        # NB: the raw headers mapping may carry a Cookie / Authorization
        # value from the browser extension; ``sanitize_headers`` drops
        # everything outside the R6 forwardable allowlist *and* validates
        # the remaining name/value pairs before they ever reach an engine
        # argv. The DEBUG line below intentionally prints the *count* of
        # headers received so the raw values never hit the log even in
        # verbose mode.
        raw_count = len(headers) if isinstance(headers, dict) else 0
        clean_headers = sanitize_headers(headers if isinstance(headers, dict) else None)
        logger.debug(
            f"[HTTP] Headers: received={raw_count} forwarded={len(clean_headers)} "
            f"names={sorted(clean_headers.keys())}"
        )
        logger.debug(f"[HTTP] Filename: {filename}")

        if not DownloadRequestHandler.on_download_request:
            self._send_response(500, {"error": "No handler registered"})
            return

        try:
            # Forward the *sanitized* dict downstream. The signal consumer
            # (``MainWindow._on_catcatch_download``) stores the returned
            # dict on ``DownloadTask.headers`` and every engine's
            # ``_build_command`` reads ``task.headers.get('cookie')`` etc.
            # Canonical-cased keys from ``sanitize_headers`` are therefore
            # also exposed as lower-cased variants so the existing engine
            # lookups keep working without case-insensitive rewiring. The
            # original key is kept so UI layers that reflect the user's
            # casing (e.g. diagnostics) still see ``Cookie`` not
            # ``cookie``.
            forwarded: dict[str, str] = {}
            for canonical, value in clean_headers.items():
                forwarded[canonical] = value
                forwarded[canonical.lower()] = value

            # Audit-finding High #2: DO NOT re-inject ``_``-prefixed keys
            # from the external ``headers`` payload. Those markers are
            # meant to be trusted-local pointers (e.g. ``_cookie_file``
            # routing ``ytdlp_engine`` at a disk path) and must only be
            # set by the UI/engine layers after their own trust checks.
            # Accepting them from a CatCatch POST would let any local
            # caller with a valid session token hand the engine an
            # arbitrary filesystem path as a "cookies" file, turning the
            # trust boundary inside-out. Leaving them unset here simply
            # means the engine falls back to its normal cookie-discovery
            # flow (see ``YtdlpEngine._resolve_cookie_file``), which is
            # the correct behaviour for extension-delivered URLs.

            DownloadRequestHandler.on_download_request(url, forwarded, filename)
            self._send_response(
                200,
                {
                    "status": "success",
                    "message": TR("log_cli_resource_added"),
                    "url": _redact_url_for_log(url),
                },
            )
        except Exception as e:
            logger.error(
                f"[HTTP] {TR('log_http_handle_failed')}: {e}",
                event="catcatch_handle_download_failed",
                stage="http_handle_download",
                error_type=type(e).__name__,
                url_redacted=_redact_url_for_log(url),
            )
            self._send_response(500, {"error": str(e)})


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class CatCatchServer(QObject):
    """Local HTTP API server used by the CatCatch browser extension.

    The server is always bound to the loopback interface (``127.0.0.1``).
    A fresh session token is generated on every ``start()`` and written to
    :data:`SESSION_TOKEN_FILE` so the trusted extension can read it; the
    token is cleared from disk on ``stop()``.
    """

    download_requested = pyqtSignal(str, dict, str)  # url, headers, filename

    def __init__(
        self,
        port: int = DEFAULT_CATCATCH_PORT,
        *,
        extra_allowed_origins: Iterable[str] | None = None,
        session_token_file: Path | None = None,
        candidate_ports: Iterable[int] | None = None,
    ):
        super().__init__()
        self.port = port
        self.server = None
        self.thread = None
        self._running = False
        self._lock = threading.Lock()
        self._start_event = threading.Event()
        self._start_error = ""
        # R22.3: candidate port list is configurable but defaults to 13
        # slots (primary + 9528..9539). We deduplicate while preserving
        # the requested primary's leading position.
        if candidate_ports is None:
            fallback = DEFAULT_FALLBACK_PORTS
        else:
            fallback = tuple(int(p) for p in candidate_ports)
        seen: set[int] = {port}
        ordered: list[int] = [port]
        for p in fallback:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        self._candidate_ports: tuple[int, ...] = tuple(ordered)
        self._allowed_origins: frozenset[str] = _build_allowed_origins(
            extra_allowed_origins
        )
        self._session_token: str = ""
        self._session_token_file: Path = session_token_file or SESSION_TOKEN_FILE
        DownloadRequestHandler.on_download_request = self._on_request

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Return the effective Origin whitelist for this instance."""
        return self._allowed_origins

    @property
    def session_token(self) -> str:
        """Return the current session token (empty string when stopped)."""
        return self._session_token

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    def _on_request(self, url: str, headers: dict, filename: str):
        self.download_requested.emit(url, headers, filename)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start server in a background thread."""
        with self._lock:
            if self._running:
                return
            self._start_event.clear()
            self._start_error = ""
            # Generate a fresh session token on every (re)start so that a
            # leaked token cannot be reused across sessions.
            self._session_token = secrets.token_urlsafe(24)
            # Publish Handler-level configuration BEFORE the serving thread
            # starts dispatching requests.
            DownloadRequestHandler.allowed_origins = self._allowed_origins
            DownloadRequestHandler.session_token = self._session_token

        # Write token file outside the lock; I/O failures are logged but
        # do not prevent the server from starting (the in-memory token is
        # still required for POST /download authentication).
        _write_session_token(self._session_token_file, self._session_token)

        self.thread = threading.Thread(
            target=self._run_server, daemon=True, name="CatCatchHTTPServer"
        )
        self.thread.start()

        # R22.1 / R22.4: dual-confirm start via ``start_event`` + the
        # in-lock ``_running`` flag. A miss of the 5s deadline is recorded
        # as ``bind_timeout`` and treated as a failed start — we never
        # optimistically assume the socket is bound just because the
        # thread was launched.
        event_fired = self._start_event.wait(timeout=START_BIND_TIMEOUT_S)

        with self._lock:
            if not event_fired and not self._start_error:
                # The serve thread never reached either the success or
                # the exhaustion branch. Record ``bind_timeout`` so the
                # failure reason surfaces to logs / UI.
                self._start_error = "bind_timeout"
            running = self._running
            current_port = self.port
            start_error = self._start_error

        if running:
            logger.info(
                f"{TR('log_catcatch_started').replace('{url}', f'http://127.0.0.1:{current_port}')}"
            )
        else:
            if not start_error:
                start_error = "startup timeout"
            logger.error(
                f"[CatCatch] {TR('log_catcatch_start_failed')}: {start_error}",
                event="catcatch_start_failed",
                stage="server_start",
                error_type=(
                    "bind_timeout"
                    if start_error == "bind_timeout"
                    else "port_exhausted"
                ),
            )

    def _run_server(self):
        # R22.1/R22.2: iterate the configured candidate port list and
        # track the last ``OSError`` so an exhaustion failure can surface
        # a concrete reason via ``_start_error``. ``_running`` is only
        # flipped to True *after* ``ThreadingHTTPServer`` successfully
        # binds the socket, and only under ``self._lock`` so the
        # ``start()`` waiter never races the bind.
        bind_error: Exception | None = None

        for port in self._candidate_ports:
            try:
                # ``ThreadingHTTPServer`` is the stdlib threaded variant of
                # ``HTTPServer``; every request is served on its own thread
                # so slow clients cannot block health checks / preflights.
                # Binding is hard-coded to 127.0.0.1 per R2.AC7 — no 0.0.0.0
                # / :: fallback is ever attempted.
                server = ThreadingHTTPServer(
                    ("127.0.0.1", port), DownloadRequestHandler
                )
            except OSError as e:
                bind_error = e
                if port == self._candidate_ports[0]:
                    logger.warning(
                        f"[CatCatch] {TR('log_catcatch_port_occupied').replace('{port}', str(port))}: {e}"
                    )
                else:
                    logger.debug(
                        f"[CatCatch] {TR('log_catcatch_port_unavailable').replace('{port}', str(port))}: {e}"
                    )
                continue
            except Exception as e:
                bind_error = e
                logger.error(
                    f"[CatCatch] {TR('log_catcatch_create_failed').replace('{port}', str(port))}: {e}",
                    event="catcatch_create_server_failed",
                    stage="server_create",
                    error_type=type(e).__name__,
                    port=port,
                )
                continue

            # Success path — populate the authoritative state under the
            # lock *before* releasing the ``start()`` waiter so the main
            # thread sees a fully initialised server. ``_running`` is the
            # single source of truth; it is never True without a bound
            # ``server`` instance.
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

        # R22.2: every candidate port failed. Record a structured error
        # message (surfaced to ``start()`` via ``_start_error``) and log
        # the exhaustion. ``PortExhausted`` is constructed so callers /
        # tests can parse the string, but we deliberately do not let it
        # propagate out of the daemon thread — that would kill the Qt
        # signal path without informing the waiter.
        exhaustion = PortExhausted(
            f"no available ports in {self._candidate_ports[0]}-{self._candidate_ports[-1]}"
            + (f": {bind_error}" if bind_error else "")
        )
        logger.warning(
            f"[CatCatch] {exhaustion}",
            event="catcatch_port_exhausted",
            stage="server_start",
            error_type="PortExhausted",
            candidate_count=len(self._candidate_ports),
        )
        with self._lock:
            # ``_running`` stays False — it was never flipped True because
            # no ``ThreadingHTTPServer`` construction succeeded.
            self._running = False
            self.server = None
            self._start_error = str(exhaustion)
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
            self._session_token = ""
            DownloadRequestHandler.session_token = ""

        # Clear on-disk token so a subsequent start generates a fresh one
        # and no stale token lingers between sessions.
        _remove_session_token(self._session_token_file)

        logger.info(f"[CatCatch] {TR('log_catcatch_stopped')}")

    def is_running(self) -> bool:
        with self._lock:
            return self._running and self.server is not None

    def get_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/download"
