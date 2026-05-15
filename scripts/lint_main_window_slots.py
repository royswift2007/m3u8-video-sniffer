"""
Stage 4 lint — MainWindow UI slot signatures (task 26.3 / 30.1 / R29).

Walks ``ui/main_window.py`` (and the extracted split modules
``ui/main_window_actions.py`` and ``ui/main_window_sniff_flow.py`` if they
define the slot) with an AST visitor and asserts that the
``task_update_received`` slot — the R29 immutable-snapshot channel — has a
typed ``TaskSnapshot`` parameter annotation. The goal is to make the
signature contract enforceable at authoring time so a future drive-by
refactor cannot accidentally reintroduce a raw ``DownloadTask`` on the
UI thread.

Rules enforced:

1. The method ``task_update_received`` MUST exist in at least one of the
   scanned UI modules.
2. It MUST be a method on a class (not a free function).
3. It MUST accept exactly one non-``self`` positional parameter whose
   annotation is the name ``TaskSnapshot`` (either bare or qualified via
   a module alias; we accept any attribute chain that ends in
   ``TaskSnapshot``).
4. The return annotation SHOULD be ``None`` (warning, not failure) — a
   missing / mismatched return annotation does not break the signal
   wiring so we keep it a soft check.

Offline, synchronous; exits 0 on pass, 1 on deviation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_CANDIDATES: tuple[str, ...] = (
    "ui/main_window.py",
    "ui/main_window_actions.py",
    "ui/main_window_sniff_flow.py",
)
SLOT_NAME = "task_update_received"
EXPECTED_PARAM_TYPE = "TaskSnapshot"


def _annotation_name(node: ast.expr | None) -> str | None:
    """Return the terminal name of an annotation expression.

    ``TaskSnapshot`` → ``TaskSnapshot``.
    ``core.task_model.TaskSnapshot`` → ``TaskSnapshot``.
    ``Optional[TaskSnapshot]`` / strings / None → ``None``.
    """

    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # ``"TaskSnapshot"`` forward-ref — tolerate.
        return node.value.strip().rstrip("'\"").rsplit(".", 1)[-1]
    return None


def _iter_method_defs(tree: ast.Module) -> Iterable[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield ``(class_name, method_node)`` pairs for every method in ``tree``."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for body_node in node.body:
            if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node.name, body_node


def check_file(path: Path) -> tuple[bool, list[str]]:
    """Return ``(found_slot, failures)`` for the given source file."""

    failures: list[str] = []
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"{path}: read failed: {exc}")
        return False, failures

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        failures.append(f"{path}: parse failed: {exc}")
        return False, failures

    found = False
    for class_name, method in _iter_method_defs(tree):
        if method.name != SLOT_NAME:
            continue
        found = True
        args = method.args
        # Reject free-function style (no ``self``) defensively — the AST
        # walker already restricts to class methods, but a misconfigured
        # @staticmethod would slip through here.
        positional = list(args.args)
        if any(
            isinstance(dec, ast.Name) and dec.id == "staticmethod"
            for dec in method.decorator_list
        ):
            failures.append(
                f"{path}:{method.lineno}: {class_name}.{SLOT_NAME} must be an "
                "instance method, not a staticmethod"
            )
            continue
        if not positional:
            failures.append(
                f"{path}:{method.lineno}: {class_name}.{SLOT_NAME} missing "
                "'self' parameter"
            )
            continue
        # Expect exactly one snapshot parameter after self.
        non_self = positional[1:]
        if len(non_self) != 1:
            failures.append(
                f"{path}:{method.lineno}: {class_name}.{SLOT_NAME} must accept "
                f"exactly one non-self parameter, got {len(non_self)}"
            )
            continue
        param = non_self[0]
        ann_name = _annotation_name(param.annotation)
        if ann_name != EXPECTED_PARAM_TYPE:
            failures.append(
                f"{path}:{method.lineno}: {class_name}.{SLOT_NAME}({param.arg}) "
                f"annotation must be '{EXPECTED_PARAM_TYPE}', got "
                f"{ast.unparse(param.annotation) if param.annotation else '<missing>'}"
            )
            continue
        # Soft-check: return annotation.
        ret_name = _annotation_name(method.returns)
        if ret_name not in (None, "None"):
            # Not a hard failure — emit a warning line but keep going.
            print(
                f"[lint_main_window_slots] warning: {path}:{method.lineno}: "
                f"{class_name}.{SLOT_NAME} return annotation is "
                f"'{ret_name}', expected 'None'",
                file=sys.stderr,
                flush=True,
            )

    return found, failures


def main() -> int:
    any_found = False
    all_failures: list[str] = []
    scanned: list[Path] = []

    for rel in UI_CANDIDATES:
        path = PROJECT_ROOT / rel
        if not path.is_file():
            # Missing optional split module — skip silently; the primary
            # ``ui/main_window.py`` file is the one that must carry the slot.
            continue
        scanned.append(path)
        found, failures = check_file(path)
        any_found = any_found or found
        all_failures.extend(failures)

    if not scanned:
        print(
            "[lint_main_window_slots] FAIL: none of the UI candidate files "
            f"were found: {UI_CANDIDATES}",
            flush=True,
        )
        return 1

    if not any_found:
        print(
            f"[lint_main_window_slots] FAIL: '{SLOT_NAME}' slot not defined "
            f"in any of {', '.join(str(p.relative_to(PROJECT_ROOT)) for p in scanned)}",
            flush=True,
        )
        return 1

    if all_failures:
        print("[lint_main_window_slots] FAIL:", flush=True)
        for line in all_failures:
            print(f"  - {line}", flush=True)
        return 1

    rels = ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in scanned)
    print(
        f"[lint_main_window_slots] OK: '{SLOT_NAME}' has TaskSnapshot "
        f"annotation across [{rels}]",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
