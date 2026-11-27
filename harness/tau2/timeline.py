"""Print one simulation's timeline: user messages, the agent's replies, and every fold in between.

    work/tau2/venv/bin/python -m harness.tau2.timeline full4_sac_airline 2 [--fold-text]

For reading a session: what the user said, what the agent answered, where the state was rewritten,
which user messages a fold removed from the context, and the Objective line of each state.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

from harness.tau2.session_log import find_session_log, load_session, state_recaps  # noqa: E402
from harness.session_replay.saclog import text_of  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run", help="results directory name under data/simulations")
    parser.add_argument("task", help="task id")
    parser.add_argument("--trial", type=int, default=0)
    parser.add_argument("--dsh-home", default=str(ROOT / "work" / "dshhome"))
    parser.add_argument("--width", type=int, default=200)
    args = parser.parse_args()
    from tau2.data_model.simulation import Results

    results = Results.load(ROOT / "benchmarks" / "tau2-bench" / "data" / "simulations" / args.run / "results.json")
    sim = next(s for s in results.simulations if str(s.task_id) == args.task and s.trial == args.trial)
    session_id = None
    for message in sim.messages:
        raw = getattr(message, "raw_data", None) or {}
        if isinstance(raw, dict) and isinstance(raw.get("dsh"), dict):
            session_id = raw["dsh"].get("session_id")
            break
    print(f"run {args.run} | task {sim.task_id} | reward {sim.reward_info.reward if sim.reward_info else None} | {sim.termination_reason}")
    replies = {t["turn"]: t["text"] for t in (lambda d: __import__("json").loads((Path(d) / "tau2_turns.json").read_text()))(next(m.raw_data["dsh"]["task_dir"] for m in sim.messages if isinstance(getattr(m, "raw_data", None), dict) and "dsh" in (m.raw_data or {})))} if session_id else {}
    log = find_session_log(Path(args.dsh_home).resolve(), session_id) if session_id else None
    if log is None:
        for m in sim.messages:
            print(f"  {m.role.upper():9s}: {' '.join(str(m.content or '').split())[:args.width]}")
        return 0
    session = load_session(log)
    recaps = {r["seq"]: r for r in state_recaps(session)}
    turn = 0
    width = args.width
    for seq in session.order:
        event = session.events[seq]
        kind = session.kind(seq)
        if event["type"] == "turn/start":
            turn = event["data"]["turn"]
        elif event["type"] == "user/message" and kind in ("user", "goal"):
            print(f"  U{turn} USER  : {' '.join(text_of(event).split())[:width]}")
        elif seq in recaps:
            r = recaps[seq]
            swallowed = ", ".join(f"U{session.turn_of(u)}" for u in r["swallowed_user"]) or "-"
            print(f"       · FOLD seq {seq} before turn {session.turn_of(seq)} | removed user msg: {swallowed} | Objective: {r['objective'][:width - 60]}")
        elif event["type"] == "compaction/end" and event["data"].get("error"):
            print(f"       · FOLD REFUSED seq {seq}: {str(event['data']['error'])[:120]}")
    for number, text in replies.items():
        print(f"  A{number} AGENT : {' '.join(str(text).split())[:width]}")
    layout = [(session.kind(q) or session.events[q]["type"]) for q in session.surface]
    print("  final surface:", layout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
