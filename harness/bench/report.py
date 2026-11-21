"""Aggregate benchmark runs into a markdown report."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def load(paths: list[Path]) -> list[dict]:
    records = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def median(values):
    values = [value for value in values if value is not None]
    return round(statistics.median(values), 1) if values else None


def mean(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize(records: list[dict]) -> str:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        groups[(record.get("bench", "?"), record.get("arm", "?"))].append(record)
    lines = ["| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (bench, arm), rows in sorted(groups.items()):
        scores = [row.get("score") or 0 for row in rows]
        peaks = [(row.get("metrics") or {}).get("peak_context") for row in rows]
        requests = [(row.get("metrics") or {}).get("requests") for row in rows]
        comps = [(row.get("metrics") or {}).get("compactions") for row in rows]
        errors = [(row.get("metrics") or {}).get("compaction_error_count") for row in rows]
        summary_tokens = [((row.get("metrics") or {}).get("summary_prompt_tokens") or 0)
                          + ((row.get("metrics") or {}).get("summary_completion_tokens") or 0)
                          for row in rows]
        main_tokens = [(row.get("metrics") or {}).get("prompt_tokens") or 0 for row in rows]
        completion = [(row.get("metrics") or {}).get("completion_tokens") or 0 for row in rows]
        total_tokens = [main + summary + comp for main, summary, comp in
                        zip(main_tokens, summary_tokens, completion)]
        walls = [(row.get("metrics") or {}).get("wall_s") for row in rows]
        lines.append(
            f"| {bench} | {arm} | {len(rows)} | {mean(scores)} | "
            f"{sum(1 for s in scores if s >= 0.999)}/{len(rows)} | "
            f"{median(peaks)} / {max([p for p in peaks if p is not None], default=None)} | "
            f"{sum(main_tokens)} | {sum(summary_tokens)} | {sum(total_tokens)} | "
            f"{mean(requests)} | {sum(c for c in comps if c is not None)} | "
            f"{sum(e for e in errors if e is not None)} | {median(walls)} |")
    return "\n".join(lines)


def detail_table(records: list[dict]) -> str:
    lines = ["| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for record in sorted(records, key=lambda r: (r.get("bench", ""), r.get("task_id", ""), r.get("arm", ""))):
        metrics = record.get("metrics") or {}
        detail = record.get("detail")
        if isinstance(detail, dict):
            note = detail.get("reason") or detail.get("actions_matched") or json.dumps(detail, ensure_ascii=False)[:80]
        else:
            note = str(detail)[:80]
        lines.append(
            f"| {record.get('bench')} | {record.get('task_id')} | {record.get('arm')} | "
            f"{record.get('score')} | {metrics.get('peak_context')} | {metrics.get('requests')} | "
            f"{metrics.get('compactions')} | {metrics.get('compaction_error_count')} | "
            f"{(metrics.get('summary_prompt_tokens') or 0) + (metrics.get('summary_completion_tokens') or 0)} | "
            f"{metrics.get('wall_s')} | {str(note).replace('|', '/')[:90]} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--out", default="")
    parser.add_argument("--title", default="Benchmark summary")
    args = parser.parse_args()
    paths = [Path(path) for path in args.paths]
    records = load(paths)
    text = f"# {args.title}\n\n## 汇总\n\n{summarize(records)}\n\n## 明细\n\n{detail_table(records)}\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(records)} records)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())