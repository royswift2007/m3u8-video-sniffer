"""
Run S8-S14 phase execution and verification, and write phase reports.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.hls_probe import HLSProbe
from utils.config_manager import ConfigManager

PHASE_DIR = ROOT / "plans" / "phase_reports"
SAMPLES_FILE = ROOT / "plans" / "test_samples.md"


@dataclass
class CmdResult:
    cmd: str
    code: int
    out: str


def run_cmd(cmd: str, timeout: int = 180) -> CmdResult:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="ignore",
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return CmdResult(cmd=cmd, code=proc.returncode, out=out.strip())


def write_report(name: str, lines: list[str]) -> Path:
    PHASE_DIR.mkdir(parents=True, exist_ok=True)
    p = PHASE_DIR / name
    p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return p


def parse_sample_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.exists():
        return rows
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 9:
            continue
        if cols[0] in {"ID", "---"}:
            continue
        row = {
            "id": cols[0],
            "category": cols[1],
            "url": cols[2],
            "referer": cols[3],
            "cookie": cols[4],
            "expected_capture": cols[5],
            "expected_download": cols[6],
            "note": cols[7],
            "status": cols[8].lower(),
        }
        rows.append(row)
    return rows


def phase_s8_probe(done_only: bool) -> dict[str, Any]:
    rows = parse_sample_rows(SAMPLES_FILE)
    candidates = [r for r in rows if r.get("url")]
    if done_only:
        candidates = [r for r in candidates if r.get("status") == "done"]

    results: list[dict[str, Any]] = []
    for r in candidates:
        headers = {}
        if r.get("referer"):
            headers["Referer"] = r["referer"]
        if r.get("cookie"):
            headers["Cookie"] = r["cookie"]
        probe = HLSProbe.probe(r["url"], headers=headers, timeout=8)
        results.append(
            {
                "id": r["id"],
                "url": r["url"],
                "expected_download": r["expected_download"],
                "status": r["status"],
                "probe_ok": bool(probe.get("ok")),
                "probe_stage": probe.get("stage", "unknown"),
                "error": probe.get("error", ""),
            }
        )

    out_json = PHASE_DIR / "S8_probe_results.json"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok_count = sum(1 for x in results if x["probe_ok"])
    lines = [
        "# DEBUG S8 报告（真实样本批次探测）",
        "",
        f"- 日期：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 样本文件：`{SAMPLES_FILE.relative_to(ROOT)}`",
        f"- 参与探测：{len(results)}",
        f"- probe_ok：{ok_count}",
        f"- probe_fail：{len(results) - ok_count}",
        "",
        "## 明细",
    ]
    for item in results:
        lines.extend(
            [
                f"- {item['id']}: ok={item['probe_ok']} stage={item['probe_stage']} url={item['url']}",
            ]
        )
        if item.get("error"):
            lines.append(f"  - error={item['error']}")
    lines.extend(["", f"- 结果文件：`{out_json.relative_to(ROOT)}`"])
    report = write_report("DEBUG_S8_report.md", lines)
    return {"results": results, "report": report}


def phase_s9_expected_compare(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        pass_count = 0
    else:
        pass_count = sum(
            1
            for r in results
            if (r["expected_download"] == "Y" and r["probe_ok"])
            or (r["expected_download"] == "N" and not r["probe_ok"])
        )
    rate = (pass_count / total) if total else 0.0
    lines = [
        "# DEBUG S9 报告（期望与实测对照）",
        "",
        f"- 总样本：{total}",
        f"- 命中期望：{pass_count}",
        f"- 命中率：{rate:.2%}",
        "",
        "## 规则",
        "- 期望下载=Y：probe_ok 视为命中",
        "- 期望下载=N：probe_fail 视为命中",
    ]
    report = write_report("DEBUG_S9_report.md", lines)
    return {"report": report, "match_rate": rate}


def phase_s10_log_audit() -> dict[str, Any]:
    log_dir = ROOT / "logs"
    logs = sorted(log_dir.glob("m3u8sniffer_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest = logs[0] if logs else None
    level_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "DEBUG": 0}
    event_count = 0
    mojibake_hits = 0
    if latest and latest.exists():
        text = latest.read_text(encoding="utf-8", errors="ignore")
        for lv in level_counts:
            level_counts[lv] = len(re.findall(rf"\[{lv}\]", text))
        event_count = len(re.findall(r"\bevent=", text))
        mojibake_hits = len(re.findall(r"褰|鍙|璇|寮|瀹|鏃|缁|闃|\\?\\?\\?", text))
    lines = [
        "# DEBUG S10 报告（日志健康审计）",
        "",
        f"- 最新日志：`{latest.relative_to(ROOT) if latest else 'N/A'}`",
        f"- INFO={level_counts['INFO']} WARNING={level_counts['WARNING']} ERROR={level_counts['ERROR']} DEBUG={level_counts['DEBUG']}",
        f"- 结构化事件字段(event=)数量：{event_count}",
        f"- 疑似乱码命中：{mojibake_hits}",
    ]
    report = write_report("DEBUG_S10_report.md", lines)
    return {"report": report, "latest": str(latest) if latest else ""}


def phase_s11_config_audit() -> dict[str, Any]:
    cm = ConfigManager("config.json")
    defaults = cm._build_default_config()
    current = cm.config
    missing = [k for k in defaults.keys() if k not in current]
    type_mismatch = []
    for k, v in defaults.items():
        if k in current and current[k] is not None and not isinstance(current[k], type(v)):
            type_mismatch.append(k)
    lines = [
        "# DEBUG S11 报告（配置结构审计）",
        "",
        f"- 缺失顶层键数量：{len(missing)}",
        f"- 类型不匹配顶层键数量：{len(type_mismatch)}",
    ]
    if missing:
        lines.append("- 缺失键：" + ", ".join(missing))
    if type_mismatch:
        lines.append("- 类型不匹配键：" + ", ".join(type_mismatch))
    report = write_report("DEBUG_S11_report.md", lines)
    return {"report": report, "missing": missing, "type_mismatch": type_mismatch}


def phase_s12_script_selfcheck() -> dict[str, Any]:
    required = [
        "run_smoke_tests.bat",
        "s4_fault_injection.py",
        "s5_smoke.py",
        "s5_sample_sheet_check.py",
        "s5_compare_metrics.py",
        "s5_two_url_probe.py",
    ]
    missing = [x for x in required if not (ROOT / "scripts" / x).exists()]
    lines = [
        "# DEBUG S12 报告（脚本自检）",
        "",
        f"- 必需脚本数：{len(required)}",
        f"- 缺失脚本数：{len(missing)}",
    ]
    if missing:
        lines.append("- 缺失项：" + ", ".join(missing))
    report = write_report("DEBUG_S12_report.md", lines)
    return {"report": report, "missing": missing}


def phase_s13_artifact_check() -> dict[str, Any]:
    required_reports = [f"DEBUG_S{i}_report.md" for i in range(1, 8)]
    miss = [r for r in required_reports if not (PHASE_DIR / r).exists()]
    lines = [
        "# DEBUG S13 报告（交付物完整性检查）",
        "",
        f"- 必需阶段报告：{len(required_reports)}",
        f"- 缺失：{len(miss)}",
    ]
    if miss:
        lines.append("- 缺失报告：" + ", ".join(miss))
    report = write_report("DEBUG_S13_report.md", lines)
    return {"report": report, "missing": miss}


def phase_s14_full_gate() -> dict[str, Any]:
    cmds = [
        "python -m compileall main.py protocol_handler.pyw core ui engines utils tests",
        "python -m pytest tests -q -p no:cacheprovider",
        "python scripts/s4_fault_injection.py",
        "python scripts/s5_smoke.py",
        "cmd /c scripts\\run_smoke_tests.bat",
    ]
    results = [run_cmd(c, timeout=240) for c in cmds]
    failed = [r for r in results if r.code != 0]
    lines = [
        "# DEBUG S14 报告（全链路门禁）",
        "",
        f"- 命令总数：{len(results)}",
        f"- 失败数量：{len(failed)}",
        "",
        "## 命令结果",
    ]
    for r in results:
        lines.append(f"- `{r.cmd}` -> code={r.code}")
    report = write_report("DEBUG_S14_report.md", lines)
    return {"report": report, "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run S8-S14 phase execution and verification")
    parser.add_argument("--done-only", action="store_true", help="Only probe rows with status=done in S8")
    args = parser.parse_args()

    s8 = phase_s8_probe(done_only=args.done_only)
    s9 = phase_s9_expected_compare(s8["results"])
    s10 = phase_s10_log_audit()
    s11 = phase_s11_config_audit()
    s12 = phase_s12_script_selfcheck()
    s13 = phase_s13_artifact_check()
    s14 = phase_s14_full_gate()

    summary = [
        "# S8-S14 Runner Summary",
        "",
        f"- S8: {Path(s8['report']).name}",
        f"- S9: {Path(s9['report']).name}",
        f"- S10: {Path(s10['report']).name}",
        f"- S11: {Path(s11['report']).name}",
        f"- S12: {Path(s12['report']).name}",
        f"- S13: {Path(s13['report']).name}",
        f"- S14: {Path(s14['report']).name}",
        "",
        f"- S14 failed commands: {len(s14['failed'])}",
    ]
    write_report("DEBUG_S8_S14_summary.md", summary)

    return 1 if s14["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
