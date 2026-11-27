/**
 * The `sac` preset must run the same policy as the plugin's shipped bundle.
 *
 * Up to plugin 1.3.0 the two config blocks drifted: the bundle config gained
 * the cost-aware keys and the preset's did not, so sessions that selected the
 * preset in the GUI kept folding after every two actions (the behaviour the
 * Pareto report measured as a bad trade) while the benchmarks ran the new
 * policy. These tests read both YAML files, so a change to one side without
 * the other fails here.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { effectiveMarks, planSacSpan, resolvePolicy } from '../../dsh-sac-compact/lib/sac-policy.js'

const here = (rel) => fileURLToPath(new URL(rel, import.meta.url))
const PRESET_PATCH = readFileSync(here('../cordis.patch.yml'), 'utf8')
const BUNDLE_PATCH = readFileSync(here('../../dsh-sac-compact/cordis.patch.yml'), 'utf8')

/** Remove a trailing `# comment` that stands outside quotes. */
function stripComment(line) {
  let quote = null
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i]
    if (quote) {
      if (ch === quote) quote = null
    } else if (ch === "'" || ch === '"') {
      quote = ch
    } else if (ch === '#' && (i === 0 || /\s/.test(line[i - 1]))) {
      return line.slice(0, i)
    }
  }
  return line
}

/** A plain scalar, or an error: this reader never guesses. */
function parseScalar(raw, where) {
  const text = raw.trim()
  if ((text.startsWith("'") && text.endsWith("'")) || (text.startsWith('"') && text.endsWith('"'))) {
    return text.slice(1, -1)
  }
  if (text === 'true' || text === 'false') return text === 'true'
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text)
  // An unquoted enum value such as `paper`, `size` or `frozen`.
  if (/^[A-Za-z_][\w.-]*$/.test(text)) return text
  throw new Error(`${where}: unsupported value "${text}" (keep the sac-compact config to plain scalars)`)
}

/**
 * The `config:` block of the `sac-compact` entry of a loader patch, as an
 * object. It understands exactly what these blocks use (`key: value` and
 * block lists of scalars) and throws on anything else, so a config it cannot
 * read fails the test instead of passing silently.
 * @param {string} patchText - contents of a cordis.patch.yml.
 * @returns {Record<string, unknown>} the config.
 */
export function readSacConfig(patchText) {
  const lines = patchText.split(/\r?\n/)
  const indentOf = (line) => line.length - line.trimStart().length
  const entry = lines.findIndex((line) => /^\s*- id:\s*sac-compact\s*$/.test(stripComment(line)))
  if (entry < 0) throw new Error('no `- id: sac-compact` entry in the patch')
  const entryIndent = indentOf(lines[entry])
  let configIndex = -1
  for (let i = entry + 1; i < lines.length; i += 1) {
    const line = stripComment(lines[i])
    if (!line.trim()) continue
    if (indentOf(lines[i]) <= entryIndent) break
    if (indentOf(lines[i]) === entryIndent + 2 && /^config:\s*$/.test(line.trim())) {
      configIndex = i
      break
    }
  }
  if (configIndex < 0) throw new Error('the sac-compact entry has no config block')
  const configIndent = indentOf(lines[configIndex])
  const out = {}
  let list = null
  for (let i = configIndex + 1; i < lines.length; i += 1) {
    const raw = lines[i]
    const line = stripComment(raw)
    if (!line.trim()) continue
    const indent = indentOf(raw)
    if (indent <= configIndent) break
    const where = `${i + 1}:`
    if (indent === configIndent + 4 && /^\s*-\s/.test(line)) {
      if (!list) throw new Error(`${where} list item outside a list`)
      list.push(parseScalar(line.replace(/^\s*-\s*/, ''), where))
      continue
    }
    if (indent !== configIndent + 2) throw new Error(`${where} unexpected indentation in the sac-compact config`)
    const match = line.trim().match(/^([A-Za-z][\w]*):\s*(.*)$/)
    if (!match) throw new Error(`${where} expected "key: value", got "${line.trim()}"`)
    if (match[2] === '') {
      list = []
      out[match[1]] = list
    } else {
      out[match[1]] = parseScalar(match[2], where)
      list = null
    }
  }
  return out
}

/** A session with `actions` answered tool calls after one user request. */
function surface(actions, charsPerAction = 2000) {
  const nodes = [{ seq: 0, type: 'user/message', sourceKind: 'user', textChars: 60 }]
  let seq = 1
  for (let i = 0; i < actions; i += 1) {
    nodes.push({ seq: seq++, type: 'assistant/message', toolCallCount: 1, textChars: charsPerAction / 4 })
    nodes.push({ seq: seq++, type: 'tool/result', textChars: charsPerAction * 3 / 4 })
  }
  return nodes
}

const presetConfig = readSacConfig(PRESET_PATCH)
const bundleConfig = readSacConfig(BUNDLE_PATCH)
const policy = resolvePolicy(presetConfig)

