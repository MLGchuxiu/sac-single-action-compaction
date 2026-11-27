// Counterfactual replay of a logged DSH session under a SAC policy.
//
//   node simulate.mjs <session.v4.jsonl> [old|new|both] [--surface <seq>[,<seq>...]] [--max-pinned N]
//
// The log's own folds are ignored. Every model/tool event is appended exactly
// as it happened; at each logged `step/start` (the pre-step point) the policy
// under test plans a fold on the simulated surface. A planned fold replaces its
// span with a synthetic checkpoint whose size is the summary the model really
// wrote at that same step in the log (median summary size when the logged run
// did not fold there). No model is called: this measures the *policy*, i.e.
// what the model would have seen on its surface, not what it would have done.
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadLog, PRESET_POLICY, textOf, kindOf } from './replay.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '../..')
// The plugin under test is the working tree; the baseline is the last commit
// before the multi-turn fix (e01ac12), exported with:
//   mkdir -p work/sac-bug/old-plugin && git archive e01ac12 plugins/dsh-sac-compact | tar -x -C work/sac-bug/old-plugin
export const PLUGINS = {
  old: process.env.SAC_OLD_PLUGIN ?? resolve(root, 'work/sac-bug/old-plugin/plugins/dsh-sac-compact'),
  new: resolve(root, 'plugins/dsh-sac-compact'),
}
export const INSTRUCTIONS = new Set(['user', 'goal'])
const CHECKPOINT = 'compact-checkpoint'

async function loadPlugin(name) {
  const index = await import(resolve(PLUGINS[name], 'index.js'))
  const policyMod = await import(resolve(PLUGINS[name], 'lib/sac-policy.js'))
  return { index, policyMod, modern: name === 'new' }
}

/** Size of the checkpoint the logged run wrote just before `step/start` seq. */
function loggedSummarySizes(log) {
  const sizes = new Map()
  for (const seq of log.order) {
    if (log.events.get(seq).type !== 'step/start') continue
    for (let q = seq - 1; q >= 0; q -= 1) {
      const event = log.events.get(q)
      if (event === undefined) continue
      if (event.type === 'user/message' && kindOf(event) === CHECKPOINT) {
        sizes.set(seq, textOf(event).length)
        break
      }
      if (event.type === 'compaction/start' || event.type === 'compaction/summary'
        || event.type === 'compaction/end' || event.type === 'compaction/prune') continue
      break
    }
  }
  return sizes
}

/** Instruction seqs a request-faithful policy must keep, whatever the plugin under test. */
async function referenceRequestSet(log) {
  const reference = await import(resolve(PLUGINS.new, 'lib/sac-policy.js'))
  const kept = new Set()
  let newestKey = null
  for (const seq of log.order) {
    const event = log.events.get(seq)
    const kind = kindOf(event)
    if (!INSTRUCTIONS.has(kind)) continue
    const key = reference.instructionKey({ sourceKind: kind, text: textOf(event) })
    if (key !== null && key === newestKey) continue
    kept.add(seq)
    newestKey = key
  }
  return kept
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted.length === 0 ? 2700 : sorted[Math.floor(sorted.length / 2)]
}

/**
 * Replay `file` under one plugin's policy.
 * @param {string} file - session log.
 * @param {'old'|'new'} pluginName - plugin tree to test.
 * @param {{policy?: object, onPreStep?: Function}} [options] - policy override
 *   and a hook `(ctx)` called at every pre-step after planning, with
 *   `{seq, surface, describe, folds}`.
 * @returns {Promise<object>} folds, instruction visibility and size statistics.
 */
