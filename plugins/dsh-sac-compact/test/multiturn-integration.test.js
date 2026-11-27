import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  apply,
  ARCHIVE_STATE_PROMPT,
  describeFold,
  describeSurface,
  paperStatePrompt,
  PAPER_STATE_PROMPT,
  parseRequests,
  registerRequests,
  renderRequests,
  resolveFold,
  telemetrySnapshot,
} from '../index.js'
import { resolvePolicy } from '../lib/sac-policy.js'

/** Minimal Cordis-shaped context: event bus + service lookup + recording LLM. */
function fakeCtx({ logger = { info() {}, warn() {}, error() {} } } = {}) {
  const listeners = new Map()
  const services = new Map()
  const ctx = {
    logger,
    streamed: [],
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
  ctx.llm = {
    /** Records every request and answers by the template it was given. */
    async *stream(options) {
      ctx.streamed.push(options)
      const instruction = options.messages.at(-1).content[0].text
      yield { type: 'text-delta', text: instruction.startsWith('Stop work. Write only a concise archive recap') ? 'ARCHIVE BODY' : 'WORKING BODY' }
      yield { type: 'finish', usage: { inputTokens: 1, outputTokens: 1 } }
    },
  }
  return ctx
}

/** Session stub with a mutable surface backed by a Map of seq -> event. */
function fakeSession(events) {
  const map = new Map(events.map((event) => [event.seq, event]))
  let next = Math.max(0, ...events.map((event) => event.seq)) + 100
  return {
    id: 'session-test',
    surface: { nodes: events.map((event) => event.seq), replaceGeneration: 1 },
    eventAt: (seq) => map.get(seq),
    requestHeader: () => ({ config: { provider: 'p', model: 'm' } }),
    toolHistory: () => [],
    /** Append an event to the log and the surface. */
    push(event) {
      map.set(event.seq, event)
      this.surface.nodes.push(event.seq)
    },
    /** Append a checkpoint as dsh-compaction-basic does. */
    appendCheckpoint(content) {
      const seq = (next += 1)
      map.set(seq, { seq, type: 'user/message', data: { role: 'user', id: `ck-${seq}`, source: { kind: 'compact-checkpoint' }, content } })
      return seq
    },
  }
}

const FIRST = 'FIRST REQUEST: find the api key'
const SECOND = 'SECOND REQUEST: add the model as a provider'
const request = (seq, text) => ({
  seq,
  type: 'user/message',
  data: { role: 'user', id: `u-${seq}`, source: { kind: 'user' }, content: [{ type: 'text', text }] },
})
const notice = (seq, kind = 'runtime-context', text = 'snapshot') => ({
  seq,
  type: 'user/message',
  data: { role: 'user', id: `n-${seq}`, source: { kind }, content: [{ type: 'text', text }] },
})
const checkpoint = (seq, text) => ({
  seq,
  type: 'user/message',
  data: {
    role: 'user', id: `ck-${seq}`, source: { kind: 'compact-checkpoint' }, content: [{ type: 'text', text }],
  },
})
const assistantCall = (seq) => ({
  seq,
  type: 'assistant/message',
  data: { message: { role: 'assistant', content: [{ type: 'tool-call', toolName: 'bash', arguments: { command: 'ls' } }] } },
})
const toolResult = (seq, text = 'x'.repeat(1500)) => ({
  seq,
  type: 'tool/result',
  data: { message: { role: 'tool', content: [{ type: 'text', text }] } },
})
const reply = (seq, text = 'Here is the answer. '.repeat(30)) => ({
  seq,
  type: 'assistant/message',
  data: { message: { role: 'assistant', content: [{ type: 'text', text }] } },
})

/** Backend stub routing compactRegion through its summarize hook, like dsh-compaction-basic. */
function fakeBackend() {
  return {
    calls: [],
    async summarize() {
      return { summary: [{ type: 'text', text: 'base summary' }] }
    },
    async compactRegion(start, end, agent) {
      const nodes = agent.session.surface.nodes
      const first = nodes.indexOf(start)
      const last = nodes.lastIndexOf(end)
      this.calls.push({ start, end })
      const region = nodes.slice(first, last + 1).map((seq) => {
        const event = agent.session.eventAt(seq)
        return event.type === 'user/message' ? event.data : event.data.message
      })
      const result = await this.summarize({ messages: region }, agent, undefined)
      nodes.splice(first, last - first + 1, agent.session.appendCheckpoint(result.summary))
      return { shadowedRange: { start, end } }
    },
  }
}

/** The shipped preset policy, with the size guards off so small stubs fold. */
const PRESET = {
  minSpanChars: 0, minSpanTokens: 0, statePrompt: 'paper', stateInputLean: true, stateMaxWords: 400,
  actionsPerCompaction: 2, keepLastObservation: true,
}
const signal = () => new AbortController().signal
const firstText = (options) => options.messages[0].content[0].text
const lastText = (options) => options.messages.at(-1).content[0].text

test('describeSurface marks provenance and carries the text of instructions only', () => {
  const session = fakeSession([request(1, 'do the thing'), notice(2), assistantCall(3), toolResult(4)])
  const [task, snapshot] = describeSurface(session)
  assert.equal(task.sourceKind, 'user')
  assert.equal(task.text, 'do the thing')
  assert.equal(snapshot.sourceKind, 'runtime-context')
  assert.equal('text' in snapshot, false, 'a notice is not quoted')
  // A task without provenance keeps working (older hosts, tests).
  const legacy = fakeSession([{ seq: 1, type: 'user/message', data: { role: 'user', content: [{ type: 'text', text: 'old host' }] } }])
  assert.equal(describeSurface(legacy)[0].text, 'old host')
})

test('a follow-up request is pinned and the state update is anchored on the request it belongs to', async () => {
  const session = fakeSession([
    request(1, FIRST), assistantCall(2), toolResult(3), assistantCall(4), toolResult(5), reply(6),
    request(7, SECOND), assistantCall(8), toolResult(9),
  ])
  const ctx = fakeCtx()
  const engine = fakeBackend()
  ctx.provide('compaction', engine)
  apply(ctx, PRESET)
  const agent = { session }

  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.deepEqual(engine.calls, [{ start: 2, end: 5 }], 'the finished turn folds up to its final reply')
  assert.equal(firstText(ctx.streamed[0]), FIRST, 'the request is quoted verbatim in front of the span')
  assert.ok(!JSON.stringify(ctx.streamed[0].messages).includes(SECOND), 'the finished turn is not told about the new request')
  assert.match(lastText(ctx.streamed[0]), /first user message above is the request this work belongs to/)

  for (const event of [assistantCall(10), toolResult(11), assistantCall(12), toolResult(13)]) session.push(event)
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.deepEqual(engine.calls[1], { start: 8, end: 11 }, 'the working state folds two actions and keeps the newest')
  assert.equal(firstText(ctx.streamed[1]), SECOND, 'the working state is anchored on the request in hand')
  assert.ok(engine.calls.every(({ start, end }) => !(start <= 7 && end >= 7)), 'the request seq 7 is never inside a span')
  assert.deepEqual(session.surface.nodes.filter((seq) => seq === 1 || seq === 7), [1, 7], 'both requests stay on the surface')
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.frozenFolds, 1)
  assert.equal(telemetry.compactions, 2)
})

