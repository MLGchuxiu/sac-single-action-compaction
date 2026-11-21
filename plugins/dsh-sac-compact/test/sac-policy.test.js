import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  CHECKPOINT_SOURCE_KIND,
  SAC_DEFAULTS,
  findTaskAnchorIndex,
  planSacSpan,
  resolvePolicy,
  withinTurnBudget,
} from '../lib/sac-policy.js'
import { assembleText, describeSurface } from '../index.js'

const big = (n = 4000) => n

/** Build a surface descriptor list: task, then action pairs. */
function surfaceOf(steps, { checkpoint = false, trailingReply = false } = {}) {
  const nodes = [{ seq: 1, type: 'user/message', isCheckpoint: false, toolCallCount: 0, textChars: 900 }]
  let seq = 2
  if (checkpoint) nodes.push({ seq: seq++, type: 'user/message', isCheckpoint: true, toolCallCount: 0, textChars: 800 })
  for (let index = 0; index < steps; index += 1) {
    nodes.push({ seq: seq++, type: 'assistant/message', toolCallCount: 1, textChars: 200 })
    nodes.push({ seq: seq++, type: 'tool/result', toolCallCount: 0, textChars: 2000 })
  }
  if (trailingReply) nodes.push({ seq: seq++, type: 'assistant/message', toolCallCount: 0, textChars: 300 })
  return nodes
}

test('no task anchor means no compaction', () => {
  assert.equal(findTaskAnchorIndex([{ type: 'assistant/message' }]), -1)
  assert.equal(planSacSpan([{ seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 5000 }]), null)
})

test('task-only surface has nothing to fold', () => {
  const nodes = [{ seq: 1, type: 'user/message', isCheckpoint: false, textChars: 900 }]
  assert.equal(planSacSpan(nodes), null)
})

test('single action folds from just after the task to the tool result', () => {
  const nodes = surfaceOf(1)
  const span = planSacSpan(nodes)
  assert.ok(span)
  assert.equal(span.startIndex, 1)
  assert.equal(span.endIndex, 2)
  assert.equal(span.actions, 1)
})

test('two actions fold both pairs in one span', () => {
  const span = planSacSpan(surfaceOf(2))
  assert.equal(span.startIndex, 1)
  assert.equal(span.endIndex, 4)
  assert.equal(span.actions, 2)
})

test('a previous checkpoint is absorbed, not appended', () => {
  const span = planSacSpan(surfaceOf(1, { checkpoint: true }))
  // index 1 is the old checkpoint, index 2/3 the new action
  assert.equal(span.startIndex, 1)
  assert.equal(span.endIndex, 3)
})

test('an open tool call is never folded', () => {
  const nodes = surfaceOf(1)
  nodes.push({ seq: 99, type: 'assistant/message', toolCallCount: 1, textChars: 300 })
  const span = planSacSpan(nodes)
  assert.equal(span.endIndex, 2, 'cut must stay before the unanswered call')
  assert.equal(span.actions, 1)
})

test('actionsPerCompaction=2 waits for the second action', () => {
  const policy = resolvePolicy({ actionsPerCompaction: 2, minSpanChars: 0 })
  assert.equal(planSacSpan(surfaceOf(1), policy), null)
  assert.equal(planSacSpan(surfaceOf(2), policy).actions, 2)
})

test('minSpanChars guards against pointless folds', () => {
  const policy = resolvePolicy({ minSpanChars: 1_000_000 })
  assert.equal(planSacSpan(surfaceOf(1), policy), null)
})

test('a trailing assistant reply is kept out of the span', () => {
  const nodes = surfaceOf(1, { trailingReply: true })
  const span = planSacSpan(nodes)
  assert.equal(span.endIndex, 2)
  const keep = planSacSpan(nodes, resolvePolicy({ keepTrailingReply: false }))
  assert.equal(keep.endIndex, 3)
})

test('config sanitization clamps hostile values', () => {
  const policy = resolvePolicy({ actionsPerCompaction: -5, minSpanChars: -1, charsPerToken: 99, maxCompactionsPerTurn: 0 })
  assert.equal(policy.actionsPerCompaction, 1)
  assert.equal(policy.minSpanChars, 0)
  assert.equal(policy.charsPerToken, 8)
  assert.equal(policy.maxCompactionsPerTurn, 1)
  assert.equal(resolvePolicy({ enabled: false }).enabled, false)
  assert.equal(resolvePolicy({ statePrompt: 'nonsense' }).statePrompt, 'native')
})

