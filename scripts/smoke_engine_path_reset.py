"""
R7 smoke check — ``utils.engine_paths.validate_engine_exe`` rejects system binaries.

A tampered ``config.json::engines.*.path`` that points at, say,
``C:\\Windows\\notepad.exe`` must be rejected so :class:`ConfigManager`
can fall back to the default bundled binary. The function signals
rejection by returning ``None``.

Offline, synchronous, exits 0 on pass / 1 on any deviation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.engine_paths import validate_engine_exe  # noqa: E402


def _assert_none(path: str) -> None:
    result = validate_engine_exe(path)
    if result is not None:
        raise AssertionError(
            f"validate_engine_exe({path!r}) should return None, got {result!r}"
        )


def main() -> int:
    if os.name == "nt":
        # Canonical Windows system binary; must not be accepted as a valid
        # engine exe even though the file exists and has an .exe suffix.
        _assert_none(r"C:\Windows\notepad.exe")
        # Another system binary outside the trusted roots.
        _assert_none(r"C:\Windows\System32\cmd.exe")
    else:
        # Cross-platform smoke: the POSIX equivalent should also be rejected
        # because it has no .exe suffix AND lives outside the safe roots.
        _assert_none("/usr/bin/vim")
        _assert_none("/bin/sh")

    # Non-existent / malformed entries must also be rejected regardless of
    # platform — these check the pre-filesystem guards (empty, UNC, ..).
    _assert_none("")
    _assert_none("..\\evil.exe")
    _assert_none("\\\\server\\share\\evil.exe")

    print(
        "PASS smoke_engine_path_reset: system binaries + traversal entries all "
        "rejected"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL smoke_engine_path_reset: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"FAIL smoke_engine_path_reset: unexpected error {exc!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
