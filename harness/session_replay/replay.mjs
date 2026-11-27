// Replay the real SAC policy (planSacSpan + describeSurface from the plugin)
// against the surfaces recorded in a DSH session log.
//
//   node replay.mjs <session.v4.jsonl> [old|new]      (default: new; SAC_OLD_PLUGIN=<dir> for the baseline)
//
// For every `compaction/start` in the log it recomputes the span the plugin
// would have chosen on the surface as it stood at that moment and compares it
// with the range the log actually replaced. It also reports, for every
// user-authored message, how many model replies saw it raw and which fold
// removed it.
//
// Run against the baseline export (old) this validates the replay: every logged
// fold must be reproduced. Run against the working tree (new) the mismatches
// are the folds that differ on purpose — those that folded a user request.
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '../..')
export const PLUGIN_ROOTS = {
  new: resolve(root, 'plugins/dsh-sac-compact'),
  // Last commit before the multi-turn fix (e01ac12), see simulate.mjs.
  old: process.env.SAC_OLD_PLUGIN ?? resolve(root, 'work/sac-bug/old-plugin/plugins/dsh-sac-compact'),
}
const which = process.argv[3] === 'old' ? 'old' : 'new'
const pluginRoot = PLUGIN_ROOTS[which]
const { describeSurface } = await import(resolve(pluginRoot, 'index.js'))
const policyMod = await import(resolve(pluginRoot, 'lib/sac-policy.js'))

export const PRESET_POLICY = policyMod.resolvePolicy({
  enabled: true,
  actionsPerCompaction: 2,
  minSpanChars: 1200,
  minSpanTokens: 400,
  charsPerToken: 3.5,
  statePrompt: 'paper',
  stateMaxTokens: 2048,
  stateInputMaxChars: 0,
  stateBudget: 2048,
  adaptiveBudget: true,
  keepLastObservation: true,
  trimObservationOver: 4000,
  stateMaxWords: 400,
  stateInputLean: true,
  contextBudgetTokens: 96000,
  inlineFallbackAfter: 2,
  ledger: false,
  ledgerChars: 2400,
  ledgerFullCommands: true,
  recall: false,
  checklist: false,
})

export const INSTRUCTION_KINDS = new Set(['user', 'goal'])

export function textOf(event) {
  const d = event.data ?? {}
  const c = event.type === 'user/message' ? d.content : d.message?.content
  if (typeof c === 'string') return c
  if (!Array.isArray(c)) return ''
  let text = ''
  for (const block of c) if (block?.type === 'text' && typeof block.text === 'string') text += block.text
  return text
}

export function kindOf(event) {
  return event.type === 'user/message' ? event.data?.source?.kind : undefined
}

/** Load a log and expose surface snapshots at any seq. */
export function loadLog(path, { policy = PRESET_POLICY } = {}) {
  const events = new Map()
  const order = []
  const before = new Map()
  const after = new Map()
  let surface = []
  const lines = readFileSync(path, 'utf8').split('\n').filter((line) => line.length > 0)
  for (const line of lines) {
    const event = JSON.parse(line)
    if (event.type === 'session') continue
    events.set(event.seq, event)
    order.push(event.seq)
    const op = event.surfaceOp
    if (op === undefined) continue
    before.set(event.seq, [...surface])
    if (op === 'append') {
      surface.push(event.seq)
    } else if (op?.op === 'replace') {
      const first = surface.indexOf(op.startSeq)
      const last = surface.lastIndexOf(op.endSeq)
      if (first < 0 || last < first) throw new Error(`replace range ${op.startSeq}-${op.endSeq} not on surface at ${event.seq}`)
      surface.splice(first, last - first + 1, event.seq)
    }
    after.set(event.seq, [...surface])
  }
  const pos = new Map(order.map((seq, index) => [seq, index]))
  /** Surface as it stood when `seq` happened (before `seq` is applied). */
  function surfaceAt(seq) {
    const snap = before.get(seq)
    if (snap) return snap
    const index = pos.get(seq) ?? order.length
    for (let i = Math.min(index, order.length) - 1; i >= 0; i -= 1) {
      const earlier = after.get(order[i])
      if (earlier) return earlier
    }
    return []
  }
  function sessionAt(seq) {
    const nodes = surfaceAt(seq)
    return { surface: { nodes }, eventAt: (q) => events.get(q) }
  }
  return {
    path,
    events,
    order,
    surfaceAt,
    sessionAt,
    plan(seq, policyOverride = policy) {
      const nodes = describeSurface(sessionAt(seq))
      return { nodes, span: policyMod.planSacSpan(nodes, policyOverride) }
    },
  }
}

