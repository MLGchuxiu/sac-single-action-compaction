"""Turn-level responsiveness audit: does each agent turn answer the user's latest message?

    work/tau2/venv/bin/python -m harness.tau2.judge_turns --tag full4 --arms sac sac-old eocc fha official-llm \
        --out work/tau2/reports/judge_full4.jsonl

For every agent reply in the official trajectories (``results.json`` of each ``<tag>_<arm>_<domain>``
run) an LLM judge sees the dialogue up to and including that turn and labels the turn:

``on_topic``   addresses the user's latest message (answers, acts, or asks for information not yet given);
``lost_info``  right topic, but ignores / contradicts / asks again for information the user already gave;
``off_topic``  does not address the latest message at all — continues an earlier request as if it
               had not been sent (the "user changed the goal, the agent did not follow" symptom);
``unclear``    cannot be decided from the dialogue.

The judge is an independent model (default ``openai/deepseek-v4-pro``, the user simulator's model and
endpoint), called through litellm. Results are cached per (run, simulation, turn) in the output file,
so the command can be re-run and extended while the benchmark runs. Judge calls carry no secrets in
their output; the key comes from ``benchmarks/tau2-bench/.env``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

SIMS = ROOT / "benchmarks" / "tau2-bench" / "data" / "simulations"
REPORTS = ROOT / "work" / "tau2" / "reports"
CONTROL = ("###STOP###", "###TRANSFER###", "###OUT_OF_SCOPE###")
LABELS = ("on_topic", "lost_info", "off_topic", "unclear")

SYSTEM = """You audit customer-service dialogues between a user and an AI agent. You get the dialogue up to and including the agent's latest turn (the tool calls it made in that turn are listed, then its reply). Judge only that latest agent turn against the user's message right before it.

Labels:
- "on_topic": the turn addresses the user's latest message: it answers it, acts on it, or asks for information the user has not given yet.
- "lost_info": the turn is about the right topic but ignores, contradicts, or asks again for information the user already gave earlier in this dialogue (a memory lapse).
- "off_topic": the turn does not address the user's latest message at all: it keeps working on an earlier request, or something else, as if the latest message had not been sent.
- "unclear": the dialogue does not allow a decision.

Be strict: use "lost_info" or "off_topic" only when the dialogue clearly shows it; an agent that explains a policy, refuses a request, or summarizes is still "on_topic" when that is a reasonable answer to the latest message.

