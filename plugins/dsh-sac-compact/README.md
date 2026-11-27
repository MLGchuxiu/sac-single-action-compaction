# @local/dsh-sac-compact

Single-Action Compaction (SAC) for the DeepSeek Harness — a context-compression policy that folds
completed work into one compact, human-readable *semantic state* **once the request has grown large
enough for a fold to pay for itself**, so a long session's model requests carry only

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

## Multi-turn sessions (1.2.0)

The paper's regime has one task per conversation. A real session does not: the user keeps talking.
Up to 1.1.0 only the *first* user message was the task anchor. Every later message was ordinary
foldable content, so a follow-up request stayed on the surface for exactly `actionsPerCompaction`
steps and was then replaced by the summarizer's paraphrase of it — while the first message stayed
verbatim forever. Two things followed, both seen in real sessions (see
[`reports/REPORT-SAC-MULTITURN.md`](../../reports/REPORT-SAC-MULTITURN.md)):

- the agent's only verbatim instruction was the *first* request, so after a few folds it drifted back
  to that (a session asked to "add the model as a provider" went back to "find the API key" within
  two folds);
- the state update never saw the request at all (`stateInputLean` sends only the folded span), so it
  rewrote "Objective" as whatever the last two actions happened to be doing.

From 1.2.0 **every user instruction is a task anchor**. Only the work *between* instructions is
folded, and the surface becomes

```
system prompt + [archive] + request₁ + state₁ + … + requestₙ + stateₙ + next action
```

- **Pinned.** A user instruction (`source.kind` `user`, or the `goal` plugin's `<goal_round>`
  message) is never inside a folded span. Harness notices that happen to be user-role messages
  (`runtime-context`, `skill-catalog`, `time-context`, `tool-jobs`, `user-approval`, …) are not
  anchors and fold as before.
- **Per-request state.** The work after a request folds into that request's own state, and the state
  update is given the request verbatim in front of the span. The objective is taken from the request,
  not from the last actions.
- **Finished turns.** When a new request arrives, the previous turn keeps its final reply and folds
  its leftover actions — but only once the model has acted on the new request, so the first reply is
  never held up by a fold.
- **Archive.** At most `maxPinnedInstructions` requests stay pinned. Older ones are merged into one
  archive state that still **quotes them verbatim** (a machine-maintained section, capped by
  `instructionChars`), so nothing the user wrote is ever paraphrased away.
- **Goal rounds.** The `<goal_round>` message the goal plugin re-delivers every round differs only in
  its `Round: n/m` line; a re-delivery is not a new anchor. An edited objective is.
- **Escape hatch.** `pinInstructions: false` restores the 1.1.0 single-anchor behaviour.

A single-task session (the benchmarks) is unchanged: one instruction, one rolling state, the same
spans as before. In a multi-turn session each pinned turn costs its state plus its final reply
(≈ 1.5K tokens); lower `maxPinnedInstructions` (1 keeps only the request in hand plus the archive) or
raise it to keep more turns raw.

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
| `frozenFolds` / `archiveFolds` | folds of finished turns / merges of released requests into the archive |
| `carriedRequests` | requests the archive state quotes verbatim |
| `refusedFolds` | finished-turn or archive spans the backend refused as "not smaller" (each span is given two attempts, then left alone) |
| `mechanicalFolds` / `replyFolds` | folds the harness wrote itself / of which took a heavy final reply |
| `truncatedRecaps` / `stateRetries` / `stateRerouted` | recaps still cut off after the retry, cut-off or empty recaps that were retried, state updates sent to another route |
| `sizeForcedFolds` / `mechanicalRefusals` | size-trigger folds made under the hard context budget, digests that would not have shrunk their span |

Every fold logs one line with the same numbers, so a session log can be audited after the fact:

```
dsh-sac-compact: folded 1 action(s) (seqs 12-15, ~812 tokens) into semantic state | folds=3 folded_tokens=2410 summaries=3 summary_chars=1902 installed=pre-step
```

