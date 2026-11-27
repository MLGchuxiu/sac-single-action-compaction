/**
 * `dsh-sac-compact` — Single-Action Compaction (SAC) for the DeepSeek Harness.
 *
 * Implements the context-management regime introduced in
 * "You don't need long context" (arXiv:2611.05306, §2.4.1, §5): after every
 * completed action the whole interaction so far is folded into one compact
 * *semantic state*, so each model request carries only
 *
 *     system prompt + task + semantic state + the single next action
 *
 * instead of a growing transcript.
 *
 * Design notes
 * ------------
 * - The plugin is a *policy* layer, not a second compaction backend. It drives
 *   the mounted `ctx.compaction` service (normally `dsh-compaction-basic`) with
 *   `compactRegion()`, which already owns the durable bracket, surface replace,
 *   checkpoint marker, token accounting and locking.
 * - Range selection is pure and lives in `lib/sac-policy.js`.
 * - Three state modes:
 *     `native`  — keep the backend's default summarizer.
 *     `paper`   — the paper's Appendix G.1 handoff-recap instruction (one extra
 *                 model call per fold).
 *     `inline`  — the model writes a `<state>…</state>` block at the start of
 *                 every reply; `summarize()` extracts it and makes **no** extra
 *                 model call. This is the v9 pipeline.
 * - v9 pipeline additions (all opt-in): a deterministic machine `ledger` of
 *   folded actions, a generated subgoal `checklist` with a `check` tool, a
 *   `recall` tool over the archived transcript, and an adaptive state budget.
 * - Multi-turn sessions: every user instruction is pinned verbatim and only the
 *   work *between* instructions is folded, so a follow-up request replaces the
 *   previous objective instead of being summarized away. The surface becomes
 *
 *     system prompt + [archive] + request₁ + state₁ + … + requestₙ + stateₙ + next action
 *
 *   The state update is anchored on the pinned request it belongs to, and a
 *   request that is released into the archive is still quoted there verbatim.
 *
 * @module dsh-sac-compact
 */
import {
  CHECKPOINT_SOURCE_KIND,
  SAC_DEFAULTS,
  instructionKey,
  isInstructionNode,
  planSacSpan,
  effectiveMarks,
  resolvePolicy,
  stateBudgetFor,
  surfaceTokensOf,
  withinTurnBudget,
} from './lib/sac-policy.js'

export const name = 'dsh-sac-compact'

/**
 * Optional services. `compaction` is the backend we drive; `llm` is used by the
 * paper/inline fallback and checklist generation; `systemPrompt` and `tools`
 * are only needed by the v9 pipeline.
 */
export const inject = { optional: ['compaction', 'llm', 'systemPrompt', 'tools'] }

/** Per-agent SAC bookkeeping. */
const stateByAgent = new WeakMap()

/**
 * Marks an engine instance whose `summarize` hook this module instance wrapped.
 * The value carries a per-module revision, so a hot-reloaded module re-installs
 * its hook instead of trusting a marker left by the previous module instance.
 */
const INSTALLED = Symbol.for('dsh-sac-compact.installed')
const INSTALL_REV = `rev-${Math.random().toString(36).slice(2, 10)}`

/** Create the per-agent telemetry record. */
function newTelemetry() {
  return {
    steps: 0,
    compactions: 0,
    foldedTokens: 0,
    foldedActions: 0,
    lastFoldActions: 0,
    skipped: 0,
    lastSkipReason: '',
    installAttempts: 0,
    installFailures: 0,
    installedOn: null,
    summarizeCalls: 0,
    summarizeChars: 0,
    summarizeErrors: 0,
    summarizeRetries: 0,
    inlineHits: 0,
    inlineMisses: 0,
    inlineNudges: 0,
    inlineBroken: false,
    budgetForcedFolds: 0,
    ledgerLines: 0,
    checklistItems: 0,
    recallCalls: 0,
    checkCalls: 0,
    frozenFolds: 0,
    archiveFolds: 0,
    carriedRequests: 0,
    refusedFolds: 0,
    mechanicalFolds: 0,
    truncatedRecaps: 0,
    stateRetries: 0,
    stateRerouted: 0,
    mechanicalRefusals: 0,
    sizeForcedFolds: 0,
    replyFolds: 0,
  }
}

/**
 * Resolve an optional service without going through the property inject guard.
 * Preset entry groups do not always propagate a plugin's static `inject`; using
 * `ctx.get(name)` keeps the plugin working in both root and preset mounts.
 * @param {object} ctx - plugin context.
 * @param {string} name - service name.
 * @returns {object|undefined} the service, when mounted.
 */
function serviceOf(ctx, name) {
  const viaGet = typeof ctx.get === 'function' ? ctx.get(name) : undefined
  return viaGet ?? ctx[name]
}

/** Create the per-agent SAC state. */
function createState() {
  return {
    compactions: 0,
    lastTurn: undefined,
    warned: new Set(),
    telemetry: newTelemetry(),
    ledger: [],
    checklist: undefined,
    checklistRequested: false,
    done: new Set(),
    nudges: 0,
    /** Fold the pre-step is about to run: `{kind, anchor, released}`. */
    pendingFold: undefined,
    /** Earlier user requests the archive state quotes verbatim, newest first. */
    requests: [],
    /** How often the backend refused a finished-turn or archive span as "not smaller". */
    refusedSpans: new Map(),
    /** Exponential average of the recaps this agent's folds produced (chars). */
    recapEmaChars: 0,
    /** Output cap of the state-update call; doubles (and stays up) once a recap is cut off. */
    stateCap: 0,
  }
}

/**
 * Count the characters a state update wrote.
 * @param {object} result - `summarize()` return value.
 * @returns {number} total characters of the summary blocks.
 */
function summaryChars(result) {
  const blocks = result?.summary
  if (!Array.isArray(blocks)) return 0
  let total = 0
  for (const block of blocks) {
    if (typeof block?.text === 'string') total += block.text.length
  }
  return total
}

/**
 * Whether this module instance already wrapped the engine's state update.
 * @param {object} engine - compaction backend instance.
 * @returns {boolean} true when this revision's hook is installed.
 */
export function stateUpdateInstalled(engine) {
  return engine?.[INSTALLED]?.rev === INSTALL_REV
}

/**
 * Read the SAC telemetry of one agent. Returns a copy, so callers cannot
 * mutate the counters.
 * @param {object} agent - agent whose counters to read.
 * @returns {object|null} counters, or null when this agent never ran SAC.
 */
export function telemetrySnapshot(agent) {
  const state = stateByAgent.get(agent)
  return state === undefined ? null : { ...state.telemetry }
}

/**
 * What the paper's recap must record after the objective line.
 *
 * Section 7 says "(none)" when the user asked for no format: asked for "the
 * exact response format the task requires", a summarizer invents a JSON
 * contract, and every later fold then treats that invention as the objective.
 */
const PAPER_RECORD = [
  '2. Established facts - exact paths, relevant symbols or commands, observed values,',
  '   and important errors/output. Separate observations from unverified ideas.',
  '3. Applied changes - each changed/created file, what changed, and whether verified.',
  '4. Verification - exact checks run, outcomes, failures, obstacles, and workarounds.',
  '5. Dead ends - failed approaches that should not be repeated.',
  '6. Remaining plan - outstanding subgoals, in execution order; the immediate next',
  '   command or edit; and the condition for completion.',
  '7. Output contract - the exact response format the user asked for (JSON keys, units,',
  '   file paths, answer shape), so the next instance cannot get it wrong. If the user',
  '   did not specify one, write "(none)" and do not invent one.',
  '8. Verbatim values - ids, numbers, paths, command lines, symbol and file names',
  '   copied exactly as observed.',
]

/**
 * The paper's Appendix G.1 compaction instruction, adapted to a harness that
 * must also keep the semantic state usable by a *separate* inference instance.
 *
 * The objective is taken from the user's request, never from what the last
 * actions were doing: a state update that only sees the last two actions
 * rewrites the objective into their sub-task, and the next instance follows it.
 * @param {{anchored?: boolean, maxWords?: number, attempt?: number}} [options] -
 *   `anchored`: the request is quoted as the first user message of the request;
 *   `maxWords`: recap length contract (0 = none); `attempt`: retry wording.
 * @returns {string} the instruction appended as the final user message.
 */
export function paperStatePrompt({ anchored = true, maxWords = 0, attempt = 0, cutOff = false, carryFacts = false } = {}) {
  const lines = [
    'Stop work. Write only a concise handoff recap; do not run commands or call tools.',
    anchored
      ? 'The first user message above is the request this work belongs to. The conversation will reset'
        + ' to the system prompt, that request, and this recap.'
      : 'The conversation will reset to the system prompt, the user\'s request, and this recap.',
    '',
    'Record:',
  ]
  if (anchored) {
    lines.push(
      '1. Objective - one line: the result the user asks for in that first message. Take it from',
      '   the message itself, never from whatever the last actions happened to be doing;',
      '   sub-tasks belong in the plan below, not here.',
    )
  } else {
    lines.push(
      '1. Objective - one line: the result the user most recently asked for. Take it from that',
      '   request itself, never from whatever the last actions happened to be doing;',
      '   sub-tasks belong in the plan below, not here.',
    )
  }
  lines.push(...PAPER_RECORD, '', 'If needed output was truncated, give a narrow command to retrieve it.')
  lines.push('Output the recap directly, with no preamble and no visible reasoning.')
  if (maxWords > 0) {
    lines.push(`Keep the whole recap under ${maxWords} words; prefer exact values over prose.`)
  }
  if (carryFacts) {
    // Without this a summarizer that is asked to stay short replaces an accumulated
    // table of values ("records 1-60 extracted") with a count of it, and the agent
    // has to read everything again.
    lines.push('An earlier recap may be among the material above: carry its established facts and verbatim')
    lines.push('values forward in full. Never replace them with a count or a summary of them.')
  }

  if (cutOff) {
    lines.push('Your previous reply was cut off before the recap was complete. Write the complete recap')
    lines.push('now, from its first section to its last; keep any reasoning internal and brief.')
  }
  if (attempt > 0) lines.push('Output the recap now, directly, with no preamble and no reasoning.')
  return lines.join('\n')
}

/** The paper template without a quoted request (kept for callers and tests). */
export const PAPER_STATE_PROMPT = paperStatePrompt({ anchored: false })

/**
 * Instruction for a fold that merges released turns into the archive state.
 * The requests themselves are quoted verbatim by the harness, so the recap
 * records their outcomes and the facts later requests may need — and must not
 * present an earlier request as the current objective.
 * @param {{maxWords?: number, attempt?: number}} [options] - length contract and retry wording.
 * @returns {string} the instruction appended as the final user message.
 */
export function archiveStatePrompt({ maxWords = 0, attempt = 0, cutOff = false, carryFacts = false } = {}) {
  const lines = [
    'Stop work. Write only a concise archive recap; do not run commands or call tools.',
    'The messages above are earlier, superseded parts of this session: user requests, the state',
    'reached for each of them, and leftover actions. The request being worked on now and its',
    'working state are not part of this span, and the requests are kept verbatim by the harness.',
    '',
    'Record:',
    '1. Outcomes - for each earlier request, in order: what was delivered, or where and why it stopped.',
    '2. Established facts - exact paths, ids, values, endpoints and errors from this earlier work',
    '   that later requests may need. Separate observations from unverified ideas.',
    '3. Applied changes - each file changed or created, and whether it was verified.',
    '4. Dead ends - approaches already tried and failed, so they are not repeated.',
    '',
    'Do not restate the requests as goals and do not propose next steps.',
    'Output the recap directly, with no preamble and no visible reasoning.',
  ]
  if (maxWords > 0) {
    lines.push(`Keep the whole recap under ${maxWords} words; prefer exact values over prose.`)
  }
  if (carryFacts) {
    lines.push('An earlier recap may be among the material above: carry its established facts and verbatim')
    lines.push('values forward in full. Never replace them with a count or a summary of them.')
  }
  if (cutOff) {
    lines.push('Your previous reply was cut off before the recap was complete. Write the complete recap')
    lines.push('now, from its first section to its last; keep any reasoning internal and brief.')
  }
  if (attempt > 0) lines.push('Output the recap now, directly, with no preamble and no reasoning.')
  return lines.join('\n')
}

