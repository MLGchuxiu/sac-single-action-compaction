/**
 * Single-Action Compaction (SAC) policy — pure decision logic.
 *
 * Implements the regime described in "You don't need long context"
 * (arXiv:2611.05306), Section 2.4.1:
 *
 *   "Each conversation receives the task and current semantic state, performs
 *    exactly one environment action, observes its result, and updates the
 *    semantic state. The harness then discards that conversation and starts a
 *    fresh one for the next action."
 *
 * The module is deliberately free of Cordis, session, and LLM imports so the
 * range selection can be unit-tested against plain surface descriptors. The
 * host plugin (`index.js`) adapts a live `Session` into this shape.
 *
 * Surface descriptor contract
 * ---------------------------
 * A surface is the ordered list of nodes currently visible to the model:
 *
 *   [{ seq, type, isCheckpoint?, hasToolCall?, textChars?, source? }, ...]
 *
 * `type` mirrors the session event type: `user/message`, `assistant/message`,
 * `tool/result`. `isCheckpoint` marks a user message written by a previous
 * compaction (its source kind is `compact-checkpoint`).
 *
 * @module dsh-sac-compact/sac-policy
 */

/** Marker used by every compaction backend for its replacement message. */
export const CHECKPOINT_SOURCE_KIND = 'compact-checkpoint'

/** Defaults tuned for the paper's "short context" regime (16K–50K tokens). */
export const SAC_DEFAULTS = Object.freeze({
  /** Compact after this many completed actions (1 = strict single-action SAC). */
  actionsPerCompaction: 1,
  /** Never compact when the compactable span is smaller than this (chars). */
  minSpanChars: 1200,
  /** Never compact when the span is smaller than this (estimated tokens).
   *  A state recap is itself several hundred tokens, so folding a tiny span
   *  only makes the backend reject the result as "not smaller". 0 = off. */
  minSpanTokens: 0,
  /** Estimated tokens per character used for the span-size guard. */
  charsPerToken: 3.5,
  /** Hard cap on compactions performed in one agent turn (runaway guard). */
  maxCompactionsPerTurn: 200,
  /** Keep the trailing assistant message (no tool call) out of the span. */
  keepTrailingReply: true,
  /** Keep the newest tool observation out of the span so the next request still
   *  sees what the last action produced (false = strict SAC folds it too). */
  keepLastObservation: false,
  /** Output cap for the paper state update (the model's recap call). */
  stateMaxTokens: 2048,
  /** State budget in tokens; the adaptive rule shrinks it early in a task. */
  stateBudget: 2048,
  /** Use `min(stateBudget, 640 + 48 * foldedActions)` instead of a fixed budget. */
  adaptiveBudget: true,
  /** Prepend a deterministic machine ledger of folded actions to the state. */
  ledger: false,
  /** Character cap for the rendered ledger. */
  ledgerChars: 2400,
  /** Include full tool-call arguments in the ledger (false truncates them). */
  ledgerFullCommands: true,
  /** Register a `recall` tool that searches the archived transcript. */
  recall: false,
  /** Generate a subgoal checklist and register a `check` tool. */
  checklist: false,
  /** Only head/tail-trim a single observation when it exceeds this many chars
   *  (0 = off). Unlike `stateInputMaxChars`, small observations stay verbatim,
   *  so coding tasks keep their exact tool output while huge document reads are
   *  bounded. */
  trimObservationOver: 0,
  /** Force a fold once the whole surface reaches this many estimated tokens,
   *  even when the normal size guard would skip (0 = off). This is the hard
   *  safety net for a 128K-context model. */
  contextBudgetTokens: 0,
  /** After this many failed inline extractions, stop trying inline and use the
   *  paper summarizer for the rest of the session (reasoning models often emit
   *  no text block before tool calls). */
  inlineFallbackAfter: 2,
  /** Ask the paper summarizer for a recap of at most this many words (0 = no
   *  limit). A terser recap is re-sent on every later request, so this is a
   *  direct per-request saving. */
  stateMaxWords: 0,
  /** Send the state-update request without the conversation's system prompt and
   *  tool schemas. The summarizer only needs the folded span plus the recap
   *  instruction; replaying the ~6-7K-token prefix on every fold is pure
   *  overhead (and is counted in the token bill even when KV-cached). */
  stateInputLean: false,
})

