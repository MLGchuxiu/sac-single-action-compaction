#!/usr/bin/env python3
"""Aggregate one or more result JSONL files into markdown + CSV tables."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def load(paths: list[Path]) -> list[dict]:
    """Load rows, keeping the last record for each cell key."""
    by_key: dict[str, dict] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("key") or f"{row.get('model')}|{row.get('regime')}|{row.get('task_id')}|{row.get('state_version')}"
            by_key[key] = row
    return list(by_key.values())


def fmt(value: float, digits: int = 0) -> str:
    if value is None:
        return "-"
    if digits == 0:
        return f"{value:,.0f}"
    return f"{value:,.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+")
    parser.add_argument("--out", default="")
    parser.add_argument("--title", default="SAC results")
    args = parser.parse_args()

    rows = load([Path(item) for item in args.results])
    if not rows:
        raise SystemExit("no results")

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["regime"], row.get("state_version") or "-")].append(row)

    lines = [f"# {args.title}", "", "## Aggregate", "",
             "| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | "
             "Total input tok | Total output tok | Compactions | Requests | Wall s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (model, regime, version), items in sorted(groups.items()):
        peak = [item.get("peak_prompt_tokens") or 0 for item in items]
        lines.append(
            f"| {model} | {regime} | {version} | {len(items)} | "
            f"{sum(1 for item in items if item.get('ok'))}/{len(items)} | "
            f"{fmt(mean(item.get('score') or 0 for item in items), 2)} | "
            f"{fmt(mean(peak))}/{fmt(max(peak))} | "
            f"{fmt(sum((item.get('usage') or {}).get('prompt_tokens', 0) for item in items))} | "
            f"{fmt(sum((item.get('usage') or {}).get('completion_tokens', 0) for item in items))} | "
            f"{fmt(sum(item.get('compactions') or 0 for item in items))} | "
            f"{fmt(sum(item.get('requests') or 0 for item in items))} | "
            f"{fmt(mean(item.get('wall_s') or 0 for item in items), 1)} |"
        )

    lines += ["", "## Per task", "",
              "| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in sorted(rows, key=lambda item: (item["task_id"], item["model"], item["regime"], item.get("state_version") or "")):
        lines.append(
            f"| {row['task_id']} | {row['model']} | {row['regime']} | {row.get('state_version') or '-'} | "
            f"{fmt(row.get('score') or 0, 2)} | {'✅' if row.get('ok') else '❌'} | "
            f"{(row.get('detail') or '').replace('|', '/')[:90]} | "
            f"{fmt(row.get('peak_prompt_tokens'))} | "
            f"{fmt((row.get('usage') or {}).get('prompt_tokens', 0))} | "
            f"{fmt((row.get('usage') or {}).get('completion_tokens', 0))} | "
            f"{row.get('compactions', 0)} | {row.get('requests', 0)} |"
        )

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())