/** The archive template (kept for callers and tests). */
export const ARCHIVE_STATE_PROMPT = archiveStatePrompt()

/** Longest request text quoted in front of the state-update input. */
const ANCHOR_MAX_CHARS = 12_000

/**
 * The system-prompt section that makes the inline pipeline work: the model must
 * open every reply with a full state block, because the harness keeps only it.
 */
export const INLINE_SYSTEM_SECTION = [
  'You are running under single-action compaction.',
  'Before calling tools, begin every reply with a `<state>...</state>` block containing the full',
  'updated recap (objective, anchors, exact facts, changes, verification, open items, next step).',
  'The harness keeps that block as the only memory of the step and discards the rest of the transcript.',
  'Keep the block terse: under 120 words, exact values only; do not restate the task or narrate.',
  'After the block, call your tools.',
  'When you are ready to answer the user without calling tools, give the final answer normally and',
  'do not include a `<state>` block: the turn ends there and no further step needs the recap.',
].join('\n')

/** Prompt used once per agent to generate the v9 subgoal checklist. */
export const CHECKLIST_PLAN = [
  'Before you start, write the checklist of subgoals this task requires.',
  'Output 3-8 short imperative items, one per line, no numbering, no commentary.',
  'Each item must be independently checkable. Do not include the final answer.',
].join('\n')

/** Markers around the machine-maintained parts of a state. */
const LEDGER_OPEN = '[SAC LEDGER]'
const LEDGER_CLOSE = '[/SAC LEDGER]'
const CHECKLIST_OPEN = '[SAC CHECKLIST]'
const CHECKLIST_CLOSE = '[/SAC CHECKLIST]'
const REQUESTS_OPEN = '[SAC EARLIER USER REQUESTS]'
const REQUESTS_CLOSE = '[/SAC EARLIER USER REQUESTS]'
const DIGEST_OPEN = '[SAC ACTION DIGEST]'
const DIGEST_CLOSE = '[/SAC ACTION DIGEST]'

/**
 * The framing every compaction backend puts around a summary (mirrors
 * `frameSummary()` of `@deepseek-ai/dsh-compaction-basic`): the harness sizes
 * its own replacements in the same tokens the backend's shrink check uses.
 */
const CHECKPOINT_PREAMBLE = 'This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint.'
const SUMMARY_OPEN_TAG = '<compacted-summary>'
const SUMMARY_CLOSE_TAG = '</compacted-summary>'

/**
 * Replace standing harness notices (the workspace instructions of AGENTS.md) in
 * the state-update input by a one-line note: the harness re-supplies them on the
 * surface after a fold that covers them, so a recap need not restate them.
 * @param {Array<object>} messages - the span's derived messages.
 * @param {object} policy - resolved policy.
 * @returns {Array<object>} messages without the standing notices.
 */
export function omitStandingNotices(messages, policy = SAC_DEFAULTS) {
  if (!Array.isArray(messages)) return messages
  let changed = false
  const out = messages.map((message) => {
    if (message?.role !== 'user' || !policy.standingSourceKinds.includes(message.source?.kind)) return message
    changed = true
    return { role: 'user', content: [{ type: 'text', text: '(the workspace instructions the harness delivered here are left out of this recap: the harness supplies them again)' }] }
  })
  return changed ? out : messages
}

/** The backend's fixed text density: four characters per token plus block and role overhead. */
const HOST_CHARS_PER_TOKEN = 4
const HOST_BLOCK_OVERHEAD = 4
const HOST_ROLE_OVERHEAD = 4

/** Smallest recap budget (host tokens) worth a model call. */
const MIN_RECAP_TOKENS = 96
/**
 * The recap length contract handed to the summarizer, in words (0 = none).
 *
 * Under a span-relative bound (`recapMaxRatio`) the recap may grow with the
 * span: a fixed word cap made the summarizer drop accumulated exact values (a
 * table of 60+ records carried across folds) once the state outgrew it. Half
 * the span in words is the room, never more than four times `stateMaxWords`;
 * the budget stays the ceiling. A retry after a cut-off keeps that contract —
 * the output cap doubles instead; asking for fewer words made the summarizer
 * drop half of the accumulated values. Without the bound the 1.2.0 contract is
 * unchanged: the smaller of `stateMaxWords` and the budget, ×0.6 on a retry.
 * @param {object} policy - resolved policy.
 * @param {number} spanTokens - size of the folded span.
 * @param {number} budget - recap budget in tokens (0 = none).
 * @param {number} attempt - 0 for the first attempt; a retry asks for less.
 * @returns {number} word limit (0 = none).
 */
export function recapWordLimit(policy, spanTokens, budget, attempt = 0) {
  if (!(policy.stateMaxWords > 0) && !(budget > 0)) return 0
  const fromBudget = budget > 0 ? Math.floor(budget * WORDS_PER_TOKEN) : Number.POSITIVE_INFINITY
  let words = policy.stateMaxWords > 0 ? Math.min(policy.stateMaxWords, fromBudget) : fromBudget
  if (policy.recapMaxRatio > 0 && spanTokens > 0) {
    const base = policy.stateMaxWords > 0 ? policy.stateMaxWords : 0
    const fromSpan = Math.floor(spanTokens * WORDS_PER_TOKEN * 0.5)
    words = Math.min(fromBudget, Math.max(base, base > 0 ? Math.min(fromSpan, base * 4) : fromSpan))
  }
  if (attempt > 0 && !(policy.recapMaxRatio > 0)) words = Math.floor(words * 0.6)
  return Math.max(60, words)
}

/** Roughly three words per four tokens. */
const WORDS_PER_TOKEN = 0.75
/** Section headings of the paper recap, in template order. */
const PAPER_SECTIONS = ['objective', 'established facts', 'applied changes', 'verification', 'dead ends', 'remaining plan', 'output contract', 'verbatim values']
/** Section headings of the archive recap. */
const ARCHIVE_SECTIONS = ['outcomes', 'established facts', 'applied changes', 'dead ends']

/**
 * Price one text the way the compaction backend prices a summary block.
 * @param {string} text - summary text.
 * @returns {number} estimated host tokens.
 */
function hostTextTokens(text) {
  return Math.ceil(text.length / HOST_CHARS_PER_TOKEN) + HOST_BLOCK_OVERHEAD
}

/**
 * Tokens the backend adds around a summary: the framed preamble, the closing
 * tag, and the role of the checkpoint message, plus a safety margin.
 * @returns {number} estimated host tokens.
 */
export function framingTokens() {
  return hostTextTokens(`${CHECKPOINT_PREAMBLE}\n\n${SUMMARY_OPEN_TAG}`)
    + hostTextTokens(SUMMARY_CLOSE_TAG) + HOST_ROLE_OVERHEAD + 16
}

/**
 * Strip the backend's framing from a checkpoint text, so a fold that carries
 * an earlier state forward does not nest one checkpoint inside another.
 * @param {string} text - checkpoint text.
 * @returns {string} the state text alone.
 */
export function stripCheckpointFraming(text) {
  if (typeof text !== 'string') return ''
  return text
    .replace(/^\s*This is an automatically generated checkpoint[^\n]*\n+<compacted-summary>\s*/, '')
    .replace(/\s*<\/compacted-summary>\s*$/, '')
    .trim()
}

/**
 * Shorten a tool result for a digest line without losing its structure.
 *
 * Structured results (JSON) are re-serialized compactly, which alone removes
 * the pretty-printing; when still too long, long arrays keep their first items
 * and a count, and long strings their head — keys, ids and numbers of the kept
 * items stay exact, unlike a cut through the middle of the text. Anything that
 * is not JSON is clipped head and tail.
 * @param {string} text - the tool result.
 * @param {number} max - line budget in characters.
 * @returns {string} the shortened result.
 */
export function shortenResult(text, max) {
  const flat = text.trim()
  let value
  try {
    value = JSON.parse(flat)
  } catch {
    return clipLine(flat, max)
  }
  const compact = JSON.stringify(value)
  if (compact.length <= max) return compact
  const shrink = (node, keepItems, keepChars) => {
    if (Array.isArray(node)) {
      const kept = node.slice(0, keepItems).map((item) => shrink(item, keepItems, keepChars))
      return node.length > keepItems ? [...kept, `…[${node.length - keepItems} more]…`] : kept
    }
    if (node !== null && typeof node === 'object') {
      const out = {}
      for (const key of Object.keys(node)) out[key] = shrink(node[key], keepItems, keepChars)
      return out
    }
    if (typeof node === 'string' && node.length > keepChars) return `${node.slice(0, keepChars)}…[+${node.length - keepChars} chars]`
    return node
  }
  for (const [keepItems, keepChars] of [[12, 400], [6, 200], [3, 100], [1, 40]]) {
    const candidate = JSON.stringify(shrink(value, keepItems, keepChars))
    if (candidate !== undefined && candidate.length <= max) return candidate
  }
  return clipLine(compact, max)
}

/** Producer kinds of harness notices whose text is worthless once the step is over. */
const VOLATILE_NOTICES = new Set(['time-context', 'tool-jobs', 'runtime-context', 'skill-catalog', 'working-directory', 'user-approval', 'tool-goal'])

/** Head/tail clip of one digest line. */
function clipLine(text, max) {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  const head = Math.floor(max * 0.8)
  const tail = Math.max(0, max - head)
  return `${flat.slice(0, head)} …[${flat.length - max} chars clipped]… ${flat.slice(-tail)}`
}

/* -------------------------------------------------------------------------- */
/* Text helpers                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Read the text blocks of one message.
 * @param {object} message - derived model message.
 * @returns {string} concatenated text.
 */
export function messageText(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  let text = ''
  for (const block of content) {
    if (block?.type === 'text' && typeof block.text === 'string') text += block.text
  }
  return text
}

/**
 * Extract the body of the first `<state>…</state>` block.
 * @param {string} text - assistant text.
 * @returns {string|null} trimmed body, or null when absent/too short.
 */
export function extractStateBlock(text) {
  if (typeof text !== 'string') return null
  const match = /<state>([\s\S]*?)<\/state>/i.exec(text)
  if (match === null) return null
  const body = match[1].trim()
  return body.length >= 40 ? body : null
}

/**
 * Find the newest assistant-written state block in a folded span.
 * @param {Array<object>} messages - shadowed conversation messages.
 * @returns {string|null} state body, or null when the model did not write one.
 */
export function extractInlineState(messages) {
  if (!Array.isArray(messages)) return null
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.role !== 'assistant') continue
    const body = extractStateBlock(messageText(message))
    if (body !== null) return body
  }
  return null
}

/**
 * Newest assistant-written state block on the live surface. With
 * `keepLastObservation` the newest action is pinned outside the folded span, so
 * its `<state>` block is not in `input.messages`; the surface still has it.
 * @param {object} agent - agent whose surface to scan.
 * @returns {string|null} state body, or null when none was written.
 */
export function latestSurfaceState(agent) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes)) return null
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const event = agent.session.eventAt(nodes[index])
    if (event?.type !== 'assistant/message') continue
    const body = extractStateBlock(messageText(event.data?.message))
    if (body !== null) return body
  }
  return null
}

