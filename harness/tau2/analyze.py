"""Post-run analysis: the official τ² metrics plus the SAC-specific checks from the DSH session logs.

    work/tau2/venv/bin/python -m harness.tau2.analyze \
        benchmarks/tau2-bench/data/simulations/sac_airline \
        benchmarks/tau2-bench/data/simulations/fha_airline --out work/tau2/reports/airline.md

Each argument is a results directory or ``results.json`` written by ``tau2 run`` (``--save-to``).
The official numbers come from the official ``compute_metrics`` (average reward, pass^k, termination
reasons). On top of them, per run:

* SAC integrity — from every simulation's DSH session log: user messages folded (must be 0), folds,
  state-update failures, context size per step (estimated tokens: surface chars / 3.5);
* DSH cost — model steps, input/output tokens, turns that exited non-zero or were ended by the bridge;
* dialogue shape — user turns, tool calls, tool errors per simulation.

With several results the arms are printed side by side.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

# tau2 logs its registry when imported; keep the report readable.
logger.remove()
logger.add(sys.stderr, level="ERROR")

from harness.tau2.dsh_agent import DEFAULT_DSH_HOME  # noqa: E402
from harness.tau2.session_log import (  # noqa: E402
    context_tokens,
    find_session_log,
    load_session,
    sac_integrity,
    token_usage,
    user_message_visibility,
)


def _resolve(path: Path) -> Path:
    """The path ``Results.load`` must get.

    ``tau2 run --save-to <name>`` writes one monolithic ``results.json`` (simulations inline) into
    ``data/simulations/<name>/``. ``Results.load`` treats a *directory* as the directory-based
    format and then reads only ``<dir>/simulations/*.json`` — passing such a directory yields zero
    simulations — so hand it the file unless the directory really is in the directory format.
    """
    if path.is_dir():
        if (path / "simulations").is_dir():
            return path
        if (path / "results.json").exists():
            return path / "results.json"
    return path


def dsh_turns(simulation) -> list[dict]:
    """The per-turn records the agent attached to its replies (``AssistantMessage.raw_data['dsh']``)."""
    records = []
    for message in simulation.messages:
        raw = getattr(message, "raw_data", None) or {}
        if isinstance(raw, dict) and isinstance(raw.get("dsh"), dict):
            records.append(raw["dsh"])
    # The same record object is attached to every reply of one turn only once; dedupe by (session, turn).
    unique = {}
    for record in records:
        unique[(record.get("session_id"), record.get("turn"))] = record
    return [unique[key] for key in sorted(unique, key=lambda k: (str(k[0]), k[1] or 0))]


def official_usage(simulation) -> dict | None:
    """Token usage of a simulation run by the official ``llm_agent``, read from its assistant messages.

    Same keys as :func:`harness.tau2.session_log.token_usage`. The official agent reports
    ``prompt_tokens`` / ``completion_tokens`` per request; cached input is only known when the
    provider reports ``prompt_tokens_details.cached_tokens``.
    """
    prompts, new, cached, other, output, requests = [], 0, 0, 0, 0, 0
    for message in simulation.messages:
        usage = getattr(message, "usage", None)
        if message.__class__.__name__ != "AssistantMessage" or not isinstance(usage, dict):
            continue
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        requests += 1
        prompts.append(prompt)
        new += max(0, prompt - hit)
        cached += hit
        output += completion
    if requests == 0:
        return None
    return {
        "agent_new_input": new,
        "agent_cached_input": cached,
        "agent_other_input": other,
        "agent_output": output,
        "state_input": 0,
        "state_output": 0,
        "state_calls": 0,
        "agent_requests": requests,
        "prompt_tokens_total": sum(prompts),
        "prompt_tokens_mean": round(sum(prompts) / len(prompts)),
        "prompt_tokens_peak": max(prompts),
        "prompt_tokens_first": prompts[0],
        "prompts": prompts,
    }


def summarize(path: Path, dsh_home: Path, keep: set | None = None) -> dict:
    from tau2.data_model.simulation import Results
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.user.user_simulator_base import OUT_OF_SCOPE, STOP, TRANSFER

    results = Results.load(_resolve(path))
    metrics = compute_metrics(results)
    sims = results.simulations
    out: dict = {
        "results": str(path),
        "domain": results.info.environment_info.domain_name if results.info and results.info.environment_info else None,
        "agent": results.info.agent_info.implementation if results.info and results.info.agent_info else None,
        "agent_llm_label": results.info.agent_info.llm if results.info and results.info.agent_info else None,
        "user_llm": results.info.user_info.llm if results.info and results.info.user_info else None,
        "num_trials": results.info.num_trials if results.info else None,
        "max_steps": results.info.max_steps if results.info else None,
        "official": {
            "avg_reward": round(metrics.avg_reward, 4) if sims else None,
            "pass_hat_ks": {str(k): round(v, 4) for k, v in metrics.pass_hat_ks.items()},
            "total_tasks": metrics.total_tasks,
            "total_simulations": metrics.total_simulations,
            "infra_errors": metrics.infra_error_count,
            "db_match": metrics.db_match_count,
            "db_mismatch": metrics.db_mismatch_count,
            "termination": {
                "user_stop": metrics.termination_user_stop,
                "agent_stop": metrics.termination_agent_stop,
                "max_steps": metrics.termination_max_steps,
                "error": metrics.termination_error,
            },
        },
    }
    reasons = Counter(str(getattr(sim.termination_reason, "value", sim.termination_reason)) for sim in sims)
    out["termination_reasons"] = dict(reasons)
    evaluated = [sim for sim in sims if str(getattr(sim.termination_reason, "value", sim.termination_reason)) != "infrastructure_error"]
    out["official"]["evaluated_simulations"] = len(evaluated)
    user_turns, tool_calls, tool_errors, replies = [], [], [], []
    dsh_steps, dsh_input, dsh_output, dsh_turns_n, dsh_bad = [], [], [], [], []
    sessions_found, sessions_missing = 0, 0
    folded_user_msgs, fold_counts, ctx_means, ctx_peaks, compaction_errors, steps_in_log, not_smaller = [], [], [], [], [], [], []
    # True token usage from the session logs (new / cached input, output, state-update calls).
    usage_rows = []
    usage = dict(agent_new_input=0, agent_cached_input=0, agent_other_input=0, agent_output=0, state_input=0, state_output=0, state_calls=0, agent_requests=0, prompt_tokens_total=0)
    prompt_means, prompt_peaks, prompt_firsts, all_prompts = [], [], [], []
    vis_later = vis_full = vis_partial = vis_ratio_sum = sims_with_folded_later = 0
    vis_min_ratios = []
    official_sims = 0
    rate_limited_sims = 0
    max_tokens_turns = 0
    breakdown = Counter()
    out["tasks"] = []
    for sim in sims:
        # Infrastructure errors (endpoint outage, exhausted quota) are not agent outcomes: the
        # official metrics exclude them, and so does everything below.
        if str(getattr(sim.termination_reason, "value", sim.termination_reason)) == "infrastructure_error":
            continue
        # `keep` = the (task id, trial) pairs a comparison restricts every arm to (complete cases).
        if keep is not None and (str(getattr(sim, "task_id", None)), getattr(sim, "trial", None)) not in keep:
            continue
        turns = dsh_turns(sim)
        # A DSH turn the endpoint cut off (HTTP 429: the agent model's proxy ran into its token-rate
        # limit) makes the agent answer with a fallback sentence. That is an infrastructure artifact
        # too, so such simulations are left out as well (they are counted, see `rate_limited_sims`).
        if any(t.get("exit_code") not in (0, None) and (t.get("reason") or {}).get("kind") == "error" for t in turns):
            rate_limited_sims += 1
            continue
        hit_cap = sum(1 for t in turns if (t.get("reason") or {}).get("kind") == "max-tokens")
        max_tokens_turns += hit_cap
        task_id = sim.task_id if hasattr(sim, "task_id") else (sim.task.id if getattr(sim, "task", None) is not None else None)
        out["tasks"].append({"task_id": task_id, "trial": getattr(sim, "trial", None), "reward": sim.reward_info.reward if sim.reward_info else None,
                             "termination": str(getattr(sim.termination_reason, "value", sim.termination_reason)),
                             "duration": sim.duration, "session_id": None, "max_tokens_turns": hit_cap})
        control = (STOP, TRANSFER, OUT_OF_SCOPE)
        user_turns.append(
            sum(
                1
                for m in sim.messages
                if m.__class__.__name__ == "UserMessage" and not m.is_tool_call() and not str(m.content or "").strip() in control
            )
        )
        tools = [m for m in sim.messages if m.__class__.__name__ == "ToolMessage"]
        tool_calls.append(len(tools))
        tool_errors.append(sum(1 for m in tools if m.error))
        replies.append(sum(1 for m in sim.messages if m.__class__.__name__ == "AssistantMessage" and not m.is_tool_call()) - 1)
        info = sim.reward_info.info if sim.reward_info else {}
        for key in ("db_check", "env_assertions", "action_checks", "communicate_checks", "nl_assertions"):
            value = (info or {}).get(key)
            if isinstance(value, dict) and "db_reward" in value:
                breakdown[f"{key}:{value['db_reward']}"] += 1
        dsh_turns_n.append(len(turns))
        official = official_usage(sim) if not turns else None
        if official is not None:
            # The official `llm_agent` (no DSH): one LLM request per assistant message, usage on it.
            official_sims += 1
            dsh_steps.append(official["agent_requests"])
            dsh_input.append(official["agent_new_input"])
            dsh_output.append(official["agent_output"])
            dsh_bad.append(0)
            for key in usage:
                usage[key] += official[key]
            prompt_means.append(official["prompt_tokens_mean"])
            prompt_peaks.append(official["prompt_tokens_peak"])
            prompt_firsts.append(official["prompt_tokens_first"])
            all_prompts.extend(official["prompts"])
            continue
        dsh_steps.append(sum(turn.get("steps", 0) for turn in turns))
        dsh_input.append(sum((turn.get("usage") or {}).get("inputTokens", 0) for turn in turns))
        dsh_output.append(sum((turn.get("usage") or {}).get("outputTokens", 0) for turn in turns))
        bad = [turn for turn in turns if turn.get("exit_code") != 0 or turn.get("error")]
        dsh_bad.append(len(bad))
        session_id = turns[0].get("session_id") if turns else None
        out["tasks"][-1]["session_id"] = session_id
        # Fallback usage from the JSON stream (agent steps only; inputTokens = new input).
        stream_usage = {
            "agent_new_input": sum((turn.get("usage") or {}).get("inputTokens", 0) for turn in turns),
            "agent_cached_input": sum((turn.get("usage") or {}).get("cacheReadTokens", 0) for turn in turns),
            "agent_other_input": sum((turn.get("usage") or {}).get("cacheWriteTokens", 0) for turn in turns),
            "agent_output": sum((turn.get("usage") or {}).get("outputTokens", 0) for turn in turns),
            "state_input": 0,
            "state_output": 0,
            "state_calls": 0,
            "agent_requests": sum(turn.get("steps", 0) for turn in turns),
            "prompt_tokens_total": 0,
        }
        log = find_session_log(dsh_home, session_id) if session_id else None
        if log is None:
            sessions_missing += 1
            for key in usage:
                usage[key] += stream_usage[key]
            continue
        sessions_found += 1
        session = load_session(log)
        integrity = sac_integrity(session)
        folded_user_msgs.append(len(integrity["folded_user_messages"]))
        fold_counts.append(integrity["folds"])
        compaction_errors.append(integrity["compaction_errors"])
        not_smaller.append(integrity["not_smaller_refusals"])
        steps_in_log.append(integrity["steps"])
        ctx = context_tokens(session)
        ctx_means.append(ctx["mean"])
        ctx_peaks.append(ctx["peak"])
        row = token_usage(session)
        usage_rows.append(row)
        for key in usage:
            usage[key] += row[key]
        prompt_means.append(row["prompt_tokens_mean"])
        prompt_peaks.append(row["prompt_tokens_peak"])
        prompt_firsts.append(row["prompt_tokens_first"])
        all_prompts.extend(row["prompts"])
        visibility = user_message_visibility(session)
        vis_later += visibility["later_messages"]
        vis_full += visibility["later_fully_visible"]
        vis_partial += visibility["later_partially_folded"]
        vis_ratio_sum += sum(row["ratio"] for row in visibility["rows"][1:] if row["ratio"] is not None)
        if visibility["later_min_ratio"] is not None:
            vis_min_ratios.append(visibility["later_min_ratio"])
        if visibility["later_messages"] > visibility["later_fully_visible"]:
            sims_with_folded_later += 1
        out["tasks"][-1]["visibility"] = {key: value for key, value in visibility.items() if key != "rows"}

    def avg(values):
        return round(statistics.fmean(values), 2) if values else None

    out["dialogue"] = {
        "user_turns_avg": avg(user_turns),
        "agent_replies_avg": avg(replies),
        "tool_calls_avg": avg(tool_calls),
        "tool_errors_avg": avg(tool_errors),
        "tool_error_free_sims": sum(1 for n in tool_errors if n == 0),
        "user_turns_total": sum(user_turns),
        "tool_calls_total": sum(tool_calls),
        "tool_errors_total": sum(tool_errors),
    }
    out["dsh"] = {
        "turns_avg": avg(dsh_turns_n),
        "model_steps_avg": avg(dsh_steps),
        "model_steps_total": sum(dsh_steps),
        "input_tokens_total": sum(dsh_input),
        "output_tokens_total": sum(dsh_output),
        "input_tokens_avg_per_sim": avg(dsh_input),
        "output_tokens_avg_per_sim": avg(dsh_output),
        "sims_with_failed_turns": sum(1 for n in dsh_bad if n),
        "failed_turns_total": sum(dsh_bad),
        "session_logs_found": sessions_found,
        "session_logs_missing": sessions_missing,
        # Simulations of the official `llm_agent` (no DSH session; usage read from the trajectory).
        "official_agent_sims": official_sims,
        # Left out as infrastructure artifacts.
        "rate_limited_sims": rate_limited_sims,
        # Kept: the model spent the whole output budget (maxTokens) on one step; a model behaviour.
        "max_tokens_turns": max_tokens_turns,
    }
    out["sac"] = {
        "folded_user_messages_total": sum(folded_user_msgs),
        "sims_with_folded_user_message": sum(1 for n in folded_user_msgs if n),
        "folds_avg": avg(fold_counts),
        "folds_total": sum(fold_counts),
        "state_update_failures_total": sum(compaction_errors),
        # Of those: folds refused by the backend because the recap was not smaller than the span.
        "not_smaller_refusals_total": sum(not_smaller),
        "other_failures_total": sum(compaction_errors) - sum(not_smaller),
        "context_tokens_mean_avg": avg(ctx_means),
        "context_tokens_peak_max": max(ctx_peaks) if ctx_peaks else None,
        "model_steps_in_logs": sum(steps_in_log),
    }
    success = sum(1 for sim in sims if sim.reward_info and sim.reward_info.reward == 1.0)
    agent_total = usage["agent_new_input"] + usage["agent_cached_input"] + usage["agent_other_input"] + usage["agent_output"]
    state_total = usage["state_input"] + usage["state_output"]
    # Later user messages (everything after the first) and the share of the model steps after
    # their arrival at which each was still on the request verbatim.
    out["visibility"] = {
        "later_user_messages": vis_later,
        "fully_visible": vis_full,
        "partially_folded": vis_partial,
        "never_visible_after_arrival": vis_later - vis_full - vis_partial,
        "sims_with_a_folded_later_message": sims_with_folded_later,
        "mean_visibility_ratio": round(vis_ratio_sum / vis_later, 4) if vis_later else None,
        "worst_visibility_ratio": min(vis_min_ratios) if vis_min_ratios else None,
    }
    out["tokens"] = {
        # Agent requests: new (uncached) input, cached input, generated output.
        "agent_new_input": usage["agent_new_input"],
        "agent_cached_input": usage["agent_cached_input"],
        "agent_output": usage["agent_output"],
        # State-update (compaction) requests of the compacting arms; 0 for FHA.
        "state_input": usage["state_input"],
        "state_output": usage["state_output"],
        "state_calls": usage["state_calls"],
        "agent_requests": usage["agent_requests"],
        "all_requests": usage["agent_requests"] + usage["state_calls"],
        "total": agent_total + state_total,
        "per_simulation": round((agent_total + state_total) / len(evaluated)) if evaluated else None,
        "per_success": round((agent_total + state_total) / success) if success else None,
        # Whole prompt per agent request (system prompt + tools + surface), true tokens.
        "prompt_tokens_total": usage["prompt_tokens_total"],
        "prompt_tokens_mean": round(sum(all_prompts) / len(all_prompts)) if all_prompts else None,
        "prompt_tokens_peak": max(all_prompts) if all_prompts else None,
        "prompt_tokens_peak_avg": avg(prompt_peaks),
        "first_request_avg": avg(prompt_firsts),
        "from_session_logs": bool(usage_rows),
    }
    out["efficiency"] = {
        "successes": success,
        "tokens_per_simulation": out["tokens"]["per_simulation"],
        "tokens_per_success": out["tokens"]["per_success"],
        "model_steps_per_success": round(sum(dsh_steps) / success) if success else None,
    }
    out["reward_components"] = dict(breakdown)
    out["duration_s_total"] = round(sum(sim.duration or 0 for sim in sims), 1)
    return out


def table(rows: list[dict], keys: list[tuple[str, str]]) -> str:
    header = ["metric"] + [row["label"] for row in rows]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for label, getter in keys:
        cells = []
        for row in rows:
            value = getter(row["summary"])
            cells.append("–" if value is None else (f"{value:,}" if isinstance(value, int) else str(value)))
        lines.append("| " + " | ".join([label] + cells) + " |")
    return "\n".join(lines)


KEYS = [
    ("成功率 (official avg_reward)", lambda s: s["official"]["avg_reward"]),
    ("pass^k", lambda s: s["official"]["pass_hat_ks"] or None),
    ("任务数 / 仿真数", lambda s: f"{s['official']['total_tasks']} / {s['official']['total_simulations']}"),
    ("DB 终态一致", lambda s: f"{s['official']['db_match']} 一致 / {s['official']['db_mismatch']} 不一致"),
    ("结束原因", lambda s: s["termination_reasons"]),
    ("用户轮数 / 仿真", lambda s: s["dialogue"]["user_turns_avg"]),
    ("工具调用数 / 仿真", lambda s: s["dialogue"]["tool_calls_avg"]),
    ("工具报错数 / 仿真", lambda s: s["dialogue"]["tool_errors_avg"]),
    ("DSH 模型步数 / 仿真", lambda s: s["dsh"]["model_steps_avg"]),
    ("DSH 输入 token 合计", lambda s: s["dsh"]["input_tokens_total"]),
    ("DSH 输出 token 合计", lambda s: s["dsh"]["output_tokens_total"]),
    ("token / 成功任务", lambda s: s["efficiency"]["tokens_per_success"]),
    ("DSH 非正常结束的回合数", lambda s: s["dsh"]["failed_turns_total"]),
    ("找到的 DSH 会话日志", lambda s: f"{s['dsh']['session_logs_found']} / 缺 {s['dsh']['session_logs_missing']}"),
    ("被折叠的用户消息数（应为 0）", lambda s: s["sac"]["folded_user_messages_total"]),
    ("折叠次数 / 仿真", lambda s: s["sac"]["folds_avg"]),
    ("状态更新失败数", lambda s: s["sac"]["state_update_failures_total"]),
    ("上下文 token：均值", lambda s: s["sac"]["context_tokens_mean_avg"]),
    ("上下文 token：峰值", lambda s: s["sac"]["context_tokens_peak_max"]),
    ("墙钟合计（秒）", lambda s: s["duration_s_total"]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="+", help="results directory or results.json written by `tau2 run --save-to`")
    parser.add_argument("--label", nargs="*", default=None, help="one label per results argument")
    parser.add_argument("--dsh-home", default=str(DEFAULT_DSH_HOME))
    parser.add_argument("--json", action="store_true", help="print the full per-run summaries as JSON")
    parser.add_argument("--out", default=None, help="write the comparison table to this markdown file")
    args = parser.parse_args()
    dsh_home = Path(args.dsh_home).resolve()
    rows = []
    for index, raw in enumerate(args.results):
        path = Path(raw).resolve()
        label = args.label[index] if args.label and index < len(args.label) else path.name
        summary = summarize(path, dsh_home)
        rows.append({"label": label, "summary": summary})
    if args.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
    report = table(rows, KEYS)
    print(report)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# τ²-bench 多轮测试结果\n\n" + report + "\n\n"
            + "<details><summary>per-run JSON</summary>\n\n```json\n" + json.dumps(rows, indent=1, ensure_ascii=False) + "\n```\n</details>\n",
            encoding="utf-8",
        )
        print(f"\nwritten to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
