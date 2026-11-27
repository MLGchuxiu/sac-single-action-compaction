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
 *   [{ seq, type, isCheckpoint?, hasToolCall?, textChars?, source?, sourceKind?, text? }, ...]
 *
 * `type` mirrors the session event type: `user/message`, `assistant/message`,
 * `tool/result`. `isCheckpoint` marks a user message written by a previous
 * compaction (its source kind is `compact-checkpoint`). `sourceKind` is the
 * producer kind of a user message (`user`, `goal`, `runtime-context`, ...) and
 * `text` the text of a message that may be a user instruction.
 *
 * Multi-turn sessions
 * -------------------
 * The paper's regime has one task per conversation. A real session has many:
 * the user keeps talking. Every user instruction is therefore a task anchor
 * that stays on the surface verbatim, and only the *work between instructions*
 * is folded. The surface becomes
 *
 *   [archive] U₁ [state of turn 1] U₂ [state of turn 2] … Uₙ [working state, actions]
 *
 * where at most `maxPinnedInstructions` instructions stay verbatim; older ones
 * are released into one archive state that still quotes them verbatim.
 *
 * @module dsh-sac-compact/sac-policy
 */

/** Marker used by every compaction backend for its replacement message. */
export const CHECKPOINT_SOURCE_KIND = 'compact-checkpoint'

/**
 * Producer kinds of user-role messages that carry the user's own instructions.
 * `user` is a typed prompt; `goal` is the `<goal_round>` message the goal
 * plugin re-delivers on every automatic round. Every other kind (`runtime-context`,
 * `skill-catalog`, `time-context`, `tool-jobs`, `user-approval`,
 * `working-directory`, ...) is harness bookkeeping and stays foldable.
 */
export const DEFAULT_INSTRUCTION_SOURCE_KINDS = Object.freeze(['user', 'goal'])

/**
 * Producer kinds of user-role messages that hold standing instructions the
 * harness itself re-supplies (the workspace instructions of AGENTS.md). They
 * are never restated in a recap.
 */
export const DEFAULT_STANDING_SOURCE_KINDS = Object.freeze(['agent-instructions'])