/** Text of the newest assistant message on the surface (null when none). */
function latestAssistantText(agent) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes)) return null
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const event = agent.session.eventAt(nodes[index])
    if (event?.type !== 'assistant/message') continue
    return messageText(event.data?.message)
  }
  return null
}

/** Reminder injected when the model forgets its inline state block. */
export const INLINE_NUDGE = 'Reminder: before your next tool call, begin the reply with a terse `<state>...</state>` block (under 120 words). The harness keeps only that block.'

/**
 * Nudge the model when it forgot the inline state block. At most three times
 * per agent, so a non-compliant model does not accumulate reminders.
 * @param {object} ctx - plugin context.
 * @param {object} agent - agent.
 * @param {object} state - per-agent SAC state.
 * @param {object} policy - resolved policy.
 * @returns {void}
 */
function nudgeInline(ctx, agent, state, policy) {
  if (policy.statePrompt !== 'inline') return
  if (state.inlineBroken === true) return
  if ((state.nudges ?? 0) >= 3) return
  const text = latestAssistantText(agent)
  if (text === null || extractStateBlock(text) !== null) return
  try {
    agent.inject({ id: crypto.randomUUID(), role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: INLINE_NUDGE }] })
    state.nudges = (state.nudges ?? 0) + 1
    state.telemetry.inlineNudges += 1
  } catch (error) {
    ctx.logger?.warn?.(`dsh-sac-compact: inline nudge failed (${String(error?.message ?? error)})`)
  }
}

/**
 * Remove previously rendered machine sections so a fold never nests them.
 * @param {string} text - state text.
 * @returns {string} state text without SAC machine markers.
 */
export function stripMachineSections(text) {
  if (typeof text !== 'string') return ''
  return text
    .replace(new RegExp(`${escapeRegExp(LEDGER_OPEN)}[\\s\\S]*?${escapeRegExp(LEDGER_CLOSE)}`, 'g'), '')
    .replace(new RegExp(`${escapeRegExp(CHECKLIST_OPEN)}[\\s\\S]*?${escapeRegExp(CHECKLIST_CLOSE)}`, 'g'), '')
    .replace(new RegExp(`${escapeRegExp(REQUESTS_OPEN)}[\\s\\S]*?${escapeRegExp(REQUESTS_CLOSE)}`, 'g'), '')
    .replace(new RegExp(`${escapeRegExp(DIGEST_OPEN)}[\\s\\S]*?${escapeRegExp(DIGEST_CLOSE)}`, 'g'), '')
    .trim()
}

/** Escape a literal for use inside a RegExp. */
function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Head/tail trim with an explicit omission marker. */
function trimText(text, cap) {
  const head = Math.floor(cap * 0.7)
  const tail = Math.floor(cap * 0.2)
  const omitted = text.length - head - tail
  return `${text.slice(0, head)}\n…[${omitted} chars omitted from the state-update input; re-read the file if the exact middle matters]…\n${text.slice(-tail)}`
}

/**
 * Bound the state-update input. A single tool observation (a 40 KB document, a
 * full test log) can dominate the summarizer's prompt; the recap only needs the
 * gist of older observations, so long text blocks are head/tail trimmed.
 * @param {Array<object>} messages - shadowed conversation messages.
 * @param {number} maxChars - per-message cap (0 disables trimming).
 * @returns {Array<object>} messages safe to send to the summarizer.
 */
export function trimForSummary(messages, maxChars) {
  if (!Array.isArray(messages) || !(maxChars > 0)) return messages
  const lastIndex = messages.length - 1
  return messages.map((message, index) => {
    const cap = index === lastIndex ? Math.floor(maxChars * 1.5) : maxChars
    const blocks = message?.content
    if (typeof blocks === 'string') {
      return blocks.length <= cap ? message : { ...message, content: trimText(blocks, cap) }
    }
    if (!Array.isArray(blocks)) return message
    let changed = false
    const content = blocks.map((block) => {
      if (block?.type !== 'text' || typeof block.text !== 'string' || block.text.length <= cap) return block
      changed = true
      return { ...block, text: trimText(block.text, cap) }
    })
    return changed ? { ...message, content } : message
  })
}

/**
 * Trim only observations that are larger than `cap` chars, leaving every small
 * observation verbatim. Reading-heavy tasks fold 40 KB documents (trim them),
 * while coding tasks fold short test outputs (keep them exact).
 * @param {Array<object>} messages - shadowed conversation messages.
 * @param {number} cap - per-text-block character cap (0 disables).
 * @returns {Array<object>} messages safe to send to the summarizer.
 */
export function trimOversized(messages, cap) {
  if (!Array.isArray(messages) || !(cap > 0)) return messages
  return messages.map((message) => {
    const blocks = message?.content
    if (typeof blocks === 'string') {
      return blocks.length <= cap ? message : { ...message, content: trimText(blocks, cap) }
    }
    if (!Array.isArray(blocks)) return message
    let changed = false
    const content = blocks.map((block) => {
      if (block?.type !== 'text' || typeof block.text !== 'string' || block.text.length <= cap) return block
      changed = true
      return { ...block, text: trimText(block.text, cap) }
    })
    return changed ? { ...message, content } : message
  })
}

/**
 * Write the digest of a span the harness folds itself: every earlier state is
 * carried forward verbatim and each folded action becomes one exact line, so
 * nothing in it is paraphrased and no model call is made. Long tool results
 * and replies are clipped head and tail (`digestResultChars`).
 * @param {Array<object>} messages - the span's derived messages (system and developer messages are skipped).
 * @param {object} policy - resolved policy.
 * @returns {{text: string, lines: number, carried: number}} the digest text.
 */
export function mechanicalDigest(messages, policy = SAC_DEFAULTS) {
  const clip = Math.max(80, policy.digestResultChars ?? SAC_DEFAULTS.digestResultChars)
  let maxChars = Math.max(500, policy.digestMaxChars ?? SAC_DEFAULTS.digestMaxChars)
  if (policy.digestShrinkToBudget === true && policy.mechanicalFoldMaxTokens > 0) {
    // The budget the planner shrank the clip for, in characters: the digest may
    // use all of it instead of dropping its oldest lines at a smaller cap.
    maxChars = Math.max(maxChars, policy.mechanicalFoldMaxTokens * (policy.charsPerToken ?? SAC_DEFAULTS.charsPerToken))
  }
  const carried = []
  const lines = []
  /** Tool calls of the current message, oldest first, waiting for their result. */
  const pending = []
  const pushLine = (call) => {
    const line = { id: undefined, call, result: '' }
    lines.push(line)
    return line
  }
  for (const message of Array.isArray(messages) ? messages : []) {
    if (message?.role === 'system' || message?.role === 'developer') continue
    if (message?.role === 'user') {
      const kind = message.source?.kind
      if (kind === CHECKPOINT_SOURCE_KIND) {
        // An earlier state inside the span is carried forward as it was written;
        // its digest lines join the budget of the new one (oldest dropped first).
        const text = stripCheckpointFraming(messageText(message))
        const open = text.lastIndexOf(DIGEST_OPEN)
        if (open >= 0 && text.endsWith(DIGEST_CLOSE)) {
          if (text.slice(0, open).trim().length > 0) carried.push(text.slice(0, open).trim())
          for (const line of text.slice(open + DIGEST_OPEN.length, text.length - DIGEST_CLOSE.length).split('\n').slice(1)) {
            if (line.trim().length === 0) continue
            if (/^  → /.test(line) && lines.length > 0 && lines.at(-1).result === '') lines.at(-1).result = line
            else pushLine(line)
          }
        } else if (text.length > 0) {
          carried.push(text)
        }
        continue
      }
      if (policy.instructionSourceKinds.includes(kind)) {
        // A repeated delivery of the pinned request: the request itself stays on the surface.
        pushLine('- (the harness delivered the current request again; nothing new was said)')
        continue
      }
      // Harness notices: volatile bookkeeping (the time, job status, the runtime context)
      // is kept as a short line; a subagent's closing message carries deliverables
      // and is kept like a reply.
      const text = clipLine(messageText(message), VOLATILE_NOTICES.has(kind) ? Math.min(clip, 240) : policy.digestReplyChars ?? SAC_DEFAULTS.digestReplyChars)
      if (text.length > 0) pushLine(`- notice (${kind}): ${text}`)
      continue
    }
    if (message?.role === 'assistant') {
      for (const block of Array.isArray(message.content) ? message.content : []) {
        if (block?.type === 'tool-call') {
          const raw = typeof block.arguments === 'string' ? block.arguments : JSON.stringify(block.arguments ?? block.input ?? {})
          pushLine(`- \`${String(block.name ?? block.toolName ?? 'tool').trim()}(${clipLine(raw, clip)})\``).id = block.id
          if (pending.length < 64) pending.push(lines.at(-1))
        } else if (block?.type === 'text' && typeof block.text === 'string' && block.text.trim().length > 0) {
          // What the model told the user is worth more than a tool result: keep more of it.
          pushLine(`- assistant said: ${clipLine(block.text, policy.digestReplyChars ?? SAC_DEFAULTS.digestReplyChars)}`)
        }
      }
      continue
    }
    if (message?.role === 'tool') {
      const text = shortenResult(messageText(message), clip)
      // Attach the result to its own call: by id when both carry one, else the oldest unanswered call.
      let index = message.toolCallId === undefined ? -1 : pending.findIndex((line) => line.id === message.toolCallId)
      if (index < 0) index = pending.findIndex((line) => line.result === '')
      if (index >= 0) {
        pending[index].result = `  → ${text}`
        pending.splice(index, 1)
      } else {
        pushLine(`- tool result: ${text}`)
      }
    }
  }
  // Keep the newest lines once the digest (together with the earlier states it
  // carries) outgrows its budget.
  const carriedChars = carried.reduce((sum, part) => sum + part.length + 2, 0)
  while (lines.length > 1 && carriedChars + lines.reduce((sum, line) => sum + line.call.length + line.result.length + 2, 0) > maxChars) {
    lines.shift()
  }
  const digest = lines.map((line) => `${line.call}${line.result ? `\n${line.result}` : ''}`).join('\n')
  const parts = [...carried]
  if (digest.length > 0) {
    parts.push(`${DIGEST_OPEN}\nEarlier steps, recorded by the harness exactly as they happened (long results clipped).\n${digest}\n${DIGEST_CLOSE}`)
  }
  return { text: parts.filter((part) => part.length > 0).join('\n\n'), lines: lines.length, carried: carried.length }
}

/* -------------------------------------------------------------------------- */
/* Ledger, budget, checklist                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Turn folded messages into deterministic ledger lines. Only tool calls and
 * their results are recorded, so the ledger is exact and independent of the
 * model's recap.
 * @param {Array<object>} messages - shadowed conversation messages.
 * @param {{fullCommands?: boolean}} [options] - command rendering.
 * @returns {Array<string>} ledger lines.
 */
export function ledgerEntries(messages, options = {}) {
  const fullCommands = options.fullCommands !== false
  const entries = []
  if (!Array.isArray(messages)) return entries
  for (const message of messages) {
    if (message?.role === 'assistant' && Array.isArray(message.content)) {
      for (const block of message.content) {
        if (block?.type !== 'tool-call') continue
        const raw = typeof block.arguments === 'string'
          ? block.arguments
          : JSON.stringify(block.arguments ?? {})
        const args = fullCommands ? raw : `${raw.slice(0, 160)}${raw.length > 160 ? '…' : ''}`
        entries.push(`- ${block.name ?? block.toolName ?? 'tool'}(${args})`)
      }
    } else if (message?.role === 'tool') {
      const text = messageText(message).replace(/\s+/g, ' ').trim()
      if (text.length > 0) entries.push(`  → ${text.slice(0, 240)}`)
    }
  }
  return entries
}

