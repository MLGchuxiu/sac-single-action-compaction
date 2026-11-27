/**
 * `dsh-tau2-bridge` — run a DSH agent inside the official tau2-bench orchestrator.
 *
 * The orchestrator (tasks, user simulator, environment, evaluation) stays the
 * official tau2-bench code and runs in the harness process; the agent under
 * test is a DSH session. This plugin is the DSH side of that setup:
 *
 *   - it registers every tau2 domain tool as a native DSH tool whose
 *     `execute` forwards the call over a local HTTP bridge to the orchestrator
 *     process, which runs it on the live environment exactly as it would for
 *     the built-in agent (`environment.get_response`), records it in the
 *     official trajectory, and returns the tool message;
 *   - it mounts the tau2 agent instruction and the domain policy as a
 *     system-prompt section, so the policy is in every request (as in the
 *     official agent) and never occupies a user message.
 *
 * Everything is driven by the process environment, so concurrent simulations
 * (one DSH process each) cannot see each other's bridge:
 *
 *   TAU2_BRIDGE_URL     e.g. http://127.0.0.1:52341   (unset: the plugin is idle)
 *   TAU2_BRIDGE_TOKEN   bearer token checked by the bridge
 *   TAU2_BRIDGE_TOOLS   JSON file: [{name, description, parameters}] — the
 *                       official OpenAI function schemas of the domain tools
 *   TAU2_BRIDGE_POLICY  optional text file with the domain policy
 *   TAU2_BRIDGE_TIMEOUT_MS  per-call timeout (default 120000)
 *
 * @module dsh-tau2-bridge
 */
import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'

export const name = 'dsh-tau2-bridge'

/** Optional services: the bridge needs the tool registry, and the system prompt for the policy. */
export const inject = { optional: ['systemPrompt', 'tools'] }

export const DEFAULT_TIMEOUT_MS = 120_000

/**
 * What the DSH agent is told about its role. The official tau2 `LLMAgent`
 * sends either one message or one tool call per step; a DSH turn may run
 * several tool calls before it replies, and only the final text of a turn
 * reaches the simulated user, so the instruction says so.
 */
export const AGENT_INSTRUCTION = [
  'You are a customer service agent that helps the user according to the <policy> provided below.',
  'You act through the available tools. A turn may run several tool calls in a row.',
  'The text you send in the same reply as a tool call is not shown to the user: only the final reply of a turn,',
  'the one you send without calling any tool, is delivered to the user.',
  'Try to be helpful and always follow the policy.',
].join('\n')

const SECTION_NAME = 'tau2-bridge:policy'
const SECTION_ORDER = 50

