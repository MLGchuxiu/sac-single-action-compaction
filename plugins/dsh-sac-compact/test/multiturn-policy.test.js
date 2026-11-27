import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  SAC_DEFAULTS,
  archiveMinTokensFor,
  findTaskAnchorIndex,
  instructionKey,
  isInstructionNode,
  planSacSpan,
  planSegments,
  resolvePolicy,
} from '../lib/sac-policy.js'

/**
 * Surface of a multi-turn session.
 *
 * task(n)    user instruction n (text long enough to tell apart)
 * call()     one assistant step with a tool call
 * result()   its result
 * reply()    a final assistant reply without tool calls
 * notice()   a harness notice delivered as a user-role message
 * state()    a checkpoint written by an earlier fold
 */
const task = (seq, text = `request number ${seq}`) => ({
  seq, type: 'user/message', sourceKind: 'user', text, toolCallCount: 0, textChars: 300,
})
const call = (seq) => ({ seq, type: 'assistant/message', toolCallCount: 1, textChars: 150 })
const result = (seq, textChars = 1500) => ({ seq, type: 'tool/result', textChars })
const reply = (seq) => ({ seq, type: 'assistant/message', toolCallCount: 0, textChars: 900 })
const notice = (seq) => ({ seq, type: 'user/message', sourceKind: 'runtime-context', textChars: 700 })
const state = (seq) => ({
  seq, type: 'user/message', isCheckpoint: true, sourceKind: 'compact-checkpoint', textChars: 2500,
})

const seqs = (nodes, span) => nodes.slice(span.startIndex, span.endIndex + 1).map((node) => node.seq)

/** The shipped preset policy. */
const preset = resolvePolicy({
  actionsPerCompaction: 2, minSpanChars: 1200, minSpanTokens: 400, statePrompt: 'paper',
  keepLastObservation: true, stateMaxWords: 400, stateInputLean: true, contextBudgetTokens: 96000,
})

/** Apply a planned fold the way the backend would: the span becomes one checkpoint. */
function applyFold(nodes, span, seq) {
  nodes.splice(span.startIndex, span.endIndex - span.startIndex + 1, state(seq))
}

test('instruction nodes: typed prompts and goal rounds, never harness notices or checkpoints', () => {
  assert.equal(isInstructionNode(task(1)), true)
  assert.equal(isInstructionNode({ seq: 1, type: 'user/message', sourceKind: 'goal', text: '<goal_round>…' }), true)
  assert.equal(isInstructionNode({ seq: 1, type: 'user/message', sourceKind: 'tool-jobs', text: 'job done' }), false)
  assert.equal(isInstructionNode(notice(1)), false)
  assert.equal(isInstructionNode({ ...task(1), isCheckpoint: true }), false)
  assert.equal(isInstructionNode({ seq: 1, type: 'assistant/message' }), false)
  // Without provenance the message is still treated as the task (older hosts).
  assert.equal(isInstructionNode({ seq: 1, type: 'user/message' }), true)
  const custom = resolvePolicy({ instructionSourceKinds: 'user, scheduled' })
  assert.deepEqual([...custom.instructionSourceKinds], ['user', 'scheduled'])
  assert.equal(isInstructionNode({ seq: 1, type: 'user/message', sourceKind: 'goal' }, custom), false)
  assert.equal(resolvePolicy({ instructionSourceKinds: [] }).instructionSourceKinds, SAC_DEFAULTS.instructionSourceKinds)
})

test('instruction key ignores the goal round counter and whitespace only', () => {
  const round = (count, objective = 'ship it') => ({
    sourceKind: 'goal',
    text: `<goal_round>\nObjective: "${objective}"\nRound: ${count}/256\n\nContinue working.\n</goal_round>`,
  })
  assert.equal(instructionKey(round(1)), instructionKey(round(17)))
  assert.notEqual(instructionKey(round(1)), instructionKey(round(2, 'ship it to port 2')))
  // A typed prompt that differs in one digit is a different request.
  assert.notEqual(instructionKey(task(1, 'use port 13001')), instructionKey(task(2, 'use port 13002')))
  assert.equal(instructionKey(task(1, ' a  b\n c ')), 'a b c')
  assert.equal(instructionKey({ seq: 1, type: 'user/message', sourceKind: 'user' }), null)
})

