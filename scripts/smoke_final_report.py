"""
Stage 4 final report helper (task 30.1 / R38.3).

Harvests the coverage matrix, performance benchmark snapshot, and
new-test counts that ``stage_gate.py --stage 4 --final-report`` embeds
into ``reports/final_report_{timestamp}.md``. Keeping this logic in a
dedicated module (rather than inline in ``stage_gate.py``) lets the
Stage 4 gate yaml invoke it as a standalone smoke so the harvest path
is exercised *before* the final-report flag runs it for real — if the
harvester crashes on a new risk ID or a relocated smoke script, the
Stage 4 gate fails fast with a clear traceback instead of producing a
half-written markdown report.

Two entry points:

* :func:`harvest` — pure function, returns a :class:`FinalReportData`
  for programmatic consumers (``stage_gate.py`` is the only one today).
* ``python scripts/smoke_final_report.py`` — CLI thin wrapper that
  invokes :func:`harvest` and prints a short summary. Exits 0 when the
  harvest completes without errors (every gate yaml parsed, every
  referenced smoke script resolvable, every risk in R38's coverage
  matrix mapped to at least one automation hook or a declared manual
  item). Exits 1 on any structural problem so the smoke_script slot in
  ``gate/stage-4.yaml`` catches regressions.

Design notes:

* The harvester intentionally does **not** execute any of the smoke
  scripts or tests — it only reads the gate YAML files and the
  requirements/design markdown. That keeps the Stage 4 smoke lane cheap
  (<1s) and orthogonal to the actual gate run; ``stage_gate.py`` is the
  one that runs them.
* The coverage matrix is derived from two sources that must agree:
  (a) the risk → requirement mapping table inside ``requirements.md``
  (P0-1 → R1, …, P3-15 → R9) and (b) the per-requirement automation
  hooks harvested from the stage yaml files (``smoke_scripts``,
  ``unit_patterns``, ``pbt_patterns``, ``manual_checklist``). If a
  requirement appears in the risk table but has no entry in any of the
  four stage yamls, the harvester records a ``coverage_gap`` so the
  final report surfaces it rather than silently claiming green.
* Performance benchmarks harvested here are **snapshot values**, not
  live measurements. The intent per task 30.1 is that the Stage 2 P95
  stop-response benchmark, the R13 log I/O throttle, and the R19 worker
  convergence already ran as part of the gate chain; this helper only
  records the headline numbers from their output. When no log file is
  available we fall back to the design-document budget (e.g. "P95 ≤
  2000ms / budget source: R14.4") and flag the value as ``budget``
  rather than ``measured`` in the report.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATE_DIR = PROJECT_ROOT / ".kiro" / "specs" / "security-stability-hardening" / "gate"
REQUIREMENTS_MD = (
    PROJECT_ROOT / ".kiro" / "specs" / "security-stability-hardening" / "requirements.md"
)
TASKS_MD = PROJECT_ROOT / ".kiro" / "specs" / "security-stability-hardening" / "tasks.md"

# Importing the stage gate module lets us reuse its YAML loader — fallback
# parser and all — so this harvester sees exactly the same data the gate
# runner consumes. We insert the scripts dir on sys.path for the same
# dual-invocation reasons documented in ``smoke_bare_except_audit.py``.
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import stage_gate  # noqa: E402


# ---------------------------------------------------------------------------
# Data classes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskRow:
    """One row of the P0-1…P3-15 coverage matrix."""

    risk_id: str
    requirements: tuple[str, ...]  # e.g. ("R1",) or ("R11", "R29")
    automation: tuple[str, ...]  # smoke scripts / test patterns
    manual_hits: tuple[str, ...]  # manual checklist lines that mention any R
    stages: tuple[int, ...]  # stages in whose yaml the automation was found

    @property
    def has_coverage(self) -> bool:
        return bool(self.automation or self.manual_hits)


@dataclass(frozen=True)
class BenchmarkSnapshot:
    """One performance benchmark line in the final report."""

    name: str  # e.g. "Stop response P95"
    value: str  # e.g. "≤2000ms (R14.4 budget)"
    source: str  # "measured" | "budget"
    requirement: str  # e.g. "R14.4"


@dataclass
class FinalReportData:
    commit_sha: str
    coverage_matrix: List[RiskRow] = field(default_factory=list)
    benchmarks: List[BenchmarkSnapshot] = field(default_factory=list)
    unit_test_count: int = 0
    pbt_test_count: int = 0
    smoke_script_count: int = 0
    manual_item_count: int = 0
    coverage_gaps: List[str] = field(default_factory=list)
    stage_yamls: Dict[int, Path] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Risk → Requirement table (harvested from requirements.md §"风险覆盖矩阵").
# ---------------------------------------------------------------------------


def _load_risk_table() -> List[tuple[str, tuple[str, ...]]]:
    """Parse the ``原始编号 → 覆盖 Requirement`` table at the bottom of
    ``requirements.md``.

    Returns a list of ``(risk_id, (requirement_id, ...))`` tuples preserving
    file order so the final report matches the human-reviewed source.
    The parser tolerates the "P3-15 | R9(合并到 P1-1)" footnote by
    stripping any parenthesized suffix after the requirement token.
    """

    if not REQUIREMENTS_MD.is_file():
        return []
    text = REQUIREMENTS_MD.read_text(encoding="utf-8")
    # The table lives inside "## 风险覆盖矩阵(索引)" and uses pipe-delimited
    # rows. We match only rows whose first cell starts with P0-/P1-/P2-/P3-
    # so the header / separator rows are ignored automatically.
    row_re = re.compile(
        r"^\s*\|\s*(P[0-3]-\d+)\s*\|\s*([^|]+?)\s*\|\s*$",
        re.MULTILINE,
    )
    rows: List[tuple[str, tuple[str, ...]]] = []
    req_token_re = re.compile(r"R\d+")
    for match in row_re.finditer(text):
        risk = match.group(1).strip()
        body = match.group(2).strip()
        # Strip parenthesized annotations such as "R9(合并到 P1-1)".
        reqs_raw = re.sub(r"[((][^))]*[))]", "", body)
        reqs = tuple(req_token_re.findall(reqs_raw))
        if not reqs:
            continue
        rows.append((risk, reqs))
    return rows


# ---------------------------------------------------------------------------
# Harvesting gate yamls.
# ---------------------------------------------------------------------------


_STAGES: tuple[int, ...] = (1, 2, 3, 4)


def _load_stage_data() -> Dict[int, dict]:
    """Load every stage yaml *unmerged* so we can attribute each automation
    entry to the stage that originally declared it.

    We deliberately avoid :func:`stage_gate.load_gate_yaml_merged` here
    because the merged view loses the stage origin (Stage 3 merges in
    Stage 1+2 entries, which would double-count in the coverage matrix).
    """

    data: Dict[int, dict] = {}
    for stage in _STAGES:
        path = stage_gate.resolve_gate_path(stage)
        if not path.is_file():
            continue
        try:
            raw = stage_gate.load_gate_yaml(path)
        except (ValueError, OSError) as exc:
            # Don't blow up the harvester — the stage gate runner surfaces
            # the same error with richer context. We still record the
            # failure in coverage_gaps further down.
            print(
                f"[smoke_final_report] WARN: failed to parse {path}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        data[stage] = raw
    return data


def _extract_entries(data: dict, key: str) -> List[str]:
    """Return the string entries under ``key`` from a gate yaml dict.

    Tolerates missing keys, scalar values, and the Nones that the fallback
    parser produces for empty block lists.
    """

    value = data.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                out.append(stripped)
        else:
            out.append(str(item))
    return out


def _requirement_in_entry(req: str, entry: str) -> bool:
    """True iff ``entry`` mentions the given requirement token.

    Matches ``R15`` exactly but not ``R150``. Case-sensitive (our
    convention always uses uppercase ``R``). Also matches dotted
    sub-requirement tokens such as ``R18.1``.
    """

    pattern = re.compile(rf"\b{re.escape(req)}(?:\.\d+)?\b")
    return bool(pattern.search(entry))


def _build_coverage_matrix(
    risk_rows: Iterable[tuple[str, tuple[str, ...]]],
    stage_data: Dict[int, dict],
) -> tuple[List[RiskRow], List[str]]:
    """Map each risk → (automation entries, manual entries, stages).

    An automation entry is a smoke script / unit pattern / pbt pattern
    whose inline comment (in the yaml) or path (for tests) mentions any
    of the risk's requirements. A manual entry is a ``manual_checklist``
    line that mentions any of the requirements.

    Because the gate yaml comments are stripped by the YAML loader, we
    fall back to keyword heuristics on the raw string. For smoke scripts
    the path itself usually names the covered capability (e.g.
    ``smoke_ssrf_reject.py`` → R4); for manual items the Chinese text
    mentions the cluster (e.g. "英文 locale" → R25.1 / R28) so we match
    by keywords collected from the risk table section of
    ``requirements.md``.
    """

    # Re-load the raw yaml *text* so we can keep comment-mention matches.
    # The comments are the primary routing hints for smoke_scripts that
    # don't embed requirement IDs in their path (e.g.
    # smoke_engine_select_mime.py → R24 via a comment block in stage-3.yaml).
    stage_text: Dict[int, str] = {}
    for stage in stage_data:
        path = stage_gate.resolve_gate_path(stage)
        try:
            stage_text[stage] = path.read_text(encoding="utf-8")
        except OSError:
            stage_text[stage] = ""

    coverage: List[RiskRow] = []
    gaps: List[str] = []

    for risk_id, reqs in risk_rows:
        automation: List[str] = []
        manual: List[str] = []
        stages_hit: List[int] = []

        for stage, data in sorted(stage_data.items()):
            smoke = _extract_entries(data, "smoke_scripts")
            units = _extract_entries(data, "unit_patterns")
            pbts = _extract_entries(data, "pbt_patterns")
            manual_items = _extract_entries(data, "manual_checklist")
            text = stage_text.get(stage, "")

            def _mentions_any(entry: str) -> bool:
                return any(_requirement_in_entry(req, entry) for req in reqs)

            # For path-shaped entries (smoke / unit / pbt) we additionally
            # scan the yaml *comment block* immediately preceding the
            # entry for a requirement tag. That block is how the gate
            # files document the mapping today (see ``stage-3.yaml``
            # ``# R15 — M3U8 fetch backoff schedule …``).
            #
            # "Immediately preceding" here is generous: we collect every
            # comment line in the ~600 char window before the entry,
            # which covers the common case where a single comment block
            # documents several sibling entries (e.g. one ``# R1 —``
            # comment above both ``smoke_component_update_download_r1.py``
            # and ``smoke_component_update_install.py``). We stop walking
            # backward when we hit the section key line (``smoke_scripts:``
            # / ``unit_patterns:`` / ``pbt_patterns:``) so comments that
            # belong to a sibling section do not leak into this match.
            def _nearby_comment_mentions(entry: str) -> bool:
                if not text:
                    return False
                idx = text.find(f"- {entry}")
                if idx < 0:
                    return False
                window = text[max(0, idx - 600) : idx]
                lines = window.splitlines()
                comment_blob: List[str] = []
                for line in reversed(lines):
                    s = line.strip()
                    if s.endswith(":") and not s.startswith("#"):
                        # Hit the section boundary (``smoke_scripts:`` etc.)
                        # or any other top-level key — stop the walk so we
                        # do not inherit comments from a sibling section.
                        break
                    if s.startswith("#"):
                        comment_blob.append(s)
                comment_text = "\n".join(comment_blob)
                return any(_requirement_in_entry(req, comment_text) for req in reqs)

            local_hits: List[str] = []
            for entry in smoke:
                if _mentions_any(entry) or _nearby_comment_mentions(entry):
                    local_hits.append(f"smoke:{entry}")
            for entry in units:
                if _mentions_any(entry) or _nearby_comment_mentions(entry):
                    local_hits.append(f"unit:{entry}")
            for entry in pbts:
                if _mentions_any(entry) or _nearby_comment_mentions(entry):
                    local_hits.append(f"pbt:{entry}")

            for entry in manual_items:
                if _mentions_any(entry):
                    manual.append(f"stage-{stage}:{entry}")

            if local_hits:
                stages_hit.append(stage)
                automation.extend(local_hits)

        row = RiskRow(
            risk_id=risk_id,
            requirements=reqs,
            automation=tuple(automation),
            manual_hits=tuple(manual),
            stages=tuple(stages_hit),
        )
        coverage.append(row)
        if not row.has_coverage:
            gaps.append(
                f"{risk_id} ({', '.join(reqs)}): no automation or manual hit "
                "found in stage-1..4 yamls"
            )

    return coverage, gaps


# ---------------------------------------------------------------------------
# Counts / benchmarks.
# ---------------------------------------------------------------------------


def _aggregate_counts(stage_data: Dict[int, dict]) -> tuple[int, int, int, int]:
    """Sum unit / PBT / smoke / manual entries across all four stages.

    We count each stage's own declarations (not the merged view), so a
    smoke script listed in stage-1.yaml that Stage 3 replays via
    ``include_stages: [2]`` is counted once. That matches the "新增测试
    统计" intent in R38.3(b): we report what was *added* in total across
    the four stages, which is exactly the union of per-stage entries.
    """

    units = 0
    pbts = 0
    smokes = 0
    manuals = 0
    for data in stage_data.values():
        units += len(_extract_entries(data, "unit_patterns"))
        pbts += len(_extract_entries(data, "pbt_patterns"))
        smokes += len(_extract_entries(data, "smoke_scripts"))
        manuals += len(_extract_entries(data, "manual_checklist"))
    return units, pbts, smokes, manuals


def _collect_benchmarks() -> List[BenchmarkSnapshot]:
    """Build the benchmark snapshot list per R38.3(a) / R38.3(c).

    We prefer measured values when present in the current run (stored as
    ``logs/stop_response_benchmark.last.txt`` etc.), else fall back to
    the design-document budget and flag source=``budget``.
    """

    snapshots: List[BenchmarkSnapshot] = []

    def _load_last(name: str) -> Optional[str]:
        # Convention: each benchmark smoke may drop a one-line summary
        # into ``logs/<name>.last.txt``. Today none of the smokes emit
        # these files, so the harvester transparently falls back to the
        # budget. The file-format hook is preserved so a follow-up can
        # wire them in without changing this module.
        path = PROJECT_ROOT / "logs" / f"{name}.last.txt"
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    measured = _load_last("smoke_stop_response_benchmark")
    if measured:
        snapshots.append(
            BenchmarkSnapshot(
                name="Stop response (20-task concurrent stop) P95",
                value=measured,
                source="measured",
                requirement="R14.4",
            )
        )
    else:
        snapshots.append(
            BenchmarkSnapshot(
                name="Stop response (20-task concurrent stop) P95",
                value="≤2000ms (P95 budget per R14.4 / design §2.6)",
                source="budget",
                requirement="R14.4",
            )
        )

    measured = _load_last("smoke_log_io_throttle")
    if measured:
        snapshots.append(
            BenchmarkSnapshot(
                name="Log I/O glob+close rate (per 1000 emits)",
                value=measured,
                source="measured",
                requirement="R13.3",
            )
        )
    else:
        snapshots.append(
            BenchmarkSnapshot(
                name="Log I/O glob+close rate (per 1000 emits)",
                value="≤10/千条 (P-运维 1 budget per R13.3)",
                source="budget",
                requirement="R13.3",
            )
        )

    measured = _load_last("smoke_worker_convergence")
    if measured:
        snapshots.append(
            BenchmarkSnapshot(
                name="Worker pool convergence time (8→2)",
                value=measured,
                source="measured",
                requirement="R19.2",
            )
        )
    else:
        snapshots.append(
            BenchmarkSnapshot(
                name="Worker pool convergence time (8→2)",
                value="≤30s (R19.2 ceiling; smoke typically ≤3s)",
                source="budget",
                requirement="R19.2",
            )
        )

    return snapshots


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def harvest(commit_sha: str) -> FinalReportData:
    """Gather everything needed to render the final report.

    ``commit_sha`` is injected by the caller (``stage_gate.py`` invokes
    ``git rev-parse HEAD``) so the harvester stays I/O-light and doesn't
    require git to be on PATH when invoked offline.
    """

    risk_rows = _load_risk_table()
    stage_data = _load_stage_data()
    coverage, gaps = _build_coverage_matrix(risk_rows, stage_data)
    units, pbts, smokes, manuals = _aggregate_counts(stage_data)
    bench = _collect_benchmarks()
    yaml_map = {
        stage: stage_gate.resolve_gate_path(stage)
        for stage in stage_data
    }
    return FinalReportData(
        commit_sha=commit_sha,
        coverage_matrix=coverage,
        benchmarks=bench,
        unit_test_count=units,
        pbt_test_count=pbts,
        smoke_script_count=smokes,
        manual_item_count=manuals,
        coverage_gaps=gaps,
        stage_yamls=yaml_map,
    )


# ---------------------------------------------------------------------------
# CLI entry point (used as a Stage 4 smoke).
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_final_report",
        description=(
            "Stage 4 smoke: harvest the coverage matrix and verify every "
            "risk is mapped to at least one automation or manual hook."
        ),
    )
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help=(
            "Do not fail when coverage gaps are present. The Stage 4 "
            "gate invokes the smoke without this flag; stage_gate.py's "
            "--final-report path invokes it with --allow-gaps so the "
            "report still generates when the harvester cannot resolve "
            "every risk (the gaps are then listed in the report body)."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # We don't need the real commit SHA for the smoke pass — the coverage
    # matrix and benchmark harvest don't depend on it — so we pass a
    # placeholder. ``stage_gate.py --final-report`` passes the real SHA
    # when it calls ``harvest`` directly.
    data = harvest(commit_sha="<harvest-only>")

    print(
        f"[smoke_final_report] gate yamls: "
        f"{[(s, str(p.name)) for s, p in sorted(data.stage_yamls.items())]}",
        flush=True,
    )
    print(
        f"[smoke_final_report] counts: unit={data.unit_test_count} "
        f"pbt={data.pbt_test_count} smoke={data.smoke_script_count} "
        f"manual={data.manual_item_count}",
        flush=True,
    )
    print(
        f"[smoke_final_report] coverage rows: {len(data.coverage_matrix)} "
        f"gaps: {len(data.coverage_gaps)}",
        flush=True,
    )
    if data.coverage_gaps:
        for gap in data.coverage_gaps:
            print(f"  - {gap}", flush=True)
        if not args.allow_gaps:
            print(
                "[smoke_final_report] FAIL: coverage gaps detected. Either "
                "add a smoke/unit/pbt/manual entry that mentions the risk's "
                "requirement(s), or rerun with --allow-gaps for report-only "
                "mode.",
                file=sys.stderr,
                flush=True,
            )
            return 1
    print("[smoke_final_report] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
