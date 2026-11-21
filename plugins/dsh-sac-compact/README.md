# @local/dsh-sac-compact

Single-Action Compaction (SAC) for the DeepSeek Harness — a context-compression policy that folds
**every completed action** into one compact, human-readable *semantic state*, so each model request
carries only

```
system prompt + task + semantic state + the single next action
```

instead of a growing transcript. Implements the regime from
[*You don't need long context*](https://arxiv.org/abs/2611.05306) (§2.4.1, §5).

## Why

Long contexts are linear-cost: in the paper's measurements, serving at 256K instead of 16K costs
3.6×–7× more per generated token and roughly doubles decode latency on local hardware. SAC keeps the
live context at a few thousand tokens while preserving task accuracy, because the agent carries an
explicit natural-language state rather than the raw history.

## How it works

```
agent/pre-step
   │
   ├─ describeSurface(session)        → ordered {seq, type, isCheckpoint, toolCallCount, chars}
   ├─ planSacSpan(nodes, policy)      → {startIndex, endIndex, actions, estimatedTokens} | null
   └─ ctx.compaction.compactRegion(start, end, agent, signal)
                                        │
                                        └─ durable bracket, surface replace, checkpoint marker,
                                           token accounting, locking (owned by the mounted backend)
```

The plugin is a **policy layer**, not a second compaction backend. It drives the mounted
`ctx.compaction` service (normally `dsh-compaction-basic`) through its public `compactRegion()` API,
so exactly one compaction implementation stays active per context and every durability guarantee of
the backend still applies.

The span always starts immediately **after the task message**, so a previously written state is
*updated* rather than duplicated, and it always ends on a **tool-pairing balanced cut**, so an
unanswered tool call is never folded away.

## v9 pipeline (inline state, ledger, checklist, recall)

`statePrompt: 'inline'` moves the state update into the model's own reply:

```
system prompt section: "before tool calls, begin every reply with <state>…</state>"
model reply:           <state>full recap</state> + tool calls
summarize() hook:      extract the block → checkpoint (no extra model call)
```

Because the model writes the state *before* the action, the action's own observation must stay raw
until the next reply folds it in — so `inline` forces `keepLastObservation: true`. If a reply omits
the block, the plugin injects one reminder (at most three per agent) and falls back to the paper
summarizer for that fold.

On top of inline, three optional pieces make the state auditable:

- **ledger** — a deterministic, cumulative list of folded tool calls and result heads (2400 chars by
  default), prepended to the state. The model cannot silently drop an action.
- **checklist** — a 3–8 item subgoal list generated once per task; the `check(index)` tool marks
  items, and the checklist is rendered into every state.
- **recall** — a `recall(query)` tool that searches the full archived session log (including folded
  history) and returns snippets with sequence numbers.

The state budget is adaptive (`min(stateBudget, 640 + 48 × foldedActions)`) so early folds stay
small and later folds get room for accumulated facts.

## Load order and observability

The state template is installed **lazily**: `apply()` makes a best-effort attempt, and the first
`agent/pre-step` retries. The compaction service lives in an isolated group that usually mounts after
`apply()` runs, so an eager-only install silently leaves the backend's default checkpoint template in
place — the session looks healthy while the paper template was never applied. A `Symbol.for(...)` mark
on the engine keeps the hook from being wrapped twice.

Each agent carries counters, readable through `telemetrySnapshot(agent)` (a copy):

| Counter | Meaning |
|---|---|
| `steps` | pre-step invocations seen by the policy |
| `compactions` / `foldedTokens` / `lastFoldActions` | folds performed and the estimated tokens they removed |
| `skipped` / `lastSkipReason` | folds the backend refused (lock, unbalanced range, …) |
| `installAttempts` / `installFailures` / `installedOn` | whether the state template really got installed, and when (`apply` / `pre-step`) |
| `summarizeCalls` / `summarizeChars` / `summarizeErrors` | what the state update actually cost |

Every fold logs one line with the same numbers, so a session log can be audited after the fact:

```
dsh-sac-compact: folded 1 action(s) (seqs 12-15, ~812 tokens) into semantic state | folds=3 folded_tokens=2410 summaries=3 summary_chars=1902 installed=pre-step
```

`stateUpdateInstalled(engine)` answers the load-order question directly.

## Install

```jsonc
// ~/.dsh/profiles/<profile>/package.json
{
  "dependencies": { "@local/dsh-sac-compact": "link:/absolute/path/to/plugins/dsh-sac-compact" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@local/dsh-sac-compact"] } }
}
```

The bundle ships [`cordis.patch.yml`](cordis.patch.yml), which inserts the `sac-compact` entry after
the base layer. Restart the app (or reload the profile) to activate it.

**Hot reload**: Desktop's HMR watches profile configuration but not linked plugin source
(`hmr.root: []`), so a source change needs a restart or a Plugin Manager toggle. The plugin's
install marker is revision-scoped, so a module replacement re-installs its `summarize()` hook
instead of trusting the previous module's mark.

## What's new in 1.1.0 (128K optimization, 2026-11-17)

- `stateInputLean` — the state-update request no longer replays the conversation's system prompt,
  tool schemas, or tool history (measured: ~11K → ~5K prompt tokens per fold, SWE total −24.6%).
- `trimObservationOver` — trim only oversized observations (doc reads), keep short tool output exact.
- `stateMaxWords` — cap the recap length (the recap is re-sent on every later request).
- `contextBudgetTokens` — hard fold trigger when the surface approaches the model window (96K for 128K).
- `inlineFallbackAfter` — after repeated missing `<state>` blocks, switch to the paper summarizer for
  the rest of the session instead of retrying inline forever.
- Paper recap template gains “7. Output contract” and “8. Verbatim values”.
- Fix: `stateMaxTokens` was read from the policy but never resolved by `resolvePolicy()`.
- New default config: `paper` + `actionsPerCompaction: 2` + `keepLastObservation` + the four options above.
- 48 tests (was 41).

## Recommended configuration for a 128K window (GLM-5.3-Inner, 2026-11-17)

```yaml
statePrompt: paper        # inline is unreliable on reasoning models (no text before tool calls)
actionsPerCompaction: 2   # batch two actions per fold
keepLastObservation: true # the newest observation stays raw
minSpanTokens: 400        # fold small spans too: a low threshold keeps context ~10K
trimObservationOver: 4000 # trim only >4 KB observations (doc reads, long logs)
stateMaxWords: 400        # recap is re-sent on every request: keep it short
stateInputLean: true      # do not replay system prompt + tool schemas on each state update
contextBudgetTokens: 96000# hard fold trigger under a 128K window
inlineFallbackAfter: 2
ledger: false
recall: false
checklist: false
```

Measured on the three downloaded benchmarks (12 tasks, n=1, same-window FHA control):
SWE total tokens **2.19M → 0.71M (−67%)**, peak **102.9K → 16.7K**; Loong score **0.80 → 0.93**,
tokens −17%; τ² unchanged (it never folds). Full report:
[`reports/BENCH-REPORT-128K.md`](../../reports/BENCH-REPORT-128K.md).

## Configuration

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. |
| `actionsPerCompaction` | `1` | Actions folded per compaction. `1` is strict SAC; raise it to batch. |
| `minSpanChars` | `1200` | Skip folds smaller than this (a pointless model call otherwise). |
| `minSpanTokens` | `0` | Skip folds estimated below this many tokens. A recap is itself hundreds of tokens, so folding a tiny span only makes the backend reject it as "not smaller"; `400` is a good production value. |
| `charsPerToken` | `3.5` | Estimator used by the span-size guard. |
| `maxCompactionsPerTurn` | `200` | Runaway guard per agent turn. |
| `keepTrailingReply` | `true` | Keep a final assistant reply (no tool call) out of the folded span. |
| `keepLastObservation` | `false` | Keep the newest tool result out of the fold, so the next request still sees what the last action produced. Cuts fold count/latency on long agentic tasks, at the cost of a higher peak context. |
| `statePrompt` | `native` | `native` keeps the backend summarizer; `paper` uses the Appendix G.1 handoff-recap template (one extra call per fold); `inline` extracts the model-written `<state>` block (no extra call). |
| `stateMaxTokens` | `2048` | Output cap for the paper state update. |
| `stateInputMaxChars` | `0` | Head/tail trim each observation in the state-update input (the newest message gets 1.5×). Helps reading-heavy tasks; hurts long coding tasks (the recap loses detail the agent then re-reads), so it is off by default. |
| `stateBudget` | `2048` | Token budget for the model-written state. |
| `adaptiveBudget` | `true` | Use `min(stateBudget, 640 + 48 × foldedActions)` instead of a fixed budget. |
| `ledger` | `false` | Prepend a deterministic, cumulative ledger of folded tool calls/results to the state. |
| `ledgerChars` | `2400` | Character cap for the rendered ledger. |
| `ledgerFullCommands` | `true` | Keep full tool arguments in the ledger (false truncates each to 160 chars). |
| `recall` | `false` | Register the `recall(query)` tool over the archived session log. |
| `checklist` | `false` | Generate a subgoal checklist and register the `check(index)` tool. |
| `trimObservationOver` | `0` | Only head/tail-trim a single observation when it exceeds this many chars. Keeps short test output exact while bounding 40 KB document reads. |
| `contextBudgetTokens` | `0` | Hard safety net for a fixed context window: once the whole surface reaches this many estimated tokens, force a fold even when the size guard would skip. Set it to ~75% of the model's window (e.g. `96000` for 128K). |
| `stateMaxWords` | `0` | Ask the paper summarizer for a recap of at most this many words. The recap is re-sent on every later request, so a terse recap is a direct per-request saving. |
| `inlineFallbackAfter` | `2` | After this many failed `<state>` extractions, stop trying inline and use the paper summarizer for the rest of the session. Reasoning models often emit no text block before tool calls, so `statePrompt: inline` degrades to paper on them. |

The state update is retried once when the model returns no text (reasoning models can spend the whole
output budget on hidden reasoning); the retry raises the cap to `4096` and asks for the recap directly.
`telemetrySnapshot(agent).summarizeRetries` counts those retries.

### `statePrompt: 'inline'`

This is the v9 pipeline. It adds a `ctx.systemPrompt.section()` telling the model to begin every
tool-calling reply with a `<state>…</state>` block, and overrides the backend's `summarize()` hook to
extract that block instead of calling the model again. The final answer (no tool call) is exempt, so
the user never sees the state block. If the block is missing, the plugin falls back to `paper` for
that fold and nudges the model once.

### `statePrompt: 'paper'`

When enabled, the plugin overrides the mounted backend's documented `summarize()` hook with the
paper's instruction (Objective / Established facts / Applied changes / Verification / Dead ends /
Remaining plan). This is the same extension point the backend documents for template or remote
summarizers, applied on the live service instance. It is opt-in because it changes what every
compaction in the session writes.

## Failure behavior

SAC must never break a turn. Every failure (no backend, busy lock, unbalanced range, aborted signal,
summarizer error) is caught, logged once per distinct message, and the step proceeds with the
uncompacted history. The next step retries the fold.

## Tests

```bash
node --test test/sac-policy.test.js test/integration.test.js
```

48 tests cover: anchor detection, checkpoint absorption, balanced-cut selection, open-call safety,
batching, size guards, config sanitization, turn budget, surface adaptation, stream assembly, the
pre-step dispatch path including failure isolation, plus the v9 pipeline: inline state extraction and
fallback, ledger rendering, adaptive budget, checklist rendering, archived recall search, and tool
registration.

## Limitations

- A single action whose own observation exceeds the window cannot be repaired by compaction; pair it
  with `dsh-compaction-tool-result-pruner`.
- The state is model-written and therefore lossy — this is inherent to SAC (paper §3.1). Pair the
  plugin with an archive + retrieval tool if the task needs exact historical recall.
- Compaction runs *between* steps, so one step's parallel tool calls are folded together; this is the
  harness-level analogue of "one action per context".