export async function simulate(file, pluginName, { policy: policyOverride, onPreStep } = {}) {
  const { index, policyMod, modern } = await loadPlugin(pluginName)
  const policy = policyOverride ?? policyMod.resolvePolicy({ ...PRESET_POLICY })
  const log = loadLog(file)
  const referenceRequests = await referenceRequestSet(log)
  const loggedSizes = loggedSummarySizes(log)
  const defaultSize = median([...loggedSizes.values()])

  const surface = []          // seqs (real > 0, synthetic < 0)
  const synthetic = new Map() // seq -> event
  let nextSynthetic = -1
  const eventAt = (seq) => (seq < 0 ? synthetic.get(seq) : log.events.get(seq))
  const session = { surface: { nodes: surface }, eventAt }
  const nodeChars = (seq) => index.describeSurface({ surface: { nodes: [seq] }, eventAt })[0].textChars
  const surfaceChars = () => surface.reduce((sum, seq) => sum + nodeChars(seq), 0)
  const label = (seq) => {
    const event = eventAt(seq)
    if (seq < 0) return `synthetic-checkpoint(${nodeChars(seq)}ch)`
    const kind = kindOf(event)
    if (kind !== undefined) return `user/${kind}`
    if (event.type === 'assistant/message') {
      const calls = event.data.message.content.filter((block) => block.type === 'tool-call').length
      return calls > 0 ? `assistant(${calls} calls)` : 'assistant(final reply)'
    }
    return event.type
  }

  const folds = []
  let carried = []            // [{key, text}] newest first (new plugin only)
  let carriedChars = 0
  const repliesSeen = new Map() // instruction seq -> model replies that saw it raw
  const repliesSeenOrQuoted = new Map() // ... raw on the surface or quoted in the archive
  let maxTokens = 0
  let sumTokens = 0
  let samples = 0
  let peakDetail = null

  const measure = (seq) => {
    const tokens = Math.ceil(surfaceChars() / policy.charsPerToken)
    sumTokens += tokens
    samples += 1
    if (tokens > maxTokens) {
      maxTokens = tokens
      peakDetail = { seq, nodes: surface.length }
    }
  }
  const instructionSeqsOnSurface = () => surface.filter((seq) => INSTRUCTIONS.has(kindOf(eventAt(seq))))

  for (const seq of log.order) {
    const event = log.events.get(seq)
    const type = event.type
    if (type === 'step/start') {
      // ---- the pre-step: plan and apply at most one fold ------------------
      let nodes = index.describeSurface(session, policy)
      const options = { carriedChars }
      let span = modern ? policyMod.planSacSpan(nodes, policy, options) : policyMod.planSacSpan(nodes, policy)
      if (span === null && policy.contextBudgetTokens > 0
        && Math.ceil(nodes.reduce((sum, node) => sum + node.textChars, 0) / policy.charsPerToken) >= policy.contextBudgetTokens) {
        span = modern
          ? policyMod.planSacSpan(nodes, policy, { ...options, relaxed: true })
          : policyMod.planSacSpan(nodes, { ...policy, minSpanChars: 0, minSpanTokens: 0 })
      }
      const before = [...surface]
      if (span !== null) {
        const startSeq = nodes[span.startIndex].seq
        const endSeq = nodes[span.endIndex].seq
        const first = surface.indexOf(startSeq)
        const last = surface.lastIndexOf(endSeq)
        const shadowed = surface.slice(first, last + 1)
        const kind = span.kind ?? 'active'
        const anchor = span.anchorIndex >= 0 ? nodes[span.anchorIndex] : undefined
        const released = (span.released ?? []).map((nodeIndex) => nodes[nodeIndex])
        let size = loggedSizes.get(seq) ?? defaultSize
        if (kind === 'archive') {
          for (const node of [...released].reverse()) {
            const item = { key: policyMod.instructionKey ? policyMod.instructionKey(node) : null, text: node.text ?? '' }
            if (item.key !== null && carried.some((kept) => kept.key === item.key)) continue
            carried.unshift(item)
          }
          while (carried.length > 1 && carried.reduce((sum, item) => sum + item.text.length, 0) > policy.instructionChars) carried.pop()
          carriedChars = carried.reduce((sum, item) => sum + item.text.length, 0)
          size = defaultSize + carriedChars
        }
        const checkpointSeq = nextSynthetic
        nextSynthetic -= 1
        synthetic.set(checkpointSeq, {
          seq: checkpointSeq,
          type: 'user/message',
          data: {
            role: 'user',
            id: `synthetic-${checkpointSeq}`,
            source: { kind: CHECKPOINT },
            content: [{ type: 'text', text: 'x'.repeat(size) }],
          },
        })
        surface.splice(first, last - first + 1, checkpointSeq)
        // `referenceRequests` is policy-independent: every typed prompt, and
        // the first of a run of goal rounds that restate the same objective.
        folds.push({
          at: seq,
          kind,
          startSeq,
          endSeq,
          shadowed,
          size,
          estimatedTokens: span.estimatedTokens,
          actions: span.actions,
          anchorSeq: anchor?.seq ?? null,
          foldedRequests: shadowed.filter((q) => referenceRequests.has(q)),
          released: released.map((node) => node.seq),
        })
      }
      for (const instruction of instructionSeqsOnSurface()) {
        repliesSeen.set(instruction, repliesSeen.get(instruction) ?? 0)
      }
      measure(seq)
      if (onPreStep !== undefined) {
        onPreStep({ seq, surface: [...surface], before, label, chars: nodeChars, folds, turn: event.data.turn, step: event.data.step })
      }
      continue
    }
    if (type === 'assistant/message') {
      const visible = instructionSeqsOnSurface()
      for (const instruction of visible) {
        repliesSeen.set(instruction, (repliesSeen.get(instruction) ?? 0) + 1)
      }
      for (const instruction of log.order) {
        if (instruction > seq || !INSTRUCTIONS.has(kindOf(log.events.get(instruction)))) continue
        const quoted = carried.some((item) => item.text === textOf(log.events.get(instruction)))
        if (quoted || visible.includes(instruction)) {
          repliesSeenOrQuoted.set(instruction, (repliesSeenOrQuoted.get(instruction) ?? 0) + 1)
        }
      }
    }
    // ---- append what the logged run appended (never its checkpoints) -----
    if (event.surfaceOp === 'append' && kindOf(event) !== CHECKPOINT) {
      surface.push(seq)
      measure(seq)
    } else if (event.surfaceOp?.op === 'replace' && kindOf(event) !== CHECKPOINT) {
      // A non-SAC replacement (e.g. image offload) — mirror it.
      const first = surface.indexOf(event.surfaceOp.startSeq)
      const last = surface.lastIndexOf(event.surfaceOp.endSeq)
      if (first >= 0 && last >= first) surface.splice(first, last - first + 1, seq)
    }
  }

  const instructions = log.order.filter((seq) => INSTRUCTIONS.has(kindOf(log.events.get(seq))))
  return {
    plugin: pluginName,
    folds,
    instructions: instructions.map((seq) => {
      const foldsAway = folds.filter((fold) => fold.foldedRequests.includes(seq))
      return {
        seq,
        kind: kindOf(log.events.get(seq)),
        text: textOf(log.events.get(seq)),
        repliesSeenRaw: repliesSeen.get(seq) ?? 0,
        repliesSeenRawOrQuoted: repliesSeenOrQuoted.get(seq) ?? 0,
        totalRepliesAfter: log.order.filter((q) => q > seq && log.events.get(q).type === 'assistant/message').length,
        foldedAwayAt: foldsAway.map((fold) => fold.at),
        stillOnSurfaceAtEnd: surface.includes(seq),
        carriedAtEnd: carried.some((item) => item.text === textOf(log.events.get(seq))),
      }
    }),
    maxTokens,
    meanTokens: samples === 0 ? 0 : sumTokens / samples,
    finalTokens: Math.ceil(surfaceChars() / policy.charsPerToken),
    peakDetail,
    finalSurface: surface.map(label),
  }
}