/**
 * Render the cumulative ledger, keeping the newest entries when it overflows.
 * @param {Array<string>} lines - ledger lines.
 * @param {number} maxChars - character cap.
 * @returns {string} rendered ledger (empty when there are no lines).
 */
export function renderLedger(lines, maxChars) {
  if (!Array.isArray(lines) || lines.length === 0) return ''
  const text = lines.join('\n')
  if (!(maxChars > 0) || text.length <= maxChars) return text
  return `…[earlier ledger entries omitted]…\n${text.slice(-maxChars)}`
}

/**
 * Keep a model-written state inside its token budget, preserving the head.
 * @param {string} text - state text.
 * @param {number} budgetTokens - token budget.
 * @param {number} charsPerToken - estimator.
 * @returns {string} bounded state.
 */
export function boundState(text, budgetTokens, charsPerToken = 3.5) {
  if (typeof text !== 'string' || text.length === 0) return text
  const maxChars = Math.max(200, Math.floor(budgetTokens * charsPerToken))
  if (text.length <= maxChars) return text
  const head = Math.floor(maxChars * 0.7)
  const tail = Math.max(0, maxChars - head)
  return `${text.slice(0, head)}\n[state truncated to budget]\n${text.slice(-tail)}`
}

/**
 * Render the subgoal checklist with its checked state.
 * @param {Array<string>} items - checklist items.
 * @param {Set<number>} done - 1-based checked indices.
 * @returns {string} rendered checklist (empty when there is none).
 */
export function renderChecklist(items, done) {
  if (!Array.isArray(items) || items.length === 0) return ''
  return items
    .map((item, index) => `${done?.has?.(index + 1) === true ? '[x]' : '[ ]'} ${index + 1}. ${item}`)
    .join('\n')
}

/**
 * Text of the first user instruction on the surface, used to generate the
 * checklist. Harness notices that happen to be user-role messages (approval
 * policy changes, runtime context) are skipped.
 * @param {object} session - agent session.
 * @param {object} policy - resolved policy.
 * @returns {string} task text (empty when unavailable).
 */
function firstTaskText(session, policy) {
  const nodes = session?.surface?.nodes
  if (!Array.isArray(nodes)) return ''
  for (const seq of nodes) {
    const event = session.eventAt(seq)
    if (event?.type !== 'user/message') continue
    const source = event.data?.source
    if (source?.kind === CHECKPOINT_SOURCE_KIND) continue
    if (source?.kind !== undefined && !policy.instructionSourceKinds.includes(source.kind)) continue
    const text = messageText({ content: event.data?.content })
    if (text.trim().length === 0) continue
    return text
  }
  return ''
}

/* -------------------------------------------------------------------------- */
/* Earlier user requests (archive)                                            */
/* -------------------------------------------------------------------------- */

/**
 * Render the verbatim earlier-request list, oldest first for reading.
 * @param {Array<{text: string}>} requests - carried requests, newest first.
 * @returns {string} rendered section (empty when nothing is carried).
 */
export function renderRequests(requests) {
  if (!Array.isArray(requests) || requests.length === 0) return ''
  const ordered = [...requests].reverse()
  const body = ordered
    .map((item, index) => `<request ${index + 1}>\n${item.text}\n</request ${index + 1}>`)
    .join('\n')
  return [
    REQUESTS_OPEN,
    'Earlier requests from the user, verbatim, oldest first. Where they disagree a later request'
      + ' supersedes an earlier one. The request being worked on now is the user message after this checkpoint.',
    body,
    REQUESTS_CLOSE,
  ].join('\n')
}

/** Clip a quoted request to a character budget, keeping its head and tail. */
function clipRequest(text, max) {
  if (text.length <= max) return text
  const marker = '\n…[truncated]…\n'
  const head = Math.floor(Math.max(0, max - marker.length) * 0.7)
  const tail = Math.max(0, max - marker.length - head)
  return text.slice(0, head) + marker + (tail > 0 ? text.slice(-tail) : '')
}

/**
 * Add requests a fold is about to shadow to the carried list. The newest
 * request always survives (trimmed if longer than the whole budget); the
 * oldest are dropped once `instructionChars` is exhausted.
 * @param {object} state - per-agent SAC state.
 * @param {Array<{text: string, key: string|null}>} carry - requests inside the folded span, oldest first.
 * @param {object} policy - resolved policy.
 * @returns {void}
 */
export function registerRequests(state, carry, policy) {
  if (!(policy.instructionChars > 0) || !Array.isArray(carry) || carry.length === 0) return
  const cap = policy.instructionChars
  const seen = new Set()
  const kept = []
  let used = 0
  // A request carried before keeps its place: only genuinely new releases
  // go in front of the list, newest first.
  const carried = new Set(state.requests.map((item) => item.key))
  const fresh = [...carry].reverse().filter((item) => item.key === null || !carried.has(item.key))
  for (const item of [...fresh, ...state.requests]) {
    if (item.key !== null && seen.has(item.key)) continue
    if (item.key !== null) seen.add(item.key)
    const remaining = cap - used
    if (remaining <= 0) break
    let text = item.text
    if (text.length > remaining) {
      // The newest request is always kept, trimmed to what is left.
      if (kept.length === 0) text = clipRequest(text, remaining)
      else break
    }
    kept.push({ text, key: item.key })
    used += text.length
  }
  state.requests = kept
}

/**
 * Read the request list a checkpoint text carries, back into memory.
 * @param {string} text - checkpoint text.
 * @returns {Array<{text: string, key: string|null}>} requests, newest first.
 */
export function parseRequests(text) {
  if (typeof text !== 'string' || !text.includes(REQUESTS_OPEN)) return []
  const block = text.slice(text.indexOf(REQUESTS_OPEN))
  const items = []
  const pattern = /<request \d+>\n([\s\S]*?)\n<\/request \d+>/g
  for (const match of block.matchAll(pattern)) {
    if (match[1].trim().length > 0) items.push({ text: match[1], key: instructionKey({ text: match[1] }) })
  }
  return items.reverse()
}

/**
 * Rebuild the carried request list after a restart: from the checkpoints in
 * the folded span, else from the checkpoints still on the surface.
 * @param {ReadonlyArray<object>} messages - the folded span's messages.
 * @param {object} agent - agent whose surface may hold the archive.
 * @returns {Array<{text: string, key: string|null}>} requests, newest first.
 */
function recoverRequests(messages, agent) {
  const fromSpan = messages.map((message) => messageText(message)).filter((text) => text.includes(REQUESTS_OPEN))
  const source = fromSpan.length > 0 ? fromSpan : checkpointTexts(agent)
  // The newest checkpoint carries the fullest list.
  for (let index = source.length - 1; index >= 0; index -= 1) {
    const recovered = parseRequests(source[index])
    if (recovered.length > 0) return recovered
  }
  return []
}

/** Texts of the checkpoints on the agent's surface, oldest first. */
function checkpointTexts(agent) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes)) return []
  const texts = []
  for (const seq of nodes) {
    const event = agent.session.eventAt(seq)
    if (event?.type !== 'user/message' || event.data?.source?.kind !== CHECKPOINT_SOURCE_KIND) continue
    texts.push(messageText({ content: event.data?.content }))
  }
  return texts
}

/**
 * Whether a checkpoint outside the folded span already shows the carried
 * requests, so a working-state fold does not need to repeat them.
 * @param {object} agent - agent whose surface to scan.
 * @param {Set<unknown>} regionIds - ids of the user messages inside the folded span.
 * @returns {boolean} true when another surface checkpoint carries the list.
 */
function requestsElsewhere(agent, regionIds) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes)) return false
  for (const seq of nodes) {
    const event = agent.session.eventAt(seq)
    if (event?.type !== 'user/message' || event.data?.source?.kind !== CHECKPOINT_SOURCE_KIND) continue
    if (regionIds.has(event.data?.id)) continue
    if (messageText({ content: event.data?.content }).includes(REQUESTS_OPEN)) return true
  }
  return false
}

/**
 * The user instruction a surface node carries, or null when it is none.
 * @param {object} agent - agent whose surface to read.
 * @param {number} seq - surface node seq.
 * @param {object} policy - resolved policy.
 * @returns {{seq: number, text: string, key: string|null}|null} the instruction, or null.
 */
function instructionAt(agent, seq, policy) {
  const event = agent?.session?.eventAt?.(seq)
  if (event?.type !== 'user/message') return null
  const source = event.data?.source
  if (source?.kind === CHECKPOINT_SOURCE_KIND) return null
  if (source?.kind !== undefined && !policy.instructionSourceKinds.includes(source.kind)) return null
  const text = messageText({ content: event.data?.content })
  if (text.trim().length === 0) return null
  return { seq, text, key: instructionKey({ sourceKind: source?.kind, text }) }
}

/**
 * Newest user instruction at or before a surface position.
 * @param {object} agent - agent whose surface to scan.
 * @param {number} fromIndex - surface position to start from (inclusive).
 * @param {object} policy - resolved policy.
 * @returns {{seq: number, text: string, key: string|null}|null} the instruction, or null.
 */
function instructionBefore(agent, fromIndex, policy) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes)) return null
  for (let index = Math.min(fromIndex, nodes.length - 1); index >= 0; index -= 1) {
    const found = instructionAt(agent, nodes[index], policy)
    if (found !== null) return found
  }
  return null
}

/**
 * Surface position of the first node whose message is inside the folded span.
 * @param {object} agent - agent whose surface to scan.
 * @param {Set<unknown>} regionIds - ids of the user messages inside the folded span.
 * @returns {number} index, or -1 when the span is not on the surface.
 */
function regionStartIndex(agent, regionIds) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes) || regionIds.size === 0) return -1
  for (let index = 0; index < nodes.length; index += 1) {
    const data = agent.session.eventAt(nodes[index])?.data
    if (data?.id !== undefined && regionIds.has(data.id)) return index
  }
  return -1
}

/**
 * Work out what a fold is about: which segment it folds, which request the
 * work belongs to, and which user requests it shadows.
 *
 * The pre-step records its plan in `state.pendingFold`. A fold the backend
 * starts on its own (pressure recovery, `/compact`) has no plan: the span
 * belongs to the newest request inside it, or to the one in front of it, and
 * every request inside the span is carried verbatim so it is not lost.
 * @param {object} state - per-agent SAC state.
 * @param {object} input - the backend's `SummarizationInput`.
 * @param {object} agent - agent.
 * @param {object} policy - resolved policy.
 * @returns {{kind: string, anchorText: string, carry: Array<{text: string, key: string|null}>}} fold description.
 */
