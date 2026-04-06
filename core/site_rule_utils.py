"""
Helpers for matching persisted site rules safely.
"""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_rule_domain(domain: str) -> str:
    """Normalize rule domains into plain lowercase hostnames."""
    text = (domain or "").strip()
    if not text:
        return ""

    parsed = urlparse(text if "://" in text else f"//{text}")
    host = parsed.hostname or text.split("/", 1)[0]
    return host.strip().lower().lstrip(".")


def extract_hostname(value: str) -> str:
    """Extract lowercase hostname from a URL-like value."""
    text = (value or "").strip()
    if not text:
        return ""

    parsed = urlparse(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").strip().lower()


def host_matches_domain(host: str, domain: str) -> bool:
    """Match exact host or subdomain, but never substring-adjacent domains."""
    normalized_host = extract_hostname(host)
    normalized_domain = normalize_rule_domain(domain)
    if not normalized_host or not normalized_domain:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def site_rule_matches(rule: dict, url: str, page_url: str = "") -> bool:
    """Return True when a site rule applies to the URL/page context."""
    url_lower = (url or "").lower()
    url_keywords = [str(k).strip().lower() for k in rule.get("url_keywords", []) if str(k).strip()]

    domains = [
        normalized
        for normalized in (normalize_rule_domain(domain) for domain in rule.get("domains", []))
        if normalized
    ]
    if domains:
        url_host = extract_hostname(url)
        page_host = extract_hostname(page_url)
        if not any(host_matches_domain(url_host, domain) or host_matches_domain(page_host, domain) for domain in domains):
            return False

    if url_keywords and not any(keyword in url_lower for keyword in url_keywords):
        return False

    return True


def has_header_key(headers: dict, key: str) -> bool:
    """Case-insensitive header existence check."""
    lookup = (key or "").strip().lower()
    return any(str(existing_key).lower() == lookup for existing_key in (headers or {}))


def set_header_if_missing(headers: dict, key: str, value: str) -> bool:
    """Set a header only when a case-insensitive equivalent is absent."""
    if not value or has_header_key(headers, key):
        return False
    headers[key] = value
    return True
