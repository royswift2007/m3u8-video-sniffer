"""
M3U8 Video Sniffer - URL protocol handler.
Receive `m3u8dl://` calls and forward to local app HTTP API.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.log_retention import prune_runtime_logs

API_HOST = "127.0.0.1"
API_PORT_MIN = 9527
API_PORT_MAX = 9539

# Session token 文件路径 (只读);与 core/catcatch_server.py::SESSION_TOKEN_FILE
# 字面相同,两处必须同步变更(本 bugfix 不改服务端,故两处当前等价)。
SESSION_TOKEN_FILE: Path = Path.home() / ".m3u8d" / "session.token"

# Token 文件大小上限;超过视为异常 / 攻击,记录 token_file_too_large 后
# 返回 None。正常 token 为 secrets.token_urlsafe(24) = 32 字节,64KB
# 足以容纳未来字段演化(如 JWT 风格 header.payload.signature)。
MAX_TOKEN_FILE_BYTES: int = 64 * 1024

# 回滚开关 (bugfix.md R1 Rollback Notes)。设置为 "1" 时,_send_to_single_port
# 完全按修复前行为运行(不读 token,不设 Origin),仅用于紧急诊断。默认关闭。
LEGACY_HANDOFF_ENV: str = "M3U8D_HANDOFF_LEGACY"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _get_handler_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _get_app_root() -> Path:
    handler_dir = _get_handler_dir()
    if _is_frozen() and handler_dir.name.lower() == "protocol_handler":
        return handler_dir.parent
    return handler_dir


def _get_logs_dir() -> Path:
    return _get_app_root() / "logs"


def _redact_url_for_log(url: str) -> str:
    """Return ``url`` with sensitive query values scrubbed for logging.

    Audit-finding High #3 (sensitive data in logs): the protocol handler
    used to record the raw ``m3u8dl://`` payload and the parsed URL at
    INFO level. Route those values through :func:`utils.redact.redact_url`
    first so tokens / signatures / auth keys never land on disk. On
    import failure the fallback simply drops the query string, which is
    still safer than echoing the raw URL.
    """

    try:
        from utils.redact import redact_url

        return redact_url(url or "")
    except Exception:
        head, _, _ = (url or "").partition("?")
        return head or (url or "")


def _redact_command_for_log(args: "list[str]") -> str:
    """Return a shell-safe, sensitive-value-scrubbed rendering of ``args``.

    Delegates to :func:`utils.redact.redact_argv` (which masks values that
    follow well-known ``-H Cookie:`` / ``--headers`` / ``--url`` flags)
    before assembling the log string. Falls back to the pre-existing
    ``_format_command`` output when the redaction helpers are not
    importable, so a broken :mod:`utils` path never prevents the handler
    from logging.
    """

    try:
        from utils.redact import redact_argv

        redacted_args = list(redact_argv(args))
    except Exception:
        redacted_args = list(args)
    rendered: list[str] = []
    for arg in redacted_args:
        if " " in arg or "\t" in arg:
            rendered.append(f'"{arg}"')
        else:
            rendered.append(arg)
    return " ".join(rendered)


def log_message(msg: str):
    log_dir = _get_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "protocol_handler.log"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"[{timestamp}] {msg}\n"
    prune_runtime_logs(log_dir, reserve_bytes=len(message.encode("utf-8")))
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(message)


def _read_session_token() -> Optional[str]:
    """读取 ~/.m3u8d/session.token 返回去空白后的字符串 (或 None).

    Pure helper for the trusted-local handshake path (bugfix.md R1 AC7 / AC8).
    Every call is a fresh disk read so the protocol handler honours
    server-side token rotation on the very next m3u8dl:// trigger.

    Returns:
        The ``strip()``-ed token string when the file exists and is
        readable within :data:`MAX_TOKEN_FILE_BYTES`. Returns ``None``
        on every failure mode below; the caller treats ``None`` as
        "token unavailable" and issues the POST without
        ``X-Session-Token`` (which the server will 401).

    Error taxonomy (all paths are swallowed — never raises to caller):
        * File missing → ``token_missing: file_not_found``.
        * ``stat()`` fails → ``token_missing: stat_failed ...``.
        * File larger than :data:`MAX_TOKEN_FILE_BYTES` →
          ``token_file_too_large: size=<n> limit=<n>``.
        * Read / decode fails → ``token_read_failed: error_type=<name>``.
        * File empty or whitespace-only → ``token_missing: file_empty``.

    The content is **never** logged in plaintext; only metadata
    (``token_loaded`` / ``token_len`` etc.) appears in downstream logs.
    """
    path = SESSION_TOKEN_FILE
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        log_message("token_missing: file_not_found")
        return None
    except OSError as e:
        log_message(
            f"token_missing: stat_failed error_type={type(e).__name__}"
        )
        return None

    if stat_result.st_size > MAX_TOKEN_FILE_BYTES:
        log_message(
            f"token_file_too_large: size={stat_result.st_size} "
            f"limit={MAX_TOKEN_FILE_BYTES}"
        )
        return None

    try:
        with open(path, "r", encoding="ascii") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        log_message(
            f"token_read_failed: error_type={type(e).__name__}"
        )
        return None

    token = raw.strip()
    if not token:
        log_message("token_missing: file_empty")
        return None
    return token


def parse_n_m3u8dl_format(raw_url: str) -> dict:
    """Parse N_m3u8DL-RE command style text."""
    result = {"url": "", "headers": {}, "filename": "", "save_dir": ""}
    data = urllib.parse.unquote(raw_url or "")
    # Audit-finding High #3: do NOT log the raw protocol payload — it may
    # contain bearer tokens, Cookie values, or signed URLs.
    log_message(f"协议原始内容长度: {len(data)} bytes")

    if data.startswith("m3u8dl://"):
        data = data[9:]
    elif data.startswith("m3u8dl:"):
        data = data[7:]

    url_match = re.search(r'^"([^"]+)"', data)
    if url_match:
        result["url"] = url_match.group(1)
    else:
        first_arg = data.split()[0] if data.split() else data
        if first_arg.startswith("http"):
            result["url"] = first_arg

    name_match = re.search(r'--save-name\s+"([^"]+)"', data)
    if name_match:
        result["filename"] = name_match.group(1)

    dir_match = re.search(r'--save-dir\s+"([^"]+)"', data)
    if dir_match:
        result["save_dir"] = dir_match.group(1)

    header_matches = re.findall(r'-H\s+"([^"]+)"', data)
    for item in header_matches:
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key not in ("accept", "accept-encoding", "accept-language"):
            result["headers"][key] = value

    return result


def parse_m3u8dl_url(raw_url: str) -> dict:
    """Parse m3u8dl:// payload in multiple formats."""
    result = {"url": "", "headers": {}, "filename": "", "save_dir": ""}
    if not raw_url:
        return result

    if raw_url.startswith("m3u8dl://"):
        data = raw_url[9:]
    elif raw_url.startswith("m3u8dl:"):
        data = raw_url[7:]
    else:
        data = raw_url

    data = urllib.parse.unquote(data)

    # Prefer JSON payload when body is object-like.
    if data.strip().startswith("{"):
        try:
            json_data = json.loads(data)
            result["url"] = json_data.get("url", "")
            result["headers"] = json_data.get("headers", {})
            result["filename"] = json_data.get("name", "") or json_data.get("filename", "")
            return result
        except json.JSONDecodeError:
            pass

    if '"' in data or "--" in data or "-H" in data:
        log_message("检测到命令行格式协议参数")
        return parse_n_m3u8dl_format(data)

    if data.startswith("http"):
        result["url"] = data.split()[0]
        return result

    try:
        json_data = json.loads(data)
        result["url"] = json_data.get("url", "")
        result["headers"] = json_data.get("headers", {})
        result["filename"] = json_data.get("name", "") or json_data.get("filename", "")
        return result
    except json.JSONDecodeError:
        result["url"] = data
        return result


