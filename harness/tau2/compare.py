"""Compare the context-management arms on τ²-bench: success, paired differences, tokens, context size.

    work/tau2/venv/bin/python -m harness.tau2.compare --tag pilot4 --arms sac sac-old eocc fha \
        --out work/tau2/reports/pilot4_compare.md

For every ``<tag>_<arm>_<domain>`` results directory it runs :mod:`harness.tau2.analyze` (official
metrics + the DSH session logs) and prints one comparison. All arms run the same tasks with the same
seed and the same user simulator, so rewards are paired per task (and trial): besides each arm's
success rate (with a Wilson 95% interval) the report gives, against the FHA baseline, the four paired
outcome counts and the exact McNemar test on the discordant pairs.

Token columns come from the DSH session logs and are the provider's own counts: agent requests
(new input, cached input, output) plus the state-update requests of the compacting arms.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

from harness.tau2.analyze import summarize  # noqa: E402
from harness.tau2.dsh_agent import DEFAULT_DSH_HOME  # noqa: E402

SIMS = ROOT / "benchmarks" / "tau2-bench" / "data" / "simulations"
ARM_LABELS = {
    "sac": "SAC 1.2.0 (new)",
    "sac-old": "SAC 1.1.0 (old)",
    "eocc": "EoCC 12K/8K",
    "fha": "FHA (full history)",
    "official-llm": "official llm_agent",
    "sac-pin1": "SAC 1.2.0 pin-1",
}
Z95 = 1.959964

# Per-run values that add up over runs (everything else in a run summary is an average).
ADDITIVE = (
    "tokens.agent_new_input", "tokens.agent_cached_input", "tokens.agent_other_input", "tokens.agent_output",
    "tokens.state_input", "tokens.state_output", "tokens.state_calls", "tokens.agent_requests", "tokens.all_requests",
    "tokens.prompt_tokens_total",
    "dialogue.user_turns_total", "dialogue.tool_calls_total", "dialogue.tool_errors_total",
    "dsh.model_steps_total", "dsh.failed_turns_total", "dsh.sims_with_failed_turns", "dsh.max_tokens_turns", "dsh.rate_limited_sims",
    "sac.folded_user_messages_total", "sac.sims_with_folded_user_message", "sac.folds_total", "sac.state_update_failures_total",
)
PER_SIM = (
    "tokens.agent_new_input", "tokens.agent_cached_input", "tokens.agent_other_input", "tokens.agent_output", "tokens.total",
    "tokens.agent_requests", "tokens.state_calls", "dialogue.user_turns_total", "dialogue.tool_calls_total", "dialogue.tool_errors_total",
    "dsh.model_steps_total", "sac.folds_total",
)


def wilson(successes: int, n: int) -> tuple[float, float]:
    """Wilson score interval (95%) for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denominator = 1 + Z95**2 / n
    centre = (p + Z95**2 / (2 * n)) / denominator
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n**2)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts ``b`` and ``c``."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2**n
    return min(1.0, 2 * tail)


def pct(value, digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:.{digits}f}%"


def signed(value, digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:+.{digits}f}"


def num(value, digits: int = 0) -> str:
    """Thousands-separated number, or an en dash."""
    return "–" if value is None else f"{value:,.{digits}f}"