/** The one line the goal plugin changes between rounds of the same objective. */
const GOAL_ROUND_LINE = /^[ \t]*Round[ \t]*:[ \t]*\d+[ \t]*\/[ \t]*\d+[ \t]*$/gm
/** The envelope the goal-round driver renders around a re-delivered objective. */
const GOAL_ENVELOPE = /^\s*<goal_round>/


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
  /** Keep every user instruction verbatim and fold only the work between
   *  instructions. `false` restores the single-anchor behaviour: only the first
   *  user message is kept and every later one is folded like any other message. */
  pinInstructions: true,
  /** Producer kinds of user-role messages that are the user's instructions. */
  instructionSourceKinds: DEFAULT_INSTRUCTION_SOURCE_KINDS,
  /** Producer kinds of user-role messages that carry standing harness
   *  instructions (re-supplied by the host after a fold); they are left out
   *  of the state-update input. */
  standingSourceKinds: DEFAULT_STANDING_SOURCE_KINDS,
  /** Number of most recent instructions kept verbatim. Older ones are released
   *  into the archive state, which still quotes them verbatim. 0 = never release. */
  maxPinnedInstructions: 4,
  /** Character budget of the verbatim earlier-request list carried by the
   *  archive state (newest first). 0 = released requests are not carried. */
  instructionChars: 4000,
  /** Minimum estimated tokens a span must shadow before an archive fold runs.
   *  The archive replacement carries the verbatim requests, so it needs a larger
   *  span than a working-state fold to be strictly smaller. 0 = derive it from
   *  `stateMaxWords` / `stateBudget` and `instructionChars`. */
  archiveMinTokens: 0,
  /** What starts a fold. `actions` (legacy): the working state folds once it
   *  holds `actionsPerCompaction` completed actions. `size`: nothing folds until
   *  the whole request is estimated at `foldAboveTokens` tokens or more, and
   *  then folds repeat until it is back under `foldBelowTokens`. */
  foldTrigger: 'actions',
  /** High-water mark of the size trigger, in estimated tokens of system prompt,
   *  tool schemas and surface together (0 = never folds by size). */
  foldAboveTokens: 0,
  /** Low-water mark of the size trigger (0 = half of `foldAboveTokens`). */
  foldBelowTokens: 0,
  /** Margin of the size trigger over the fixed part of the request (system
   *  prompt + tool schemas): the high-water mark is the fixed part plus this
   *  many tokens, the low-water mark the fixed part plus half of it. Used when
   *  `foldAboveTokens` is 0, so one setting fits prompts of very different
   *  sizes (a 5K customer-service prompt and a 20K coding-agent prompt). */
  foldMarginTokens: 0,
  /** Folds one pre-step may run in a row under the size trigger, which loops
   *  until the request is under the low-water mark or nothing is left to
   *  fold. The action trigger always makes at most one fold per step. */
  maxFoldsPerStep: 3,
  /** A model-written recap only replaces a span that is at least the expected
   *  recap plus this many tokens larger (0 = off). A fold that frees less is
   *  paid for in summarizer tokens and buys almost nothing. */
  foldMinGainTokens: 0,
  /** Recap size the gain test expects, in tokens (0 = derived from the word
   *  cap or the state budget). The host passes what this agent's recaps
   *  actually measured instead. */
  recapExpectedTokens: 0,
  /** Keep the recap under this share of the span it replaces, in the
   *  backend's own tokens (0 = off). The word limit given to the model and a
   *  hard bound on the written recap both follow it, so the backend never
   *  refuses the fold as "not smaller". */
  recapMaxRatio: 0,
  /** Largest digest (estimated tokens) the harness writes by itself: a span
   *  whose verbatim digest of the actions fits here is folded without a model
   *  call, whatever the span weighs (hidden reasoning leaves for free). Larger
   *  digests, and every archive fold, get a model-written recap (0 = off). */
  mechanicalFoldMaxTokens: 0,
  /** Smallest surface reduction (tokens) that makes a mechanical fold worth
   *  a checkpoint. */
  mechanicalMinGainTokens: 80,
  /** Which segments the harness may fold itself. `frozen`: only finished turns
   *  (and repeated goal rounds) — the current turn's working memory is left to
   *  a model-written recap; `all`: the working state as well. Folding the
   *  working state every step as a digest made the agent redo work (+60–80%
   *  model steps on τ²-bench), so `frozen` is the default. */
  mechanicalSegments: 'frozen',
  /** Longest tool result / assistant text kept in one line of a digest (chars). */
  digestResultChars: 600,
  /** Longest carried digest (chars); older digest lines are dropped beyond it. */
  digestMaxChars: 3000,
  // Shrink every result's share of a digest so the digest fits the mechanical
  // budget, instead of handing a span with many results to the model.
  digestShrinkToBudget: false,
  /** Fold a re-delivered goal round even when no tool action happened in the
   *  span: the harness replaces the duplicate objective and the reply around
   *  it with a digest, so automatic rounds never pile up copies of it. */
  goalDedupe: true,
  /** Provider and model of the state-update call ('' = the agent's own route). */
  stateProvider: '',
  stateModel: '',
  /** Upper bound of the state-update output cap once retries double it. */
  stateMaxTokensCeiling: 8192,
  /** Output cap of the state update when the agent's own messages carry
   *  reasoning (a thinking model spends much of a small cap before the recap):
   *  the first attempt starts here instead of paying a cut-off and a retry. */
  stateMaxTokensThinking: 0,
  /** A finished turn's final reply whose hidden reasoning is at least this many
   *  characters is folded with the turn instead of staying raw (0 = always keep
   *  the reply). Only a harness-written digest takes it: the reply's visible
   *  text is kept (up to `digestReplyChars`), its reasoning is not. */
  replyReasoningFoldChars: 0,
  /** Longest assistant reply kept in one line of a digest (chars). */
  digestReplyChars: 4000,
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
 * Normalize the configured instruction source kinds.
 * @param {unknown} value - array or comma/space separated string.
 * @returns {ReadonlyArray<string>} trimmed, non-empty kinds.
 */
export function resolveInstructionKinds(value) {
  const list = Array.isArray(value)
    ? value
    : typeof value === 'string' ? value.split(/[\s,]+/) : []
  const kinds = list.filter((kind) => typeof kind === 'string' && kind.length > 0)
  return kinds.length > 0 ? kinds : SAC_DEFAULTS.instructionSourceKinds
}

/** Normalize the configured standing-notice kinds (unknown values fall back to the default). */
function resolveStandingKinds(value) {
  const list = Array.isArray(value)
    ? value
    : typeof value === 'string' ? value.split(/[\s,]+/) : []
  const kinds = list.filter((kind) => typeof kind === 'string' && kind.length > 0)
  return kinds.length > 0 ? kinds : SAC_DEFAULTS.standingSourceKinds
}

/** Instruction kinds of a possibly partial policy object. */
function instructionKinds(policy) {
  return Array.isArray(policy?.instructionSourceKinds)
    ? policy.instructionSourceKinds
    : SAC_DEFAULTS.instructionSourceKinds
}

/**
 * Normalize user configuration into a complete policy.
 * @param {object} [config] - raw plugin config.
 * @returns {typeof SAC_DEFAULTS & {enabled: boolean, statePrompt: string}} resolved policy.
 */
