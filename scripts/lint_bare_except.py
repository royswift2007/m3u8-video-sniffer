"""
Stage 4 lint — bare except / unannotated ``except Exception: pass`` policing
(task 25.3 / 30.1 / R26.3, R26.4).

Scans ``core/``, ``engines/``, ``ui/``, ``utils/`` recursively for two
anti-patterns that R26.3/R26.4 prohibit:

* **Bare ``except: pass``** — catches every exception including
  ``BaseException`` subclasses (``KeyboardInterrupt``, ``SystemExit``) and
  silently discards it. Always a failure; any hit exits non-zero.
* **Unannotated ``except Exception: pass``** — catches broadly and
  silently. Permitted only when paired with an explanatory
  ``# NOSONAR: <reason>`` comment on the ``except`` line (the R26.4 escape
  valve). The lint tolerates at most **3** NOSONAR-annotated exceptions
  across the full tree; above that threshold the rule forces the
  maintainer to either narrow the exception type or justify a ceiling
  bump in the spec.

The checker deliberately uses Python AST parsing (not raw grep) so that
``except`` clauses inside strings / comments are never flagged and
multi-line ``except Exception:\n    pass`` patterns are matched the same
way as single-line ones. Bodies that contain statements other than
``pass`` (e.g. ``logger.debug(...)``) are treated as structured handlers
and never trip the lint.

Offline, synchronous. Exits 0 on pass, 1 on any violation.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS: tuple[str, ...] = ("core", "engines", "ui", "utils")
NOSONAR_BUDGET = 3  # Max unannotated (NOSONAR-tagged) ``except Exception: pass``.


@dataclass(frozen=True)
class Finding:
    path: Path
    lineno: int
    kind: str  # "bare" | "nosonar" | "plain_exception"
    snippet: str


def _is_body_only_pass(body: list[ast.stmt]) -> bool:
    """Return True iff the handler body is a single ``pass`` statement.

    Docstrings are technically an ``ast.Expr(Constant)`` but R26 does not
    treat them as silenced — a docstring-only handler is still silent, so
    we coerce to the same "silent" bucket by accepting only pure ``pass``.
    Anything with logging / re-raise / real work is fine.
    """

    if len(body) != 1:
        return False
    stmt = body[0]
    return isinstance(stmt, ast.Pass)


def _handler_source_line(source_lines: list[str], handler: ast.ExceptHandler) -> str:
    """Return the raw source of the ``except`` line for comment inspection.

    The ``ast`` module does not preserve comments, so to detect the
    ``# NOSONAR: ...`` marker we must fall back to the original source.
    ``lineno`` is 1-indexed.
    """

    idx = handler.lineno - 1
    if 0 <= idx < len(source_lines):
        return source_lines[idx]
    return ""


def _has_nosonar_marker(line: str) -> bool:
    """Check whether the source line carries a ``# NOSONAR:`` comment."""

    # The R26.4 spec mandates a *reason* after the colon; we do not try to
    # validate the reason content here — the human reviewer does — but we
    # do require the ``:`` separator to prevent an empty placeholder
    # marker from gaming the budget.
    hash_idx = line.find("#")
    if hash_idx < 0:
        return False
    comment = line[hash_idx:]
    upper = comment.upper()
    marker_idx = upper.find("NOSONAR")
    if marker_idx < 0:
        return False
    # Look for ":" after the "NOSONAR" token.
    tail = comment[marker_idx + len("NOSONAR") :]
    return ":" in tail


def _is_name_exception(exc_type: ast.expr | None, name: str) -> bool:
    """True iff ``exc_type`` is exactly the given bare Name (``Exception``)."""

    return isinstance(exc_type, ast.Name) and exc_type.id == name


