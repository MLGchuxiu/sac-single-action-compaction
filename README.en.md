# SAC — Single-Action Compaction: Agents That Don't Need Long Contexts

[简体中文](README.md) | **English**

> Reproduces and productizes the most aggressive context-management regime from the paper
> [*You don't need long context*](https://arxiv.org/abs/2611.05306) (arXiv:2611.05306, NVIDIA, 2026-11) —
> **Single-Action Compaction (SAC)**: every context carries only "task + current semantic state + next action",
> and each completed action is immediately folded into the state.
> This repository contains three things: **an installable DeepSeek Harness plugin**, **a test harness that
> reproduces the paper's mechanism verbatim**, and **independent validation reports on three standard benchmarks**.

| | |
|---|---|
| Paper | Belcak et al., *You don't need long context*, arXiv:2611.05306 (local copy: [paper.pdf](paper.pdf) / [paper.txt](paper.txt); reading notes: [docs/paper-notes.md](docs/paper-notes.md)) |
| Plugin | [`plugins/dsh-sac-compact`](plugins/dsh-sac-compact/README.md) (v1.1.0, 48 tests) + [`plugins/dsh-sac-preset`](plugins/dsh-sac-preset/README.md) (GUI preset) |
| Harness | [`harness/`](harness/): model client, token accounting, tool sandbox, 5 task families, 13 state-template versions |
| Benchmarks | Loong / SWE-bench Verified / τ²-bench ([benchmarks/](benchmarks/README.md), downloaded and verified), 4 iterations + a 128K optimization track |
| Models tested | qwen3.7-flash, deepseek-v4.1-flash, GLM-5.3-Inner (a locally deployed GLM 5.3) |
| Hard limits | Input cap **130,172** tokens, output cap **8,192** tokens (reproduction track) |

---

## 1. The Problem: What Long Contexts Really Cost

The compute cost of generating a token grows **linearly with its depth** in the context (an inherent property of attention). This is invisible on retail price lists — APIs charge a flat rate per token — but it is real on actual GPU serving:

- Paper **Figure 1** (H100 on-demand at $3.85/GPU·h, Nebius, 2026-09): serving at a **256K** instead of **16K** window, the cost per 1M output tokens is **3.6×** for GLM 5.2, **5.6×** for Kimi K2.6, and **7×** for Qwen 3 Coder Next. That gap rivals or exceeds many orthogonal optimizations (aggressive quantization, structured sparsity).
- Paper **Figure 2** (DGX Spark, local): decode latency for later tokens is roughly **2×** that of earlier tokens, with memory footprint rising in step.
- Meanwhile model windows keep inflating (paper Table 1: frontier models at 750K–1.05M, longer than all of *War and Peace*), and an agent's context grows linearly with the number of actions — **the bigger the window, the fuller it gets, the more every token costs**.

The paper's response is a rhetorical question: **do these windows actually need to be filled?** Its answer is no — and it pushes compaction frequency to the extreme (single-action compaction) without collapse. This project reproduces that claim end to end, and turns it into a plugin you can install and use daily.

## 2. Principle: From "Full History" to "Single Action"

### 2.1 Three context-management regimes (paper §2.4.1, Figure 3)

```
FHA   Full-History Accumulation (baseline; the mini-swe-agent default)
      [task][a1][r1][a2][r2][a3][r3][a4][r4] ...              ← context grows without bound

EoCC  End-of-Conversation Compaction (threshold-triggered)
      [task][a1][r1][a2][r2] ──threshold──▶ [summary]
      [task][summary][a3][r3][a4][r4] ──threshold──▶ [summary'] ...   ← context held in a sawtooth

SAC   Single-Action Compaction (this project's focus)
      [task][state S0] [a1][r1] ▶ update to S1
      [task][state S1] [a2][r2] ▶ update to S2
      [task][state S2] [a3][r3] ▶ update to S3 ...            ← fold after every action
```

(`a` = action/tool call, `r` = observation)

- **FHA (Full-History Accumulation)**: one conversation appends forever; context grows linearly with steps until it hits the window or the budget.
- **EoCC (End-of-Conversation Compaction)**: when the compaction threshold is reached, the model writes a bounded handoff summary, then a **fresh conversation** starts (`system + original task + summary`). The paper denotes configs as `total-cap/threshold`, e.g. `16K/12K`.
- **SAC (Single-Action Compaction)**: the limit of compaction frequency. From the paper:

  > "Each conversation receives the task and current semantic state, performs exactly one
  > environment action, observes its result, and updates the semantic state. The harness then
  > discards that conversation and starts a fresh one for the next action."

  Each context contains only: **system + task + current semantic state → one action → observation → state update**, then the whole context is discarded.

### 2.2 Semantic state: why SAC works at all

SAC bets on one thing: **the information a long-horizon task truly needs to carry across steps is small**, and it can be expressed compactly in natural language. The paper's RQ2 experiments support this from the side — once long-input tasks are reduced to "selective reading", **conversation length barely grows with document length** (paper Figures 7/8).

So SAC replaces the raw history with a **semantic state**:

- It is **natural language** — a human can read it, diff it, audit it. This is the key difference between the paper's **SSSM (Semantic State Space Model)** concept and a classic SSM: the state transition is interpretable text, not a hidden vector.
- It is **bounded** — ≤2,048 tokens by default in this project (adaptive rule in §4.3), regardless of how many steps the task takes.
- It is **maintained by the model itself** — at each fold the model condenses "old state + just-completed action + observation" into the "new state".

### 2.3 The paper's empirical results (our reproduction target)

- **RQ1 (long-horizon tasks)**: Opus 4.5 is within −3.7~+5.6 pp of baseline, GPT 5.6 Sol −3.3~+4.4 pp — frequent compaction barely hurts accuracy while saving **2.2×–8.8×** cost.
- **RQ2 (long-input tasks)**: after pushing long inputs to the filesystem and reading them selectively with `grep/head/sed`, NIAH 60→100%, RULER 62.5→95.7% (Opus), BABILong +24.9 pp, with savings up to **233×**.
- **RQ3 (single-action compaction; paper Table 2, SWE-bench Verified / Terminal-Bench 2.1)**:

  | Model | mini baseline | **SAC** | Peak context: mini → **SAC** |
  |---|---|---|---|
  | Claude Opus 4.5 | 70.0 / 47.2 | **70.2 / 51.8** | 32K, 27K → **8K, 9K** |
  | GPT 5.6 Sol | 84.4 / 87.6 | **82.4 / 84.3** | 18K, 17K → **7K, 8K** |

  SAC compresses peak context from **130–200K down to 7–37K**; Opus holds or slightly improves, GPT loses only 2–3.3 pp. "Compaction frequency can be pushed to the extreme without collapse" is thereby established.

### 2.4 Failure modes the paper itself admits (§3.1 — implementations must face them)

- **Compaction is lossy**, and re-compacting already-compacted history compounds the loss.
- The paper's counterexample (coin flips): if the task needs "the result of the *i*-th coin flip", any **bounded** compaction must eventually drop it — not a budget problem, an information-theoretic lower bound.
- The paper's answer is not a bigger budget but an **architectural** one: archive every message and give the agent a **selective history-retrieval tool**, so "looking up history" becomes just another long-input task (the RQ2 reduction). Distinguish "must be in context" from "retrievable on demand" (need-to-know basis).

This project's `recall` tool and `ledger` are direct implementations of that answer (§4.4).

## 3. What This Repository Is

Three things, each independently usable:

1. **Productization**: [`plugins/dsh-sac-compact`](plugins/dsh-sac-compact/README.md) — a real DeepSeek Harness plugin. It hooks `agent/pre-step` and layers SAC as a policy on top of the already-mounted compaction backend; 48 unit/integration tests green. Install it and use it in daily sessions.
2. **Reproduction**: [`harness/`](harness/) — a verbatim reproduction and ablation testbed of the paper's mechanism. Hard budget of 130,172 input / 8,192 output tokens; all three regimes (FHA/EoCC/SAC) side by side; the evolution of **13 state-template versions (v1–v13)** (v10–v13 are research extensions whose validation-task data is not published).
3. **Independent validation**: [`benchmarks/`](benchmarks/README.md) + [`reports/`](reports/BENCH-REPORT.md) — the plugin runs inside real `dsh headless` sessions and is compared against a same-model FHA baseline on Loong, SWE-bench Verified, and τ²-bench: 4 iterations plus a 128K optimization track (12 tasks × n=5). **Every number can be reproduced by scripts in this repository.**

## 4. Algorithm Design

### 4.1 Overall architecture: a policy layer, not a second compaction backend

```
agent/pre-step (fires before every model request)
   │
   ├─ describeSurface(session)        project the session surface into ordered node descriptors
   │      [{seq, type, isCheckpoint, toolCallCount, textChars}, …]
   │
   ├─ planSacSpan(nodes, policy)      pure function: decide this round's fold span
   │      → {startIndex, endIndex, actions, estimatedTokens} | null
   │
   └─ ctx.compaction.compactRegion(start, end, agent, signal)
          └─ owned by the mounted backend (dsh-compaction-basic):
             durable transactions, locking, checkpoint messages, token accounting
```

The key decision: the plugin **does not implement a second compaction backend**; it drives the backend through its public `compactRegion()` API. Exactly one compaction implementation stays active per context, every durability and concurrency guarantee of the backend applies unchanged, and the plugin only makes **decisions** (when to fold, what range, how to write the state).

### 4.2 Fold-span planning — `planSacSpan()` ([lib/sac-policy.js](plugins/dsh-sac-compact/lib/sac-policy.js))

This is SAC's core algorithm, a **pure function** — input is the surface node descriptors, output is a fold span or `null`. No Cordis, session, or LLM dependencies, so it can be exhaustively unit-tested off-runtime. Four rules:

1. **Task anchor**: the span starts **after** "the first non-checkpoint user message". Everything at or before the anchor (system, the task itself) is never folded. This also means the state message written by the previous fold is **absorbed** — the new state is a merge of "old state + new action", not a stack of two states.
2. **Tool-pairing balanced cut**: the span's right end may only land on a boundary with no unanswered tool calls. The scan maintains an `openToolCalls` counter (+1 per tool call in an assistant message, −1 per tool result); only positions where it returns to zero become candidate cut points. **A tool call that has not received its reply is never folded away** — a hard correctness guarantee.
3. **Batching and retention**: `actionsPerCompaction` controls how many complete actions each fold absorbs (1 = the paper's strict SAC, 2 = SAC-2, the optimum on our benchmarks); `keepTrailingReply` keeps a final reply without tool calls out of the span (the user-visible answer never enters the state); `keepLastObservation` keeps the newest observation raw (ends the "folded it away → re-read it" loop, at the cost of a slightly higher peak context).
4. **Size guards and a hard safety net**: `minSpanChars` / `minSpanTokens` — rewriting the state itself costs hundreds of tokens, so folding a tiny span can end up larger; skip it. `contextBudgetTokens` — once the whole surface approaches the model window (e.g. 96K under a 128K window), **force a fold** as the last line of defense against misconfiguration.

### 4.3 Generating the semantic state: three modes

| Mode | Who writes the state | Extra calls | Notes |
|---|---|---|---|
| `native` | the backend's default summarizer | 1 per fold | most conservative; changes no summarization behavior in the session |
| `paper` | the paper's Appendix G.1 handoff template | 1 per fold | six sections: Objective / Established facts / Applied changes / Verification / Dead ends / Remaining plan; this project adds "output contract" and "verbatim values" sections so the state cannot rewrite key numbers |
| `inline` | **the model itself**, writing a `<state>…</state>` block at the head of its action reply | **0** | the v9 pipeline; the summarize hook extracts the block as the checkpoint, saving an entire model call |

**Adaptive budget**: the state's token cap is not a constant but `min(stateBudget, 640 + 48 × foldedActions)` — early in a task there are few facts and the state stays small; as folds accumulate the budget relaxes, so a late-stage state is never choked by an early small budget.

**Reasoning-model adaptation (empirical finding)**: reasoning models like GLM-5.3-Inner **emit no text block** in tool-calling replies, so `inline` fails broadly. Hence `inlineFallbackAfter` — after N consecutive failures to extract a `<state>` block, the plugin **stickily falls back** to `paper` mode instead of retrying forever.

### 4.4 Three mechanisms that make the state auditable (v9 pipeline; each independently toggleable)

Paper §3.1 only points in the "archive + retrieval" direction (§2.4); here it becomes three concrete mechanisms:

- **ledger**: a complete list of folded actions maintained by the **machine** (not the model) — tool names, full arguments, result digests, capped at 2,400 chars by default, prepended to every state. Effect: the model cannot **silently** drop an action; in real-session fidelity tests, adding the full ledger raised state fidelity from **0.19 to 0.90** ([REPORT-2.md](reports/REPORT-2.md)).
- **checklist**: 3–8 subgoals generated at task start; a `check(index)` tool lets the model tick items off, and the checklist is re-rendered into every state. Counters "forgot what's left to do".
- **recall**: a `recall(query)` tool that searches the **full archived session log** (including folded-away history) and returns snippets with sequence numbers. This is a direct implementation of the paper's "history lookup = another long-input task" — folded ≠ deleted.

### 4.5 Token accounting and the hard budget (test harness)

Every request on the reproduction track is bound by a **hard budget**: a script-aware estimator (CJK ≈ 1 token/char, Latin ≈ 1 token/3.6 chars) estimates first, then calibrates its coefficients **online** against the server's `usage.prompt_tokens`. When over budget, the emergency order is: trim the oldest oversized observation first, then drop whole "assistant+tool" groups, with every event counted in `budget_violations` / `emergency_trims`. Output is uniformly capped at `max_tokens=8192`. "Peak context" is therefore not an estimate but an auditable measured sequence.

### 4.6 Failure semantics: SAC must never break the conversation

Compaction is a bypass optimization, not a critical path. Every failure — backend not mounted, busy lock, unbalanced range, summarizer error, aborted signal — is caught, logged once per distinct message, and the step proceeds with the uncompacted history; the next step retries. `maxCompactionsPerTurn` (default 200) prevents runaway loops. Every fold leaves one auditable line in the session log:

```
dsh-sac-compact: folded 1 action(s) (seqs 12-15, ~812 tokens) into semantic state | folds=3 folded_tokens=2410 summaries=3 summary_chars=1902 installed=pre-step
```

`telemetrySnapshot(agent)` additionally exposes all counters (steps / compactions / foldedTokens / skipped+lastSkipReason / installAttempts / summarizeCalls…).

## 5. Improvements Over the Paper

The paper proved "feasible"; this project answers "**how to do it well, and when it breaks**". Each item below is something the paper did not do or did not provide data for:

1. **State-template ablation (v1–v13)**. The paper ships exactly one state template (Appendix G.1) with no sensitivity analysis. This project ran a systematic ablation across versions (fixed six sections → ACTION DIGEST → ledger/recall/adaptive/ANCHORS → checklist → inline → …), with the conclusion: **template quality affects accuracy more than compaction frequency itself** (see the two version lines in [docs/VERSIONS.md](docs/VERSIONS.md)).
2. **Inline state updates**. The paper's SAC pays **two** model calls per action (action + state update). v6 merges the state update into the action call (the model writes the `<state>` block at the head of its reply): **requests −68%, input tokens −62%**, both models 8/8 green — cutting the largest overhead of SAC by more than half.
3. **A machine-maintained ledger**. The paper only says "the model maintains the state"; in practice the model **silently drops** actions (v1 fidelity just 0.19 in real-session tests). Adding the deterministic ledger raises it to **0.90**. A hybrid of "model writes state" and "machine keeps books".
4. **The recall tool**. The paper's §3.1 failure-mode analysis (the information-theoretic bound of lossy compaction) suggests the solution but does not implement it; this project's `recall(query)` lands it and validates "folded ≠ lost".
5. **The engineering trade-off between batching and observation retention**. Strict SAC (a fold per action) costs 4.8× wall clock on SWE; `actionsPerCompaction: 2` + `keepLastObservation` brings it to 1.6× while keeping total tokens **−36%** and peak **−55%** vs FHA. The paper never explores this frequency–cost–latency trade-off surface.
6. **Four engineering pieces for a 128K window** (v1.1.0): `stateInputLean` (state-update requests stop replaying the system prompt + 22 tool schemas, saving ≈4.8K tokens per fold measured), `trimObservationOver` (trim only oversized observations; short tool output stays verbatim), `stateMaxWords` (cap the recap — it is re-sent on every later request), `contextBudgetTokens` (force-fold near the window). Combined: 12 tasks × n=5, total tokens **−58.6%**, peak **−61%**, with weighted score even **slightly up** (§6.2).
7. **Making the cost explicit**. The paper's Appendix C admits total tokens need not fall, but gives no "how many extra calls did we pay" comparison. The harness explicitly records `requests / actions / compactions`, turning "the duplicated labor paid for short contexts" into a measurable metric (Loong strict SAC: tokens +44% but peak 6.7K vs 29.3K — trade-offs like this are all laid out in the reports).

## 6. Validation Results

> All results are reproducible with the repository's scripts: `cd harness && python3 -m bench.matrix --bench loong,tau2,swe --arms sac,fha --iteration iterN --workers 4`.

### 6.1 Standard-benchmark track (GLM-5.3-Inner, real `dsh headless` sessions, 4 iterations)

> Model note: **GLM-5.3-Inner is a locally deployed GLM 5.3** (served through a local gateway with an OpenAI-compatible endpoint, 1M context window). All reasoning-model behavior reported here comes from that deployment.

| Benchmark | Config | SAC vs FHA |
|---|---|---|
| Loong (long-input reading) | strict SAC | peak **6.7K vs 29.3K**, accuracy 0.933 vs 0.80, total tokens +44% |
| SWE-bench Verified (3 instances) | strict SAC | 3/3 vs 3/3, peak 8.8K vs 14.1K, wall clock 4.8× |
| SWE-bench Verified | **SAC-2 + keep-last-observation** (iter4) | 3/3 vs 3/3, total tokens **0.79M vs 1.23M (−36%)**, peak −55%, wall clock 1.6× |
| τ²-bench (static-plan variant) | SAC-2 | 0.70 vs 0.57 (within noise), 0 folds |

Report: [BENCH-REPORT.md](reports/BENCH-REPORT.md) (includes the v9 spot-check and per-iteration data in [bench-all-iterations.md](reports/bench-all-iterations.md)).

### 6.2 128K optimization track (2026-11-17, full 12 tasks × n=5, 120 cells)

| Metric | FHA (full history) | Optimized SAC | Δ |
|---|---|---|---|
| Task-weighted mean score | 0.837 | **0.853** | +0.016 |
| Total tokens | 17,744,686 | **7,348,882** | **−58.6%** |
| Peak context (max) | 129,575 (2 of 3 full runs exceeded 128K) | **50,385** | **−61%** |
| SWE 3-task tokens | 14.97M | **4.82M** | −68%, peak 130K→37K |
| Loong 5-task mean | 0.848 | **0.885** | +0.037 |

Report: [BENCH-REPORT-128K.md](reports/BENCH-REPORT-128K.md); per-iteration log: [work/goal_round1/ITERATIONS.md](work/goal_round1/ITERATIONS.md).

### 6.3 Reproduction track (qwen3.7-flash / deepseek-v4.1-flash)

Iteration 1, 5-task snapshot (full 7 tasks × 3 regimes × 4 iterations in [REPORT.md](reports/REPORT.md)):

| Model | Regime | Pass | Mean score | Peak context | Total input tokens |
|---|---|---|---|---|---|
| qwen3.7-flash | FHA | 4/5 | 0.83 | 23.6K / **109.5K** | 2,024,260 |
| qwen3.7-flash | **SAC** (v1) | 4/5 | 0.94 | **1.9K** / 2.7K | **271,847** |
| deepseek-v4.1-flash | FHA | 5/5 | 1.00 | 8.3K / 24.3K | 450,050 |
| deepseek-v4.1-flash | **SAC** (v1) | 4/5 | 0.94 | **2.3K** / 3.3K | **224,779** |

Round 2 ([REPORT-2.md](reports/REPORT-2.md)) validated SAC under extreme conditions:

- **Million-token input** (a 992K-token / 1,287-file coding task): every regime solved it within **3–4K context** — more evidence that "long input ≠ long context";
- **Real work-session replay** (raw data not in this repository): neither FHA nor EoCC can hold the 130K hard budget, **only SAC never exceeds it**; the machine command ledger lifts state fidelity from 0.19 to **0.90**.

## 7. Quick Start

```bash
# Plugin tests (48 cases)
cd plugins/dsh-sac-compact && node --test test/sac-policy.test.js test/integration.test.js

# Single cell
python3 harness/run.py --models qwen3.7-flash --regimes sac --tasks code_edit --verbose

# Full matrix (2 models × 3 regimes × 5 tasks)
python3 harness/run.py --models qwen3.7-flash,deepseek-v4.1-flash \
    --regimes fha,eocc,sac --state-versions v1 --workers 4 --out results/iter1
python3 harness/report.py results/iter1/*.jsonl --out reports/iter1.md

# Standard-benchmark matrix (real dsh headless sessions)
cd harness && python3 -m bench.matrix --bench loong,tau2,swe --arms sac,fha --iteration iter0 --workers 6
```

**Enable per-session in the GUI**: install [`plugins/dsh-sac-preset`](plugins/dsh-sac-preset/README.md) (a standalone preset that only affects selected sessions) and pick "SAC 单动作压缩" when creating a session; other sessions are untouched. To verify: the session log should show `compaction/start → compaction/summary → compaction/end` and `dsh-sac-compact: folded N action(s)` lines.

Install at profile level:

```jsonc
// ~/.dsh/profiles/<profile>/package.json
{
  "dependencies": { "@local/dsh-sac-compact": "link:/absolute/path/to/plugins/dsh-sac-compact" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@local/dsh-sac-compact"] } }
}
```

Recommended configuration for a 128K window (these are the current defaults, and measured optimal): `statePrompt: paper` + `actionsPerCompaction: 2` + `keepLastObservation` + `trimObservationOver: 4000` + `stateMaxWords: 400` + `stateInputLean` + `contextBudgetTokens: 96000`. All 22 options are documented in the [plugin README](plugins/dsh-sac-compact/README.md#configuration).

## 8. Repository Layout

| Path | Contents |
|---|---|
| [`paper.pdf`](paper.pdf) / [`paper.txt`](paper.txt) | the paper and its extracted text |
| [`plugins/dsh-sac-compact/`](plugins/dsh-sac-compact/README.md) | **the SAC plugin**: policy layer + state pipeline + 48 tests |
| [`plugins/dsh-sac-preset/`](plugins/dsh-sac-preset/README.md) | GUI agent preset (enables SAC per session) |
| [`harness/sac/`](harness/sac/agent.py) | reproduction harness core (agent loop, prompts, tasks, token accounting) |
| [`harness/bench/`](harness/bench/runner.py) | three benchmark adapters + parallel matrix runner |
| [`benchmarks/`](benchmarks/README.md) | the paper's test sets (SWE-bench Verified 500 / τ²-bench 184 / LOONG 1,600) |
| [`reports/`](reports/BENCH-REPORT.md) | four benchmark rounds + reproduction reports + the 128K optimization report |
| [`docs/`](docs/paper-notes.md) | paper reading notes / version tables / benchmark methodology / ecosystem survey |
| [`work/`](work/goal_round1/ITERATIONS.md) | per-iteration data of the 128K optimization track |

Version naming note: the **plugin line** (plugin-iter0–iter6 / plugin-v9) and the **harness line** (harness-v1–v13) are two independent lines — check [docs/VERSIONS.md](docs/VERSIONS.md) before reading version numbers.

## 9. Limitations

- **Lossiness is fundamental** (paper §3.1): tasks requiring verbatim historical recall (e.g. "the result of the i-th coin flip") defeat any bounded state; pair with `recall` / external archives.
- When a single action's own observation exceeds the window, compaction cannot help; pair with an observation-pruning plugin (e.g. `dsh-compaction-tool-result-pruner`).
- `inline` mode is unavailable on reasoning models (no text block in tool-call replies); it already falls back stickily to `paper`.
- Strict SAC's wall-clock cost (more, smaller requests) is significant on SWE-style tasks (4.8×); SAC-2 + `keepLastObservation` brings it to 1.6× — a frequency-vs-latency trade-off, not a defect.
- τ²-bench uses a static single-turn variant (not the official multi-turn orchestration score); SWE-bench instances are a pure-Python subset runnable in a shared venv. Methods and caveats in [docs/bench-methodology.md](docs/bench-methodology.md).

## 10. Citation

The paper:

> Belcak, P., Muralidharan, S., Jiang, J., Fu, Y., Byeon, W., Ye, H., Angilly, R., Taghibakhshi, A., Liu, S.-Y., Ouyang, S., Heinrich, G., & Molchanov, P. (2026).
> *You don't need long context.* arXiv:2611.05306. https://arxiv.org/abs/2611.05306

All experiments, failure analyses, and the plugin implementation in this repository build on the mechanisms published in that paper.
