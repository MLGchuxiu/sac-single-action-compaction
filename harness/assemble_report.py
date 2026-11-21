#!/usr/bin/env python3
"""Assemble the final report tables from every results/*.jsonl and inject them
into reports/REPORT.md at the <!--TABLES--> and <!--ITERATIONS--> markers."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "REPORT.md"


def load() -> tuple[list[dict], list[dict]]:
    """Return (every row with its source, latest row per cell key)."""
    everything: list[dict] = []
    latest: dict[str, dict] = {}
    for path in sorted((ROOT / "results").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_source"] = path.name
            everything.append(row)
            latest[row["key"]] = row
    return everything, list(latest.values())


def n(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def agg(rows: list[dict]) -> dict:
    return {
        "runs": len(rows),
        "pass": sum(1 for row in rows if row.get("ok")),
        "score": mean(row.get("score") or 0 for row in rows),
        "peak_avg": mean(row.get("peak_prompt_tokens") or 0 for row in rows),
        "peak_max": max(row.get("peak_prompt_tokens") or 0 for row in rows),
        "input": sum((row.get("usage") or {}).get("prompt_tokens", 0) for row in rows),
        "output": sum((row.get("usage") or {}).get("completion_tokens", 0) for row in rows),
        "reasoning": sum((row.get("usage") or {}).get("reasoning_tokens", 0) for row in rows),
        "comp": sum(row.get("compactions") or 0 for row in rows),
        "req": sum(row.get("requests") or 0 for row in rows),
        "wall": mean(row.get("wall_s") or 0 for row in rows),
        "viol": sum(row.get("budget_violations") or 0 for row in rows),
    }


SHORT = ["code_edit", "doc_read", "multimodal", "long_output", "state_retention"]
LONG = ["swe_multifile", "doc_qa_multi"]


def table_regimes(rows: list[dict]) -> str:
    """Regime comparison on the iteration-1 task set (v1 for SAC)."""
    lines = [
        "### 4.1 三种范式对照（迭代 1，5 个短程任务，SAC 用 v1 模板）", "",
        "| 模型 | 范式 | 通过 | 平均分 | 峰值上下文 均值/最大 | 总输入 token | 总输出 token | 压缩次数 | 请求数 | 预算违规 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model in ("qwen3.7-flash", "deepseek-v4.1-flash"):
        for regime in ("fha", "eocc", "sac"):
            subset = [row for row in rows if row["model"] == model and row["regime"] == regime
                      and row["task_id"] in SHORT and row.get("state_version") in ("", "-", "v1")
                      and row.get("_source") == "runs_iter1.jsonl"]
            if not subset:
                continue
            stats = agg(subset)
            lines.append(
                f"| {model} | {regime.upper()} | {stats['pass']}/{stats['runs']} | {n(stats['score'], 2)} | "
                f"{n(stats['peak_avg'])}/{n(stats['peak_max'])} | {n(stats['input'])} | {n(stats['output'])} | "
                f"{stats['comp']} | {stats['req']} | {stats['viol']} |")
    return "\n".join(lines)


def table_ladder(rows: list[dict]) -> str:
    """SAC state-template ladder across all seven tasks."""
    lines = [
        "### 4.2 状态模板迭代阶梯（SAC，7 个任务）", "",
        "| 模板 | 模型 | 通过 | 平均分 | 峰值上下文 均值/最大 | 总输入 token | 压缩次数 | 请求数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for version in ("v1", "v2", "v3", "v4"):
        for model in ("qwen3.7-flash", "deepseek-v4.1-flash"):
            subset = [row for row in rows if row["model"] == model and row["regime"] == "sac"
                      and row.get("state_version") == version]
            if not subset:
                continue
            stats = agg(subset)
            lines.append(
                f"| {version} | {model} | {stats['pass']}/{stats['runs']} | {n(stats['score'], 2)} | "
                f"{n(stats['peak_avg'])}/{n(stats['peak_max'])} | {n(stats['input'])} | {stats['comp']} | {stats['req']} |")
    return "\n".join(lines)


def table_long(rows: list[dict]) -> str:
    """Long-horizon tasks: every regime, SAC at its best available template."""
    best = {"v1": 0, "v2": 1, "v3": 2, "v4": 3}
    lines = [
        "### 4.3 长程任务（`swe_multifile` 8 缺陷 / `doc_qa_multi` 6 问）", "",
        "| 模型 | 任务 | FHA 分数 / 峰值 | EoCC 分数 / 峰值 | SAC 分数 / 峰值 (模板) |",
        "|---|---|---|---|---|",
    ]
    for model in ("qwen3.7-flash", "deepseek-v4.1-flash"):
        for task in LONG:
            cells = []
            for regime in ("fha", "eocc"):
                subset = [row for row in rows if row["model"] == model and row["regime"] == regime and row["task_id"] == task]
                cells.append(f"{n(subset[0]['score'], 2)} / {n(subset[0]['peak_prompt_tokens'])}" if subset else "-")
            sac = [row for row in rows if row["model"] == model and row["regime"] == "sac" and row["task_id"] == task]
            if sac:
                pick = max(sac, key=lambda row: (row.get("score") or 0, best.get(row.get("state_version"), -1)))
                cells.append(f"{n(pick['score'], 2)} / {n(pick['peak_prompt_tokens'])} ({pick.get('state_version')})")
            else:
                cells.append("-")
            lines.append(f"| {model} | {task} | {cells[0]} | {cells[1]} | {cells[2]} |")
    return "\n".join(lines)


def table_tasks(rows: list[dict]) -> str:
    """Per-task detail for the final template of each model."""
    lines = [
        "### 4.4 逐任务明细（SAC v4 与 v1 对照）", "",
        "| 任务 | 模型 | v1 分数 / 请求 | v4 分数 / 请求 | v1 峰值 | v4 峰值 |",
        "|---|---|---|---|---|---|",
    ]
    for task in SHORT + LONG:
        for model in ("qwen3.7-flash", "deepseek-v4.1-flash"):
            def pick(version):
                subset = [row for row in rows if row["model"] == model and row["regime"] == "sac"
                          and row["task_id"] == task and row.get("state_version") == version]
                return subset[0] if subset else None
            one, four = pick("v1"), pick("v4")
            if one is None and four is None:
                continue
            cells = []
            for row in (one, four):
                cells.append(f"{n(row['score'], 2)} / {row['requests']}" if row else "-")
            cells.append(n(one["peak_prompt_tokens"]) if one else "-")
            cells.append(n(four["peak_prompt_tokens"]) if four else "-")
            lines.append(f"| {task} | {model} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
    return "\n".join(lines)


def main() -> int:
    everything, rows = load()
    print(f"loaded {len(everything)} rows / {len(rows)} unique cells", file=sys.stderr)
    tables = "\n\n".join([table_regimes(everything), table_ladder(rows), table_long(rows), table_tasks(rows)])
    text = REPORT.read_text(encoding="utf-8")
    start = "<!--TABLES:START-->"
    end = "<!--TABLES:END-->"
    if start in text and end in text:
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        text = f"{head}{start}\n{tables}\n{end}{tail}"
    else:
        text = text.replace("<!--TABLES-->", tables)
    REPORT.write_text(text, encoding="utf-8")
    print(tables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())