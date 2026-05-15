"""
Stage Gate runner for security-stability-hardening spec.

Usage::

    python scripts/stage_gate.py --stage 1

The script reads ``.kiro/specs/security-stability-hardening/gate/stage-{N}.yaml``
and runs the following sections in order, failing fast on the first non-zero
exit code:

1. ``unit_patterns``  - each entry is passed to ``pytest`` as a positional
   argument (file path or glob). Multiple patterns are grouped into a single
   pytest invocation for speed.
2. ``pbt_patterns``   - same as above, but ``--hypothesis-profile=ci`` is
   appended so the property-based tests run in CI profile.
3. ``smoke_scripts``  - each entry is executed via ``sys.executable <script>``
   (the script path is interpreted relative to the repository root).
4. ``manual_checklist`` - each entry is echoed to stdout prefixed with
   ``[MANUAL]`` for the human operator to confirm off-script.

On success the script prints ``Gate PASS`` and exits 0. On first failure it
prints ``Gate FAIL: <reason>`` and exits 1. Git tags are never created
automatically; that remains a manual step for the maintainer.

The script prefers :mod:`yaml` (PyYAML) if available, but falls back to a
minimal built-in parser that understands the flat schema used by the gate
files (``key:`` followed by ``- value`` list items or ``key: value`` /
``key: [a, b]`` inline forms). The fallback is intentionally small and exists
only to keep the gate runnable in environments where PyYAML is not installed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

try:  # pragma: no cover - availability depends on environment
    import yaml  # type: ignore[import-untyped]

    _HAS_PYYAML = True
except ImportError:  # pragma: no cover - exercised only when PyYAML absent
    yaml = None  # type: ignore[assignment]
    _HAS_PYYAML = False


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATE_DIR = PROJECT_ROOT / ".kiro" / "specs" / "security-stability-hardening" / "gate"
REPORTS_DIR = (
    PROJECT_ROOT / ".kiro" / "specs" / "security-stability-hardening" / "reports"
)

# Sections recognized in the gate YAML. Unknown keys are ignored with a warning.
#
# ``include_stages`` is a meta-key handled during post-load merge (see
# :func:`load_gate_yaml_merged`) and is not a runnable section on its own;
# it is listed here so the unknown-keys warning does not fire on files that
# use it. All other keys are flat lists consumed directly by the runner,
# except ``report_script`` which is a single-script hook run AFTER all
# smoke scripts pass (see :func:`run_report_script`). The hook is optional
# and keeping Stage 1-3 files unchanged is backwards-compatible because
# the default value when the key is absent is ``None``.
SECTION_KEYS = (
    "unit_patterns",
    "pbt_patterns",
    "smoke_scripts",
    "manual_checklist",
    "include_stages",
    "report_script",
)

# Sections that participate in the transitive merge. Order matters: the
# runner iterates this tuple when merging included stages so that, e.g.,
# included ``unit_patterns`` are rerun before the current stage's additions.
#
# ``report_script`` intentionally does NOT participate in the merge —
# a parent stage's report generator is its own responsibility, and
# Stage 4's final report should be the only one that emits the overall
# coverage matrix (task 30.1 / R38.3). Child stages therefore override
# rather than accumulate.
MERGEABLE_SECTIONS = (
    "unit_patterns",
    "pbt_patterns",
    "smoke_scripts",
    "manual_checklist",
)


# ---------------------------------------------------------------------------
# YAML loading (PyYAML preferred, minimal fallback otherwise)
# ---------------------------------------------------------------------------


def load_gate_yaml(path: Path) -> dict[str, Any]:
    """Load a gate YAML file and return a plain ``dict``.

    Uses PyYAML when available, otherwise falls back to a minimal parser that
    supports the flat schema the gate files use.

    This function loads a *single* file verbatim. Use
    :func:`load_gate_yaml_merged` to resolve ``include_stages`` transitively.
    """
    text = path.read_text(encoding="utf-8")
    if _HAS_PYYAML:
        data = yaml.safe_load(text) or {}  # type: ignore[union-attr]
        if not isinstance(data, dict):
            raise ValueError(
                f"Gate file {path} must be a mapping at top level, got {type(data).__name__}"
            )
        return data
    print(
        "[stage_gate] PyYAML not installed; using the minimal built-in parser. "
        "Install PyYAML for richer error messages.",
        file=sys.stderr,
    )
    return _minimal_yaml_parse(text, source=str(path))


def _parse_include_stages(
    value: Any, *, source: Path
) -> list[int]:
    """Normalize the ``include_stages`` section into a list of ``int`` stages.

    Accepts ``None`` / ``[]`` (no includes), a list of integers or integer
    strings, or a single scalar. Raises ``ValueError`` for anything else so
    typos surface immediately rather than silently disabling the include.
    """

    if value is None:
        return []
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return [int(value)]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        raise ValueError(
            f"{source}: include_stages must be a list of integers, "
            f"got {type(value).__name__}"
        )
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            # ``bool`` is a subclass of ``int`` in Python; reject explicitly
            # so ``include_stages: [true]`` does not masquerade as stage 1.
            raise ValueError(
                f"{source}: include_stages items must be integers, got bool {item!r}"
            )
        if isinstance(item, int):
            out.append(int(item))
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if not stripped:
                continue
            try:
                out.append(int(stripped))
            except ValueError as exc:
                raise ValueError(
                    f"{source}: include_stages contains non-integer entry {item!r}"
                ) from exc
            continue
        raise ValueError(
            f"{source}: include_stages items must be integers, "
            f"got {type(item).__name__}: {item!r}"
        )
    return out


def load_gate_yaml_merged(
    path: Path,
    *,
    visited: Optional[set[Path]] = None,
) -> dict[str, Any]:
    """Load a gate YAML file and transitively merge ``include_stages``.

    The merge is order-preserving and deduplicated: for each section in
    :data:`MERGEABLE_SECTIONS` the result lists the included stages' entries
    first (recursively, depth-first) followed by the current file's entries,
    skipping any entry whose exact string value was already added. Keys
    other than :data:`MERGEABLE_SECTIONS` come from the top-level file only
    — the include mechanism is meant for test-surface accumulation, not for
    inheriting arbitrary metadata.

    Raises ``ValueError`` on cyclic includes (``stage-2`` → ``stage-1`` →
    ``stage-2``) and on missing referenced files.
    """

    if visited is None:
        visited = set()
    resolved = path.resolve()
    if resolved in visited:
        cycle = " -> ".join(sorted(str(p) for p in visited) + [str(resolved)])
        raise ValueError(f"Cyclic include detected while loading gate files: {cycle}")
    visited.add(resolved)

    data = load_gate_yaml(path)

    include_stages = _parse_include_stages(
        data.get("include_stages"), source=path
    )
    if not include_stages:
        # Nothing to merge; strip the meta-key so downstream validation does
        # not need to special-case it.
        data.pop("include_stages", None)
        return data

    # Start with empty merged lists for the known sections and then layer
    # included stages first, current stage last.
    merged: dict[str, Any] = {section: [] for section in MERGEABLE_SECTIONS}
    seen: dict[str, set[str]] = {section: set() for section in MERGEABLE_SECTIONS}

    def _append_unique(section: str, items: Iterable[Any]) -> None:
        bucket = merged[section]
        seen_set = seen[section]
        for item in items:
            if item is None:
                continue
            key = item if isinstance(item, str) else str(item)
            key = key.strip()
            if not key:
                continue
            if key in seen_set:
                continue
            seen_set.add(key)
            bucket.append(key)

    for included_stage in include_stages:
        included_path = resolve_gate_path(included_stage)
        if not included_path.is_file():
            raise ValueError(
                f"{path}: include_stages references missing gate file: {included_path}"
            )
        included_data = load_gate_yaml_merged(included_path, visited=visited)
        for section in MERGEABLE_SECTIONS:
            _append_unique(section, _iter_items(included_data.get(section)))

    for section in MERGEABLE_SECTIONS:
        _append_unique(section, _iter_items(data.get(section)))

    # Copy forward any non-mergeable keys the current file defined (future
    # extensions). Unknown keys still get the warning from ``run_stage``.
    for key, value in data.items():
        if key in MERGEABLE_SECTIONS or key == "include_stages":
            continue
        merged[key] = value

    visited.discard(resolved)
    return merged


def _iter_items(value: Any) -> list[Any]:
    """Coerce a loaded section value into an iterable of items.

    The minimal parser always emits ``list[str]`` for list sections, but a
    PyYAML-loaded file can expose a scalar or ``None`` when a key was
    declared without a body. Callers funnel everything through this helper
    so the merge code stays uniform.
    """

    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    # Tolerate any other iterable (tuple, generator) without crashing the
    # gate — they are not the documented shape but harmless to materialize.
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _minimal_yaml_parse(text: str, *, source: str) -> dict[str, Any]:
    """A very small YAML subset parser.

    Supported at top level:

    * ``key:`` on its own line followed by zero or more ``  - value`` entries
      (two-space indent) → ``{"key": [value, ...]}``
    * ``key: value`` → ``{"key": "value"}``
    * ``key: [a, b, c]`` → ``{"key": ["a", "b", "c"]}``
    * ``#`` comments (line and trailing) are stripped.
    * Blank lines are ignored.

    Anything else raises ``ValueError`` with the source location.
    """
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    inline_list_re = re.compile(r"^\[(.*)\]$")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        # Strip trailing comments (very naive, but fine for our schema).
        if "#" in raw:
            # Don't strip comment markers that appear inside quoted strings;
            # the gate schema never uses quoted strings so a plain split is safe.
            raw = raw.split("#", 1)[0]
        line = raw.rstrip()
        if not line.strip():
            continue

        # List item belonging to the current block key.
        if line.startswith(("  - ", "- ")):
            if current_list is None:
                raise ValueError(
                    f"{source}:{lineno}: list item without a parent key: {raw!r}"
                )
            item = line.lstrip()[2:].strip()
            # Unquote simple single/double-quoted strings.
            if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"'):
                item = item[1:-1]
            current_list.append(item)
            continue

        # Top-level key:[ value ].
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, rest = line.partition(":")
            key = key.strip()
            rest = rest.strip()
            if not key:
                raise ValueError(f"{source}:{lineno}: empty key: {raw!r}")
            if rest == "":
                # Block list or scalar to be filled by following lines.
                result[key] = []
                current_key = key
                current_list = result[key]  # type: ignore[assignment]
                continue
            m = inline_list_re.match(rest)
            if m:
                inner = m.group(1).strip()
                items = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                # Unquote simple items.
                items = [
                    x[1:-1] if len(x) >= 2 and x[0] == x[-1] and x[0] in ("'", '"') else x
                    for x in items
                ]
                result[key] = items
                current_key = key
                current_list = None
                continue
            # Plain scalar.
            if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("'", '"'):
                rest = rest[1:-1]
            result[key] = rest
            current_key = key
            current_list = None
            continue

        raise ValueError(f"{source}:{lineno}: unsupported line: {raw!r}")

    return result


# ---------------------------------------------------------------------------
# Section normalization
# ---------------------------------------------------------------------------


def _coerce_list(value: Any, *, section: str) -> list[str]:
    """Coerce a section value into a list of stripped, non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        raise ValueError(
            f"Section '{section}' must be a list of strings, got {type(value).__name__}"
        )
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        if not isinstance(item, str):
            raise ValueError(
                f"Section '{section}' items must be strings, got {type(item).__name__}: {item!r}"
            )
        item = item.strip()
        if item:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------


