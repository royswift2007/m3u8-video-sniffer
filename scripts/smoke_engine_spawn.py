"""
Stage 4 smoke: ``BaseEngine.spawn`` dispatch audit
(task 29.2 / 30.1 / R37.1, R37.2).

Named ``smoke_engine_spawn`` to match the Stage 4 gate manifest
(``.kiro/specs/security-stability-hardening/gate/stage-4.yaml``). The
actual verification logic lives in
:mod:`scripts.smoke_engine_spawn_migration`, which performs:

1. An AST scan of ``engines/*.py`` to flag rogue ``subprocess.Popen`` call
   sites (anything outside the single allowed location, ``base_engine.py``).
2. A runtime probe that monkey-patches ``BaseEngine.spawn`` and invokes
   each engine's ``download`` entry point to confirm the call funnels
   through ``spawn`` rather than directly constructing a ``Popen``.

This wrapper keeps the gate manifest self-describing — one smoke script
per requirement bullet — without duplicating the scan logic.

Exits 0 when every engine routes through ``BaseEngine.spawn``; exits 1
(via the delegate) on any rogue ``Popen`` or missed ``spawn`` call.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DELEGATE_PATH = PROJECT_ROOT / "scripts" / "smoke_engine_spawn_migration.py"


def _load_delegate() -> ModuleType:
    """Import the sibling migration smoke without assuming a package layout.

    ``scripts/`` is not a Python package (no ``__init__.py``), so the
    usual ``import`` machinery cannot reach the sibling module when this
    script is launched as ``python scripts/smoke_engine_spawn.py`` from
    the project root. :mod:`importlib.util` loads the file directly.
    """

    spec = importlib.util.spec_from_file_location(
        "m3u8d_smoke_engine_spawn_migration", DELEGATE_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - safety only
        raise RuntimeError(f"failed to load delegate spec from {DELEGATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    print(
        "[smoke_engine_spawn] running BaseEngine.spawn dispatch audit "
        f"(delegates to {DELEGATE_PATH.relative_to(PROJECT_ROOT)})",
        flush=True,
    )
    if not DELEGATE_PATH.is_file():
        print(
            f"[smoke_engine_spawn] FAIL: delegate not found at {DELEGATE_PATH}",
            flush=True,
        )
        return 1
    delegate = _load_delegate()
    return int(delegate.main())


if __name__ == "__main__":
    sys.exit(main())