test('nothing folds before the model has acted on a new request', async () => {
  const session = fakeSession([request(1, FIRST), assistantCall(2), toolResult(3), reply(4), request(5, SECOND)])
  const ctx = fakeCtx()
  const engine = fakeBackend()
  ctx.provide('compaction', engine)
  apply(ctx, PRESET)
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
  assert.equal(engine.calls.length, 0, 'the first reply is not held up by a fold')
  assert.equal(ctx.streamed.length, 0)
})

test('regression: with pinInstructions off the follow-up request is folded with the previous turn', async () => {
  const session = fakeSession([
    request(1, FIRST), assistantCall(2), toolResult(3), assistantCall(4), toolResult(5), reply(6),
    request(7, SECOND), assistantCall(8), toolResult(9),
  ])
  const ctx = fakeCtx()
  const engine = fakeBackend()
  ctx.provide('compaction', engine)
  apply(ctx, { ...PRESET, pinInstructions: false })
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
  assert.deepEqual(engine.calls, [{ start: 2, end: 7 }], 'the old policy folds the request seq 7 away')
})

test('an older request is released into an archive that quotes it verbatim', async () => {
  const session = fakeSession([
    request(1, FIRST), checkpoint(2, 'STATE OF TURN ONE'), reply(3), request(4, SECOND), assistantCall(5), toolResult(6),
  ])
  const ctx = fakeCtx()
  const engine = fakeBackend()
  ctx.provide('compaction', engine)
  apply(ctx, { ...PRESET, maxPinnedInstructions: 1, archiveMinTokens: 1 })
  const agent = { session }

  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.deepEqual(engine.calls, [{ start: 1, end: 2 }], 'the first request and its state merge; the final reply stays')
  const archiveCall = ctx.streamed[0]
  assert.equal(archiveCall.messages.length, 3, 'no request is quoted in front: the archive has no request in hand')
  assert.match(lastText(archiveCall), /^Stop work\. Write only a concise archive recap/)
  const text = session.eventAt(session.surface.nodes[0]).data.content[0].text
  assert.match(text, /\[SAC EARLIER USER REQUESTS\]/)
  assert.ok(text.includes('<request 1>\nFIRST REQUEST: find the api key\n</request 1>'), 'the request is quoted exactly')
  assert.match(text, /ARCHIVE BODY/)
  assert.ok(session.surface.nodes.includes(4), 'the request in hand stays pinned')
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.archiveFolds, 1)
  assert.equal(telemetry.carriedRequests, 1)

  // The next working-state fold is anchored on the request in hand and does not repeat the list.
  for (const event of [assistantCall(7), toolResult(8), assistantCall(9), toolResult(10)]) session.push(event)
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.deepEqual(engine.calls[1], { start: 5, end: 8 })
  assert.equal(firstText(ctx.streamed[1]), SECOND)
  const working = session.eventAt(session.surface.nodes.at(-3)).data.content[0].text
  assert.equal(working, 'WORKING BODY', 'the archive checkpoint on the surface already shows the list')
  assert.equal(telemetrySnapshot(agent).archiveFolds, 1)
})