/**
 * Clamp a numeric config value into a safe range.
 * @param {unknown} value - candidate value.
 * @param {number} fallback - value used when the candidate is not finite.
 * @param {number} min - inclusive lower bound.
 * @param {number} max - inclusive upper bound.
 * @returns {number} sanitized number.
 */
export function clampNumber(value, fallback, min, max) {
  const numeric = typeof value === 'number' && Number.isFinite(value) ? value : fallback
  return Math.min(max, Math.max(min, numeric))
}

/**
 * Normalize user configuration into a complete policy.
 * @param {object} [config] - raw plugin config.
 * @returns {typeof SAC_DEFAULTS & {enabled: boolean, statePrompt: string}} resolved policy.
 */
export function resolvePolicy(config = {}) {
  return {
    enabled: config.enabled !== false,
    actionsPerCompaction: Math.floor(clampNumber(config.actionsPerCompaction, SAC_DEFAULTS.actionsPerCompaction, 1, 64)),
    minSpanChars: Math.floor(clampNumber(config.minSpanChars, SAC_DEFAULTS.minSpanChars, 0, 10_000_000)),
    minSpanTokens: Math.floor(clampNumber(config.minSpanTokens, SAC_DEFAULTS.minSpanTokens, 0, 100_000)),
    charsPerToken: clampNumber(config.charsPerToken, SAC_DEFAULTS.charsPerToken, 1, 8),
    maxCompactionsPerTurn: Math.floor(clampNumber(config.maxCompactionsPerTurn, SAC_DEFAULTS.maxCompactionsPerTurn, 1, 10_000)),
    keepTrailingReply: config.keepTrailingReply !== false,
    keepLastObservation: config.keepLastObservation === true,
    statePrompt: config.statePrompt === 'paper' || config.statePrompt === 'inline'
      ? config.statePrompt
      : 'native',
    stateInputMaxChars: Math.floor(clampNumber(config.stateInputMaxChars, 0, 0, 1_000_000)),
    stateMaxTokens: Math.floor(clampNumber(config.stateMaxTokens, SAC_DEFAULTS.stateMaxTokens, 256, 8192)),
    stateBudget: Math.floor(clampNumber(config.stateBudget, SAC_DEFAULTS.stateBudget, 256, 32_000)),
    adaptiveBudget: config.adaptiveBudget !== false,
    ledger: config.ledger === true,
    ledgerChars: Math.floor(clampNumber(config.ledgerChars, SAC_DEFAULTS.ledgerChars, 200, 200_000)),
    ledgerFullCommands: config.ledgerFullCommands !== false,
    recall: config.recall === true,
    checklist: config.checklist === true,
    trimObservationOver: Math.floor(clampNumber(config.trimObservationOver, 0, 0, 1_000_000)),
    contextBudgetTokens: Math.floor(clampNumber(config.contextBudgetTokens, 0, 0, 10_000_000)),
    inlineFallbackAfter: Math.floor(clampNumber(config.inlineFallbackAfter, 2, 1, 10)),
    stateMaxWords: Math.floor(clampNumber(config.stateMaxWords, 0, 0, 2000)),
    stateInputLean: config.stateInputLean === true,
  }
}

/**
 * Find the index of the task anchor: the first non-checkpoint user message.
 * Everything at or before this index is retained verbatim forever.
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @returns {number} anchor index, or -1 when the surface has no task message.
 */
export function findTaskAnchorIndex(nodes) {
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index]
    if (node.type === 'user/message' && node.isCheckpoint !== true) return index
  }
  return -1
}

/**
 * Compute the inclusive index span that one SAC step should compact.
 *
 * The span starts immediately after the task anchor so a previously written
 * semantic state is absorbed (updated) rather than duplicated, and ends at the
 * newest balanced cut that still leaves `actionsPerCompaction` actions behind.
 *
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @param {object} [policy] - resolved policy (see {@link resolvePolicy}).
 * @returns {{startIndex: number, endIndex: number, actions: number, chars: number, reason: string}|null}
 *   the span to compact, or `null` when SAC must not run this step.
 */
