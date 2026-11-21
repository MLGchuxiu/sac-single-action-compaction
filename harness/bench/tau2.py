"""τ²-bench adapter (static, single-turn policy evaluation).

τ²-bench is interactive: an LLM user simulator talks to the agent while the
domain tools mutate a database.  Running the official orchestrator needs the
full package plus a simulator model per task, so this adapter uses a *static*
variant that keeps the parts SAC can affect in a single session: the long domain
policy, the tool catalogue, and the user request.  The agent must emit the exact
tool-call plan and the final customer message; scoring compares them with the
task's own `evaluation_criteria` (actions + communicate_info).

The limitation is explicit in the report: this measures policy comprehension,
not multi-turn tool use.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path

from .common import ROOT, WORK, BenchTask, token_f1, write_text

TAU = ROOT / "benchmarks" / "tau2-bench"
DOMAINS = TAU / "data" / "tau2" / "domains"


def _tool_catalog(domain: str) -> str:
    path = TAU / "src" / "tau2" / "domains" / domain / "tools.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            nodes.extend(node.body)
        else:
            nodes.append(node)
    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any("is_tool" in ast.unparse(decorator) for decorator in node.decorator_list):
            continue
        args = [arg.arg for arg in node.args.args if arg.arg not in ("self", "kwargs")]
        doc = ast.get_docstring(node) or ""
        summary = doc.strip().split("\n\n")[0].replace("\n", " ").strip()
        lines.append(f"- {node.name}({', '.join(args)}): {summary}")
    return "\n".join(lines)


def _policy(domain: str) -> str:
    name = "main_policy.md" if domain == "telecom" else "policy.md"
    return (DOMAINS / domain / name).read_text(encoding="utf-8")


def _prompt(domain: str, task: dict) -> str:
    return (
        f"You are the customer-service agent for the **{domain}** domain.\n\n"
        f"The working directory contains three files:\n"
        f"- `policy.md` — the domain policy you must follow;\n"
        f"- `tools.md` — the catalogue of tools available to you;\n"
        f"- `request.md` — the user's request and what they do / do not know.\n\n"
        f"Read all three, then work out the ordered sequence of tool names you "
        f"would call to handle the request (include authentication / lookup calls "
        f"the policy requires), and the final message you would send the user. "
        f"There is no domain database in this session, so do not try to execute "
        f"the tools.\n\n"
        f"Answer with one compact JSON object and nothing else — tool names only, "
        f"no arguments, and a final_message of at most 80 words:\n"
        f'{{"tool_calls": ["<tool name>", ...], "final_message": "<what you tell the user>"}}\n'
    )


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _args_match(expected: dict, predicted: dict) -> bool:
    for key, want in (expected or {}).items():
        if key not in (predicted or {}):
            return False
        got = predicted[key]
        if isinstance(want, list):
            if not isinstance(got, list) or len(got) != len(want):
                return False
            if any(_norm(a) != _norm(b) for a, b in zip(want, got)):
                return False
        elif _norm(want) != _norm(got):
            return False
    return True


def _lcs_length(a: list[str], b: list[str]) -> int:
    rows, cols = len(a), len(b)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            table[i][j] = (table[i + 1][j + 1] + 1 if a[i] == b[j]
                           else max(table[i + 1][j], table[i][j + 1]))
    return table[0][0]


def _lcs_length(a: list[str], b: list[str]) -> int:
    rows, cols = len(a), len(b)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            table[i][j] = (table[i + 1][j + 1] + 1 if a[i] == b[j]
                           else max(table[i + 1][j], table[i][j + 1]))
    return table[0][0]


def _verify(task: dict):
    criteria = task["evaluation_criteria"] or {}
    expected = criteria.get("actions") or []
    communicate = criteria.get("communicate_info") or []
    expected_names = [_norm(action.get("name")) for action in expected]

    def _name(call) -> str:
        if isinstance(call, str):
            return _norm(call)
        if isinstance(call, dict):
            return _norm(call.get("name"))
        return ""

    def verify(result: dict) -> dict:
        parsed = result.get("parsed")
        if not isinstance(parsed, dict):
            return {"score": 0.0, "detail": "no JSON object in final answer"}
        calls = parsed.get("tool_calls") or []
        predicted_names = [name for name in (_name(call) for call in calls) if name]
        message = parsed.get("final_message") or ""

        matched = _lcs_length(predicted_names, expected_names)
        name_recall = matched / len(expected_names) if expected_names else 1.0
        info_scores = []
        for item in communicate:
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            info_scores.append(1.0 if token_f1(message, text) >= 0.55 else 0.0)
        info_recall = sum(info_scores) / len(info_scores) if info_scores else 1.0
        score = round(0.7 * name_recall + 0.3 * info_recall, 3)
        return {"score": score, "detail": {
            "names": f"{matched}/{len(expected_names)}",
            "info_recall": round(info_recall, 3),
            "predicted": predicted_names,
        }}
    return verify


def build(domain: str, task: dict) -> BenchTask:
    name = f"{domain}-{task['id']}"
    workspace = WORK / "tasks" / "tau2" / name
    if workspace.exists():
        shutil.rmtree(workspace)
    scenario = task["user_scenario"]["instructions"]
    write_text(workspace / "policy.md", _policy(domain))
    write_text(workspace / "tools.md", "# Available tools\n\n" + _tool_catalog(domain) + "\n")
    write_text(workspace / "request.md",
               f"# The user\n{scenario.get('reason_for_call')}\n\n"
               f"Known information: {scenario.get('known_info') or '(none)'}\n\n"
               f"Unknown / must ask: {scenario.get('unknown_info') or '(none)'}\n")
    return BenchTask(
        bench="tau2",
        task_id=name,
        prompt=_prompt(domain, task),
        workspace=workspace,
        verify=_verify(task),
        meta={"domain": domain, "actions": len((task["evaluation_criteria"] or {}).get("actions") or []),
              "communicate": len((task["evaluation_criteria"] or {}).get("communicate_info") or []),
              "criteria": task["evaluation_criteria"]},
        timeout_s=600,
    )


def _pick(domain: str, count: int) -> list[dict]:
    name = "tasks_small.json" if domain == "telecom" else "tasks.json"
    data = json.loads((DOMAINS / domain / name).read_text(encoding="utf-8"))
    tasks = data if isinstance(data, list) else data.get("tasks", data)
    preferred, fallback = [], []
    for task in tasks:
        criteria = task.get("evaluation_criteria") or {}
        actions = criteria.get("actions") or []
        if not 1 <= len(actions) <= 6:
            continue
        if criteria.get("communicate_info") or criteria.get("nl_assertions"):
            preferred.append(task)
        else:
            fallback.append(task)
    chosen = (preferred + fallback)[:count]
    return chosen


def load_tasks(limit: int | None = None) -> list[BenchTask]:
    tasks = []
    for domain, count in (("retail", 3), ("airline", 1)):
        for task in _pick(domain, count):
            tasks.append(build(domain, task))
    if limit:
        tasks = tasks[:limit]
    return tasks