test('a fold the backend starts on its own keeps every request it shadows', async () => {
  const makeEngine = () => ({ compactRegion: async () => {}, summarize: async () => ({ summary: [] }) })
  const derived = (session, seqs) => seqs.map((seq) => {
    const event = session.eventAt(seq)
    return event.type === 'user/message' ? event.data : event.data.message
  })

  // The backend's own range (pressure recovery, /compact) crosses the pinned request.
  const session = fakeSession([request(1, FIRST), checkpoint(2, 'state'), request(3, 'THIRD REQUEST'), assistantCall(4), toolResult(5)])
  const ctx = fakeCtx()
  ctx.provide('compaction', makeEngine())
  apply(ctx, PRESET)
  const result = await ctx.get('compaction').summarize({ messages: derived(session, [2, 3, 4, 5]) }, { session })
  assert.equal(firstText(ctx.streamed[0]), 'THIRD REQUEST', 'the span belongs to the newest request inside it')
  assert.match(result.summary[0].text, /\[SAC EARLIER USER REQUESTS\]/)
  assert.ok(result.summary[0].text.includes('THIRD REQUEST'), 'a request that leaves the surface is quoted')
  assert.ok(!result.summary[0].text.includes(FIRST), 'a request that stays pinned is not repeated')

  // A span without a request belongs to the request in front of it on the surface.
  const before = fakeSession([request(1, FIRST), checkpoint(2, 'state'), assistantCall(3), toolResult(4)])
  const plain = fakeCtx()
  plain.provide('compaction', makeEngine())
  apply(plain, PRESET)
  const plainResult = await plain.get('compaction').summarize({ messages: derived(before, [2, 3, 4]) }, { session: before })
  assert.equal(firstText(plain.streamed[0]), FIRST)
  assert.equal(plainResult.summary[0].text, 'WORKING BODY', 'nothing is carried: no request left the surface')
})