export function resolveFold(state, input, agent, policy) {
  const pending = state?.pendingFold
  if (pending !== undefined && typeof pending.kind === 'string') {
    return {
      kind: pending.kind,
      anchorText: pending.kind === 'archive' || typeof pending.anchor?.text !== 'string'
        ? ''
        : pending.anchor.text,
      carry: pending.kind === 'archive' && Array.isArray(pending.released)
        ? pending.released.map((item) => ({ text: item.text ?? '', key: item.key ?? null }))
        : [],
      mechanical: pending.mechanical === true,
      startSeq: pending.startSeq,
      endSeq: pending.endSeq,
      estimatedTokens: pending.estimatedTokens ?? 0,
      spanChars: pending.spanChars ?? 0,
    }
  }
  const shadowed = []
  const ids = new Set()
  for (const message of input?.messages ?? []) {
    if (message?.role !== 'user' || message.id === undefined) continue
    ids.add(message.id)
    const kind = message.source?.kind
    if (!policy.instructionSourceKinds.includes(kind)) continue
    const text = messageText(message)
    if (text.trim().length === 0) continue
    shadowed.push({ id: message.id, text, key: instructionKey({ sourceKind: kind, text }) })
  }
  const start = regionStartIndex(agent, ids)
  // The work in the span belongs to the newest request in it; a span without
  // one belongs to the request in front of it on the surface.
  const surfaceAnchor = start >= 0 ? instructionBefore(agent, start, policy) : null
  const anchor = shadowed[shadowed.length - 1] ?? surfaceAnchor
    ?? (shadowed.length === 0 ? instructionBefore(agent, Number.POSITIVE_INFINITY, policy) : null)
  // Every request inside the span leaves the surface with this fold and is
  // carried verbatim — unless the same request stays pinned outside the span
  // (a re-delivery) or was carried before.
  const seen = new Set(state?.requests?.map((item) => item.key) ?? [])
  const nodes = agent?.session?.surface?.nodes
  if (Array.isArray(nodes)) {
    for (const seq of nodes) {
      const data = agent.session.eventAt(seq)?.data
      if (data?.id === undefined || ids.has(data.id)) continue
      const pinned = instructionAt(agent, seq, policy)
      if (pinned?.key != null) seen.add(pinned.key)
    }
  }
  const carry = []
  for (let index = shadowed.length - 1; index >= 0; index -= 1) {
    const item = shadowed[index]
    if (item.key != null) {
      // Newest occurrence wins over an earlier re-delivery of the same request.
      if (seen.has(item.key)) continue
      seen.add(item.key)
    }
    carry.unshift({ text: item.text, key: item.key })
  }
  return { kind: 'active', anchorText: anchor?.text ?? '', carry, mechanical: false, startSeq: undefined, endSeq: undefined, estimatedTokens: 0, spanChars: 0 }
}

/* -------------------------------------------------------------------------- */
/* Recall over the archived transcript                                        */
/* -------------------------------------------------------------------------- */

/** Extract searchable text from one session event. */
function eventText(event) {
  if (event?.type === 'user/message') return messageText({ content: event.data?.content })
  if (event?.type === 'assistant/message') return messageText(event.data?.message)
  if (event?.type === 'tool/result') return messageText(event.data?.message)
  return ''
}

/**
 * Search the whole session log (including folded history) for a keyword query.
 * @param {object} session - agent session.
 * @param {string} query - keywords.
 * @param {number} [limit] - maximum snippets (1-8).
 * @returns {string} matching snippets with sequence numbers.
 */
export function searchSession(session, query, limit = 4) {
  const terms = String(query ?? '')
    .toLowerCase()
    .split(/\s+/)
    .filter((term) => term.length >= 2)
  if (session === undefined || session === null || terms.length === 0) return '(no archived match)'
  const total = typeof session.seq === 'number'
    ? session.seq
    : (Array.isArray(session.surface?.nodes) ? (session.surface.nodes.at(-1) ?? 0) : 0)
  const hits = []
  for (let seq = 1; seq <= total; seq += 1) {
    const event = session.eventAt(seq)
    if (event === undefined) continue
    const text = eventText(event)
    if (text.length === 0) continue
    const lower = text.toLowerCase()
    let score = 0
    let position = -1
    for (const term of terms) {
      const found = lower.indexOf(term)
      if (found >= 0) {
        score += 1
        if (position < 0 || found < position) position = found
      }
    }
    if (score === 0) continue
    const start = Math.max(0, position - 160)
    hits.push({
      seq,
      score,
      snippet: text.slice(start, start + 480).replace(/\s+/g, ' ').trim(),
    })
  }
  hits.sort((left, right) => right.score - left.score || right.seq - left.seq)
  const top = hits.slice(0, Math.max(1, Math.min(Number(limit) || 4, 8)))
  if (top.length === 0) return '(no archived match)'
  let output = ''
  for (const hit of top) {
    const line = `[seq ${hit.seq}] ${hit.snippet}\n`
    if (output.length + line.length > 4000) break
    output += line
  }
  return output.trim()
}

/* -------------------------------------------------------------------------- */
/* Tools (recall / check)                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Tool definition for archived-transcript recall. Plain definition object: the
 * registry accepts any `ToolDefinition`, so the plugin needs no import of the
 * shipped tools package (which is not resolvable from a linked package).
 * @returns {object} tool definition.
 */
export function recallTool() {
  return {
    name: 'recall',
    description: 'Search the full archived transcript of this session (including folded history) by keyword. Returns matching snippets with their sequence numbers.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Keywords to search for.' },
        limit: { type: 'number', description: 'Maximum snippets (1-8, default 4).' },
      },
      required: ['query'],
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      const agent = exec?.agent
      const state = agent === undefined ? undefined : stateByAgent.get(agent)
      if (state !== undefined) state.telemetry.recallCalls += 1
      return searchSession(agent?.session, args?.query, args?.limit)
    },
  }
}

/**
 * Tool definition for the subgoal checklist.
 * @returns {object} tool definition.
 */
export function checkTool() {
  return {
    name: 'check',
    description: 'Mark one checklist item as completed. Returns the updated checklist.',
    parameters: {
      type: 'object',
      properties: {
        index: { type: 'number', description: '1-based checklist index.' },
      },
      required: ['index'],
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      const agent = exec?.agent
      const state = agent === undefined ? undefined : stateByAgent.get(agent)
      if (state === undefined || !Array.isArray(state.checklist) || state.checklist.length === 0) {
        return '[error] no checklist is active in this session'
      }
      const index = Math.floor(Number(args?.index))
      if (!(index >= 1 && index <= state.checklist.length)) {
        return `[error] checklist index ${args?.index} is out of range (1-${state.checklist.length})`
      }
      state.done.add(index)
      state.telemetry.checkCalls += 1
      return `[checked] item ${index}: ${state.checklist[index - 1]}\n\n${renderChecklist(state.checklist, state.done)}`
    },
  }
}

/* -------------------------------------------------------------------------- */
/* State update                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Build a minimal text assembler for the LLM stream used by the paper prompt.
 * Tolerant by design: accepts `text-delta` chunks, `block-end` blocks and a
 * terminal `finish`/`usage` record, and never throws on unknown chunk types.
 * @param {AsyncIterable<object>} stream - `ctx.llm.stream()` output.
 * @returns {Promise<{text: string, usage: object|undefined, finish?: string}>} assembled text and,
 *   when the stream ended with a finish chunk, the reason's `kind` (`stop`, `max-tokens`, ...).
 */
export async function assembleText(stream) {
  let text = ''
  let usage
  let finish
  for await (const chunk of stream) {
    if (chunk === null || typeof chunk !== 'object') continue
    if (chunk.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text
    else if (chunk.type === 'block-end' && chunk.block?.type === 'text' && typeof chunk.block.text === 'string') {
      // `block-end` repeats the full block; prefer deltas when both arrive.
      if (text.length === 0) text += chunk.block.text
    } else if (chunk.type === 'usage' && chunk.usage !== undefined) usage = chunk.usage
    else if (chunk.type === 'finish') {
      if (chunk.usage !== undefined) usage = chunk.usage
      if (typeof chunk.reason?.kind === 'string') finish = chunk.reason.kind
    }
  }
  return { text, usage, ...(finish === undefined ? {} : { finish }) }
}

/**
 * Whether a recap looks cut off before it was complete: the model stopped at
 * the output cap, or the template's sections are missing at its end.
 * @param {string} text - the recap.
 * @param {string} kind - `paper` or `archive`.
 * @returns {boolean} true when the recap should be treated as incomplete.
 */
export function recapLooksTruncated(text, kind = 'paper') {
  if (typeof text !== 'string' || text.trim().length === 0) return true
  const sections = kind === 'archive' ? ARCHIVE_SECTIONS : PAPER_SECTIONS
  const lower = text.toLowerCase()
  let found = 0
  for (const section of sections) {
    if (new RegExp(`(^|\\n)\\s*(?:#+\\s*)?(?:\\*\\*)?\\d*\\s*${section}`, 'i').test(lower)) found += 1
  }
  return found < Math.ceil(sections.length * 0.75)
}

/**
 * Whether a long recap stops in the middle of a sentence (a host that reports
 * no finish reason leaves this as the only sign of a cut-off).
 * @param {string} text - the recap.
 * @returns {boolean} true for a long recap without a closing punctuation mark.
 */
export function recapStopsMidSentence(text) {
  if (typeof text !== 'string' || text.length < 400) return false
  return !/[.。!！?？)）\]」』*\"'`:]\s*$/.test(text.trimEnd())
}

/**
 * The largest recap (host tokens) a span may be replaced with under
 * `recapMaxRatio`: a share of the span, minus the backend's framing. 0 = no
 * such bound (the policy does not tie recaps to their span).
 * @param {object} policy - resolved policy.
 * @param {number} spanTokens - the span's size in the backend's own tokens.
 * @returns {number} recap budget in host tokens, or 0.
 */
export function recapBudget(policy, spanTokens) {
  if (!(policy.recapMaxRatio > 0) || !(spanTokens > 0)) return 0
  return Math.floor(spanTokens * policy.recapMaxRatio) - framingTokens()
}

/** Estimated tokens of the tool schemas (chars per token of the policy). */
function toolSchemaChars(session) {
  const tools = session?.requestHeader?.()?.tools
  return Array.isArray(tools) ? JSON.stringify(tools).length : 0
}

/** Whole prompt of the next request in estimated tokens: surface plus tool schemas. */
function requestTokensOf(session, nodes, policy) {
  const cpt = policy.charsPerToken ?? 3.5
  const chars = nodes.reduce((sum, node) => sum + (node.textChars ?? 0), 0) + toolSchemaChars(session)
  return Math.ceil(chars / cpt)
}

/**
 * Whether a user instruction is waiting in the agent's inbox for the next step.
 * The pre-step runs before that message reaches the surface, so from the
 * planner's point of view the current turn is already over.
 * @param {object} agent - agent.
 * @param {object} policy - resolved policy.
 * @returns {boolean} true when the next step starts with a user instruction.
 */
export function instructionPending(agent, policy = SAC_DEFAULTS) {
  const pending = agent?.inbox?.nextStep
  if (!Array.isArray(pending)) return false
  return pending.some((message) => {
    const kind = message?.source?.kind
    if (kind === CHECKPOINT_SOURCE_KIND) return false
    return kind === undefined || policy.instructionSourceKinds.includes(kind)
  })
}

/** The fixed part of the request in estimated tokens: system prompt plus tool schemas. */
function fixedTokensOf(session, nodes, policy) {
  const cpt = policy.charsPerToken ?? 3.5
  const system = nodes.find((node) => node.type === 'system/message')
  return Math.ceil(((system?.textChars ?? 0) + toolSchemaChars(session)) / cpt)
}

/**
 * Compose the final state text: model recap, bounded by the adaptive budget,
 * with the machine sections in front.
 * @param {object} state - per-agent SAC state.
 * @param {object} policy - resolved policy.
 * @param {string} recap - model-written (or extracted) recap.
 * @param {{machine?: boolean, requests?: boolean, maxRecapTokens?: number}} [options] - `machine`
 *   renders the checklist and ledger (working state only); `requests` renders
 *   the verbatim earlier-request list (archive, or when no other checkpoint
 *   on the surface carries it); `maxRecapTokens` (host tokens) caps the recap
 *   below the budget, so the checkpoint stays under a share of its span.
 * @returns {string} state text written into the checkpoint.
 */
