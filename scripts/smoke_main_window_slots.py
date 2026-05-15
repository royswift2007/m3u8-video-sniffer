"""
Stage 4 smoke: MainWindow typed-slot + TaskSnapshot serialization audit
(task 26.3 / Requirement 29.1, 29.2, 29.3).

This smoke is the CI-enforceable wrapper around the R29 lint contract:

1. **R29.1 / R29.2 — typed slot** — delegates to
   :mod:`scripts.lint_main_window_slots`, which walks
   ``ui/main_window.py`` / ``ui/main_window_actions.py`` /
   ``ui/main_window_sniff_flow.py`` and asserts that
   ``task_update_received`` is declared with a ``TaskSnapshot``
   parameter annotation. The AST check is the authoring-time lint
   mirror of ``mypy --strict`` / ``pyright`` (see ``mypy.ini`` /
   ``pyrightconfig.json``); it runs without requiring the type
   checkers to be installed in the CI environment, so the R29 contract
   is enforced in every ``stage_gate.py --stage 4`` invocation even if
   the pyright/mypy binaries are absent.

2. **R29.3 — stable ``TaskSnapshot`` serialization** — instantiates a
   :class:`core.task_model.TaskSnapshot` and verifies that
   ``to_dict`` returns a dictionary whose key order matches
   ``TaskSnapshot._SERIALIZED_FIELDS`` exactly. A reorder (or a new
   field without a corresponding ``_SERIALIZED_FIELDS`` update) breaks
   downstream JSON/telemetry consumers, which is the explicit failure
   mode R29.3 exists to prevent.

Why a wrapper (mirroring ``smoke_bare_except_audit.py``):

* ``scripts/stage_gate.py``'s final-report harvester uses the
  ``smoke_*.py`` prefix to distinguish gate smokes from ad-hoc dev
  tools; keeping the R29 lint under that prefix lets the coverage
  matrix (R38.3) recognise it as an automation hook for P3-5 / R29.
* It gives us a single seam to add the ``TaskSnapshot.to_dict``
  determinism assertion alongside the AST lint without touching
  ``lint_main_window_slots.py`` (which the dev workflow can also
  invoke directly without pulling in ``core.task_model``).

Returns 0 on success, non-zero on the first failing check.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ensure ``scripts/`` is importable regardless of the working directory
# used by ``stage_gate.py`` (``sys.executable <script>`` leaves
# ``sys.path[0]`` as the scripts dir, but an explicit insert keeps the
# wrapper import-safe when invoked via ``python -m`` or under a test
# harness).
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
# And the project root so ``from core.task_model import TaskSnapshot``
# resolves when the gate invokes us with ``cwd=PROJECT_ROOT`` (the
# common case) as well as from a fresh shell.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lint_main_window_slots import main as _slot_lint_main  # noqa: E402


def _check_task_snapshot_serialization() -> tuple[bool, str]:
    """Return ``(ok, message)`` for the R29.3 serialization contract.

    The snapshot must:

    * be a ``@dataclass(frozen=True)`` (so the UI thread can never mutate
      it);
    * expose a ``_SERIALIZED_FIELDS`` tuple that enumerates every field
      the wire contract promises;
    * emit ``to_dict()`` with keys in exactly that order, so downstream
      log / telemetry consumers see a stable JSON shape across versions.

    A failure in any of these checks is returned as a non-zero smoke
    exit; the message is short and identifies which sub-check tripped.
    """

    try:
        import dataclasses
        from core.task_model import TaskSnapshot
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"TaskSnapshot import failed: {exc!r}"

    if not dataclasses.is_dataclass(TaskSnapshot):
        return False, "TaskSnapshot is not a dataclass"

    # ``frozen`` surfaces as ``__dataclass_params__.frozen`` on the class.
    params = getattr(TaskSnapshot, "__dataclass_params__", None)
    if params is None or not getattr(params, "frozen", False):
        return False, "TaskSnapshot is not declared frozen (@dataclass(frozen=True))"

    serialized = getattr(TaskSnapshot, "_SERIALIZED_FIELDS", None)
    if not isinstance(serialized, tuple) or not serialized:
        return False, "TaskSnapshot._SERIALIZED_FIELDS missing or empty"

    # Every serialized field must correspond to a real dataclass field
    # (protects against a typo in _SERIALIZED_FIELDS drifting from the
    # dataclass schema).
    dc_field_names = {f.name for f in dataclasses.fields(TaskSnapshot)}
    missing = [name for name in serialized if name not in dc_field_names]
    if missing:
        return (
            False,
            f"_SERIALIZED_FIELDS references unknown dataclass field(s): {missing!r}",
        )

    # Build a minimal snapshot instance; every field on TaskSnapshot is
    # declared without a default, so we must pass one positional value per
    # field. Use neutral sentinels that are trivially JSON-encodable so
    # the determinism check doesn't depend on redact_url or from_task.
    try:
        sample = TaskSnapshot(
            task_id="smoke-task",
            url_masked="https://example.invalid/",
            title="smoke-title",
            engine="n_m3u8dl_re",
            status="waiting",
            progress=0.0,
            speed_bps=0,
            stop_reason=None,
            error=None,
            updated_at=0.0,
        )
    except Exception as exc:
        return False, f"TaskSnapshot construction failed: {exc!r}"

    try:
        wire = sample.to_dict()
    except Exception as exc:
        return False, f"TaskSnapshot.to_dict() raised: {exc!r}"

    if not isinstance(wire, dict):
        return False, f"TaskSnapshot.to_dict() returned {type(wire).__name__}, expected dict"

    wire_keys = tuple(wire.keys())
    if wire_keys != serialized:
        return (
            False,
            "TaskSnapshot.to_dict() key order drifted from _SERIALIZED_FIELDS: "
            f"to_dict={wire_keys!r} expected={serialized!r}",
        )

    # Running to_dict twice on the same frozen instance MUST produce the
    # same mapping (no hidden time.time() / random sources); this is the
    # runtime counterpart to R29.3's "序列化稳定".
    wire2 = sample.to_dict()
    if wire != wire2:
        return (
            False,
            "TaskSnapshot.to_dict() produced different output on repeat call — "
            f"first={wire!r} second={wire2!r}",
        )

    return True, ""


def main() -> int:
    print(
        "[smoke_main_window_slots] Stage 4 R29 audit — "
        "typed slot + TaskSnapshot serialization",
        flush=True,
    )

    # --- 1. AST lint (R29.1 / R29.2) ---
    rc = _slot_lint_main()
    if rc != 0:
        print(
            f"[smoke_main_window_slots] FAIL: lint_main_window_slots exit={rc}",
            flush=True,
            file=sys.stderr,
        )
        return rc

    # --- 2. TaskSnapshot serialization contract (R29.3) ---
    ok, msg = _check_task_snapshot_serialization()
    if not ok:
        print(
            f"[smoke_main_window_slots] FAIL: TaskSnapshot serialization — {msg}",
            flush=True,
            file=sys.stderr,
        )
        return 1

    print(
        "[smoke_main_window_slots] PASS: task_update_received typed slot OK, "
        "TaskSnapshot.to_dict() key order stable against _SERIALIZED_FIELDS",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
