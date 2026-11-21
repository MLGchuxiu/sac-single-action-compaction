import assert from 'node:assert/strict'
import { test } from 'node:test'
import { apply, PAPER_STATE_PROMPT } from '../index.js'

/** Minimal Cordis-shaped context: event bus + service lookup + logger. */
function fakeCtx({ engine, logger = { info() {}, warn() {} } } = {}) {
  const listeners = new Map()
  const services = new Map()
  if (engine !== undefined) services.set('compaction', engine)
  const ctx = {
    logger,
    on(name, handler) {
      if (!listeners.has(name)) listeners.set(name, [])
      listeners.get(name).push(handler)
      return () => {}
    },
    get(name) {
      return services.get(name)
    },
    async dispatch(name, payload) {
      const chain = listeners.get(name) ?? []
      let index = 0
      const next = async () => {
        if (index >= chain.length) return 'done'
        const handler = chain[index++]
        return handler(payload, next)
      }
      return next()
    },
  }
  return ctx
}

/** Session stub exposing a surface and events. */
function fakeSession(events) {
  const map = new Map(events.map((event) => [event.seq, event]))
  return {
    id: 'session-test',
    surface: { nodes: events.map((event) => event.seq), replaceGeneration: 1 },
    eventAt: (seq) => map.get(seq),
    requestHeader: () => ({ config: { provider: 'p', model: 'm' } }),
    toolHistory: () => [],
  }
}

const taskEvent = (seq) => ({
  seq,
  type: 'user/message',
  data: { role: 'user', content: [{ type: 'text', text: 'do the thing' }] },
})
const assistantCall = (seq) => ({
  seq,
  type: 'assistant/message',
  data: { message: { role: 'assistant', content: [{ type: 'tool-call', toolName: 'bash', arguments: { command: 'ls' } }] } },
})
const toolResult = (seq, text = 'a.py\nb.py') => ({
  seq,
  type: 'tool/result',
  data: { message: { role: 'tool', content: [{ type: 'text', text }] } },
})

test('compacts one action and passes a balanced range to the engine', async () => {
  const calls = []
  const engine = {
    compactRegion: async (start, end, agent, signal) => {
      calls.push({ start, end, agent, signal })
      return { shadowedSeqs: [start, end] }
    },
  }
  const ctx = fakeCtx({ engine })
  apply(ctx, { minSpanChars: 0, statePrompt: 'native' })
  const session = fakeSession([taskEvent(1), assistantCall(2), toolResult(3)])
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: new AbortController().signal })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].start, 2)
  assert.equal(calls[0].end, 3)
})

test('does nothing without a completed action', async () => {
  const calls = []
  const engine = { compactRegion: async (start, end) => calls.push([start, end]) }
  const ctx = fakeCtx({ engine })
  apply(ctx, { minSpanChars: 0 })
  const session = fakeSession([taskEvent(1)])
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: new AbortController().signal })
  assert.equal(calls.length, 0)
})

test('engine failures never break the turn and are logged once', async () => {
  const warnings = []
  let attempts = 0
  const engine = {
    compactRegion: async () => {
      attempts += 1
      throw new Error('compaction busy')
    },
  }
  const ctx = fakeCtx({ engine, logger: { info() {}, warn: (m) => warnings.push(m) } })
  apply(ctx, { minSpanChars: 0 })
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) }
  const signal = new AbortController().signal
  assert.equal(await ctx.dispatch('agent/pre-step', { agent, signal }), 'done')
  assert.equal(await ctx.dispatch('agent/pre-step', { agent, signal }), 'done')
  assert.equal(attempts, 2)
  assert.equal(warnings.length, 1, 'identical failures are logged once')
})

test('aborted signals skip compaction', async () => {
  const calls = []
  const engine = { compactRegion: async () => calls.push(1) }
  const ctx = fakeCtx({ engine })
  apply(ctx, { minSpanChars: 0 })
  const controller = new AbortController()
  controller.abort()
  await ctx.dispatch('agent/pre-step', {
    agent: { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) },
    signal: controller.signal,
  })
  assert.equal(calls.length, 0)
})