function composeState(state, policy, recap, { machine = true, requests = false, maxRecapTokens = 0 } = {}) {
  const budget = stateBudgetFor(policy, state?.telemetry?.foldedActions ?? 0)
  const cpt = policy.charsPerToken ?? 3.5
  const parts = []
  if (machine && policy.checklist) {
    const checklist = renderChecklist(state?.checklist, state?.done)
    if (checklist.length > 0) parts.push(`${CHECKLIST_OPEN}\n${checklist}\n${CHECKLIST_CLOSE}`)
  }
  if (machine && policy.ledger) {
    const ledger = renderLedger(state?.ledger, policy.ledgerChars)
    if (ledger.length > 0) parts.push(`${LEDGER_OPEN}\n${ledger}\n${LEDGER_CLOSE}`)
  }
  if (requests) {
    const list = renderRequests(state?.requests)
    if (list.length > 0) parts.push(list)
  }
  // Machine sections sit outside the recap's own budget.
  const machineTokens = parts.reduce((sum, part) => sum + hostTextTokens(part), 0)
  const cap = maxRecapTokens > 0 ? maxRecapTokens - machineTokens : 0
  let maxChars
  if (cap > 0) {
    // A span-relative bound replaces the early-fold budget: it already keeps the
    // recap under a share of what it replaces, and the early-fold budget cut a
    // quarter of 1.2.0's recaps head and tail (640 tokens whatever the span).
    const absolute = policy.stateBudget ?? SAC_DEFAULTS.stateBudget
    maxChars = Math.min(Math.max(200, Math.floor(absolute * cpt)), cap * HOST_CHARS_PER_TOKEN)
  } else {
    maxChars = Math.max(200, Math.floor(budget * cpt))
  }
  let body = stripMachineSections(recap)
  // The model sometimes wraps a fallback recap in the same tags; keep one level.
  body = body.replace(/<\/?state>/gi, '').trim()
  if (body.length > maxChars) {
    const head = Math.floor(maxChars * 0.7)
    const tail = Math.max(0, maxChars - head)
    body = `${body.slice(0, head)}\n[recap cut to ${cap > 0 ? `${Math.round(cap)} tokens` : 'its budget'}]\n${body.slice(-tail)}`
  }
  parts.push(body)
  return parts.filter((part) => part.length > 0).join('\n\n')
}

/**
 * Split the backend's replay input into the leading system/developer messages
 * and the folded span itself.
 * @param {ReadonlyArray<object>|undefined} messages - `input.messages`.
 * @param {boolean} lean - whether the system prompt is dropped.
 * @returns {{leading: object[], region: object[]}} prefix and span messages.
 */
function splitRegion(messages, lean) {
  const all = Array.isArray(messages) ? messages : []
  if (lean) return { leading: [], region: all.filter((message) => message?.role !== 'system' && message?.role !== 'developer') }
  let split = all.length
  for (let index = 0; index < all.length; index += 1) {
    if (all[index]?.role === 'system' || all[index]?.role === 'developer') continue
    split = index
    break
  }
  return { leading: all.slice(0, split), region: all.slice(split) }
}

/**
 * Create the `summarize` implementation: inline extraction first, then the
 * paper template, then ledger/checklist/budget composition.
 * @param {object} ctx - plugin context.
 * @param {object} policy - resolved SAC policy.
 * @returns {(input: object, agent: object, signal?: AbortSignal) => Promise<object>} summarize hook.
 */
function stateSummarize(ctx, policy) {
  return async function summarize(input, agent, signal) {
    let state = stateByAgent.get(agent)
    if (state === undefined) {
      state = createState()
      stateByAgent.set(agent, state)
    }
    if (policy.ledger && state !== undefined) {
      const entries = ledgerEntries(input?.messages, { fullCommands: policy.ledgerFullCommands })
      if (entries.length > 0) {
        const merged = [...state.ledger, ...entries]
        const seen = new Set()
        state.ledger = merged.filter((line) => {
          if (seen.has(line)) return false
          seen.add(line)
          return true
        }).slice(-400)
        state.telemetry.ledgerLines = state.ledger.length
      }
    }

    // What this fold is about, and which user requests it must keep verbatim.
    // The carried list lives in memory; a session resumed after a restart
    // still has it on the surface, inside the archive checkpoint.
    if (state.requests.length === 0) {
      state.requests = recoverRequests(input?.messages ?? [], agent)
      if (state.requests.length > 0) state.telemetry.carriedRequests = state.requests.length
    }
    const fold = resolveFold(state, input, agent, policy)
    const regionIds = new Set((input?.messages ?? [])
      .filter((message) => message?.role === 'user' && message?.id !== undefined)
      .map((message) => message.id))
    /** The span's size in the backend's own tokens (0 when it cannot be measured). */
    const spanTokens = spanRouteTokens(ctx, agent, fold)
    /** Compose the checkpoint; the request list is shown unless another checkpoint has it. */
    const compose = (text, machine = true, maxRecapTokens = 0) => composeState(state, policy, text, {
      machine,
      requests: fold.kind === 'archive' || !requestsElsewhere(agent, regionIds),
      maxRecapTokens,
    })

    // The harness folds a small span itself: no model call, nothing paraphrased.
    if (fold.mechanical === true) {
      // The planner shrank every result's share so the digest fits its budget:
      // write it with that same clip.
      const digestPolicy = fold.digestClip > 0 && fold.digestClip < (policy.digestResultChars ?? SAC_DEFAULTS.digestResultChars)
        ? { ...policy, digestResultChars: fold.digestClip }
        : policy
      const digest = mechanicalDigest(trimOversized(input?.messages ?? [], 0), digestPolicy)
      const framed = `${CHECKPOINT_PREAMBLE}\n\n${SUMMARY_OPEN_TAG}\n${digest.text}\n${SUMMARY_CLOSE_TAG}`
      const size = checkpointTokens(ctx, framed)
      if (spanTokens > 0 && size >= Math.floor(spanTokens * 0.9)) {
        if (state !== undefined) state.telemetry.mechanicalRefusals += 1
        throw new Error(`dsh-sac-compact: mechanical digest would not shrink the span (${size} >= 90% of ${spanTokens} tokens)`)
      }
      const target = agent.session.requestHeader()?.config
      return {
        summary: [{ type: 'text', text: digest.text }],
        rawOutput: [{ type: 'text', text: digest.text }],
        llmStreamCall: false,
        mechanical: true,
        ...(target?.provider === undefined ? {} : { provider: target.provider }),
        ...(target?.model === undefined ? {} : { model: target.model }),
      }
    }

    // The model's working `<state>` block describes the current turn only, so an
    // archive fold always uses the summarizer, and a finished turn reads the
    // block its own last tool call wrote, not the one the new turn is writing.
    if (policy.statePrompt === 'inline' && state?.inlineBroken !== true && fold.kind !== 'archive') {
      const inline = fold.kind === 'frozen'
        ? extractInlineState(input?.messages)
        : (latestSurfaceState(agent) ?? extractInlineState(input?.messages))
      if (inline !== null) {
        if (state !== undefined) state.telemetry.inlineHits += 1
        registerRequests(state, fold.carry, policy)
        const header = agent.session.requestHeader()
        const target = header?.config
        return {
          summary: [{ type: 'text', text: compose(inline, fold.kind === 'active') }],
          rawOutput: [{ type: 'text', text: inline }],
          llmStreamCall: false,
          inline: true,
          ...(target?.provider === undefined ? {} : { provider: target.provider }),
          ...(target?.model === undefined ? {} : { model: target.model }),
        }
      }
      if (state !== undefined) {
        state.telemetry.inlineMisses += 1
        if (state.telemetry.inlineMisses >= (policy.inlineFallbackAfter ?? 2)) {
          state.inlineBroken = true
          state.telemetry.inlineBroken = true
          ctx.logger?.warn?.(
            `dsh-sac-compact: inline state block unavailable after `
            + `${state.telemetry.inlineMisses} fold(s); switching to the paper summarizer for this session`,
          )
        }
      }
    }

    // The state update runs on the agent's own route unless the policy routes
    // it elsewhere (a cheaper or non-reasoning model writes the recap).
    const header = agent.session.requestHeader()
    const own = header?.config
    const provider = policy.stateProvider || own?.provider
    const model = policy.stateModel || own?.model
    if (!provider || !model) {
      throw new Error('dsh-sac-compact: no routed provider/model available for the state update')
    }
    const llm = serviceOf(ctx, 'llm')
    if (llm === undefined || typeof llm.stream !== 'function') {
      throw new Error('dsh-sac-compact: no llm service available for the state update')
    }
    const rerouted = policy.stateProvider !== '' || policy.stateModel !== ''
    if (rerouted && state !== undefined) state.telemetry.stateRerouted += 1
    // The recap must stay under a share of its span: the word limit handed to
    // the model and a hard bound on what it wrote both follow that budget, so
    // the backend never refuses the fold as "not smaller".
    const carriedTokens = fold.kind === 'archive'
      ? Math.ceil(state.requests.reduce((sum, item) => sum + item.text.length, 0) / HOST_CHARS_PER_TOKEN)
      : 0
    const budget = spanTokens > 0 ? recapBudget(policy, Math.max(0, spanTokens - carriedTokens)) : 0
    if (policy.recapMaxRatio > 0 && spanTokens > 0 && budget < MIN_RECAP_TOKENS) {
      throw new Error(`dsh-sac-compact: span of ${spanTokens} tokens is too small for a bounded recap; leaving it on the surface`)
    }
    const wordLimit = (attempt) => recapWordLimit(policy, spanTokens, budget, attempt)
    if (state !== undefined && !(state.stateCap > 0)) {
      // A thinking model spends much of a small cap before the recap: when the
      // agent's own messages carry reasoning, start from the larger cap instead
      // of paying a cut-off and a retry on the first fold of every session.
      const thinks = policy.stateMaxTokensThinking > 0 && surfaceHasReasoning(agent)
      state.stateCap = thinks
        ? Math.min(policy.stateMaxTokensCeiling, policy.stateMaxTokensThinking)
        : Math.max(512, Math.min(4096, Math.floor(policy.stateMaxTokens ?? 2048)))
    }
    const baseMax = state?.stateCap ?? 2048
    const ceiling = policy.stateMaxTokensCeiling ?? 8192
    const lean = policy.stateInputLean === true
    const { leading, region } = splitRegion(input?.messages, lean)
    // The request the folded work belongs to, quoted verbatim in front of the
    // span: without it the state update has to guess the objective from the
    // last actions, and writes their sub-task into the recap instead.
    const anchorMessage = fold.anchorText.trim().length > 0
      ? {
        role: 'user',
        content: [{
          type: 'text',
          text: fold.anchorText.length > ANCHOR_MAX_CHARS
            ? trimText(fold.anchorText, ANCHOR_MAX_CHARS)
            : fold.anchorText,
        }],
      }
      : null
    const span = trimOversized(trimForSummary(omitStandingNotices(region, policy), policy.stateInputMaxChars ?? 0), policy.trimObservationOver ?? 0)
    // A reasoning model can spend the whole output budget on hidden reasoning
    // and return no text, or be cut off halfway through the recap. Both are
    // retried once with a direct instruction, a tighter word limit, and a cap
    // that doubles — and stays doubled for the rest of the session.
    let text = ''
    let usage
    let retried = false
    let truncated = false
    let cutOffBefore = false
    let maxTokens = baseMax
    const attemptsLog = []
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const limits = { maxWords: wordLimit(attempt), attempt, cutOff: attempt > 0 && cutOffBefore }
      const carryFacts = policy.recapMaxRatio > 0
      const instruction = fold.kind === 'archive'
        ? archiveStatePrompt({ ...limits, carryFacts })
        : paperStatePrompt({ ...limits, anchored: anchorMessage !== null, carryFacts })
      const messages = [
        ...leading,
        ...(anchorMessage === null ? [] : [anchorMessage]),
        ...span,
        { role: 'user', content: [{ type: 'text', text: instruction }] },
      ]
      maxTokens = attempt === 0 ? baseMax : Math.min(ceiling, Math.max(2048, baseMax * 2))
      const options = {
        provider,
        model,
        messages,
        ...(lean ? {} : { toolHistory: agent.session.toolHistory() }),
        ...(lean || input.tools === undefined ? {} : { tools: [...input.tools] }),
        maxTokens,
        sessionId: agent.session.id,
        purpose: 'compaction',
        ...(signal === undefined ? {} : { signal }),
      }
      const assembled = await assembleText(llm.stream(options))
      text = assembled.text.trim()
      if (assembled.usage !== undefined) usage = assembled.usage
      attemptsLog.push(`attempt${attempt}: finish=${assembled.finish ?? 'none'} out=${assembled.usage?.outputTokens ?? '?'} chars=${text.length}`)
      // A cut-off is certain when the stream ended at the output cap. A host
      // that reports no finish reason is judged by the recap's last line only:
      // a long recap that stops mid-sentence was cut, whatever its sections.
      const cutOff = assembled.finish === 'max-tokens'
        || (assembled.finish === undefined && recapStopsMidSentence(text))
      if (text.length === 0 || cutOff) {
        truncated = true
        cutOffBefore = cutOff
        if (state !== undefined) state.stateCap = maxTokens
        if (attempt === 0 && text.length > 0) {
          // Keep the better of two incomplete attempts.
          state.lastPartialRecap = text
        }
        continue
      }
      truncated = false
      retried = attempt > 0
      break
    }
    if (text.length === 0) {
      text = typeof state?.lastPartialRecap === 'string' ? state.lastPartialRecap : ''
    }
    if (state !== undefined) state.lastPartialRecap = undefined
    if (text.length === 0) throw new Error('dsh-sac-compact: state update produced no text (2 attempts)')
    if (truncated) {
      // Both attempts were cut off: keep the complete lines of the longer attempt.
      text = text.replace(/\n[^\n]*$/, (line) => (/[.。!！?？):："'`*]\s*$/.test(line) ? line : ''))
      if (state !== undefined) state.telemetry.truncatedRecaps += 1
    }
    if (retried && state !== undefined) state.telemetry.stateRetries += 1
    // Registered only after the recap exists, so a failed fold retries the same requests.
    registerRequests(state, fold.carry, policy)
    state.telemetry.carriedRequests = state.requests.length
    const summaryText = compose(text, fold.kind === 'active', budget)
    if (state !== undefined) {
      const chars = summaryText.length
      state.recapEmaChars = state.recapEmaChars > 0 ? Math.round(0.5 * state.recapEmaChars + 0.5 * chars) : chars
    }
    return {
      summary: [{ type: 'text', text: summaryText }],
      rawOutput: [{ type: 'text', text }, { type: 'text', text: `\n[sac-debug ${attemptsLog.join(' | ')}]` }],
      llmStreamCall: true,
      provider,
      model,
      maxTokens,
      retried,
      ...(truncated ? { truncated: true } : {}),
      ...(usage === undefined ? {} : { usage }),
    }
  }
}

/**
 * Whether any assistant message on the surface carries a reasoning block, i.e.
 * whether the agent's model thinks in the open.
 * @param {object} agent - agent.
 * @returns {boolean} true when a reasoning block with text is on the surface.
 */
export function surfaceHasReasoning(agent) {
  const nodes = agent?.session?.surface?.nodes
  if (!Array.isArray(nodes)) return false
  for (const seq of nodes) {
    const event = agent.session.eventAt(seq)
    if (event?.type !== 'assistant/message') continue
    const blocks = event.data?.message?.content
    if (Array.isArray(blocks) && blocks.some((block) => block?.type === 'reasoning' && typeof block.text === 'string' && block.text.length > 0)) return true
  }
  return false
}

/**
 * The span's size in the backend's own tokens, read from the same token meter
 * its shrink check uses. 0 when no meter is mounted or the span is unknown.
 * @param {object} ctx - plugin context.
 * @param {object} agent - agent.
 * @param {{startSeq?: number, endSeq?: number, estimatedTokens?: number}} fold - the planned fold.
 * @returns {number} route-priced tokens of the span.
 */
function spanRouteTokens(ctx, agent, fold) {
  if (typeof fold?.startSeq !== 'number' || typeof fold?.endSeq !== 'number') return 0
  const meter = serviceOf(ctx, 'tokenMeter')
  if (meter === undefined || typeof meter.measure !== 'function') {
    // Without a meter, estimate conservatively from the planner's character count.
    return Math.floor((fold.estimatedTokens ?? 0) * 0.8)
  }
  try {
    const { nodes } = meter.measure(agent.session)
    const first = nodes.findIndex((node) => node.seq === fold.startSeq)
    const last = nodes.findIndex((node) => node.seq === fold.endSeq)
    if (first < 0 || last < first) return 0
    let total = 0
    for (let index = first; index <= last; index += 1) total += nodes[index]?.tokens ?? 0
    return total
  } catch {
    return 0
  }
}

/**
 * Price a checkpoint text the way the backend prices the framed summary.
 * @param {object} ctx - plugin context.
 * @param {string} framed - preamble, tags and state text.
 * @returns {number} estimated host tokens.
 */
function checkpointTokens(ctx, framed) {
  const meter = serviceOf(ctx, 'tokenMeter')
  if (meter !== undefined && typeof meter.estimateMessage === 'function') {
    try {
      return meter.estimateMessage({ role: 'user', content: [{ type: 'text', text: framed }] })
    } catch {
      // fall through to the fixed heuristic
    }
  }
  return Math.ceil(framed.length / HOST_CHARS_PER_TOKEN) + HOST_BLOCK_OVERHEAD + HOST_ROLE_OVERHEAD
}

/**
 * Generate the v9 subgoal checklist once per agent.
 * @param {object} ctx - plugin context.
 * @param {object} agent - agent.
 * @param {object} state - per-agent SAC state.
 * @param {AbortSignal|undefined} signal - cancellation.
 * @param {object} policy - resolved policy (which user messages are the task).
 * @returns {Promise<void>} resolves after the attempt (success or not).
 */
async function ensureChecklist(ctx, agent, state, signal, policy) {
  if (state.checklistRequested === true) return
  // The first pre-step runs before any request is routed, so there is no
  // provider/model yet. Wait for the first fold (after the first action), when
  // the request header exists, instead of giving up permanently.
  const target = agent.session.requestHeader()?.config
  const llm = serviceOf(ctx, 'llm')
  if (target?.provider === undefined || target?.model === undefined || llm?.stream === undefined) return
  const task = firstTaskText(agent.session, policy)
  if (task.length === 0) return
  state.checklistRequested = true
  state.checklist = []
  try {
    const baseMessages = [{
      role: 'user',
      content: [{ type: 'text', text: `${CHECKLIST_PLAN}\n\nTASK:\n${task.slice(0, 4000)}` }],
    }]
    const timeout = typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(30_000) : undefined
    const bounded = signal !== undefined && timeout !== undefined && typeof AbortSignal.any === 'function'
      ? AbortSignal.any([signal, timeout])
      : (signal ?? timeout)
    let assembled = { text: '' }
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const options = {
        provider: target.provider,
        model: target.model,
        messages: attempt === 0
          ? baseMessages
          : [...baseMessages, {
            role: 'user',
            content: [{ type: 'text', text: 'Output the checklist now, one item per line, with no reasoning.' }],
          }],
        maxTokens: attempt === 0 ? 800 : 1600,
        sessionId: agent.session.id,
        purpose: 'compaction',
        ...(bounded === undefined ? {} : { signal: bounded }),
      }
      assembled = await assembleText(llm.stream(options))
      if (assembled.text.trim().length > 0) break
    }
    const items = assembled.text
      .split('\n')
      .map((line) => line.replace(/^\s*(?:[-*]|\d+[.)])\s*/, '').trim())
      .filter((line) => line.length > 0)
      .slice(0, 8)
    if (items.length > 0) {
      state.checklist = items
      state.telemetry.checklistItems = items.length
    }
  } catch (error) {
    ctx.logger?.warn?.(`dsh-sac-compact: checklist generation failed (${String(error?.message ?? error)})`)
  }
}

