"""SWE-bench Verified adapter (patch + test execution).

Each instance is a checkout of the repository at `base_commit` (downloaded from
codeload).  The agent gets only the problem statement; after the run we apply the
official `test_patch` and run the `FAIL_TO_PASS` tests, which is the benchmark's
own resolution criterion.  When an environment cannot be built, the record says
so and falls back to a patch-localisation proxy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from .common import BENCH, ROOT, WORK, BenchTask, git, init_git, read_jsonl, write_text

CACHE = WORK / "cache"
TASKS = WORK / "tasks" / "swe"
VENV_PY = WORK / "venv" / "bin" / "python"
ROWS = list(read_jsonl(BENCH / "swe-bench-verified" / "swe-bench-verified.jsonl"))
BY_ID = {row["instance_id"]: row for row in ROWS}

# Repos with light, pure-Python test dependencies.
# Instances whose FAIL_TO_PASS suite is reproducible in this environment
# (Python 3.12, no Docker): validated by running base -> fail, gold -> pass.
SELECTION = [
    "sympy__sympy-24066",
    "sympy__sympy-24539",
    "sympy__sympy-24213",
]


def _download(row: dict) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / f"{row['instance_id']}.tar.gz"
    if archive.exists() and archive.stat().st_size > 0:
        return archive
    url = f"https://codeload.github.com/{row['repo']}/tar.gz/{row['base_commit']}"
    with urllib.request.urlopen(url, timeout=180) as response:
        data = response.read()
    archive.write_bytes(data)
    return archive


def _gold_files(row: dict) -> list[str]:
    return sorted({match for match in re.findall(r"^\+\+\+ b/(\S+)", row["patch"], re.M)})


def _test_files(row: dict) -> list[str]:
    return sorted({match for match in re.findall(r"^\+\+\+ b/(\S+)", row["test_patch"], re.M)
                   if Path(match).name.startswith("test") or "/tests/" in match})


def _prompt(row: dict) -> str:
    return (
        "You are working inside a checkout of the repository "
        f"`{row['repo']}` at the commit before a bug fix.\n\n"
        "# Issue\n"
        f"{row['problem_statement'].strip()}\n\n"
        "# Your task\n"
        "Find the cause and fix it in the source files. You may run the existing "
        "tests. Do not edit the test files: the grader applies the official test "
        "patch afterwards.\n\n"
        "Work efficiently: read the code around the issue, make the smallest "
        "correct change, and run at most the test module(s) that cover it. Do not "
        "run the whole test suite, and do not chase unrelated pre-existing "
        "failures.\n\n"
        "When you are done, leave the working tree modified (no commit needed).\n"
    )


def _run(command: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str]:
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                                timeout=timeout, env={**os.environ, "PYTHONPATH": str(cwd)})
        return result.returncode, (result.stdout + result.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def _verify(row: dict):
    gold_files = _gold_files(row)
    test_files = _test_files(row)
    f2p = json.loads(row["FAIL_TO_PASS"]) if isinstance(row["FAIL_TO_PASS"], str) else row["FAIL_TO_PASS"]
    p2p = json.loads(row["PASS_TO_PASS"]) if isinstance(row["PASS_TO_PASS"], str) else row["PASS_TO_PASS"]

    def verify(result: dict) -> dict:
        workspace = Path(result["workspace"])
        diff = result.get("diff") or ""
        touched = sorted(set(re.findall(r"^diff --git a/(\S+)", diff, re.M)))
        hit = len(set(touched) & set(gold_files)) / len(gold_files) if gold_files else 0.0
        detail = {"files": touched, "gold_files": gold_files, "file_recall": round(hit, 3)}
        if not touched:
            return {"score": 0.0, "detail": {**detail, "reason": "empty diff"}}

        # Apply the official test patch on top of the agent's tree, then run F2P.
        patch_path = workspace / ".bench_test.patch"
        patch_path.write_text(row["test_patch"], encoding="utf-8")
        apply = git(["apply", "--3way", str(patch_path)], workspace, timeout=120)
        if apply.returncode != 0:
            apply = git(["apply", str(patch_path)], workspace, timeout=120)
        if apply.returncode != 0:
            return {"score": round(0.5 * hit, 3),
                    "detail": {**detail, "reason": "test patch does not apply",
                               "stderr": apply.stderr[-500:]}}

        targets = [name.split("::")[0] for name in f2p if "::" in name] or test_files
        selectors = [name.split("::")[1] for name in f2p if "::" in name]
        command = [str(VENV_PY), "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
        if selectors:
            command += ["-k", " or ".join(selectors)]
        code, output = _run(command, workspace, timeout=600)
        resolved = code == 0
        detail["test_command"] = " ".join(command)
        detail["test_tail"] = output[-1200:]
        score = 1.0 if resolved else round(0.4 * hit + 0.1, 3)
        return {"score": score, "detail": detail}
    return verify


def build(row: dict) -> BenchTask:
    name = row["instance_id"]
    workspace = TASKS / name
    if not (workspace / ".git").exists():
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        archive = _download(row)
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            prefix = members[0].name.split("/")[0]
            for member in members:
                parts = member.name.split("/", 1)
                if len(parts) == 1:
                    continue
                member.name = parts[1]
                tar.extract(member, path=workspace, filter="data")
        init_git(workspace)
        write_text(workspace / ".bench_instance.json",
                   json.dumps({"instance_id": name, "repo": row["repo"],
                               "base_commit": row["base_commit"]}, indent=2))
        git(["add", "-A"], workspace)
        git(["-c", "user.email=bench@local", "-c", "user.name=bench",
             "commit", "-qm", "base"], workspace)
    return BenchTask(
        bench="swe",
        task_id=name,
        prompt=_prompt(row),
        workspace=workspace,
        verify=_verify(row),
        meta={"repo": row["repo"], "version": row["version"],
              "gold_files": _gold_files(row), "f2p": row["FAIL_TO_PASS"]},
        timeout_s=1800,
    )


def load_tasks(limit: int | None = None) -> list[BenchTask]:
    tasks = []
    for name in SELECTION:
        row = BY_ID.get(name)
        if row is None:
            continue
        tasks.append(build(row))
    if limit:
        tasks = tasks[:limit]
    return tasks