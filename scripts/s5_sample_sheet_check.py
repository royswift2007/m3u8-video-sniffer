import argparse
from pathlib import Path


def parse_rows(md_path: Path):
    rows = []
    with md_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|"):
                continue
            if line.startswith("| ID ") or line.startswith("|---"):
                continue
            parts = [x.strip() for x in line.strip("|").split("|")]
            if len(parts) < 9:
                continue
            rows.append(
                {
                    "id": parts[0],
                    "category": parts[1],
                    "url": parts[2],
                    "referer": parts[3],
                    "cookie": parts[4],
                    "expect_capture": parts[5],
                    "expect_download": parts[6],
                    "note": parts[7],
                    "status": parts[8].lower(),
                }
            )
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate S5 sample sheet completeness")
    parser.add_argument("--file", default="plans/test_samples.md")
    parser.add_argument("--min", type=int, default=20)
    args = parser.parse_args()

    p = Path(args.file)
    if not p.exists():
        raise SystemExit(f"missing sample file: {p}")

    rows = parse_rows(p)
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    missing_url = sum(1 for r in rows if not r["url"])

    print("=== S5 Sample Sheet Check ===")
    print(f"file={p}")
    print(f"total_rows={total}")
    print(f"done_rows={done}")
    print(f"missing_url_rows={missing_url}")

    if total < args.min:
        print(f"FAIL: rows < min ({total} < {args.min})")
        raise SystemExit(1)

    if missing_url > 0:
        print(f"WARN: still has rows without URL ({missing_url})")

    cat_counts = {}
    for r in rows:
        cat = r["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    print("categories=")
    for k in sorted(cat_counts):
        print(f"  - {k}: {cat_counts[k]}")

    print("PASS: sample sheet structure is valid")
