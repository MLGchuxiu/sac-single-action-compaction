"""List the failures of a run (or of several) with the evaluator's reasons, for reading.

    work/tau2/venv/bin/python -m harness.tau2.failures benchmarks/tau2-bench/data/simulations/full4_sac_airline [...]

Prints one block per failed simulation: task id, termination, reward breakdown, the evaluator's
notes (DB diff summary, unmet NL assertions), the user turns, the agent's replies (truncated) and
the tool calls it made. ``--full`` prints whole replies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

from harness.tau2.analyze import _resolve  # noqa: E402

CONTROL = ("###STOP###", "###TRANSFER###", "###OUT_OF_SCOPE###")


def short(value, limit):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + " …"


def reasons(reward_info) -> list[str]:
    info = (reward_info.info if reward_info else {}) or {}
    out = []
    breakdown = (reward_info.reward_breakdown if reward_info else {}) or {}
    if breakdown:
        out.append("reward_breakdown=" + json.dumps({str(k): v for k, v in breakdown.items()}, ensure_ascii=False))
    db = info.get("db_check") if isinstance(info.get("db_check"), dict) else None
    if db and db.get("db_match") is False:
        out.append("DB mismatch: " + short(json.dumps(db, ensure_ascii=False), 500))
    for check in info.get("nl_assertions") or []:
        if not getattr(check, "met", True):
            out.append(f"NL unmet: {short(getattr(check, 'nl_assertion', check), 200)} — {short(getattr(check, 'justification', ''), 240)}")
    for check in info.get("communicate_checks") or []:
        if not getattr(check, "met", True):
            out.append(f"COMMUNICATE unmet: {short(getattr(check, 'message', check), 200)}")
    for check in info.get("action_checks") or []:
        if not getattr(check, "met", True):
            out.append(f"ACTION unmet: {short(getattr(check, 'action_id', check), 120)} — {short(getattr(check, 'justification', ''), 200)}")
    for check in info.get("env_assertions") or []:
        if not getattr(check, "met", True):
            out.append(f"ENV unmet: {short(check, 200)}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="+")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--task", nargs="*", default=None, help="only these task ids (pass or fail)")
    parser.add_argument("--max", type=int, default=40, help="at most this many failed simulations")
    args = parser.parse_args()
    from tau2.data_model.simulation import Results

    limit = 100000 if args.full else 420
    shown = 0
    for raw in args.results:
        path = Path(raw).resolve()
        results = Results.load(_resolve(path))
        sims = results.simulations
        failed = [sim for sim in sims if (sim.reward_info is None or sim.reward_info.reward != 1.0)]
        print(f"\n################ {path.name}: {len(sims) - len(failed)}/{len(sims)} passed, {len(failed)} failed")
        for sim in sims:
            is_fail = sim.reward_info is None or sim.reward_info.reward != 1.0
            if args.task:
                if str(sim.task_id) not in args.task:
                    continue
            elif not is_fail or shown >= args.max:
                continue
            shown += is_fail
            print(f"\n=== task {sim.task_id} (trial {sim.trial}) | {'FAIL' if is_fail else 'pass'} | {sim.termination_reason} | {round(sim.duration or 0)} s")
            for line in reasons(sim.reward_info):
                print("   ", line)
            for message in sim.messages:
                name = message.__class__.__name__
                if name == "UserMessage" and not message.is_tool_call():
                    content = str(message.content or "").strip()
                    if content not in CONTROL:
                        print(f"  USER : {short(content, 260 if not args.full else 4000)}")
                    elif content != "###STOP###":
                        print(f"  USER : {content}")
                elif name == "UserMessage" and message.is_tool_call():
                    print("  USER-TOOLS: " + "; ".join(f"{c.name}({short(json.dumps(c.arguments, ensure_ascii=False), 100)})" for c in message.tool_calls or []))
                elif name == "AssistantMessage" and message.is_tool_call():
                    print("  AGENT-TOOLS: " + "; ".join(f"{c.name}({short(json.dumps(c.arguments, ensure_ascii=False), 140)})" for c in message.tool_calls or []))
                elif name == "AssistantMessage":
                    print(f"  AGENT: {short(message.content, limit)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