test('no compaction service means a silent pass-through', async () => {
  const ctx = fakeCtx()
  apply(ctx, {})
  assert.equal(await ctx.dispatch('agent/pre-step', {
    agent: { session: fakeSession([taskEvent(1)]) },
    signal: new AbortController().signal,
  }), 'done')
})

test('disabled plugin registers no listener', async () => {
  const ctx = fakeCtx({ engine: { compactRegion: async () => {} } })
  apply(ctx, { enabled: false })
  assert.equal(await ctx.dispatch('agent/pre-step', { agent: {}, signal: undefined }), 'done')
})

test('paper state prompt swaps the backend summarize hook', () => {
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  apply(ctx, { statePrompt: 'paper' })
  assert.notEqual(engine.summarize, undefined)
  assert.ok(PAPER_STATE_PROMPT.includes('Established facts'))
})

test('paper summarize forwards provider, model, tools and cancellation', async () => {
  const seen = []
  const engine = {
    compactRegion: async () => {},
    summarize: async () => ({ summary: [] }),
  }
  const ctx = fakeCtx({ engine })
  apply(ctx, { statePrompt: 'paper' })
  ctx.llm = {
    stream: async function* (options) {
      seen.push(options)
      yield { type: 'text-delta', text: 'state body' }
      yield { type: 'finish', usage: { totalTokens: 9 } }
    },
  }
  const controller = new AbortController()
  const result = await engine.summarize(
    { messages: [{ role: 'user', content: [{ type: 'text', text: 'hist' }] }], tools: [] },
    { session: fakeSession([taskEvent(1)]) },
    controller.signal,
  )
  assert.equal(result.summary[0].text, 'state body')
  assert.equal(seen[0].provider, 'p')
  assert.equal(seen[0].signal, controller.signal)
  assert.equal(seen[0].purpose, 'compaction')
  assert.ok(seen[0].messages.at(-1).content[0].text.includes('Objective'))
})

// --------------------------------------------------------------------------- //
// A1: lazy install   /   A3: telemetry
// --------------------------------------------------------------------------- //
import { stateUpdateInstalled, telemetrySnapshot, trimForSummary, trimOversized } from '../index.js'

/** Context whose service table can be filled after apply() ran. */
function mutableCtx({ logger } = {}) {
  const services = new Map()
  const listeners = new Map()
  const ctx = {
    logger: logger ?? { info() {}, warn() {}, error() {} },
    streamed: [],
    llm: {
      async *stream(options) {
        ctx.streamed.push(options)
        yield { type: 'text-delta', text: 'OBJECTIVE: do the thing' }
        yield { type: 'finish', usage: { inputTokens: 1, outputTokens: 1 } }
      },
    },
    on(name, handler) {
      if (!listeners.has(name)) listeners.set(name, [])
      listeners.get(name).push(handler)
      return () => {}
    },
    get(name) {
      return services.get(name)
    },
    provide(name, value) {
      services.set(name, value)
    },
    async dispatch(name, payload) {
      const chain = listeners.get(name) ?? []
      let index = 0
      const next = async () => (index >= chain.length ? 'done' : chain[index++](payload, next))
      return next()
    },
  }
  return ctx
}

/** Backend that routes compactRegion through its own summarize hook. */
function routingEngine() {
  return {
    async compactRegion(start, end, agent) {
      await this.summarize({ messages: [{ role: 'user', content: [{ type: 'text', text: 'history' }] }] }, agent, undefined)
      return undefined
    },
    async summarize() {
      return { summary: [{ type: 'text', text: 'base summary' }] }
    },
  }
}

