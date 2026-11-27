import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  apply,
  assembleText,
  describeSurface,
  instructionPending,
  framingTokens,
  mechanicalDigest,
  omitStandingNotices,
  paperStatePrompt,
  PAPER_STATE_PROMPT,
  surfaceHasReasoning,
  recapBudget,
  recapStopsMidSentence,
  shortenResult,
  stripCheckpointFraming,
  telemetrySnapshot,
  recapWordLimit,
} from '../index.js'
import {
  effectiveMarks,
  instructionKey,
  mechanicalDigestEstimate,
  planSacSpan,
  planSegments,
  recapExpectation,
  resolvePolicy,
  SAC_DEFAULTS,
  surfaceTokensOf,
} from '../lib/sac-policy.js'

/* -------------------------------------------------------------------------- */
/* stubs (same shape as multiturn-integration.test.js)                         */
/* -------------------------------------------------------------------------- */

function fakeCtx() {
  const listeners = new Map()
  const services = new Map()
  const ctx = {
    logger: { info() {}, warn() {}, error() {} },
    streamed: [],
    on(name, handler) {
      if (!listeners.has(name)) listeners.set(name, [])
      listeners.get(name).push(handler)
      return () => {}
    },
    get: (name) => services.get(name),
    provide: (name, value) => services.set(name, value),
    async dispatch(name, payload) {
      const chain = listeners.get(name) ?? []
      let index = 0
      const next = async () => (index >= chain.length ? 'done' : chain[index++](payload, next))
      return next()
    },
  }
  /** Answers every request with `replies.shift()`; a reply is `{ text, finish? }`. */
  ctx.setReplies = (replies) => {
    ctx.llm = {
      async *stream(options) {
        ctx.streamed.push(options)
        const reply = replies.length > 1 ? replies.shift() : replies[0]
        if (reply.text.length > 0) yield { type: 'text-delta', text: reply.text }
        yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 5 } }
        yield { type: 'finish', reason: { kind: reply.finish ?? 'stop' } }
      },
    }
  }
  return ctx
}

function fakeSession(events) {
  const map = new Map(events.map((event) => [event.seq, event]))
  let next = Math.max(0, ...events.map((event) => event.seq)) + 100
  return {
    id: 'session-adaptive',
    surface: { nodes: events.map((event) => event.seq), replaceGeneration: 1 },
    eventAt: (seq) => map.get(seq),
    requestHeader: () => ({ config: { provider: 'agent-provider', model: 'agent-model' }, tools: [{ name: 'read' }] }),
    toolHistory: () => [],
    push(event) {
      map.set(event.seq, event)
      this.surface.nodes.push(event.seq)
    },
    appendCheckpoint(content) {
      const seq = (next += 1)
      map.set(seq, { seq, type: 'user/message', data: { role: 'user', id: `ck-${seq}`, source: { kind: 'compact-checkpoint' }, content } })
      return seq
    },
  }
}

const request = (seq, text, kind = 'user') => ({
  seq, type: 'user/message', data: { role: 'user', id: `u-${seq}`, source: { kind }, content: [{ type: 'text', text }] },
})
const call = (seq, command = 'ls -la', name = 'bash') => ({
  seq, type: 'assistant/message',
  data: { message: { role: 'assistant', content: [{ type: 'tool-call', id: `call-${seq}`, name, arguments: { command } }] } },
})
const result = (seq, text, id) => ({
  seq, type: 'tool/result', data: { message: { role: 'tool', toolCallId: id ?? `call-${seq - 1}`, content: [{ type: 'text', text }] } },
})
const reply = (seq, text = 'Done. The answer is 42.') => ({
  seq, type: 'assistant/message', data: { message: { role: 'assistant', content: [{ type: 'text', text }] } },
})
const checkpoint = (seq, text) => ({
  seq, type: 'user/message',
  data: { role: 'user', id: `ck-${seq}`, source: { kind: 'compact-checkpoint' }, content: [{ type: 'text', text }] },
})

/** Backend stub: compactRegion → summarize hook → surface replace, as dsh-compaction-basic does. */
function fakeBackend() {
  return {
    calls: [],
    async summarize() {
      return { summary: [{ type: 'text', text: 'base summary' }] }
    },
    async compactRegion(start, end, agent) {
      const nodes = agent.session.surface.nodes
      const first = nodes.indexOf(start)
      const last = nodes.lastIndexOf(endSeqSafe(nodes, end))
      this.calls.push({ start, end })
      const region = nodes.slice(first, last + 1).map((seq) => {
        const event = agent.session.eventAt(seq)
        return event.type === 'user/message' ? event.data : event.data.message
      })
      const summary = await this.summarize({ messages: region }, agent, undefined)
      nodes.splice(first, last - first + 1, agent.session.appendCheckpoint(summary.summary))
      return { shadowedRange: { start, end } }
    },
  }
}
function endSeqSafe(nodes, end) {
  return nodes.includes(end) ? end : nodes.at(-1)
}

const signal = () => new AbortController().signal
const lastText = (options) => options.messages.at(-1).content[0].text
/** Text of the first checkpoint on the surface (the fold's replacement). */
const checkpointText = (session) => {
  for (const seq of session.surface.nodes) {
    const event = session.eventAt(seq)
    if (event?.data?.source?.kind === 'compact-checkpoint') return event.data.content[0].text
  }
  return undefined
}

/** The shipped bundle config (see cordis.patch.yml). */
const BUNDLE = {
  actionsPerCompaction: 2, minSpanChars: 1200, minSpanTokens: 400, statePrompt: 'paper', stateMaxTokens: 2048,
  stateBudget: 2048, adaptiveBudget: true, keepLastObservation: true, trimObservationOver: 4000, stateMaxWords: 400,
  stateInputLean: true, contextBudgetTokens: 96000, pinInstructions: true, maxPinnedInstructions: 4, instructionChars: 4000,
}
/** Stub-friendly: the character guards off, everything else as shipped. */
const LOOSE = { ...BUNDLE, minSpanChars: 0, minSpanTokens: 0 }

const BIG = 'x'.repeat(6000)

/* -------------------------------------------------------------------------- */
/* policy                                                                     */
/* -------------------------------------------------------------------------- */

