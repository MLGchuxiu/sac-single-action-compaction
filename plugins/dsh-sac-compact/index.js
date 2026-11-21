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
 *
 * @module dsh-sac-compact
 */
import {
  CHECKPOINT_SOURCE_KIND,
  planSacSpan,
  resolvePolicy,
  stateBudgetFor,
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
 * The paper's Appendix G.1 compaction instruction, adapted to a harness that
 * must also keep the semantic state usable by a *separate* inference instance.
 */
export const PAPER_STATE_PROMPT = [
  'Stop work. Write only a concise handoff recap; do not run commands or call tools.',
  'The conversation will reset to the system prompt, task, and this recap.',
  '',
  'Record:',
  '1. Objective - one line describing the required result.',
  '2. Established facts - exact paths, relevant symbols or commands, observed values,',
  '   and important errors/output. Separate observations from unverified ideas.',
  '3. Applied changes - each changed/created file, what changed, and whether verified.',
  '4. Verification - exact checks run, outcomes, failures, obstacles, and workarounds.',
  '5. Dead ends - failed approaches that should not be repeated.',
  '6. Remaining plan - outstanding subgoals, in execution order; the immediate next',
  '   command or edit; and the condition for completion.',
  '7. Output contract - the exact response format the task requires (JSON keys, units,',
  '   file paths, answer shape). Restate it so the next instance cannot get it wrong.',
  '8. Verbatim values - ids, numbers, paths, command lines, symbol and file names',
  '   copied exactly as observed.',
  '',
  'If needed output was truncated, give a narrow command to retrieve it.',
  'Output the recap directly, with no preamble and no visible reasoning.',
].join('\n')

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
 * First non-checkpoint user message text, used to generate the checklist.
 * @param {object} session - agent session.
 * @returns {string} task text (empty when unavailable).
 */
function firstTaskText(session) {
  const nodes = session?.surface?.nodes
  if (!Array.isArray(nodes)) return ''
  for (const seq of nodes) {
    const event = session.eventAt(seq)
    if (event?.type !== 'user/message') continue
    const source = event.data?.source
    if (source?.kind === CHECKPOINT_SOURCE_KIND) continue
    return messageText({ content: event.data?.content })
  }
  return ''
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
 * @returns {Promise<{text: string, usage: object|undefined}>} assembled text.
 */
export async function assembleText(stream) {
  let text = ''
  let usage
  for await (const chunk of stream) {
    if (chunk === null || typeof chunk !== 'object') continue
    if (chunk.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text
    else if (chunk.type === 'block-end' && chunk.block?.type === 'text' && typeof chunk.block.text === 'string') {
      // `block-end` repeats the full block; prefer deltas when both arrive.
      if (text.length === 0) text += chunk.block.text
    } else if (chunk.type === 'usage' && chunk.usage !== undefined) usage = chunk.usage
    else if (chunk.type === 'finish' && chunk.usage !== undefined) usage = chunk.usage
  }
  return { text, usage }
}

/**
 * Compose the final state text: model recap, bounded by the adaptive budget,
 * with the machine checklist and ledger in front.
 * @param {object} state - per-agent SAC state.
 * @param {object} policy - resolved policy.
 * @param {string} recap - model-written (or extracted) recap.
 * @returns {string} state text written into the checkpoint.
 */
function composeState(state, policy, recap) {
  const budget = stateBudgetFor(policy, state?.telemetry?.foldedActions ?? 0)
  let body = stripMachineSections(recap)
  // The model sometimes wraps a fallback recap in the same tags; keep one level.
  body = body.replace(/<\/?state>/gi, '').trim()
  body = boundState(body, budget, policy.charsPerToken)
  const parts = []
  if (policy.checklist) {
    const checklist = renderChecklist(state?.checklist, state?.done)
    if (checklist.length > 0) parts.push(`${CHECKLIST_OPEN}\n${checklist}\n${CHECKLIST_CLOSE}`)
  }
  if (policy.ledger) {
    const ledger = renderLedger(state?.ledger, policy.ledgerChars)
    if (ledger.length > 0) parts.push(`${LEDGER_OPEN}\n${ledger}\n${LEDGER_CLOSE}`)
  }
  parts.push(body)
  return parts.filter((part) => part.length > 0).join('\n\n')
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

    if (policy.statePrompt === 'inline' && state?.inlineBroken !== true) {
      const inline = latestSurfaceState(agent) ?? extractInlineState(input?.messages)
      if (inline !== null) {
        if (state !== undefined) state.telemetry.inlineHits += 1
        const header = agent.session.requestHeader()
        const target = header?.config
        return {
          summary: [{ type: 'text', text: composeState(state, policy, inline) }],
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

    const header = agent.session.requestHeader()
    const target = header?.config
    if (target === undefined || !target.provider || !target.model) {
      throw new Error('dsh-sac-compact: no routed provider/model available for the state update')
    }
    const llm = serviceOf(ctx, 'llm')
    if (llm === undefined || typeof llm.stream !== 'function') {
      throw new Error('dsh-sac-compact: no llm service available for the state update')
    }
    const baseMax = Math.max(512, Math.min(4096, Math.floor(policy.stateMaxTokens ?? 2048)))
    // Reasoning models can spend the whole output budget on hidden reasoning and
    // return no text. One retry with a direct instruction and a larger cap turns
    // that transient failure into a normal fold instead of a lost compaction.
    let text = ''
    let usage
    let retried = false
    let maxTokens = baseMax
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const wordLimit = (policy.stateMaxWords ?? 0) > 0
        ? `\nKeep the whole recap under ${policy.stateMaxWords} words; prefer exact values over prose.`
        : ''
      const instruction = attempt === 0
        ? `${PAPER_STATE_PROMPT}${wordLimit}`
        : `${PAPER_STATE_PROMPT}${wordLimit}\n\nOutput the recap now, directly, with no preamble and no reasoning.`
      const lean = policy.stateInputLean === true
      const sourceMessages = lean
        ? (input.messages ?? []).filter((message) => message?.role !== 'system' && message?.role !== 'developer')
        : input.messages
      const bounded = trimForSummary(sourceMessages, policy.stateInputMaxChars ?? 0)
      const messages = [
        ...trimOversized(bounded, policy.trimObservationOver ?? 0),
        { role: 'user', content: [{ type: 'text', text: instruction }] },
      ]
      maxTokens = attempt === 0 ? baseMax : Math.min(4096, Math.max(2048, baseMax * 2))
      const options = {
        provider: target.provider,
        model: target.model,
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
      if (text.length > 0) {
        retried = attempt > 0
        break
      }
    }
    if (text.length === 0) throw new Error('dsh-sac-compact: state update produced no text (2 attempts)')
    return {
      summary: [{ type: 'text', text: composeState(state, policy, text) }],
      rawOutput: [{ type: 'text', text }],
      llmStreamCall: true,
      provider: target.provider,
      model: target.model,
      maxTokens,
      retried,
      ...(usage === undefined ? {} : { usage }),
    }
  }
}

/**
 * Generate the v9 subgoal checklist once per agent.
 * @param {object} ctx - plugin context.
 * @param {object} agent - agent.
 * @param {object} state - per-agent SAC state.
 * @param {AbortSignal|undefined} signal - cancellation.
 * @returns {Promise<void>} resolves after the attempt (success or not).
 */
async function ensureChecklist(ctx, agent, state, signal) {
  if (state.checklistRequested === true) return
  // The first pre-step runs before any request is routed, so there is no
  // provider/model yet. Wait for the first fold (after the first action), when
  // the request header exists, instead of giving up permanently.
  const target = agent.session.requestHeader()?.config
  const llm = serviceOf(ctx, 'llm')
  if (target?.provider === undefined || target?.model === undefined || llm?.stream === undefined) return
  const task = firstTaskText(agent.session)
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

    if (policy.checklist) await ensureChecklist(ctx, agent, state, signal)

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
      if (!withinTurnBudget(state.compactions, policy)) {
        nudgeInline(ctx, agent, state, policy)
        return next()
      }
      const nodes = describeSurface(agent.session)
      let span = planSacSpan(nodes, policy)
      if (span === null && (policy.contextBudgetTokens ?? 0) > 0) {
        const surfaceTokens = Math.ceil(
          nodes.reduce((sum, node) => sum + (node.textChars ?? 0), 0) / policy.charsPerToken,
        )
        if (surfaceTokens >= policy.contextBudgetTokens) {
          span = planSacSpan(nodes, { ...policy, minSpanChars: 0, minSpanTokens: 0 })
          if (span !== null) {
            state.telemetry.budgetForcedFolds += 1
            ctx.logger?.info?.(
              `dsh-sac-compact: context ${surfaceTokens} >= budget ${policy.contextBudgetTokens}; forcing a fold`,
            )
          }
        }
      }
      if (span === null) {
        nudgeInline(ctx, agent, state, policy)
        return next()
      }

      const start = nodes[span.startIndex].seq
      const end = nodes[span.endIndex].seq
      const result = await engine.compactRegion(start, end, agent, signal)
      state.compactions += 1
      state.telemetry.compactions += 1
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
        + `installed=${t.installedOn ?? 'n/a'}`,
      )
      nudgeInline(ctx, agent, state, policy)
      return result === undefined ? next() : next()
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
 * @param {object} event - session event.
 * @returns {{chars: number, toolCalls: number}} weight and tool-call count.
 */
function weighEvent(event) {
  const blocks = event?.type === 'user/message'
    ? event.data?.content
    : event.data?.message?.content
  let chars = 0
  let toolCalls = 0
  if (Array.isArray(blocks)) {
    for (const block of blocks) {
      if (typeof block?.text === 'string') chars += block.text.length
      else if (block?.type === 'tool-call') {
        toolCalls += 1
        chars += JSON.stringify(block.arguments ?? block.input ?? {}).length
      } else if (block?.type === 'image' || block?.type === 'image-url') chars += 1600
      else if (block !== null && typeof block === 'object') chars += 64
    }
  }
  return { chars, toolCalls }
}

/**
 * Adapt a live session surface into the pure descriptor list.
 * @param {object} session - the agent's session.
 * @returns {Array<object>} surface descriptors in order.
 */
export function describeSurface(session) {
  const nodes = []
  for (const seq of session.surface.nodes) {
    const event = session.eventAt(seq)
    if (event === undefined) continue
    const weight = weighEvent(event)
    const source = event.type === 'user/message' ? event.data?.source : undefined
    nodes.push({
      seq,
      type: event.type,
      isCheckpoint: source?.kind === CHECKPOINT_SOURCE_KIND,
      toolCallCount: weight.toolCalls,
      textChars: weight.chars,
    })
  }
  return nodes
}

export default apply