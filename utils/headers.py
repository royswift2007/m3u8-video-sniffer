"""Header sanitization helpers for cross-origin header forwarding.

This module is the single source of truth for the set of HTTP request
headers that are *safe to forward* from an untrusted origin (browser
extension / CLI `--headers` / sniffer capture) into an engine command
line per Requirement 6 of ``security-stability-hardening``:

    6.1  header name must match ``[A-Za-z0-9-]`` and length ≤ 64
    6.2  header value must NOT contain ``\\r`` / ``\\n`` / ``\\0``
         and UTF-8 byte length ≤ 4096
    6.3  only the names in :data:`FORWARDABLE_HEADER_ALLOWLIST` are kept;
         ``Cookie`` / ``Authorization`` can be additionally gated by caller
         policy, and everything else is dropped with a structured warning
    6.4  output uses canonical ("HTTP-style") casing so that downstream
         argv entries render as ``-H "Cookie: <value>"`` regardless of
         how the upstream client cased the name
    6.5  duplicate names (case-insensitive) are collapsed to the last
         occurrence so a later value wins a race with an earlier one
    6.6  the caller forwards this dict to the engine as parameterized
         argv (``["-H", f"{name}: {value}"]``) — never via string
         concatenation (enforced at the call sites, not here)

The helper is pure, side-effect-free except for a ``logger.warning``
call on each dropped entry, and depends only on the standard library
plus :mod:`utils.logger`. It is therefore safe to import from both
``core.catcatch_server`` and ``main.py`` without pulling any heavier
modules in.

Exports:
    FORWARDABLE_HEADER_ALLOWLIST  — frozenset of canonical-cased names
    MAX_HEADER_NAME_LEN           — 64
    MAX_HEADER_VALUE_BYTES        — 4096
    sanitize_headers(raw)         — the main entry point
"""

from __future__ import annotations

import re
from typing import Mapping

from utils.logger import logger


__all__ = (
    "FORWARDABLE_HEADER_ALLOWLIST",
    "MAX_HEADER_NAME_LEN",
    "MAX_HEADER_VALUE_BYTES",
    "sanitize_headers",
    "normalized_forward_headers",
    "classify_range_header",
    "prepare_aria2_headers",
    "iter_engine_headers",
)


# ---------------------------------------------------------------------------
# Constants (canonical-cased names; R6.4)
# ---------------------------------------------------------------------------

#: Canonical-cased names for the forwardable headers. Kept as a frozenset of
#: canonical-cased names so R33 (``main.py --headers``) can reuse the same
#: constant without round-tripping through ``.lower()`` / ``.title()``.
#:
#: 2026-06 engine_success_rate_optimization: expanded from 5 to 12 entries to
#: forward browser-context headers (Sec-Fetch-*, Sec-Ch-Ua-*, Accept, Range)
#: that CDN / anti-hotlink checks commonly inspect. Authorization remains
#: outside the default allowlist and is accepted only through an explicit
#: caller-side opt-in.
FORWARDABLE_HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "Referer",
        "User-Agent",
        "Origin",
        "Cookie",
        "Accept",
        "Accept-Language",
        "Range",
        "Sec-Fetch-Site",
        "Sec-Fetch-Mode",
        "Sec-Fetch-Dest",
        "Sec-Ch-Ua",
        "Sec-Ch-Ua-Mobile",
        "Sec-Ch-Ua-Platform",
    }
)

# Lower-cased lookup table mapping ``name.lower()`` → canonical form.
# Built once at import time; `sanitize_headers` uses it to normalize the
# output key to the canonical casing declared above.
_CANONICAL_BY_LOWER: dict[str, str] = {
    name.lower(): name for name in FORWARDABLE_HEADER_ALLOWLIST
}

_OPT_IN_HEADER_ALLOWLIST: frozenset[str] = frozenset({"Authorization"})
_OPT_IN_CANONICAL_BY_LOWER: dict[str, str] = {
    name.lower(): name for name in _OPT_IN_HEADER_ALLOWLIST
}

#: Length ceiling per R6.1.
MAX_HEADER_NAME_LEN: int = 64

#: Byte-length ceiling per R6.2. Measured in UTF-8 bytes because the
#: receiving engines serialize the value as UTF-8 on the command line.
MAX_HEADER_VALUE_BYTES: int = 4096

# R6.1: ``[A-Za-z0-9-]`` only. ASCII range is deliberate — HTTP RFC 7230
# forbids non-ASCII characters in field names, and allowing them here would
# open a door for homoglyph spoofing of the allowlist.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")

