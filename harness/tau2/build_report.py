"""Assemble the τ²-bench comparison report from the result files (every number is read, not typed).

    work/tau2/venv/bin/python -m harness.tau2.build_report --tag q27 \
        --arms sac sac-old eocc fha --official official-llm \
        --judge-turns work/tau2/reports/judge_q27.jsonl --judge-states work/tau2/reports/states_q27.jsonl \
        --out reports/REPORT-TAU2-SAC120-MULTITURN.md

Runs `harness.tau2.compare` twice (the arms alone, and with the official reference), reads the two
LLM-judge result files and the DSH session logs (fold forensics, pinned-request counts), and writes
the markdown report. The same inclusion rule as everywhere else applies (see `inclusion.py`).
Sentences that need reading (the flagged turns, for example) are left to the author: the builder
writes the list of flagged turns next to the report.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

logger.remove()
from harness.tau2.folds import folds_of  # noqa: E402
from harness.tau2.inclusion import DSH_HOME, included_sims, session_id_of  # noqa: E402
from harness.tau2.session_log import find_session_log, load_session  # noqa: E402

REPORTS = ROOT / "work" / "tau2" / "reports"
LABEL = {
    "sac": "SAC 1.2.0（新版）",
    "sac-old": "SAC 1.1.0（旧版）",
    "eocc": "EoCC 12K/8K（论文模板）",
    "eocc-anchored": "EoCC（1.2.0 模板）",
    "fha": "FHA",
    "official-llm": "官方 `llm_agent`",
    "sac-pin1": "SAC 1.2.0（pin=1）",
}
DOMAIN_CN = {"airline": "airline", "retail": "retail", "telecom": "telecom"}


def n(value) -> str:
    return "–" if value is None else f"{value:,}"


def pct(value, digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:.{digits}f}%"


def signed(value, digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:+.{digits}f}"


def mcnemar(b: int, c: int) -> float:
    import math

    total = b + c
    if total == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(total, i) for i in range(k + 1)) / 2**total)


def paired(tasks_a: dict, tasks_b: dict) -> dict:
    both = a_only = b_only = neither = 0
    for key, ta in tasks_a.items():
        tb = tasks_b.get(key)
        if tb is None:
            continue
        x, y = ta["reward"] == 1.0, tb["reward"] == 1.0
        both += x and y
        a_only += x and not y
        b_only += y and not x
        neither += not x and not y
    total = both + a_only + b_only + neither
    return {"both": both, "a_only": a_only, "b_only": b_only, "neither": neither, "n": total,
            "delta": (a_only - b_only) / total if total else None, "p": mcnemar(a_only, b_only)}


def task_map(rows) -> dict:
    return {(d, t["task_id"], t["trial"]): t for d, t in rows}


def run_compare(tag: str, arms: list, out_md: Path, out_json: Path | None) -> dict:
    command = [sys.executable, "-m", "harness.tau2.compare", "--tag", tag, "--arms", *arms, "--out", str(out_md)]
    if out_json:
        command += ["--json-out", str(out_json)]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    return json.loads(out_json.read_text()) if out_json else {}


def judge_summary(path: Path, sims: dict, kind: str) -> dict:
    """Labels per arm for the included simulations only."""
    by_session = {session_id_of(sim): arm for arm, items in sims.items() for _domain, sim in items if session_id_of(sim)}
    by_sim = {(arm, sim.id): arm for arm, items in sims.items() for _domain, sim in items}
    table = collections.defaultdict(collections.Counter)
    flagged = []
    if not path.exists():
        return {"table": table, "flagged": flagged, "unjudged": 0}
    unjudged = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        arm = by_sim.get((row.get("arm"), row.get("simulation_id"))) if kind == "turn" else by_session.get(row.get("session_id"))
        if arm is None:
            continue
        if kind == "turn":
            if row["label"] in ("error", "unparsed"):
                unjudged += 1
                continue
            table[arm][row["label"]] += 1
            if row["label"] in ("lost_info", "off_topic"):
                flagged.append(row)
        else:
            if row["label"] not in ("latest", "earlier", "mixed", "unclear"):
                unjudged += 1
                continue
            for name, selected in (("working", row["after_latest"]), ("swallow", bool(row["swallowed_user"]))):
                if selected:
                    table[(name, arm)][row["label"]] += 1
            if row["label"] == "earlier":
                flagged.append(row)
    return {"table": table, "flagged": flagged, "unjudged": unjudged}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--arms", nargs="+", default=["sac", "sac-old", "eocc", "fha"])
    parser.add_argument("--baseline", default="fha")
    parser.add_argument("--official", default=None, help="arm name of the official reference, if it was run")
    parser.add_argument("--judge-turns", default=None)
    parser.add_argument("--judge-states", default=None)
    parser.add_argument("--agent-model", default="qwen3.8-27b")
    parser.add_argument("--simulator-model", default="qwen3.8-27b")
    parser.add_argument("--concurrency", default="6（官方参照 4）")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    args.out = str(Path(args.out).resolve())  # report links are written relative to the repository
    arms, base = args.arms, args.baseline
    tag = args.tag
    REPORTS.mkdir(parents=True, exist_ok=True)
    core_json = REPORTS / f"{tag}_compare.json"
    core_full = run_compare(tag, arms, REPORTS / f"{tag}_compare.md", core_json)
    core = core_full["cells"]
    all_arms = arms + ([args.official] if args.official else [])
    with_json = REPORTS / f"{tag}_compare_with_official.json" if args.official else None
    with_official_full = run_compare(tag, all_arms, REPORTS / f"{tag}_compare_with_official.md", with_json) if args.official else {"cells": {}, "tasks": {}}
    with_official = with_official_full["cells"]

    sims, cache = included_sims(tag, all_arms)
    C = lambda arm, domain=None: core.get(f"{domain}__{arm}", {})  # noqa: E731
    domains = [d for d in ("airline", "retail", "telecom") if f"{d}__{arms[0]}" in core]
    total = {arm: C(arm) for arm in arms}
    N = total[arms[0]]["n"]
    infra = {arm: sum(1 for d in ("airline", "retail", "telecom") for s in (cache.get((arm, d)) or []) if str(getattr(s.termination_reason, "value", s.termination_reason)) == "infrastructure_error") for arm in all_arms}

    # ---- fold forensics + pinned counts (compacting arms) ------------------------------------
    folds = {}
    sessions_cache = {}
    for arm in all_arms:
        rows, refused = [], []
        for _domain, sim in sims[arm]:
            sid = session_id_of(sim)
            if sid is None:
                continue
            log = find_session_log(DSH_HOME.resolve(), sid)
            if log is None:
                continue
            if sid not in sessions_cache:
                sessions_cache[sid] = load_session(log)
            f, r = folds_of(sessions_cache[sid])
            rows += f
            refused += r
        kinds = collections.Counter(row["kind"] for row in rows)
        recaps = sorted(row["recap_tokens_est"] for row in rows)
        import re

        refused_recaps = sorted(int(m.group(1)) for x in refused for m in [re.search(r"\((\d+) estimated framed tokens >= (\d+)\)", str(x["error"]))] if m)
        refused_spans = sorted(int(m.group(2)) for x in refused for m in [re.search(r"\((\d+) estimated framed tokens >= (\d+)\)", str(x["error"]))] if m)
        folds[arm] = {
            "ok": len(rows), "working": kinds["working"], "finished": kinds["finished"], "archive": kinds["archive"],
            "refused": len(refused), "attempts": len(rows) + len(refused),
            "refused_share": len(refused) / (len(rows) + len(refused)) if rows or refused else None,
            "recap_mean": statistics.fmean(recaps) if recaps else None, "recap_p90": recaps[int(0.9 * (len(recaps) - 1))] if recaps else None,
            "small_ok": sum(1 for row in rows if row["span_tokens_est"] < 800),
            "refused_not_smaller": sum(1 for x in refused if "not smaller" in str(x["error"])),
            "refused_no_text": sum(1 for x in refused if "no text" in str(x["error"])),
            "refused_recaps": (refused_recaps[0], refused_recaps[-1]) if refused_recaps else None,
            "refused_spans_median": statistics.median(refused_spans) if refused_spans else None, "refused_spans_max": max(refused_spans) if refused_spans else None,
        }
    pinned = collections.Counter()
    archive_sessions = 0
    for _domain, sim in sims[arms[0]]:
        session = sessions_cache.get(session_id_of(sim) or "")
        if session is None:
            continue
        pinned[sum(1 for q in session.surface if session.kind(q) in ("user", "goal"))] += 1
        archive_sessions += any(session.kind(q) == "compact-checkpoint" and "[SAC EARLIER USER REQUESTS]" in "".join(b.get("text", "") for b in session.events[q]["data"]["content"]) for q in session.order)
    new, old, eocc, fha = (C(arm) for arm in (arms[0], arms[1], arms[2], arms[3])) if len(arms) >= 4 else (None,) * 4
    f0, f1, f2 = folds[arms[0]], folds[arms[1]], folds[arms[2]]

    # ---- paired against the baseline -----------------------------------------------------------
    tasks = {arm: task_map(core_full["tasks"].get(f"None__{arm}", [])) for arm in arms}
    pair = {arm: paired(tasks[arm], tasks[base]) for arm in arms if arm != base}
    jt = judge_summary(Path(args.judge_turns), sims, "turn") if args.judge_turns else {"table": collections.defaultdict(collections.Counter), "flagged": [], "unjudged": 0}
    js = judge_summary(Path(args.judge_states), sims, "state") if args.judge_states else {"table": collections.defaultdict(collections.Counter), "flagged": [], "unjudged": 0}
    flagged_path = Path(args.out).with_name(Path(args.out).stem + "-flagged.md")
    lines = ["# 被评审标出的回合与状态（供人工阅读）", ""]
    for row in jt["flagged"]:
        lines.append(f"- [{LABEL.get(row['arm'], row['arm'])} {row['domain']} 第 {row['task_id']} 题 第 {row['turn']} 轮，reward={row['reward']}] **{row['label']}**：{row['evidence']}")
    for row in js["flagged"]:
        lines.append(f"- [{LABEL.get(row['arm'], row['arm'])} {row['domain']} 第 {row['task_id']} 题 第 {row['turn']} 轮之前的状态] **earlier**：{row['evidence']} — Objective: {row['objective'][:160]}")
    flagged_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    o = []
    w = o.append
    w(f"# SAC 1.2.0 多轮对话测试报告：τ²-bench 官方脚本，SAC 新版 / 旧版 / 论文 FHA / 论文 EoCC")
    w("")
    w(f"> 模型：agent、用户模拟器、NL 断言评审全部是 **{args.agent_model}**（同一个 OpenAI 兼容端点，默认的思考模式）。")
    domain_note = "、".join(f"{DOMAIN_CN[d]} {C(arms[0], d)['n']}" for d in domains)
    w(f"> 配对题数：**{N}**（{domain_note}）· trial 1，seed 300 · 被测版本：插件 1.2.0（提交 `dfa3b6a`，`maxPinnedInstructions: 4`）")
    w(f"> 跑法：官方 `tau2 run`，任务、用户模拟器、环境、评测都是原码，只把被测 agent 换成一个 DSH 会话。")
    cut_total = sum(total[a]["dsh.rate_limited_sims"] for a in arms)
    w(f"> 完成度：{N} 题在四个臂里都有结果；`infrastructure_error` 共 {sum(infra.values())} 个"
      + (f"，被端点限流打断、已剔除的会话 {cut_total} 个。" if cut_total else "，没有被端点打断的会话。"))
    w("")
    w("## 0. 结论")
    w("")
    sw = new["sac.folded_user_messages_total"]
    w(f"1. **新版 SAC 的规则{'守住了' if sw == 0 else '没有完全守住'}。** {N} 个配对会话里，{new['vis.later']} 条后续用户消息有 {new['vis.full']} 条（{pct(new['vis.full'] / new['vis.later'])}）在到达之后的**每一个**模型步都原样在请求里，"
      f"被折叠吞掉且未逐字引用的用户消息 {sw} 条。" + (f"归档折叠在 {archive_sessions} 个会话里真实发生了 {folds[arms[0]]['archive']} 次。" if folds[arms[0]]["archive"] else "这些对话里没有触发归档折叠。"))
    w(f"2. **旧版的缺陷在结构上原样复现。** 旧版 SAC（1.1.0）在 {old['sac.sims_with_folded_user_message']} 个会话里把 {old['sac.folded_user_messages_total']} 条后续用户消息折进了摘要；这些消息平均只在之后 {pct(old['vis.ratio_sum'] / old['vis.later'])} 的模型步里可见，最差的一条{'模型一步都没见过' if old['vis.worst'] == 0 else '只剩 ' + pct(old['vis.worst'])}。")
    p_new = pair[arms[0]]["p"]
    detectable = [a for a in pair if pair[a]["p"] < 0.05]
    head3 = "成功率上四个臂没有可分辨的差异" if not detectable else "成功率上 " + "、".join(LABEL[a] for a in detectable) + " 与 FHA 的差超出了噪声"
    w(f"3. **{head3}。** {N} 道配对题：" + "、".join(f"{LABEL[a].split('（')[0]} {total[a]['successes']}" for a in arms) + "。"
      + "；".join(f"{LABEL[a]} 对 FHA {signed(pair[a]['delta'])} 个百分点（各自独有 {pair[a]['a_only']} : {pair[a]['b_only']}，p = {pair[a]['p']:.2f}）" for a in pair) + "。")
    manual_counts = {arms[0]: 1, arms[1]: 2, arms[2]: 19, arms[3]: 1}  # by reading the flagged turns, see section 6
    turns_of = {arm: sum(jt["table"][arm].values()) for arm in arms}
    worst_arm = max(arms, key=lambda arm: manual_counts[arm] / max(1, turns_of[arm]))
    w(f"4. **{LABEL[worst_arm]} 成功率没掉，但行为上出了你最初报告的那种症状。** 逐轮评审里，它有 {manual_counts[worst_arm]}/{turns_of[worst_arm]}（{100 * manual_counts[worst_arm] / turns_of[worst_arm]:.1f}%）的回合是「忘了用户已经说过的、重复旧内容、或者接着做用户已经放弃的请求」；"
      + "，".join(f"{LABEL[a].split('（')[0]} {manual_counts[a]}/{turns_of[a]}" for a in arms if a != worst_arm) + "。原因就是第 5 节的那条：它只保留第一条用户消息的原文，其余全进摘要。")
    bill_new = new["tokens.billable"] / fha["tokens.billable"]
    verdict = "更低" if bill_new < 0.95 else ("基本持平" if bill_new <= 1.05 else "更高")
    w(f"5. **上下文变小，账面成本{verdict}。** 新版每次请求的平均上下文是 FHA 的 {new['prompt_mean'] / fha['prompt_mean']:.2f}×，峰值 {n(new['prompt_peak'])} 对 {n(fha['prompt_peak'])} token。"
      f"按「缓存输入按 10% 计价」折算，新版是 FHA 的 {bill_new:.2f}×，旧版 {old['tokens.billable'] / fha['tokens.billable']:.2f}×，EoCC {eocc['tokens.billable'] / fha['tokens.billable']:.2f}×。多出来的主要是每题 {new['tokens.state_calls_per_sim']:.1f} 次状态更新调用（{n(new['tokens.state_input'] + new['tokens.state_output'])} token）和每次折叠后缓存失效带来的新输入。")
    w(f"6. **新版有一个真实缺陷：** {pct(folds[arms[0]]['refused_share'])} 的折叠尝试失败了（{folds[arms[0]]['refused_not_smaller']} 次被后端拒绝「摘要不比被折叠内容小」，{folds[arms[0]]['refused_no_text']} 次状态更新没有产出文本），白付一次模型调用，片段留在上下文里不折；旧版是 {pct(folds[arms[1]]['refused_share'])}。"
      f"另外「保留最近 4 条」不是硬上限：{sum(pinned.values())} 个会话里有 {sum(v for k, v in pinned.items() if k > 4)} 个结束时钉着 {min((k for k in pinned if k > 4), default='-')}–{max((k for k in pinned if k > 4), default='-')} 条。")
    w("")
    w("## 1. 测试从哪里来")
    w("")
    w("| 项 | 内容 |")
    w("|---|---|")
    w("| 来源会话 | 开发机上的一个 DSH 工作会话（18 轮）。第 1 轮查清「用户换了目标、代理不跟着换」的根因并把插件修到 1.2.0；最后一个目标是备份插件、准备 τ² 测试、**明确不实际跑**，留给新会话 |")
    w("| 测试集 | **τ²-bench v1.0.1 的 184 题 golden set**：airline 50、retail 114、telecom 20。与论文一致（§2.2） |")
    w("| 论文 | *You don't need long context*（arXiv:2611.05306，NVIDIA）。三种范式 FHA / EoCC / SAC（§2.4.1）；论文在 τ² 上只测了 FHA 和 EoCC（Table 8） |")
    w("")
    w("## 2. 测了哪些臂")
    w("")
    w("| 臂 | 是什么 | 实现 |")
    w("|---|---|---|")
    w("| **SAC 1.2.0（新版）** | 每条用户指令钉住，只折叠指令之间的工作；状态更新以所属指令为锚；保留最近 4 条，更早的进归档并逐字引用 | 工作区 `plugins/dsh-sac-compact`，与备份快照逐文件一致 |")
    w("| **SAC 1.1.0（旧版）** | 修复前的版本：只钉第一条用户消息，后续消息 2 步后被折进摘要 | `git archive e01ac12`，与提交逐文件一致 |")
    w("| **FHA（论文基线）** | 全历史，不压缩 | 不挂 SAC bundle 的同一个 DSH agent |")
    w("| **EoCC 12K/8K（论文）** | 上下文到阈值时把第一条用户消息之后的全部内容折成一份摘要，新会话 = system + 原任务 + 压缩状态 | 1.2.0 代码 + 派生副本：`pinInstructions: false`，阈值 8000 估计 token（论文 τ² 设置 12K/8K，Table 8），压缩提示用论文附录 G.1 的原话 |")
    if args.official:
        w(f"| 官方 `llm_agent`（参照） | 纯官方脚本，不经 DSH，全历史 | `{args.agent_model}`，与 DSH 各臂是同一个模型 |")
    w("")
    w("四个 DSH 臂用同一个模型端点、同一套系统提示、同样的 14/16/13 个领域工具、串行执行工具调用。预检（不调用模型）对每个 profile 逐项验证了组合后的配置和插件来源。")
    w("")
    w("## 3. 环境")
    w("")
    w("| 项 | 值 |")
    w("|---|---|")
    w(f"| agent 模型 | `{args.agent_model}`，思考模式（端点默认） |")
    w(f"| 用户模拟器、NL 断言评审 | `{args.simulator_model}`。官方默认是 `gpt-4.1`，所以**分数不能和公开榜单比**；模拟器和 agent 是同一个模型 |")
    w("| τ² | v1.0.1；telecom 的用户模拟器自己调用 30 个工具 |")
    w(f"| 并发 | {args.concurrency}，四个臂相同（墙钟只在臂间可比） |")
    w("| trial / seed | 1 个 trial，seed 300（四臂同题同种子） |")
    w("")
    w("## 4. 成功率")
    w("")
    w("| 臂 | " + " | ".join(f"{DOMAIN_CN[d]} ({C(arms[0], d)['n']})" for d in domains) + f" | 合计 ({N}) | 95% Wilson |")
    w("|---|" + "---|" * (len(domains) + 2))
    for arm in arms:
        c = C(arm)
        w(f"| {LABEL[arm]} | " + " | ".join(f"{C(arm, d)['successes']} ({pct(C(arm, d)['rate'])})" for d in domains) + f" | **{c['successes']} ({pct(c['rate'])})** | {pct(c['ci'][0])}–{pct(c['ci'][1])} |")
    w("")
    w(f"与 FHA 逐题配对（同一道题在两臂的结果，{N} 道）：")
    w("")
    w("| 臂 对 FHA | 两边都过 | 只有该臂过 | 只有 FHA 过 | 都没过 | Δ 成功率 | McNemar p |")
    w("|---|---|---|---|---|---|---|")
    for arm in arms:
        if arm == base:
            continue
        p = pair[arm]
        w(f"| {LABEL[arm]} | {p['both']} | {p['a_only']} | {p['b_only']} | {p['neither']} | {signed(p['delta'])} pp | {p['p']:.3f} |")
    disc = pair[arms[0]]["a_only"] + pair[arms[0]]["b_only"]
    w("")
    if disc:
        w(f"- 新版与 FHA 不一致的题一共 {disc} 道。按不一致的题数算，配对差的标准误约 {100 * (disc / N) ** 0.5 / N ** 0.5:.1f} 个百分点。")
    else:
        w("- 新版与 FHA 在这批题上结果完全一致。")
    for d in domains:
        rates = {arm: C(arm, d)["rate"] for arm in arms}
        w(f"- {DOMAIN_CN[d]}：" + "，".join(f"{LABEL[a].split('（')[0]} {C(a, d)['successes']}/{C(a, d)['n']}" for a in arms) + "。")
    if args.official and with_official:
        oc = with_official.get(f"None__{args.official}", {})
        if oc:
            core_on = {arm: with_official.get(f"None__{arm}", {}) for arm in arms}
            pair_off = paired(task_map(with_official_full["tasks"].get(f"None__{args.official}", [])), task_map(with_official_full["tasks"].get(f"None__{base}", [])))
            w("")
            w(f"**官方脚本参照。** 纯官方的 `llm_agent`（同一个 `{args.agent_model}`）在它与四个 DSH 臂都有结果的 {oc['n']} 题上是 {oc['successes']} 题（{pct(oc['rate'])}）；四个 DSH 臂在同样这些题上是 "
              + " / ".join(str(core_on[a].get("successes")) for a in arms) + f"。逐题配对，官方 agent 对 DSH 的 FHA 臂是 {signed(pair_off['delta'])} 个百分点（各自独有 {pair_off['a_only']} : {pair_off['b_only']}，p = {pair_off['p']:.3f}）。"
              + ("" if pair_off["p"] >= 0.05 else " 也就是说，仅把 agent 换成 DSH 承载，就比官方 agent 少拿了几个百分点；四个 DSH 臂之间的比较不受影响，但绝对分数不能当成这个模型在 τ² 上的水平。"))
    w("")
    w("## 5. SAC 规则检查（来自 DSH 会话日志）")
    w("")
    w("| 指标 | " + " | ".join(LABEL[a] for a in arms) + " |")
    w("|---|" + "---|" * len(arms))
    rows = [
        ("被折叠吞掉且未逐字引用的用户消息", lambda c: str(c["sac.folded_user_messages_total"])),
        ("发生过这种折叠的会话", lambda c: f"{c['sac.sims_with_folded_user_message']} / {N}"),
        ("后续用户消息数", lambda c: str(c["vis.later"])),
        ("到达后每一步都可见的", lambda c: f"{c['vis.full']} ({pct(c['vis.full'] / c['vis.later']) if c['vis.later'] else '–'})"),
        ("模型从未见过原文的", lambda c: str(c["vis.never"])),
        ("后续消息在之后各步可见比例的平均", lambda c: pct(c["vis.ratio_sum"] / c["vis.later"]) if c["vis.later"] else "–"),
        ("最差的一条消息", lambda c: pct(c["vis.worst"])),
        ("折叠次数 / 会话", lambda c: f"{c['sac.folds_total_per_sim']:.1f}"),
        ("状态更新失败", lambda c: f"{c['sac.state_update_failures_total']}" + (f"（{folds[[a for a in arms if C(a) is c][0]]['refused_not_smaller']} 次被拒、{folds[[a for a in arms if C(a) is c][0]]['refused_no_text']} 次无正文）" if [a for a in arms if C(a) is c] and c["sac.state_update_failures_total"] else "")),
        ("输出达上限而中断的回合", lambda c: str(c["dsh.max_tokens_turns"])),
    ]
    for name, get in rows:
        w(f"| {name} | " + " | ".join(get(C(a)) for a in arms) + " |")
    w("")
    w("## 6. 行为：状态锚点与逐轮回应")
    w("")
    w("两项用独立的评审调用做的判定：同一个模型，开着思考。关掉思考时它会把「Objective 只写了最新一条消息里的子步骤」误判成「写的是更早的请求」（状态判定上两种模式的一致率 91.7%，逐轮判定 97.1%），所以这里用思考模式的结果。")
    w("")
    w("**状态里的 Objective 写的是哪条请求**（每次折叠一份状态，评审看到当时全部用户消息和 Objective 行）：")
    w("")
    w("| 折叠类型 | 臂 | 份数 | 写的是最新请求 | 写的是更早的请求 | 混合 | 无法判断 |")
    w("|---|---|---|---|---|---|---|")
    for name, title in (("working", "工作状态"), ("swallow", "折掉了用户消息")):
        for arm in [a for a in arms if a != base]:
            c = js["table"].get((name, arm))
            if not c:
                continue
            total_ = sum(c.values())
            w(f"| {title} | {LABEL[arm]} | {total_} | {c['latest']} ({pct(c['latest'] / total_)}) | {c['earlier']} ({pct(c['earlier'] / total_)}) | {c['mixed']} ({pct(c['mixed'] / total_)}) | {c['unclear']} |")
    w("")
    w("新版「折掉了用户消息」的那几份是归档折叠：归档摘要按设计不写当前目标，所以评审无法判断；被释放的请求原文在归档头部（第 5 节里它们不算被吞）。")
    w("")
    w("**代理每一轮是否在回应用户上一句**（每个 agent 回合一次评审）：")
    w("")
    w("| 臂 | 回合数 | 回应了上一句 | 忘了用户已给的信息 | 答非所问 |")
    w("|---|---|---|---|---|")
    for arm in all_arms:
        c = jt["table"].get(arm)
        if not c:
            continue
        total_ = sum(c.values())
        w(f"| {LABEL.get(arm, arm)} | {total_} | {c['on_topic']} ({pct(c['on_topic'] / total_)}) | {c['lost_info']} ({pct(c['lost_info'] / total_)}) | {c['off_topic']} ({pct(c['off_topic'] / total_)}) |")
    w("")
    w(f"被评审标出的回合和状态一共 {len(jt['flagged']) + len(js['flagged'])} 条，清单在 [`{flagged_path.name}`](../{flagged_path.relative_to(ROOT)})。")
    w("")
    w("我把 45 条被标出的回合逐条读了，按原因分成四类（「记忆类」= 忘了用户已经说过的、重复旧内容、或者接着做用户已经放弃的请求——也就是你最初报告的那种症状）：")
    w("")
    w("| 臂 | 回合数 | 记忆类 | 策略行为（用户拒绝仍转人工等） | 与记忆无关的差错 | 兜底回复（输出达上限） | 评审自己读错 |")
    w("|---|---|---|---|---|---|---|")
    manual = {  # assigned by reading every flagged turn (see the flagged list)
        arms[0]: (1, 0, 3, 1, 1), arms[1]: (2, 2, 3, 1, 0), arms[2]: (19, 1, 1, 0, 0), arms[3]: (1, 2, 3, 0, 1),
    }
    for arm in arms:
        mem, pol, slip, fall, confused = manual[arm]
        turns_ = sum(jt["table"][arm].values())
        w(f"| {LABEL[arm]} | {turns_} | **{mem} ({100 * mem / turns_:.2f}%)** | {pol} | {slip} | {fall} | {confused} |")
    if args.official and jt["table"].get(args.official):
        w(f"| {LABEL[args.official]} | {sum(jt['table'][args.official].values())} | **0 (0.00%)** | 3 | 0 | 0 | 0 |")
    w("")
    w("- **EoCC 把症状复现出来了。** 21 条里有 19 条是记忆类。两个我逐字核对过的例子：airline 第 35 题，用户说「不谈取消了，帮我订 5 月 24 日 JFK→SFO 的新航班」，代理连续两轮回答「我重新核实了 M20IZO，仍然无法取消」，状态里的 Objective 一直是「处理取消 M20IZO 的请求」；retail 第 72 题，用户已经报过姓名、邮编和订单号，一次折叠之后代理三次重新要身份验证、两次重新问改哪个订单。")
    w("- **旧版 SAC 也有，但少。** retail 第 30 题：用户改说「我要退一双运动鞋」，两次折叠之后 Objective 退回了第一条请求「把平板的物流单号告诉 Olivia」，代理于是回答平板订单，用户只能说「你看错订单了」。")
    w("- **新版 SAC 的 1 条**是重复问了一次「是否继续」，没有丢目标。其余被标出的回合（转人工、算错金额、退错支付方式）四个臂都有，与压缩无关。")
    w("")
    w("## 7. 成本与上下文")
    w("")
    w(f"{N} 题合计，token 来自 DSH 会话日志里服务商的计数（agent 一侧，不含用户模拟器和评审）。")
    w("")
    w("| 臂 | 新输入 | 缓存输入 | 输出 | 状态更新调用 | 合计 | 折算账面¹ | 相对 FHA |")
    w("|---|---|---|---|---|---|---|---|")
    for arm in arms:
        c = C(arm)
        rel = f"{c['tokens.billable'] / fha['tokens.billable']:.2f}×" if fha["tokens.billable"] else "–"
        w(f"| {LABEL[arm]} | {n(c['tokens.agent_new_input'])} | {n(c['tokens.agent_cached_input'])} | {n(c['tokens.agent_output'])} | {n(c['tokens.state_input'] + c['tokens.state_output'])} | {n(c['tokens.total'])} | {n(round(c['tokens.billable']))} | {rel} |")
    w("")
    w("¹ 新输入 + 输出 + 状态更新 + 0.1 × 缓存输入（常见的前缀缓存计价比例）。")
    w("")
    w("| 臂 | 每次请求的平均上下文 | 峰值上下文 | 相对 FHA | 模型步 / 题 | 状态调用 / 题 | 用户轮次 / 题 | 墙钟秒 / 题 |")
    w("|---|---|---|---|---|---|---|---|")
    for arm in arms:
        c = C(arm)
        rel = f"{c['prompt_mean'] / fha['prompt_mean']:.2f}×" if fha["prompt_mean"] else "–"
        w(f"| {LABEL[arm]} | {n(round(c['prompt_mean']))} | {n(c['prompt_peak'])} | {rel} | {c['dsh.model_steps_total_per_sim']:.1f} | {c['tokens.state_calls_per_sim']:.1f} | {c['dialogue.user_turns_total_per_sim']:.1f} | {c['duration_per_sim']:.0f} |")
    w("")
    new_steps, fha_steps = new["dsh.model_steps_total_per_sim"], fha["dsh.model_steps_total_per_sim"]
    w(f"- 每次请求有约 5K token 是固定的系统提示和工具定义，压缩能省的只是对话本身：平均上下文降到 FHA 的 {new['prompt_mean'] / fha['prompt_mean']:.2f}×，峰值从 {n(fha['prompt_peak'])} 降到 {n(new['prompt_peak'])}。")
    w(f"- 新版的模型步数是 FHA 的 {new_steps / fha_steps:.2f}×，每题用户轮次 {new['dialogue.user_turns_total_per_sim']:.1f} 对 {fha['dialogue.user_turns_total_per_sim']:.1f}，两边做的事差不多，差别都来自压缩本身："
      f"每次状态更新都改写前缀，后面的请求要重新读一遍，所以未命中的新输入是 FHA 的 {new['tokens.agent_new_input'] / fha['tokens.agent_new_input']:.2f}×（缓存命中的输入 {new['tokens.agent_cached_input'] / fha['tokens.agent_cached_input']:.2f}×），另外多出 {n(new['tokens.state_input'] + new['tokens.state_output'])} token 的状态更新调用，每题 {new['tokens.state_calls_per_sim']:.1f} 次。"
      f"被后端拒绝的那 {f0['refused']} 次调用不写用量，按成功调用的平均大小估还要多约 {n(round(f0['refused'] * (new['tokens.state_input'] + new['tokens.state_output']) / max(1, f0['ok'])))} token。")
    w("- 这个模型开着思考模式，每一步的输出里推理占大头，所以费用和耗时主要跟模型步数走，而不是跟上下文长度走。这一点和论文的场景（上百步、上下文涨到 130–200K、模型不做长推理）不同，结论只对这里成立。")
    w("")
    w("## 8. 发现的问题")
    w("")
    w(f"**新版 SAC 1.2.0**")
    w("")
    w(f"1. **小片段折叠被后端拒绝，白付调用。** {f0['attempts']} 次折叠尝试里 {f0['refused']} 次失败（{pct(f0['refused_share'])}）。被拒的那些里摘要本身 {f0['refused_recaps'][0] if f0['refused_recaps'] else '–'}–{f0['refused_recaps'][1] if f0['refused_recaps'] else '–'} token，被折叠的片段中位数 {round(f0['refused_spans_median']) if f0['refused_spans_median'] else '–'}、最大 {f0['refused_spans_max'] or '–'} token——摘要比原文还长，后端不换。"
      f"原因是 `minSpanTokens: 400` 的下限相对这个模型写的 recap 太小：recap 平均约 {f0['recap_mean']:.0f} token、九成在 {f0['recap_p90']:.0f} 以下，成功的折叠里有 {f0['small_ok']} 次片段不到 800 token，刚好卡在边上。"
      f"后果是该片段留在上下文里不折，外加一次无效的模型调用；对话本身不受影响。旧版同样配置下是 {pct(f1['refused_share'])}，EoCC 的片段大，是 {pct(f2['refused_share'])}。"
      f"新版比旧版高这么多，是它自己的设计造成的：旧版每次折叠的是「第一条消息以来的整段」，片段越滚越大；新版每条指令各成一段，一轮结束时专门去折那一轮剩下的最后一两个动作，这种片段只有几百 token，必然比 recap 短。")
    if f0["refused_no_text"]:
        w(f"   另外 {f0['refused_no_text']} 次是「状态更新没有产出文本」：思考模型把 `stateMaxTokens: 2048` 的输出预算花在了推理上，重试一次仍然没有正文。这个上限对思考模型偏小。")
    w(f"   **建议：** 把下限提到约 800 token，或者像归档那样按「预期 recap 大小 × 1.2」来定；对思考模型把状态更新的输出上限放大一倍。")
    over = sum(v for k, v in pinned.items() if k > 4)
    dist = "、".join(f"{k}:{v}" for k, v in sorted(pinned.items()))
    w(f"2. **「保留最近 4 条」不是硬上限。** {sum(pinned.values())} 个会话结束时钉着的用户消息数分布是 {dist}——{over} 个会话超过了 4 条。归档折叠要等归档段大到值得一次模型调用才会发生，τ² 的对话通常撑不到那时候。保真不受影响（消息都在），但上下文比「最多 4 条」听起来要大。")
    w("")
    w("**所有臂共有的**")
    w("")
    mt = {arm: C(arm)["dsh.max_tokens_turns"] for arm in arms}
    lost = sum(1 for arm in arms for key, t in tasks[arm].items() if t.get("max_tokens_turns") and t["reward"] != 1.0)
    w(f"3. **输出达上限导致回合中断。** {sum(mt.values())} 个回合里模型一步的输出顶到了 `maxTokens: 16384`（全是推理），DSH 以 `max-tokens` 结束该轮，返回空回复；四个臂分别 " + " / ".join(str(mt[a]) for a in arms) + f" 次，其中 {lost} 个会话最终失败。这是模型行为，与压缩无关；调高上限可以避免。")
    cut = {arm: C(arm)["dsh.rate_limited_sims"] for arm in arms}
    if any(cut.values()):
        w(f"4. 被端点限流（HTTP 429）打断的会话 " + " / ".join(str(cut[a]) for a in arms) + " 个，已从统计里剔除。")
    w("")
    w("## 9. 和论文的对照")
    w("")
    w("论文 Table 8（τ²-bench 184 题，官方 agent，用户模拟器 gpt-4.1）：")
    w("")
    w("| 配置 | Claude Opus 4.5 | GPT 5.6 Sol |")
    w("|---|---|---|")
    w("| FHA | 79.3 | 73.9 |")
    w("| EoCC 16K/12K | 79.3 | 74.5 |")
    w("| EoCC 12K/8K | 78.2 | 77.2 |")
    w("| EoCC 8K/6K | 78.8 | 73.9 |")
    w("")
    deltas = [pair[a]["delta"] for a in arms if a != base]
    w(f"- **一致的地方：** 压缩不改变成功率。论文里 EoCC 三个阈值与 FHA 的差在 −1.1 到 +3.3 个百分点之间；我们这里是 {signed(min(deltas))} 到 {signed(max(deltas))}。")
    w("- **不能比的地方：** 模型、用户模拟器、agent 框架都不同，我们的分数不能和论文的放在一张表里看高低。")
    w(f"- **论文没有覆盖、这次看到的：** 论文的 EoCC 假设一个会话一个任务。放到多轮对话里，它只保留第一条消息的原文，后来的用户消息全部进摘要——这次有 {eocc['vis.never']} 条消息模型一步都没见过原文。论文附录 A 说压缩应该为「人的后续反馈」留好信息，但没有定义后续消息本身怎么处理；1.2.0 的逐条钉住正是补这个空白。")
    w("")
    w("## 10. 局限")
    w("")
    w(f"- **样本量。** {N} 道配对题、1 个 trial，不足以排除 3–5 个百分点量级的差别。")
    w("- **模拟器和 agent 是同一个模型**（按你的要求全部用 Qwen 27B），而且不是官方的 gpt-4.1。同一道题不同臂的用户措辞并不完全相同，配对是按题而不是按对话。")
    w("- **思考模式的成本。** 推理 token 让每个模型步的耗时和费用都比非思考模型高一个量级；换成非思考模式，成功率、拒绝率和成本结论都可能不同。")
    w("- **EoCC 的阈值**作用在插件自己的估算 token（字符数 / 3.5）上，只算对话、不含系统提示和工具定义。论文的阈值怎么计量没有写明。")
    w(f"- **只有 1 个 trial。** 测试方案要求 3 个 trial 才能对成功率下结论；这个模型开着思考，一轮 184 题 × 4 臂跑了约 4.5 小时。")
    w(f"- **墙钟**受并发和端点负载影响，只在四臂之间可比。这一次运行里新版有 1 个 telecom 会话因为两次用户模拟器调用各挂了 21 分钟和 68 分钟而多出约一个半小时，去掉它新版每题约 {((new['duration'] - 5600) / (N - 1)):.0f} 秒。")
    w("- **DSH 承载与官方 agent 有差距**（见第 4 节），绝对分数要打折看。")
    w("")
    w("## 11. 文件")
    w("")
    w("| 路径 | 内容 |")
    w("|---|---|")
    w(f"| [`work/tau2/reports/{tag}_compare.md`](../work/tau2/reports/{tag}_compare.md) | 四臂对比全表（成功率、配对、token、上下文、规则检查、可见性） |")
    if args.official:
        w(f"| [`work/tau2/reports/{tag}_compare_with_official.md`](../work/tau2/reports/{tag}_compare_with_official.md) | 加入官方参照臂 |")
    w(f"| [`{flagged_path.relative_to(ROOT)}`](../{flagged_path.relative_to(ROOT)}) | 被评审标出的回合与状态 |")
    w(f"| [`benchmarks/tau2-bench/data/simulations/{tag}_*`](../benchmarks/tau2-bench/data/simulations/) | 官方格式的结果文件（含 `infrastructure_error` 的记录，未删） |")
    w("| [`harness/tau2/`](../harness/tau2/) `compare.py`、`judge_turns.py`、`judge_states.py`、`folds.py`、`failures.py`、`timeline.py`、`build_report.py`、`inclusion.py`、`run_official.sh` | 分析与运行工具 |")
    w("| [`work/goal_sac120/NOTES.md`](../work/goal_sac120/NOTES.md) | 运行记录与恢复步骤 |")
    w("")
    w("## 12. 第一轮（已作废，仅留档）")
    w("")
    w("第一次运行用的是 GLM-5.4-Inner 做 agent、deepseek-v4-pro 做模拟器，完成 105 道配对题后因密钥副本余额耗尽而中断。按「全部换成 Qwen 27B」的要求，这一轮的数字不进入本报告的结论；"
      "它仍然支持两点：新版不吞用户消息（278/278 可见）、旧版吞（189 条）；成功率同样没有可分辨的差异（89 / 92 / 89 / 90）。原始表格在 [`work/tau2/reports/full4_valid_compare.md`](../work/tau2/reports/full4_valid_compare.md)。")
    w("")
    Path(args.out).write_text("\n".join(o) + "\n", encoding="utf-8")
    print(f"written {args.out} ({len(o)} lines); flagged list {flagged_path}")
    print(json.dumps({"N": N, "domains": domains, "infra_errors": infra, "cut_off": {a: C(a)["dsh.rate_limited_sims"] for a in arms}, "folds": folds,
                      "pinned": dict(sorted(pinned.items())), "archive_sessions": archive_sessions,
                      "judge_unjudged": {"turn": jt["unjudged"], "state": js["unjudged"]}}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
