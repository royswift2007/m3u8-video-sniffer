"""
Stage 4 smoke: bare-except audit (task 30.1 / R26.3, R26.4, R38.1).

Thin wrapper around :mod:`scripts.lint_bare_except` so that the Stage 4 gate
yaml can reference the audit under the ``smoke_*.py`` naming convention
that :func:`scripts.stage_gate.run_smoke_scripts` expects.

Why a wrapper instead of listing ``scripts/lint_bare_except.py`` directly
in ``gate/stage-4.yaml``:

* The gate file is the canonical inventory of smoke scripts for reporting
  purposes (``smoke_final_report.py`` counts entries to populate the
  "# smoke scripts" metric). Keeping every gate entry under the
  ``smoke_*.py`` prefix lets the harvester distinguish gate smokes from
  ad-hoc dev tools without hand-maintained allowlists.
* A wrapper also gives us a single seam to add Stage-4-specific preflight
  (e.g., a rootdir sanity print) without touching the lint module
  referenced elsewhere (e.g., CI lint jobs in task 25.3).

The wrapper intentionally does no work of its own — it imports
``lint_bare_except.main`` and returns its exit code. Any changes to the
lint policy (bare except / NOSONAR budget) stay in ``lint_bare_except.py``
so both the CI lint job and this smoke observe the same rule set.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "scripts") not in sys.path:
    # Allow running both as ``python scripts/smoke_bare_except_audit.py``
    # and as ``python -m scripts.smoke_bare_except_audit`` without pip
    # installing the project. The stage-gate runner invokes us with
    # ``sys.executable <script>`` so sys.path[0] is the scripts dir, but
    # the explicit insert keeps us import-safe in both modes.
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from lint_bare_except import main as _lint_main  # noqa: E402


def main() -> int:
    print(
        "[smoke_bare_except_audit] Stage 4 R26 audit — delegating to "
        "scripts/lint_bare_except.py",
        flush=True,
    )
    rc = _lint_main()
    if rc == 0:
        print("[smoke_bare_except_audit] PASS", flush=True)
    else:
        print(
            f"[smoke_bare_except_audit] FAIL (lint_bare_except exit={rc})",
            flush=True,
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