# R6.2: the three CR / LF / NUL characters that enable header injection.
_FORBIDDEN_VALUE_CHARS = ("\r", "\n", "\x00")

# Conservative single-range classifier used by Aria2 diagnostics.  It is
# deliberately narrow: multi-range, suffix-range and malformed values are
# classified as ``other`` and dropped by the Aria2-specific policy.
_RANGE_SINGLE_RE = re.compile(
    r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def sanitize_headers(
    raw_headers: Mapping[str, str] | None,
    *,
    include_cookie: bool = True,
    include_authorization: bool = False,
) -> dict[str, str]:
    """Return a cleaned, allowlist-filtered copy of ``raw_headers``.

    Validation rules (matching the design §1.9 pseudocode and R6):

    1. ``name`` must be a non-empty ``str`` matching ``[A-Za-z0-9-]`` with
       length ≤ :data:`MAX_HEADER_NAME_LEN`.
    2. ``value`` must be a ``str`` (or a type coercible to ``str`` via
       ``str()``), must not contain ``\\r`` / ``\\n`` / ``\\0``, and its
       UTF-8 byte length must be ≤ :data:`MAX_HEADER_VALUE_BYTES`.
    3. ``name`` (case-insensitive) must appear in
       :data:`FORWARDABLE_HEADER_ALLOWLIST`.
    4. On duplicate names (case-insensitive) the *last* occurrence wins, so
       callers passing a client-captured header bag don't accidentally
       forward a stale / overwritten value.

    Rejected entries are dropped silently from the returned dict and logged
    via :func:`logger.warning` with the header *name* only — never the
    value — so that sensitive tokens (Cookie / Authorization spoofs) never
    leak into the log even when the record is rejected. The log also tags
    the structured reason so observers can distinguish "bad name" from
    "bad value" from "not on allowlist".

    Parameters
    ----------
    raw_headers:
        Mapping of header name to header value. ``None`` and empty
        mappings are accepted and yield an empty dict.
    include_cookie:
        Keep ``Cookie`` when true. Callers handling untrusted origins can
        turn this off while preserving the historical default.
    include_authorization:
        Keep ``Authorization`` only when explicitly true. The default stays
        fail-closed to avoid forwarding bearer tokens into engine argv.

    Returns
    -------
    dict[str, str]
        Canonical-cased header name → validated value. The ordering
        preserves insertion order of the *kept* entries (Python 3.7+
        dict ordering).
    """

    if not raw_headers:
        return {}
    # ``isinstance(..., Mapping)`` rejects plain lists / tuples. A
    # defensive check keeps the function total when the caller hands us a
    # surprise value (e.g. a JSON array from a malformed POST body).
    if not isinstance(raw_headers, Mapping):
        logger.warning(
            "[headers] sanitize_headers: input is not a Mapping, dropping all",
            event="header_sanitize_non_mapping",
            stage="sanitize_headers",
            type=type(raw_headers).__name__,
        )
        return {}

    cleaned: dict[str, str] = {}

    for raw_name, raw_value in raw_headers.items():
        # --- 1. Validate name ------------------------------------------------
        if not isinstance(raw_name, str):
            logger.warning(
                "[headers] 丢弃非字符串 header name",
                event="header_dropped",
                stage="sanitize_headers",
                reason="name_not_str",
                name_type=type(raw_name).__name__,
            )
            continue

        name = raw_name.strip()
        if not name:
            logger.warning(
                "[headers] 丢弃空 header name",
                event="header_dropped",
                stage="sanitize_headers",
                reason="name_empty",
            )
            continue

        if len(name) > MAX_HEADER_NAME_LEN:
            logger.warning(
                f"[headers] 丢弃超长 header name (len={len(name)})",
                event="header_dropped",
                stage="sanitize_headers",
                reason="name_too_long",
                name=name[:MAX_HEADER_NAME_LEN],
                length=len(name),
            )
            continue

        if not _HEADER_NAME_RE.match(name):
            logger.warning(
                f"[headers] 丢弃非法 header name: {name!r}",
                event="header_dropped",
                stage="sanitize_headers",
                reason="name_invalid_chars",
                name=name,
            )
            continue

        # --- 2. Check allowlist / caller policy (case-insensitive) ----------
        name_lower = name.lower()
        canonical = _CANONICAL_BY_LOWER.get(name_lower)
        if canonical is None and include_authorization:
            canonical = _OPT_IN_CANONICAL_BY_LOWER.get(name_lower)
        if canonical == "Cookie" and not include_cookie:
            logger.warning(
                "[headers] Cookie 转发被调用方策略关闭，已丢弃",
                event="header_dropped",
                stage="sanitize_headers",
                reason="cookie_disabled",
                name="Cookie",
            )
            continue
        if canonical is None:
            # Log the name (safe) — never the value (could be a secret).
            logger.warning(
                f"[headers] header 不在白名单,丢弃: {name!r}",
                event="header_dropped",
                stage="sanitize_headers",
                reason="not_in_allowlist",
                name=name,
            )
            continue

        # --- 3. Validate value ----------------------------------------------
        # Accept plain strings only; other types are coerced via ``str`` so
        # a caller that hands us e.g. ``Cookie: 123`` (int) still works,
        # but outright rejects things like ``None``.
        if raw_value is None:
            logger.warning(
                f"[headers] 丢弃 None value: {canonical}",
                event="header_dropped",
                stage="sanitize_headers",
                reason="value_none",
                name=canonical,
            )
            continue
        if not isinstance(raw_value, str):
            try:
                value = str(raw_value)
            except Exception as e:
                logger.warning(
                    f"[headers] 丢弃不可字符串化 value: {canonical} ({e})",
                    event="header_dropped",
                    stage="sanitize_headers",
                    reason="value_not_coercible",
                    name=canonical,
                    error_type=type(e).__name__,
                )
                continue
        else:
            value = raw_value

        if any(ch in value for ch in _FORBIDDEN_VALUE_CHARS):
            # Do NOT log the value — it may contain a deliberate injection
            # payload we don't want to echo into the log.
            logger.warning(
                f"[headers] 丢弃含控制符的 header value: {canonical}",
                event="header_dropped",
                stage="sanitize_headers",
                reason="value_has_crlf_nul",
                name=canonical,
            )
            continue

        try:
            byte_len = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as e:
            logger.warning(
                f"[headers] 丢弃不可 UTF-8 编码的 header value: {canonical} ({e})",
                event="header_dropped",
                stage="sanitize_headers",
                reason="value_not_utf8",
                name=canonical,
                error_type=type(e).__name__,
            )
            continue

        if byte_len > MAX_HEADER_VALUE_BYTES:
            logger.warning(
                f"[headers] 丢弃超长 header value: {canonical} "
                f"(bytes={byte_len})",
                event="header_dropped",
                stage="sanitize_headers",
                reason="value_too_long",
                name=canonical,
                length_bytes=byte_len,
            )
            continue

        # --- 4. Deduplicate by canonical name (last occurrence wins) -------
        # ``dict`` keeps insertion order; overwriting the key preserves the
        # slot position, but callers that care about "last wins" should not
        # depend on iteration order beyond this documented contract.
        cleaned[canonical] = value

    return cleaned


# ---------------------------------------------------------------------------
# Engine command-building helpers (engine_success_rate_optimization)
# ---------------------------------------------------------------------------


def normalized_forward_headers(
    raw_headers: Mapping[str, str] | None,
    *,
    include_cookie: bool = True,
    include_authorization: bool = False,
) -> dict[str, str]:
    """Return lower-case-key headers safe for engine command construction.

    Wraps :func:`sanitize_headers` and normalises the output so that
    engine command builders can do a simple ``headers.get("referer")``
    without worrying about casing.

    1. Calls :func:`sanitize_headers` for allowlist / injection / length.
    2. Output keys are always lowercase.
    3. Empty-value entries (``""``) are omitted.
    4. Cookie value is never logged by this function (that's
       :func:`sanitize_headers`'s job).
    """
    clean = sanitize_headers(
        raw_headers,
        include_cookie=include_cookie,
        include_authorization=include_authorization,
    )
    result: dict[str, str] = {}
    for key, value in clean.items():
        if value not in (None, ""):
            result[key.lower()] = value
    return result


def classify_range_header(value: object | None) -> str:
    """Classify a captured HTTP ``Range`` header without exposing its value.

    Return values are intentionally compact and stable for log/test use:
    ``absent`` (no usable value), ``bytes_0`` (single byte range starting at
    zero), ``bytes_nonzero`` (single byte range starting after zero), and
    ``other`` (suffix, multi-range, unsupported unit, malformed, etc.).
    """
    if value is None:
        return "absent"
    try:
        text = str(value).strip()
    except Exception:
        return "other"
    if not text:
        return "absent"

    match = _RANGE_SINGLE_RE.match(text)
    if not match:
        return "other"

    start = match.group(1)
    if start == "":
        return "other"
    try:
        start_int = int(start)
    except ValueError:
        return "other"
    return "bytes_0" if start_int == 0 else "bytes_nonzero"


def _aria2_range_drop_reason(range_kind: str) -> str:
    if range_kind == "absent":
        return ""
    if range_kind == "bytes_0":
        return "aria2_manages_range"
    if range_kind == "bytes_nonzero":
        return "nonzero_range"
    return "unsupported_range"


def _last_raw_header_value(
    raw_headers: Mapping[str, str] | None,
    lower_name: str,
) -> object | None:
    """Return the last raw header value for ``lower_name`` if present."""
    if not isinstance(raw_headers, Mapping):
        return None
    found: object | None = None
    for raw_name, raw_value in raw_headers.items():
        if isinstance(raw_name, str) and raw_name.strip().lower() == lower_name:
            found = raw_value
    return found


def prepare_aria2_headers(
    raw_headers: Mapping[str, str] | None,
    *,
    include_cookie: bool = True,
    include_authorization: bool = False,
) -> tuple[dict[str, str], dict[str, object]]:
    """Return Aria2-safe lower-case headers plus a non-sensitive summary.

    Aria2 manages byte ranges and resumable segmented requests internally, so
    the browser-captured ``Range`` header is always removed from the returned
    header map.  The companion summary records only booleans/counts/categories
    so logs can explain the policy decision without leaking Cookie,
    Authorization, or signed URL tokens.
    """
    safe = normalized_forward_headers(
        raw_headers,
        include_cookie=include_cookie,
        include_authorization=include_authorization,
    )

    raw_range_value = _last_raw_header_value(raw_headers, "range")
    range_value = raw_range_value if raw_range_value is not None else safe.get("range")
    range_kind = classify_range_header(range_value)
    safe.pop("range", None)

    range_dropped = range_kind != "absent"
    summary: dict[str, object] = {
        "has_range": range_dropped,
        "range_kind": range_kind,
        "range_forwarded": False,
        "range_dropped": range_dropped,
        "range_drop_reason": _aria2_range_drop_reason(range_kind),
        "range_action": "drop" if range_dropped else "none",
        "has_cookie": bool(safe.get("cookie")),
        "cookie_len": len(safe.get("cookie", "")),
        "header_count": len(safe),
        "has_referer": bool(safe.get("referer")),
        "has_user_agent": bool(safe.get("user-agent")),
        "has_origin": bool(safe.get("origin")),
    }
    return safe, summary


def iter_engine_headers(
    raw_headers: Mapping[str, str] | None,
    *,
    include_cookie: bool = True,
    include_range: bool = True,
    include_authorization: bool = False,
    exclude: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Return canonical header pairs for engine argv builders.

    Each element is ``(canonical_name, value)``, e.g.
    ``("User-Agent", "Mozilla/5.0 ...")``, ready to pass to
    ``-H`` / ``--header`` / ``--add-header`` flags.

    Parameters
    ----------
    raw_headers: input header dict (may be ``None``).
    include_cookie: when *False*, the ``Cookie`` header is omitted
        (useful for engines like yt-dlp that handle cookies separately).
    include_range: when *False*, the ``Range`` header is omitted
        (useful when the engine re-requests partial content on its own).
    include_authorization: when *True*, forward ``Authorization`` as an
        explicit opt-in for trusted site rules.
    exclude: optional set of *lowercase* header names to drop.
    """
    safe = normalized_forward_headers(
        raw_headers,
        include_cookie=include_cookie,
        include_authorization=include_authorization,
    )
    pairs: list[tuple[str, str]] = []
    exclude_lower = {k.lower() for k in (exclude or set())}

    # canonical name to lowercase key mapping — mirrors the allowed set
    for lower_key, value in safe.items():
        if lower_key in exclude_lower:
            continue
        if lower_key == "cookie" and not include_cookie:
            continue
        if lower_key == "range" and not include_range:
            continue

        # Recover the canonical name from the default or opt-in allowlist.
        canonical = _CANONICAL_BY_LOWER.get(
            lower_key,
            _OPT_IN_CANONICAL_BY_LOWER.get(lower_key, lower_key),
        )
        pairs.append((canonical, value))

    return pairs
