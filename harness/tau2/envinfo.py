"""Offline inventory of the 184-task golden set and the three domains (no model is called).

    work/tau2/venv/bin/python -m harness.tau2.envinfo
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The golden set, as in the NVIDIA paper: retail base + airline base + telecom small.
GOLDEN: dict[str, str] = {"airline": "base", "retail": "base", "telecom": "small"}


def inventory() -> dict:
    from tau2.runner import build_environment, get_tasks

    out: dict = {"domains": {}, "total": 0}
    for domain, split in GOLDEN.items():
        tasks = get_tasks(domain, task_split_name=split)
        env = build_environment(domain)
        tools = env.get_tools()
        try:
            user_tools = env.get_user_tools()
        except Exception:  # noqa: BLE001 — domains without user tools
            user_tools = []
        with_user_tools = sum(1 for task in tasks if task.user_tools)
        criteria = {"actions": 0, "communicate_info": 0, "nl_assertions": 0, "env_assertions": 0}
        for task in tasks:
            ec = task.evaluation_criteria
            if ec is None:
                continue
            criteria["actions"] += bool(ec.actions)
            criteria["communicate_info"] += bool(ec.communicate_info)
            criteria["nl_assertions"] += bool(ec.nl_assertions)
            criteria["env_assertions"] += bool(ec.env_assertions)
        out["domains"][domain] = {
            "split": split,
            "tasks": len(tasks),
            "policy_chars": len(env.get_policy()),
            "tools": [tool.name for tool in tools],
            "tool_schema_chars": sum(len(json.dumps(tool.openai_schema)) for tool in tools),
            "user_tools": [tool.name for tool in user_tools],
            "tasks_with_user_tools": with_user_tools,
            "tasks_with_criteria": criteria,
        }
        out["total"] += len(tasks)
    return out


def main() -> int:
    info = inventory()
    print(json.dumps(info, indent=1, ensure_ascii=False))
    print(f"golden set total: {info['total']} tasks (expected 184)", file=sys.stderr)
    return 0 if info["total"] == 184 else 1


if __name__ == "__main__":
    raise SystemExit(main())