def _candidate_ports() -> list[int]:
    return list(range(API_PORT_MIN, API_PORT_MAX + 1))


def _send_to_single_port(port: int, url: str, headers: dict, filename: str, timeout: float = 1.5) -> bool:
    """POST a download request to one CatCatchServer candidate port.

    Task 3.3 / 3.4 / 3.5 (bugfix ``protocol-handler-session-handoff``):
        - 3.3 header construction: always send ``Content-Type``; when the
          legacy escape hatch is off, also send hardcoded ``Origin: http://127.0.0.1``
          (no port — this exact literal is a member of
          ``DEFAULT_ALLOWED_ORIGINS`` on the server) and — if
          ``_read_session_token()`` yields a string — ``X-Session-Token``.
        - 3.4 exception taxonomy: split socket/Connection/Timeout,
          ``http.client.HTTPException`` (request / getresponse / read
          phases), 401/403 auth failures, other 4xx/5xx, and an
          unexpected-exception catch-all. Every branch logs a redacted
          line and returns False. Never raises to caller (protects
          ``main()``).
        - 3.5 success predicate: 2xx AND (if JSON decodable, ``status`` is
          missing or equal to ``"success"``). JSON decode failure falls
          back to "2xx-only" (forward-compat with future bodies).

    The ``X-Session-Token`` header value is NEVER logged in plaintext;
    only metadata (``token_loaded``, ``token_len``, ``status_code``,
    ``auth_ok``, ``error_type``) appears in logs (R3 AC5).
    """
    payload = json.dumps({"url": url, "headers": headers, "name": filename}).encode("utf-8")

    legacy = os.environ.get(LEGACY_HANDOFF_ENV) == "1"

    req_headers: dict[str, str] = {"Content-Type": "application/json"}
    token: Optional[str] = None
    if legacy:
        # Rollback mode — behave byte-for-byte like the pre-bugfix F.
        # Log once per call so operators can see the legacy path at runtime.
        log_message(
            f"HTTP 投递(legacy 模式): {API_HOST}:{port} token 与 Origin 头被跳过"
        )
    else:
        # R1 AC3 / R3 AC6: hardcoded loopback Origin (no port). The literal
        # "http://127.0.0.1" is already a direct member of
        # DEFAULT_ALLOWED_ORIGINS so no server-side change is needed. We
        # deliberately do NOT f-string or concatenate the port — that would
        # produce "http://127.0.0.1:<port>" which the server's
        # _normalize_origin preserves unchanged and therefore fails the
        # whitelist comparison.
        req_headers["Origin"] = "http://127.0.0.1"
        token = _read_session_token()
        if token is not None:
            req_headers["X-Session-Token"] = token

    token_loaded = bool(token)
    token_len = len(token) if token else 0

    conn = None
    try:
        conn = http.client.HTTPConnection(API_HOST, port, timeout=timeout)
        try:
            conn.request("POST", "/download", payload, req_headers)
            response = conn.getresponse()
        except http.client.HTTPException as e:
            log_message(
                f"HTTP 协议异常: {API_HOST}:{port} "
                f"error_type={type(e).__name__} token_loaded={token_loaded}"
            )
            return False

        status_code = response.status
        try:
            body_raw = response.read()
        except http.client.HTTPException as e:
            log_message(
                f"HTTP 投递失败(读响应): {API_HOST}:{port} "
                f"token_loaded={token_loaded} token_len={token_len} "
                f"error_type={type(e).__name__}"
            )
            return False

        if status_code in (401, 403):
            log_message(
                f"HTTP 投递被拒: {API_HOST}:{port} "
                f"status_code={status_code} token_loaded={token_loaded} "
                f"token_len={token_len} auth_ok=False"
            )
            return False

        if not (200 <= status_code < 300):
            log_message(
                f"HTTP 投递失败: {API_HOST}:{port} "
                f"status_code={status_code} token_loaded={token_loaded} "
                f"token_len={token_len}"
            )
            return False

        # 2xx — further validate the `status` field per R1 AC5/AC6.
        auth_ok = True  # noqa: F841 — reserved for future logging diffs
        status_field: Optional[str] = None
        try:
            decoded = json.loads(body_raw.decode("utf-8", errors="replace"))
            if isinstance(decoded, dict):
                status_field = decoded.get("status")
        except (ValueError, UnicodeDecodeError):
            status_field = None  # fall back to 2xx-only (R1 AC6)

        if status_field is None or status_field == "success":
            log_message(
                f"HTTP 投递成功: {API_HOST}:{port} "
                f"status_code={status_code} token_loaded={token_loaded} "
                f"token_len={token_len} auth_ok=True"
            )
            return True

        log_message(
            f"HTTP 投递失败(status 字段 != success): {API_HOST}:{port} "
            f"status_code={status_code} status_field={status_field!r} "
            f"token_loaded={token_loaded} token_len={token_len}"
        )
        return False

    except (socket.error, ConnectionError, TimeoutError) as e:
        log_message(
            f"HTTP 端口不可达: {API_HOST}:{port} "
            f"error_type={type(e).__name__} token_loaded={token_loaded}"
        )
        return False
    except http.client.HTTPException as e:
        # Defence-in-depth: anything http.client raises that escaped
        # the inner try/except (e.g. during socket setup) lands here.
        log_message(
            f"HTTP 协议异常: {API_HOST}:{port} "
            f"error_type={type(e).__name__} token_loaded={token_loaded}"
        )
        return False
    except Exception as e:
        # Catch-all — never re-raise; protects main() per R2 AC4.
        log_message(
            f"HTTP 投递未预期异常: {API_HOST}:{port} "
            f"error_type={type(e).__name__} token_loaded={token_loaded}"
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                log_message(
                    f"HTTP 连接关闭异常 {API_HOST}:{port} "
                    f"error_type={type(e).__name__}"
                )


def send_to_app(url: str, headers: dict, filename: str) -> bool:
    """Try sending payload to any running app API port."""
    for port in _candidate_ports():
        if _send_to_single_port(port, url, headers, filename):
            return True
    return False


def _resolve_python_executable() -> str:
    """Resolve best-effort python/pythonw executable."""
    candidates = []
    app_root = _get_app_root()

    candidates.append(app_root / ".venv" / "Scripts" / "pythonw.exe")
    candidates.append(app_root / ".venv" / "Scripts" / "python.exe")

    user_profile = os.environ.get("USERPROFILE", "")
    if user_profile:
        candidates.append(Path(user_profile) / "Documents" / ".venv" / "Scripts" / "pythonw.exe")
        candidates.append(Path(user_profile) / "Documents" / ".venv" / "Scripts" / "python.exe")

    if sys.executable:
        current = Path(sys.executable)
        candidates.append(current)
        candidates.append(current.with_name("pythonw.exe"))

    for candidate in candidates:
        try:
            if candidate and candidate.exists():
                return str(candidate)
        except Exception as e:
            log_message(f"Python 路径探测异常: {candidate} error={e}")
            continue

    return sys.executable


def _resolve_main_command() -> list[str]:
    app_root = _get_app_root()
    handler_dir = _get_handler_dir()

    if _is_frozen():
        candidates = [
            app_root / "M3U8D.exe",
            handler_dir / "M3U8D.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return [str(candidate)]
        return [str(app_root / "M3U8D.exe")]

    python_exe = _resolve_python_executable()
    for candidate in (app_root / "main.py", app_root / "mvs.pyw"):
        if candidate.exists():
            return [python_exe, str(candidate)]
    return [python_exe, str(app_root / "main.py")]


def _format_command(args: list[str]) -> str:
    rendered = []
    for arg in args:
        if " " in arg or "\t" in arg:
            rendered.append(f'"{arg}"')
        else:
            rendered.append(arg)
    return " ".join(rendered)


def launch_app_with_url(url: str, headers: dict, filename: str) -> bool:
    """Launch app and wait briefly for local API to accept request."""
    app_root = _get_app_root()
    args = _resolve_main_command()
    if url:
        args.extend(["--url", url])
    if headers:
        args.extend(["--headers", json.dumps(headers, ensure_ascii=False)])
    if filename:
        args.extend(["--filename", filename])

    # Audit-finding High #3: redact the rendered startup command; it may
    # contain ``--url`` / ``--headers`` values that carry sensitive
    # tokens or cookies.
    log_message(f"启动命令: {_redact_command_for_log(args)}")

    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        if app_root.exists():
            try:
                subprocess.Popen(args, creationflags=creationflags, cwd=str(app_root))
            except TypeError:
                subprocess.Popen(args, creationflags=creationflags)
        else:
            subprocess.Popen(args, creationflags=creationflags)
    except Exception as e:
        log_message(f"启动主程序失败: {e}")
        return False

    # Wait for app startup and retry API handoff.
    deadline = time.time() + 12.0
    while time.time() < deadline:
        if send_to_app(url, headers, filename):
            log_message("主程序启动后投递成功")
            return True
        time.sleep(0.5)

    log_message("主程序已启动但在等待窗口内未完成投递")
    return False


def main():
    log_message("=" * 50)
    log_message(f"Protocol Handler 启动, 参数数量: {len(sys.argv)}")

    if len(sys.argv) < 2:
        log_message("ERROR: 未提供协议参数")
        return

    protocol_url = sys.argv[1]
    # Audit-finding High #3: log only the payload length, not a raw prefix.
    log_message(f"收到协议URL长度: {len(protocol_url)}")

    parsed = parse_m3u8dl_url(protocol_url)
    if not parsed["url"]:
        log_message("ERROR: 无法解析有效URL")
        return

    log_message(f"解析结果 URL: {_redact_url_for_log(parsed['url'])}")
    log_message(f"解析结果 Filename: {parsed['filename']}")
    log_message(f"解析结果 Headers: {list((parsed.get('headers') or {}).keys())}")

    if send_to_app(parsed["url"], parsed["headers"], parsed["filename"]):
        log_message("已投递到运行中的主程序")
        return

    log_message("未发现运行中的主程序，尝试启动并回投")
    if launch_app_with_url(parsed["url"], parsed["headers"], parsed["filename"]):
        log_message("协议处理完成")
    else:
        log_message("ERROR: 启动主程序失败")


if __name__ == "__main__":
    main()