test('iter6: new config fields are sanitized', () => {
  const policy = resolvePolicy({ trimObservationOver: -5, contextBudgetTokens: 'nope', inlineFallbackAfter: 99 })
  assert.equal(policy.trimObservationOver, 0)
  assert.equal(policy.contextBudgetTokens, 0)
  assert.equal(policy.inlineFallbackAfter, 10)
  const on = resolvePolicy({ trimObservationOver: 4000, contextBudgetTokens: 96_000, inlineFallbackAfter: 1 })
  assert.equal(on.trimObservationOver, 4000)
  assert.equal(on.contextBudgetTokens, 96_000)
  assert.equal(on.inlineFallbackAfter, 1)
})

test('turn budget stops runaway compaction', () => {
  const policy = resolvePolicy({ maxCompactionsPerTurn: 2 })
  assert.equal(withinTurnBudget(0, policy), true)
  assert.equal(withinTurnBudget(1, policy), true)
  assert.equal(withinTurnBudget(2, policy), false)
})

test('describeSurface maps a live session into descriptors', () => {
  const events = new Map([
    [1, { type: 'user/message', data: { role: 'user', content: [{ type: 'text', text: 'task' }] } }],
    [2, {
      type: 'user/message',
      data: { role: 'user', content: [{ type: 'text', text: 'state' }], source: { kind: CHECKPOINT_SOURCE_KIND, compactionId: 'c1' } },
    }],
    [3, {
      type: 'assistant/message',
      data: { message: { role: 'assistant', content: [{ type: 'tool-call', toolName: 'bash', arguments: { command: 'ls' } }] } },
    }],
    [4, { type: 'tool/result', data: { message: { role: 'tool', content: [{ type: 'text', text: 'a.py' }] } } }],
  ])
  const session = { surface: { nodes: [1, 2, 3, 4] }, eventAt: (seq) => events.get(seq) }
  const nodes = describeSurface(session)
  assert.equal(nodes.length, 4)
  assert.equal(nodes[1].isCheckpoint, true)
  assert.equal(nodes[2].toolCallCount, 1)
  assert.ok(nodes[3].textChars >= 4)
})

test('assembleText tolerates unknown chunks and prefers deltas', async () => {
  async function* stream() {
    yield { type: 'text-delta', text: 'hello ' }
    yield { type: 'text-delta', text: 'world' }
    yield { type: 'mystery' }
    yield { type: 'block-end', block: { type: 'text', text: 'hello world' } }
    yield { type: 'finish', usage: { totalTokens: 3 } }
  }
  const { text, usage } = await assembleText(stream())
  assert.equal(text, 'hello world')
  assert.deepEqual(usage, { totalTokens: 3 })
})

test('assembleText falls back to block-end text', async () => {
  async function* stream() {
    yield { type: 'block-end', block: { type: 'text', text: 'only-block' } }
  }
  assert.equal((await assembleText(stream())).text, 'only-block')
})
test('B2: minSpanTokens guards spans too small for a state recap', () => {
  const nodes = [
    { seq: 1, type: 'user/message', isCheckpoint: false },
    { seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 200 },
    { seq: 3, type: 'tool/result', textChars: 200 },
  ]
  assert.equal(planSacSpan(nodes, { ...SAC_DEFAULTS, minSpanChars: 0, minSpanTokens: 400 }), null)
  assert.notEqual(planSacSpan(nodes, { ...SAC_DEFAULTS, minSpanChars: 0, minSpanTokens: 0 }), null)
})

test('D1: keepLastObservation leaves the newest tool result raw', () => {
  const nodes = [
    { seq: 1, type: 'user/message', isCheckpoint: false, textChars: 900 },
    { seq: 2, type: 'assistant/message', toolCallCount: 1, textChars: 200 },
    { seq: 3, type: 'tool/result', textChars: 2000 },
    { seq: 4, type: 'assistant/message', toolCallCount: 1, textChars: 200 },
    { seq: 5, type: 'tool/result', textChars: 2000 },
  ]
  const strict = planSacSpan(nodes, { ...SAC_DEFAULTS, minSpanChars: 0, keepLastObservation: false })
  assert.equal(strict.endIndex, 4)
  assert.equal(strict.actions, 2)
  const pinned = planSacSpan(nodes, { ...SAC_DEFAULTS, minSpanChars: 0, keepLastObservation: true })
  assert.equal(pinned.endIndex, 2)
  assert.equal(pinned.actions, 1)
})

test('iter6: stateMaxTokens is a recognized, sanitized policy key', () => {
  assert.equal(resolvePolicy({}).stateMaxTokens, 2048)
  assert.equal(resolvePolicy({ stateMaxTokens: 4096 }).stateMaxTokens, 4096)
  assert.equal(resolvePolicy({ stateMaxTokens: 1 }).stateMaxTokens, 256)
  assert.equal(resolvePolicy({ stateMaxTokens: 999999 }).stateMaxTokens, 8192)
})
