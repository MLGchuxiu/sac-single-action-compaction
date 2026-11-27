# @local/dsh-tau2-bridge

让一个 DSH agent 作为被测对象跑在**官方 τ²-bench 编排器**里。编排器（任务、用户模拟器、环境、评测）原封不动地跑在
τ² 的进程里；DSH 这一侧只做两件事：

1. 把 τ² 的领域工具注册成 DSH 原生工具。每次调用都经本机 HTTP 桥转发给编排器进程，由它在真实环境上执行
   （走官方路径 `environment.get_response`），把「工具调用 + 工具结果」这对官方消息写进轨迹，再把结果返回给 DSH；
2. 把 τ² 的 agent 指令和领域 policy 挂成 system prompt 段落。这样 policy 出现在每一次请求里（与官方 agent 一致），
   也不占用户消息的位置，不会被 SAC 折叠。

完整用法见 [`docs/TEST-PLAN-TAU2-MULTITURN.md`](../../docs/TEST-PLAN-TAU2-MULTITURN.md)；
驱动这一侧的 agent 实现在 [`harness/tau2/dsh_agent.py`](../../harness/tau2/dsh_agent.py)。

## 配置：全部走进程环境变量

每次 DSH 调用由 `harness/tau2/run.py` 启动，环境变量按调用注入，所以并发的多个仿真（各自一个 DSH 进程）互不可见。

| 变量 | 含义 |
|---|---|
| `TAU2_BRIDGE_URL` | 桥的地址，如 `http://127.0.0.1:52341`。**不设置时插件空闲**，不注册任何工具 |
| `TAU2_BRIDGE_TOKEN` | Bearer 令牌，桥会校验 |
| `TAU2_BRIDGE_TOOLS` | JSON 文件 `[{name, description, parameters}]`：领域工具的官方 OpenAI function schema |
| `TAU2_BRIDGE_POLICY` | 可选，领域 policy 文本文件 |
| `TAU2_BRIDGE_TIMEOUT_MS` | 单次调用超时，默认 120000 |

## 行为

- 工具名、描述、参数 schema 原样透传，模型看到的和官方 `llm_agent` 看到的一致。
- 工具失败（环境报错）按 τ² 的方式作为工具自己的错误文本返回，不算 harness 故障。
- 桥不可达时明确抛错。
- 工具标记为不可并发；测试 profile 还把 `maxParallelToolCalls` 设成 1，与官方编排器逐个执行一致。
- policy 段落设置 `interpolate: false`，policy 里的 `{{…}}` 保持原样。

## 指令文本

官方 `LLMAgent` 每一步只能发一条消息或一次工具调用；DSH 的一个回合可以先连续调用多次工具再回复，而且只有回合最后的
文本会送到模拟用户手里。所以指令在官方文本的基础上多了这一句说明，见 `index.js` 的 `AGENT_INSTRUCTION`。

## 测试

```bash
node --test test/bridge.test.js
```

4 个测试，用本地 HTTP 桩验证配置读取、policy 段落、工具注册与转发、错误路径；不调用模型。