test('a span the backend refuses as "not smaller" is not retried every step', async () => {
  const session = fakeSession([
    request(1, FIRST), assistantCall(2), toolResult(3), reply(4), request(5, SECOND), assistantCall(6), toolResult(7),
  ])
  let attempts = 0
  const engine = {
    compactRegion: async () => {
      attempts += 1
      throw new Error('summary is not smaller than the shadowed content (1200 >= 1100)')
    },
  }
  const ctx = fakeCtx({ engine })
  ctx.provide('compaction', engine)
  apply(ctx, PRESET)
  const agent = { session }
  for (let step = 0; step < 5; step += 1) {
    assert.equal(await ctx.dispatch('agent/pre-step', { agent, signal: signal() }), 'done')
  }
  assert.equal(attempts, 2, 'two attempts, then the span is left alone')
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.refusedFolds, 2)
  assert.equal(telemetry.skipped, 2)
  assert.equal(telemetry.compactions, 0)
})

test('resolveFold follows the plan the pre-step recorded', () => {
  const policy = resolvePolicy(PRESET)
  const anchor = { seq: 1, text: FIRST, key: FIRST }
  // Since 1.3.0 the plan also says where the span lies and who writes it.
  assert.deepEqual(
    resolveFold({ pendingFold: { kind: 'frozen', anchor, released: [], mechanical: true, startSeq: 2, endSeq: 5, estimatedTokens: 900, spanChars: 3150 } }, { messages: [] }, {}, policy),
    {
      kind: 'frozen', anchorText: FIRST, carry: [], mechanical: true, startSeq: 2, endSeq: 5, estimatedTokens: 900, spanChars: 3150,
    },
  )
  assert.deepEqual(
    resolveFold({ pendingFold: { kind: 'archive', anchor: null, released: [anchor] } }, { messages: [] }, {}, policy),
    {
      kind: 'archive', anchorText: '', carry: [{ text: FIRST, key: FIRST }], mechanical: false,
      startSeq: undefined, endSeq: undefined, estimatedTokens: 0, spanChars: 0,
    },
  )
  // Without a plan and without any request: an ordinary working-state fold written by a model.
  assert.deepEqual(
    resolveFold({}, { messages: [{ role: 'assistant', content: [] }] }, { session: fakeSession([]) }, policy),
    { kind: 'active', anchorText: '', carry: [], mechanical: false, startSeq: undefined, endSeq: undefined, estimatedTokens: 0, spanChars: 0 },
  )
})

test('describeFold reports the request a span belongs to and the requests it releases', () => {
  const nodes = describeSurface(fakeSession([request(1, 'one'), assistantCall(2), toolResult(3), request(4, 'four')]))
  assert.deepEqual(
    describeFold(nodes, { kind: 'active', anchorIndex: 3, released: [], startIndex: 1, endIndex: 2, estimatedTokens: 420, chars: 1470, mechanical: false }),
    {
      kind: 'active', anchor: { seq: 4, text: 'four', key: 'four' }, released: [], mechanical: false, digestClip: 0,
      startSeq: 2, endSeq: 3, estimatedTokens: 420, spanChars: 1470,
    },
  )
  // A legacy anchor is a harness notice: there is no request to quote.
  assert.deepEqual(
    describeFold(nodes, { kind: 'active', anchorIndex: -1, released: [0], startIndex: 1, endIndex: 2, mechanical: true }),
    {
      kind: 'active', anchor: null, released: [{ seq: 1, text: 'one', key: 'one' }], mechanical: true, digestClip: 0,
      startSeq: 2, endSeq: 3, estimatedTokens: 0, spanChars: 0,
    },
  )
})