test('size trigger: nothing folds under the high-water mark, however many actions pile up', () => {
  const policy = resolvePolicy({ ...BUNDLE, minSpanChars: 0, minSpanTokens: 0, foldTrigger: 'size', foldAboveTokens: 16000 })
  const nodes = [
    { seq: 1, type: 'user/message', sourceKind: 'user', text: 'do it', toolCallCount: 0, textChars: 300 },
  ]
  let seq = 2
  for (let action = 0; action < 12; action += 1) {
    nodes.push({ seq: seq++, type: 'assistant/message', toolCallCount: 1, textChars: 150 })
    nodes.push({ seq: seq++, type: 'tool/result', textChars: 2500 })
  }
  // 12 actions ≈ 9.4K tokens of surface: above nothing, below the 16K mark.
  assert.ok(surfaceTokensOf(nodes, policy) > 9000 && surfaceTokensOf(nodes, policy) < 16000)
  assert.equal(planSacSpan(nodes, policy), null, 'the legacy trigger would have folded after two actions')
  assert.notEqual(planSacSpan(nodes, { ...BUNDLE, minSpanChars: 0, minSpanTokens: 0 }), null, '…while the action trigger folds the same surface')
  // One action is enough once the request is large enough (tool schemas count too).
  const span = planSacSpan(nodes, policy, { requestTokens: 16000 })
  assert.equal(span.kind, 'active')
  assert.equal(span.mechanical, false, 'a large span is still written by a model')
  assert.ok(span.endIndex < nodes.length - 1, 'the newest observation stays raw')
})

test('size trigger: the low-water mark defaults to half the high-water mark and follows it', () => {
  assert.equal(resolvePolicy({ foldAboveTokens: 16000 }).foldBelowTokens, 8000)
  assert.equal(resolvePolicy({ foldAboveTokens: 16000, foldBelowTokens: 12000 }).foldBelowTokens, 12000)
  assert.equal(resolvePolicy({ foldAboveTokens: 16000, foldBelowTokens: 20000 }).foldBelowTokens, 8000, 'never above the mark')
  assert.equal(resolvePolicy({}).foldTrigger, 'actions', 'legacy by default')
  assert.equal(resolvePolicy({ foldTrigger: 'whenever' }).foldTrigger, 'actions')
})

test('gain guard: a span barely larger than the recap it would buy is left alone', () => {
  const policy = resolvePolicy({ ...LOOSE, foldMinGainTokens: 400 })
  const expectation = recapExpectation(policy)
  assert.equal(expectation, 560, '400 words ≈ 560 tokens')
  // A 1-action span of ~700 tokens: the recap (~560) leaves a gain of 140 < 400.
  const small = [
    { seq: 1, type: 'user/message', sourceKind: 'user', text: 'do it', toolCallCount: 0, textChars: 300 },
    { seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 150 },
    { seq: 3, type: 'tool/result', textChars: 2000 },
    { seq: 4, type: 'assistant/message', toolCallCount: 1, textChars: 150 },
    { seq: 5, type: 'tool/result', textChars: 500 },
  ]
  assert.equal(planSacSpan(small, { ...policy, keepLastObservation: false }), null)
  // What this agent's recaps really measured is blended in: smaller recaps make small spans foldable.
  assert.notEqual(planSacSpan(small, { ...policy, keepLastObservation: false }, { expectedRecapTokens: 200 }), null)
  // A span with room for a real gain folds, and the guard is off by default.
  const large = [
    small[0], small[1], { seq: 3, type: 'tool/result', textChars: 12000 }, small[3], small[4],
  ]
  assert.notEqual(planSacSpan(large, { ...policy, keepLastObservation: false }), null)
  assert.notEqual(planSacSpan(small, { ...policy, foldMinGainTokens: 0, keepLastObservation: false }), null)
})

test('mechanical fold: what decides is the digest, not the span; a big digest goes to a model', () => {
  const policy = resolvePolicy({ ...LOOSE, mechanicalFoldMaxTokens: 2500, mechanicalSegments: 'all' })
  const nodes = [
    { seq: 1, type: 'user/message', sourceKind: 'user', text: 'do it', toolCallCount: 0, textChars: 300 },
    { seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 150 },
    { seq: 3, type: 'tool/result', textChars: 4000 },
    { seq: 4, type: 'assistant/message', toolCallCount: 1, textChars: 150 },
    { seq: 5, type: 'tool/result', textChars: 4000 },
    { seq: 6, type: 'assistant/message', toolCallCount: 1, textChars: 150 },
    { seq: 7, type: 'tool/result', textChars: 4000 },
  ]
  const small = planSacSpan(nodes, policy)
  assert.equal(small.mechanical, true, 'two actions with results clipped to 600 chars: a ~180-token digest')
  assert.ok(mechanicalDigestEstimate(nodes, small.startIndex, small.endIndex, policy) < small.estimatedTokens)
  // A digest too large for the harness is a model's job.
  const large = planSacSpan(nodes, { ...policy, mechanicalFoldMaxTokens: 100 })
  assert.equal(large.mechanical, false)
  // The legacy default never folds mechanically.
  assert.equal(planSacSpan(nodes, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false }).mechanical, false)
  // Nothing to gain: results shorter than the clip make the digest as big as the span.
  const tiny = [
    nodes[0], nodes[1], { seq: 3, type: 'tool/result', textChars: 200 }, nodes[3], { seq: 5, type: 'tool/result', textChars: 200 },
    nodes[5], { seq: 7, type: 'tool/result', textChars: 200 },
  ]
  const span = planSacSpan(tiny, { ...policy, mechanicalMinGainTokens: 100000 })
  assert.equal(span.mechanical, false, 'the gain test sends a worthless digest back to the model')
})

test('goal rounds: repeated objectives fold without any work, and only mechanically', () => {
  const goal = (seq, round) => ({
    seq, type: 'user/message', sourceKind: 'goal', toolCallCount: 0, textChars: 2400,
    text: `<goal_round>\nObjective: "ship it"\nRound: ${round}/256\n</goal_round>`,
  })
  const said = (seq) => ({ seq, type: 'assistant/message', toolCallCount: 0, textChars: 600 })
  const nodes = [goal(1, 1), said(2), goal(3, 2), said(4), goal(5, 3)]
  assert.equal(planSacSpan(nodes, LOOSE), null, 'action trigger: turns without tools leave everything on the surface')
  assert.equal(planSacSpan(nodes, { ...LOOSE, mechanicalFoldMaxTokens: 5000, goalDedupe: false }), null)
  const span = planSacSpan(nodes, { ...LOOSE, mechanicalFoldMaxTokens: 5000 })
  assert.deepEqual([nodes[span.startIndex].seq, nodes[span.endIndex].seq], [2, 3], 'the first reply and the middle delivery')
  assert.equal(span.mechanical, true)
  assert.equal(span.actions, 0)
  // The anchor and the newest delivery both stay: the model keeps seeing the
  // objective right before its next action, and at most these two copies of it.
  // With nothing else foldable, the newest delivery is the one deduplicated:
  // the anchor copy stays, so the surface never holds more than two copies.
  const bare = planSacSpan([goal(1, 1), said(2), goal(3, 2)], { ...LOOSE, mechanicalFoldMaxTokens: 5000 })
  assert.deepEqual([bare.startIndex, bare.endIndex], [1, 2])
  // Once real work happened the fold is an ordinary one: the gain guards decide.
  const worked = [goal(1, 1), { seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 150 }, { seq: 3, type: 'tool/result', textChars: 6000 }, goal(4, 2)]
  assert.equal(planSacSpan(worked, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false, foldMinGainTokens: 100000 }), null)
  // …and an ordinary fold cuts exactly as before: the work and the older re-delivery fold together.
  const ordinary = planSacSpan(worked, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false })
  assert.deepEqual([worked[ordinary.startIndex].seq, worked[ordinary.endIndex].seq], [2, 4])
  assert.equal(ordinary.mechanical, false)
})