export function resolvePolicy(config = {}) {
  const policy = {
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
    pinInstructions: config.pinInstructions !== false,
    instructionSourceKinds: resolveInstructionKinds(config.instructionSourceKinds),
    standingSourceKinds: resolveStandingKinds(config.standingSourceKinds),
    maxPinnedInstructions: Math.floor(clampNumber(config.maxPinnedInstructions, SAC_DEFAULTS.maxPinnedInstructions, 0, 10_000)),
    instructionChars: Math.floor(clampNumber(config.instructionChars, SAC_DEFAULTS.instructionChars, 0, 1_000_000)),
    archiveMinTokens: Math.floor(clampNumber(config.archiveMinTokens, 0, 0, 10_000_000)),
    foldTrigger: config.foldTrigger === 'size' ? 'size' : 'actions',
    foldAboveTokens: Math.floor(clampNumber(config.foldAboveTokens, 0, 0, 10_000_000)),
    foldBelowTokens: Math.floor(clampNumber(config.foldBelowTokens, 0, 0, 10_000_000)),
    foldMarginTokens: Math.floor(clampNumber(config.foldMarginTokens, 0, 0, 10_000_000)),
    maxFoldsPerStep: Math.floor(clampNumber(config.maxFoldsPerStep, SAC_DEFAULTS.maxFoldsPerStep, 1, 16)),
    foldMinGainTokens: Math.floor(clampNumber(config.foldMinGainTokens, 0, 0, 1_000_000)),
    recapExpectedTokens: Math.floor(clampNumber(config.recapExpectedTokens, 0, 0, 100_000)),
    recapMaxRatio: clampNumber(config.recapMaxRatio, 0, 0, 0.95),
    mechanicalFoldMaxTokens: Math.floor(clampNumber(config.mechanicalFoldMaxTokens, 0, 0, 1_000_000)),
    mechanicalMinGainTokens: Math.floor(clampNumber(config.mechanicalMinGainTokens, SAC_DEFAULTS.mechanicalMinGainTokens, 0, 1_000_000)),
    mechanicalSegments: config.mechanicalSegments === 'all' ? 'all' : 'frozen',
    digestResultChars: Math.floor(clampNumber(config.digestResultChars, SAC_DEFAULTS.digestResultChars, 80, 100_000)),
    digestMaxChars: Math.floor(clampNumber(config.digestMaxChars, SAC_DEFAULTS.digestMaxChars, 500, 1_000_000)),
    digestShrinkToBudget: config.digestShrinkToBudget === true,
    goalDedupe: config.goalDedupe !== false,
    stateProvider: typeof config.stateProvider === 'string' ? config.stateProvider.trim() : '',
    stateModel: typeof config.stateModel === 'string' ? config.stateModel.trim() : '',
    stateMaxTokensCeiling: Math.floor(clampNumber(config.stateMaxTokensCeiling, SAC_DEFAULTS.stateMaxTokensCeiling, 256, 65_536)),
    stateMaxTokensThinking: Math.floor(clampNumber(config.stateMaxTokensThinking, 0, 0, 65_536)),
    replyReasoningFoldChars: Math.floor(clampNumber(config.replyReasoningFoldChars, 0, 0, 10_000_000)),
    digestReplyChars: Math.floor(clampNumber(config.digestReplyChars, SAC_DEFAULTS.digestReplyChars, 80, 400_000)),
  }
  if (policy.foldBelowTokens === 0 || policy.foldBelowTokens > policy.foldAboveTokens) {
    policy.foldBelowTokens = Math.floor(policy.foldAboveTokens / 2)
  }
  return policy
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
 * Whether a surface node is a message in which the user states a request.
 * A user message without provenance is treated as one (older hosts, tests).
 * @param {object} node - surface descriptor.
 * @param {object} [policy] - resolved policy.
 * @returns {boolean} true for a user instruction that is not a checkpoint.
 */
export function isInstructionNode(node, policy = SAC_DEFAULTS) {
  if (node?.type !== 'user/message' || node.isCheckpoint === true) return false
  if (node.sourceKind === undefined) return true
  return instructionKinds(policy).includes(node.sourceKind)
}

/**
 * Whitespace-normalized text of an instruction, used to recognize a
 * re-delivery of the newest instruction. For `<goal_round>` messages the round
 * counter line is ignored, because the goal plugin re-delivers an unchanged
 * objective on every automatic round with only that line changed.
 * @param {object} node - surface descriptor carrying `text`.
 * @returns {string|null} the key, or null when there is no text to compare.
 */
export function instructionKey(node) {
  if (typeof node?.text !== 'string') return null
  let text = node.text
  // A goal round is recognised by its producer kind, or by the envelope the
  // goal-round driver renders (the same text reached the surface another way).
  if (node.sourceKind === 'goal' || GOAL_ENVELOPE.test(text)) text = text.replace(GOAL_ROUND_LINE, '')
  const key = text.replace(/\s+/g, ' ').trim()
  return key.length > 0 ? key : null
}

/**
 * The archive replacement carries a recap plus the verbatim earlier requests,
 * so it needs a larger span than a working-state fold to be strictly smaller.
 * @param {object} policy - resolved policy.
 * @param {number} [carriedChars] - request characters the replacement will quote.
 * @returns {number} minimum estimated tokens the archive span must shadow.
 */
export function archiveMinTokensFor(policy = SAC_DEFAULTS, carriedChars = 0) {
  const configured = policy.archiveMinTokens ?? 0
  if (configured > 0) return configured
  const charsPerToken = policy.charsPerToken ?? SAC_DEFAULTS.charsPerToken
  const budget = policy.stateBudget ?? SAC_DEFAULTS.stateBudget
  // A recap asked to stay under N words is bounded well below the hard budget;
  // leave room for models that overrun the word limit.
  const recap = (policy.stateMaxWords ?? 0) > 0
    ? clampNumber(policy.stateMaxWords * 2.75, budget, 640, budget)
    : budget
  return Math.ceil((recap + Math.max(0, carriedChars) / charsPerToken) * 1.2)
}

/**
 * Split the surface into the spans SAC may fold, most urgent first.
 *
 * Every user instruction is pinned. The nodes after an instruction up to the
 * next one form that turn's segment: the newest segment is `active` (the work
 * in progress), the others are `frozen` (finished turns), and everything before
 * the oldest pinned instruction is the `archive` (released turns). With
 * `pinInstructions` off, or without any instruction on the surface, the single
 * legacy segment after the first user message is returned.
 *
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @param {object} [policy] - resolved policy (see {@link resolvePolicy}).
 * @returns {null|{segments: Array<object>, pinned: number[], released: number[], instructions: number[]}}
 *   segments `{kind, startIdx, endIdx, anchorIdx}` in priority order, or null
 *   when nothing on the surface may ever be folded.
 */
export function planSegments(nodes, policy = SAC_DEFAULTS, { turnEnding = false } = {}) {
  if (!Array.isArray(nodes) || nodes.length === 0) return null
  if (policy.pinInstructions === false) return legacySegments(nodes)

  const instructions = []
  const redelivered = []
  let newestKey = null
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index]
    if (!isInstructionNode(node, policy)) continue
    const key = instructionKey(node)
    // The same instruction delivered again (a new goal round) is not a new task.
    if (key !== null && key === newestKey) {
      redelivered.push(index)
      continue
    }
    instructions.push(index)
    newestKey = key
  }
  if (instructions.length === 0) return legacySegments(nodes)

  const keep = policy.maxPinnedInstructions ?? SAC_DEFAULTS.maxPinnedInstructions
  const releasedCount = keep > 0 ? Math.max(0, instructions.length - keep) : 0
  const pinned = instructions.slice(releasedCount)
  const released = instructions.slice(0, releasedCount)
  const last = nodes.length - 1
  const segments = []
  const push = (kind, startIdx, endIdx, anchorIdx) => {
    if (startIdx >= 0 && startIdx <= endIdx && endIdx <= last) {
      segments.push({ kind, startIdx, endIdx, anchorIdx })
    }
  }
  // Working state first: keeping it small is what every request depends on.
  push('active', pinned[pinned.length - 1] + 1, last, pinned[pinned.length - 1])
  // Finished turns fold their leftover actions once, when the next instruction arrives.
  for (let index = pinned.length - 2; index >= 0; index -= 1) {
    push('frozen', pinned[index] + 1, pinned[index + 1] - 1, pinned[index])
  }
  // Released turns merge into one archive that precedes the pinned instructions.
  const firstUser = nodes.findIndex((node) => node.type === 'user/message')
  if (released.length > 0) push('archive', firstUser, pinned[0] - 1, -1)
  // A user instruction is waiting in the inbox: the current turn is over, and
  // its work is a finished turn's (it may be folded by a digest right away).
  if (turnEnding === true && segments.length > 0 && segments[0].kind === 'active') {
    segments[0] = { ...segments[0], kind: 'frozen' }
  }
  return { segments, pinned, released, instructions, redelivered }
}