From 1.2.0 the line also says which segment folded (`segment=active|frozen|archive`), which request
it belongs to (`request=<seq>`), and how many requests an archive fold released.

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

## What's new in 1.3.0 (cost-aware folding, 2026-11-27)

**The bundle now ships the cost-aware policy** (the keys at the end of
[`cordis.patch.yml`](cordis.patch.yml)). With it the plugin behaves like full history on short
dialogues — nothing folds until the whole request is about 32K tokens over the fixed prompt — and like
SAC on long sessions, where it keeps the request near that size for about 5–10% of the tokens of full
history. Every mechanism is a config key; setting them back (`foldTrigger: actions`, margins 0,
`mechanicalFoldMaxTokens: 0`, `recapMaxRatio: 0`, …) reproduces 1.2.0 exactly (checked against all 820
logged fold decisions of the 184-task τ² run; `SAC_DEFAULTS` in the code are still the 1.2.0 values, so
a host that mounts the plugin without the bundle config gets the old behaviour).

Why: on the 184-task τ² run (qwen3.8-27b, thinking) a fold was a bad trade. A model-written recap
cost ≈4.2K tokens (2.2K in + 2.0K of reasoning), replaced a median 1,212-token span with a 613-token
recap, and 24% of the attempts were refused as "not smaller"; another 24% of the "successful" recaps
were cut off at the output cap and 25% more were truncated by the early-fold budget. On the 36
longest of those tasks, 29% of the agent's tool calls repeated an earlier call ("folded away, read
again"). Folding after every two actions is right for a 300-step coding session and wrong for a
ten-step dialogue.

- **Size trigger with hysteresis** (`foldTrigger: size`, `foldAboveTokens`, `foldBelowTokens`,
  `maxFoldsPerStep`): nothing folds while the whole request (system prompt, tool schemas, surface) is
  under the high-water mark; above it the policy folds, in one pre-step if needed, until the request is
  back under the low-water mark. Short dialogues never pay for a fold; long sessions fold as before.
- **Gain guard** (`foldMinGainTokens`, `recapExpectedTokens`): a model-written recap only replaces a
  span that frees at least the expected recap plus the margin. The expected recap size tracks what this
  agent's folds actually produced.
- **Mechanical folds** (`mechanicalFoldMaxTokens`, `mechanicalSegments`, `mechanicalMinGainTokens`,
  `digestResultChars`, `digestReplyChars`, `digestMaxChars`): when the verbatim digest of a span is
  small enough, the harness writes the checkpoint itself — earlier state carried forward as written,
  one line per action (`- tool(args)` / `  → result`), replies quoted — and makes no model call.
  Structured (JSON) results are compacted rather than cut: keys, ids and numbers of the kept items stay
  exact. Hidden reasoning is dropped for free (providers replay it in every later request; a digest
  never keeps it), so a digest may replace a span of any size. A mechanical fold costs no waiting and
  may therefore free an earlier turn before the model's first step on a new request; a model-written
  recap still waits. By default only **finished turns** are folded this way
  (`mechanicalSegments: frozen`): digesting the working state after every step made the agent redo
  its work on τ²-bench (+60–80% model steps), so the current turn keeps a model-written recap.
- **Heavy final replies fold** (`replyReasoningFoldChars`): a finished turn's final reply whose
  reasoning is large goes with the turn's digest (its visible text stays), instead of staying raw as a
  13K-token node for the sake of 300 characters of text.
- **Bounded recaps** (`recapMaxRatio`): the recap must stay under a share of its span, measured in the
  backend's own tokens. The word limit handed to the model and a hard bound on what it wrote both
  follow it, so the backend no longer refuses folds as "not smaller". With such a bound the early-fold
  state budget no longer applies: it cut 156 of the 620 recaps of the τ² run head and tail (640 tokens
  on a first fold, whatever the span).