test('mechanical digest carries the earlier state and one exact line per action', () => {
  const framed = `This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint.\n\n<compacted-summary>\n1. Objective: find the key\n2. Established facts: key is in .env\n</compacted-summary>`
  const digest = mechanicalDigest([
    { role: 'user', source: { kind: 'compact-checkpoint' }, content: [{ type: 'text', text: framed }] },
    { role: 'assistant', content: [{ type: 'tool-call', id: 'c1', name: 'bash', arguments: { command: 'cat .env' } }] },
    { role: 'tool', toolCallId: 'c1', content: [{ type: 'text', text: 'A=1\n'.repeat(400) }] },
    { role: 'assistant', content: [{ type: 'text', text: 'The key is ABC.' }] },
  ], resolvePolicy({ digestResultChars: 200 }))
  assert.match(digest.text, /^1\. Objective: find the key\n2\. Established facts: key is in \.env\n\n\[SAC ACTION DIGEST\]/)
  assert.doesNotMatch(digest.text, /automatically generated checkpoint|<compacted-summary>/, 'no checkpoint inside the checkpoint')
  assert.match(digest.text, /- `bash\(\{"command":"cat \.env"\}\)`\n  → A=1/)
  assert.match(digest.text, /…\[1399 chars clipped\]…  A=1 A=1/, 'a long result keeps head and tail');
  assert.match(digest.text, /- assistant said: The key is ABC\./)
  assert.equal(digest.lines, 2)
  assert.equal(stripCheckpointFraming(framed).startsWith('1. Objective'), true)
})

test('mechanical digest: a repeated objective is noted, not copied, and old digest lines age out', () => {
  const policy = resolvePolicy({ digestMaxChars: 600 })
  const digest = mechanicalDigest([
    { role: 'user', source: { kind: 'goal' }, content: [{ type: 'text', text: '<goal_round>Objective "ship it" Round: 2/256</goal_round>' }] },
    { role: 'assistant', content: [{ type: 'text', text: 'Still working on it.' }] },
  ], policy)
  assert.match(digest.text, /delivered the current request again; nothing new was said/)
  assert.doesNotMatch(digest.text, /ship it/)
  // A subagent's closing message carries deliverables: it is kept like a reply; volatile notices are not.
  const report = `报告已完成。\n## 交付物\n**路径**：\`docs/analysis.md\`（第 ${'x'.repeat(1200)} 行之后的结论）\n## 结论\n- 建议 A`
  const notices = mechanicalDigest([
    { role: 'user', source: { kind: 'time-context' }, content: [{ type: 'text', text: 'T'.repeat(2000) }] },
    { role: 'user', source: { kind: 'subagent-settled' }, content: [{ type: 'text', text: report }] },
  ], resolvePolicy({ digestReplyChars: 4000 }))
  assert.match(notices.text, /notice \(time-context\): T{190}/)
  assert.doesNotMatch(notices.text, /T{400}/)
  assert.match(notices.text, /notice \(subagent-settled\): 报告已完成/)
  assert.match(notices.text, /建议 A\n\[\/SAC ACTION DIGEST\]$/, 'the whole closing message survives')
  // Carried digest lines are dropped oldest first once they outgrow the budget.
  const line = (name, fill) => `- \`${name}()\`\n  → ${fill.repeat(150)}`
  const old = mechanicalDigest([
    {
      role: 'user', source: { kind: 'compact-checkpoint' },
      content: [{ type: 'text', text: `STATE\n\n[SAC ACTION DIGEST]\nEarlier steps.\n${[line('old1', 'a'), line('old2', 'b'), line('old3', 'c'), line('old4', 'd')].join('\n')}\n[/SAC ACTION DIGEST]` }],
    },
    { role: 'assistant', content: [{ type: 'tool-call', id: 'c9', name: 'read', arguments: { file: 'x' } }] },
    { role: 'tool', toolCallId: 'c9', content: [{ type: 'text', text: 'new result' }] },
  ], resolvePolicy({ digestMaxChars: 500 }))
  assert.match(old.text, /^STATE\n\n\[SAC ACTION DIGEST\]/)
  assert.doesNotMatch(old.text, /old1|old2/, 'the oldest lines went first')
  assert.ok(old.text.endsWith('- `read({"file":"x"})`\n  → new result\n[/SAC ACTION DIGEST]'), 'the newest lines survive in order')
})