/** The single span after the first user message (pre-multi-turn behaviour). */
function legacySegments(nodes) {
  const anchor = findTaskAnchorIndex(nodes)
  if (anchor < 0 || anchor + 1 >= nodes.length) return null
  return {
    segments: [{ kind: 'active', startIdx: anchor + 1, endIdx: nodes.length - 1, anchorIdx: anchor }],
    pinned: [],
    released: [],
    instructions: [],
    redelivered: [],
  }
}

/**
 * Count the completed actions (assistant tool calls, all answered) in an index range.
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @param {number} startIdx - first index.
 * @param {number} endIdx - last index.
 * @returns {number} completed actions.
 */
function completedActions(nodes, startIdx, endIdx) {
  let actions = 0
  let open = 0
  for (let index = startIdx; index <= endIdx; index += 1) {
    const node = nodes[index]
    if (node.type === 'assistant/message') {
      open += node.toolCallCount ?? (node.hasToolCall === true ? 1 : 0)
    } else if (node.type === 'tool/result') {
      open = Math.max(0, open - 1)
      if (open === 0) actions += 1
    }
  }
  return actions
}

/**
 * Plan one fold of a segment: walk it counting completed actions and choose
 * the newest tool-pairing balanced cut.
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @param {object} segment - `{kind, startIdx, endIdx, anchorIdx}`.
 * @param {object} policy - resolved policy.
 * @param {{relaxed?: boolean, carriedChars?: number, released?: ReadonlySet<number>, redelivered?: ReadonlySet<number>,
 *   requiredActions?: number, expectedRecapTokens?: number}} [options] -
 *   `relaxed` drops the size guards (hard context budget); `carriedChars` is the
 *   size of the request list the archive already carries; `released` are the
 *   indices of the instructions this segment may release (a re-delivery of a
 *   pinned instruction is not one); `redelivered` the indices of goal rounds
 *   that repeated the newest instruction; `requiredActions` overrides the
 *   action count that starts a working-state fold; `expectedRecapTokens` is
 *   what a recap for this agent is expected to cost.
 * @returns {object|null} the span, or null when this segment must not fold now.
 */