- **Cut-off detection** (`stateMaxTokensThinking`, `stateMaxTokensCeiling`): the state update reads
  the stream's finish reason. A recap cut off at the output cap or returned without text is retried
  once with a tighter word limit and a doubled cap, and the doubled cap stays for the session. When the
  agent's own messages carry reasoning, the first attempt can start from the larger cap
  (`stateMaxTokensThinking`). 1.2.0 accepted truncated recaps silently: 147 of its 620 "successful"
  τ² recaps stopped in the middle.
- **Recap length contract follows the span** (with `recapMaxRatio`): the word limit handed to the
  summarizer is half the span in words (never more than 4 × `stateMaxWords`, never more than the
  budget) instead of a fixed 400, a cut-off retry keeps that contract (the output cap doubles instead
  of asking for fewer words), and the prompt asks for carried values in full. A fixed 400-word contract
  made a summarizer replace an accumulated table of 60 records with a count of it; a retry that asked
  for 40% fewer words dropped half of it.
- **Separate summarizer route** (`stateProvider`, `stateModel`): the recap may be written by a cheaper
  or non-reasoning model.
- **Goal rounds** (`goalDedupe`): a `<goal_round>` that re-delivers the current objective is folded by
  a digest even when no tool action happened in the span, so automatic rounds never pile up copies of
  the objective (27 rounds: 27 copies on the surface without compaction, at most 2 with it).

108 tests (was 78). Evidence and the Pareto comparison with 1.2.0, EoCC and full history:
[`reports/REPORT-SAC-PARETO.md`](../../reports/REPORT-SAC-PARETO.md).

## What's new in 1.2.0 (multi-turn requests, 2026-11-25)

- **Every user instruction is pinned**; only the work between instructions is folded (`pinInstructions`,
  `instructionSourceKinds`, `maxPinnedInstructions`, `instructionChars`, `archiveMinTokens`). Fixes the
  "a follow-up request is summarized away and the agent keeps working on the first task" bug.
- The state update is **anchored on the request** it belongs to (quoted verbatim in front of the span),
  and the paper template takes the objective from that request instead of from the last actions.
- The paper template writes **"(none)"** for an output contract the user did not ask for. Asked for
  "the exact response format the task requires", the summarizer invented JSON contracts that later
  folds then treated as the objective.
- Released requests are quoted verbatim in an archive state; the list is read back from the surface
  after a restart.
- Finished-turn and archive spans the backend refuses as "not smaller" get two attempts instead of one
  summarizer call per step.
- 78 tests (was 48).

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

### Cost-aware policy (1.3.0) — shipped

```yaml
foldTrigger: size            # fold by request size, not by action count
foldMarginTokens: 32000      # …once the request exceeds the fixed prompt (system + tools) by ~32K
                             #   tokens, and down to the fixed prompt + ~16K. 24000 / 12000 trade
                             #   more folds for a smaller context; 0 disables folding by size.
maxFoldsPerStep: 3
foldMinGainTokens: 400       # a model-written recap must free the recap + 400 tokens
recapMaxRatio: 0.6           # recap ≤ 60% of its span (never "not smaller")
mechanicalFoldMaxTokens: 4000 # digests up to ~4K tokens are written by the harness, no model call
mechanicalSegments: frozen   # (default) …for finished turns only, never the working state
replyReasoningFoldChars: 1500 # a final reply with heavy reasoning folds with its turn
digestResultChars: 2000      # JSON results are compacted by structure, not cut
stateMaxTokensThinking: 4096 # a thinking agent's first recap starts from a 4,096 cap
# stateProvider: other-provider        # optional: a non-reasoning model writes the recaps
# stateModel: deepseek-v4.1-flash      # (~5 s per fold instead of 20–70 s with a thinking model)
```

Keep `contextBudgetTokens` as the hard safety net; `foldMarginTokens` is the knob that traces the
cost/context trade-off. Measured on 13 real long sessions (replayed): full history 2.19 billion
tokens, this policy 189 million with 503 model-written recaps, 1.2.0 119 million with
2,558. On the τ²-bench subset it is full history by construction (1–2 folds in 36 tasks).

