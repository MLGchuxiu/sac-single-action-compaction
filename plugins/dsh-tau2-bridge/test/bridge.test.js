import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import {
  AGENT_INSTRUCTION,
  apply,
  callBridge,
  loadBridgeConfig,
  parseTools,
  tau2ToolDefinition,
} from '../index.js'

/** Local stand-in for the orchestrator's bridge: records calls, answers like tau2. */
async function fakeBridge({ token = 'secret', reply } = {}) {
  const seen = []
  const server = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8')
    request.on('data', (chunk) => { body += chunk })
    request.on('end', () => {
      seen.push({ url: request.url, authorization: request.headers.authorization, body: body.length > 0 ? JSON.parse(body) : null })
      const answer = reply ? reply(seen.at(-1)) : { content: '{"user_id": 1}' }
      response.writeHead(answer.status ?? 200, { 'content-type': 'application/json' })
      response.end(JSON.stringify(answer.payload ?? answer))
    })
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  return {
    seen,
    url: `http://127.0.0.1:${server.address().port}`,
    token,
    close: () => new Promise((resolve) => server.close(resolve)),
  }
}

/** Context that records what the plugin registers. */
function fakeCtx() {
  const sections = []
  const tools = []
  return {
    sections,
    tools,
    logger: { info() {}, warn() {} },
    get(name) {
      if (name === 'systemPrompt') return { section: (section) => sections.push(section) }
      if (name === 'tools') return { register: (definition) => { tools.push(definition); return () => {} } }
      return undefined
    },
  }
}

const TOOL = {
  name: 'get_user_details',
  description: 'Get the details of a user.',
  parameters: { properties: { user_id: { type: 'integer', title: 'User Id' } }, required: ['user_id'], title: 'parameters', type: 'object' },
}

test('without TAU2_BRIDGE_URL the plugin is idle', () => {
  const ctx = fakeCtx()
  apply(ctx, {})
  assert.equal(ctx.tools.length, 0)
  assert.equal(ctx.sections.length, 0)
  assert.equal(loadBridgeConfig({}), null)
  assert.equal(loadBridgeConfig({ TAU2_BRIDGE_URL: '' }), null, 'an empty variable counts as unset')
})

test('the bridge configuration is read from the environment', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tau2-bridge-'))
  writeFileSync(join(dir, 'tools.json'), JSON.stringify([TOOL, { name: 'bare' }]))
  writeFileSync(join(dir, 'policy.md'), '## Policy\nBe polite. {{literal}}')
  const config = loadBridgeConfig({
    TAU2_BRIDGE_URL: 'http://127.0.0.1:5000/',
    TAU2_BRIDGE_TOKEN: 't0k',
    TAU2_BRIDGE_TOOLS: join(dir, 'tools.json'),
    TAU2_BRIDGE_POLICY: join(dir, 'policy.md'),
    TAU2_BRIDGE_TIMEOUT_MS: '2500',
  })
  assert.equal(config.url, 'http://127.0.0.1:5000', 'trailing slash removed')
  assert.equal(config.token, 't0k')
  assert.equal(config.timeoutMs, 2500)
  assert.equal(config.policy, '## Policy\nBe polite. {{literal}}')
  assert.deepEqual(config.tools.map((tool) => tool.name), ['get_user_details', 'bare'])
  assert.equal(config.tools[1].description, 'bare', 'a tool without a description is described by its name')
  assert.deepEqual(config.tools[1].parameters, { type: 'object', properties: {} })
  assert.throws(() => parseTools('{"name": "x"}'), /JSON array/)
})

test('the plugin mounts the policy as a literal system-prompt section and one tool per tau2 tool', async (t) => {
  const bridge = await fakeBridge()
  t.after(() => bridge.close())
  const dir = mkdtempSync(join(tmpdir(), 'tau2-bridge-'))
  writeFileSync(join(dir, 'tools.json'), JSON.stringify([TOOL]))
  writeFileSync(join(dir, 'policy.md'), 'Respect the budget {{var}}.')
  const previous = {
    TAU2_BRIDGE_URL: process.env.TAU2_BRIDGE_URL,
    TAU2_BRIDGE_TOKEN: process.env.TAU2_BRIDGE_TOKEN,
    TAU2_BRIDGE_TOOLS: process.env.TAU2_BRIDGE_TOOLS,
    TAU2_BRIDGE_POLICY: process.env.TAU2_BRIDGE_POLICY,
  }
  process.env.TAU2_BRIDGE_URL = bridge.url
  process.env.TAU2_BRIDGE_TOKEN = bridge.token
  process.env.TAU2_BRIDGE_TOOLS = join(dir, 'tools.json')
  process.env.TAU2_BRIDGE_POLICY = join(dir, 'policy.md')
  t.after(() => {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  })

  const ctx = fakeCtx()
  apply(ctx, {})

  assert.equal(ctx.tools.length, 1)
  assert.equal(ctx.tools[0].name, 'get_user_details')
  assert.deepEqual(ctx.tools[0].parameters, TOOL.parameters, 'the official schema is passed through untouched')
  assert.equal(ctx.tools[0].isConcurrencySafe(), false, 'the official orchestrator runs tool calls one at a time')
  assert.equal(ctx.sections.length, 1)
  assert.equal(ctx.sections[0].interpolate, false, 'policy text with braces stays literal')
  assert.ok(ctx.sections[0].text.startsWith('<instructions>'))
  assert.ok(ctx.sections[0].text.includes(AGENT_INSTRUCTION))
  assert.ok(ctx.sections[0].text.includes('<policy>\nRespect the budget {{var}}.\n</policy>'))

  const exec = { callId: 'call_1', signal: undefined }
  const value = await ctx.tools[0].execute({ user_id: 7 }, exec)
  assert.equal(value, '{"user_id": 1}')
  assert.deepEqual(bridge.seen, [{
    url: '/call',
    authorization: 'Bearer secret',
    body: { id: 'call_1', name: 'get_user_details', arguments: { user_id: 7 } },
  }], 'the DSH call id travels to tau2, where it pairs the tool call with its result')
})

test('a tool error comes back as the tool text; an unreachable bridge fails loudly', async (t) => {
  const failing = await fakeBridge({ reply: () => ({ content: 'Error: user not found', error: true }) })
  t.after(() => failing.close())
  const text = await callBridge({ url: failing.url, token: null, timeoutMs: 2000 }, 'id', 'get_user_details', {})
  assert.equal(text, 'Error: user not found')
  assert.equal(failing.seen[0].authorization, undefined, 'no token configured, no header sent')

  const unauthorized = await fakeBridge({ token: 'other', reply: (call) => (call.authorization === 'Bearer other' ? { content: 'ok' } : { status: 401, payload: { error: true, content: 'bad token' } }) })
  t.after(() => unauthorized.close())
  await assert.rejects(
    callBridge({ url: unauthorized.url, token: 'wrong', timeoutMs: 2000 }, 'id', 'x', {}),
    /HTTP 401/,
  )

  const tool = tau2ToolDefinition(TOOL, () => callBridge({ url: 'http://127.0.0.1:9', token: null, timeoutMs: 500 }, 'id', 'x', {}))
  await assert.rejects(tool.execute({}, { callId: 'c' }), /tau2 bridge unreachable/)
})
