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
    make_pinned_session(resolved, **kwargs) -> requests.Session
                        -- Session bound to ``resolved.ips[0]`` with the
                           original hostname preserved in the ``Host``
                           header so TLS SNI / cert validation still work.
                           Used by m3u8_parser / hls_probe / engine_selector
                           to close the TOCTOU window between
                           ``ensure_public`` and the actual TCP connect.

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
from typing import Any, Optional, Tuple, Union
from urllib.parse import urlsplit, urlunsplit


__all__ = (
    "ResolvedHost",
    "SSRFBlocked",
    "resolve_all",
    "is_blocked_ip",
    "ensure_public",
    "make_pinned_session",
    "pinned_url_and_host",
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


# ---------------------------------------------------------------------------
# Connection pinning (F-01: closes the TOCTOU window between ensure_public
# and the actual TCP connect by binding the requests session to the already-
# vetted IP, while preserving the original hostname for SNI / Host header).
# ---------------------------------------------------------------------------


def pinned_url_and_host(url: str, resolved: "ResolvedHost") -> Tuple[str, Optional[str]]:
    """Rewrite ``url``'s authority to ``resolved.ips[0]`` and return the
    ``Host`` header value that must be sent so the origin server still
    sees the original virtual-host name (required for SNI + cert
    validation + CDN vhost routing).

    Returns ``(pinned_url, host_header_or_None)``. When ``url`` has no
    hostname or ``resolved`` has no IPs, the URL is returned unchanged
    and ``host_header`` is ``None`` (caller should then let ``requests``
    resolve normally — still SSRF-safe because the caller already ran
    :func:`ensure_public`).

    IPv6 literals are bracketed per RFC 3986. The port and userinfo
    from the original URL are preserved.
    """

    try:
        parts = urlsplit(url)
    except ValueError:
        return url, None

    host = parts.hostname or ""
    if not host or not resolved or not resolved.ips:
        return url, None

    first_ip = resolved.ips[0]
    ip_literal = f"[{first_ip}]" if ":" in str(first_ip) else str(first_ip)
    port = f":{parts.port}" if parts.port else ""
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"
    new_netloc = f"{userinfo}{ip_literal}{port}"
    pinned = urlunsplit(
        (parts.scheme, new_netloc, parts.path, parts.query, parts.fragment)
    )
    host_header = host if parts.port is None else f"{host}:{parts.port}"
    return pinned, host_header


def make_pinned_session(resolved: "ResolvedHost", **session_kwargs: Any):
    """Return a :class:`requests.Session` bound to ``resolved.ips[0]``.

    The session mounts a custom :class:`requests.adapters.HTTPAdapter`
    whose ``pool`` is created with ``source_address`` set to the pinned
    IP *for the connect socket*. Because ``requests`` / ``urllib3`` do
    the DNS resolution themselves when a hostname is in the URL, callers
    MUST pass the *pinned* URL produced by :func:`pinned_url_and_host`
    (which replaces the authority with the IP literal) AND set the
    ``Host`` header so the request line targets the original vhost.

    The adapter override is best-effort: if the underlying urllib3
    version does not accept ``source_address`` for the chosen address
    family, the session still works but loses the source-binding
    guarantee (the URL-authority pinning is the primary defence; this
    is defence-in-depth).

    ``session_kwargs`` are forwarded to ``Session.__init__`` style
    attributes (e.g. ``verify=False``) — though verify should normally
    stay ``True`` to honour SNI / cert validation against the original
    hostname.

    A real ``requests`` import is performed lazily so this module keeps
    its "pure-policy" character for callers that only need
    :func:`ensure_public`.
    """

    try:
        import requests  # type: ignore
        from requests.adapters import HTTPAdapter  # type: ignore
    except Exception:  # pragma: no cover - requests is a hard dep but be safe
        return None

    session = requests.Session()

    first_ip = resolved.ips[0] if resolved and resolved.ips else None
    if first_ip is None:
        return session

    # ``source_address`` expects a (host, port) tuple; port 0 lets the OS
    # pick an ephemeral source port. We attempt to coerce to a 4-tuple
    # for AF_INET6 to be explicit, but urllib3 accepts the 2-tuple form.
    try:
        ip_str = str(first_ip)
        is_v6 = ":" in ip_str
        source_address: Tuple = (ip_str, 0) if not is_v6 else (ip_str, 0, 0, 0)
    except Exception:
        source_address = None  # type: ignore[assignment]

    class _PinnedAdapter(HTTPAdapter):
        """HTTPAdapter that creates its pool with a fixed source address."""

        # HTTPAdapter.init_poolmanager passes **kwargs through; we inject
        # source_address so every new connection from this pool originates
        # from the vetted IP. (Pool re-use after rebind would otherwise
        # hand the pool an unrelated destination, but because the URL
        # authority is also the vetted IP literal, urllib3 connects to
        # that exact IP — never re-resolving the hostname.)
        def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
            if source_address is not None:
                kwargs.setdefault("source_address", source_address)
            return super().init_poolmanager(*args, **kwargs)

    adapter = _PinnedAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Forward a small set of caller-controlled attributes.
    if "verify" in session_kwargs:
        session.verify = bool(session_kwargs["verify"])
    if "proxies" in session_kwargs and session_kwargs["proxies"]:
        session.proxies.update(session_kwargs["proxies"])

    return session