On a 40-record memory test (read one 2,700-token record per step, then answer 120 facts and a total)
the shipped policy answered 120/120 in 6 of 6 runs (qwen3.8-27b ×4, GLM-5.3-Inner ×2) for 1.08M
tokens against 3.28M for full history; SAC 1.2.0 with qwen3.8-27b re-read every file and still got
74/120. On an 80-record version (GLM) the shipped policy was clean in 2 of 3 runs; in the third the
model-written recap dropped part of the accumulated table and the agent re-read everything. A task
that must carry many exact values for a long time is the known boundary of model-written recaps; the
harness-written `ledger` (result heads) is the likely fix and is not wired into this policy yet.

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
| `pinInstructions` | `true` | Keep every user instruction verbatim and fold only the work between instructions (see *Multi-turn sessions*). `false` restores the single-anchor behaviour of 1.1.0. |
| `instructionSourceKinds` | `['user', 'goal']` | Producer kinds of user-role messages that are the user's instructions (array or comma-separated). Other kinds are harness notices and stay foldable. |
| `maxPinnedInstructions` | `4` | Number of most recent instructions kept verbatim. Older ones merge into the archive state, which still quotes them. `0` = never release. |
| `instructionChars` | `4000` | Character budget of the verbatim earlier-request list the archive carries, newest first. `0` disables the carry. |
| `archiveMinTokens` | `0` | Minimum estimated tokens an archive fold must shadow. The archive replacement carries the verbatim requests, so it needs a larger span than a working-state fold to come out smaller. `0` = derive it from `stateMaxWords` / `stateBudget` and `instructionChars`. |
| `foldTrigger` | `actions` | `actions`: the working state folds once it holds `actionsPerCompaction` completed actions (the 1.2.0 behaviour). `size`: nothing folds until the whole request reaches `foldAboveTokens`, then folds repeat until it is under `foldBelowTokens`. |
| `foldAboveTokens` | `0` | High-water mark of the size trigger, in estimated tokens of system prompt + tool schemas + surface. `0` = never folds by size. |
| `foldBelowTokens` | `0` | Low-water mark of the size trigger. `0` = half of `foldAboveTokens`. |
| `foldMarginTokens` | `0` | Margin of the size trigger over the fixed part of the request (system prompt + tool schemas), used when `foldAboveTokens` is `0`: high-water = fixed + margin, low-water = fixed + margin / 2. One setting then fits a 5K customer-service prompt and a 20K coding-agent prompt. |
| `maxFoldsPerStep` | `3` | Folds one pre-step may run in a row under the size trigger. The action trigger always makes at most one. |
| `foldMinGainTokens` | `0` | A model-written recap only replaces a span at least the expected recap plus this many tokens larger. `0` = off. |
| `recapExpectedTokens` | `0` | Recap size the gain test expects. `0` = derived from `stateMaxWords` / `stateBudget`, blended with what this agent's recaps measured. |
| `recapMaxRatio` | `0` | Keep the recap under this share of its span (backend tokens): the word limit and a hard bound follow it. `0` = off. |
| `mechanicalFoldMaxTokens` | `0` | Largest digest (estimated tokens) the harness writes itself; a span whose digest fits is folded without a model call. `0` = off. |
| `mechanicalSegments` | `frozen` | `frozen`: only finished turns and repeated goal rounds are folded by the harness; the working state gets a model recap. `all`: the working state as well (a digest after every step made the agent redo work on τ²-bench). |
| `mechanicalMinGainTokens` | `80` | Smallest surface reduction that makes a mechanical fold worth a checkpoint. |
| `digestResultChars` | `600` | Longest tool result kept in one digest line (head and tail). |
| `digestReplyChars` | `4000` | Longest assistant reply kept in one digest line. |
| `digestMaxChars` | `3000` | Longest digest; older lines are dropped first. |
| `digestShrinkToBudget` | `true` | When the digest of a finished turn would outgrow `mechanicalFoldMaxTokens`, every result takes a smaller share (its head) so the digest fits, instead of the turn being handed to the model. |
| `replyReasoningFoldChars` | `0` | A finished turn's final reply with at least this many characters of hidden reasoning folds with the turn's digest. `0` = the reply always stays raw. |
| `goalDedupe` | `true` | Fold a re-delivered goal round without any tool action (as a digest). |
| `stateProvider` / `stateModel` | `''` | Route of the state-update call. Empty = the agent's own route. |
| `standingSourceKinds` | `['agent-instructions']` | User-role messages that hold standing instructions the host itself re-supplies after a fold (the workspace instructions of AGENTS.md). They are left out of the state-update input. |
| `stateMaxTokensCeiling` | `8192` | Upper bound of the state-update output cap once a cut-off doubles it. |
| `stateMaxTokensThinking` | `0` | Output cap of the state update when the agent's messages carry reasoning blocks (thinking model): the first attempt starts here. `0` = always start from `stateMaxTokens`. |

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
Remaining plan / Output contract / Verbatim values). This is the same extension point the backend
documents for template or remote summarizers, applied on the live service instance. It is opt-in
because it changes what every compaction in the session writes.