/* -------------------------------------------------------------------------- */
/* Plugin entry                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Plugin entry.
 * @param {object} ctx - Cordis context.
 * @param {object} [config] - plugin configuration.
 */
export function apply(ctx, config = {}) {
  const policy = resolvePolicy(config)
  if (!policy.enabled) {
    ctx.logger?.info?.('dsh-sac-compact: disabled by configuration')
    return
  }

  const wantsStateUpdate = policy.statePrompt !== 'native'
  if (policy.statePrompt === 'inline' && policy.keepLastObservation !== true) {
    // The inline model writes its state before the action; the action's own
    // observation must therefore stay raw until the next reply folds it in.
    policy.keepLastObservation = true
    ctx.logger?.warn?.(
      'dsh-sac-compact: statePrompt=inline keeps the newest observation raw; '
      + 'enabling keepLastObservation',
    )
  }

  /**
   * Install the state hook on the mounted backend, at most once per module
   * revision. Called eagerly (load time) and again on every pre-step: the
   * isolated compaction group often mounts after `apply()`, and the eager
   * attempt then finds no service. Retrying on the first pre-step closes that
   * gap; the revision marker lets a hot-reloaded module replace an older hook.
   * @param {'apply'|'pre-step'} phase - where the attempt comes from.
   * @returns {boolean} whether this revision's hook is installed.
   */
  function installStateUpdate(phase) {
    const engine = ctx.get('compaction')
    if (engine === undefined || typeof engine.summarize !== 'function') return false
    if (stateUpdateInstalled(engine)) return true
    const base = stateSummarize(ctx, policy)
    engine.summarize = async function summarize(input, agent, signal) {
      const telemetry = stateByAgent.get(agent)?.telemetry
      try {
        const result = await base.call(this, input, agent, signal)
        if (telemetry !== undefined) {
          telemetry.summarizeCalls += 1
          telemetry.summarizeChars += summaryChars(result)
          if (result?.retried === true) telemetry.summarizeRetries += 1
        }
        return result
      } catch (error) {
        if (telemetry !== undefined) telemetry.summarizeErrors += 1
        throw error
      }
    }
    Object.defineProperty(engine, INSTALLED, {
      value: { rev: INSTALL_REV },
      configurable: true,
    })
    ctx.logger?.info?.(`dsh-sac-compact: state update installed (${phase}, ${policy.statePrompt})`)
    return true
  }

  let promptInstalled = false
  let toolsInstalled = false
  let promptWarned = false
  let toolsWarned = false

  /** Register the inline-state system prompt section (v9 pipeline). */
  function installPromptSection() {
    if (promptInstalled || policy.statePrompt !== 'inline') return
    const prompt = serviceOf(ctx, 'systemPrompt')
    if (prompt === undefined || typeof prompt.section !== 'function') {
      if (!promptWarned) {
        promptWarned = true
        ctx.logger?.warn?.(
          'dsh-sac-compact: statePrompt=inline needs ctx.systemPrompt.section(); '
          + 'the session will fall back to the paper summarizer',
        )
      }
      return
    }
    const install = () => prompt.section({
      name: 'sac-compact:inline-state',
      order: 9500,
      text: INLINE_SYSTEM_SECTION,
    })
    if (typeof ctx.effect === 'function') ctx.effect(install)
    else install()
    promptInstalled = true
  }

  /** Register the recall/check tools (v9 pipeline). */
  function installTools() {
    if (toolsInstalled) return
    const tools = serviceOf(ctx, 'tools')
    if (tools === undefined || typeof tools.register !== 'function') {
      if ((policy.recall || policy.checklist) && !toolsWarned) {
        toolsWarned = true
        ctx.logger?.warn?.('dsh-sac-compact: recall/checklist requested but ctx.tools is unavailable')
      }
      return
    }
    const definitions = []
    if (policy.recall) definitions.push(recallTool())
    if (policy.checklist) definitions.push(checkTool())
    if (definitions.length === 0) return
    const install = () => {
      const disposers = definitions.map((definition) => tools.register(definition))
      return () => {
        for (const dispose of disposers) {
          try {
            dispose?.()
          } catch {
            // disposal is best-effort
          }
        }
      }
    }
    if (typeof ctx.effect === 'function') ctx.effect(install)
    else install()
    toolsInstalled = true
  }

  installPromptSection()
  installTools()

  // Best effort at load time; a miss is not an error yet, because the group may
  // simply not be mounted. The first pre-step retries and reports a real miss.
  const installedAtLoad = wantsStateUpdate ? installStateUpdate('apply') : false

  ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    if (signal?.aborted) return next()
    const engine = ctx.get('compaction')
    if (engine === undefined || typeof engine.compactRegion !== 'function') return next()

    let state = stateByAgent.get(agent)
    if (state === undefined) {
      state = createState()
      stateByAgent.set(agent, state)
    }
    state.telemetry.steps += 1

    // The preset composes these services around the agent; retry once they exist.
    installPromptSection()
    installTools()

    if (policy.checklist) await ensureChecklist(ctx, agent, state, signal, policy)

    // A1: the state template must never silently stay uninstalled.
    if (wantsStateUpdate) {
      state.telemetry.installAttempts += 1
      if (installStateUpdate('pre-step')) {
        if (state.telemetry.installedOn === null) {
          state.telemetry.installedOn = installedAtLoad ? 'apply' : 'pre-step'
        }
      } else {
        state.telemetry.installFailures += 1
        const signature = 'state update not installed'
        if (!state.warned.has(signature)) {
          state.warned.add(signature)
          ctx.logger?.error?.(
            `dsh-sac-compact: statePrompt=${policy.statePrompt} was requested but the compaction backend `
            + 'exposes no summarize() hook; the session keeps the backend default template',
          )
        }
      }
    }

    const turn = agent.session?.surface?.replaceGeneration ?? 0
    if (state.lastTurn !== turn) {
      state.lastTurn = turn
      state.compactions = 0
    }

    try {
      const carriedChars = () => state.requests.reduce((sum, item) => sum + item.text.length, 0)
      const observedRecap = () => (state.recapEmaChars > 0 ? Math.ceil(state.recapEmaChars / policy.charsPerToken) : 0)
      // Legacy: at most one fold per step. The size trigger loops, because one
      // span rarely brings an oversized request back under the low-water mark.
      const sizeMode = policy.foldTrigger === 'size'
      const maxFolds = sizeMode ? policy.maxFoldsPerStep : 1
      let folds = 0
      for (;;) {
        if (!withinTurnBudget(state.compactions, policy)) break
        const nodes = describeSurface(agent.session, policy)
        const marks = effectiveMarks(policy, fixedTokensOf(agent.session, nodes, policy))
        const options = {
          carriedChars: carriedChars(),
          requestTokens: requestTokensOf(agent.session, nodes, policy),
          foldAboveTokens: marks.above,
          expectedRecapTokens: observedRecap(),
          // Only a digest may fold at a turn boundary; without mechanical folds the
          // legacy timing (fold after the first action of the next turn) stays.
          turnEnding: policy.mechanicalFoldMaxTokens > 0 && instructionPending(agent, policy),
        }
        let span = planSacSpan(nodes, policy, options)
        let forced = false
        if (span === null && (policy.contextBudgetTokens ?? 0) > 0) {
          const surfaceTokens = surfaceTokensOf(nodes, policy)
          if (surfaceTokens >= policy.contextBudgetTokens) {
            span = planSacSpan(nodes, policy, { ...options, relaxed: true })
            if (span !== null) {
              forced = true
              state.telemetry.budgetForcedFolds += 1
              ctx.logger?.info?.(
                `dsh-sac-compact: context ${surfaceTokens} >= budget ${policy.contextBudgetTokens}; forcing a fold`,
              )
            }
          }
        }
        if (span === null) break

        const start = nodes[span.startIndex].seq
        const end = nodes[span.endIndex].seq
        // Nothing changes inside a finished turn or the archive between two
        // steps, so a span the backend refused as "not smaller" would cost one
        // summarizer call per step. Give each such span two attempts.
        const spanKey = `${start}:${end}`
        if (span.kind !== 'active' && (state.refusedSpans.get(spanKey) ?? 0) >= 2) break

        // Tell the state update which request this fold belongs to and which
        // requests it shadows; the backend calls `summarize()` inside compactRegion.
        const fold = describeFold(nodes, span)
        state.pendingFold = fold
        try {
          await engine.compactRegion(start, end, agent, signal)
        } catch (error) {
          if (span.kind !== 'active' && /not smaller|would not shrink/i.test(String(error?.message ?? error))) {
            state.refusedSpans.set(spanKey, (state.refusedSpans.get(spanKey) ?? 0) + 1)
            state.telemetry.refusedFolds += 1
          }
          throw error
        } finally {
          state.pendingFold = undefined
        }
        folds += 1
        state.compactions += 1
        state.telemetry.compactions += 1
        if (span.mechanical === true) state.telemetry.mechanicalFolds += 1
        if (span.replyFolded === true) state.telemetry.replyFolds += 1
        if (span.kind === 'frozen') state.telemetry.frozenFolds += 1
        if (span.kind === 'archive') state.telemetry.archiveFolds += 1
        if (sizeMode && forced) state.telemetry.sizeForcedFolds += 1
        state.telemetry.foldedTokens += span.estimatedTokens
        state.telemetry.foldedActions += span.actions
        state.telemetry.lastFoldActions = span.actions
        const t = state.telemetry
        ctx.logger?.info?.(
          `dsh-sac-compact: folded ${span.actions} action(s) `
          + `(seqs ${start}-${end}, ~${span.estimatedTokens} tokens) into semantic state `
          + `| folds=${t.compactions} folded_tokens=${t.foldedTokens} `
          + `summaries=${t.summarizeCalls} inline=${t.inlineHits}/${t.inlineHits + t.inlineMisses} `
          + `ledger_lines=${t.ledgerLines} checklist=${t.checklistItems} `
          + `segment=${span.kind} mode=${span.mechanical === true ? 'mechanical' : 'llm'} `
          + `reply=${span.replyFolded === true ? 'folded' : 'kept'} `
          + `request=${fold.anchor?.seq ?? 'none'} released=${fold.released.length} `
          + `frozen=${t.frozenFolds} archive=${t.archiveFolds} mechanical=${t.mechanicalFolds} `
          + `carried_requests=${t.carriedRequests} request_tokens≈${options.requestTokens} `
          + `installed=${t.installedOn ?? 'n/a'}`,
        )
        if (folds >= maxFolds || forced || !sizeMode) break
        // The size trigger is satisfied once the next request fits the low-water mark.
        const after = describeSurface(agent.session, policy)
        if (requestTokensOf(agent.session, after, policy) <= marks.below) break
      }
      nudgeInline(ctx, agent, state, policy)
      return next()
    } catch (error) {
      // SAC must never break a turn: log once per failure signature and go on.
      const signature = String(error?.message ?? error)
      state.telemetry.skipped += 1
      state.telemetry.lastSkipReason = signature
      if (!state.warned.has(signature)) {
        state.warned.add(signature)
        ctx.logger?.warn?.(`dsh-sac-compact: skipped (${signature})`)
      }
      nudgeInline(ctx, agent, state, policy)
      return next()
    }
  })
}