test('A1: the paper template is installed on the first pre-step when the backend mounts late', async () => {
  const ctx = mutableCtx()
  apply(ctx, { minSpanChars: 0, statePrompt: 'paper' })
  const engine = routingEngine()
  ctx.provide('compaction', engine)
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) }
  const signal = new AbortController().signal

  assert.equal(stateUpdateInstalled(engine), false, 'not installed before the first step')
  await ctx.dispatch('agent/pre-step', { agent, signal })
  assert.equal(stateUpdateInstalled(engine), true, 'installed on the first pre-step')
  assert.equal(ctx.streamed.length, 1, 'the wrapped summarize ran once')

  await ctx.dispatch('agent/pre-step', { agent, signal })
  assert.equal(ctx.streamed.length, 2, 'the hook must not be wrapped twice (one call per fold)')

  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.installedOn, 'pre-step')
  assert.equal(telemetry.installFailures, 0)
})

test('A1: a backend without a summarize hook is reported once and compaction still runs', async () => {
  const errors = []
  const warnings = []
  let compactions = 0
  const engine = { compactRegion: async () => { compactions += 1 } }
  const ctx = mutableCtx({ logger: { info() {}, warn: (m) => warnings.push(m), error: (m) => errors.push(m) } })
  apply(ctx, { minSpanChars: 0, statePrompt: 'paper' })
  ctx.provide('compaction', engine)
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) }
  const signal = new AbortController().signal

  await ctx.dispatch('agent/pre-step', { agent, signal })
  await ctx.dispatch('agent/pre-step', { agent, signal })

  assert.equal(compactions, 2, 'compaction is not blocked by a missing hook')
  assert.equal(errors.length, 1, 'the degradation is reported exactly once')
  assert.match(errors[0], /statePrompt=paper/)
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.installFailures, 2)
  assert.equal(telemetry.installedOn, null)
})

test('A3: telemetry counts steps, folds, folded tokens and summaries', async () => {
  const ctx = mutableCtx()
  apply(ctx, { minSpanChars: 0, statePrompt: 'paper' })
  ctx.provide('compaction', routingEngine())
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) }
  const signal = new AbortController().signal

  await ctx.dispatch('agent/pre-step', { agent, signal })
  await ctx.dispatch('agent/pre-step', { agent, signal })

  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.steps, 2)
  assert.equal(telemetry.compactions, 2)
  assert.ok(telemetry.foldedTokens > 0, 'folded tokens are accumulated')
  assert.equal(telemetry.lastFoldActions, 1)
  assert.equal(telemetry.summarizeCalls, 2)
  assert.ok(telemetry.summarizeChars > 0, 'summary size is recorded')
  assert.equal(telemetry.skipped, 0)
})

test('A3: telemetry records a skipped fold and its reason', async () => {
  const engine = { compactRegion: async () => { throw new Error('compaction busy') } }
  const ctx = mutableCtx()
  apply(ctx, { minSpanChars: 0, statePrompt: 'native' })
  ctx.provide('compaction', engine)
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) }

  await ctx.dispatch('agent/pre-step', { agent, signal: new AbortController().signal })

  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.skipped, 1)
  assert.match(telemetry.lastSkipReason, /busy/)
  assert.equal(telemetry.compactions, 0)
})

test('A3: telemetry is null for an agent that never ran SAC', () => {
  assert.equal(telemetrySnapshot({}), null)
})

test('B1: paper summarize retries once when the first attempt yields no text', async () => {
  const calls = []
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  apply(ctx, { statePrompt: 'paper' })
  let attempt = 0
  ctx.llm = {
    stream: async function* (options) {
      calls.push(options)
      attempt += 1
      if (attempt === 1) {
        yield { type: 'finish', usage: { totalTokens: 5 } }
      } else {
        yield { type: 'text-delta', text: 'recovered state' }
      }
    },
  }
  const result = await engine.summarize(
    { messages: [{ role: 'user', content: [{ type: 'text', text: 'hist' }] }] },
    { session: fakeSession([taskEvent(1)]) },
  )
  assert.equal(result.summary[0].text, 'recovered state')
  assert.equal(result.retried, true)
  assert.equal(calls.length, 2)
  assert.ok(calls[1].maxTokens > calls[0].maxTokens, 'retry raises the output cap')
})


