"""
Stage 4 smoke: bare-except / silent broad-except budget enforcement
(task 25.3 / 30.1 / R26.3, R26.4).

Named ``smoke_bare_except_grep`` to match the Stage 4 gate manifest
(``.kiro/specs/security-stability-hardening/gate/stage-4.yaml``), but the
implementation delegates to :mod:`scripts.lint_bare_except` which already
does the heavy lifting: AST-level scan (so ``except`` clauses inside
strings are never matched), a hard ban on ``except: pass`` and
unannotated ``except Exception: pass``, and a budget of at most 3
NOSONAR-annotated broad catches across the whole tree.

A plain grep-based equivalent would misfire on string literals and on
formatted source that puts ``pass`` on the same line as ``except``; the
linter's AST walk is what guarantees the "≤ 3 NOSONAR" budget demanded
by R26.4. Wrapping it here keeps the Stage 4 gate manifest self-describing
(one smoke script per requirement bullet) without duplicating the scan
logic.

Exits 0 when the codebase is within budget; exits 1 (via the underlying
linter) when any bare-except / plain-except violation or over-budget
NOSONAR entry is detected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LINT_PATH = PROJECT_ROOT / "scripts" / "lint_bare_except.py"


def _load_linter() -> ModuleType:
    """Import the sibling ``lint_bare_except`` module without assuming a package.

    ``scripts/`` is not a Python package (no ``__init__.py``), so the usual
    ``import scripts.lint_bare_except`` would fail when this file is run
    via ``python scripts/smoke_bare_except_grep.py`` from the project
    root. Instead we load the file directly via :mod:`importlib.util`.
    """

    spec = importlib.util.spec_from_file_location(
        "m3u8d_lint_bare_except", LINT_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - safety only
        raise RuntimeError(f"failed to load linter spec from {LINT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    print(
        "[smoke_bare_except_grep] running AST-level bare-except audit "
        f"(delegates to {LINT_PATH.relative_to(PROJECT_ROOT)})",
        flush=True,
    )
    if not LINT_PATH.is_file():
        print(
            f"[smoke_bare_except_grep] FAIL: linter not found at {LINT_PATH}",
            flush=True,
        )
        return 1
    linter = _load_linter()
    return int(linter.main())


if __name__ == "__main__":
    sys.exit(main())
