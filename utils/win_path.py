"""Windows filename robustness utility.

Pure function for sanitizing arbitrary user/site-provided titles into safe
Windows path components. Part of Stage 2 (P1-5) of the
``security-stability-hardening`` feature.

Guarantees:
    * No I/O, no randomness, never raises.
    * Deterministic given its inputs (``timestamp_fn`` is an explicit input).
    * Output is a valid Windows path component:
        - No ASCII control chars (``0x00``-``0x1f``).
        - No reserved chars: ``< > : " / \\ | ? *``.
        - No trailing ``.`` or `` ``.
        - Stem (portion before the first ``.``) is never a Windows reserved
          device name (``CON`` / ``PRN`` / ``AUX`` / ``NUL`` /
          ``COM1``-``COM9`` / ``LPT1``-``LPT9``), case-insensitive.
        - UTF-8 byte length ``<= max_abs_bytes`` (default ``240``), truncated
          with a ``sha1[:8]`` suffix when necessary to preserve uniqueness.
    * Empty input falls back to ``"<fallback_prefix>_<timestamp>"``.

Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable, Tuple

__all__ = ["sanitize_title"]


# Windows reserved device names (case-insensitive). Matching rule is applied
# to the stem (portion before the first '.') of the cleaned title.
_RESERVED: frozenset = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Characters forbidden in Windows filenames plus all ASCII control codes.
# NOTE: backslash and forward slash are both forbidden because the result
# MUST be a single path component (no directory separators leaking in).
_INVALID: frozenset = frozenset('<>:"/\\|?*') | frozenset(
    chr(c) for c in range(0x00, 0x20)
)


def _split_stem_suffix(name: str) -> Tuple[str, str]:
    """Split ``name`` on the first ``.`` into (stem, suffix_including_dot).

    Examples:
        ``"CON.txt"`` -> ``("CON", ".txt")``
        ``"foo.tar.gz"`` -> ``("foo", ".tar.gz")``
        ``"plain"`` -> ``("plain", "")``

    Using the *first* dot (rather than ``pathlib.PurePath.stem`` which splits
    on the last dot) guarantees that names like ``"CON.txt"`` become
    ``"CON_.txt"`` after appending ``"_"`` to the stem, instead of looping
    forever because ``Path("CON.txt_").stem`` would still return ``"CON"``.
    """
    idx = name.find(".")
    if idx < 0:
        return name, ""
    return name[:idx], name[idx:]


def _default_timestamp() -> int:
    """Return the current unix timestamp.

    Only invoked as the ``timestamp_fn`` default when the cleaned title is
    empty. Callers that need strict determinism (e.g. property tests) SHOULD
    inject a frozen callable via ``timestamp_fn``.
    """
    return int(time.time())


def sanitize_title(
    title: str,
    *,
    fallback_prefix: str = "media",
    max_abs_bytes: int = 240,
    timestamp_fn: Callable[[], int] = _default_timestamp,
) -> str:
    """Return a Windows-safe filename stem derived from ``title``.

    Args:
        title: Arbitrary user/site-provided string. ``None`` is treated as
            empty.
        fallback_prefix: Prefix used when ``title`` becomes empty after
            cleansing.
        max_abs_bytes: Maximum allowed UTF-8 byte length of the result.
            Defaults to ``240`` to leave room for directory, extension, and
            temporary suffix within Windows ``MAX_PATH`` (260).
        timestamp_fn: Callable returning an ``int`` timestamp; used only on
            the empty-input fallback path. Injectable to keep the function
            deterministic in tests.

    Returns:
        A string safe to use as a Windows path component. Never raises.
    """
    if title is None:
        title = ""

    # Step 1: replace invalid chars with '_' and strip trailing dots / spaces.
    cleaned = "".join("_" if c in _INVALID else c for c in title).rstrip(" .")
    if not cleaned:
        return f"{fallback_prefix}_{timestamp_fn()}"

    # Step 2: avoid collisions with Windows reserved device names by
    # appending '_' to the stem (portion up to the first '.') until it is no
    # longer reserved. The loop terminates after at most one iteration:
    # "<reserved>_" can never itself be reserved because reserved names are
    # strictly alpha / alphanumeric and contain no '_'.
    stem, suffix = _split_stem_suffix(cleaned)
    while stem.upper() in _RESERVED:
        stem = stem + "_"
    cleaned = stem + suffix

    # Step 3: enforce the UTF-8 byte budget. When too long, truncate at a
    # codepoint boundary and append an 8-char sha1 digest of the *full*
    # pre-truncation bytes to keep outputs unique across distinct long
    # titles. The final length is guaranteed to be <= max_abs_bytes.
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_abs_bytes:
        digest = hashlib.sha1(encoded).hexdigest()[:8]
        # Reserve room for the separator '_' + 8-byte digest.
        keep = max_abs_bytes - len(digest) - 1
        if keep < 0:
            keep = 0
        # ``errors="ignore"`` drops any partial UTF-8 sequence at the cut,
        # so the re-encoded head never exceeds ``keep`` bytes. Strip
        # trailing dots/spaces that may have been exposed by the cut.
        head = encoded[:keep].decode("utf-8", "ignore").rstrip(" .")
        cleaned = f"{head}_{digest}"

    return cleaned
