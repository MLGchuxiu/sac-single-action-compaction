#!/usr/bin/env python3
"""Real-session fidelity probe.

For each of the heaviest sessions on this machine, fold a window of real steps
into a semantic state with a given template (exactly as SAC would), then ask the
model — with only that state in context — to recall the exact file paths and
commands it handled. Score = fraction of facts recovered verbatim.

This is the paper's §3.1 concern measured on real data: compaction is lossy, so
what survives?

Usage:
    python3 harness/real_probe.py --templates v1,v5 --steps 12 --out results/real
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sac.agent import RunLedger  # noqa: E402
from sac.llm import Client  # noqa: E402
from sac.prompts import INITIAL_STATE, SYSTEM_PROMPT_COMPACT, state_instruction  # noqa: E402
from sac.realsession import (  # noqa: E402
    RECALL_QUESTIONS,
    Step,
    extract_facts,
    load_session,
    parse_recall,
    recall_score,
)
from sac.tokens import estimate_text  # noqa: E402


def pick_window(steps: list[Step], size: int) -> tuple[int, int]:
    """The `size`-step window with the most content (stress the state)."""
    if len(steps) <= size:
        return 0, len(steps)
    best, best_start = -1, 0
    for start in range(0, len(steps) - size):
        weight = sum(len(r) + len(c.get("arguments", "")) for step in steps[start:start + size]
                     for r in step.results for c in step.calls)
        if weight > best:
            best, best_start = weight, start
    return best_start, best_start + size


LEDGER_VERSIONS = {"v4", "v5", "v6", "v7", "v8", "v9"}


def fold_state(client: Client, model: str, session, window: list[Step], version: str,
               budget: int = 2048) -> tuple[str, int, int]:
    """Run SAC's state-update loop over a real window, ledger included.

    Mirrors the harness: for ledger versions the machine-maintained command/file
    log is prepended to every state, so the probe measures the *system* rather
    than just the prompt text.
    """
    state = INITIAL_STATE
    calls = 0
    ledger = RunLedger()
    for step in window:
        for call in step.calls:
            try:
                arguments = json.loads(call.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            ledger.observe(call.get("name") or "", arguments, "")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_COMPACT},
            {"role": "user", "content": f"<task>\n{session.task_text[:2000]}\n</task>\n<semantic_state>\n{state}\n</semantic_state>"},
        ] + step.messages()
        messages.append({"role": "user", "content": state_instruction(version, budget)})
        completion = client.complete_text(model=model, messages=messages,
                                          max_tokens=min(4096, max(2048, budget * 2)),
                                          purpose="state")
        calls += 1
        text = completion.content.strip()
        if text:
            state = text[: int(budget * 3.4)]
            if version in LEDGER_VERSIONS:
                full = version in ("v8", "v9")
                state = ledger.render(2400 if full else 1600, full_commands=full) + "\n\n" + state
    return state, calls, int(estimate_text(state))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--templates", default="v1")
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--models", default="deepseek-v4.1-flash")
    parser.add_argument("--out", default="results/real")
    parser.add_argument("--label", default="")
    parser.add_argument("--workers", type=int, default=1, help="accepted for CLI parity; probes run sequentially")
    args = parser.parse_args()

    index = json.load(open(Path(__file__).resolve().parent.parent / "results" / "session_index.json"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / f"real_{args.label or stamp}.jsonl"
    client = Client()

    jobs = []
    for model in [item.strip() for item in args.models.split(",") if item.strip()]:
        for rank, row in enumerate(index[: args.sessions], 1):
            session = load_session(os.path.expanduser("~/.dsh/sessions/") + row["path"])
            start, end = pick_window(session.steps, args.steps)
            window = session.steps[start:end]
            facts = extract_facts(window)
            for version in [item.strip() for item in args.templates.split(",") if item.strip()]:
                jobs.append((model, rank, session, window, facts, version, start, end))

    def run_job(job):
        model, rank, session, window, facts, version, start, end = job
        if True:
            if True:
                state, calls, state_tokens = fold_state(client, model, session, window, version)
                probe_messages = [
                    {"role": "system", "content": "You resume work from a compacted semantic state."},
                    {"role": "user", "content": f"<semantic_state>\n{state}\n</semantic_state>\n\n{RECALL_QUESTIONS}"},
                ]
                answer = client.complete_text(model=model, messages=probe_messages,
                                              max_tokens=2048, purpose="probe")
                recalled = parse_recall(answer.content)
                path_score = recall_score(recalled["paths"], facts["paths"]) if facts["paths"] else None
                command_score = recall_score(recalled["commands"], facts["commands"]) if facts["commands"] else None
                measured = [score for score in (path_score, command_score) if score is not None]
                overall = sum(measured) / len(measured) if measured else 0.0
                record = {
                    "key": f"real|{model}|{version}|{session.session_id}|{rank}",
                    "kind": "real_recall",
                    "model": model,
                    "template": version,
                    "session": session.session_id,
                    "rank": rank,
                    "cwd": session.cwd,
                    "window": [start, end],
                    "steps": len(window),
                    "facts_paths": len(facts["paths"]),
                    "facts_commands": len(facts["commands"]),
                    "recalled_paths": len(recalled["paths"]),
                    "recalled_commands": len(recalled["commands"]),
                    "path_recall": None if path_score is None else round(path_score, 3),
                    "command_recall": None if command_score is None else round(command_score, 3),
                    "recall": round(overall, 3),
                    "state_tokens": state_tokens,
                    "state_calls": calls,
                    "state": state[:2000],
                    "answer": answer.content[:2000],
                    "run_at": stamp,
                }
                with result_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"[real] {model} {version} #{rank} {session.cwd.split('/')[-1][:24]} "
                      f"paths {path_score if path_score is None else f'{path_score:.2f}'} "
                      f"cmds {command_score if command_score is None else f'{command_score:.2f}'} "
                      f"state={state_tokens}tok", flush=True)
                return record

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for _ in as_completed([pool.submit(run_job, job) for job in jobs]):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())