test('a follow-up request is never inside the folded span, however long the turn runs', () => {
  const nodes = [task(1), call(2), result(3), call(4), result(5), reply(6), task(7)]
  let folds = 0
  let next = 100
  for (let step = 0; step < 12; step += 1) {
    nodes.push(call(next), result(next + 1))
    next += 2
    const span = planSacSpan(nodes, preset)
    if (span === null) continue
    assert.ok(!seqs(nodes, span).includes(7), `step ${step}: the request seq 7 stays on the surface`)
    assert.ok(!seqs(nodes, span).includes(1), 'and so does the first request')
    applyFold(nodes, span, (next += 1))
    folds += 1
  }
  assert.ok(folds >= 4, 'the turn kept folding')
  assert.ok(nodes.some((node) => node.seq === 7), 'the request outlives every fold')
})

test('regression: the single-anchor behaviour folded the follow-up request after two actions', () => {
  const nodes = [task(1), state(2), call(3), result(4), reply(5), task(6), call(7), result(8), call(9), result(10)]
  const legacy = planSacSpan(nodes, { ...preset, pinInstructions: false })
  assert.deepEqual(seqs(nodes, legacy), [2, 3, 4, 5, 6, 7, 8], 'old policy: request 6 is folded with the turn-1 leftovers')
  const fixed = planSacSpan(nodes, preset)
  assert.deepEqual(seqs(nodes, fixed), [2, 3, 4], 'new policy: only the finished turn folds, final reply 5 stays')
  assert.equal(fixed.kind, 'frozen')
  assert.equal(fixed.anchorIndex, 0)
})

test('the model answers a new request before earlier turns are folded', () => {
  const nodes = [task(1), state(2), call(3), result(4), reply(5), task(6)]
  assert.equal(planSacSpan(nodes, preset), null, 'nothing has happened in the new turn yet')
  // One action into the new turn the finished turn may fold, in a free step.
  const span = planSacSpan([...nodes, call(7), result(8)], preset)
  assert.deepEqual(seqs(nodes, span), [2, 3, 4])
  assert.equal(span.kind, 'frozen')
  // A hard context budget does not wait, and folds the final reply as well.
  const forced = planSacSpan(nodes, preset, { relaxed: true })
  assert.deepEqual(seqs(nodes, forced), [2, 3, 4, 5])
})

test('a finished turn keeps its final reply and folds its leftover action even without snapshots', () => {
  const nodes = [task(1), state(2), call(3), result(4, 80), task(5), call(6), result(7)]
  const span = planSacSpan(nodes, preset)
  assert.deepEqual(seqs(nodes, span), [2, 3, 4], 'keepLastObservation does not pin the finished turn\'s observation')
  const withReply = [task(1), state(2), call(3), result(4), reply(5), task(6), call(7), result(8)]
  assert.deepEqual(seqs(withReply, planSacSpan(withReply, preset)), [2, 3, 4], 'the final reply 5 stays')
})

test('a lone checkpoint is never re-summarized; bare snapshots fold only under a hard budget', () => {
  const stateOnly = [task(1), state(2), task(3), call(4), result(5)]
  assert.equal(planSacSpan(stateOnly, preset, { relaxed: true }), null)
  const snapshots = [task(1), state(2), notice(3), task(4), call(5), result(6)]
  assert.equal(planSacSpan(snapshots, preset), null, 'no action, no pressure: nothing to do')
  assert.deepEqual(seqs(snapshots, planSacSpan(snapshots, preset, { relaxed: true })), [2, 3])
})