test('C1: trimForSummary bounds long blocks and leaves short ones alone', () => {
  const long = 'x'.repeat(20000)
  const messages = [
    { role: 'tool', content: [{ type: 'text', text: long }] },
    { role: 'tool', content: [{ type: 'text', text: 'short observation' }] },
  ]
  const out = trimForSummary(messages, 1000)
  assert.ok(out[0].content[0].text.length < 2000)
  assert.ok(out[0].content[0].text.includes('omitted'))
  assert.equal(out[1].content[0].text, 'short observation')
  assert.equal(trimForSummary(messages, 0), messages)
})

// --------------------------------------------------------------------------- //
// v9 pipeline: inline state, ledger, recall/check, adaptive budget
// --------------------------------------------------------------------------- //
import {
  boundState,
  checkTool,
  extractInlineState,
  extractStateBlock,
  ledgerEntries,
  latestSurfaceState,
  recallTool,
  renderChecklist,
  renderLedger,
  searchSession,
} from '../index.js'

const assistantState = (seq, body) => ({
  seq,
  type: 'assistant/message',
  data: { message: { role: 'assistant', content: [{ type: 'text', text: `<state>\n${body}\n</state>` }] } },
})

test('v9: extractStateBlock and extractInlineState read the model-written block', () => {
  assert.equal(extractStateBlock('x <state>hello world this state body is long enough to keep</state> y'), 'hello world this state body is long enough to keep')
  assert.equal(extractStateBlock('<state>too short</state>'), null)
  const messages = [{ role: 'assistant', content: [{ type: 'text', text: '<state>first state body that is long enough to keep</state>' }] }]
  assert.equal(extractInlineState(messages), 'first state body that is long enough to keep')
  assert.equal(extractInlineState([]), null)
})

test('v9: inline summarize uses the model-written state and makes no LLM call', async () => {
  let llmCalls = 0
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.systemPrompt = { section: () => () => {} }
  ctx.llm = { stream: async function* () { llmCalls += 1; yield { type: 'text-delta', text: 'llm' } } }
  apply(ctx, { statePrompt: 'inline', minSpanChars: 0 })
  const agent = { session: fakeSession([taskEvent(1), assistantState(2, 'the model wrote this state and it is long enough to keep')]) }
  const result = await engine.summarize({ messages: [] }, agent)
  assert.match(result.summary[0].text, /the model wrote this state/)
  assert.equal(result.llmStreamCall, false)
  assert.equal(llmCalls, 0)
})

test('v9: inline falls back to the paper summarizer when the block is missing', async () => {
  let llmCalls = 0
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.systemPrompt = { section: () => () => {} }
  ctx.llm = {
    stream: async function* () {
      llmCalls += 1
      yield { type: 'text-delta', text: 'paper fallback body' }
    },
  }
  apply(ctx, { statePrompt: 'inline', minSpanChars: 0 })
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2)]) }
  const result = await engine.summarize({ messages: [] }, agent)
  assert.match(result.summary[0].text, /paper fallback body/)
  assert.equal(llmCalls, 1)
})

test('v9: ledger entries render tool calls and results, newest entries survive truncation', () => {
  const messages = [
    { role: 'assistant', content: [{ type: 'tool-call', name: 'bash', arguments: '{"command":"ls"}' }] },
    { role: 'tool', content: [{ type: 'text', text: 'a.py b.py' }] },
  ]
  const entries = ledgerEntries(messages)
  assert.equal(entries.length, 2)
  assert.match(entries[0], /bash/)
  assert.match(entries[1], /a\.py/)
  const rendered = renderLedger(entries, 20)
  assert.ok(rendered.length <= 60)
  assert.match(rendered, /a\.py/)
})

