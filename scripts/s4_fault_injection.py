import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import http.client
import json
import socket
import time
from importlib.machinery import SourceFileLoader

from core.catcatch_server import CatCatchServer
from core.m3u8_parser import M3U8FetchThread

protocol_handler = SourceFileLoader("protocol_handler", str(PROJECT_ROOT / "protocol_handler.pyw")).load_module()
parse_m3u8dl_url = protocol_handler.parse_m3u8dl_url


def assert_true(expr, msg):
    if not expr:
        raise AssertionError(msg)


def test_protocol_parse_bad_payload():
    parsed = parse_m3u8dl_url("m3u8dl://{bad-json")
    assert_true(isinstance(parsed, dict), "parse result must be dict")
    assert_true("url" in parsed, "parse result missing url")


def test_catcatch_invalid_json_and_missing_url():
    server = CatCatchServer(port=9537)
    server.start()
    time.sleep(0.3)
    assert_true(server.is_running(), "catcatch server should be running")

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    conn.request("POST", "/download", "{bad-json", {"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="ignore")
    assert_true(resp.status == 400, f"invalid json should return 400, got {resp.status}")
    assert_true("Invalid JSON" in body, "invalid json response body mismatch")
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    conn.request("POST", "/download", json.dumps({"headers": {"referer": "https://x"}}), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="ignore")
    assert_true(resp.status == 400, f"missing url should return 400, got {resp.status}")
    assert_true("Missing 'url'" in body, "missing url response body mismatch")
    conn.close()

    server.stop()
    assert_true(not server.is_running(), "catcatch server should stop")


def test_catcatch_port_fallback():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", 9527))
    s.listen(1)

    server = CatCatchServer(port=9527)
    server.start()
    time.sleep(0.4)
    assert_true(server.is_running(), "fallback server should run")
    assert_true(server.port != 9527, "fallback port should not be 9527")
    server.stop()
    s.close()


def test_m3u8_bad_domain_no_crash():
    t = M3U8FetchThread("https://nonexistent.invalid/test.m3u8", headers={})
    # run() should catch its own errors and not raise.
    t.run()


if __name__ == "__main__":
    tests = [
        test_protocol_parse_bad_payload,
        test_catcatch_invalid_json_and_missing_url,
        test_catcatch_port_fallback,
        test_m3u8_bad_domain_no_crash,
    ]
    for fn in tests:
        fn()
        print(f"PASS: {fn.__name__}")
    print("S4 fault injection passed")