test('working state goes first; finished turns use the steps it leaves free', () => {
  const nodes = [
    task(1), state(2), call(3), result(4),
    task(5), state(6), call(7), result(8), call(9), result(10), call(11), result(12),
  ]
  const span = planSacSpan(nodes, preset)
  assert.equal(span.kind, 'active', 'the working state has enough to fold')
  assert.deepEqual(seqs(nodes, span), [6, 7, 8, 9, 10], 'two actions fold, the newest stays raw')
  applyFold(nodes, span, 13)
  const frozen = planSacSpan(nodes, preset)
  assert.equal(frozen.kind, 'frozen', 'with one action in hand the working state waits, the finished turn folds')
  assert.deepEqual(seqs(nodes, frozen), [2, 3, 4])
})

test('older requests are released into one archive that still quotes them', () => {
  const policy = { ...preset, maxPinnedInstructions: 2 }
  const nodes = [
    task(1), state(2), reply(3),
    task(4), state(5),
    task(6), state(7), call(8), result(9),
    task(10), state(11), call(12), result(13),
    task(14), call(15), result(16),
  ]
  const layout = planSegments(nodes, policy)
  assert.deepEqual(layout.instructions, [0, 3, 5, 9, 13])
  assert.deepEqual(layout.pinned, [9, 13])
  assert.deepEqual(layout.released, [0, 3, 5])
  const kinds = []
  let next = 100
  let archive
  for (let round = 0; round < 6; round += 1) {
    const span = planSacSpan(nodes, policy)
    if (span === null) break
    kinds.push(span.kind)
    assert.ok(span.kind !== 'active', 'the one action in hand is not enough for the working state')
    if (span.kind === 'archive') {
      archive = span
      break
    }
    applyFold(nodes, span, (next += 1))
  }
  assert.deepEqual(kinds, ['frozen', 'archive'], 'the finished turn with material folds first, then the archive')
  const seq = (index) => nodes[index].seq
  assert.equal(archive.anchorIndex, -1)
  assert.deepEqual(
    seqs(nodes, archive),
    nodes.slice(0, archive.endIndex + 1).map((node) => node.seq),
    'everything before the oldest pinned request, in one span',
  )
  assert.deepEqual(archive.released, [0, 3, 5], 'the three released requests are reported in order')
  assert.equal(seq(archive.released[2]), 6, 'released indices are positions on the current surface')
})

test('an archive fold waits until its span can come out smaller', () => {
  const policy = { ...preset, maxPinnedInstructions: 1 }
  const tiny = [task(1, 'short'), reply(2), task(3), call(4), result(5)]
  assert.equal(planSacSpan(tiny, policy, { carriedChars: 0 }), null, 'a short request and one reply are cheaper raw')
  const roomy = [task(1), state(2), call(3), result(4), call(5), result(6), reply(7), task(8), call(9), result(10)]
  const span = planSacSpan(roomy, policy, { carriedChars: 0 })
  assert.equal(span.kind, 'archive')
  assert.deepEqual(seqs(roomy, span), [1, 2, 3, 4, 5, 6], 'the final reply 7 stays')
  // The same span is refused once the archive already carries a long list.
  assert.equal(planSacSpan(roomy, policy, { carriedChars: 4000 }), null)
  assert.ok(archiveMinTokensFor(policy, 4000) > archiveMinTokensFor(policy, 0))
  assert.ok(Number.isFinite(archiveMinTokensFor(policy, 0)), 'the automatic guard is a number')
  assert.equal(archiveMinTokensFor({ ...policy, archiveMinTokens: 5000 }, 0), 5000)
})

test('maxPinnedInstructions 0 never releases a request', () => {
  const nodes = [task(1), state(2), task(3), state(4), task(5), call(6), result(7)]
  const layout = planSegments(nodes, { ...preset, maxPinnedInstructions: 0 })
  assert.deepEqual(layout.released, [])
  assert.equal(layout.segments.some((segment) => segment.kind === 'archive'), false)
})