function printResult(result) {
  const byKind = {}
  for (const fold of result.folds) byKind[fold.kind] = (byKind[fold.kind] ?? 0) + 1
  console.log(`===== ${result.plugin} plugin: ${result.folds.length} folds ${JSON.stringify(byKind)}; `
    + `folds whose span contained a user request: ${result.folds.filter((fold) => fold.foldedRequests.length > 0).length}`
    + ` (of which archive folds that quote it verbatim: ${result.folds.filter((fold) => fold.kind === 'archive' && fold.foldedRequests.length > 0).length})`)
  console.log(`  surface tokens: mean ${Math.round(result.meanTokens)}, peak ${result.maxTokens} `
    + `(at seq ${result.peakDetail?.seq}, ${result.peakDetail?.nodes} nodes), final ${result.finalTokens}`)
  for (const item of result.instructions) {
    console.log(`  instruction seq ${item.seq} [${item.kind}] replies after it: ${item.totalRepliesAfter}; `
      + `saw it raw: ${item.repliesSeenRaw}/${item.totalRepliesAfter} (raw or quoted: ${item.repliesSeenRawOrQuoted}/${item.totalRepliesAfter}); `
      + `folded at: ${item.foldedAwayAt.length === 0 ? 'never' : item.foldedAwayAt.join(',')}; `
      + `pinned at end: ${item.stillOnSurfaceAtEnd}; quoted in archive: ${item.carriedAtEnd}`
      + ` | ${item.text.slice(0, 40).replace(/\n/g, '⏎')}`)
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  const file = resolve(args[0])
  const which = args[1] && !args[1].startsWith('--') ? args[1] : 'both'
  const surfaceFlag = args.indexOf('--surface')
  const pinnedFlag = args.indexOf('--max-pinned')
  const policyTweak = pinnedFlag >= 0 ? { maxPinnedInstructions: Number(args[pinnedFlag + 1]) } : undefined
  const points = surfaceFlag >= 0
    ? new Set(args[surfaceFlag + 1].split(',').map((value) => Number(value)))
    : null
  for (const name of which === 'both' ? ['old', 'new'] : [which]) {
    const policy = policyTweak === undefined ? undefined
      : { ...PRESET_POLICY, ...policyTweak }
    const result = await simulate(file, name, {
      policy,
      onPreStep: points === null ? undefined : (ctx) => {
        if (!points.has(ctx.seq)) return
        console.log(`--- [${name}] simulated surface before step seq ${ctx.seq} (turn ${ctx.turn} step ${ctx.step}): `
          + `${ctx.surface.length} nodes, ${Math.round(ctx.surface.reduce((sum, q) => sum + ctx.chars(q), 0) / 3.5)} tokens`
          + `${ctx.folds.length > 0 && ctx.folds.at(-1).at === ctx.seq ? `; folded at this pre-step: ${ctx.folds.at(-1).kind} ${ctx.folds.at(-1).startSeq}..${ctx.folds.at(-1).endSeq} (anchor seq ${ctx.folds.at(-1).anchorSeq})` : ''}`)
        for (const seq of ctx.surface) console.log(`      ${String(seq).padStart(6)} ${ctx.label(seq).padEnd(32)} ${ctx.chars(seq)} chars`)
      },
    })
    printResult(result)
  }
}
