"""Short-lived media URL diagnostics.

This module centralizes best-effort detection for CDN-signed / ephemeral media
URLs. It never mutates URLs and only exposes parameter names plus derived timing
metadata, so callers can log or display risk summaries without leaking token
values.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

SIGNED_QUERY_NAMES: frozenset[str] = frozenset(
    {
        "expires",
        "expire",
        "expiry",
        "exp",
        "e",
        "end",
        "endtime",
        "token",
        "signature",
        "sig",
        "policy",
        "key-pair-id",
        "x-amz-date",
        "x-amz-expires",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-date",
        "x-goog-expires",
        "x-goog-signature",
        "auth_key",
        "hdntl",
        "hdnts",
    }
)

EPOCH_QUERY_NAMES: frozenset[str] = frozenset(
    {"expires", "expire", "expiry", "exp", "e", "end", "endtime"}
)

TTL_WARNING_SECONDS = 15 * 60


@dataclass(frozen=True)
class MediaURLTTL:
    """Best-effort short-lived URL analysis result."""

    url: str
    detected_at: float = field(default_factory=time.time)
    ephemeral: bool = False
    expires_at: float | None = None
    expires_in_seconds: float | None = None
    matched_params: list[str] = field(default_factory=list)
    risk_level: str = "low"
    reason: str = "ordinary_url"

    def to_dict(self) -> dict:
        """Return a plain dictionary for UI/task metadata."""
        return {
            "url": self.url,
            "detected_at": self.detected_at,
            "ephemeral": self.ephemeral,
            "expires_at": self.expires_at,
            "expires_in_seconds": self.expires_in_seconds,
            "matched_params": list(self.matched_params),
            "risk_level": self.risk_level,
            "reason": self.reason,
        }


def _strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_epoch_seconds(value: str) -> float | None:
    raw = _strip_quotes(str(value or "")).strip()
    if not re.fullmatch(r"\d{8,13}", raw):
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    if number > 10_000_000_000:
        number = int(number / 1000)
    if number < 946684800:  # 2000-01-01T00:00:00Z
        return None
    return float(number)


def _parse_compact_utc(value: str) -> float | None:
    raw = _strip_quotes(str(value or "")).strip()
    if not raw:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _query_map(url: str) -> dict[str, list[str]]:
    try:
        parsed = urlparse(url or "")
        query = parse_qs(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return {}
    return {str(key).lower(): values for key, values in query.items()}


def matched_signed_query_params(url: str) -> list[str]:
    """Return matched signed/ephemeral query parameter names."""
    lowered = _query_map(url)
    return sorted(name for name in lowered if name in SIGNED_QUERY_NAMES)


def estimate_expiry_timestamp(url: str) -> float | None:
    """Infer an absolute expiry timestamp from known signed URL query forms."""
    lowered = _query_map(url)
    if not lowered:
        return None

    expires_at: float | None = None
    for name in EPOCH_QUERY_NAMES:
        values = lowered.get(name) or []
        if not values:
            continue
        parsed_epoch = _parse_epoch_seconds(values[0])
        if parsed_epoch is not None:
            expires_at = parsed_epoch if expires_at is None else min(expires_at, parsed_epoch)

    for date_name, ttl_name in (("x-amz-date", "x-amz-expires"), ("x-goog-date", "x-goog-expires")):
        date_values = lowered.get(date_name) or []
        ttl_values = lowered.get(ttl_name) or []
        if not date_values or not ttl_values:
            continue
        signed_at = _parse_compact_utc(date_values[0])
        try:
            ttl_seconds = int(str(ttl_values[0]).strip())
        except (TypeError, ValueError):
            ttl_seconds = 0
        if signed_at is not None and ttl_seconds > 0:
            candidate = signed_at + ttl_seconds
            expires_at = candidate if expires_at is None else min(expires_at, candidate)

    return expires_at


def seconds_until_expiry(url: str, *, now: float | None = None) -> float | None:
    """Return seconds until URL expiry when it can be inferred."""
    expires_at = estimate_expiry_timestamp(url)
    if expires_at is None:
        return None
    base = time.time() if now is None else float(now)
    return round(float(expires_at) - base, 3)


def is_probably_ephemeral_media_url(url: str) -> bool:
    """Return True when URL carries known signed/temporary query markers."""
    return bool(matched_signed_query_params(url) or estimate_expiry_timestamp(url) is not None)


def analyze_media_url_ttl(
    url: str,
    *,
    now: float | None = None,
    warning_threshold_seconds: int = TTL_WARNING_SECONDS,
) -> MediaURLTTL:
    """Analyze whether a media URL is signed/short-lived and how risky it is."""
    detected_at = time.time() if now is None else float(now)
    params = matched_signed_query_params(url)
    expires_at = estimate_expiry_timestamp(url)
    expires_in = round(float(expires_at) - detected_at, 3) if expires_at is not None else None
    ephemeral = bool(params or expires_at is not None)

    if not ephemeral:
        risk_level = "low"
        reason = "ordinary_url"
    elif expires_in is None:
        risk_level = "medium"
        reason = "signed_params_without_expiry"
    elif expires_in <= 0:
        risk_level = "expired"
        reason = "expired"
    elif expires_in <= warning_threshold_seconds:
        risk_level = "high"
        reason = "expires_soon"
    else:
        risk_level = "medium"
        reason = "signed_url"

    return MediaURLTTL(
        url=url or "",
        detected_at=detected_at,
        ephemeral=ephemeral,
        expires_at=expires_at,
        expires_in_seconds=expires_in,
        matched_params=params,
        risk_level=risk_level,
        reason=reason,
    )
