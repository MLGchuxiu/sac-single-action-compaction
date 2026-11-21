#!/usr/bin/env node
/**
 * Late-mount demo for A1 (lazy install) and A3 (telemetry).
 *
 * Reproduces the real load order of the DSH compaction group:
 *
 *   1. the plugin is applied before `ctx.compaction` exists;
 *   2. the group mounts a moment later;
 *   3. the first `agent/pre-step` must install the paper template;
 *   4. a second step must not wrap the hook twice;
 *   5. a failing fold must be counted, not thrown.
 *
 * Run: node test/late-mount.demo.mjs
 */
import { apply, stateUpdateInstalled, telemetrySnapshot } from '../index.js'

const listeners = new Map()
const services = new Map()
const streamed = []
const log = []

const ctx = {
  logger: {
    info: (m) => log.push(`info  ${m}`),
    warn: (m) => log.push(`warn  ${m}`),
    error: (m) => log.push(`error ${m}`),
  },
  llm: {
    async *stream(options) {
      streamed.push(options)
      yield { type: 'text-delta', text: 'OBJECTIVE: demo\nFACTS:\n- one action folded' }
      yield { type: 'finish', usage: { inputTokens: 10, outputTokens: 5 } }
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
  async dispatch(name, payload) {
    const chain = listeners.get(name) ?? []
    let index = 0
    const next = async () => (index >= chain.length ? 'done' : chain[index++](payload, next))
    return next()
  },
}

const events = new Map()
const seqs = []
function add(event) {
  events.set(event.seq, event)
  seqs.push(event.seq)
}
add({ seq: 1, type: 'user/message', data: { content: [{ type: 'text', text: 'do the thing' }] } })
add({ seq: 2, type: 'assistant/message', data: { message: { content: [{ type: 'tool-call', toolName: 'bash', arguments: { command: 'ls' } }] } } })
add({ seq: 3, type: 'tool/result', data: { message: { content: [{ type: 'text', text: 'a.pdf' }] } } })

const agent = {
  session: {
    id: 'demo',
    surface: { nodes: seqs, replaceGeneration: 1 },
    eventAt: (seq) => events.get(seq),
    requestHeader: () => ({ config: { provider: 'p', model: 'm' } }),
    toolHistory: () => [],
  },
}

console.log('1. apply() before the compaction group mounts')
apply(ctx, { minSpanChars: 0, statePrompt: 'paper' })
console.log('   installed?', stateUpdateInstalled(undefined), '(no engine yet)')

console.log('2. the group mounts the backend')
let failNext = false
const engine = {
  async compactRegion(start, end, mountedAgent) {
    if (failNext) {
      failNext = false
      throw new Error('compaction busy')
    }
    await this.summarize({ messages: [{ role: 'user', content: [{ type: 'text', text: 'history' }] }] }, mountedAgent, undefined)
    return undefined
  },
  async summarize() {
    return { summary: [{ type: 'text', text: 'base' }] }
  },
}
services.set('compaction', engine)
console.log('   installed?', stateUpdateInstalled(engine))

console.log('3. first pre-step installs the template')
await ctx.dispatch('agent/pre-step', { agent, signal: new AbortController().signal })
console.log('   installed?', stateUpdateInstalled(engine), '| summarize calls:', streamed.length)
console.log('   telemetry:', telemetrySnapshot(agent))

console.log('4. second pre-step must not double-wrap')
await ctx.dispatch('agent/pre-step', { agent, signal: new AbortController().signal })
console.log('   summarize calls after 2 folds:', streamed.length, '(must be 2)')
console.log('   telemetry:', telemetrySnapshot(agent))

console.log('5. a failing fold is counted, not thrown')
failNext = true
await ctx.dispatch('agent/pre-step', { agent, signal: new AbortController().signal })
const telemetry = telemetrySnapshot(agent)
console.log('   skipped:', telemetry.skipped, '| reason:', telemetry.lastSkipReason)

console.log('\nlog lines:')
for (const line of log) console.log('  ', line)

const ok = stateUpdateInstalled(engine)
  && streamed.length === 2
  && telemetry.compactions === 2
  && telemetry.summarizeCalls === 2
  && telemetry.skipped === 1
  && telemetry.installedOn === 'pre-step'
console.log('\nRESULT:', ok ? 'PASS' : 'FAIL')
process.exit(ok ? 0 : 1)