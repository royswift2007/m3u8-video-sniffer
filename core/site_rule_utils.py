"""
Helpers for matching persisted site rules safely.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from urllib.parse import urlparse


_RESOURCE_TYPE_ALIASES: dict[str, str] = {
    "*": "*",
    "all": "*",
    "any": "*",
    "hls": "hls",
    "m3u8": "hls",
    "playlist": "hls",
    "dash": "dash",
    "mpd": "dash",
    "direct": "direct_video",
    "direct_video": "direct_video",
    "video": "direct_video",
    "page": "page",
    "webpage": "page",
    "segment": "segment",
    "fragment": "segment",
    "unknown": "unknown",
}


def _as_list(value) -> list[str]:
    """Return config value as a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "," in text:
            return [part.strip() for part in text.split(",") if part.strip()]
        return [text]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _normalize_resource_type(value: str) -> str:
    """Normalize rule resource-type aliases to canonical download-context names."""
    key = (value or "").strip().lower().replace("-", "_")
    return _RESOURCE_TYPE_ALIASES.get(key, key)


def _rule_target_resource_types(rule: dict) -> set[str]:
    """Collect ``apply_to`` / ``resource_type`` filters from a rule."""
    targets: set[str] = set()
    for key in ("apply_to", "resource_type", "resource_types"):
        for value in _as_list(rule.get(key)):
            normalized = _normalize_resource_type(value)
            if normalized:
                targets.add(normalized)
    return targets


def _infer_rule_resource_type(url: str) -> str:
    """Lightweight fallback inference used only for rule filtering."""
    parsed = urlparse(url or "")
    path = (parsed.path or url or "").lower()
    if ".m3u8" in path:
        return "hls"
    if ".mpd" in path:
        return "dash"
    if path.endswith((".ts", ".m4s", ".aac", ".key")):
        return "segment"
    if path.endswith((".mp4", ".flv", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".f4v")):
        return "direct_video"
    return "page" if (url or "").strip() else "unknown"


def _path_candidates(value: str) -> list[str]:
    """Build path/url candidates for pattern matching without leaking credentials."""
    text = (value or "").strip().lower()
    if not text:
        return []
    parsed = urlparse(text)
    path = parsed.path or ""
    candidates = [text]
    if path:
        candidates.append(path)
        candidates.append(path.lstrip("/"))
        if parsed.query:
            candidates.append(f"{path}?{parsed.query}")
    return candidates


def _matches_any_pattern(patterns: list[str], candidates: list[str]) -> bool:
    """Return True when any shell-style pattern matches any candidate."""
    if not patterns:
        return True
    lowered_patterns = [pattern.strip().lower() for pattern in patterns if pattern.strip()]
    if not lowered_patterns:
        return True
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in lowered_patterns
        for candidate in candidates
    )


def site_rule_priority(rule: dict) -> int:
    """Return a numeric priority for deterministic rule ordering."""
    try:
        return int(rule.get("priority", 0) or 0)
    except (TypeError, ValueError):
        return 0


def iter_matching_site_rules(
    rules: Iterable[dict] | None,
    url: str,
    page_url: str = "",
    resource_type: str = "",
):
    """Yield matching rules sorted by priority while preserving legacy order ties."""
    candidates: list[tuple[int, int, dict]] = []
    for index, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            continue
        if site_rule_matches(rule, url, page_url, resource_type=resource_type):
            candidates.append((site_rule_priority(rule), -index, rule))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _priority, _order, rule in candidates:
        yield rule


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


def site_rule_matches(
    rule: dict,
    url: str,
    page_url: str = "",
    *,
    resource_type: str = "",
) -> bool:
    """Return True when a site rule applies to the URL/page/resource context."""
    if not isinstance(rule, dict):
        return False
    if rule.get("enabled") is False:
        return False

    url_lower = (url or "").lower()
    page_url_lower = (page_url or "").lower()
    url_keywords = [str(k).strip().lower() for k in rule.get("url_keywords", []) if str(k).strip()]

    targets = _rule_target_resource_types(rule)
    if targets and "*" not in targets:
        actual_type = _normalize_resource_type(resource_type) or _infer_rule_resource_type(url)
        if actual_type not in targets:
            return False

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

    url_patterns = _as_list(rule.get("url_patterns")) + _as_list(rule.get("url_pattern"))
    if url_patterns and not _matches_any_pattern(url_patterns, [url_lower, page_url_lower]):
        return False

    path_patterns = (
        _as_list(rule.get("path_patterns"))
        + _as_list(rule.get("path_pattern"))
        + _as_list(rule.get("paths"))
        + _as_list(rule.get("path"))
    )
    if path_patterns:
        candidates = _path_candidates(url) + _path_candidates(page_url)
        if not _matches_any_pattern(path_patterns, candidates):
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