/**
 * Estimate the character weight of one surface node for the span-size guard.
 * Reasoning text is reported separately: providers replay it in later requests
 * (it counts towards the surface) but a harness-written digest never keeps it.
 * @param {object} event - session event.
 * @returns {{chars: number, toolCalls: number, reasoningChars: number, argsChars: number, plainChars: number}} weights.
 */
function weighEvent(event) {
  const blocks = event?.type === 'user/message'
    ? event.data?.content
    : event.data?.message?.content
  let chars = 0
  let toolCalls = 0
  let reasoningChars = 0
  let argsChars = 0
  let plainChars = 0
  if (Array.isArray(blocks)) {
    for (const block of blocks) {
      if (block?.type === 'reasoning' && typeof block.text === 'string') {
        reasoningChars += block.text.length
        chars += block.text.length
      } else if (block?.type === 'tool-call') {
        const length = JSON.stringify(block.arguments ?? block.input ?? {}).length
        toolCalls += 1
        argsChars += length
        chars += length
      } else if (typeof block?.text === 'string') {
        plainChars += block.text.length
        chars += block.text.length
      } else if (block?.type === 'image' || block?.type === 'image-url') chars += 1600
      else if (block !== null && typeof block === 'object') chars += 64
    }
  }
  return { chars, toolCalls, reasoningChars, argsChars, plainChars }
}

/**
 * Adapt a live session surface into the pure descriptor list.
 * @param {object} session - the agent's session.
 * @param {object} [policy] - resolved policy; decides which user messages are
 *   instructions and therefore carry their text for the planner.
 * @returns {Array<object>} surface descriptors in order.
 */
export function describeSurface(session, policy = SAC_DEFAULTS) {
  const nodes = []
  for (const seq of session.surface.nodes) {
    const event = session.eventAt(seq)
    if (event === undefined) continue
    const weight = weighEvent(event)
    const isUser = event.type === 'user/message'
    const source = isUser ? event.data?.source : undefined
    const node = {
      seq,
      type: event.type,
      isCheckpoint: source?.kind === CHECKPOINT_SOURCE_KIND,
      toolCallCount: weight.toolCalls,
      textChars: weight.chars,
      reasoningChars: weight.reasoningChars,
      argsChars: weight.argsChars,
      plainChars: weight.plainChars,
    }
    if (isUser) {
      node.sourceKind = source?.kind
      if (isInstructionNode(node, policy)) node.text = messageText({ content: event.data?.content })
    }
    nodes.push(node)
  }
  return nodes
}

/**
 * Describe a planned fold for the state update: the segment it folds, the
 * pinned request that segment belongs to, the requests it releases, the span's
 * place on the surface, and whether the harness or a model writes it.
 * @param {ReadonlyArray<object>} nodes - surface descriptors in order.
 * @param {object} span - result of `planSacSpan()`.
 * @returns {{kind: string, anchor: {seq: number, text: string, key: string|null}|null,
 *   released: Array<{seq: number, text: string, key: string|null}>, mechanical: boolean,
 *   startSeq: number, endSeq: number, estimatedTokens: number, spanChars: number}} fold description.
 */
export function describeFold(nodes, span) {
  const anchorNode = span.anchorIndex >= 0 ? nodes[span.anchorIndex] : undefined
  const anchor = typeof anchorNode?.text === 'string' && anchorNode.text.trim().length > 0
    ? { seq: anchorNode.seq, text: anchorNode.text, key: instructionKey(anchorNode) }
    : null
  const released = (span.released ?? []).map((index) => ({
    seq: nodes[index].seq,
    text: nodes[index].text ?? '',
    key: instructionKey(nodes[index]),
  }))
  return {
    kind: span.kind,
    anchor,
    released,
    mechanical: span.mechanical === true,
    digestClip: span.digestClip ?? 0,
    startSeq: nodes[span.startIndex]?.seq,
    endSeq: nodes[span.endIndex]?.seq,
    estimatedTokens: span.estimatedTokens ?? 0,
    spanChars: span.chars ?? 0,
  }
}

export default apply