def _print_header(title: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\n[stage_gate] {title}\n{bar}", flush=True)


def _run(cmd: list[str], *, cwd: Path) -> int:
    """Run ``cmd`` and stream its output. Returns the exit code."""
    display = " ".join(cmd)
    print(f"[stage_gate] $ {display}", flush=True)
    try:
        completed = subprocess.run(cmd, cwd=str(cwd), check=False)
    except FileNotFoundError as exc:
        print(f"[stage_gate] command not found: {exc}", file=sys.stderr, flush=True)
        return 127
    return completed.returncode


def run_unit_tests(patterns: list[str], *, cwd: Path) -> tuple[bool, str]:
    if not patterns:
        print("[stage_gate] unit_patterns: (empty, skipping)", flush=True)
        return True, ""
    _print_header(f"unit_patterns ({len(patterns)})")
    cmd = [sys.executable, "-m", "pytest", *patterns]
    rc = _run(cmd, cwd=cwd)
    if rc != 0:
        return False, f"pytest unit tests failed (exit={rc}) for patterns: {patterns}"
    return True, ""


def run_pbt_tests(patterns: list[str], *, cwd: Path) -> tuple[bool, str]:
    if not patterns:
        print("[stage_gate] pbt_patterns: (empty, skipping)", flush=True)
        return True, ""
    _print_header(f"pbt_patterns ({len(patterns)})")
    cmd = [sys.executable, "-m", "pytest", *patterns, "--hypothesis-profile=ci"]
    rc = _run(cmd, cwd=cwd)
    if rc != 0:
        return False, f"pytest PBT failed (exit={rc}) for patterns: {patterns}"
    return True, ""


def run_smoke_scripts(scripts: list[str], *, cwd: Path) -> tuple[bool, str]:
    if not scripts:
        print("[stage_gate] smoke_scripts: (empty, skipping)", flush=True)
        return True, ""
    _print_header(f"smoke_scripts ({len(scripts)})")
    for script in scripts:
        script_path = (cwd / script).resolve() if not os.path.isabs(script) else Path(script)
        if not script_path.is_file():
            return False, f"smoke script not found: {script} (resolved to {script_path})"
        cmd = [sys.executable, str(script_path)]
        rc = _run(cmd, cwd=cwd)
        if rc != 0:
            return False, f"smoke script failed (exit={rc}): {script}"
    return True, ""


def echo_manual_checklist(items: list[str]) -> None:
    if not items:
        return
    _print_header(f"manual_checklist ({len(items)})")
    for item in items:
        print(f"[MANUAL] {item}", flush=True)


def run_report_script(script: str | None, *, cwd: Path) -> tuple[bool, str]:
    """Run an optional post-smoke report script.

    Called AFTER ``run_smoke_scripts`` succeeds. ``None`` / empty string
    means "no report" (backwards-compatible default for Stage 1-3). The
    script is invoked exactly like a smoke script (``sys.executable
    <path>`` with ``cwd=PROJECT_ROOT``); its exit code is treated as a
    gate signal — non-zero fails the whole stage just like a smoke
    script would. The hook exists so Stage 4 can emit the final
    coverage matrix + performance benchmark summary without bolting
    unrelated output into the smoke contract (task 30.1 / R38.3).
    """

    if not script:
        return True, ""
    _print_header(f"report_script: {script}")
    script_path = (cwd / script).resolve() if not os.path.isabs(script) else Path(script)
    if not script_path.is_file():
        return False, f"report script not found: {script} (resolved to {script_path})"
    cmd = [sys.executable, str(script_path)]
    rc = _run(cmd, cwd=cwd)
    if rc != 0:
        return False, f"report script failed (exit={rc}): {script}"
    return True, ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def resolve_gate_path(stage: int) -> Path:
    return GATE_DIR / f"stage-{stage}.yaml"


def run_stage(stage: int, *, cwd: Path = PROJECT_ROOT) -> int:
    gate_path = resolve_gate_path(stage)
    if not gate_path.is_file():
        print(f"Gate FAIL: gate file not found: {gate_path}", flush=True)
        return 1

    try:
        # ``load_gate_yaml_merged`` transitively resolves ``include_stages``
        # so Stage 2+ can replay Stage 1 / 2 / ... regression lists without
        # duplicating entries. See :func:`load_gate_yaml_merged` for merge
        # semantics (dedup, depth-first, cycle-safe).
        data = load_gate_yaml_merged(gate_path)
    except (ValueError, OSError) as exc:
        print(f"Gate FAIL: failed to parse {gate_path}: {exc}", flush=True)
        return 1

    unknown = [k for k in data.keys() if k not in SECTION_KEYS]
    if unknown:
        print(
            f"[stage_gate] warning: ignoring unknown keys in {gate_path.name}: {unknown}",
            file=sys.stderr,
            flush=True,
        )

    try:
        unit_patterns = _coerce_list(data.get("unit_patterns"), section="unit_patterns")
        pbt_patterns = _coerce_list(data.get("pbt_patterns"), section="pbt_patterns")
        smoke_scripts = _coerce_list(data.get("smoke_scripts"), section="smoke_scripts")
        manual_checklist = _coerce_list(
            data.get("manual_checklist"), section="manual_checklist"
        )
    except ValueError as exc:
        print(f"Gate FAIL: invalid gate schema: {exc}", flush=True)
        return 1

    # ``report_script`` is a single-script hook, not a list. Accept either a
    # bare string or the top entry of a length-1 list for operator ergonomics
    # (e.g. when a yaml author writes ``report_script: [scripts/foo.py]``).
    report_script_raw = data.get("report_script")
    report_script: str | None = None
    if isinstance(report_script_raw, str):
        report_script = report_script_raw.strip() or None
    elif isinstance(report_script_raw, list):
        non_empty = [str(x).strip() for x in report_script_raw if x]
        if len(non_empty) > 1:
            print(
                "Gate FAIL: invalid gate schema: report_script must be a single "
                f"script path, got {len(non_empty)} entries: {non_empty}",
                flush=True,
            )
            return 1
        report_script = non_empty[0] if non_empty else None
    elif report_script_raw is not None:
        print(
            "Gate FAIL: invalid gate schema: report_script must be a string, got "
            f"{type(report_script_raw).__name__}",
            flush=True,
        )
        return 1

    print(
        f"[stage_gate] stage={stage} file={gate_path} "
        f"unit={len(unit_patterns)} pbt={len(pbt_patterns)} "
        f"smoke={len(smoke_scripts)} manual={len(manual_checklist)}"
        f"{' report=' + report_script if report_script else ''}",
        flush=True,
    )

    ok, reason = run_unit_tests(unit_patterns, cwd=cwd)
    if not ok:
        print(f"Gate FAIL: {reason}", flush=True)
        return 1

    ok, reason = run_pbt_tests(pbt_patterns, cwd=cwd)
    if not ok:
        print(f"Gate FAIL: {reason}", flush=True)
        return 1

    ok, reason = run_smoke_scripts(smoke_scripts, cwd=cwd)
    if not ok:
        print(f"Gate FAIL: {reason}", flush=True)
        return 1

    ok, reason = run_report_script(report_script, cwd=cwd)
    if not ok:
        print(f"Gate FAIL: {reason}", flush=True)
        return 1

    echo_manual_checklist(manual_checklist)

    print("\nGate PASS", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Final report generation (Stage 4 / task 30.1 / R38.3)
# ---------------------------------------------------------------------------


def _current_commit_sha(cwd: Path = PROJECT_ROOT) -> str:
    """Return the current git HEAD SHA, or a sentinel when git is unavailable.

    We prefer ``git rev-parse HEAD`` over reading ``.git/HEAD`` directly so
    packed refs and detached HEAD states resolve correctly. The fallback is
    intentionally a descriptive sentinel (``unknown-<reason>``) rather than
    an empty string so the rendered report never has a blank SHA cell — the
    sentinel is an obvious signal that the report should be regenerated in
    a git checkout before release.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "unknown-no-git"
    if result.returncode != 0:
        return f"unknown-exit-{result.returncode}"
    sha = (result.stdout or "").strip()
    return sha or "unknown-empty"


def _render_final_report(data: Any, *, stage: int) -> str:
    """Render a :class:`FinalReportData` instance to a Markdown string.

    Kept separate from the harvester (``smoke_final_report.harvest``) so
    tests can round-trip the rendering independently. ``Any`` rather than
    a concrete type in the signature because importing the helper lazily
    (inside the report path) lets Stage 1-3 invocations skip the import.
    """

    now = _dt.datetime.now()
    lines: list[str] = []
    lines.append("# security-stability-hardening — Final Stage Gate Report")
    lines.append("")
    lines.append(f"- **Generated:** {now.isoformat(timespec='seconds')}")
    lines.append(f"- **Stage:** {stage}")
    lines.append(f"- **Commit SHA:** `{data.commit_sha}`")
    gate_files = ", ".join(
        f"stage-{s}.yaml" for s in sorted(data.stage_yamls)
    ) or "(none)"
    lines.append(f"- **Gate files scanned:** {gate_files}")
    lines.append("")

    lines.append("## Test / Smoke Counts (per R38.3 b)")
    lines.append("")
    lines.append("| Kind | Count |")
    lines.append("| --- | ---: |")
    lines.append(f"| Unit test patterns (aggregated) | {data.unit_test_count} |")
    lines.append(f"| PBT patterns (aggregated) | {data.pbt_test_count} |")
    lines.append(f"| Smoke scripts (aggregated) | {data.smoke_script_count} |")
    lines.append(f"| Manual checklist items (aggregated) | {data.manual_item_count} |")
    lines.append("")
    lines.append(
        "_Counts sum per-stage declarations across stage-1…stage-4 yamls "
        "(not the merged replay). Optional tests flagged with ``*`` in "
        "tasks.md are included only when their yaml entry is uncommented._"
    )
    lines.append("")

    lines.append("## Performance Benchmarks (per R38.3 c)")
    lines.append("")
    lines.append("| Metric | Requirement | Source | Value |")
    lines.append("| --- | --- | --- | --- |")
    for bench in data.benchmarks:
        lines.append(
            f"| {bench.name} | {bench.requirement} | {bench.source} | "
            f"{bench.value} |"
        )
    lines.append("")
    lines.append(
        "_When ``Source`` is ``budget`` the value is copied from the "
        "design document; a ``measured`` value replaces the budget the "
        "first time the corresponding smoke writes "
        "``logs/<smoke>.last.txt``._"
    )
    lines.append("")

    lines.append("## Coverage Matrix (per R38.3 a)")
    lines.append("")
    lines.append(
        "Maps each P0-1…P3-15 risk ID to its Requirement(s) and the "
        "automation / manual hooks that replay it through the stage gate "
        "chain. ``Stages`` lists which gate yamls own the hook(s)."
    )
    lines.append("")
    lines.append(
        "| Risk | Requirements | Stages | Automation | Manual |"
    )
    lines.append("| --- | --- | --- | --- | --- |")
    for row in data.coverage_matrix:
        reqs = ", ".join(row.requirements)
        stages = (
            ", ".join(str(s) for s in row.stages) if row.stages else "—"
        )
        auto = (
            "<br>".join(row.automation[:8]) if row.automation else "—"
        )
        # We truncate to the first 8 automation entries to keep the
        # table scannable; the full list is always derivable from the
        # yamls themselves and the harvester log output.
        if len(row.automation) > 8:
            auto += f"<br>… (+{len(row.automation) - 8} more)"
        manual = f"{len(row.manual_hits)} line(s)" if row.manual_hits else "—"
        lines.append(f"| **{row.risk_id}** | {reqs} | {stages} | {auto} | {manual} |")
    lines.append("")
    lines.append(
        "_Commit SHA above applies to every row — the report is a "
        "whole-feature snapshot at ``git rev-parse HEAD``. Per-test "
        "case IDs are encoded in the Automation column (e.g. "
        "``smoke:scripts/smoke_ssrf_reject.py``) so grepping the gate "
        "yaml recovers the exact invocation used._"
    )
    lines.append("")

    if data.coverage_gaps:
        lines.append("## Coverage Gaps")
        lines.append("")
        lines.append(
            "The following risks have no automation or manual hook in "
            "any of the four stage yamls. Close the gap by adding a "
            "``smoke_scripts`` / ``unit_patterns`` / ``pbt_patterns`` "
            "entry whose path or adjacent comment mentions the "
            "requirement, or a ``manual_checklist`` line that does the "
            "same."
        )
        lines.append("")
        for gap in data.coverage_gaps:
            lines.append(f"- {gap}")
        lines.append("")
    else:
        lines.append("## Coverage Gaps")
        lines.append("")
        lines.append("_None — every risk in the P0-1…P3-15 matrix has at "
                     "least one hook._")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Per R38.2 the maintainer must tag "
        "``security-stability-hardening/complete`` manually only after "
        "``Gate PASS`` for stage 4 AND this report has been reviewed."
    )
    lines.append(
        "- Per R38.4 any P0/P1 regression discovered in the final gate "
        "blocks the tag and the team rolls back to "
        "``stage-3/p2-complete``."
    )
    lines.append("")

    return "\n".join(lines) + "\n"


def generate_final_report(
    *,
    stage: int,
    cwd: Path = PROJECT_ROOT,
    out_dir: Path | None = None,
) -> Path:
    """Harvest coverage data and write the markdown final report.

    Returns the path to the generated report. The caller is responsible
    for reporting it to the operator; we only create the directory and
    the file here. ``out_dir`` overrides the default reports directory,
    primarily for tests.
    """

    # Lazy import so Stage 1-3 gate runs (which don't pass ``--final-report``)
    # don't pay the stage-gate-self-import cost that the harvester triggers.
    if str(PROJECT_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import smoke_final_report  # type: ignore[import-not-found]

    sha = _current_commit_sha(cwd=cwd)
    data = smoke_final_report.harvest(commit_sha=sha)
    body = _render_final_report(data, stage=stage)

    target_dir = out_dir if out_dir is not None else REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = target_dir / f"final_report_{stamp}.md"
    out_path.write_text(body, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stage_gate",
        description="Run the security-stability-hardening stage gate for a given stage.",
    )
    parser.add_argument(
        "--stage",
        type=int,
        required=True,
        help="Stage number (1-4) to run. Resolves to gate/stage-{N}.yaml.",
    )
    parser.add_argument(
        "--final-report",
        action="store_true",
        help=(
            "After the gate passes, render a markdown final report to "
            ".kiro/specs/security-stability-hardening/reports/"
            "final_report_{timestamp}.md. Per R38.3 the report contains "
            "the current commit SHA, the P0-1…P3-15 coverage matrix, "
            "performance benchmark snapshots, and new test / PBT / "
            "smoke counts. Only meaningful with --stage 4 (task 30.1); "
            "the flag is accepted on earlier stages but prints a "
            "warning and produces a report scoped to that stage's yaml."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.stage < 1:
        print(f"Gate FAIL: stage must be >= 1, got {args.stage}", flush=True)
        return 1

    rc = run_stage(args.stage)
    if rc != 0:
        # Gate failed; don't emit a misleading "PASS" report. Operators
        # can re-run with ``--final-report`` once the gate is green.
        return rc

    if args.final_report:
        if args.stage != 4:
            # Not a hard failure — the operator may want a partial
            # snapshot mid-feature — but we flag the deviation from the
            # R38 flow in case it was a typo.
            print(
                f"[stage_gate] warning: --final-report used with "
                f"--stage {args.stage} (R38 expects stage=4); "
                "proceeding anyway.",
                file=sys.stderr,
                flush=True,
            )
        try:
            out_path = generate_final_report(stage=args.stage)
        except Exception as exc:
            # Don't mask a gate PASS with a report-write failure: print
            # the error and return a distinct exit code so the operator
            # knows the gate itself passed but the report did not.
            print(
                f"[stage_gate] final-report FAILED to generate: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 2
        print(
            f"[stage_gate] final report written to: {out_path}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
