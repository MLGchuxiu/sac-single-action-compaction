"""Loong (arXiv:2406.17419) adapter.

The official benchmark pastes every document into one prompt.  For an agentic
comparison we keep the *task* and the *documents* but move the documents onto
disk: the agent must read what it needs, which is exactly the setting in which
single-action compaction is supposed to pay off.  Verification uses the gold
answer and the same scoring families as the benchmark (set-F1 for the
citation/classification JSON tasks, normalised match for span answers).
"""
from __future__ import annotations

import glob
import json
import re
from pathlib import Path

from .common import (BENCH, WORK, BenchTask, read_jsonl, set_f1, token_f1,
                     write_text)

LOONG = BENCH / "loong"
DOCS = LOONG / "data" / "doc"
ROWS = list(read_jsonl(LOONG / "data" / "loong.jsonl"))

_LEGAL = json.loads((DOCS / "legal" / "legal.json").read_text(encoding="utf-8"))


def _content(row: dict, doc_name: str, index: int) -> str:
    doc_type = row["type"]
    level = row["level"]
    if doc_type == "financial":
        pattern = (f"*2024-{doc_name}*.txt" if str(level) != "4"
                   else f"*{doc_name}*.txt")
        matches = glob.glob(str(DOCS / "financial" / pattern))
        if not matches:
            raise FileNotFoundError(f"financial doc not found: {doc_name}")
        path = Path(matches[0])
        title = path.stem.split("-")[-1]
        return f"《{title}》\n" + path.read_text(encoding="utf-8", errors="replace")
    if doc_type == "paper":
        path = DOCS / "paper" / doc_name
        text = path.read_text(encoding="utf-8", errors="replace")
        title = text.split("\n", 1)[0].strip("#").strip()
        return f"{title}\n" + text
    if doc_type == "legal":
        entry = _LEGAL[doc_name]
        content = entry["content"]
        if not (level == 4 and "阅读以上判决文书，我将给你若干份判决结果：" in row["instruction"]):
            content += entry["result"]
        return f"《判决文书{index + 1}》\n" + content
    raise ValueError(f"unknown loong doc type {doc_type}")


def _prompt(row: dict, count: int) -> str:
    instruction = row["instruction"]
    return (
        f"{instruction}\n\n"
        f"# Documents\n"
        f"This task has {count} source documents, stored as files in ./docs/ "
        f"(doc_01.txt … doc_{count:02d}.txt). The first line of each file is its "
        f"title. Read the files you need with the read/grep tools; nothing is "
        f"pre-loaded into the conversation.\n\n"
        f"# Question\n{row['question']}\n\n"
        f"Finish with your final answer in exactly the format the instructions "
        f"request (a JSON object for structured tasks, the bare value for span "
        f"questions).\n"
    )


def _verify_structured(gold: dict):
    def verify(result: dict) -> dict:
        pred = result.get("parsed")
        if not isinstance(pred, dict):
            return {"score": 0.0, "detail": "no JSON object in final answer"}
        scores = []
        detail = {}
        for key, gold_list in gold.items():
            pred_list = pred.get(key) or []
            if not isinstance(pred_list, list):
                pred_list = [pred_list]
            score = set_f1(pred_list, gold_list)
            scores.append(score)
            detail[key] = round(score, 3)
        return {"score": sum(scores) / len(scores) if scores else 0.0,
                "detail": detail}
    return verify


def _verify_span(gold: str):
    doc_ids = set(re.findall(r"判决文书\s*(\d+)", str(gold)))

    def verify(result: dict) -> dict:
        answer = (result.get("final_text") or "").strip()
        if not answer:
            return {"score": 0.0, "detail": "empty answer"}
        if doc_ids:
            predicted = set(re.findall(r"判决文书\s*(\d+)", answer))
            score = set_f1(sorted(predicted), sorted(doc_ids))
            return {"score": round(score, 3),
                    "detail": {"predicted": sorted(predicted), "gold": sorted(doc_ids)}}
        norm = answer.lower().replace(",", "").replace("$", "").replace(" ", "")
        target = str(gold).lower().replace(",", "").replace("$", "").replace(" ", "")
        if target and target in norm:
            return {"score": 1.0, "detail": "normalised match"}
        return {"score": round(token_f1(answer, str(gold)), 3),
                "detail": "token f1"}
    return verify


def build(row: dict, name: str) -> BenchTask:
    contents = [_content(row, doc, index) for index, doc in enumerate(row["doc"])]
    workspace = WORK / "tasks" / "loong" / name
    for index, text in enumerate(contents, start=1):
        write_text(workspace / "docs" / f"doc_{index:02d}.txt", text + "\n\n")
    write_text(workspace / "README.md",
               "Source documents for this task. Read them with the tools.\n")
    answer = row["answer"]
    if isinstance(answer, dict):
        verify = _verify_structured(answer)
    else:
        verify = _verify_span(answer)
    return BenchTask(
        bench="loong",
        task_id=name,
        prompt=_prompt(row, len(contents)),
        workspace=workspace,
        verify=verify,
        meta={"type": row["type"], "level": row["level"], "language": row["language"],
              "docs": len(contents), "chars": sum(len(text) for text in contents),
              "gold": answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)},
    )


# Deterministic selection: 2 paper / 2 financial / 2 legal, ~22-140 KB each.
SELECTION = [
    ("paper", 3, 0), ("paper", 3, 1),
    ("financial", 2, 0), ("financial", 2, 5),
    ("legal", 2, 0),
]


def load_tasks(limit: int | None = None) -> list[BenchTask]:
    tasks = []
    for doc_type, level, index in SELECTION:
        rows = [row for row in ROWS if row["type"] == doc_type and row["level"] == level]
        if index >= len(rows):
            continue
        name = f"{doc_type}-L{level}-{index}"
        tasks.append(build(rows[index], name))
    if limit:
        tasks = tasks[:limit]
    return tasks