"""Offline dry run of the whole τ² ↔ DSH pipeline. No real model is called and no token is spent.

    work/tau2/venv/bin/python -m harness.tau2.selftest_offline             # stub DSH executable
    work/tau2/venv/bin/python -m harness.tau2.selftest_offline --with-dsh  # + the real DSH runtime, fake LLM

Part 1 — stub
    A stand-in for the ``dsh`` executable drives the bridge like the real one (one tool call, then a
    final reply), the official orchestrator runs the official environment and evaluator, and a scripted
    user plays two turns. This checks the adapter: agent registration, the ``build_orchestrator`` seam,
    bridge execution through ``orchestrator._execute_tool_calls``, the official trajectory format, and
    that evaluation runs on it.

Part 2 — real DSH, fake LLM (``--with-dsh``)
    The same simulation with the real DSH runtime, once per compacting arm (``sac`` = plugin 1.2.0,
    ``sac-old`` = plugin 1.1.0, ``eocc`` = 1.2.0 with the paper's threshold compaction at a tiny
    threshold so the path really runs): a generated profile (SAC bundle + bridge plugin, hermetic tool
    set) routes to a local fake OpenAI-compatible endpoint that asks for three tool calls and then
    replies. This checks what the DSH harness really puts on the wire (the τ² policy in the system
    prompt, exactly the τ² tools, tool results reaching the model), session resume across user turns,
    and each arm's fold behaviour: 1.2.0 never folds a user message; the old plugin and EoCC do.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.tau2.dsh_agent import AGENT_NAME, bind_agent_to_orchestrator, register_agent  # noqa: E402
from harness.tau2.make_profiles import (  # noqa: E402
    DEFAULT_DSH_HOME,
    EOCC_NOTE,
    EOCC_PAPER_NOTE,
    EOCC_PAPER_WORDING,
    OLD_SAC,
    SAC,
    derive_plugin,
    eocc_overrides,
    write_profile,
)
from tau2.evaluator.evaluator import EvaluationType  # noqa: E402

USER_LINES = ["I want to fly from San Francisco to New York.", "Thanks. Which airports do you serve?", "###STOP###"]
TASK_ID = "0"

STUB_DSH = r'''#!{python}
"""Stand-in for the dsh CLI: reads the task on stdin, makes one bridge tool call, prints the JSON stream."""
import json, os, sys, urllib.request
args = sys.argv[1:]
text = sys.stdin.read()
resumed = "--session-id" in args
session = args[args.index("--session-id") + 1] if resumed else "session-stub-dry-run"
events = [{{"type": "session", "sessionId": session, "cwd": os.getcwd()}}]
if not resumed:
    tools = json.load(open(os.environ["TAU2_BRIDGE_TOOLS"], encoding="utf-8"))
    tool = next((t for t in tools if t["name"] == "list_all_airports"), tools[0])
    request = urllib.request.Request(
        os.environ["TAU2_BRIDGE_URL"].rstrip("/") + "/call",
        data=json.dumps({{"id": "call_stub_1", "name": tool["name"], "arguments": {{}}}}).encode(),
        headers={{"content-type": "application/json", "authorization": "Bearer " + os.environ["TAU2_BRIDGE_TOKEN"]}},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    events.append({{"type": "tool_call", "name": tool["name"], "arguments": {{}}, "result": str(payload.get("content"))[:80]}})
    events.append({{"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {{"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}}})
events.append({{"type": "status", "phase": "step_end", "turn": 1, "step": 2, "usage": {{"inputTokens": 20, "outputTokens": 5, "totalTokens": 25}}}})
events.append({{"type": "final", "sessionId": session, "reason": {{"kind": "completed"}}, "text": "STUB REPLY (turn resumed)" if resumed else "STUB REPLY"}})
for event in events:
    print(json.dumps(event))
'''


# ----- a scripted stand-in for the LLM user simulator ----------------------------------------


def register_scripted_user() -> None:
    from tau2.registry import registry
    from tau2.user.user_simulator import UserSimulator
    from tau2.user.user_simulator_base import UserState
    from tau2.data_model.message import AssistantMessage, UserMessage

    class _ScriptedUser(UserSimulator):
        """Says ``llm_args['script']`` line by line, one per turn; records what the agent said."""

        def __init__(self, llm, instructions=None, tools=None, llm_args=None, persona_config=None):
            super().__init__(llm=llm, instructions=instructions, tools=tools, llm_args=llm_args, persona_config=persona_config)
            self.script = list((llm_args or {}).get("script") or [])
            self.heard: list[str] = []

        def get_init_state(self, message_history=None) -> UserState:
            return UserState(messages=[], system_messages=[])

        def generate_next_message(self, message, state):
            self.heard.append(message.content if isinstance(message, AssistantMessage) else str(message))
            line = self.script[min(len(self.heard), len(self.script)) - 1]
            return UserMessage(role="user", content=line), state

    if "scripted_user" not in registry.get_users():
        registry.register_user(_ScriptedUser, "scripted_user")


# ----- helpers ----------------------------------------------------------------------------------


def run_case(label: str, llm_args: dict, runs_dir: Path) -> object:
    """One airline task through the official batch runner (checkpoint file included), offline."""
    from tau2.data_model.simulation import TextRunConfig
    from tau2.runner import get_tasks, run_tasks

    tasks = get_tasks("airline", task_ids=[TASK_ID])
    config = TextRunConfig(
        domain="airline",
        agent=AGENT_NAME,
        user="scripted_user",
        llm_agent="dry-run",
        llm_args_agent=llm_args,
        llm_user="scripted",
        llm_args_user={"script": USER_LINES},
        task_ids=[TASK_ID],
        seed=300,
        num_trials=1,
        max_concurrency=1,
        max_retries=1,
    )
    results = run_tasks(
        config,
        tasks,
        save_path=runs_dir / label / "results.json",
        save_dir=runs_dir / label,
        evaluation_type=EvaluationType.ALL,
        console_display=False,
    )
    return results.simulations[0]


def find_session_log(dsh_home: Path, session_id: str) -> Path | None:
    for path in (dsh_home / "sessions").rglob("session.v4.jsonl*"):
        if path.parent.name == session_id:
            return path
    return None


def read_session_events(path: Path) -> list[dict]:
    if path.suffix == ".zstd":
        raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    else:
        raw = path.read_bytes()
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def that(self, condition: bool, what: str) -> bool:
        if condition:
            self.passed += 1
            print(f"  ok    {what}")
        else:
            self.failed += 1
            print(f"  FAIL  {what}")
        return bool(condition)


# ----- part 1 -----------------------------------------------------------------------------------


def part_stub(runs_dir: Path, check: Check) -> None:
    from tau2.data_model.message import AssistantMessage, ToolMessage

    print("\n[1/2] stub dsh executable + official orchestrator + evaluator")
    stub = runs_dir / "stub" / "dsh"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(STUB_DSH.format(python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    result = run_case("stub", {"profile": "none", "dsh_bin": str(stub), "runs_dir": str(runs_dir / "stub")}, runs_dir)
    messages = result.messages
    check.that(result.termination_reason == "user_stop", f"termination reason is user_stop (got {result.termination_reason})")
    tool_steps = [(i, m) for i, m in enumerate(messages) if isinstance(m, AssistantMessage) and m.is_tool_call()]
    check.that(len(tool_steps) == 1, f"exactly one tool-call message in the trajectory (got {len(tool_steps)})")
    if tool_steps:
        i, call_message = tool_steps[0]
        call = call_message.tool_calls[0]
        follow = messages[i + 1]
        check.that(isinstance(follow, ToolMessage) and follow.id == call.id, "the tool message follows immediately with the same id")
        check.that(isinstance(follow, ToolMessage) and not follow.error, f"the official environment executed it without error (content: {str(follow.content)[:60]!r})")
        check.that(call.name == "list_all_airports", f"the call went to the tool the stub asked for ({call.name})")
    replies = [m.content for m in messages if isinstance(m, AssistantMessage) and not m.is_tool_call()]
    check.that(replies[1:] == ["STUB REPLY", "STUB REPLY (turn resumed)"], f"the agent's replies are the stub's final texts; the user's stop token ends the dialogue without a reply ({replies[1:]})")
    check.that(replies[0] == "Hi! How can I help you today?", "the official greeting opens the trajectory")
    users = [m.content for m in messages if m.__class__.__name__ == "UserMessage"]
    check.that(users == USER_LINES, f"the user's three turns are in the trajectory ({users})")
    check.that(result.reward_info is not None and result.reward_info.reward in (0.0, 1.0), f"evaluation produced a reward ({result.reward_info.reward if result.reward_info else None})")
    info = (result.reward_info.info if result.reward_info else {}) or {}
    check.that(sorted(info) == ["action", "communicate", "env", "nl"], f"the official evaluators ran on the reconstructed trajectory (keys: {sorted(info)})")
    print("  reward info:", json.dumps(result.reward_info.model_dump() if result.reward_info else None, ensure_ascii=False, default=str)[:700])
    record = next(m.raw_data["dsh"] for m in messages if isinstance(m, AssistantMessage) and m.raw_data and "dsh" in (m.raw_data or {}))
    check.that(record["usage"].get("totalTokens") == 40, f"DSH usage from the JSON stream is carried into the message ({record['usage']})")


# ----- part 2 -----------------------------------------------------------------------------------

# The compacting arms, each against the fake LLM. `folded_user` is how many user messages the
# arm's folds are *expected* to swallow in this three-turn dialogue: 0 is the 1.2.0 invariant;
# ">=1" is the documented behaviour of the old plugin (the bug under test) and of EoCC, which by
# the paper's definition restarts from the original task and the compacted state only.
SELFTEST_ARMS = {
    "sac": dict(sac_path=SAC, overrides=None, folded_user=0, min_folds=1),
    "sac-old": dict(sac_path=OLD_SAC, overrides=None, folded_user=">=1", min_folds=1),
    # A threshold the tiny dialogue reaches, so the threshold path really runs offline. `eocc` is the
    # comparison arm (the paper's compaction wording); `eocc-anchored` the reference with 1.2.0's.
    "eocc": dict(overrides=eocc_overrides(150), code=EOCC_PAPER_WORDING, note=EOCC_PAPER_NOTE, folded_user=">=1", min_folds=2),
    "eocc-anchored": dict(overrides=eocc_overrides(150), folded_user=">=1", min_folds=2),
}


def run_arm(runs_dir: Path, dsh_home: Path, arm: str, spec: dict, check: "Check") -> None:
    from harness.tau2.fake_llm import FakeLlm
    from tau2.data_model.message import AssistantMessage

    sac_path = spec.get("sac_path")
    if spec.get("overrides"):
        sac_path = derive_plugin(f"{arm}-selftest-thr150", spec["overrides"], spec.get("note", EOCC_NOTE), code_replacements=spec.get("code"))
    out = runs_dir / f"dsh-{arm}"
    print(f"\n[{arm}] real DSH runtime + fake LLM — plugin {sac_path}")
    fake = FakeLlm(
        calls=[
            {"name": "list_all_airports", "arguments": {}},
            {"name": "search_direct_flight", "arguments": {"origin": "SFO", "destination": "JFK", "date": "2026-03-01"}},
            {"name": "list_all_airports", "arguments": {}},
        ],
        final_text="You are all set!",
        log_path=out / "fake_llm_requests.jsonl",
    ).start()
    os.environ["TAU2_FAKE_KEY"] = "fake-key-for-offline-dry-run"
    try:
        profile = write_profile(
            f"tau2-selftest-{arm}",
            dsh_home=dsh_home,
            sac=True,
            sac_path=sac_path,
            provider="tau2-fake",
            model="fake-model",
            base_url=fake.base_url,
            api_key_env="TAU2_FAKE_KEY",
            max_tokens=4096,
        )
        result = run_case(f"dsh-{arm}", {"profile": profile.name, "runs_dir": str(out), "turn_timeout": 600}, runs_dir)
    finally:
        fake.stop()
    messages = result.messages
    replies = [m.content for m in messages if isinstance(m, AssistantMessage) and not m.is_tool_call()]
    print(f"  agent replies: {replies}")
    user_turns = sum(1 for line in USER_LINES if not line.startswith("###"))
    check.that(result.termination_reason == "user_stop", f"{arm}: termination reason is user_stop (got {result.termination_reason})")
    check.that(replies[1:] == ["You are all set!"] * user_turns, f"{arm}: each of the {user_turns} DSH turns returned the fake model's final text")
    tool_messages = [m for m in messages if m.__class__.__name__ == "ToolMessage"]
    check.that(len(tool_messages) >= 3, f"{arm}: tool calls were executed by the official environment ({len(tool_messages)} tool messages)")
    check.that(all(not m.error for m in tool_messages), f"{arm}: none of them is an error")

    requests = [json.loads(line) for line in (out / "fake_llm_requests.jsonl").read_text(encoding="utf-8").splitlines()]
    main_requests = [r for r in requests if r.get("kind") == "main"]
    summary_requests = [r for r in requests if r.get("kind") == "summary"]
    print(f"  LLM requests: {len(main_requests)} agent steps, {len(summary_requests)} state updates")
    check.that(0 < len(main_requests) <= 24, f"{arm}: the fake LLM received a bounded number of agent requests ({len(main_requests)})")
    check.that(len(summary_requests) >= spec["min_folds"], f"{arm}: the plugin issued state updates ({len(summary_requests)})")
    if main_requests:
        first = main_requests[0]
        system = Path(f"{out / 'fake_llm_requests.jsonl'}.first-system.txt").read_text(encoding="utf-8")
        check.that("<policy>" in system and "You are a customer service agent that helps the user" in system, f"{arm}: the τ² agent instruction and policy are in the DSH system prompt")
        check.that("You are in plan mode" not in system, f"{arm}: the plan-mode section does not reach the model")
        if arm == "sac":
            from tau2.runner import build_environment

            expected = sorted(tool.name for tool in build_environment("airline").get_tools())
            check.that(first["tool_names"] == expected, f"exactly the {len(expected)} τ² airline tools are exposed (got {len(first['tool_names'])})")
        check.that(first["roles"][:2] == ["system", "user"], f"{arm}: the first request is system + the user's message ({first['roles']})")
        if arm.startswith("eocc"):
            # At a threshold this tiny every observation is folded before the model sees it raw
            # (at the real threshold raw observations stay until the surface reaches it).
            check.that(not any(r["tool_results"] for r in main_requests), f"{arm}: at a tiny threshold each observation is folded before the next request")
        else:
            check.that(any(r["tool_results"] for r in main_requests), f"{arm}: tool results reached the model in a later request")
        check.that(len({r["system_sha"] for r in main_requests}) == 1, f"{arm}: the system prompt is identical in every agent request")
        check.that(all(r["system_chars"] == 0 for r in summary_requests), f"{arm}: state updates go out without the system prompt (lean mode)")

    record = next(m.raw_data["dsh"] for m in messages if isinstance(m, AssistantMessage) and m.raw_data and "dsh" in (m.raw_data or {}))
    log = find_session_log(dsh_home, record["session_id"])
    check.that(log is not None, f"{arm}: the DSH session log was persisted ({record['session_id']})")
    if log is not None:
        plain = out / "session.v4.jsonl"
        if log.suffix == ".zstd":
            plain.write_bytes(subprocess.run(["zstd", "-dc", str(log)], capture_output=True, check=True).stdout)
        else:
            plain.write_bytes(log.read_bytes())
        (out / "SESSION_LOG.txt").write_text(f"{log}\n", encoding="utf-8")
        sys.path.insert(0, str(ROOT / "harness" / "session_replay"))
        from saclog import Session

        session = Session(str(plain))
        user_msgs = [seq for seq in session.order if session.kind(seq) == "user"]
        check.that(len(user_msgs) == user_turns, f"{arm}: {user_turns} user messages in one DSH session ({len(user_msgs)})")
        folds = [seq for seq in session.order if session.kind(seq) == "compact-checkpoint"]
        check.that(len(folds) >= spec["min_folds"], f"{arm}: folded at least {spec['min_folds']} time(s) ({len(folds)} fold(s))")
        swallowed = []
        for seq in folds:
            shadowed = session.events[seq].get("sourceEventSeqs") or []
            swallowed += [seq2 for seq2 in shadowed if session.kind(seq2) == "user"]
        expect = spec["folded_user"]
        if expect == 0:
            check.that(not swallowed, f"{arm}: no fold folded a user message (folded: {swallowed})")
        else:
            check.that(bool(swallowed), f"{arm}: as documented, a fold swallowed a user message (folded: {swallowed})")
        layout = [(session.kind(seq) or session.events[seq]["type"]) for seq in session.surface]
        check.that(layout[1] == "user" and session.kind(session.surface[1]) == "user", f"{arm}: the first user message (the task anchor) is still on the surface ({layout})")
        if arm == "sac":
            # system · 请求1 · 状态1 · 回复1 · 请求2 · …
            check.that(layout[:5] == ["system/message", "user", "compact-checkpoint", "assistant/message", "user"], f"the surface reads system · request 1 · state 1 · reply 1 · request 2 ({layout[:5]})")
            check.that(layout.count("user") == user_turns, f"every user message is on the surface verbatim ({layout})")
        if arm.startswith("eocc"):
            # EoCC: nothing but the task, the compacted state, harness bookkeeping and the reply
            # in progress survives a fold — no raw tool call or observation.
            survivors = {"system/message", "user", "compact-checkpoint", "assistant/message", "runtime-context", "time-context"}
            check.that(all(kind in survivors for kind in layout), f"{arm}: no raw tool call or result survives on the surface ({layout})")
        failed = [seq for seq in session.order if session.events[seq]["type"] == "compaction/end" and "error" in session.events[seq]["data"]]
        check.that(not failed, f"{arm}: no state update failed ({len(failed)})")
        steps = [seq for seq in session.order if session.events[seq]["type"] == "step/start"]
        turns = json.loads((Path(record["task_dir"]) / "tau2_turns.json").read_text(encoding="utf-8"))
        check.that(len(steps) == sum(turn["steps"] for turn in turns), f"{arm}: step count in the DSH log matches the JSON stream")
        check.that(all(turn.get("exit_code") == 0 for turn in turns), f"{arm}: every DSH turn exited 0 ({[turn.get('exit_code') for turn in turns]})")
        check.that(sum(turn["usage"].get("inputTokens", 0) for turn in turns) > 0, f"{arm}: DSH reported usage ({[turn['usage'] for turn in turns]})")

    from harness.tau2.analyze import summarize

    summary = summarize(out / "results.json", dsh_home)
    check.that(summary["dsh"]["session_logs_found"] == 1, f"{arm}: analyze.py reads the results file and the DSH session log")
    print("  analysis:", json.dumps({key: summary[key] for key in ("official", "dsh", "sac")}, ensure_ascii=False)[:900])


def part_dsh(runs_dir: Path, dsh_home: Path, check: Check, arms: list[str] | None = None) -> None:
    print("\n[2/2] real DSH runtime + fake LLM (three user turns)")
    for arm in arms or list(SELFTEST_ARMS):
        run_arm(runs_dir, dsh_home, arm, SELFTEST_ARMS[arm], check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--with-dsh", action="store_true", help="also run the real DSH runtime against a fake LLM")
    parser.add_argument("--dsh-home", default=os.environ.get("TAU2_DSH_HOME") or str(DEFAULT_DSH_HOME))
    parser.add_argument("--runs-dir", default=None, help="default: a fresh directory under work/tau2/selftest")
    parser.add_argument("--sac-path", default=None, help="relocate the new-SAC arm's plugin (default: plugins/dsh-sac-compact)")
    parser.add_argument("--arms", nargs="*", default=None, help=f"subset of {list(SELFTEST_ARMS)} (default: all; only with --with-dsh)")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir) if args.runs_dir else Path(tempfile.mkdtemp(prefix="run_", dir=ROOT / "work" / "tau2" / "selftest"))
    runs_dir.mkdir(parents=True, exist_ok=True)
    if args.sac_path:
        SELFTEST_ARMS["sac"]["sac_path"] = Path(args.sac_path).resolve()
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    register_agent(AGENT_NAME)
    register_scripted_user()
    bind_agent_to_orchestrator()
    check = Check()
    part_stub(runs_dir, check)
    if args.with_dsh:
        part_dsh(runs_dir, Path(args.dsh_home), check, args.arms)
    else:
        print("\n[2/2] skipped (pass --with-dsh to run the real DSH runtime against the fake LLM)")
    print(f"\n{check.passed} passed, {check.failed} failed — artifacts in {runs_dir}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