test('registerRequests keeps the newest requests within the character budget', () => {
  const state = { requests: [] }
  registerRequests(state, [
    { text: 'oldest request', key: 'oldest request' },
    { text: 'newer request', key: 'newer request' },
  ], { instructionChars: 100 })
  assert.deepEqual(state.requests.map((item) => item.text), ['newer request', 'oldest request'], 'newest first')
  // A later release adds one new request; a retry of an old one adds nothing twice.
  registerRequests(state, [
    { text: 'oldest request', key: 'oldest request' },
    { text: 'y'.repeat(75), key: 'y' },
  ], { instructionChars: 100 })
  assert.deepEqual(state.requests.map((item) => item.key), ['y', 'newer request'], 'the oldest request is dropped first')
  const capped = { requests: [] }
  registerRequests(capped, [{ text: 'z'.repeat(500), key: 'z' }], { instructionChars: 100 })
  assert.equal(capped.requests.length, 1, 'a single long request is trimmed, not dropped')
  assert.ok(capped.requests[0].text.length <= 100)
  const off = { requests: [] }
  registerRequests(off, [{ text: 'kept?', key: 'kept?' }], { instructionChars: 0 })
  assert.equal(off.requests.length, 0, 'instructionChars 0 disables the carry')
})

test('the carried list survives a restart: it is read back from the archive checkpoint', async () => {
  const archiveText = renderRequests([{ text: SECOND }, { text: FIRST }])
  // After a restart the plugin has no memory of the session; the surface does.
  const session = fakeSession([
    checkpoint(1, `${archiveText}\n\nOUTCOME OF TURNS ONE AND TWO`), request(2, 'THIRD REQUEST'),
    request(3, 'FOURTH REQUEST'), assistantCall(4), toolResult(5),
  ])
  const engine = { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) }
  const ctx = fakeCtx()
  ctx.provide('compaction', engine)
  apply(ctx, { ...PRESET, maxPinnedInstructions: 1 })
  const region = [1, 2, 3, 4, 5].map((seq) => {
    const event = session.eventAt(seq)
    return event.type === 'user/message' ? event.data : event.data.message
  })
  // The backend folds the old archive together with the third request.
  const result = await engine.summarize({ messages: region }, { session })
  const text = result.summary[0].text
  const quoted = parseRequests(text).map((item) => item.text)
  assert.deepEqual(quoted, ['FOURTH REQUEST', 'THIRD REQUEST', SECOND, FIRST], 'newest first, nothing lost')
  assert.match(text, /<request 4>\nFOURTH REQUEST\n<\/request 4>/, 'and oldest first on the surface')
})

test('renderRequests lists requests oldest first between machine markers', () => {
  const text = renderRequests([{ text: 'second' }, { text: 'first' }])
  assert.match(text, /^\[SAC EARLIER USER REQUESTS\]\n/)
  assert.match(text, /\n\[\/SAC EARLIER USER REQUESTS\]$/)
  assert.ok(text.indexOf('<request 1>\nfirst\n</request 1>') < text.indexOf('<request 2>\nsecond\n</request 2>'))
  assert.equal(renderRequests([]), '')
})

test('the paper template takes the objective from the request and invents no output contract', () => {
  const anchored = paperStatePrompt({ anchored: true, maxWords: 400 })
  assert.match(anchored, /1\. Objective - one line: the result the user asks for in that first message\./)
  assert.match(anchored, /never from whatever the last actions happened to be doing/)
  assert.match(anchored, /write "\(none\)" and do not invent one/)
  assert.match(anchored, /under 400 words/)
  assert.match(PAPER_STATE_PROMPT, /most recently asked for/)
  assert.match(paperStatePrompt({ attempt: 1 }), /Output the recap now/)
  assert.match(ARCHIVE_STATE_PROMPT, /Do not restate the requests as goals/)
  assert.doesNotMatch(ARCHIVE_STATE_PROMPT, /Objective/)
})

test('the checklist is planned from the first request, not from a harness notice', async () => {
  const ctx = fakeCtx()
  ctx.provide('compaction', { compactRegion: async () => {}, summarize: async () => ({ summary: [] }) })
  apply(ctx, { ...PRESET, checklist: true })
  await ctx.dispatch('agent/pre-step', {
    agent: { session: fakeSession([notice(1), request(2, 'do the thing')]) },
    signal: signal(),
  })
  const planned = firstText(ctx.streamed[0])
  assert.match(planned, /TASK:\ndo the thing/)
  assert.doesNotMatch(planned, /snapshot/)
})