export function replay(path, options) {
  const log = loadLog(path, options)
  const folds = []
  let mismatches = 0
  for (const seq of log.order) {
    const event = log.events.get(seq)
    if (event.type !== 'compaction/start') continue
    // The checkpoint that closed this fold carries the range the log replaced.
    let checkpoint
    for (let q = seq + 1; checkpoint === undefined; q += 1) {
      const candidate = log.events.get(q)
      if (candidate?.type === 'user/message' && kindOf(candidate) === 'compact-checkpoint') checkpoint = candidate
      if (candidate?.type === 'compaction/end') break
    }
    const { nodes, span } = log.plan(seq)
    const actual = checkpoint?.surfaceOp
    const planned = span === null ? null : [nodes[span.startIndex].seq, nodes[span.endIndex].seq]
    const matches = actual === undefined ? span === null
      : planned !== null && planned[0] === actual.startSeq && planned[1] === actual.endSeq
    if (!matches) mismatches += 1
    folds.push({ seq, planned, actual: actual ? [actual.startSeq, actual.endSeq] : null, matches, span, nodes })
  }
  // Visibility of user-authored instructions.
  const instructions = log.order.filter((seq) => INSTRUCTION_KINDS.has(kindOf(log.events.get(seq))))
  const visibility = instructions.map((seq) => {
    const event = log.events.get(seq)
    const replies = log.order.filter((q) => q > seq
      && log.events.get(q).type === 'assistant/message'
      && log.surfaceAt(q).includes(seq)).length
    let foldedBy = null
    for (const fold of folds) {
      if (fold.actual && seq >= fold.actual[0] && seq <= fold.actual[1]) { foldedBy = fold.seq; break }
    }
    return { seq, kind: kindOf(event), text: textOf(event), repliesSeenRaw: replies, foldedBy }
  })
  return { log, folds, mismatches, visibility }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const file = process.argv[2]
  if (file === undefined) {
    console.error('usage: node replay.mjs <session.v4.jsonl> [old|new]')
    process.exit(2)
  }
  const { folds, mismatches, visibility } = replay(resolve(file))
  console.log(`folds logged: ${folds.length}, reproduced by planSacSpan (${which === 'old' ? 'baseline e01ac12' : 'working tree'}): `
    + `${folds.length - mismatches}, different: ${mismatches}`)
  for (const fold of folds.filter((item) => !item.matches)) {
    const swallowed = (fold.actual ?? []).length === 2
      ? fold.nodes.filter((node) => INSTRUCTION_KINDS.has(node.sourceKind) && node.seq >= fold.actual[0] && node.seq <= fold.actual[1]).map((node) => node.seq)
      : []
    console.log(`  DIFFERENT seq ${fold.seq}: logged ${JSON.stringify(fold.actual)} (user request inside: ${JSON.stringify(swallowed)}), `
      + `plugin now plans ${JSON.stringify(fold.planned)}`)
  }
  for (const item of visibility) {
    console.log(`  instruction seq ${item.seq} [${item.kind}] replies that saw it raw: ${item.repliesSeenRaw}, folded by compaction ${item.foldedBy ?? '-'} | ${item.text.slice(0, 70).replace(/\n/g, '⏎')}`)
  }
}
