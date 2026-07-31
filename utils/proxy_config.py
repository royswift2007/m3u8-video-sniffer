"""Proxy configuration helpers.

F-11 centralizes the parsing of ``config["proxy"]`` so every network path
(engine argv builders and lightweight requests probes) interprets the switch in
the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


@dataclass(frozen=True)
class ProxySettings:
    """Normalized proxy settings read from runtime config."""

    enabled: bool = False
    http: str = ""
    https: str = ""

    def proxy_for_url(self, url: str = "") -> str:
        """Return the best CLI proxy value for ``url``, or ``""``.

        Prefer the scheme-specific proxy when available. Fall back to the other
        configured endpoint so a single HTTP proxy can cover HTTPS downloads and
        vice versa.
        """

        if not self.enabled:
            return ""
        scheme = ""
        try:
            scheme = (urlparse(url).scheme or "").lower()
        except Exception:
            scheme = ""
        if scheme == "https":
            return self.https or self.http
        if scheme == "http":
            return self.http or self.https
        return self.http or self.https

    def requests_proxies(self) -> Optional[dict[str, str]]:
        """Return a ``requests``-compatible proxies dict, or ``None``."""

        if not self.enabled:
            return None
        proxies: dict[str, str] = {}
        if self.http:
            proxies["http"] = self.http
        if self.https:
            proxies["https"] = self.https
        elif self.http:
            proxies["https"] = self.http
        return proxies or None


def _valid_proxy_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    proxy = value.strip()
    if not proxy:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in proxy):
        return ""
    try:
        parsed = urlparse(proxy)
    except Exception:
        return ""
    if parsed.scheme.lower() not in _ALLOWED_PROXY_SCHEMES:
        return ""
    if not parsed.netloc:
        return ""
    return proxy


def load_proxy_settings(config_provider: Any = None) -> ProxySettings:
    """Load and normalize proxy settings from ``utils.config_manager.config``.

    ``config_provider`` is injectable for tests and only needs a ``get`` method
    compatible with the project config manager.
    """

    if config_provider is None:
        try:
            from utils.config_manager import config as config_provider
        except Exception:
            return ProxySettings()

    try:
        raw = config_provider.get("proxy", {}) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    enabled = bool(raw.get("enabled", False))
    http = _valid_proxy_url(raw.get("http", ""))
    https = _valid_proxy_url(raw.get("https", ""))
    if enabled and not (http or https):
        enabled = False
    return ProxySettings(enabled=enabled, http=http, https=https)


def proxy_for_url(url: str = "", config_provider: Any = None) -> str:
    """Convenience helper for CLI engines."""

    return load_proxy_settings(config_provider).proxy_for_url(url)


def requests_proxies(config_provider: Any = None) -> Optional[dict[str, str]]:
    """Convenience helper for ``requests`` calls."""

    return load_proxy_settings(config_provider).requests_proxies()