function planSegment(nodes, segment, policy, options = {}) {
  const { startIdx, endIdx: lastIdx, kind, anchorIdx } = segment
  const relaxed = options.relaxed === true
  const minSpanChars = relaxed ? 0 : (policy.minSpanChars ?? SAC_DEFAULTS.minSpanChars)
  const minSpanTokens = relaxed ? 0 : (policy.minSpanTokens ?? SAC_DEFAULTS.minSpanTokens)
  // A finished turn folds every leftover action; the working state batches.
  // Under a hard context budget a finished turn also folds its bare snapshots.
  const requiredActions = kind === 'active'
    ? (options.requiredActions ?? policy.actionsPerCompaction ?? SAC_DEFAULTS.actionsPerCompaction)
    : kind === 'frozen' ? (relaxed ? 0 : 1) : 0
  // A span that repeats the pinned objective (another goal round) may fold
  // without any tool action, so automatic rounds do not pile up copies of it.
  const dedupe = kind === 'active' && policy.goalDedupe !== false
    && containsAny(options.redelivered, startIdx, lastIdx)

  // Walk the span counting completed actions (assistant tool call + result)
  // and remembering every index whose trailing cut is tool-pairing balanced.
  let actions = 0
  let openToolCalls = 0
  const balancedEnds = []
  for (let index = startIdx; index <= lastIdx; index += 1) {
    const node = nodes[index]
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
  // A finished turn that holds nothing but a heavy final reply (and earlier
  // states): the reply alone is the fold.
  const loneHeavyReply = kind === 'frozen' && policy.replyReasoningFoldChars > 0 && actions === 0
    && nodes[lastIdx]?.type === 'assistant/message' && (nodes[lastIdx]?.toolCallCount ?? 0) === 0
    && (nodes[lastIdx]?.reasoningChars ?? 0) >= policy.replyReasoningFoldChars
    && nodes.slice(startIdx, lastIdx).every((node) => node.isCheckpoint === true)
  // A span that repeats the pinned objective may fold with fewer actions than
  // the trigger asks for (see `dedupeOnly` below).
  if (actions < requiredActions && !dedupe && !loneHeavyReply) return null

  /** The cut to fold up to: `keepReminder` leaves the newest re-delivery raw. */
  const selectCut = (keepReminder) => {
    let position = balancedEnds.length - 1
    let keptReminder = false
    if (keepReminder && options.redelivered?.has?.(balancedEnds[position])) {
      position -= 1
      keptReminder = true
    }
    let endIndex = balancedEnds[position]
    if (endIndex === undefined || endIndex < startIdx) return null
    let keptReply = -1
    // Optionally keep a trailing assistant reply (no tool call) verbatim so the
    // user-visible answer of the current step is not folded into the state. A
    // finished turn keeps its final answer for the follow-up that answers to it,
    // unless a hard context budget needs the room.
    if (policy.keepTrailingReply !== false && (kind === 'active' || !relaxed) && position > 0) {
      const last = nodes[endIndex]
      const previous = balancedEnds[position - 1]
      if (last.type === 'assistant/message' && (last.toolCallCount ?? 0) === 0 && previous >= startIdx) {
        keptReply = endIndex
        endIndex = previous
        position -= 1
      }
    }
    // Optional continuity: leave the newest observation raw. The next request
    // then carries the state + that observation + the new action, which stops
    // the "I folded it away, let me read it again" loop. Only the working state
    // has a next action; in a finished turn the observation would never fold.
    if (policy.keepLastObservation === true && kind === 'active' && nodes[endIndex]?.type === 'tool/result') {
      const previous = balancedEnds[balancedEnds.length - 2]
      if (previous !== undefined && previous >= startIdx) endIndex = previous
    }
    return { endIndex, keptReminder, keptReply }
  }
  /** Completed actions and character weight of the span that ends at `endIndex`. */
  const measure = (endIndex) => {
    let chars = 0
    let actions = 0
    let open = 0
    for (let index = startIdx; index <= endIndex; index += 1) {
      const node = nodes[index]
      chars += node.textChars ?? 0
      if (node.type === 'assistant/message') {
        open += node.toolCallCount ?? (node.hasToolCall === true ? 1 : 0)
      } else if (node.type === 'tool/result') {
        open = Math.max(0, open - 1)
        if (open === 0) actions += 1
      }
    }
    return { chars, actions }
  }
  // An ordinary fold (the trigger's action count is met) cuts exactly where it
  // always did. A fold granted only for a repeated objective keeps the newest
  // delivery raw — it is the reminder the model is meant to see right before
  // its next action — unless that cut holds no re-delivery to dedupe at all.
  const dedupeFold = dedupe && actions < requiredActions
  let cut = selectCut(dedupeFold)
  if (cut === null) return null
  if (cut.keptReminder && !containsAny(options.redelivered, startIdx, cut.endIndex)) cut = selectCut(false)
  if (cut === null) return null
  const endIndex = cut.endIndex

  // Recompute the chosen span's weight: endIndex may have moved back.
  let spanChars = 0
  let spanActions = 0
  let material = 0
  const released = []
  for (let index = startIdx; index <= endIndex; index += 1) {
    const node = nodes[index]
    if (node.isCheckpoint !== true) material += 1
    if (options.released?.has(index) === true) released.push(index)
  }
  ;({ chars: spanChars, actions: spanActions } = measure(endIndex))
  const estimatedTokens = Math.ceil(spanChars / (policy.charsPerToken ?? SAC_DEFAULTS.charsPerToken))
  // Re-summarizing a state on its own can only come out the same size.
  if (kind !== 'active' && material === 0) return null
  // An archive fold is only worth a model call once it actually releases a turn.
  if (kind === 'archive') {
    if (released.length === 0) return null
    if (estimatedTokens < archiveMinTokensFor(policy, (options.carriedChars ?? 0)
      + released.reduce((sum, index) => sum + (nodes[index].textChars ?? 0), 0))) return null
  }
  if (spanChars < minSpanChars) return null
  if (estimatedTokens < minSpanTokens) return null
  if (spanActions < requiredActions && !dedupe && !loneHeavyReply) return null
  // The action-free fold is granted for a repeated objective only: the cut
  // must still contain one, and it is made without a model call.
  const dedupeOnly = dedupe && spanActions < requiredActions
  if (dedupeOnly && !containsAny(options.redelivered, startIdx, endIndex)) return null

  // A final reply that carried heavy reasoning is dead weight once its turn is
  // over. When the fold is a harness-written digest, the reply may go with it
  // (its visible text stays, its reasoning does not), so the span is evaluated
  // with the reply first.
  const replyIndex = cut.keptReply
  const reply = replyIndex >= 0 ? nodes[replyIndex] : undefined
  const wantsReply = kind !== 'archive' && policy.replyReasoningFoldChars > 0
    && (reply?.reasoningChars ?? 0) >= policy.replyReasoningFoldChars
  const withReply = wantsReply ? measure(replyIndex) : undefined
  const withReplyTokens = wantsReply ? Math.ceil(withReply.chars / (policy.charsPerToken ?? SAC_DEFAULTS.charsPerToken)) : 0
  let replyFolded = false
  const modeOut = { digestClip: 0 }
  let mode = wantsReply
    ? chooseFoldMode(nodes, startIdx, replyIndex, withReplyTokens, kind, policy, options, relaxed, dedupeOnly || loneHeavyReply, modeOut)
    : null
  let endIndexFinal = endIndex
  let charsFinal = spanChars
  let tokensFinal = estimatedTokens
  let actionsFinal = spanActions
  if (mode === 'mechanical') {
    replyFolded = true
    endIndexFinal = replyIndex
    charsFinal = withReply.chars
    tokensFinal = withReplyTokens
    actionsFinal = withReply.actions
  } else {
    mode = chooseFoldMode(nodes, startIdx, endIndex, estimatedTokens, kind, policy, options, relaxed, dedupeOnly || loneHeavyReply, modeOut)
  }
  if (mode === null) return null
  const span = {
    startIndex: startIdx,
    endIndex: endIndexFinal,
    actions: actionsFinal,
    chars: charsFinal,
    estimatedTokens: tokensFinal,
    kind,
    anchorIndex: anchorIdx,
    released,
    mechanical: mode === 'mechanical',
    // The per-result clip the digest must use to stay inside the mechanical budget.
    digestClip: modeOut.digestClip,
    replyFolded,
  }
  span.reason = `sac:${kind}:${span.actions}-action(s)`
  return span
}

/** Whether any index in `[from, to]` is in the set. */
function containsAny(indices, from, to) {
  if (!indices) return false
  for (const index of indices) {
    if (index >= from && index <= to) return true
  }
  return false
}

/** The recap size a gain test expects, in estimated tokens. */
export function recapExpectation(policy = SAC_DEFAULTS, observed = 0) {
  const words = policy.stateMaxWords ?? 0
  const derived = words > 0
    ? Math.min(policy.stateBudget ?? SAC_DEFAULTS.stateBudget, Math.round(words * 1.4))
    : Math.min(policy.stateBudget ?? SAC_DEFAULTS.stateBudget, 640)
  const configured = policy.recapExpectedTokens ?? 0
  if (configured > 0) return configured
  return observed > 0 ? Math.round((observed + derived) / 2) : derived
}

/** Estimated tokens of the digest the harness would write for a span. */
export function mechanicalDigestEstimate(nodes, startIdx, endIndex, policy = SAC_DEFAULTS) {
  const cpt = policy.charsPerToken ?? SAC_DEFAULTS.charsPerToken
  const clip = policy.digestResultChars ?? SAC_DEFAULTS.digestResultChars
  let chars = 90
  for (let index = startIdx; index <= endIndex; index += 1) {
    const node = nodes[index]
    const text = node.textChars ?? 0
    if (node.type === 'user/message') {
      chars += node.isCheckpoint === true ? Math.max(0, text - 420) : (node.sourceKind === 'goal' ? 60 : Math.min(text, 240) + 30)
    } else if (node.type === 'assistant/message') {
      // The digest keeps a tool call's arguments and any visible text, never its reasoning.
      const replyCap = policy.digestReplyChars ?? SAC_DEFAULTS.digestReplyChars
      if ((node.toolCallCount ?? 0) > 0) chars += (node.argsChars ?? text) + 50 + Math.min(node.plainChars ?? 0, replyCap)
      else chars += Math.min(node.plainChars ?? text, replyCap) + 40
    } else if (node.type === 'tool/result') {
      chars += Math.min(text, clip) + 30
    } else {
      chars += Math.min(text, clip) + 30
    }
  }
  return Math.ceil(chars / cpt)
}

/**
 * The per-result clip and the size of the digest the harness would write for a
 * span, shrunk to fit the mechanical budget when `digestShrinkToBudget` is on:
 * a finished turn with many results would otherwise never be digestible (every
 * result at its full clip outgrows the budget), although the head of each
 * result is exactly what later turns need. The clip shrinks uniformly; when
 * even the smallest clip does not fit, the returned `clip` is the smallest one
 * and `tokens` is its size (the caller then declines the mechanical fold).
 * @param {Array<object>} nodes - surface nodes.
 * @param {number} startIdx - first index of the span.
 * @param {number} endIndex - last index of the span.
 * @param {object} policy - resolved policy.
 * @returns {{clip: number, tokens: number}} the clip and the digest size in estimated tokens.
 */
export function mechanicalDigestFit(nodes, startIdx, endIndex, policy = SAC_DEFAULTS) {
  const clip = policy.digestResultChars ?? SAC_DEFAULTS.digestResultChars
  const tokens = mechanicalDigestEstimate(nodes, startIdx, endIndex, policy)
  const budget = policy.mechanicalFoldMaxTokens > 0 ? policy.mechanicalFoldMaxTokens : Number.POSITIVE_INFINITY
  if (policy.digestShrinkToBudget !== true || tokens <= budget) return { clip, tokens }
  const MIN_CLIP = 120
  const size = (value) => mechanicalDigestEstimate(nodes, startIdx, endIndex, { ...policy, digestResultChars: value })
  let low = MIN_CLIP
  let high = clip
  let best = null
  while (low <= high) {
    const mid = Math.floor((low + high) / 2)
    const at = size(mid)
    if (at <= budget) {
      best = { clip: mid, tokens: at }
      low = mid + 1
    } else {
      high = mid - 1
    }
  }
  return best ?? { clip: MIN_CLIP, tokens: size(MIN_CLIP) }
}

/**
 * Decide how a planned span folds: `mechanical` (a harness-written digest, no
 * model call), `llm` (a model-written recap), or `null` when the fold is not
 * worth making under the configured guards.
 * @param {object} [out] - receives `digestClip`, the per-result clip a
 *   mechanical fold must use (the runtime writes the digest with the same clip).
 * @returns {'mechanical'|'llm'|null} the fold mode.
 */
function chooseFoldMode(nodes, startIdx, endIndex, estimatedTokens, kind, policy, options, relaxed, dedupeOnly, out = {}) {
  // An archive merges the outcomes of released turns: a model writes it.
  if (kind === 'archive') return 'llm'
  // The harness writes the fold itself when the digest it would write is small
  // and clearly smaller than the span. What the span weighs does not matter on
  // its own: hidden reasoning leaves a digest for free.
  const selfWritten = kind === 'frozen' || dedupeOnly || policy.mechanicalSegments === 'all'
  if (policy.mechanicalFoldMaxTokens > 0 && selfWritten) {
    const fit = mechanicalDigestFit(nodes, startIdx, endIndex, policy)
    const minGain = relaxed ? 1 : (policy.mechanicalMinGainTokens ?? SAC_DEFAULTS.mechanicalMinGainTokens)
    if (fit.tokens <= policy.mechanicalFoldMaxTokens && estimatedTokens - fit.tokens >= minGain) {
      out.digestClip = fit.clip
      return 'mechanical'
    }
  }
  // A repeated objective without any work in it is not worth a model call:
  // the fold is granted only for the harness-written digest.
  if (dedupeOnly) return null
  const minGain = relaxed ? 0 : (policy.foldMinGainTokens ?? 0)
  if (minGain > 0 && estimatedTokens - recapExpectation(policy, options.expectedRecapTokens) < minGain) return null
  return 'llm'
}

/**
 * Compute the inclusive index span that the next SAC step should compact.
 *
 * The span lies inside one segment of the surface (see {@link planSegments}),
 * starts right after the segment's pinned instruction so a previously written
 * semantic state is absorbed (updated) rather than duplicated, and ends at the
 * newest balanced cut that still leaves the trailing reply and (optionally)
 * the newest observation on the surface. A user instruction is never inside
 * the span, except a re-delivery of its own segment's instruction.
 *
 * The working state goes first. Finished turns and the archive are folded in
 * the steps the working state leaves free, and never before the model has
 * acted on the newest request: answering the user is not held up by a fold.
 *
 * With `foldTrigger: 'size'` nothing folds while the whole request is under
 * `foldAboveTokens`; above it the same order applies, one span per call.
 *
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @param {object} [policy] - resolved policy (see {@link resolvePolicy}).
 * @param {{relaxed?: boolean, carriedChars?: number, requestTokens?: number, expectedRecapTokens?: number}} [options] -
 *   size-guard override for the hard context budget, the archive's carried
 *   size, the estimated tokens of the whole next request (system prompt, tool
 *   schemas and surface; defaults to the surface alone), and the recap size
 *   this agent's folds have actually produced.
 * @returns {{startIndex: number, endIndex: number, actions: number, chars: number, estimatedTokens: number,
 *   reason: string, kind: string, anchorIndex: number, released: number[], mechanical: boolean}|null}
 *   the span to compact, or `null` when SAC must not run this step.
 */
export function planSacSpan(nodes, policy = SAC_DEFAULTS, options = {}) {
  const layout = planSegments(nodes, policy, { turnEnding: options.turnEnding === true })
  if (layout === null) return null
  const sizeMode = policy.foldTrigger === 'size' && options.relaxed !== true
  if (sizeMode) {
    const requestTokens = options.requestTokens ?? surfaceTokensOf(nodes, policy)
    if (requestTokens < (options.foldAboveTokens ?? policy.foldAboveTokens ?? 0)) return null
  }
  const released = new Set(layout.released)
  const redelivered = new Set(layout.redelivered)
  const working = layout.segments[0]
  const answering = options.relaxed !== true && working !== undefined
    && (working.kind !== 'active' || completedActions(nodes, working.startIdx, working.endIdx) < 1)
  const requiredActions = sizeMode ? 1 : undefined
  for (const segment of layout.segments) {
    const span = planSegment(nodes, segment, policy, {
      ...options,
      released,
      redelivered,
      requiredActions,
    })
    if (span === null) continue
    // A model-written recap of an earlier turn waits until the model has acted
    // on the newest request; a digest the harness writes itself costs no call
    // and no waiting, so it may free the surface before the very first step.
    if (answering && segment.kind !== 'active' && span.mechanical !== true) continue
    return span
  }
  return null
}

/**
 * The two marks of the size trigger for one request shape.
 * @param {object} policy - resolved policy.
 * @param {number} fixedTokens - estimated tokens of system prompt and tool schemas.
 * @returns {{above: number, below: number}} high- and low-water marks (0 = the trigger never fires).
 */
export function effectiveMarks(policy = SAC_DEFAULTS, fixedTokens = 0) {
  if ((policy.foldAboveTokens ?? 0) > 0) return { above: policy.foldAboveTokens, below: policy.foldBelowTokens }
  if ((policy.foldMarginTokens ?? 0) > 0) {
    const margin = policy.foldMarginTokens
    return { above: Math.max(1, fixedTokens + margin), below: Math.max(1, fixedTokens + Math.floor(margin / 2)) }
  }
  return { above: 0, below: 0 }
}

/** Estimated tokens of everything on the surface. */
export function surfaceTokensOf(nodes, policy = SAC_DEFAULTS) {
  if (!Array.isArray(nodes)) return 0
  return Math.ceil(nodes.reduce((sum, node) => sum + (node.textChars ?? 0), 0) / (policy.charsPerToken ?? SAC_DEFAULTS.charsPerToken))
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
