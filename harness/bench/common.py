"""Shared plumbing for the benchmark adapters.

Each benchmark is reduced to a `BenchTask`: a prompt, a pristine workspace that
is copied per run, and a `verify(result) -> dict` function.  The runner boots a
real `dsh headless` agent (with or without the SAC bundle) inside an isolated
copy of the workspace, then scores the final answer / diff.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks"
WORK = ROOT / "work" / "bench"
# Run workspaces live outside the project tree: the agent must not be able to
# wander into the benchmark data (gold answers) while exploring its cwd.
RUN_ROOT = Path(os.environ.get("BENCH_RUN_ROOT", Path.home() / "dsh-bench-runs"))
DSH = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")


@dataclass
class BenchTask:
    bench: str
    task_id: str
    prompt: str
    workspace: Path          # pristine template, copied per run
    verify: Callable[[dict], dict]
    meta: dict[str, Any] = field(default_factory=dict)
    timeout_s: int = 1800

    def as_dict(self) -> dict[str, Any]:
        return {
            "bench": self.bench,
            "task_id": self.task_id,
            "prompt": self.prompt,
            "workspace": str(self.workspace),
            "meta": self.meta,
            "timeout_s": self.timeout_s,
        }


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def clone_workspace(src: Path, dst: Path) -> None:
    """Copy a task workspace; on APFS `cp -c` clones instead of duplicating."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cp", "-c", "-R", str(src), str(dst)],
                            capture_output=True, text=True)
    if result.returncode != 0:  # clonefile unsupported (e.g. cross-device)
        shutil.copytree(src, dst, symlinks=True)


def git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def init_git(workspace: Path) -> None:
    git(["init", "-q"], workspace)
    git(["add", "-A"], workspace)
    git(["-c", "user.email=bench@local", "-c", "user.name=bench",
         "commit", "-qm", "base"], workspace)


def workspace_diff(workspace: Path) -> str:
    """Return the tracked diff of the run workspace (agent changes only)."""
    result = git(["diff", "--binary", "HEAD"], workspace, timeout=120)
    return result.stdout if result.returncode == 0 else ""


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model reply (fenced or embedded).

    Models sometimes print a Python dict literal (single quotes) instead of
    JSON; `ast.literal_eval` accepts both without executing anything.
    """
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = [*fenced, text]
    for candidate in candidates:
        candidate = candidate.strip()
        for start, end in (("{", "}"), ("[", "]")):
            first = candidate.find(start)
            last = candidate.rfind(end)
            if first != -1 and last > first:
                snippet = candidate[first:last + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    pass
                try:
                    return ast.literal_eval(snippet)
                except (ValueError, SyntaxError):
                    pass
    return None


def token_f1(pred: str, gold: str) -> float:
    pred_tokens = _tokens(pred)
    gold_tokens = _tokens(gold)
    if not pred_tokens or not gold_tokens:
        return 0.0
    common = 0
    remaining = list(gold_tokens)
    for token in pred_tokens:
        if token in remaining:
            remaining.remove(token)
            common += 1
    precision = common / len(pred_tokens)
    recall = common / len(gold_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _tokens(text: str) -> list[str]:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(text).lower())
    return [token for token in text.split() if token]


def set_f1(pred: list[str], gold: list[str]) -> float:
    pred_set = {_norm_title(item) for item in pred}
    gold_set = {_norm_title(item) for item in gold}
    if not pred_set or not gold_set:
        return 0.0 if (pred_set or gold_set) else 1.0
    common = len(pred_set & gold_set)
    precision = common / len(pred_set)
    recall = common / len(gold_set)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lstrip("#").strip().lower())