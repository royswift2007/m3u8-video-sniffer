import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import argparse
from pathlib import Path

from core.m3u8_parser import M3U8FetchThread
from core.services.hls_probe import HLSProbe

URLS_DEFAULT = [
    "https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8",
    "https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8",
]


def run_case(url: str, headers: dict):
    probe = HLSProbe.probe(url, headers=headers, timeout=10)

    parser_thread = M3U8FetchThread(url, headers=headers)
    variants_holder = {"variants": None}

    def _on_finished(v):
        variants_holder["variants"] = v

    parser_thread.finished.connect(_on_finished)
    parser_thread.run()

    variants = variants_holder["variants"]
    if variants is None:
        variants = []

    return {
        "url": url,
        "probe_ok": bool(probe.get("ok")),
        "probe_stage": probe.get("stage"),
        "probe_error": probe.get("error", ""),
        "playlist_url": probe.get("playlist_url", ""),
        "key_url": probe.get("key_url", ""),
        "segment_url": probe.get("segment_url", ""),
        "variant_count": len(variants),
        "is_master": len(variants) > 0,
    }


def write_report(path: Path, rows: list[dict]):
    lines = []
    lines.append("# S5 两样本探测报告")
    lines.append("")
    lines.append("| URL | 预探测 | 阶段 | 变体数 | 备注 |")
    lines.append("|---|---|---|---:|---|")
    for r in rows:
        probe_flag = "PASS" if r["probe_ok"] else "FAIL"
        note = r["probe_error"] if r["probe_error"] else ""
        lines.append(f"| {r['url']} | {probe_flag} | {r['probe_stage']} | {r['variant_count']} | {note} |")

    lines.append("")
    lines.append("## 详细")
    for i, r in enumerate(rows, 1):
        lines.append(f"### 样本 {i}")
        lines.append(f"- url: {r['url']}")
        lines.append(f"- probe_ok: {r['probe_ok']}")
        lines.append(f"- probe_stage: {r['probe_stage']}")
        lines.append(f"- variant_count: {r['variant_count']}")
        lines.append(f"- playlist_url: {r['playlist_url']}")
        lines.append(f"- key_url: {r['key_url']}")
        lines.append(f"- segment_url: {r['segment_url']}")
        if r["probe_error"]:
            lines.append(f"- probe_error: {r['probe_error']}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run quick S5 probe for two urls")
    parser.add_argument("--referer", default="https://ddys.io/")
    parser.add_argument("--out", default="plans/phase_reports/S5_two_url_probe_report.md")
    parser.add_argument("urls", nargs="*", default=URLS_DEFAULT)
    args = parser.parse_args()

    headers = {
        "referer": args.referer,
        "origin": "https://ddys.io",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    rows = []
    for url in args.urls:
        rows.append(run_case(url, headers))

    out = Path(args.out)
    write_report(out, rows)

    print("=== S5 Two URL Probe ===")
    for r in rows:
        print(f"url={r['url']}")
        print(f"  probe_ok={r['probe_ok']} stage={r['probe_stage']} variants={r['variant_count']}")
        if r['probe_error']:
            print(f"  error={r['probe_error']}")
    print(f"report={out}")

