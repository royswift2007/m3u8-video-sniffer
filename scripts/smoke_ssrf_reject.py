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

    print(
        "PASS smoke_ssrf_reject: loopback/link-local/private/file all refused"
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
