"""
R4 smoke check — ``ensure_public`` refuses loopback and link-local URLs.

Covers the two canonical SSRF targets from Requirement 4.AC1:

* ``http://127.0.0.1/x.m3u8`` — IPv4 loopback.
* ``http://169.254.169.254/latest`` — AWS/cloud metadata service on the
  link-local 169.254.0.0/16 range.

Both MUST raise :class:`utils.ssrf_guard.SSRFBlocked`. The script also
asserts that a ``file://`` URL is refused with ``scheme_not_allowed`` so
the scheme allowlist stays active alongside the IP blocklist. Offline,
synchronous, exits 0 on pass / 1 on any deviation.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.ssrf_guard import SSRFBlocked, ensure_public  # noqa: E402


def _expect_blocked(url: str, *, expected_reason_prefix: str | None = None) -> None:
    try:
        ensure_public(url)
    except SSRFBlocked as exc:
        if expected_reason_prefix and not exc.reason.startswith(
            expected_reason_prefix
        ):
            raise AssertionError(
                f"{url}: expected reason starting with {expected_reason_prefix!r}, "
                f"got {exc.reason!r}"
            )
        return
    raise AssertionError(f"{url}: ensure_public did not raise SSRFBlocked")


def _check_pinned_url_and_host() -> None:
    """F-01: a public IP literal pins the URL authority and restores Host."""
    from utils.ssrf_guard import ResolvedHost, pinned_url_and_host
    from ipaddress import IPv4Address

    resolved = ResolvedHost(hostname="example.com", ips=(IPv4Address("93.184.216.34"),))
    pinned, host = pinned_url_and_host("http://example.com:8080/a.m3u8", resolved)
    assert pinned == "http://93.184.216.34:8080/a.m3u8", pinned
    assert host == "example.com:8080", host

    # No hostname / empty resolved should leave the URL untouched.
    pinned2, host2 = pinned_url_and_host("http://example.com/a.m3u8", ResolvedHost(hostname="", ips=()))
    assert pinned2 == "http://example.com/a.m3u8", pinned2
    assert host2 is None, host2


def _check_pinned_session_builds() -> None:
    """F-01: make_pinned_session returns a Session bound to the IP."""
    from utils.ssrf_guard import ResolvedHost, make_pinned_session
    from ipaddress import IPv4Address

    resolved = ResolvedHost(hostname="example.com", ips=(IPv4Address("93.184.216.34"),))
    session = make_pinned_session(resolved)
    assert session is not None, "make_pinned_session returned None for a valid resolved host"
    # The pinned adapter replaces the default for both schemes.
    import requests as _requests
    assert isinstance(session, _requests.Session)


def _check_redirect_to_private_blocked() -> None:
    """F-01: a 30x whose Location resolves to loopback must be refused.

    Simulates the TOCTOU window: even after the *initial* URL passes
    ensure_public (it's a public IP), following a redirect whose target
    is 127.0.0.1 must raise SSRFBlocked because each hop re-runs the
    guard. We exercise this against ``ensure_public`` directly since the
    hop re-validation in hls_probe / m3u8_parser calls ensure_public on
    the resolved redirect URL.
    """
    # The redirect target itself is private — must be blocked.
    _expect_blocked(
        "http://127.0.0.1/private.m3u8", expected_reason_prefix="ip_in_blocklist"
    )
    # A 302 to a hostname that resolves to a private IP: ensure_public
    # inspects every resolved IP, so a rebind-style private A record is
    # caught at the guard (we cannot do real DNS here, but the literal
    # 127.0.0.1 case proves the hop guard would catch it).
    _expect_blocked(
        "http://[::ffff:127.0.0.1]/x", expected_reason_prefix="ip_in_blocklist"
    )


def _check_allow_private_opt_in() -> None:
    """F-02: allow_private=True accepts a private target (still resolved)."""
    from utils.ssrf_guard import ensure_public

    # Default (allow_private=False) must hard-block.
    try:
        ensure_public("http://192.168.1.1/x")
    except SSRFBlocked:
        pass
    else:
        raise AssertionError("ensure_public did not block 192.168.1.1 by default")

    # Opt-in must return a ResolvedHost (the IP is private but allowed).
    resolved = ensure_public("http://192.168.1.1/x", allow_private=True)
    assert resolved is not None
    assert len(resolved.ips) >= 1


def main() -> int:
    # IPv4 loopback — also covers 127.0.0.0/8.
    _expect_blocked("http://127.0.0.1/x.m3u8", expected_reason_prefix="ip_in_blocklist")
    # Cloud metadata / link-local.
    _expect_blocked(
        "http://169.254.169.254/latest", expected_reason_prefix="ip_in_blocklist"
    )
    # Private IPv4 (192.168/16).
    _expect_blocked(
        "http://192.168.1.1/x.m3u8", expected_reason_prefix="ip_in_blocklist"
    )
    # IPv6 loopback literal.
    _expect_blocked("http://[::1]/x.m3u8", expected_reason_prefix="ip_in_blocklist")
    # Non-http scheme must be refused before any DNS lookup.
    _expect_blocked(
        "file:///etc/passwd", expected_reason_prefix="scheme_not_allowed"
    )
    # F-01: redirect-to-private / pinned session helpers.
    _check_redirect_to_private_blocked()
    _check_pinned_url_and_host()
    _check_pinned_session_builds()
    # F-02: allow_private_networks opt-in.
    _check_allow_private_opt_in()

    print(
        "PASS smoke_ssrf_reject: loopback/link-local/private/file/redirect/pin/allow_private all refused or honoured"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL smoke_ssrf_reject: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"FAIL smoke_ssrf_reject: unexpected error {exc!r}", file=sys.stderr
        )
        raise SystemExit(1)