test('v9: ledger is cumulative across folds and prepended to the state', async () => {
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.llm = { stream: async function* () { yield { type: 'text-delta', text: 'recap' } } }
  apply(ctx, { statePrompt: 'paper', ledger: true, minSpanChars: 0 })
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2), toolResult(3)]) }
  const span = [
    { role: 'assistant', content: [{ type: 'tool-call', name: 'read', arguments: '{"file":"a.py"}' }] },
    { role: 'tool', content: [{ type: 'text', text: 'alpha content' }] },
  ]
  const first = await engine.summarize({ messages: span }, agent)
  assert.match(first.summary[0].text, /\[SAC LEDGER\]/)
  assert.match(first.summary[0].text, /read/)
  const second = await engine.summarize({
    messages: [{ role: 'assistant', content: [{ type: 'tool-call', name: 'grep', arguments: '{"pattern":"x"}' }] }],
  }, agent)
  assert.match(second.summary[0].text, /read/)
  assert.match(second.summary[0].text, /grep/)
})

test('v9: adaptive budget bounds the state and renderChecklist marks items', () => {
  const bounded = boundState('x'.repeat(5000), 200, 3.5)
  assert.ok(bounded.length < 1000)
  assert.match(bounded, /truncated to budget/)
  const rendered = renderChecklist(['first', 'second'], new Set([1]))
  assert.match(rendered, /\[x\] 1\. first/)
  assert.match(rendered, /\[ \] 2\. second/)
})

test('v9: searchSession finds folded history by keyword', () => {
  const session = fakeSession([
    taskEvent(1),
    { seq: 2, type: 'assistant/message', data: { message: { role: 'assistant', content: [{ type: 'text', text: 'the budget total is 2171' }] } } },
    { seq: 3, type: 'tool/result', data: { message: { role: 'tool', content: [{ type: 'text', text: 'unrelated output' }] } } },
  ])
  const found = searchSession(session, 'budget', 3)
  assert.match(found, /2171/)
  assert.equal(searchSession(session, 'zzz', 3), '(no archived match)')
})

test('v9: recall and check tools are registered when enabled', async () => {
  const registered = []
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.tools = { register: (definition) => { registered.push(definition.name); return () => {} } }
  apply(ctx, { recall: true, checklist: true })
  assert.deepEqual(registered.sort(), ['check', 'recall'])
})

test('v9: check tool marks an item after the checklist is generated', async () => {
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.llm = {
    stream: async function* () {
      yield { type: 'text-delta', text: 'read the file\nfix the bug\nrun the test' }
    },
  }
  apply(ctx, { checklist: true, minSpanChars: 0 })
  const agent = { session: fakeSession([taskEvent(1)]) }
  await ctx.dispatch('agent/pre-step', { agent, signal: new AbortController().signal })
  const result = await checkTool().execute({ index: 2 }, { agent })
  assert.match(result, /\[checked\] item 2: fix the bug/)
  assert.match(result, /\[x\] 2\. fix the bug/)
})

test('v9: inline forces keepLastObservation so the newest action stays raw', async () => {
  const calls = []
  const engine = {
    compactRegion: async (start, end) => { calls.push({ start, end }); return {} },
    summarize: async () => ({ summary: [] }),
  }
  const ctx = fakeCtx({ engine })
  ctx.systemPrompt = { section: () => () => {} }
  apply(ctx, { statePrompt: 'inline', minSpanChars: 0, minSpanTokens: 0 })
  const session = fakeSession([
    taskEvent(1), assistantCall(2), toolResult(3), assistantCall(4), toolResult(5),
  ])
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: new AbortController().signal })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].end, 3, 'the newest observation (seq 5) stays outside the fold')
})


