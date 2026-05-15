"""Header sanitization helpers for cross-origin header forwarding.

This module is the single source of truth for the set of HTTP request
headers that are *safe to forward* from an untrusted origin (browser
extension / CLI `--headers` / sniffer capture) into an engine command
line per Requirement 6 of ``security-stability-hardening``:

    6.1  header name must match ``[A-Za-z0-9-]`` and length ≤ 64
    6.2  header value must NOT contain ``\\r`` / ``\\n`` / ``\\0``
         and UTF-8 byte length ≤ 4096
    6.3  only the names in :data:`FORWARDABLE_HEADER_ALLOWLIST` are kept;
         everything else is dropped with a structured warning
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
)


# ---------------------------------------------------------------------------
# Constants (canonical-cased names; R6.4)
# ---------------------------------------------------------------------------

#: Canonical casing for the 5 forwardable headers. Kept as a frozenset of
#: canonical-cased names so R33 (``main.py --headers``) can reuse the same
#: constant without round-tripping through ``.lower()`` / ``.title()``.
FORWARDABLE_HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "Referer",
        "User-Agent",
        "Origin",
        "Cookie",
        "Accept-Language",
    }
)

# Lower-cased lookup table mapping ``name.lower()`` → canonical form.
# Built once at import time; `sanitize_headers` uses it to normalize the
# output key to the canonical casing declared above.
_CANONICAL_BY_LOWER: dict[str, str] = {
    name.lower(): name for name in FORWARDABLE_HEADER_ALLOWLIST
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


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def sanitize_headers(raw_headers: Mapping[str, str] | None) -> dict[str, str]:
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

        # --- 2. Check allowlist (case-insensitive) --------------------------
        canonical = _CANONICAL_BY_LOWER.get(name.lower())
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
