"""
Aggregate offline smoke checks for component update flows.

Runs the component update smoke scripts in a deterministic order using the
current Python interpreter. The child scripts are expected to avoid real
network downloads, real UI interaction, and writes to repository component
installation targets.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SMOKE_SCRIPTS = [
    Path("scripts/smoke_component_remote_release.py"),
    Path("scripts/smoke_component_update_download.py"),
    Path("scripts/smoke_component_update_install.py"),
    Path("scripts/smoke_component_update_service.py"),
    Path("scripts/smoke_component_update_ui.py"),
]


def run_script(script: Path) -> int:
    script_path = PROJECT_ROOT / script
    print(f"\n=== running {script.as_posix()} ===", flush=True)
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    if result.returncode != 0:
        print(f"=== FAILED {script.as_posix()} exit={result.returncode} ===", flush=True)
        return result.returncode
    print(f"=== passed {script.as_posix()} ===", flush=True)
    return 0


def main() -> int:
    missing = [script.as_posix() for script in SMOKE_SCRIPTS if not (PROJECT_ROOT / script).is_file()]
    if missing:
        print("missing smoke script(s): " + ", ".join(missing), file=sys.stderr)
        return 2

    for script in SMOKE_SCRIPTS:
        code = run_script(script)
        if code != 0:
            return code

    print("\nall component update smoke checks passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
