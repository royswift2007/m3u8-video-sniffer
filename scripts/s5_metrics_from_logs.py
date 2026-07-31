import argparse
import re
from pathlib import Path

PATTERNS = {
    "sniffer_hit": re.compile(r"event=sniffer_hit"),
    "hls_probe_ok": re.compile(r"event=hls_probe_ok|\[HLS-PROBE\].*预探测通过"),
    "hls_probe_fail": re.compile(r"event=hls_probe_fail|event=hls_probe_failed|\[HLS-PROBE\].*预探测失败"),
    "hls_probe_soft_fail": re.compile(r"event=hls_probe_soft_failed|event=hls_probe_failed_soft_allowed"),
    "download_start": re.compile(r"通知: 开始下载|已添加下载任务"),
    "task_failed": re.compile(r"\[FAILED\].*任务失败|通知: 下载失败"),
    "task_completed": re.compile(r"通知: 下载完成|任务完成|下载成功"),
    "retry": re.compile(r"event=download_retry|\[RETRY\]"),
    "rate_limit_backoff": re.compile(r"event=download_rate_limit_backoff"),
    "auth_retry": re.compile(r"event=download_auth_retry"),
    "auth_retry_success": re.compile(r"event=auth_retry_success"),
    "auth_retry_failed": re.compile(r"event=download_auth_retry_failed"),
    "fallback_success": re.compile(r"event=download_fallback_recovered"),
    "segment_suppressed": re.compile(r"event=segment_suppressed"),
    "low_concurrency_retry": re.compile(r"event=nm3u8dlre_low_concurrency_retry|event=aria2_low_connection_retry"),
    "nm_ok": re.compile(r"event=nm3u8dlre_source_ok"),
    "fail_reason_auth": re.compile(r"event=fail_reason_auth\b"),
    "fail_reason_rate_limit": re.compile(r"event=fail_reason_rate_limit\b"),
    "fail_reason_timeout": re.compile(r"event=fail_reason_timeout\b"),
    "fail_reason_parse": re.compile(r"event=fail_reason_parse\b"),
    "fail_reason_drm": re.compile(r"event=fail_reason_drm\b"),
    "fail_reason_expired": re.compile(r"event=fail_reason_expired\b"),
    "fail_reason_geo": re.compile(r"event=fail_reason_geo\b"),
    "fail_reason_tls": re.compile(r"event=fail_reason_tls\b"),
    "fail_reason_disk": re.compile(r"event=fail_reason_disk\b"),
    "fail_reason_segment_noise": re.compile(r"event=fail_reason_segment_noise\b"),
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
    print(f"hls_probe_soft_fail={total['hls_probe_soft_fail']}")
    print(f"nm3u8dlre_source_ok={total['nm_ok']}")

    print("--- Strategy / Reasons ---")
    for key in sorted(k for k in total if k not in {"sniffer_hit", "download_start", "task_completed", "task_failed", "retry", "hls_probe_ok", "hls_probe_fail", "hls_probe_soft_fail", "nm_ok"}):
        print(f"{key}={total[key]}")

    print("--- Ratios ---")
    print(f"download_success_rate={safe_ratio(completes, starts):.4f}")
    print(f"download_fail_rate={safe_ratio(fails, starts):.4f}")
    print(f"probe_pass_rate={safe_ratio(total['hls_probe_ok'], probe_total):.4f}")
    print(f"auth_retry_success_rate={safe_ratio(total['auth_retry_success'], total['auth_retry']):.4f}")
    print(f"rate_limit_backoff_per_retry={safe_ratio(total['rate_limit_backoff'], total['retry']):.4f}")

    print("--- Per File ---")
    for p, c in per:
        print(f"{p.name}: start={c['download_start']} complete={c['task_completed']} fail={c['task_failed']} probe_ok={c['hls_probe_ok']} probe_fail={c['hls_probe_fail']}")
