"""State-anchor audit: does each semantic state's `Objective` line describe the user's *latest* request?

    work/tau2/venv/bin/python -m harness.tau2.judge_states --tag full4 --arms sac sac-old eocc \
        --out work/tau2/reports/states_full4.jsonl

For every fold in the DSH session logs of the compacting arms, an LLM judge sees the user messages
the recap was written after (verbatim, numbered, oldest first) and the recap's Objective line, and
says which request that line describes:

``latest``    the user's most recent request at that moment;
``earlier``   an earlier request, as if the latest message had not arrived (the drift seen in the
              logs that motivated plugin 1.2.0);
``mixed``     blends the latest request with earlier ones or with the work in progress;
``unclear``   cannot be decided.

Plugin 1.2.0 also folds *finished* turns, whose state deliberately describes their own (earlier)
request; those folds are skipped (``after_latest`` is False). The judge is called through litellm
(default ``openai/deepseek-v4-pro``, key from ``benchmarks/tau2-bench/.env``); results are cached
per session and fold in the output file.
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

from harness.tau2.dsh_agent import DEFAULT_DSH_HOME  # noqa: E402
from harness.tau2.session_log import find_session_log, load_session, state_recaps  # noqa: E402
from tau2.data_model.simulation import Results  # noqa: E402

SIMS = ROOT / "benchmarks" / "tau2-bench" / "data" / "simulations"
REPORTS = ROOT / "work" / "tau2" / "reports"
LABELS = ("latest", "earlier", "mixed", "unclear")

SYSTEM = """You audit the memory of an AI customer-service agent. The agent's context is compacted into a short written state after the user has sent one or more messages. You get the user's messages so far (verbatim, numbered, oldest first) and the Objective line of the state written right after them.

Decide which request the Objective line describes:
- "latest": the user's most recent message (message N) — what the user wants now.
- "earlier": an earlier message, as if the latest message had not arrived.
- "mixed": a blend of the latest request with earlier requests or with whatever the agent was doing.
- "unclear": cannot be decided.

A later message often continues the same request (for example: the user adds an ID, confirms a detail, or answers a question). In that case an Objective that still describes the ongoing request counts as "latest". Use "earlier" only when the latest message asks for something different and the Objective ignores it.