export function planSacSpan(nodes, policy = SAC_DEFAULTS) {
  if (!Array.isArray(nodes) || nodes.length === 0) return null
  const anchor = findTaskAnchorIndex(nodes)
  if (anchor < 0) return null

  const startIndex = anchor + 1
  if (startIndex >= nodes.length) return null

  // Walk the span counting completed actions (assistant tool call + result)
  // and remembering every index whose trailing cut is tool-pairing balanced.
  let actions = 0
  let openToolCalls = 0
  let chars = 0
  const balancedEnds = []
  for (let index = startIndex; index < nodes.length; index += 1) {
    const node = nodes[index]
    chars += node.textChars ?? 0
    if (node.type === 'assistant/message') {
      const calls = node.toolCallCount ?? (node.hasToolCall === true ? 1 : 0)
      openToolCalls += calls
      if (openToolCalls === 0) balancedEnds.push(index)
    } else if (node.type === 'tool/result') {
      openToolCalls = Math.max(0, openToolCalls - 1)
      if (openToolCalls === 0) {
        actions += 1
        balancedEnds.push(index)
      }
    } else if (openToolCalls === 0) {
      balancedEnds.push(index)
    }
  }

  if (balancedEnds.length === 0) return null
  if (actions < policy.actionsPerCompaction) return null

  // Optionally keep a trailing assistant reply (no tool call) verbatim so the
  // user-visible answer of the current step is not folded into the state.
  let endIndex = balancedEnds[balancedEnds.length - 1]
  if (policy.keepTrailingReply && balancedEnds.length > 1) {
    const last = nodes[endIndex]
    const previous = balancedEnds[balancedEnds.length - 2]
    if (last.type === 'assistant/message' && (last.toolCallCount ?? 0) === 0 && previous >= startIndex) {
      endIndex = previous
    }
  }

  // Optional continuity: leave the newest observation raw. The next request
  // then carries the state + that observation + the new action, which stops the
  // "I folded it away, let me read it again" loop.
  if (policy.keepLastObservation === true && nodes[endIndex]?.type === 'tool/result') {
    const previous = balancedEnds[balancedEnds.length - 2]
    if (previous !== undefined && previous >= startIndex) endIndex = previous
  }

  // Recompute the chosen span's weight: endIndex may have moved back.
  let spanChars = 0
  let spanActions = 0
  let open = 0
  for (let index = startIndex; index <= endIndex; index += 1) {
    const node = nodes[index]
    spanChars += node.textChars ?? 0
    if (node.type === 'assistant/message') {
      open += node.toolCallCount ?? (node.hasToolCall === true ? 1 : 0)
    } else if (node.type === 'tool/result') {
      open = Math.max(0, open - 1)
      if (open === 0) spanActions += 1
    }
  }
  const estimatedTokens = Math.ceil(spanChars / policy.charsPerToken)
  if (spanChars < (policy.minSpanChars ?? SAC_DEFAULTS.minSpanChars)) return null
  if (estimatedTokens < (policy.minSpanTokens ?? SAC_DEFAULTS.minSpanTokens)) return null
  if (spanActions < policy.actionsPerCompaction) return null

  return {
    startIndex,
    endIndex,
    actions: spanActions,
    chars: spanChars,
    estimatedTokens,
    reason: `sac:${spanActions}-action(s)`,
  }
}

/**
 * Decide whether another compaction may run in the current turn.
 * @param {number} performed - compactions already performed this turn.
 * @param {object} policy - resolved policy.
 * @returns {boolean} whether the budget still allows one more compaction.
 */
export function withinTurnBudget(performed, policy) {
  return performed < policy.maxCompactionsPerTurn
}


/**
 * Resolve the state budget for one fold. The paper's state is meant to stay
 * small; early folds get less room, later folds earn more as the task's facts
 * accumulate.
 * @param {object} policy - resolved policy.
 * @param {number} actions - completed actions folded so far.
 * @returns {number} budget in tokens.
 */
export function stateBudgetFor(policy, actions = 0) {
  const fixed = policy.stateBudget ?? SAC_DEFAULTS.stateBudget
  if (policy.adaptiveBudget === false) return fixed
  return Math.max(512, Math.min(fixed, 640 + 48 * Math.max(0, actions)))
}
