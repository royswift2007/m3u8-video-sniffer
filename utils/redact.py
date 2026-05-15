"""Pure-function redaction helpers for argv / URL / HTTP headers.

The functions here MUST NOT perform I/O and MUST NOT rely on anything
beyond the standard library. They are the single source of truth for
"what counts as sensitive" across the application (engine command logs,
CatCatch headers, ``main.py`` CLI echo, etc.) as required by Requirement 3
and Requirement 6 of ``security-stability-hardening``.

Exports:
    SENSITIVE_HEADERS     -- frozenset of lowercased header names treated
                             as secrets (Cookie / Authorization / ...).
    SENSITIVE_QUERY_KEYS  -- frozenset of URL query keys treated as secrets
                             (token / sign / signature / auth).
    REDACTED              -- canonical placeholder string.
    redact_argv(argv)     -- redact a subprocess argv list.
    redact_url(url)       -- redact sensitive query values inside a URL.
    redact_header_line(n,v) -- produce a safe ``"Name: value"`` log line.
"""

from __future__ import annotations

from typing import Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


__all__ = (
    "SENSITIVE_HEADERS",
    "SENSITIVE_QUERY_KEYS",
    "REDACTED",
    "redact_argv",
    "redact_url",
    "redact_header_line",
)


# Header names are compared case-insensitively; keep lowercase in the set.
SENSITIVE_HEADERS: frozenset = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-session-token",
        "user-agent",
        "referer",
        "origin",
    }
)

# URL query keys compared case-insensitively; keep lowercase in the set.
# Audit-finding High #3: expanded beyond the original four to cover
# common OAuth / JWT / cloud-CDN signing conventions so signed URLs from
# services like AWS CloudFront, Aliyun OSS, Tencent Cloud COS, GCS, and
# Azure never leak through the logging pipeline in plaintext.
SENSITIVE_QUERY_KEYS: frozenset = frozenset(
    {
        # --- original set ---
        "token",
        "sign",
        "signature",
        "auth",
        # --- OAuth / bearer / session ---
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "session",
        "sessionid",
        "sid",
        "sessid",
        "api_key",
        "apikey",
        "key",
        "secret",
        "password",
        "passwd",
        # --- CDN / cloud-storage presigned URL parameters ---
        "sig",
        "expires",
        "policy",
        "credential",
        "x-amz-signature",
        "x-amz-security-token",
        "x-amz-credential",
        "x-goog-signature",
        "x-goog-credential",
        "key-pair-id",
        "keyid",
        "uid_tid",
        "encoded_signature",
        "pksign",
    }
)

REDACTED: str = "<redacted>"


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def redact_url(url: str) -> str:
    """Return ``url`` with values of sensitive query keys replaced.

    Only the *value* of a matching key is replaced; the key itself is
    preserved so logs remain useful for debugging. Non-URL strings (or
    URLs without a recognizable query) are returned unchanged.
    """

    if not isinstance(url, str) or not url:
        return url

    # ``urlsplit`` accepts arbitrary strings; a bare "token=abc" without
    # scheme will produce an empty query and bail out below.
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.query:
        return url

    # ``keep_blank_values=True`` avoids losing keys with empty values.
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    new_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if key.lower() in SENSITIVE_QUERY_KEYS:
            new_pairs.append((key, REDACTED))
            changed = True
        else:
            new_pairs.append((key, value))

    if not changed:
        return url

    new_query = urlencode(new_pairs, doseq=False, safe="")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


# ---------------------------------------------------------------------------
# Header redaction
# ---------------------------------------------------------------------------


def _is_sensitive_header_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    return name.strip().lower() in SENSITIVE_HEADERS


def redact_header_line(name: str, value: str) -> str:
    """Return ``"Name: value"`` with the value redacted if ``name`` is sensitive.

    Used by engine command logging so the same predicate governs both
    ``-H "X: Y"`` style argv entries and free-form log formatting.
    """

    if not isinstance(name, str):
        name = str(name) if name is not None else ""
    if not isinstance(value, str):
        value = str(value) if value is not None else ""

    if _is_sensitive_header_name(name):
        return f"{name}: {REDACTED}"
    return f"{name}: {value}"


def _redact_inline_header(value: str) -> str:
    """Redact a header expressed as ``Name: Value`` inside a single argv entry."""

    colon = value.find(":")
    if colon <= 0:
        return value
    name = value[:colon]
    # Preserve the whitespace after the colon so the output looks identical
    # to the input except for the redacted value.
    rest = value[colon + 1 :]
    if _is_sensitive_header_name(name):
        # Keep any leading whitespace between ``:`` and the value so the
        # resulting line keeps its human-readable shape.
        stripped = rest.lstrip()
        prefix = rest[: len(rest) - len(stripped)]
        return f"{name}:{prefix}{REDACTED}"
    return value