Respond with JSON only: {"label": "<one of on_topic, lost_info, off_topic, unclear>", "evidence": "<one short sentence>"}"""


def load_env() -> None:
    for line in (ROOT / "benchmarks" / "tau2-bench" / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(OPENAI_API_KEY|OPENAI_BASE_URL)\s*=\s*(\S+)", line)
        if m:
            import os

            os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


def short(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + " …"


def turns_of(simulation) -> list[dict]:
    """Every agent reply with the dialogue prefix the judge needs."""
    messages = simulation.messages
    transcript: list[str] = []
    turns = []
    pending_calls: list[str] = []
    index = 0
    latest_user = None
    for message in messages:
        name = message.__class__.__name__
        if name == "UserMessage" and not message.is_tool_call():
            content = str(message.content or "").strip()
            if content in CONTROL:
                latest_user = None
                continue
            latest_user = index
            transcript.append(f"USER: {short(message.content, 700)}")
        elif name == "AssistantMessage" and message.is_tool_call():
            for call in message.tool_calls or []:
                pending_calls.append(f"{call.name}({short(json.dumps(call.arguments, ensure_ascii=False), 160)})")
        elif name == "AssistantMessage":
            calls, pending_calls[:] = pending_calls[:], []
            if latest_user is None:
                continue  # the opening greeting, or a reply after a control message
            turns.append({
                "turn": len(turns) + 1,
                "prefix": list(transcript),
                "calls": calls,
                "reply": short(message.content, 1200),
            })
            transcript.append(f"AGENT: {short(message.content, 700)}")
        index += 1
    return turns


def parse_label(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    try:
        data = json.loads(raw)
        label = str(data.get("label", "")).strip().lower()
        evidence = str(data.get("evidence", "")).strip()
    except Exception:  # noqa: BLE001
        m = re.search(r'"label"\s*:\s*"([a-z_]+)"', raw)
        label = m.group(1) if m else ""
        evidence = raw[:200]
    return (label if label in LABELS else "unparsed"), short(evidence, 300)


def judge_one(model: str, item: dict, max_tokens: int = 2000, no_thinking: bool = False) -> dict:
    import litellm

    lines = [f"Dialogue (oldest first):\n"] + item["prefix"]
    if item["calls"]:
        lines.append("AGENT tool calls in the latest turn: " + "; ".join(item["calls"]))
    lines.append(f"AGENT reply (the turn to judge): {item['reply']}")
    # A reasoning judge may spend the whole output budget on hidden reasoning and return no text;
    # the budget therefore leaves room for it, or thinking is switched off in the request body
    # (Qwen-style `enable_thinking`, honoured by the endpoint used in the experiments; ~5× fewer output tokens).
    extra = {"extra_body": {"enable_thinking": False}} if no_thinking else {}
    for attempt in range(3):
        try:
            response = litellm.completion(
                model=model,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}],
                temperature=0.0,
                max_tokens=max_tokens,
                timeout=180,
                num_retries=1,
                **extra,
            )
            label, evidence = parse_label(response.choices[0].message.content or "")
            return {**item, "label": label, "evidence": evidence}
        except Exception as error:  # noqa: BLE001
            if attempt == 2:
                return {**item, "label": "error", "evidence": f"{type(error).__name__}: {str(error)[:160]}"}
    return {**item, "label": "error", "evidence": "unreachable"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--arms", nargs="*", default=["sac", "sac-old", "eocc", "fha", "official-llm"])
    parser.add_argument("--judge", default="openai/qwen3.8-27b")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=2000, help="output budget; must leave room for the judge's hidden reasoning")
    parser.add_argument("--no-thinking", action="store_true", help="send `enable_thinking: false` in the body (Qwen-style; fast, ~5× fewer output tokens)")
    parser.add_argument("--out", default=None, help="jsonl cache (default work/tau2/reports/judge_<tag>.jsonl)")
    parser.add_argument("--summary", default=None, help="markdown summary")
    parser.add_argument("--limit", type=int, default=0, help="judge at most this many turns per run (0 = all)")
    args = parser.parse_args()
    load_env()
    from tau2.data_model.simulation import Results

    out_path = Path(args.out or REPORTS / f"judge_{args.tag}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[tuple] = set()
    rows: list[dict] = []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows.append(row)
                if row.get("label") not in ("error", "unparsed"):
                    done.add((row["run"], row["simulation_id"], row["turn"]))
    lock = threading.Lock()
    pending: list[dict] = []
    for arm in args.arms:
        for path in sorted(SIMS.glob(f"{args.tag}_{arm}_*")):
            if not (path / "results.json").exists():
                continue
            results = Results.load(path / "results.json")
            count = 0
            for sim in results.simulations:
                for turn in turns_of(sim):
                    key = (path.name, sim.id, turn["turn"])
                    if key in done:
                        continue
                    pending.append({
                        "run": path.name, "arm": arm, "domain": results.info.environment_info.domain_name if results.info and results.info.environment_info else path.name.rsplit("_", 1)[1],
                        "task_id": sim.task_id, "trial": sim.trial, "simulation_id": sim.id,
                        "reward": sim.reward_info.reward if sim.reward_info else None,
                        "turn": turn["turn"], "user_turns": turn["turn"], "prefix": turn["prefix"], "calls": turn["calls"], "reply": turn["reply"],
                    })
                    count += 1
                    if args.limit and count >= args.limit:
                        break
                if args.limit and count >= args.limit:
                    break
    print(f"{len(done)} turns already judged, {len(pending)} to judge", file=sys.stderr)
    import litellm

    litellm.drop_params = True
    with ThreadPoolExecutor(max_workers=args.workers) as pool, out_path.open("a", encoding="utf-8") as handle:
        futures = [pool.submit(judge_one, args.judge, item, args.max_tokens, args.no_thinking) for item in pending]
        for number, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            row = {key: value for key, value in row.items() if key not in ("prefix",)}
            with lock:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                rows.append(row)
            if number % 100 == 0:
                print(f"  {number}/{len(pending)}", file=sys.stderr)

    # ---- summary ------------------------------------------------------------------------------
    lines = ["| domain | arm | turns judged | on_topic | lost_info | off_topic | unclear/error |", "|---|---|---|---|---|---|---|"]
    cells: dict[tuple, dict] = {}
    for row in rows:
        if row.get("label") in ("error", "unparsed"):
            continue
        key = (row["domain"], row["arm"])
        cell = cells.setdefault(key, {label: 0 for label in LABELS})
        cell[row["label"]] += 1
    domains = sorted({key[0] for key in cells})
    for domain in domains + [None]:
        for arm in args.arms:
            keys = [key for key in cells if key[1] == arm and (domain is None or key[0] == domain)]
            if not keys:
                continue
            total = {label: sum(cells[key][label] for key in keys) for label in LABELS}
            n = sum(total.values())
            share = lambda label: f"{total[label]} ({100 * total[label] / n:.1f}%)"  # noqa: E731
            lines.append("| " + " | ".join(str(x) for x in (
                "**all**" if domain is None else domain, arm, n,
                share("on_topic"), share("lost_info"), share("off_topic"), share("unclear"))) + " |")
    report = "\n".join(lines)
    print(report)
    if args.summary:
        Path(args.summary).write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
