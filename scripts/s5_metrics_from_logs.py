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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute S5 metrics from log files")
    parser.add_argument("logs", nargs="+", help="log file paths")
    args = parser.parse_args()

    per = []
    for lp in args.logs:
        path = Path(lp)
        c = scan_log(path)
        per.append((path, c))

    total = merge_counts([x[1] for x in per])

    starts = total["download_start"]
    fails = total["task_failed"]
    completes = total["task_completed"]
    probe_total = total["hls_probe_ok"] + total["hls_probe_fail"]

    print("=== S5 Metrics From Logs ===")
    print(f"logs={len(per)}")
    print(f"sniffer_hit={total['sniffer_hit']}")
    print(f"download_start={starts}")
    print(f"task_completed={completes}")
    print(f"task_failed={fails}")
    print(f"retry_count={total['retry']}")
    print(f"hls_probe_ok={total['hls_probe_ok']}")
    print(f"hls_probe_fail={total['hls_probe_fail']}")
    print(f"nm3u8dlre_source_ok={total['nm_ok']}")

    print("--- Ratios ---")
    print(f"download_success_rate={safe_ratio(completes, starts):.4f}")
    print(f"download_fail_rate={safe_ratio(fails, starts):.4f}")
    print(f"probe_pass_rate={safe_ratio(total['hls_probe_ok'], probe_total):.4f}")

    print("--- Per File ---")
    for p, c in per:
        print(f"{p.name}: start={c['download_start']} complete={c['task_completed']} fail={c['task_failed']} probe_ok={c['hls_probe_ok']} probe_fail={c['hls_probe_fail']}")
