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

from utils.log_retention import prune_runtime_logs

API_HOST = "127.0.0.1"
API_PORT_MIN = 9527
API_PORT_MAX = 9539


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


def log_message(msg: str):
    log_dir = _get_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "protocol_handler.log"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"[{timestamp}] {msg}\n"
    prune_runtime_logs(log_dir, reserve_bytes=len(message.encode("utf-8")))
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(message)


def parse_n_m3u8dl_format(raw_url: str) -> dict:
    """Parse N_m3u8DL-RE command style text."""
    result = {"url": "", "headers": {}, "filename": "", "save_dir": ""}
    data = urllib.parse.unquote(raw_url or "")
    log_message(f"协议原始内容(前300): {data[:300]}")

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
    payload = json.dumps({"url": url, "headers": headers, "name": filename}).encode("utf-8")
    conn = None
    try:
        conn = http.client.HTTPConnection(API_HOST, port, timeout=timeout)
        conn.request("POST", "/download", payload, {"Content-Type": "application/json"})
        response = conn.getresponse()
        _ = response.read()
        if 200 <= response.status < 300:
            log_message(f"HTTP 投递成功: {API_HOST}:{port} status={response.status}")
            return True
        log_message(f"HTTP 投递失败: {API_HOST}:{port} status={response.status}")
        return False
    except (socket.error, ConnectionError, TimeoutError) as e:
        log_message(f"HTTP 端口不可达: {API_HOST}:{port} error={e}")
        return False
    except Exception as e:
        log_message(f"HTTP 投递异常: {API_HOST}:{port} error={e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                log_message(f"HTTP 连接关闭异常 {API_HOST}:{port} error={e}")


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

    log_message(f"启动命令: {_format_command(args)}")

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
    log_message(f"收到协议URL长度: {len(protocol_url)}")
    log_message(f"协议URL前100字符: {protocol_url[:100]}")

    parsed = parse_m3u8dl_url(protocol_url)
    if not parsed["url"]:
        log_message("ERROR: 无法解析有效URL")
        return

    log_message(f"解析结果 URL: {parsed['url'][:120]}")
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