test('mechanical fold makes no model call and replaces the span through the backend', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: 'NEVER USED' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, { ...LOOSE, mechanicalFoldMaxTokens: 5000, mechanicalSegments: 'all' })
  const session = fakeSession([
    request(1, 'find the api key'), call(2, 'cat .env'), result(3, BIG), call(4, 'ls keys'), result(5, BIG), call(6, 'ls'), result(7, 'ok'),
  ])
  const agent = { session }
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed.length, 0, 'no state-update request')
  assert.equal(backend.calls.length, 1)
  assert.deepEqual([backend.calls[0].start, backend.calls[0].end], [2, 5], 'everything but the newest observation')
  assert.equal(session.surface.nodes.length, 4, 'request + checkpoint + the newest action')
  const text = checkpointText(session)
  assert.match(text, /\[SAC ACTION DIGEST\]/)
  assert.match(text, /- `bash\(\{"command":"cat \.env"\}\)/)
  assert.equal(telemetrySnapshot(agent).mechanicalFolds, 1)
  assert.equal(telemetrySnapshot(agent).summarizeCalls, 1)
})

test('size trigger through the pre-step: folds only above the mark, then down to the low-water mark', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: `1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values\n${BIG}` }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  const session = fakeSession([request(1, 'find the api key')])
  const agent = { session }
  // High-water mark 8K request tokens, low-water mark 6K; no mechanical folds.
  apply(ctx, { ...LOOSE, stateMaxWords: 0, foldTrigger: 'size', foldAboveTokens: 8000, foldBelowTokens: 6000, recapMaxRatio: 0.6 })
  let seq = 2
  const act = async (size) => {
    session.push(call(seq, `step ${seq}`))
    session.push(result(seq + 1, 'y'.repeat(size)))
    seq += 2
    await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  }
  await act(1000) // ≈ 0.3K tokens
  await act(1000)
  await act(9000) // ≈ 3.5K
  await act(9000) // ≈ 6.0K: under the mark
  assert.equal(backend.calls.length, 0, 'nothing folds under the high-water mark')
  await act(9000) // ≈ 8.6K: over it
  assert.equal(backend.calls.length, 1, 'one model-written fold')
  assert.equal(ctx.streamed.length, 1)
  // The recap is bounded by its share of the span: with no word cap configured,
  // the span budget alone sets the word limit.
  const words = Number(/under (\d+) words/.exec(lastText(ctx.streamed[0]))[1])
  assert.ok(words >= 60 && words <= 2300, `word limit ${words} follows the span budget`)
  const telemetry = telemetrySnapshot(agent)
  assert.equal(telemetry.compactions, 1)
  assert.equal(telemetry.mechanicalFolds, 0)
  assert.match(checkpointText(session), /8\. Verbatim values/)
  // ≈ 4.4K tokens now: under the low-water mark, and under the high-water mark
  // on the following step: no further fold.
  await act(500)
  assert.equal(backend.calls.length, 1)
})

test('bounded recap: the word limit and the written recap both stay under a share of the span', async () => {
  const ctx = fakeCtx()
  const longRecap = `1. Objective\n${BIG}\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values`
  ctx.setReplies([{ text: longRecap }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  // Without a token meter the span is priced conservatively: 0.8 × the planner's estimate.
  apply(ctx, { ...LOOSE, mechanicalFoldMaxTokens: 2500, recapMaxRatio: 0.5, actionsPerCompaction: 1, mechanicalSegments: 'all' })
  const session = fakeSession([request(1, 'find the api key'), call(2), result(3, BIG), call(4), result(5, BIG)])
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
  // The span (≈ 1.7K planner tokens → ≈ 1.4K host tokens) is mechanical: it never reaches the model.
  assert.equal(ctx.streamed.length, 0)
  // A larger span goes to the model with a word limit derived from the span.
  const ctx2 = fakeCtx()
  ctx2.setReplies([{ text: longRecap }])
  const backend2 = fakeBackend()
  ctx2.provide('compaction', backend2)
  apply(ctx2, { ...LOOSE, recapMaxRatio: 0.5, actionsPerCompaction: 1, stateMaxWords: 0 })
  // A span of ~2.8K host tokens allows half of it: the 6K-char reply is cut to that share.
  const session2 = fakeSession([request(1, 'find the api key'), call(2), result(3, BIG.repeat(2)), call(4), result(5, BIG.repeat(6))])
  await ctx2.dispatch('agent/pre-step', { agent: { session: session2 }, signal: signal() })
  assert.equal(ctx2.streamed.length, 1)
  assert.match(lastText(ctx2.streamed[0]), /under \d+ words/, 'no stateMaxWords configured: the span budget alone sets one')
  const written = checkpointText(session2)
  assert.match(written, /\[recap cut to \d+ tokens\]/)
  assert.ok(written.length <= 5300, `cut to its share of the span (${written.length} chars)`)
  assert.ok(recapBudget(resolvePolicy({ recapMaxRatio: 0.5 }), 1) < 0, 'a span of one host token cannot carry a recap')
  assert.equal(framingTokens() > 80 && framingTokens() < 160, true)
})

test('a span too small for a bounded recap is left on the surface without a model call', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: 'x' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  const agent = { session: fakeSession([request(1, 'find the api key'), call(2), result(3, 'small'), call(4), result(5, 'small')]) }
  // The guards are off, so the planner proposes the fold; the state update then
  // finds that a ~40-token span cannot hold a bounded recap and skips it.
  apply(ctx, { ...LOOSE, recapMaxRatio: 0.5, actionsPerCompaction: 1, keepLastObservation: false })
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed.length, 0, 'no call for a span that cannot hold a recap')
  assert.equal(agent.session.surface.nodes.length, 5, 'nothing was replaced')
  assert.match(telemetrySnapshot(agent).lastSkipReason, /too small for a bounded recap/)
})

test('cut-off recap: retried once with a doubled cap that stays up for the session', async () => {
  const ctx = fakeCtx()
  const complete = '1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values'
  ctx.setReplies([{ text: '1. Objective: find the key\n2. Established facts: the key is in', finish: 'max-tokens' }, { text: complete }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false })
  const session = fakeSession([request(1, 'find the api key'), call(2), result(3, BIG.repeat(3))])
  const agent = { session }
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed.length, 2, 'the cut-off recap was retried')
  assert.equal(ctx.streamed[0].maxTokens, 2048)
  assert.equal(ctx.streamed[1].maxTokens, 4096, 'doubled')
  assert.match(lastText(ctx.streamed[1]), /cut off/, 'and told why')
  assert.match(checkpointText(session), /8\. Verbatim values/, 'the complete recap is the one kept')
  assert.equal(telemetrySnapshot(agent).stateRetries, 1)
  assert.equal(telemetrySnapshot(agent).truncatedRecaps, 0)
  // The next fold starts from the doubled cap.
  session.push(call(4), result(5, BIG.repeat(3)))
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed.at(-1).maxTokens, 4096, 'sticky')
})

test('cut-off twice: the longer partial recap is kept, cut at its last complete line, and counted', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([
    { text: '1. Objective\nshort', finish: 'max-tokens' },
    { text: `1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n${BIG.slice(0, 500)}\n5. Dead ends: nothing worked, the key`, finish: 'max-tokens' },
  ])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false })
  const session = fakeSession([request(1, 'find the api key'), call(2), result(3, BIG.repeat(3))])
  const agent = { session }
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed.length, 2)
  const written = checkpointText(session)
  assert.match(written, /4\. Verification/, 'the second, longer attempt is kept')
  assert.doesNotMatch(written, /the key$/, 'its unfinished last line is dropped')
  assert.equal(telemetrySnapshot(agent).truncatedRecaps, 1)
})

test('state update can run on another provider/model than the agent', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: '1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, {
    ...LOOSE, stateProvider: 'cheap', stateModel: 'flash', actionsPerCompaction: 1, keepLastObservation: false,
  })
  const session = fakeSession([request(1, 'find the api key'), call(2), result(3, BIG.repeat(3))])
  const agent = { session }
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed[0].provider, 'cheap')
  assert.equal(ctx.streamed[0].model, 'flash')
  assert.equal(telemetrySnapshot(agent).stateRerouted, 1)
  // Default: the agent's own route.
  assert.equal(resolvePolicy({}).stateProvider, '')
})

test('assembleText reports the finish reason; mid-sentence detection is conservative', async () => {
  async function* stream() {
    yield { type: 'text-delta', text: 'abc' }
    yield { type: 'finish', reason: { kind: 'max-tokens' }, usage: { inputTokens: 3, outputTokens: 3 } }
  }
  assert.deepEqual(await assembleText(stream()), { text: 'abc', usage: { inputTokens: 3, outputTokens: 3 }, finish: 'max-tokens' })
  assert.equal(recapStopsMidSentence('short'), false, 'short texts are never judged')
  assert.equal(recapStopsMidSentence(`${BIG} and then the value of`), true)
  assert.equal(recapStopsMidSentence(`${BIG}.`), false)
  assert.equal(recapStopsMidSentence(`${BIG}\n**4. Verification**`), false)
})

test('goal rounds through the pre-step: a repeated objective never piles up, and no model is called', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: 'NEVER USED' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, { ...LOOSE, mechanicalFoldMaxTokens: 5000 })
  const objective = (round) => `<goal_round>\nObjective: "ship the release: ${'follow the checklist, run the tests and write the report. '.repeat(30)}"\nRound: ${round}/256\n\nContinue working.\n</goal_round>`
  const session = fakeSession([
    request(1, objective(1), 'goal'),
    reply(2, 'Round 1 done, still working. '.repeat(20)),
    request(3, objective(2), 'goal'),
    reply(4, 'Round 2 done, still working. '.repeat(20)),
    request(5, objective(3), 'goal'),
  ])
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
  assert.equal(backend.calls.length, 1, 'one digest replaces the two older rounds')
  assert.equal(ctx.streamed.length, 0)
  assert.deepEqual([...session.surface.nodes], [1, session.surface.nodes[1], 4, 5], 'the first and the newest delivery stay verbatim')
  // The anchor, the digest, the latest reply and the newest delivery.
  assert.deepEqual(
    session.surface.nodes.map((seq) => session.eventAt(seq).data.source?.kind ?? session.eventAt(seq).type),
    ['goal', 'compact-checkpoint', 'assistant/message', 'goal'],
  )
  const digest = checkpointText(session)
  assert.match(digest, /Round 1 done, still working/)
  assert.doesNotMatch(digest, /Round 2 done/, 'the latest reply stays raw: it is the trailing reply')
  assert.equal((digest.match(/delivered the current request again/g) ?? []).length, 1, 'the middle delivery was folded')
})

test('legacy configuration keeps its behaviour: one model-written fold per step, no guards', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: '1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, LOOSE)
  const session = fakeSession([
    request(1, 'first'), call(2), result(3, BIG), call(4), result(5, BIG), reply(6), request(7, 'second'), call(8), result(9, BIG), call(10), result(11, BIG),
  ])
  const agent = { session }
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(backend.calls.length, 1, 'exactly one fold per step')
  assert.deepEqual([backend.calls[0].start, backend.calls[0].end], [2, 5], 'the finished turn folds first… as the legacy plan goes')
  assert.equal(ctx.streamed.length, 1)
  assert.equal(telemetrySnapshot(agent).mechanicalFolds, 0)
})

test('a harness digest may free an earlier turn before the model acts on a new request; a model recap waits', () => {
  const nodes = [
    { seq: 1, type: 'user/message', sourceKind: 'user', text: 'first request', toolCallCount: 0, textChars: 300 },
    { seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 150 },
    { seq: 3, type: 'tool/result', textChars: 6000 },
    { seq: 4, type: 'assistant/message', toolCallCount: 0, textChars: 500 },
    { seq: 5, type: 'user/message', sourceKind: 'user', text: 'second request', toolCallCount: 0, textChars: 300 },
  ]
  const inside = (span, seq) => nodes.slice(span.startIndex, span.endIndex + 1).some((node) => node.seq === seq)
  const span = planSacSpan(nodes, { ...LOOSE, mechanicalFoldMaxTokens: 5000 })
  assert.deepEqual([nodes[span.startIndex].seq, nodes[span.endIndex].seq], [2, 3], "the finished turn's action folds right away")
  assert.equal(span.mechanical, true)
  assert.equal(inside(span, 4), false, 'its final reply stays')
  // Without mechanical folds the legacy rule holds: nothing folds before the first action.
  assert.equal(planSacSpan(nodes, LOOSE), null)
  // Once the model has acted, a model-written recap may fold the earlier turn.
  const acted = [...nodes, { seq: 6, type: 'assistant/message', toolCallCount: 1, textChars: 150 }, { seq: 7, type: 'tool/result', textChars: 6000 }]
  const late = planSacSpan(acted, LOOSE)
  assert.deepEqual([acted[late.startIndex].seq, acted[late.endIndex].seq], [2, 3])
  assert.equal(late.mechanical, false)
})

test('mechanical digest estimate ignores reasoning: thinking is the cheapest thing a fold removes', () => {
  const thinking = (seq, id, command) => ({
    seq, type: 'assistant/message',
    data: { message: { role: 'assistant', content: [
      { type: 'reasoning', text: 't'.repeat(9000) },
      { type: 'tool-call', id, name: 'bash', arguments: { command } },
    ] } },
  })
  const session = fakeSession([
    request(1, 'find the api key'), thinking(2, 'c2', 'cat .env'), result(3, 'KEY=abc'), thinking(4, 'c4', 'ls'), result(5, 'a b c'),
  ])
  const nodes = describeSurface(session)
  assert.equal(nodes[1].reasoningChars, 9000, 'reasoning is part of the surface: providers replay it')
  assert.equal(nodes[1].argsChars, JSON.stringify({ command: 'cat .env' }).length)
  const policy = resolvePolicy({ ...LOOSE, actionsPerCompaction: 1, mechanicalFoldMaxTokens: 5000, mechanicalSegments: 'all' })
  const span = planSacSpan(nodes, policy)
  assert.deepEqual([span.startIndex, span.endIndex], [1, 2], 'the newest observation stays raw')
  assert.ok(span.estimatedTokens > 2500, `the span is large (${span.estimatedTokens} tokens)`)
  assert.equal(span.mechanical, true, '…but its digest keeps only the call and the result')
  assert.ok(mechanicalDigestEstimate(nodes, 1, 2, policy) < 120, 'the reasoning leaves for free')
  const digest = mechanicalDigest([session.eventAt(2).data.message, session.eventAt(3).data.message], policy)
  assert.doesNotMatch(digest.text, /tttt/, 'no reasoning in the digest')
  assert.match(digest.text, /- `bash\(\{"command":"cat \.env"\}\)`\n  → KEY=abc/)
})

test('a finished turn\'s final reply with heavy reasoning folds mechanically; its visible text stays', () => {
  const heavy = (seq, text, reasoningChars) => ({
    seq, type: 'assistant/message', toolCallCount: 0, textChars: text.length + reasoningChars,
    reasoningChars, argsChars: 0, plainChars: text.length, text,
  })
  const callNode = (seq) => ({ seq, type: 'assistant/message', toolCallCount: 1, textChars: 150, reasoningChars: 0, argsChars: 150, plainChars: 0 })
  const resultNode = (seq, textChars = 900) => ({ seq, type: 'tool/result', textChars })
  const task = (seq, text) => ({ seq, type: 'user/message', sourceKind: 'user', text, toolCallCount: 0, textChars: text.length })
  const answer = 'Here is the refund summary: '.repeat(12) // 324 chars of user-visible text
  const nodes = [
    task(1, 'first request'), callNode(2), resultNode(3), heavy(4, answer, 40000),
    task(5, 'second request'), callNode(6), resultNode(7),
  ]
  const policy = resolvePolicy({ ...LOOSE, mechanicalFoldMaxTokens: 5000, replyReasoningFoldChars: 1500 })
  const span = planSacSpan(nodes, policy)
  assert.deepEqual([nodes[span.startIndex].seq, nodes[span.endIndex].seq], [2, 4], 'the leftover action and the heavy final reply')
  assert.equal(span.mechanical, true)
  assert.equal(span.replyFolded, true)
  assert.ok(span.estimatedTokens > 10000, `the span is large (${span.estimatedTokens} tokens)…`)
  assert.ok(mechanicalDigestEstimate(nodes, 1, 3, policy) < 400, '…but its digest is small: only the call, the result and the reply text')
  // A light reply stays raw, as before.
  const light = [nodes[0], nodes[1], nodes[2], heavy(4, answer, 600), nodes[4], nodes[5], nodes[6]]
  const kept = planSacSpan(light, policy)
  assert.deepEqual([light[kept.startIndex].seq, light[kept.endIndex].seq], [2, 3])
  assert.equal(kept.replyFolded, false)
  // Off by default.
  assert.equal(planSacSpan(nodes, { ...LOOSE, mechanicalFoldMaxTokens: 5000 }).replyFolded, false)
  // The digest quotes the reply (well beyond a tool result's clip) and carries no reasoning.
  const digest = mechanicalDigest([
    { role: 'assistant', content: [{ type: 'reasoning', text: 'r'.repeat(40000) }, { type: 'text', text: answer }] },
  ], policy)
  assert.ok(digest.text.includes(answer.trim()), 'the whole reply text is kept')
  assert.doesNotMatch(digest.text, /rrrr/)
})

test('a thinking agent starts the state update from the larger cap (no cut-off, no retry on the first fold)', async () => {
  const ctx = fakeCtx()
  // The model would be cut off at 2048: the single reply is what a 4096 cap returns.
  ctx.setReplies([{ text: '1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  const thinking = (seq, id) => ({
    seq, type: 'assistant/message',
    data: { message: { role: 'assistant', content: [{ type: 'reasoning', text: 'let me think' }, { type: 'tool-call', id, name: 'bash', arguments: { command: 'ls' } }] } },
  })
  const session = fakeSession([request(1, 'find the api key'), thinking(2, 'c2'), result(3, BIG.repeat(3)), call(4), result(5, BIG.repeat(3))])
  assert.equal(surfaceHasReasoning({ session }), true)
  assert.equal(surfaceHasReasoning({ session: fakeSession([request(1, 'x'), call(2), result(3)]) }), false)
  apply(ctx, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false, stateMaxTokensThinking: 4096 })
  const agent = { session }
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(ctx.streamed.length, 1, 'one call')
  assert.equal(ctx.streamed[0].maxTokens, 4096, 'the larger cap from the start')
  assert.equal(telemetrySnapshot(agent).stateRetries, 0)
  assert.equal(telemetrySnapshot(agent).truncatedRecaps, 0)
})

test('a structured tool result is compacted, not cut: keys and ids of the kept items stay exact', () => {
  const flights = Array.from({ length: 40 }, (_, i) => ({ flight_number: `HAT${String(i).padStart(3, '0')}`, price: 100 + i, note: 'x'.repeat(300) }))
  const pretty = JSON.stringify({ flights, next_page: null }, null, 2)
  const line = shortenResult(pretty, 600)
  assert.ok(line.length <= 620, `fits the budget (${line.length})`)
  assert.match(line, /^\{"flights":\[/, 'compact JSON, same structure')
  assert.match(line, /"flight_number":"HAT000","price":100/, 'first item exact')
  assert.match(line, /…\[\d+ more\]…/, 'later items are counted, not mangled')
  assert.match(line, /"next_page":null/, 'sibling keys survive')
  assert.doesNotMatch(line, /…\[\d+ chars clipped\]…/, 'no head/tail cut through the text')
  // A small result is kept whole (just without pretty-printing); plain text is clipped head and tail.
  assert.equal(shortenResult('{\n  "status": "available"\n}', 600), '{"status":"available"}')
  assert.match(shortenResult('line one\n' + 'x'.repeat(2000) + '\nlast line', 300), /line one .* chars clipped\]… .*last line$/)
})

test('by default the harness folds only finished turns itself; the working state gets a model recap', () => {
  const callNode = (seq) => ({ seq, type: 'assistant/message', toolCallCount: 1, textChars: 150 })
  const resultNode = (seq, textChars = 900) => ({ seq, type: 'tool/result', textChars })
  const task = (seq, text) => ({ seq, type: 'user/message', sourceKind: 'user', text, toolCallCount: 0, textChars: text.length })
  const reply = (seq) => ({ seq, type: 'assistant/message', toolCallCount: 0, textChars: 400 })
  const working = [task(1, 'first'), callNode(2), resultNode(3), callNode(4), resultNode(5), callNode(6), resultNode(7)]
  const policy = resolvePolicy({ ...LOOSE, mechanicalFoldMaxTokens: 4000 })
  assert.equal(resolvePolicy({}).mechanicalSegments, 'frozen')
  const active = planSacSpan(working, policy)
  assert.equal(active.kind, 'active')
  assert.equal(active.mechanical, false, 'the working state is written by a model (as in 1.2.0)')
  // The same actions behind a second request are a finished turn: a digest takes them.
  const finished = [working[0], working[1], working[2], working[3], working[4], reply(8), task(9, 'second'), callNode(10), resultNode(11)]
  const frozen = planSacSpan(finished, policy)
  assert.equal(frozen.kind, 'frozen')
  assert.equal(frozen.mechanical, true)
})

test('with a span-relative bound the early-fold budget no longer cuts a complete recap', async () => {
  const ctx = fakeCtx()
  const complete = `1. Objective\n2. Established facts\n- ${'fact '.repeat(300)}\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values`
  ctx.setReplies([{ text: complete }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  // The adaptive budget would allow ~640 tokens (2,240 chars) on a first fold; the span allows 60% of ~11K.
  apply(ctx, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false, recapMaxRatio: 0.6 })
  const session = fakeSession([request(1, 'find the api key'), call(2), result(3, BIG.repeat(6)), call(4), result(5, BIG.repeat(6))])
  return (async () => {
    await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
    const written = checkpointText(session)
    assert.doesNotMatch(written, /recap cut/, 'a complete recap under the word limit is kept whole')
    assert.match(written, /8\. Verbatim values/)
  })()
})

test('margin-based marks follow the fixed part of the prompt', async () => {
  const margin = resolvePolicy({ foldTrigger: 'size', foldMarginTokens: 12000 })
  assert.deepEqual(effectiveMarks(margin, 5000), { above: 17000, below: 11000 })
  assert.deepEqual(effectiveMarks(margin, 20000), { above: 32000, below: 26000 }, 'a 20K coding-agent prompt folds later')
  assert.deepEqual(effectiveMarks(resolvePolicy({ foldTrigger: 'size', foldAboveTokens: 16000 }), 20000), { above: 16000, below: 8000 }, 'an explicit mark wins')
  assert.deepEqual(effectiveMarks(resolvePolicy({ foldTrigger: 'size' }), 5000), { above: 0, below: 0 }, 'no mark: never by size')
  // Through the pre-step: the fixed part moves the trigger, the conversation alone decides.
  const ctx = fakeCtx()
  ctx.setReplies([{ text: 'NEVER CALLED' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, { ...LOOSE, foldTrigger: 'size', foldMarginTokens: 600, mechanicalFoldMaxTokens: 4000 })
  const system = (chars) => ({ seq: 1, type: 'system/message', data: { role: 'system', content: [{ type: 'text', text: 's'.repeat(chars) }] } })
  const run = async (systemChars, body) => {
    const session = fakeSession([system(systemChars), ...body])
    session.requestHeader = () => ({ config: { provider: 'p', model: 'm' } })
    backend.calls.length = 0
    await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
    return backend.calls.length
  }
  const heavy = [request(2, 'first request'), call(3), result(4, 'r'.repeat(14000)), reply(5, 'Done.'), request(6, 'second request'), call(7), result(8, 'ok')]
  const light = [request(2, 'first request'), call(3), result(4, 'ok'), reply(5, 'Done.'), request(6, 'second request'), call(7), result(8, 'ok')]
  assert.ok(await run(3500, heavy) >= 1, 'a 4K-token conversation folds under a 1K system prompt')
  assert.ok(await run(35000, heavy) >= 1, '…and under a 10K system prompt: the margin is over the fixed part')
  assert.equal(await run(3500, light), 0, 'a tiny conversation never folds')
  assert.equal(await run(140000, light), 0, '…not even when the system prompt alone is 40K tokens')
})


test('workspace instructions are left out of the state-update input (the harness re-supplies them)', async () => {
  const ctx = fakeCtx()
  ctx.setReplies([{ text: '1. Objective\n2. Established facts\n3. Applied changes\n4. Verification\n5. Dead ends\n6. Remaining plan\n7. Output contract\n8. Verbatim values' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, { ...LOOSE, actionsPerCompaction: 1, keepLastObservation: false })
  const agents = { seq: 1, type: 'user/message', data: { role: 'user', id: 'a1', source: { kind: 'agent-instructions' }, content: [{ type: 'text', text: 'You are the travel expense audit agent. '.repeat(200) }] } }
  const session = fakeSession([request(2, 'audit the claims'), call(3), result(4, BIG.repeat(3)), agents, call(5), result(6, BIG.repeat(3))])
  await ctx.dispatch('agent/pre-step', { agent: { session }, signal: signal() })
  const sent = JSON.stringify(ctx.streamed[0].messages)
  assert.doesNotMatch(sent, /travel expense audit agent/, 'the AGENTS.md text is not sent to the summarizer')
  assert.match(sent, /workspace instructions the harness delivered here are left out/)
  assert.deepEqual(omitStandingNotices([{ role: 'user', content: [] }], resolvePolicy({})), [{ role: 'user', content: [] }])
  assert.match(checkpointText(session), /8\. Verbatim values/)
})


test('a waiting user instruction ends the turn: the finished turn folds as a digest before the first step', async () => {
  const heavy = (seq, text, reasoningChars) => ({ seq, type: 'assistant/message', data: { message: { role: 'assistant', content: [
    { type: 'reasoning', text: 'r'.repeat(reasoningChars) }, { type: 'text', text },
  ] } } })
  const policy = { ...LOOSE, mechanicalFoldMaxTokens: 4000, replyReasoningFoldChars: 1500 }
  // At the last pre-step of a turn the next request is not on the surface yet.
  const open = fakeSession([request(1, 'first request'), call(2, 'ls keys'), result(3, 'k1 k2'), heavy(4, 'Found two keys.', 40000)])
  assert.equal(planSacSpan(describeSurface(open), resolvePolicy(policy), { turnEnding: false }), null,
    'while the turn is open the working state keeps its reply (and one action is below the trigger)')
  const ended = planSacSpan(describeSurface(open), resolvePolicy(policy), { turnEnding: true })
  assert.equal(ended.kind, 'frozen')
  assert.equal(ended.mechanical, true)
  assert.deepEqual([ended.startIndex, ended.endIndex], [1, 3], 'the action and the heavy reply fold')
  // …and a turn that holds nothing but the heavy reply folds that reply alone.
  const bare = fakeSession([request(1, 'first request'), heavy(2, 'Done.', 40000)])
  const lone = planSacSpan(describeSurface(bare), resolvePolicy(policy), { turnEnding: true })
  assert.equal(lone.mechanical, true)
  assert.deepEqual([lone.startIndex, lone.endIndex], [1, 1])
  const withRequest = fakeSession([request(1, 'first request'), call(2, 'ls keys'), result(3, 'k1 k2'), heavy(4, 'Found two keys.', 40000),
    request(5, 'second request')])
  assert.deepEqual(
    [planSacSpan(describeSurface(withRequest), resolvePolicy(policy)).startIndex, planSacSpan(describeSurface(withRequest), resolvePolicy(policy)).endIndex],
    [1, 3], 'once the request is on the surface the same fold is an ordinary finished-turn digest')
  // Through the pre-step: the inbox holds the next request.
  const ctx = fakeCtx()
  ctx.setReplies([{ text: 'NEVER CALLED' }])
  const backend = fakeBackend()
  ctx.provide('compaction', backend)
  apply(ctx, policy)
  const agent = { session: open, inbox: { nextStep: [{ id: 'u9', role: 'user', source: { kind: 'user' }, content: [] }] } }
  assert.equal(instructionPending(agent), true)
  assert.equal(instructionPending({ session: open, inbox: { nextStep: [{ source: { kind: 'tool-jobs' } }] } }), false, 'a harness notice does not end the turn')
  assert.equal(instructionPending({ session: open }), false)
  await ctx.dispatch('agent/pre-step', { agent, signal: signal() })
  assert.equal(backend.calls.length, 1)
  assert.equal(ctx.streamed.length, 0, 'a digest, no model call')
  assert.match(checkpointText(open), /Found two keys\./)
})


test('a goal round is recognised by its envelope even when it reaches the surface as a typed prompt', () => {
  const round = (seq, n) => ({ seq, type: 'user/message', sourceKind: 'user', toolCallCount: 0, textChars: 600,
    text: `<goal_round>\nObjective: "ship it"\nRound: ${n}/256\n\nContinue working.\n</goal_round>` })
  assert.equal(instructionKey(round(1, 1)), instructionKey(round(3, 9)), 'the same objective: the round line is ignored')
  const nodes = [round(1, 1), { seq: 2, type: 'assistant/message', toolCallCount: 0, textChars: 400 }, round(3, 2)]
  const layout = planSegments(nodes, LOOSE)
  assert.deepEqual(layout.redelivered, [2], 'the second delivery is a re-delivery, not a new anchor')
})


test('the recap word limit grows with the span under a span-relative bound, and is unchanged without it', () => {
  // 1.2.0 contract: the smaller of stateMaxWords (400) and the adaptive budget (640 tokens ≈ 480 words).
  const legacy = resolvePolicy({ stateMaxWords: 400 })
  assert.equal(recapWordLimit(legacy, 8900, 640, 0), 400)
  assert.equal(recapWordLimit(legacy, 8900, 300, 0), Math.floor(300 * 0.75))
  assert.equal(recapWordLimit(legacy, 8900, 640, 1), 240)
  // 1.3.0: a 1,200-token span allows half the span in words (450) …
  const bounded = resolvePolicy({ stateMaxWords: 400, recapMaxRatio: 0.6 })
  assert.equal(recapWordLimit(bounded, 1200, 720, 0), 450)
  // … a large span never more than four times stateMaxWords (1,600), well inside its budget …
  assert.equal(recapWordLimit(bounded, 8900, 5340, 0), 1600)
  // … and the budget is still the ceiling when the span is small (420 tokens ≈ 315 words).
  assert.equal(recapWordLimit(bounded, 700, 420, 0), Math.floor(420 * 0.75))
  assert.equal(recapWordLimit(bounded, 8900, 900, 0), Math.floor(900 * 0.75))
  // A retry keeps the contract under the bound (the output cap doubles instead).
  assert.equal(recapWordLimit(bounded, 8900, 5340, 1), 1600)
  // The prompt then asks for carried values in full; the 1.2.0 prompt is unchanged.
  assert.match(paperStatePrompt({ carryFacts: true }), /carry its established facts and verbatim\nvalues forward in full/)
  assert.doesNotMatch(PAPER_STATE_PROMPT, /carry its established facts/)
  // No word contract at all when neither is configured.
  assert.equal(recapWordLimit(resolvePolicy({ stateMaxWords: 0 }), 8900, 0, 0), 0)
})


test('digest shrink to budget: a finished turn with many results is digested with a smaller share per result', () => {
  const nodes = [{ seq: 1, type: 'user/message', sourceKind: 'user', text: 'read them', toolCallCount: 0, textChars: 300 }]
  let seq = 2
  for (let i = 0; i < 12; i += 1) {
    nodes.push({ seq: seq++, type: 'assistant/message', toolCallCount: 1, textChars: 150 })
    nodes.push({ seq: seq++, type: 'tool/result', textChars: 10000 })
  }
  nodes.push({ seq: seq++, type: 'user/message', sourceKind: 'user', text: 'next: answer', toolCallCount: 0, textChars: 300 })
  const base = { ...LOOSE, mechanicalFoldMaxTokens: 4000, digestResultChars: 2000 }
  // Twelve results at the full clip outgrow the budget: without shrinking, the
  // harness does not digest this turn (the fold is left to the model or not made) …
  const plain = planSacSpan(nodes, base, { turnEnding: true })
  assert.notEqual(plain?.mechanical, true)
  assert.equal(plain?.digestClip ?? 0, 0)
  // … while the head of every result is exactly what the next turn needs.
  const shrunk = planSacSpan(nodes, { ...base, digestShrinkToBudget: true }, { turnEnding: true })
  assert.equal(shrunk.mechanical, true)
  assert.ok(shrunk.digestClip > 120 && shrunk.digestClip < 2000, `clip ${shrunk.digestClip}`)
  assert.ok(mechanicalDigestEstimate(nodes, shrunk.startIndex, shrunk.endIndex, { ...base, digestResultChars: shrunk.digestClip }) <= 4000)
  // The runtime writes the digest with that clip: every result keeps its head, the whole fits the budget.
  const messages = []
  for (let i = 0; i < 12; i += 1) {
    messages.push({ role: 'assistant', content: [{ type: 'tool-call', id: `c${i}`, name: 'read', arguments: { file_path: `rec_${i}.txt` } }] })
    messages.push({ role: 'tool', toolCallId: `c${i}`, content: [{ type: 'text', text: `record ${i}\ninvoice_id: INV-${200000 + i}\namount: ${i}.00\npriority_flag: yes\n${'noise '.repeat(2000)}` }] })
  }
  const digest = mechanicalDigest(messages, { ...base, digestShrinkToBudget: true, digestResultChars: shrunk.digestClip })
  assert.equal(digest.lines, 12)
  for (let i = 0; i < 12; i += 1) assert.match(digest.text, new RegExp(`INV-${200000 + i}`))
  assert.ok(digest.text.length <= 4000 * 3.5 + 400, `${digest.text.length} chars`)
  // A budget no clip can satisfy still declines the mechanical fold.
  const tiny = planSacSpan(nodes, { ...base, digestShrinkToBudget: true, mechanicalFoldMaxTokens: 300 }, { turnEnding: true })
  assert.notEqual(tiny?.mechanical, true)
})