def table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join("–" if cell is None else str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def aggregate(runs: dict, keys: list) -> dict:
    """Sum the additive values of several run summaries and derive per-simulation figures."""
    out: dict = {"tasks": [], "n": 0, "duration": 0.0, "runs": len(keys)}
    for key in keys:
        for s in runs[key]:
            out["n"] += s["official"]["total_simulations"]
            out["tasks"] += [(key[0], t) for t in s["tasks"]]
            for name in ADDITIVE:
                group, field = name.split(".", 1)
                out[name] = out.get(name, 0) + (s[group].get(field) or 0)
            vis = s["visibility"]
            out["vis.later"] = out.get("vis.later", 0) + vis["later_user_messages"]
            out["vis.full"] = out.get("vis.full", 0) + vis["fully_visible"]
            out["vis.partial"] = out.get("vis.partial", 0) + vis["partially_folded"]
            out["vis.never"] = out.get("vis.never", 0) + vis["never_visible_after_arrival"]
            out["vis.sims_folded"] = out.get("vis.sims_folded", 0) + vis["sims_with_a_folded_later_message"]
            out["vis.ratio_sum"] = out.get("vis.ratio_sum", 0.0) + (vis["mean_visibility_ratio"] or 0) * vis["later_user_messages"]
            worst = vis["worst_visibility_ratio"]
            out["vis.worst"] = min(out.get("vis.worst", 1.0), worst if worst is not None else 1.0)
            out["duration"] += s["duration_s_total"]
            out["prompt_peak"] = max(out.get("prompt_peak", 0), s["tokens"]["prompt_tokens_peak"] or 0)
    n = out["n"]
    out["successes"] = sum(1 for _, t in out["tasks"] if t["reward"] == 1.0)
    out["rate"] = out["successes"] / n if n else None
    out["ci"] = wilson(out["successes"], n)
    out["reasons"] = {}
    for _, t in out["tasks"]:
        out["reasons"][t["termination"]] = out["reasons"].get(t["termination"], 0) + 1
    for name in PER_SIM:
        out[name + "_per_sim"] = out[name] / n if n and name in out else None
    total = out["tokens.agent_new_input"] + out["tokens.agent_cached_input"] + out["tokens.agent_other_input"] + out["tokens.agent_output"] \
        + out["tokens.state_input"] + out["tokens.state_output"]
    out["tokens.total"] = total
    out["tokens.total_per_sim"] = total / n if n else None
    out["tokens.per_success"] = total / out["successes"] if out["successes"] else None
    # What the tokens would cost with a provider's usual prefix-cache price (cached input at 10%):
    # new input + output + state-update calls + 0.1 × cached input.
    billable = total - 0.9 * out["tokens.agent_cached_input"]
    out["tokens.billable"] = billable
    out["tokens.billable_per_sim"] = billable / n if n else None
    out["tokens.billable_per_success"] = billable / out["successes"] if out["successes"] else None
    out["duration_per_sim"] = out["duration"] / n if n else None
    # Whole prompt per agent request (system prompt + tools + surface), true tokens.
    steps = out["tokens.agent_requests"]
    out["prompt_mean"] = out["tokens.prompt_tokens_total"] / steps if steps else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="*", help="results directories (default: <tag>_<arm>_<domain> for --tag/--arms)")
    parser.add_argument("--tag", default=None, nargs="+", help="one or more tags whose <tag>_<arm>_<domain> directories are compared")
    parser.add_argument("--arms", nargs="*", default=["sac", "sac-pin1", "sac-old", "eocc", "fha", "official-llm"])
    parser.add_argument("--baseline", default="fha")
    parser.add_argument("--dsh-home", default=str(DEFAULT_DSH_HOME))
    parser.add_argument("--out", default=None, help="markdown report")
    parser.add_argument("--json-out", default=None, help="per-run summaries as JSON")
    parser.add_argument("--complete-cases", action="store_true", default=True,
                        help="per domain keep only the (task, trial) pairs every arm has a result for (default)")
    parser.add_argument("--no-complete-cases", dest="complete_cases", action="store_false",
                        help="use every non-infrastructure result of each arm (arms may then cover different tasks)")
    args = parser.parse_args()

    dirs = [Path(p).resolve() for p in args.results]
    for tag in args.tag or []:
        for arm in args.arms:
            dirs += sorted(p for p in SIMS.glob(f"{tag}_{arm}_*") if (p / "results.json").exists())
    if not dirs:
        raise SystemExit("no results directories (give them, or --tag)")
    def arm_of(path: Path) -> str:
        return path.name.split("_", 1)[1].rsplit("_", 1)[0] if path.parent == SIMS else path.name

    dsh_home = Path(args.dsh_home).resolve()
    first_pass = {}
    for path in dirs:
        summary = summarize(path, dsh_home)
        first_pass.setdefault((summary["domain"], arm_of(path)), []).append((path, summary))
    domains = sorted({key[0] for key in first_pass})
    arms = [arm for arm in args.arms if any(key[1] == arm for key in first_pass)]
    if args.baseline not in arms:
        raise SystemExit(f"baseline arm {args.baseline!r} has no results")
    runs, dropped = {}, {}
    if not args.complete_cases:
        for key, group in first_pass.items():
            runs[key] = [summary for _path, summary in group]
    else:
        # Complete cases: in every domain, all arms present there describe the same (task, trial)
        # pairs — those for which each of them has an included result. Every run is then summarised
        # again under that restriction, so success, tokens, context and integrity share one basis.
        for domain in domains:
            present = [arm for arm in arms if (domain, arm) in first_pass]
            keys = None
            for arm in present:
                have = {(str(t["task_id"]), t["trial"]) for _path, summary in first_pass[(domain, arm)] for t in summary["tasks"]}
                keys = have if keys is None else keys & have
            for arm in present:
                kept = 0
                for path, summary in first_pass[(domain, arm)]:
                    restricted = summarize(path, dsh_home, keep=keys)
                    runs.setdefault((domain, arm), []).append(restricted)
                    kept += len(restricted["tasks"])
                dropped[f"{domain}/{arm}"] = sum(len(summary["tasks"]) for _path, summary in first_pass[(domain, arm)]) - kept
    runs = {key: value for key, value in runs.items() if any(s["tasks"] for s in value)}
    domains = sorted({key[0] for key in runs})
    arms = [arm for arm in arms if any(key[1] == arm for key in runs)]
    cells = {(domain, arm): aggregate(runs, [key for key in runs if key[1] == arm and (domain is None or key[0] == domain)])
             for domain in domains + [None] for arm in arms}
    cells = {key: value for key, value in cells.items() if value["n"]}
    label = lambda arm: ARM_LABELS.get(arm, arm)  # noqa: E731
    scope = lambda domain: "**all domains**" if domain is None else domain  # noqa: E731

    # ---- 1. success -------------------------------------------------------------------------
    rows = []
    for domain in domains + [None]:
        for arm in arms:
            c = cells.get((domain, arm))
            if not c:
                continue
            low, high = c["ci"]
            reasons = " / ".join(f"{k.replace('TerminationReason.', '').lower()} {v}" for k, v in sorted(c["reasons"].items()))
            rows.append([scope(domain), label(arm), c["n"], c["successes"], pct(c["rate"]), f"[{pct(low)}, {pct(high)}]", reasons])
    success_table = table(["domain", "arm", "sims", "success", "success rate", "95% Wilson", "termination"], rows)

    # ---- 2. paired against the baseline ------------------------------------------------------
    rows = []
    for domain in domains + [None]:
        base = {(d, t["task_id"], t["trial"]): t for d, t in cells.get((domain, args.baseline), {"tasks": []})["tasks"]}
        for arm in arms:
            if arm == args.baseline or (domain, arm) not in cells:
                continue
            both = arm_only = base_only = neither = 0
            for d, t in cells[(domain, arm)]["tasks"]:
                other = base.get((d, t["task_id"], t["trial"]))
                if other is None:
                    continue
                a, b = t["reward"] == 1.0, other["reward"] == 1.0
                both += a and b
                arm_only += a and not b
                base_only += b and not a
                neither += not a and not b
            paired = both + arm_only + base_only + neither
            diff = (arm_only - base_only) / paired if paired else None
            rows.append([scope(domain), label(arm), paired, both, arm_only, base_only, neither, signed(diff), f"{mcnemar_exact(arm_only, base_only):.3f}"])
    paired_table = table(["domain", "arm vs " + label(args.baseline), "paired sims", "both pass", "arm only", "baseline only", "both fail",
                          "Δ success rate (pp)", "McNemar p (exact)"], rows)

    # ---- 3. tokens and requests --------------------------------------------------------------
    rows = []
    for domain in domains + [None]:
        for arm in arms:
            c = cells.get((domain, arm))
            if not c:
                continue
            rows.append([scope(domain), label(arm),
                         num(c["tokens.agent_new_input"]), num(c["tokens.agent_cached_input"]), num(c["tokens.agent_output"]),
                         num(c["tokens.state_input"] + c["tokens.state_output"]), num(c["tokens.total"]),
                         num(c["tokens.total_per_sim"]), num(c["tokens.per_success"]), num(c["tokens.billable"]),
                         num(c["tokens.billable_per_sim"]),
                         num(c["tokens.agent_requests_per_sim"], 1), num(c["tokens.state_calls_per_sim"], 1)])
    token_table = table(["domain", "arm", "new input tok", "cached input tok", "output tok", "state-update tok", "total tok", "tok / sim", "tok / success",
                         "billable-equiv. tok (cache at 10%)", "billable / sim", "agent requests / sim", "state calls / sim"], rows)

    # ---- 4. context size and dialogue shape --------------------------------------------------
    rows = []
    for domain in domains + [None]:
        base = cells.get((domain, args.baseline))
        for arm in arms:
            c = cells.get((domain, arm))
            if not c:
                continue
            rel = f"{c['prompt_mean'] / base['prompt_mean']:.2f}×" if base and base.get("prompt_mean") and c.get("prompt_mean") else "–"
            rows.append([scope(domain), label(arm), num(c.get("prompt_mean")), num(c.get("prompt_peak")), rel,
                         num(c["dsh.model_steps_total_per_sim"], 1), num(c["dialogue.user_turns_total_per_sim"], 1),
                         num(c["dialogue.tool_calls_total_per_sim"], 1), num(c["dialogue.tool_errors_total_per_sim"], 2),
                         num(c["duration_per_sim"])])
    context_table = table(["domain", "arm", "prompt tok / agent request (mean)", "peak prompt tok", "mean vs FHA", "model steps / sim",
                           "user turns / sim", "tool calls / sim", "tool errors / sim", "wall-clock s / sim"], rows)

    # ---- 5. compaction integrity -------------------------------------------------------------
    rows = []
    for domain in domains + [None]:
        for arm in arms:
            c = cells.get((domain, arm))
            if not c:
                continue
            rows.append([scope(domain), label(arm), c["sac.folded_user_messages_total"], c["sac.sims_with_folded_user_message"],
                         num(c["sac.folds_total_per_sim"], 1), c["sac.state_update_failures_total"],
                         f"{c.get('dsh.max_tokens_turns', 0)} ({c.get('dsh.rate_limited_sims', 0)})", c["dsh.sims_with_failed_turns"],
                         num(c["duration"] / 60)])
    invariant_table = table(["domain", "arm", "user messages swallowed by a fold", "sims with such a fold", "folds / sim",
                             "state-update failures", "turns cut off (rate-limited sims left out)", "sims with such turns", "wall-clock min"], rows)

    # ---- 6. later user messages: how long the model kept seeing them verbatim -----------------
    rows = []
    for domain in domains + [None]:
        for arm in arms:
            c = cells.get((domain, arm))
            if not c or not c.get("vis.later"):
                continue
            later = c["vis.later"]
            rows.append([scope(domain), label(arm), later,
                         f"{c['vis.full']} ({100 * c['vis.full'] / later:.1f}%)", f"{c['vis.partial']} ({100 * c['vis.partial'] / later:.1f}%)",
                         c["vis.never"], pct(c["vis.ratio_sum"] / later), pct(c["vis.worst"]), c["vis.sims_folded"]])
    visibility_table = table(["domain", "arm", "later user messages", "shown at every later step", "folded at some later step",
                              "never shown", "mean share of later steps", "worst single message", "sims with a folded later message"], rows)

    meta = {
        "complete_cases": args.complete_cases,
        "results_dropped_for_complete_cases": dropped,
        "user_llm": sorted({str(runs[key][0]["user_llm"]) for key in runs}),
        "agent_llm_label": sorted({str(runs[key][0]["agent_llm_label"]) for key in runs}),
        "num_trials": sorted({runs[key][0]["num_trials"] for key in runs}),
        "results_dirs": sorted(str(path) for path in dirs),
        "session_logs_missing": {f"{d}/{a}": sum(s["dsh"]["session_logs_missing"] for s in runs[(d, a)]) for d in domains for a in arms if (d, a) in runs},
        "agent_requests_without_session_log": {f"{d}/{a}": sum(s["dsh"]["model_steps_total"] - s["sac"]["model_steps_in_logs"] for s in runs[(d, a)])
                                               for d in domains for a in arms if (d, a) in runs},
    }
    report = "\n\n".join([
        "## 1. Success rate (official avg_reward)",
        success_table,
        f"## 2. Paired outcomes against **{label(args.baseline)}** (same tasks, same seed, same user simulator)",
        paired_table,
        "## 3. Tokens and requests (agent side; provider counts from the DSH session logs)",
        token_table,
        "## 4. Context size and dialogue shape",
        context_table,
        "## 5. Compaction integrity (from the DSH session logs)",
        invariant_table,
        "## 6. Later user messages: share of the model steps after their arrival at which each was still on the request verbatim",
        visibility_table,
        "## 7. Run metadata",
        "```json\n" + json.dumps(meta, indent=1, ensure_ascii=False) + "\n```",
    ])
    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("# τ²-bench: arms compared\n\n" + report + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}", file=sys.stderr)
    if args.json_out:
        payload = {
            "meta": meta,
            "cells": {f"{d}__{a}": {k: v for k, v in c.items() if k != "tasks"} for (d, a), c in cells.items()},
            "tasks": {f"{d}__{a}": c["tasks"] for (d, a), c in cells.items()},
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