test('both patches are read in full: every shipped key is present on both sides', () => {
  const keys = (config) => Object.keys(config).sort()
  assert.deepEqual(keys(presetConfig), keys(bundleConfig))
  // The keys the cost-aware policy is made of (plugin 1.3.0).
  for (const key of ['foldTrigger', 'foldMarginTokens', 'foldAboveTokens', 'foldBelowTokens', 'maxFoldsPerStep',
    'foldMinGainTokens', 'recapExpectedTokens', 'recapMaxRatio', 'mechanicalFoldMaxTokens', 'mechanicalSegments',
    'mechanicalMinGainTokens', 'digestResultChars', 'digestReplyChars', 'digestMaxChars', 'digestShrinkToBudget',
    'replyReasoningFoldChars', 'stateMaxTokensThinking', 'goalDedupe', 'stateProvider', 'stateModel',
    'stateMaxTokensCeiling', 'standingSourceKinds']) {
    assert.equal(key in presetConfig, true, `preset config lacks ${key}`)
  }
})

test('the preset and the shipped bundle resolve to the same policy', () => {
  assert.deepEqual(presetConfig, bundleConfig)
  assert.deepEqual(resolvePolicy(bundleConfig), policy)
})

test('that policy is the cost-aware one measured in reports/REPORT-SAC-PARETO.md', () => {
  assert.equal(policy.statePrompt, 'paper')
  assert.equal(policy.foldTrigger, 'size')
  assert.equal(policy.foldMarginTokens, 32000)
  assert.equal(policy.maxFoldsPerStep, 3)
  assert.equal(policy.foldMinGainTokens, 400)
  assert.equal(policy.recapMaxRatio, 0.6)
  assert.equal(policy.mechanicalFoldMaxTokens, 4000)
  assert.equal(policy.mechanicalSegments, 'frozen')
  assert.equal(policy.digestResultChars, 2000)
  assert.equal(policy.digestShrinkToBudget, true)
  assert.equal(policy.replyReasoningFoldChars, 1500)
  assert.equal(policy.stateMaxTokensThinking, 4096)
  assert.equal(policy.contextBudgetTokens, 96000)
  assert.equal(policy.pinInstructions, true)
  assert.equal(policy.stateProvider, '')
  assert.equal(policy.stateModel, '')
})

test('a short dialogue is not folded: three answered actions stay on the surface', () => {
  const { above } = effectiveMarks(policy, 20000)
  assert.equal(above, 52000, 'fixed prompt + 32K')
  // The same dialogue the 1.2.0 trigger folds (see the last test).
  assert.equal(planSacSpan(surface(3), policy, { requestTokens: above - 1, foldAboveTokens: above }), null)
})

test('over the mark the working state folds, unless the fold would not pay for itself', () => {
  const { above } = effectiveMarks(policy, 20000)
  // Two folded actions of about 515 tokens: less than the expected recap plus the 400-token margin.
  const thin = surface(3, 900)
  assert.equal(planSacSpan(thin, policy, { requestTokens: above, foldAboveTokens: above }), null)
  const nodes = surface(3)
  const span = planSacSpan(nodes, policy, { requestTokens: above, foldAboveTokens: above })
  assert.notEqual(span, null)
  assert.equal(span.kind, 'active')
  assert.equal(span.mechanical, false, 'the working state gets a model-written recap, never a digest')
  assert.equal(span.actions, 2)
  assert.equal(span.startIndex, 1, 'the span starts right after the request')
  assert.equal(span.endIndex, nodes.length - 3, 'the newest observation (keepLastObservation) stays raw')
  assert.equal(nodes[span.endIndex].type, 'tool/result')
})

test('what changed: the 1.2.0 trigger folded that same short dialogue, whatever the request size', () => {
  // The keys the preset shipped before 1.3.0; the plugin defaults supplied the rest.
  const legacy = resolvePolicy({ actionsPerCompaction: 2, minSpanChars: 1200, minSpanTokens: 400, statePrompt: 'paper',
    keepLastObservation: true })
  assert.equal(legacy.foldTrigger, 'actions')
  const span = planSacSpan(surface(3), legacy)
  assert.notEqual(span, null)
  assert.equal(span.actions, 2, 'two actions fold, the newest observation stays raw')
})

test('the preset registers `sac` and mounts the policy inside the isolated compaction group', () => {
  assert.match(PRESET_PATCH, /^- insert:\n\s+- id: preset-sac\n\s+name: '@deepseek-ai\/dsh-agent-preset'\n/m)
  assert.match(PRESET_PATCH, /^\s+id: sac\n/m)
  assert.match(PRESET_PATCH, /^\s+name: '@local\/dsh-sac-preset\/sac'\n/m)
  assert.match(BUNDLE_PATCH, /^\s+name: '@local\/dsh-sac-compact'\n/m)
  const order = ['- id: compaction', '- id: compaction-basic', '- id: sac-compact', '- id: delegation']
    .map((needle) => PRESET_PATCH.indexOf(needle))
  assert.ok(order.every((index) => index >= 0), 'compaction group, backend, policy and delegation group are all listed')
  assert.deepEqual([...order].sort((a, b) => a - b), order, 'sac-compact sits in the compaction group, after the backend')
})