def scan_file(path: Path) -> Iterator[Finding]:
    """Yield findings for one Python source file.

    The file is parsed once with :mod:`ast`. Every ``ExceptHandler`` whose
    body is a single ``pass`` is inspected for the two anti-patterns.
    """

    # ``utf-8-sig`` transparently strips a leading UTF-8 BOM (\ufeff) if
    # one is present. Without this, ``ast.parse`` raises
    # ``invalid non-printable character U+FEFF`` on files that were saved
    # by editors like Windows Notepad / older PowerShell redirections
    # which prepend a BOM. Plain UTF-8 files pass through untouched.
    try:
        source = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        yield Finding(path, 0, "bare", f"<read failed: {exc}>")
        return
    except UnicodeDecodeError as exc:
        # The file is not valid UTF-8 at all. Flag it as a parse failure
        # so the operator investigates rather than silently skipping it.
        yield Finding(path, 0, "bare", f"<decode failed: {exc}>")
        return

    source_lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        yield Finding(path, 0, "bare", f"<parse failed: {exc}>")
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_body_only_pass(node.body):
            continue
        line = _handler_source_line(source_lines, node)
        snippet = line.strip()
        if node.type is None:
            # ``except: pass`` — always a failure.
            yield Finding(path, node.lineno, "bare", snippet)
            continue
        if _is_name_exception(node.type, "Exception"):
            kind = "nosonar" if _has_nosonar_marker(line) else "plain_exception"
            yield Finding(path, node.lineno, kind, snippet)
            continue
        # ``except SomeSpecificError: pass`` is explicitly allowed by
        # R26.3 (narrow exception catches don't hide failure modes like a
        # broad ``Exception`` does), so we do not flag them here.


def _iter_python_files() -> Iterator[Path]:
    for rel in SCAN_ROOTS:
        root = PROJECT_ROOT / rel
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            # Skip cached bytecode directories (ripgrep-style, even though
            # rglob already filters by suffix, __pycache__ could hold
            # stray ``.py`` files in some toolchains — cheap to guard).
            if any(part == "__pycache__" for part in py.parts):
                continue
            yield py


def main() -> int:
    bare: list[Finding] = []
    plain: list[Finding] = []
    nosonar: list[Finding] = []

    for path in _iter_python_files():
        for finding in scan_file(path):
            if finding.kind == "bare":
                bare.append(finding)
            elif finding.kind == "plain_exception":
                plain.append(finding)
            elif finding.kind == "nosonar":
                nosonar.append(finding)

    fail = False

    if bare:
        fail = True
        print(
            f"[lint_bare_except] FAIL: {len(bare)} bare 'except: pass' occurrence(s):",
            flush=True,
        )
        for f in bare:
            rel = f.path.relative_to(PROJECT_ROOT)
            print(f"  - {rel}:{f.lineno}: {f.snippet}", flush=True)

    if plain:
        fail = True
        print(
            f"[lint_bare_except] FAIL: {len(plain)} unannotated "
            "'except Exception: pass' occurrence(s) (add '# NOSONAR: <reason>' "
            "or narrow the exception):",
            flush=True,
        )
        for f in plain:
            rel = f.path.relative_to(PROJECT_ROOT)
            print(f"  - {rel}:{f.lineno}: {f.snippet}", flush=True)

    if len(nosonar) > NOSONAR_BUDGET:
        fail = True
        print(
            f"[lint_bare_except] FAIL: {len(nosonar)} NOSONAR-annotated "
            f"'except Exception: pass' occurrences exceed the budget of "
            f"{NOSONAR_BUDGET}:",
            flush=True,
        )
        for f in nosonar:
            rel = f.path.relative_to(PROJECT_ROOT)
            print(f"  - {rel}:{f.lineno}: {f.snippet}", flush=True)
    elif nosonar:
        rels = ", ".join(
            f"{f.path.relative_to(PROJECT_ROOT)}:{f.lineno}" for f in nosonar
        )
        print(
            f"[lint_bare_except] info: {len(nosonar)} NOSONAR-annotated "
            f"handler(s) within budget ({NOSONAR_BUDGET}): [{rels}]",
            flush=True,
        )

    if fail:
        return 1

    print(
        f"[lint_bare_except] OK: scanned {', '.join(SCAN_ROOTS)} — "
        f"bare=0, plain_exception=0, nosonar={len(nosonar)}/{NOSONAR_BUDGET}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