The request the folded work belongs to is quoted in front of the span and the objective is taken from
it (see *Multi-turn sessions*). An archive fold uses a separate template (`ARCHIVE_STATE_PROMPT`) that
records the outcome of each released request instead of proposing next steps.

A fold the backend starts on its own (pressure recovery, `/compact`) has no plan: the span is taken to
belong to the newest request inside it, or to the one in front of it, and every request inside the span
is quoted verbatim in the new checkpoint so it is not lost.

## Failure behavior

SAC must never break a turn. Every failure (no backend, busy lock, unbalanced range, aborted signal,
summarizer error) is caught, logged once per distinct message, and the step proceeds with the
uncompacted history. The next step retries the fold.

## Tests

```bash
node --test test/*.test.js
# (or list the five files: sac-policy, integration, multiturn-policy, multiturn-integration, adaptive)
```

108 tests cover: anchor detection, checkpoint absorption, balanced-cut selection, open-call safety,
batching, size guards, config sanitization, turn budget, surface adaptation, stream assembly, the
pre-step dispatch path including failure isolation, plus the v9 pipeline: inline state extraction and
fallback, ledger rendering, adaptive budget, checklist rendering, archived recall search, and tool
registration; multi-turn sessions: pinned requests, finished-turn and archive folds, goal-round
re-delivery, the anchored state update, the verbatim request list and its recovery after a restart;
and cost-aware folding: the size trigger with hysteresis and margin-based marks, the gain guard,
harness-written digests (finished turns only by default, structured JSON results compacted rather
than cut, heavy final replies, shrinking to budget), bounded and uncut recaps (span-relative word
limit, cut-off detection and retry with a doubled cap that stays up for the session, the larger cap
for thinking models), the separate summarizer route, goal-round dedupe, workspace instructions kept
out of the state update, and a legacy configuration that reproduces the 1.2.0 behaviour.

The preset bundle adds 7 tests of its own
([`plugins/dsh-sac-preset/test/preset-policy.test.js`](../dsh-sac-preset/test/preset-policy.test.js))
that pin its `sac-compact` configuration to this bundle's shipped one.

## Limitations

- A single action whose own observation exceeds the window cannot be repaired by compaction; pair it
  with `dsh-compaction-tool-result-pruner`.
- The state is model-written and therefore lossy — this is inherent to SAC (paper §3.1). Pair the
  plugin with an archive + retrieval tool if the task needs exact historical recall. User *requests*
  are the exception: they are never summarized.
- Compaction runs *between* steps, so one step's parallel tool calls are folded together; this is the
  harness-level analogue of "one action per context".
- The pinned requests and the states of finished turns are not reducible by compaction; keep
  `maxPinnedInstructions` in proportion to the model's window (the hard `contextBudgetTokens` fold does
  fold finished turns' final replies, but never a pinned request).