Respond with JSON only: {"label": "<latest|earlier|mixed|unclear>", "evidence": "<one short sentence>"}"""


def short(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + " …"


def judge_one(model: str, item: dict, max_tokens: int, no_thinking: bool = False) -> dict:
    import litellm

    lines = ["User messages so far (oldest first):"]
    lines += [f"{index}. USER: {short(message['text'], 600)}" for index, message in enumerate(item["user_before"], start=1)]
    lines.append(f"\nObjective line of the state written now: {item['objective']}")
    # With `enable_thinking: false` (Qwen-style) the judge answers without hidden reasoning.
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
            raw = (response.choices[0].message.content or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
            try:
                data = json.loads(raw)
                label, evidence = str(data.get("label", "")).strip().lower(), str(data.get("evidence", "")).strip()
            except Exception:  # noqa: BLE001
                m = re.search(r'"label"\s*:\s*"([a-z_]+)"', raw)
                label, evidence = (m.group(1) if m else ""), raw[:200]
            return {key: value for key, value in {**item, "label": label if label in LABELS else "unparsed", "evidence": short(evidence, 300)}.items() if key != "user_before"}
        except Exception as error:  # noqa: BLE001
            if attempt == 2:
                return {key: value for key, value in {**item, "label": "error", "evidence": f"{type(error).__name__}: {str(error)[:160]}"}.items() if key != "user_before"}
    return {key: value for key, value in {**item, "label": "error", "evidence": "unreachable"}.items() if key != "user_before"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--arms", nargs="*", default=["sac", "sac-old", "eocc"])
    parser.add_argument("--judge", default="openai/qwen3.8-27b")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--no-thinking", action="store_true", help="send `enable_thinking: false` in the body (Qwen-style; ~5× fewer output tokens)")
    parser.add_argument("--dsh-home", default=str(DEFAULT_DSH_HOME))
    parser.add_argument("--out", default=None)
    parser.add_argument("--summary", default=None)
    args = parser.parse_args()
    for line in (ROOT / "benchmarks" / "tau2-bench" / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(OPENAI_API_KEY|OPENAI_BASE_URL)\s*=\s*(\S+)", line)
        if m:
            import os

            os.environ.setdefault(m.group(1), m.group(2).strip("'\""))

    out_path = Path(args.out or REPORTS / f"states_{args.tag}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done, rows = set(), []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows.append(row)
                if row.get("label") not in ("error", "unparsed"):
                    done.add((row["session_id"], row["seq"]))
    dsh_home = Path(args.dsh_home).resolve()
    pending = []
    for arm in args.arms:
        for path in sorted(SIMS.glob(f"{args.tag}_{arm}_*")):
            if not (path / "results.json").exists():
                continue
            results = Results.load(path / "results.json")
            domain = results.info.environment_info.domain_name if results.info and results.info.environment_info else path.name.rsplit("_", 1)[1]
            for sim in results.simulations:
                session_id = None
                for message in sim.messages:
                    raw = getattr(message, "raw_data", None) or {}
                    if isinstance(raw, dict) and isinstance(raw.get("dsh"), dict):
                        session_id = raw["dsh"].get("session_id")
                        break
                log = find_session_log(dsh_home, session_id) if session_id else None
                if log is None:
                    continue
                session = load_session(log)
                for recap in state_recaps(session):
                    # Working-state folds (see the module dockey), plus every fold that swallows a user
                    # message: what its Objective says about the message it removes is the point.
                    if (session_id, recap["seq"]) in done or len(recap["user_before"]) < 2:
                        continue
                    if not (recap["after_latest"] or recap["swallowed_user"]):
                        continue
                    pending.append({
                        "arm": arm, "domain": domain, "task_id": sim.task_id, "trial": sim.trial, "session_id": session_id,
                        "reward": sim.reward_info.reward if sim.reward_info else None, **recap,
                    })
    print(f"{len(done)} folds already judged, {len(pending)} to judge", file=sys.stderr)
    import litellm

    litellm.drop_params = True
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool, out_path.open("a", encoding="utf-8") as handle:
        futures = [pool.submit(judge_one, args.judge, item, args.max_tokens, args.no_thinking) for item in pending]
        for number, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            with lock:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                rows.append(row)
            if number % 100 == 0:
                print(f"  {number}/{len(pending)}", file=sys.stderr)

    lines = ["| domain | arm | states judged | latest | earlier | mixed | unclear/unparsed | after a swallowed message: latest | earlier |", "|---|---|---|---|---|---|---|---|---|"]
    cells: dict[tuple, dict] = {}
    for row in rows:
        if row["label"] not in LABELS:
            continue
        for key in ((row["domain"], row["arm"], "all"), (row["domain"], row["arm"], "swallowed")):
            if key[2] == "swallowed" and not row["swallowed_user"]:
                continue
            cell = cells.setdefault(key, {"latest": 0, "earlier": 0, "mixed": 0, "unclear": 0})
            cell[row["label"]] += 1
    domains = sorted({key[0] for key in cells})
    for domain in domains + [None]:
        for arm in args.arms:
            keys = [key for key in cells if key[1] == arm and (domain is None or key[0] == domain)]
            if not keys:
                continue
            t = {label: sum(cells[key][label] for key in keys if key[2] == "all") for label in LABELS}
            s = {label: sum(cells[key][label] for key in keys if key[2] == "swallowed") for label in LABELS}
            n, m_ = sum(t.values()), sum(s.values())
            share = lambda total, k, label: f"{total[label]} ({100 * total[label] / k:.1f}%)" if k else "–"  # noqa: E731
            lines.append("| " + " | ".join(str(x) for x in (
                "**all**" if domain is None else domain, arm, n, share(t, n, "latest"), share(t, n, "earlier"), share(t, n, "mixed"),
                share(t, n, "unclear"), share(s, m_, "latest"), share(s, m_, "earlier"))) + " |")
    report = "\n".join(lines)
    print(report)
    if args.summary:
        Path(args.summary).write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
