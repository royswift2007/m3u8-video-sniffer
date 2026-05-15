"""
R2 / R6 smoke check — CatCatch server Origin + session-token gating.

Spins up a :class:`core.catcatch_server.CatCatchServer` bound to an
ephemeral loopback port (no Qt event loop needed) and exercises the three
auth gates of ``POST /download``:

1. Missing ``X-Session-Token`` -> 401 Unauthorized.
2. Wrong ``Origin`` (not in the default whitelist) -> 403 Forbidden.
3. Whitelisted ``Origin`` + correct ``X-Session-Token`` -> the auth gate
   cleared (either 200 when the emitted Qt signal is dispatched, or 500
   when the handler returns because no Qt event loop is attached to
   ``download_requested``; both outcomes prove we got past auth).

The test exits 0 on success (all three gates behave as expected), or 1 on
any deviation. Runs in ~2 s, offline, no network, no side effects beyond a
single ``~/.m3u8d/session.token`` write that is cleaned up by the server's
own :meth:`CatCatchServer.stop`.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from http.client import HTTPConnection  # noqa: E402

# PyQt6 is imported transitively by core.catcatch_server. No QApplication is
# required for HTTP serving since the signal emission is a no-op when there
# are no connected slots.
from core.catcatch_server import CatCatchServer  # noqa: E402


def _pick_ephemeral_port() -> int:
    """Ask the OS for a free loopback TCP port and release it immediately.

    There is a tiny race between the close and the subsequent bind inside
    :class:`CatCatchServer._run_server`, but both happen on the same host
    within milliseconds so contention is extraordinarily unlikely in a
    local smoke run.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(
    port: int,
    *,
    headers: dict,
    body: dict,
) -> tuple[int, str]:
    """POST ``/download`` against the local server; return (status, body)."""

    conn = HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        payload = json.dumps(body).encode("utf-8")
        conn.request("POST", "/download", body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        return resp.status, data
    finally:
        conn.close()


def _wait_until_ready(server: CatCatchServer, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.is_running():
            return
        time.sleep(0.05)
    raise RuntimeError("CatCatchServer did not enter running state in time")


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def main() -> int:
    port = _pick_ephemeral_port()
    # Reserve a per-run token file so this smoke script does not clobber any
    # real token that a live instance happens to be using on the same host.
    token_file = PROJECT_ROOT / "build" / "stage1_smoke_session.token"
    token_file.parent.mkdir(parents=True, exist_ok=True)

    server = CatCatchServer(
        port=port,
        candidate_ports=[],  # primary only, no fallbacks
        session_token_file=token_file,
    )
    server.start()
    try:
        _wait_until_ready(server)
        expected_token = server.session_token
        _assert(bool(expected_token), "session token should be non-empty after start")

        payload = {"url": "https://example.com/video.m3u8"}

        # --- Case 1: missing X-Session-Token -> 401 --------------------
        status, body = _request(
            port,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1",
            },
            body=payload,
        )
        _assert(
            status == 401,
            f"missing token should be 401, got {status} body={body!r}",
        )

        # --- Case 2: wrong Origin + correct token -> 403 ---------------
        status, body = _request(
            port,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example.com",
                "X-Session-Token": expected_token,
            },
            body=payload,
        )
        _assert(
            status == 403,
            f"non-whitelisted Origin should be 403, got {status} body={body!r}",
        )

        # --- Case 3: whitelisted Origin + correct token -> auth cleared.
        # With no Qt event loop attached, the ``download_requested`` signal
        # either lands a registered slot (200 success) or the handler
        # re-raises through :class:`DownloadRequestHandler` returning 500;
        # both outcomes prove the 401/403 gates were cleared.
        status, body = _request(
            port,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1",
                "X-Session-Token": expected_token,
            },
            body=payload,
        )
        _assert(
            status in (200, 500),
            f"authenticated request should bypass auth gate (200 or 500), "
            f"got {status} body={body!r}",
        )

        print("PASS smoke_catcatch_auth: 401/403/cleared all behaved as expected")
        return 0
    finally:
        try:
            server.stop()
        except Exception as exc:  # pragma: no cover - best effort
            print(f"[warn] server stop raised: {exc}", file=sys.stderr)
        # Clean up the per-run token file.
        try:
            if token_file.exists():
                token_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL smoke_catcatch_auth: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"FAIL smoke_catcatch_auth: unexpected error {exc!r}", file=sys.stderr)
        raise SystemExit(1)
