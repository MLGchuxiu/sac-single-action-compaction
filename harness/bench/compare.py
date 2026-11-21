"""Compare benchmark iterations side by side."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def load(paths):
    records = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(record)
    return records


def median(values):
    values = [value for value in values if value is not None]
    return round(statistics.median(values), 1) if values else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    records = load(args.paths)

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for record in records:
        groups[(record.get("iteration", "?"), record.get("bench", "?"), record.get("arm", "?"))].append(record)

    lines = ["| 迭代 | 基准 | 范式 | n | 平均分 | 满分率 | 峰值(中位) | 总 token | 折叠 | 折叠错误 | 墙钟(中位,s) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (iteration, bench, arm), rows in sorted(groups.items()):
        scores = [row.get("score") or 0 for row in rows]
        peaks = [(row.get("metrics") or {}).get("peak_context") for row in rows]
        totals = []
        for row in rows:
            metrics = row.get("metrics") or {}
            totals.append((metrics.get("prompt_tokens") or 0)
                          + (metrics.get("summary_prompt_tokens") or 0)
                          + (metrics.get("summary_completion_tokens") or 0)
                          + (metrics.get("completion_tokens") or 0))
        folds = [(row.get("metrics") or {}).get("compactions") or 0 for row in rows]
        errors = [(row.get("metrics") or {}).get("compaction_error_count") or 0 for row in rows]
        walls = [(row.get("metrics") or {}).get("wall_s") for row in rows]
        lines.append(
            f"| {iteration} | {bench} | {arm} | {len(rows)} | "
            f"{round(sum(scores) / len(rows), 3)} | {sum(1 for s in scores if s >= 0.999)}/{len(rows)} | "
            f"{median(peaks)} | {sum(totals)} | {sum(folds)} | {sum(errors)} | {median(walls)} |")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())