/** Read one bridge variable from the environment (empty counts as unset). */
function variable(env, key) {
  const value = env[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** Parse a tools file: `[{name, description, parameters}]`. */
export function parseTools(text) {
  const parsed = JSON.parse(text)
  if (!Array.isArray(parsed)) throw new Error('TAU2_BRIDGE_TOOLS must hold a JSON array of tool definitions')
  return parsed.map((tool) => ({
    name: String(tool.name),
    description: typeof tool.description === 'string' && tool.description.length > 0 ? tool.description : String(tool.name),
    parameters: tool.parameters ?? { type: 'object', properties: {} },
  }))
}

/**
 * Resolve the bridge configuration from the environment.
 * @param {NodeJS.ProcessEnv} [env] - environment to read.
 * @returns {null|{url: string, token: string|null, tools: Array<object>, policy: string, timeoutMs: number}}
 *   null when the plugin is not in a bridged run.
 */
export function loadBridgeConfig(env = process.env) {
  const url = variable(env, 'TAU2_BRIDGE_URL')
  if (url === null) return null
  const toolsPath = variable(env, 'TAU2_BRIDGE_TOOLS')
  const policyPath = variable(env, 'TAU2_BRIDGE_POLICY')
  const rawTimeout = Number(variable(env, 'TAU2_BRIDGE_TIMEOUT_MS') ?? DEFAULT_TIMEOUT_MS)
  return {
    url: url.replace(/\/+$/, ''),
    token: variable(env, 'TAU2_BRIDGE_TOKEN'),
    tools: toolsPath === null ? [] : parseTools(readFileSync(toolsPath, 'utf8')),
    policy: policyPath === null ? '' : readFileSync(policyPath, 'utf8'),
    timeoutMs: Number.isFinite(rawTimeout) && rawTimeout > 0 ? rawTimeout : DEFAULT_TIMEOUT_MS,
  }
}

/**
 * Forward one tool call to the orchestrator's bridge.
 * @param {{url: string, token: string|null, timeoutMs: number}} config - bridge settings.
 * @param {string} id - tool call id recorded in the tau2 trajectory.
 * @param {string} name - tool name.
 * @param {object} args - tool arguments.
 * @param {AbortSignal} [signal] - cancellation of the model step.
 * @returns {Promise<string>} the tool message content.
 */
export async function callBridge(config, id, name, args, signal) {
  const timeout = AbortSignal.timeout(config.timeoutMs)
  const combined = signal === undefined ? timeout : AbortSignal.any([signal, timeout])
  let response
  try {
    response = await fetch(`${config.url}/call`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...(config.token === null ? {} : { authorization: `Bearer ${config.token}` }),
      },
      body: JSON.stringify({ id, name, arguments: args ?? {} }),
      signal: combined,
    })
  } catch (error) {
    throw new Error(`tau2 bridge unreachable at ${config.url} (${String(error?.message ?? error)})`)
  }
  const payload = await response.json().catch(() => null)
  if (!response.ok || payload === null || typeof payload !== 'object') {
    throw new Error(`tau2 bridge answered HTTP ${response.status} for ${name}`)
  }
  // A failed tool is reported the way tau2 reports it to the official agent:
  // as the tool's own error text, not as a harness failure.
  return String(payload.content ?? '')
}

/**
 * Build the DSH tool definition for one tau2 tool.
 * @param {{name: string, description: string, parameters: object}} tool - tau2 function schema.
 * @param {(id: string, name: string, args: object, signal?: AbortSignal) => Promise<string>} call - transport.
 * @returns {object} tool definition.
 */
export function tau2ToolDefinition(tool, call) {
  return {
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    // The official orchestrator executes tool calls one at a time, in order.
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const id = typeof exec?.callId === 'string' && exec.callId.length > 0
        ? exec.callId
        : randomUUID()
      return call(id, tool.name, args ?? {}, exec?.signal)
    },
  }
}

/**
 * Plugin entry.
 * @param {object} ctx - Cordis context.
 * @param {object} [config] - plugin configuration (unused; the bridge is configured by environment).
 */
export function apply(ctx, config = {}) {
  const bridge = loadBridgeConfig(process.env)
  if (bridge === null) {
    ctx.logger?.info?.('dsh-tau2-bridge: TAU2_BRIDGE_URL is not set; no tau2 tools are registered')
    return
  }
  const service = (name) => (typeof ctx.get === 'function' ? (ctx.get(name) ?? ctx[name]) : ctx[name])

  const prompt = service('systemPrompt')
  if (prompt === undefined || typeof prompt.section !== 'function') {
    ctx.logger?.warn?.('dsh-tau2-bridge: ctx.systemPrompt.section() is unavailable; the domain policy is not mounted')
  } else {
    prompt.section({
      name: SECTION_NAME,
      order: SECTION_ORDER,
      text: `<instructions>\n${AGENT_INSTRUCTION}\n</instructions>\n<policy>\n${bridge.policy}\n</policy>`,
      // Policies and tool documentation may contain `{{…}}`; keep them literal.
      interpolate: false,
    })
  }

  const tools = service('tools')
  if (tools === undefined || typeof tools.register !== 'function') {
    throw new Error('dsh-tau2-bridge: ctx.tools.register() is unavailable; cannot expose the tau2 tools')
  }
  const disposers = bridge.tools.map((tool) => tools.register(tau2ToolDefinition(tool, (id, name, args, signal) => callBridge(bridge, id, name, args, signal))))
  ctx.logger?.info?.(`dsh-tau2-bridge: registered ${disposers.length} tau2 tool(s) through ${bridge.url}`)
  if (typeof ctx.effect === 'function') {
    ctx.effect(() => () => {
      for (const dispose of disposers) {
        try {
          dispose?.()
        } catch {
          // disposal is best-effort
        }
      }
    })
  }
}

export default apply
