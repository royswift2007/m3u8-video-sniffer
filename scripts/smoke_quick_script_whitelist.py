"""
R5 smoke check — ``ui.main_window.ALLOWED_QUICK_SCRIPTS`` integrity.

Asserts that the code-level whitelist:

* contains the known-good ``download_tools.bat`` entry; and
* does NOT contain a traversal-laden ``..\\evil.bat`` string or any other
  control-char / path-prefixed variant that an attacker might try to
  smuggle through the quick-manual dialog.

The whitelist is a module-level :class:`frozenset`, so importing
``ui.main_window`` is enough — no QApplication or window is spawned.
Offline, synchronous, exits 0 on pass / 1 on any deviation.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.main_window import ALLOWED_QUICK_SCRIPTS  # noqa: E402


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def main() -> int:
    _assert(
        "download_tools.bat" in ALLOWED_QUICK_SCRIPTS,
        "'download_tools.bat' must remain in ALLOWED_QUICK_SCRIPTS",
    )
    _assert(
        "..\\evil.bat" not in ALLOWED_QUICK_SCRIPTS,
        "'..\\\\evil.bat' must NEVER appear in ALLOWED_QUICK_SCRIPTS",
    )
    _assert(
        "../evil.bat" not in ALLOWED_QUICK_SCRIPTS,
        "'../evil.bat' must NEVER appear in ALLOWED_QUICK_SCRIPTS",
    )
    # Any whitelisted entry must be a bare filename — no directory separators,
    # schemes, or control characters. This is a structural invariant that
    # catches accidental future additions like ``"scripts/foo.bat"``.
    for entry in ALLOWED_QUICK_SCRIPTS:
        _assert(
            isinstance(entry, str) and entry,
            f"whitelist entry must be non-empty str, got {entry!r}",
        )
        _assert(
            "/" not in entry and "\\" not in entry,
            f"whitelist entry must be a bare filename, got {entry!r}",
        )
        _assert(
            ":" not in entry,
            f"whitelist entry must not contain a scheme/drive, got {entry!r}",
        )
        for ch in entry:
            _assert(
                ord(ch) >= 0x20,
                f"whitelist entry {entry!r} contains a control character",
            )

    print(
        f"PASS smoke_quick_script_whitelist: {len(ALLOWED_QUICK_SCRIPTS)} entries, "
        "traversal strings absent"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL smoke_quick_script_whitelist: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"FAIL smoke_quick_script_whitelist: unexpected error {exc!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