test('a goal round that re-delivers the objective is not a new anchor', () => {
  const goal = (seq, count, objective = 'build the plugin') => ({
    seq, type: 'user/message', sourceKind: 'goal', toolCallCount: 0, textChars: 400,
    text: `<goal_round>\nObjective: "${objective}"\nRound: ${count}/256\n</goal_round>`,
  })
  const nodes = [goal(1, 1), call(2), result(3), call(4), result(5), reply(6), goal(7, 2)]
  const layout = planSegments(nodes, preset)
  assert.deepEqual(layout.instructions, [0], 'round 2 restates round 1')
  const span = planSacSpan(nodes, preset)
  assert.ok(seqs(nodes, span).includes(7), 'the re-delivery is ordinary foldable content')
  assert.deepEqual(span.released, [], 'and is not a released request')
  const edited = [...nodes, goal(8, 3, 'build the plugin and deploy it')]
  assert.deepEqual(planSegments(edited, preset).instructions, [0, 7], 'an edited objective is a new anchor')
})

test('harness notices are not anchors: the first real request is pinned', () => {
  const nodes = [notice(1), task(2), call(3), result(4)]
  const layout = planSegments(nodes, preset)
  assert.deepEqual(layout.pinned, [1])
  assert.equal(layout.segments.some((segment) => segment.kind === 'archive'), false, 'nothing precedes the request to archive')
  const span = planSacSpan(nodes) // strict defaults: one action is enough
  assert.equal(span.kind, 'active')
  assert.equal(span.startIndex, 2)
  // Without any instruction the surface keeps the legacy single anchor.
  const synthetic = [notice(1), call(2), result(3)]
  const legacy = planSacSpan(synthetic)
  assert.equal(legacy.kind, 'active')
  assert.equal(legacy.startIndex, findTaskAnchorIndex(synthetic) + 1)
  assert.equal(legacy.anchorIndex, 0, 'the notice itself is the anchor')
})

test('pinInstructions off restores the single anchor', () => {
  const policy = resolvePolicy({ pinInstructions: false })
  assert.equal(policy.pinInstructions, false)
  const nodes = [task(1), call(2), result(3), task(4), call(5), result(6)]
  const layout = planSegments(nodes, policy)
  assert.equal(layout.segments.length, 1)
  assert.equal(layout.segments[0].kind, 'active')
  assert.equal(layout.segments[0].startIdx, 1)
})

test('single-task surfaces plan exactly as before', () => {
  const policy = resolvePolicy({ actionsPerCompaction: 2, minSpanChars: 0, minSpanTokens: 0, keepLastObservation: true })
  for (let actions = 0; actions <= 5; actions += 1) {
    for (const trailing of [false, true]) {
      const nodes = [task(1)]
      for (let index = 0; index < actions; index += 1) {
        nodes.push(call(nodes.length + 1), result(nodes.length + 2))
      }
      if (trailing) nodes.push(reply(nodes.length + 1))
      const before = planSacSpan(nodes, { ...policy, pinInstructions: false })
      const after = planSacSpan(nodes, policy)
      assert.deepEqual(after, before, `${actions} actions, trailing reply ${trailing}`)
    }
  }
})

test('new policy fields are sanitized', () => {
  const policy = resolvePolicy({ maxPinnedInstructions: -3, instructionChars: 'nope', archiveMinTokens: 12.7 })
  assert.equal(policy.maxPinnedInstructions, 0)
  assert.equal(policy.instructionChars, SAC_DEFAULTS.instructionChars)
  assert.equal(policy.archiveMinTokens, 12)
  assert.equal(resolvePolicy({}).pinInstructions, true)
  assert.equal(resolvePolicy({ maxPinnedInstructions: 2.9 }).maxPinnedInstructions, 2)
})
