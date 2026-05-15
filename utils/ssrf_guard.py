"""SSRF filtering: resolve a hostname and refuse non-public addresses.

This is a pure-policy module: it performs DNS resolution and IP range
checks, but does NOT open network sockets itself. Callers pass the
result of :func:`ensure_public` to their HTTP client so that the
connection is pinned to an already-vetted public IP (blocking DNS
rebinding across a subsequent ``getaddrinfo`` call).

Exports:
    ResolvedHost        -- frozen dataclass ``(hostname, ips)``.
    resolve_all(host)   -- getaddrinfo-backed, de-duplicated resolver.
    is_blocked_ip(ip)   -- boolean predicate for R4.AC1 address families.
    ensure_public(url, *, allow_private=False) -> ResolvedHost.
    SSRFBlocked         -- exception raised by ``ensure_public``.

The blocked ranges cover Requirement 4.AC1 exactly:

    IPv4: 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
          169.254.0.0/16 (and 169.254.169.254 cloud metadata),
          172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, broadcast.
    IPv6: ::1/128, fc00::/7, fe80::/10, multicast (ff00::/8),
          IPv4-mapped addresses inherit their IPv4 policy.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Tuple, Union
from urllib.parse import urlsplit


__all__ = (
    "ResolvedHost",
    "SSRFBlocked",
    "resolve_all",
    "is_blocked_ip",
    "ensure_public",
)


IPAddress = Union[IPv4Address, IPv6Address]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SSRFBlocked(Exception):
    """Raised when :func:`ensure_public` refuses to let a URL through.

    Attributes:
        url:           the URL that was checked (unredacted; callers that
                       log it must run :func:`utils.redact.redact_url`).
        reason:        short machine-readable reason code.
        offending_ip:  the IP that triggered the block, if any.
    """

    def __init__(
        self,
        url: str,
        reason: str,
        offending_ip: IPAddress | None = None,
    ) -> None:
        self.url = url
        self.reason = reason
        self.offending_ip = offending_ip
        suffix = f" offending_ip={offending_ip}" if offending_ip is not None else ""
        super().__init__(f"SSRF blocked: {reason} url={url}{suffix}")


# ---------------------------------------------------------------------------
# Blocked range policy
# ---------------------------------------------------------------------------


# R4.AC1 explicit IPv4 networks. ``broadcast``/``multicast``/loopback etc.
# are also covered by ``ipaddress`` attribute predicates, but keeping the
# explicit networks here makes the policy auditable against requirements.
_BLOCKED_IPV4_NETWORKS: tuple[IPv4Network, ...] = (
    IPv4Network("0.0.0.0/8"),        # "this network"
    IPv4Network("10.0.0.0/8"),       # RFC1918 private
    IPv4Network("100.64.0.0/10"),    # CGNAT
    IPv4Network("127.0.0.0/8"),      # loopback
    IPv4Network("169.254.0.0/16"),   # link-local (inc. 169.254.169.254)
    IPv4Network("172.16.0.0/12"),    # RFC1918 private
    IPv4Network("192.0.0.0/24"),     # IETF protocol assignments
    IPv4Network("192.0.2.0/24"),     # TEST-NET-1
    IPv4Network("192.168.0.0/16"),   # RFC1918 private
    IPv4Network("198.18.0.0/15"),    # benchmarking
    IPv4Network("198.51.100.0/24"),  # TEST-NET-2
    IPv4Network("203.0.113.0/24"),   # TEST-NET-3
    IPv4Network("224.0.0.0/4"),      # multicast
    IPv4Network("240.0.0.0/4"),      # reserved / class E
    IPv4Network("255.255.255.255/32"),  # limited broadcast
)

_BLOCKED_IPV6_NETWORKS: tuple[IPv6Network, ...] = (
    IPv6Network("::1/128"),          # loopback
    IPv6Network("::/128"),           # unspecified
    IPv6Network("fc00::/7"),         # unique local
    IPv6Network("fe80::/10"),        # link-local
    IPv6Network("ff00::/8"),         # multicast
    IPv6Network("2001:db8::/32"),    # documentation
    IPv6Network("::ffff:0:0/96"),    # IPv4-mapped (checked via mapped v4 too)
    IPv6Network("64:ff9b::/96"),     # NAT64 well-known
    IPv6Network("100::/64"),         # discard prefix
)


def is_blocked_ip(ip: object) -> bool:
    """Return ``True`` if ``ip`` falls in any non-public range of R4.AC1.

    Accepts ``IPv4Address`` / ``IPv6Address`` instances and strings. Any
    unparsable input is treated as blocked (fail-closed).
    """

    if isinstance(ip, (IPv4Address, IPv6Address)):
        address: IPAddress = ip
    elif isinstance(ip, str):
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return True
    else:
        return True

    # IPv4-mapped IPv6 (e.g. ``::ffff:10.0.0.1``) should be evaluated as
    # the embedded IPv4 address, otherwise rebind tricks can tunnel RFC1918
    # traffic past the v4 checks.
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    if isinstance(address, IPv4Address):
        # ``ipaddress`` predicates already cover most categories; keep them
        # first because they're O(1).
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return True
        for net in _BLOCKED_IPV4_NETWORKS:
            if address in net:
                return True
        return False

    # IPv6
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_site_local
    ):
        return True
    for net6 in _BLOCKED_IPV6_NETWORKS:
        if address in net6:
            return True
    return False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedHost:
    """All IPs that a hostname resolves to, captured in one snapshot."""

    hostname: str
    ips: Tuple[IPAddress, ...]


def _strip_brackets(host: str) -> str:
    """Remove surrounding ``[...]`` from an IPv6 literal if present."""

    if len(host) >= 2 and host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def resolve_all(hostname: str) -> ResolvedHost:
    """Resolve ``hostname`` to every address returned by ``getaddrinfo``.

    If ``hostname`` is already an IP literal the function returns a
    :class:`ResolvedHost` with that single address without touching the
    network.
    """

    if not isinstance(hostname, str) or not hostname:
        raise ValueError("hostname must be a non-empty string")

    host = _strip_brackets(hostname.strip())

    # Fast path: numeric literals (no DNS traffic).
    try:
        literal = ipaddress.ip_address(host)
        return ResolvedHost(hostname=host, ips=(literal,))
    except ValueError:
        pass

    # ``AI_ADDRCONFIG`` would filter out IPv4 on pure-IPv6 machines and
    # vice-versa; we purposefully DO NOT set it so SSRF policy sees every
    # record the OS would normally feed to the HTTP client.
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFBlocked(host, reason=f"dns_error: {exc}") from exc

    seen: list[IPAddress] = []
    seen_keys: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        if family == socket.AF_INET:
            raw = sockaddr[0]
            try:
                addr: IPAddress = IPv4Address(raw)
            except ValueError:
                continue
        elif family == socket.AF_INET6:
            raw = sockaddr[0]
            # Strip any scope-id suffix (``fe80::1%eth0``) before parsing.
            if "%" in raw:
                raw = raw.split("%", 1)[0]
            try:
                addr = IPv6Address(raw)
            except ValueError:
                continue
        else:
            continue
        key = str(addr)
        if key not in seen_keys:
            seen_keys.add(key)
            seen.append(addr)

    if not seen:
        raise SSRFBlocked(host, reason="dns_empty")

    return ResolvedHost(hostname=host, ips=tuple(seen))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_ALLOWED_SCHEMES: frozenset = frozenset({"http", "https"})


def ensure_public(url: str, *, allow_private: bool = False) -> ResolvedHost:
    """Refuse ``url`` unless every resolved IP is publicly routable.

    Args:
        url:            absolute http(s) URL.
        allow_private:  when true, only log but do not refuse private IPs
                        (for the ``security.allow_private_networks`` flag
                        documented in design 1.2).

    Raises:
        SSRFBlocked: if the scheme is unsupported, the host is missing,
                     DNS fails, or any resolved IP falls in a blocked
                     range (and ``allow_private`` is false).

    Returns:
        ResolvedHost describing the hostname and ALL resolved IPs. The
        caller is expected to connect to ``ips[0]`` while still passing
        the original hostname as the TLS ``server_hostname`` to keep SNI
        and certificate validation working.
    """

    if not isinstance(url, str) or not url:
        raise SSRFBlocked(str(url), reason="url_empty")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SSRFBlocked(url, reason=f"url_invalid: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlocked(url, reason=f"scheme_not_allowed: {scheme!r}")

    host = parts.hostname
    if not host:
        raise SSRFBlocked(url, reason="host_missing")

    resolved = resolve_all(host)

    # R4.AC3: inspect EVERY resolved IP. Any single blocked address is
    # enough to reject (DNS rebind defence).
    for addr in resolved.ips:
        if is_blocked_ip(addr):
            if allow_private:
                # Caller opted in to private-network downloads; still keep
                # the resolved host so they can pin the connection.
                continue
            raise SSRFBlocked(url, reason="ip_in_blocklist", offending_ip=addr)

    return resolved
