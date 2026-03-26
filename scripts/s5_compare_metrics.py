import argparse
import re
from pathlib import Path

PATTERNS = {
    "sniffer_hit": re.compile(r"event=sniffer_hit"),
    "hls_probe_ok": re.compile(r"event=hls_probe_ok|\[HLS-PROBE\].*预探测通过"),
    "hls_probe_fail": re.compile(r"event=hls_probe_fail|\[HLS-PROBE\].*预探测失败"),
    "download_start": re.compile(r"通知: 开始下载|已添加下载任务"),
    "task_failed": re.compile(r"\[FAILED\].*任务失败|通知: 下载失败"),
    "task_completed": re.compile(r"通知: 下载完成|任务完成|下载成功"),
    "retry": re.compile(r"event=download_retry|\[RETRY\]"),
    "nm_ok": re.compile(r"event=nm3u8dlre_source_ok"),
}


def scan_log(path: Path):
    counts = {k: 0 for k in PATTERNS}
    if not path.exists():
        return counts
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            for key, pat in PATTERNS.items():
                if pat.search(line):
                    counts[key] += 1
    return counts


def merge_counts(items):
    out = {k: 0 for k in PATTERNS}
    for c in items:
        for k in out:
            out[k] += c.get(k, 0)
    return out


def safe_ratio(a, b):
    return (a / b) if b else 0.0


def load_group(files):
    parsed = []
    for fp in files:
        p = Path(fp)
        parsed.append((p, scan_log(p)))
    total = merge_counts([x[1] for x in parsed])
    starts = total["download_start"]
    completes = total["task_completed"]
    fails = total["task_failed"]
    probe_total = total["hls_probe_ok"] + total["hls_probe_fail"]
    ratios = {
        "download_success_rate": safe_ratio(completes, starts),
        "download_fail_rate": safe_ratio(fails, starts),
        "probe_pass_rate": safe_ratio(total["hls_probe_ok"], probe_total),
    }
    return parsed, total, ratios


def pct_delta(new, old):
    if old == 0:
        return "n/a"
    return f"{((new - old) / old) * 100:.2f}%"


def fmt_float(x):
    return f"{x:.4f}"


def write_report(path: Path, baseline_files, candidate_files, base_total, base_ratios, cand_total, cand_ratios):
    lines = []
    lines.append("# S5 Metrics Compare Report")
    lines.append("")
    lines.append("## Baseline Logs")
    for p in baseline_files:
        lines.append(f"- {p}")
    lines.append("")
    lines.append("## Candidate Logs")
    for p in candidate_files:
        lines.append(f"- {p}")
    lines.append("")
    lines.append("## Metrics")
    lines.append("| Metric | Baseline | Candidate | Delta |")
    lines.append("|---|---:|---:|---:|")

    keys = ["sniffer_hit", "download_start", "task_completed", "task_failed", "retry", "hls_probe_ok", "hls_probe_fail", "nm_ok"]
    for k in keys:
        b = base_total.get(k, 0)
        c = cand_total.get(k, 0)
        lines.append(f"| {k} | {b} | {c} | {c - b:+d} |")

    lines.append(f"| download_success_rate | {fmt_float(base_ratios['download_success_rate'])} | {fmt_float(cand_ratios['download_success_rate'])} | {pct_delta(cand_ratios['download_success_rate'], base_ratios['download_success_rate'])} |")
    lines.append(f"| download_fail_rate | {fmt_float(base_ratios['download_fail_rate'])} | {fmt_float(cand_ratios['download_fail_rate'])} | {pct_delta(cand_ratios['download_fail_rate'], base_ratios['download_fail_rate'])} |")
    lines.append(f"| probe_pass_rate | {fmt_float(base_ratios['probe_pass_rate'])} | {fmt_float(cand_ratios['probe_pass_rate'])} | {pct_delta(cand_ratios['probe_pass_rate'], base_ratios['probe_pass_rate'])} |")

    lines.append("")
    lines.append("## Notes")
    lines.append("- Ensure baseline/candidate logs come from the same sample batch and workflow.")
    lines.append("- Exclude development dummy tasks, pause/cancel-only runs, and unrelated regression logs.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_group_args(group_args):
    files = []
    for item in group_args:
        if "*" in item or "?" in item:
            files.extend([str(p) for p in sorted(Path().glob(item))])
        else:
            files.append(item)
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare S5 metrics between baseline and candidate logs")
    parser.add_argument("--baseline", nargs="+", required=True, help="baseline log paths/globs")
    parser.add_argument("--candidate", nargs="+", required=True, help="candidate log paths/globs")
    parser.add_argument("--out", default="plans/phase_reports/S5_compare_report.md", help="output markdown report")
    args = parser.parse_args()

    baseline_files = parse_group_args(args.baseline)
    candidate_files = parse_group_args(args.candidate)

    if not baseline_files:
        raise SystemExit("no baseline log files found")
    if not candidate_files:
        raise SystemExit("no candidate log files found")

    _, base_total, base_ratios = load_group(baseline_files)
    _, cand_total, cand_ratios = load_group(candidate_files)

    print("=== S5 Compare Summary ===")
    print(f"baseline_logs={len(baseline_files)} candidate_logs={len(candidate_files)}")
    print(f"baseline_success_rate={fmt_float(base_ratios['download_success_rate'])}")
    print(f"candidate_success_rate={fmt_float(cand_ratios['download_success_rate'])}")
    print(f"baseline_probe_pass_rate={fmt_float(base_ratios['probe_pass_rate'])}")
    print(f"candidate_probe_pass_rate={fmt_float(cand_ratios['probe_pass_rate'])}")

    out_path = Path(args.out)
    write_report(
        out_path,
        baseline_files,
        candidate_files,
        base_total,
        base_ratios,
        cand_total,
        cand_ratios,
    )
    print(f"report={out_path}")