def _redact_cookie_pair(value: str) -> str:
    """Redact a ``name=value`` (or ``a=1; b=2``) cookie expression."""

    if not value or "=" not in value:
        return REDACTED if value else value
    # Redact every value in a ``;``-separated cookie string.
    parts = []
    for raw in value.split(";"):
        chunk = raw.strip()
        if not chunk:
            parts.append(raw)
            continue
        if "=" not in chunk:
            parts.append(REDACTED)
            continue
        name, _, _ = chunk.partition("=")
        parts.append(f"{name}={REDACTED}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# argv redaction
# ---------------------------------------------------------------------------


# Flags that take a header-style ``Name: Value`` argument in the NEXT argv
# element. Lowercased for case-insensitive comparison.
_HEADER_PAIR_FLAGS: frozenset = frozenset(
    {
        "-h",
        "--header",
        "--add-header",
        "--add-headers",
    }
)

# Flags that take a cookie-style argument in the NEXT argv element. Value
# may be ``name=value`` (single cookie) or a semicolon-separated string.
_COOKIE_PAIR_FLAGS: frozenset = frozenset(
    {
        "--http-cookie",
        "--cookie",
        "-b",
    }
)

# Flags whose NEXT argv element should be fully redacted regardless of
# structure (file paths to cookie jars, raw tokens, etc.).
_OPAQUE_VALUE_FLAGS: frozenset = frozenset(
    {
        "--cookies",
        "--cookies-from-browser",
        "--cookie-jar",
        "--load-cookies",
        "--save-cookies",
        "--session-token",
        "--x-session-token",
    }
)

# Flags whose value carries a URL that needs query redaction.
_URL_VALUE_FLAGS: frozenset = frozenset(
    {
        "--url",
        "--referer",
        "--origin",
        "--proxy",
    }
)


def _split_flag(arg: str) -> tuple[str, str | None]:
    """Split ``--flag=value`` into ``("--flag", "value")``.

    For ``-X`` bare flags returns ``(arg, None)``.
    """

    if not arg or not arg.startswith("-"):
        return arg, None
    if "=" in arg:
        flag, _, rest = arg.partition("=")
        return flag, rest
    return arg, None


def _rebuild(flag: str, had_eq: bool, original: str, new_value: str) -> str:
    """Recombine a ``--flag=value`` argument after redaction.

    Falls back to the ``flag=REDACTED`` form if the argument used ``=``.
    """

    if had_eq:
        return f"{flag}={new_value}"
    # Caller handled the separate-argv case; not used when had_eq is False.
    return original


def redact_argv(argv: Sequence[str] | Iterable[str]) -> list[str]:
    """Return a new argv list with sensitive values replaced by ``<redacted>``.

    Known patterns covered:
        -H "Name: Value"            (both ``-H NAME:VAL`` and ``-H=NAME:VAL``)
        --header "Name: Value"
        --add-header "Name: Value"
        --http-cookie name=value
        --cookies path/to/jar       (entire path redacted)
        --url http://host/?token=x  (token value redacted, URL otherwise kept)

    The input is NOT mutated. Non-string entries are coerced via ``str``
    to keep the function total.
    """

    if argv is None:
        return []

    # Materialize iterators once; str() coercion keeps the function total.
    items: list[str] = [a if isinstance(a, str) else str(a) for a in argv]
    out: list[str] = []
    i = 0
    n = len(items)
    while i < n:
        arg = items[i]
        flag, inline_value = _split_flag(arg)
        flag_l = flag.lower()

        # --- inline "--flag=value" form --------------------------------
        if inline_value is not None:
            if flag_l in _HEADER_PAIR_FLAGS:
                out.append(_rebuild(flag, True, arg, _redact_inline_header(inline_value)))
            elif flag_l in _COOKIE_PAIR_FLAGS:
                out.append(_rebuild(flag, True, arg, _redact_cookie_pair(inline_value)))
            elif flag_l in _OPAQUE_VALUE_FLAGS:
                out.append(_rebuild(flag, True, arg, REDACTED))
            elif flag_l in _URL_VALUE_FLAGS:
                out.append(_rebuild(flag, True, arg, redact_url(inline_value)))
            else:
                out.append(arg)
            i += 1
            continue

        # --- separate "--flag VALUE" form ------------------------------
        if flag_l in _HEADER_PAIR_FLAGS and i + 1 < n:
            out.append(arg)
            out.append(_redact_inline_header(items[i + 1]))
            i += 2
            continue
        if flag_l in _COOKIE_PAIR_FLAGS and i + 1 < n:
            out.append(arg)
            out.append(_redact_cookie_pair(items[i + 1]))
            i += 2
            continue
        if flag_l in _OPAQUE_VALUE_FLAGS and i + 1 < n:
            out.append(arg)
            out.append(REDACTED)
            i += 2
            continue
        if flag_l in _URL_VALUE_FLAGS and i + 1 < n:
            out.append(arg)
            out.append(redact_url(items[i + 1]))
            i += 2
            continue

        # --- bare positional: may itself be a URL ---------------------
        # A value that parses as an absolute http(s) URL is redacted so
        # that engines that accept the URL as a positional argument still
        # get their token/sign query values scrubbed.
        if arg[:7].lower() == "http://" or arg[:8].lower() == "https://":
            out.append(redact_url(arg))
        else:
            out.append(arg)
        i += 1

    return out
