"""Refold forensics for the compacting arms: what was folded, how big the recap was, what was refused.

    work/tau2/venv/bin/python -m harness.tau2.folds --tag full4 --arms sac sac-old eocc

For every fold in the DSH session logs: the span's estimated size (chars / 3.5, the plugin's own
estimator), the recap's size, whether the backend refused it ("summary is not smaller"), and the
segment kind inferred from the span's position relative to the newest user message (after it =
working state; before it = a finished turn; containing released requests = archive).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

from harness.tau2.dsh_agent import DEFAULT_DSH_HOME  # noqa: E402
from harness.tau2.session_log import find_session_log, load_session, user_messages  # noqa: E402
from harness.session_replay.saclog import text_of  # noqa: E402
from tau2.data_model.simulation import Results  # noqa: E402

SIMS = ROOT / "benchmarks" / "tau2-bench" / "data" / "simulations"
CPT = 3.5
_REFUSAL = re.compile(r"\((\d+) estimated framed tokens >= (\d+)\)")


def parse_refusal(error: str) -> tuple[int, int] | None:
    """`(recap tokens, shadowed tokens)` from a "summary is not smaller" error, if it says so."""
    match = _REFUSAL.search(error or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def folds_of(session) -> list[dict]:
    users = user_messages(session)
    out = []
    for seq in session.order:
        event = session.events[seq]
        if event["type"] != "compaction/summary":
            continue
        checkpoint = next((q for q in session.order if q > seq and session.events[q]["type"] == "user/message"), None)
        if checkpoint is None:
            continue
        span = [q for q in (session.events[checkpoint].get("sourceEventSeqs") or []) if q in session.events]
        surface = [q for q in span if session.events[q]["type"] in ("user/message", "assistant/message", "tool/result", "system/message")]
        chars = sum(session.chars(q) for q in surface)
        recap = sum(len(block.get("text", "")) for block in (event["data"].get("summary") or []))
        newest = max([u for u in users if u < seq], default=None)
        swallowed = [u for u in users if u in span]
        if swallowed and text_of(session.events[checkpoint]).find("[SAC EARLIER USER REQUESTS]") >= 0:
            kind = "archive"
        elif newest is not None and surface and min(surface) > newest:
            kind = "working"
        else:
            kind = "finished"
        out.append({"seq": seq, "kind": kind, "span_tokens_est": chars / CPT, "recap_chars": recap, "recap_tokens_est": recap / CPT, "swallowed": len(swallowed)})
    refused = []
    for seq in session.order:
        event = session.events[seq]
        if event["type"] == "compaction/end" and event["data"].get("error"):
            refused.append({"seq": seq, "error": str(event["data"]["error"])})
    return out, refused


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--arms", nargs="*", default=["sac", "sac-old", "eocc"])
    parser.add_argument("--dsh-home", default=str(DEFAULT_DSH_HOME))
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()
    dsh_home = Path(args.dsh_home).resolve()
    report = {}
    for arm in args.arms:
        rows, refused = [], []
        for path in sorted(SIMS.glob(f"{args.tag}_{arm}_*")):
            if not (path / "results.json").exists():
                continue
            for sim in Results.load(path / "results.json").simulations:
                if str(getattr(sim.termination_reason, "value", sim.termination_reason)) == "infrastructure_error":
                    continue  # endpoint outage / exhausted quota: not an agent outcome
                if any(t.get("exit_code") not in (0, None) and (t.get("reason") or {}).get("kind") == "error"
                       for m in sim.messages for t in (((getattr(m, "raw_data", None) or {}).get("dsh") or {}) and [((getattr(m, "raw_data", None) or {}).get("dsh") or {})] or [])):
                    continue  # a DSH turn the agent endpoint cut off (rate limit): same rule as analyze.py
                session_id = None
                for message in sim.messages:
                    raw = getattr(message, "raw_data", None) or {}
                    if isinstance(raw, dict) and isinstance(raw.get("dsh"), dict):
                        session_id = raw["dsh"].get("session_id")
                        break
                log = find_session_log(dsh_home, session_id) if session_id else None
                if log is None:
                    continue
                f, r = folds_of(load_session(log))
                rows += [{**x, "domain": path.name.rsplit("_", 1)[1], "task": sim.task_id} for x in f]
                refused += [{**x, "domain": path.name.rsplit("_", 1)[1], "task": sim.task_id} for x in r]
        kinds = Counter(row["kind"] for row in rows)
        spans = [row["span_tokens_est"] for row in rows]
        recaps = [row["recap_tokens_est"] for row in rows]
        small = [row for row in rows if row["span_tokens_est"] < 800]
        parsed = [p for p in (parse_refusal(x["error"]) for x in refused) if p is not None]
        refused_recaps = sorted(recap for recap, _shadow in parsed)
        refused_spans = sorted(shadow for _recap, shadow in parsed)
        report[arm] = {
            "folds": len(rows),
            "by_kind": dict(kinds),
            "folds_swallowing_a_user_message": sum(1 for row in rows if row["swallowed"]),
            "span_tokens_est_mean": round(statistics.fmean(spans)) if spans else None,
            "span_tokens_est_median": round(statistics.median(spans)) if spans else None,
            "recap_tokens_est_mean": round(statistics.fmean(recaps)) if recaps else None,
            "recap_tokens_est_max": round(max(recaps)) if recaps else None,
            "recap_tokens_est_p90": round(sorted(recaps)[int(0.9 * (len(recaps) - 1))]) if recaps else None,
            "folds_with_span_below_800": len(small),
            "refused_not_smaller": len(refused),
            "refused_errors": Counter(x["error"].split("(")[0].strip() for x in refused),
            "refused_share_of_attempts": round(len(refused) / (len(rows) + len(refused)), 3) if rows or refused else None,
            # What the backend compared in the refused folds: recap tokens vs shadowed tokens.
            "refused_recap_tokens_range": [refused_recaps[0], refused_recaps[-1]] if refused_recaps else None,
            "refused_shadowed_tokens_median": round(statistics.median(refused_spans)) if refused_spans else None,
            "refused_shadowed_tokens_max": refused_spans[-1] if refused_spans else None,
        }
        print(f"\n=== {arm}")
        for key, value in report[arm].items():
            print(f"  {key}: {dict(value) if isinstance(value, Counter) else value}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({k: {kk: (dict(vv) if isinstance(vv, Counter) else vv) for kk, vv in v.items()} for k, v in report.items()}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