// --------------------------------------------------------------------------- //
// iter6: oversized-observation trim, sticky inline fallback, context budget
// --------------------------------------------------------------------------- //
test('iter6: trimOversized leaves small observations verbatim and bounds huge ones', () => {
  const small = 'x'.repeat(500)
  const huge = 'y'.repeat(10_000)
  const messages = [
    { role: 'tool', content: [{ type: 'text', text: small }] },
    { role: 'tool', content: [{ type: 'text', text: huge }] },
  ]
  const out = trimOversized(messages, 4000)
  assert.equal(out[0].content[0].text, small, 'small observation untouched')
  assert.ok(out[1].content[0].text.length < huge.length, 'huge observation trimmed')
  assert.match(out[1].content[0].text, /chars omitted/)
  assert.equal(trimOversized(messages, 0), messages, '0 disables trimming')
})

test('iter6: inline state stays disabled after repeated misses (sticky fallback)', async () => {
  let llmCalls = 0
  const warnings = []
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine, logger: { info() {}, warn: (m) => warnings.push(m) } })
  ctx.systemPrompt = { section: () => () => {} }
  ctx.llm = { stream: async function* () { llmCalls += 1; yield { type: 'text-delta', text: 'paper fallback body' } } }
  apply(ctx, { statePrompt: 'inline', minSpanChars: 0, inlineFallbackAfter: 2 })
  const agent = { session: fakeSession([taskEvent(1), assistantCall(2)]) }
  await engine.summarize({ messages: [] }, agent)
  await engine.summarize({ messages: [] }, agent)
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.inlineMisses, 2)
  assert.equal(telemetry.inlineBroken, true)
  assert.equal(llmCalls, 2, 'paper summarizer used for both folds')
  assert.ok(warnings.some((line) => line.includes('switching to the paper summarizer')))
})

test('iter6: contextBudgetTokens forces a fold the size guard would skip', async () => {
  const ctx = mutableCtx()
  apply(ctx, { minSpanChars: 0, minSpanTokens: 1_000_000, contextBudgetTokens: 1_000, statePrompt: 'paper' })
  const engine = routingEngine()
  ctx.provide('compaction', engine)
  const agent = {
    session: fakeSession([
      taskEvent(1),
      assistantCall(2),
      toolResult(3, 'z'.repeat(10_000)),
    ]),
  }
  await ctx.dispatch('agent/pre-step', { agent, signal: new AbortController().signal })
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.budgetForcedFolds, 1, 'budget safety net folded once')
  assert.equal(telemetry.compactions, 1)
})

test('iter6: stateMaxWords adds a recap length contract to the summarizer prompt', async () => {
  const seen = []
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.llm = {
    stream: async function* (options) {
      seen.push(options)
      yield { type: 'text-delta', text: 'state body' }
    },
  }
  apply(ctx, { statePrompt: 'paper', minSpanChars: 0, stateMaxWords: 180 })
  const agent = { session: fakeSession([taskEvent(1)]) }
  await engine.summarize({ messages: [], tools: [] }, agent)
  assert.match(seen[0].messages.at(-1).content[0].text, /under 180 words/)
})

test('iter6: stateInputLean drops the system prompt and tool schemas from the summarizer request', async () => {
  const seen = []
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx({ engine })
  ctx.llm = {
    stream: async function* (options) {
      seen.push(options)
      yield { type: 'text-delta', text: 'state body' }
    },
  }
  apply(ctx, { statePrompt: 'paper', minSpanChars: 0, stateInputLean: true })
  const agent = { session: fakeSession([taskEvent(1)]) }
  const messages = [
    { role: 'system', content: [{ type: 'text', text: 'SYSTEM PROMPT' }] },
    { role: 'user', content: [{ type: 'text', text: 'task' }] },
  ]
  await engine.summarize({ messages, tools: [{ name: 'bash' }] }, agent)
  assert.equal(seen[0].messages.some((message) => message.role === 'system'), false)
  assert.equal('tools' in seen[0], false)
  assert.equal('toolHistory' in seen[0], false)
})
