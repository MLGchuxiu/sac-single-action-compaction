# τ²-bench 多轮测试：SAC 插件 1.2.0（保留 4 条请求）

> **状态：测试环境和文档已经备好，真实测试一次都没有跑。** 按要求，真实运行留给新会话。
> 本文所有写着「已验证」的条目，都是不调用模型、不花 token 的离线检查（见第 11 节）。
> 日期：2026-11-26 · 被测版本：插件 1.2.0，`maxPinnedInstructions: 4`，git 提交 `dfa3b6a`

## 0. 一页摘要

| 项 | 内容 |
|---|---|
| 被测对象 | `@local/dsh-sac-compact` 1.2.0，每条用户指令钉住，默认保留最近 4 条 |
| 备份 | git 提交 `dfa3b6a`；目录快照与压缩包见第 2 节 |
| 测试数据 | τ²-bench v1.0.1 的 184 题 golden set：airline 50、retail 114、telecom 20 |
| 跑法 | 官方 `tau2 run`，任务、用户模拟器、环境、评测全部不变；被测 agent 换成一个 DSH 会话（`--agent dsh_agent`） |
| 对照臂 | `tau2-sac`（SAC，保留 4 条）对 `tau2-fha`（同一 DSH agent，不挂 SAC，保留全部历史） |
| 新会话需要补的 | 用户模拟器和评审模型的密钥（第 6 节）；agent 模型默认是 GLM-5.3-Inner，可换 |
| 执行顺序 | 预检 → 离线自检 → 试跑（每域 5 题）→ 全量 184 题 → 分析 |

---

## 1. 测试目的

1. **插件在真实多轮对话里守不守得住自己的规则。** 用户模拟器每说一句就是 DSH 会话里新的一轮。要检查：用户消息从不被折叠；超过 4 条后最旧的请求进归档且被逐字引用；状态更新不失败。
2. **任务成功率有没有被 SAC 拖低。** 和不挂 SAC 的同一 agent 比，三个域的平均奖励。
3. **代价。** 每步上下文大小、模型步数、token 用量，按成功任务折算。
4. **行为层面。** 代理是否回应用户最新一句话，而不是早先的目标。这一条主要靠成功率和抽样阅读对话，见 8.4。

这不是和公开榜单比分数的测试：agent 模型、系统提示、用户模拟器模型都与官方设定不同（第 4.3 节），只有本测试台各臂之间可以互相比。

---

## 2. 被测对象与备份

| 项 | 值 |
|---|---|
| 插件源码 | `plugins/dsh-sac-compact/`、`plugins/dsh-sac-preset/` |
| 备份提交 | `dfa3b6a`（`git show --stat dfa3b6a`），不含两个会话 zip |
| 目录快照 | `work/bench/plugin_versions/multiturn-1.2.0-pin4/`（含 `dsh-sac-compact` 和 `dsh-sac-preset`） |
| 压缩包 | `work/bench/plugin_versions/multiturn-1.2.0-pin4.tar.gz` |
| SHA-256 | `bc4ff6c254e9d98d80f660683c47654a0c917e861f162198c19375ea2636f2a7` |
| 快照上的测试 | 78 / 78 通过 |
| 关键配置 | `statePrompt: paper`、`actionsPerCompaction: 2`、`pinInstructions: true`、`maxPinnedInstructions: 4`、`instructionChars: 4000` |

测试 profile `tau2-sac` 链接的是工作区里的 `plugins/dsh-sac-compact`。预检会逐文件比对它与快照的哈希，工作区被改动后会报错。

恢复备份：

```bash
git checkout dfa3b6a -- plugins/dsh-sac-compact plugins/dsh-sac-preset
# 或者
tar -xzf work/bench/plugin_versions/multiturn-1.2.0-pin4.tar.gz -C <目标目录>
```

想让测试钉在一个不会变的副本上，生成 profile 时指向快照：

```bash
work/tau2/venv/bin/python -m harness.tau2.make_profiles \
  --sac-path work/bench/plugin_versions/multiturn-1.2.0-pin4/dsh-sac-compact
```

---

## 3. 测试数据

τ²-bench v1.0.1（提交 `fc0055d`）的 184 题 golden set，与 NVIDIA 论文用的一致：

| 域 | split | 题数 | policy 字符数 | agent 工具数 | 用户工具 | 奖励依据 | 需要评审模型 |
|---|---|---|---|---|---|---|---|
| airline | base | 50 | 7,676 | 14 | 无 | DB 终态 + 传达信息 | 否 |
| retail | base | 114 | 6,699 | 16 | 无 | 112 题 DB + 自然语言断言，2 题仅 DB | **是** |
| telecom | small | 20 | 23,318 | 13 | 30 个，由用户模拟器调用 | 18 题环境断言，2 题加动作检查 | 否 |

每道题是一次完整对话：LLM 扮演的用户按剧本逐步交代信息，直到目标达成后发出 `###STOP###`。对 SAC 来说，这意味着每个任务里有多个用户轮次，且用户会补充、纠正信息。telecom 域的用户自己也有工具（查信号、开关飞行模式等），对话里产生的事实必须保留到终态。

---

## 4. 跑法

### 4.1 保持官方的部分

任务数据、`user_simulator` 及其全局指引文件、领域环境与工具、半双工编排循环（开场白、终止条件、`max_steps` 200、`max_errors` 10）、评测类型 `ALL`、指标计算 `compute_metrics`、命令行选项、随机种子 300、结果文件格式，全部是 τ²-bench 原码：

```bash
tau2 run --domain <域> --task-split-name <split> --agent <agent> --agent-llm <模型> \
         --user-llm <模型> --num-trials 1 --max-concurrency 3 --save-to <名字>
```

本测试台用 `python -m harness.tau2.run run …` 包了一层，只多做一件事：在官方命令行构建之前把 `dsh_agent` 注册进去。其余选项照旧。

### 4.2 被测 agent：一个 DSH 会话

```
τ² 进程（官方代码）                              DSH 进程（headless）
┌──────────────────────────────────┐          ┌──────────────────────────────┐
│ 任务 → 用户模拟器 → 编排器 → 环境 │  用户消息 │ system prompt（τ² policy）    │
│              ↑ 评测               │ ───────▶ │ 模型 → τ² 工具 → 模型 → … → 回复│
│                                  │ ◀─────── │                              │
└──────────────────────────────────┘  最终回复 └────────────────────────────────┘
         ▲                                                  │
         └──────────── 本机 HTTP 桥：工具调用 ─────────────────┘
```

- τ² 的一个「agent 回合」= DSH 的一个「turn」。用户模拟器每说一句，`DshAgent` 就用这句话跑一次 `dsh --profile tau2-sac --json`，之后各轮用 `--session-id` 接着同一个会话。
- 模型调用 τ² 工具时，DSH 侧的桥接插件把调用转发给 τ² 进程。τ² 进程走它自己的执行路径（`orchestrator._execute_tool_calls`），把「工具调用 + 工具结果」按官方消息格式写进轨迹，再把结果返回给 DSH。数据库改动和错误计数与官方 agent 完全一样。
- DSH 这一轮最后的文本回复返回给 τ²，作为 agent 的话送到用户模拟器。模型在调用工具的同时说的旁白不会送给用户。
- τ² policy 挂在 DSH 的 system prompt 里，每次请求都在，不占用户消息的位置。

### 4.3 与官方 `llm_agent` 的差异

| 差异 | 原因 | 影响 |
|---|---|---|
| agent 模型是 DSH profile 路由到的模型 | 被测的是 DSH + SAC | 分数不能和公开榜单比 |
| 系统提示多了 DSH 的身份句和角色句；官方指令按 DSH 的多步回合改写了说明（工具调用时附带的话不送给用户） | DSH 回合是多步的 | 小 |
| 一个 agent 回合里可以先调多次工具再回复；官方 agent 每步只能做一件事 | DSH 的工作方式 | 步数记法：每次工具调用记 2 步，每次回复记 1 步 |
| 模型与工具调用同时输出的旁白不送给用户 | 官方协议规定一步只能发消息或调工具 | 无 |
| 用户模拟器、评审模型可能不是官方默认的 gpt-4.1 | 本机没有 OpenAI 密钥 | 各臂之间仍可比 |

---

## 5. 环境（已准备好）

| 组件 | 位置 | 状态 |
|---|---|---|
| Python 3.12 虚拟环境，装有 tau2 1.0.1（可编辑安装）、litellm 1.82.6 | `work/tau2/venv/` | 已验证可导入 |
| τ²-bench 源码与数据 | `benchmarks/tau2-bench/` | 已验证 184 题可加载 |
| 用户模拟器全局指引（本次从官方仓库补下载，逐文件校验 git blob 哈希） | `benchmarks/tau2-bench/data/tau2/user_simulator/` | 已验证 |
| DSH headless profile `tau2-sac`（SAC，保留 4 条）与 `tau2-fha`（无 SAC） | `work/dshhome/profiles/` | 已验证可组合 |
| DSH 桥接插件 `@local/dsh-tau2-bridge` | `plugins/dsh-tau2-bridge/` | 4 个单元测试通过 |
| DSH home（工作区本地的，不是 `~/.dsh`） | `work/dshhome/` | 已验证 |

脚本都在 `harness/tau2/`：

| 脚本 | 作用 | 是否调用模型 |
|---|---|---|
| `preflight.py` | 预检：τ²、数据、DSH、profile、插件源码与备份是否一致、单元测试、密钥是否就位 | 否 |
| `selftest_offline.py` | 离线自检：桩 DSH + 官方编排器；再加真实 DSH 运行时 + 假 LLM | 否 |
| `make_profiles.py` | 生成 DSH profile | 否 |
| `run.py` | 官方 `tau2` 命令行，注册了 `dsh_agent` | 是（真实运行时） |
| `run_matrix.sh` | 按臂 × 域批量执行 | 是（`DRY_RUN=1` 时只打印） |
| `analyze.py` | 官方指标 + SAC 不变量 + 成本 | 否 |
| `envinfo.py` | 数据与工具清单 | 否 |

### 5.1 两个 profile

两个 profile 除了是否挂 SAC 之外完全相同：

- 底座是官方的 `dsh-base` + `dsh-headless`，加桥接插件；`tau2-sac` 再加 SAC bundle。SAC 的配置就是 bundle 自带的，`maxPinnedInstructions: 4`。
- **封闭工具集**：shell、文件、web、子代理、todo、goal、skill、工作区指令、向用户提问的工具、LLM 起标题等 29 项全部禁用。模型只看得到 τ² 的 policy 和 τ² 的领域工具，和官方 `llm_agent` 拿到的信息一致。
- 工具串行执行（`maxParallelToolCalls: 1`），与官方编排器逐个执行一致。
- 角色句改为 customer service agent。

### 5.2 换 agent 模型

profile 默认走 JMS 代理上的 GLM-5.3-Inner。换模型时重新生成：

```bash
work/tau2/venv/bin/python -m harness.tau2.make_profiles \
  --provider <provider id> --model <model id> --base-url <OpenAI 兼容地址> \
  --api-key-env <存放密钥的环境变量名> --max-tokens <上限>
```

`--api-key-env` 指向的变量要在环境里，或在 `work/dshhome/.credentials.yaml` 的 `refs:` 里。`run.py` 启动时会把 `refs:` 里缺的变量导出，不会打印值。

---

## 6. 新会话需要补的东西

真实运行需要两类模型，都要在运行前配好：

**agent 模型**：已就绪。profile 指向 `JMS_PROXY_API_KEY`，预检确认该变量在凭据存储里。

**用户模拟器模型，以及 retail 的评审模型**：目前没有配置。τ² 通过 litellm 调用，默认模型名是 `gpt-4.1-2025-04-14`，需要 `OPENAI_API_KEY`。可选做法：

- 有 OpenAI 密钥：`OPENAI_API_KEY` 放进 `benchmarks/tau2-bench/.env`。τ² 启动时会读这个文件，该路径已被 git 忽略。
- 用 OpenAI 兼容的第三方端点：同一个 `.env` 里写 `OPENAI_API_KEY`、`OPENAI_BASE_URL`，模型名用 `openai/<model>` 的形式传给 `--user-llm`。任何 OpenAI 兼容端点上的模型都可以（例如 qwen3.7-flash、deepseek-v4.1-flash）；密钥只放在 `.env` 里，不要写进仓库。
- retail 的评审模型写死在 τ² 的 config 里，用环境变量 `TAU2_JUDGE_LLM` 覆盖，例如 `openai/qwen3.7-flash`。评审模型必须直接输出 JSON，不能带代码围栏或推理过程；试跑时留意 retail 是否报 JSON 解析错误。

telecom 的用户模拟器要自己调工具，所选模型必须支持 function calling。

---

## 7. 执行步骤

以下命令都在仓库根目录执行。

### 第 0 步：预检（不调用模型）

```bash
work/tau2/venv/bin/python -m harness.tau2.preflight
```

输出 `GO` 才继续。它会检查：τ² 能导入且 184 题能加载；DSH 命令行可用；两个 profile 能组合且配置正确；工作区的 SAC 插件与备份快照逐文件一致；78 + 4 个单元测试通过；密钥是否就位（只看变量名）。密钥缺失只是警告，其余任何一项失败都是阻塞。

### 第 1 步：离线自检（不调用模型）

```bash
work/tau2/venv/bin/python -m harness.tau2.selftest_offline --with-dsh
```

用桩程序和假 LLM 把整条链路走一遍，包括真实 DSH 运行时。预期 34 项全过。产物在 `work/tau2/selftest/run_*/`。

### 第 2 步：试跑

每个域 5 题、两个臂、并发 1。目的是在花大钱之前量出单题 token 和耗时，并确认三件事：用户模拟器能正常对话；retail 的评审模型能返回可解析的 JSON；DSH 回合都正常退出。

```bash
export TAU2_JUDGE_LLM='openai/<评审模型>'        # 用官方 gpt-4.1 时不用设
TAG=pilot NUM_TASKS=5 CONCURRENCY=1 \
  USER_LLM='openai/<用户模拟器模型>' bash harness/tau2/run_matrix.sh
```

跑完自动生成 `work/tau2/reports/pilot.md`。看这几项：

| 看什么 | 在哪 | 不能接受的情况 |
|---|---|---|
| DSH 非正常结束的回合数 | 表中一行 | 大于 0 |
| 状态更新失败数 | 表中一行 | 大于 0 |
| 被折叠的用户消息数 | 表中一行，应为 0 | 大于 0 |
| DSH 会话日志 | 「找到的 DSH 会话日志 / 缺」 | 有缺失 |
| token / 成功任务 | 表中一行 | 用来推算全量预算 |

有不能接受的情况，先读 `work/tau2/runs/<域>__<题>__<id>/turn<N>.stderr.log` 和同目录的 `turn<N>.jsonl`。

### 第 3 步：全量 184 题

```bash
TAG=full CONCURRENCY=3 USER_LLM='openai/<用户模拟器模型>' bash harness/tau2/run_matrix.sh
```

结果在 `benchmarks/tau2-bench/data/simulations/full_{sac,fha}_{airline,retail,telecom}/results.json`，报告在 `work/tau2/reports/full.md`。脚本带了 `--auto-resume`，中断后重跑同一条命令会接着做。

### 第 4 步（可选）

- **保留 1 条的设置**：`work/tau2/venv/bin/python -m harness.tau2.make_profiles --with-pin1` 生成 `tau2-sac-pin1`，再 `ARMS="sac fha sac-pin1" TAG=full3 …`。
- **多个 trial**：`TRIALS=3`，报告里会多出 pass^k。
- **与官方 `llm_agent` 同模型对照**：agent 模型能通过 litellm 调用时，用官方命令行加 `--agent llm_agent --agent-llm openai/<模型>` 各跑一遍，可量化第 4.3 节差异的大小。

### 第 5 步：分析

```bash
work/tau2/venv/bin/python -m harness.tau2.analyze \
  benchmarks/tau2-bench/data/simulations/full_{sac,fha}_{airline,retail,telecom} \
  --out work/tau2/reports/full.md
```

想单独看某个会话里 SAC 的行为。会话 id 在结果文件里（agent 每条回复的 `raw_data.dsh.session_id`），`analyze.py --json` 也会列出每个任务的运行目录：

```bash
# 找到并解压这个会话的日志，打印 SAC 概况（步数、折叠、被折叠的用户消息、上下文大小）
work/tau2/venv/bin/python -m harness.tau2.session_log <session-id>
node harness/session_replay/replay.mjs work/tau2/sessions/<session-id>.jsonl new
python3 harness/session_replay/objectives.py work/tau2/sessions/<session-id>.jsonl
```

---

## 8. 指标与判读

### 8.1 官方指标

来自 τ² 的 `compute_metrics`：平均奖励、pass^k、DB 终态一致数、结束原因分布。

### 8.2 SAC 不变量（从 DSH 会话日志读）

| 指标 | 含义 | 要求 |
|---|---|---|
| 被折叠的用户消息数 | 被工作状态折叠或回合收尾折叠吞掉的用户消息。归档折叠吞掉但已逐字引用的不算 | **0** |
| 折叠次数 / 仿真 | SAC 折叠的总次数 | 信息 |
| 归档折叠数 | 超过 4 条用户消息后触发的归档次数 | 信息。对话长时会大于 0，属于正常 |
| 状态更新失败数 | `compaction/end` 带错误 | 接近 0 |
| 上下文 token：均值、峰值 | 每个模型步的 surface 大小（字符数 / 3.5），不含 system prompt 和工具 schema | 均值应在数千 token 量级 |

### 8.3 代价

模型步数、输入输出 token、按成功任务折算的 token 和步数、墙钟。token 来自 DSH 的用量统计，只含 agent 一侧，不含用户模拟器和评审。

### 8.4 判定标准

| 问题 | 通过条件 |
|---|---|
| 插件规则守住了吗 | 所有会话里被折叠的用户消息数为 0；每个会话日志都能找到 |
| 运行稳定吗 | DSH 非正常结束的回合数为 0；状态更新失败数为 0 |
| 成功率掉了没有 | `full_sac` 的平均奖励不低于 `full_fha` 减去噪声。184 题在成功率 70% 附近的标准误约 3.4 个百分点，1 个 trial 下差距小于 7 个百分点不能算差异；要下结论请用 `TRIALS=3` |
| 代价可接受吗 | token / 成功任务与 FHA 同量级 |
| 行为对了吗 | 成功率之外，每个域抽 5 个失败和 5 个成功的 SAC 会话，读对话，确认代理回应的是用户最新一句。发现「还在做早先目标」的，记下会话 id，用 `objectives.py` 看当时的 Objective 行 |

---

## 9. 成本与时间

没有真实数据，下面是量级估算，以试跑实测为准。

- **每步输入**：airline 的 system prompt 8.4K 字符、工具 schema 11K 字符，是每次请求都带的固定开销，约 5K token；再加上下文里 SAC 保留的部分（离线自检里均值约 2.5K token）。
- **每题**：用户轮数乘以每轮的模型步数，再加每两步一次的状态更新，估计 20–60 步，即 0.2–0.8M 输入 token。
- **全量**：每臂 184 题约 35–150M 输入 token，两个臂翻倍。用户模拟器每个用户轮次调用一次，带完整对话，估计每臂 15M 左右。
- **时间**：DSH 每轮启动开销约 1.5 秒，其余是模型延迟。并发 1 时每臂可能要十几到二十小时，并发 3 是官方默认值。

---

## 10. 风险与注意事项

1. **DSH home**。在 DSH 会话里环境自带 `DSH_HOME=~/.dsh`。测试台一律强制使用 `work/dshhome`，并清掉继承的 `DSH_*` 变量。手工调用 `dsh` 时务必自己设 `DSH_HOME`，否则会动到真实的 profile 和会话目录。
2. **失控保护**。编排器的 `max_steps` 和 `max_errors` 在 DSH 回合内部由桥接层代为执行：达到上限就结束这一轮 DSH，编排器随后记录官方的终止原因。另外每轮工具调用上限 80 次、墙钟上限 1800 秒。开发自检时假 LLM 曾经陷入死循环，10 分钟发了 3 万次请求，这层保护就是为此加的。
3. **并发**。每个仿真有独立的桥接端口、令牌、工作目录和 DSH 会话，`--max-concurrency 3` 是安全的。并发 3 以上的真实运行没有验证过。
4. **结果位置**。`--save-to` 的名字对应 `benchmarks/tau2-bench/data/simulations/<名字>/`，该目录被 git 忽略。重名会触发续跑，换名字或删掉旧目录。
5. **凭据**。`.env` 已加入 `.gitignore`。`run.py` 导出的密钥只在进程环境里；结果文件和 `analyze.py --json` 的输出都不含密钥，但不要把环境变量打印进报告。
6. **headless 的时序**。DSH 的 pre-step 发生在用户消息写入之前，所以上一轮的收尾折叠出现在新一轮的开头、新消息到达之前。这是 1.2.0 的既定行为，离线自检里能看到。

---

## 11. 本次已完成的验证

全部是离线检查，没有调用任何真实模型。

| 检查 | 结果 |
|---|---|
| 备份：快照、压缩包、SHA-256、与工作区逐文件一致 | 通过；快照上 78 / 78 |
| `tau2 check-data`、184 题加载（50 / 114 / 20） | 通过 |
| 补下载的 11 个官方文件（4 个用户模拟器指引、4 个 agent 示例、`.env.example`、`.python-version`、`uv.lock`） | 逐文件 git blob SHA-1 校验通过 |
| 桥接插件单元测试 | 4 / 4 |
| 离线自检第一部分：桩 DSH + 官方编排器 + 官方评测 | 11 项通过 |
| 离线自检第二部分：真实 DSH 运行时 + `tau2-selftest` profile + 假 LLM | 23 项通过 |
| 预检 | `GO`，34 项通过，1 个预期警告（模拟器密钥未配置） |

离线自检第二部分证实的事：

- 真实 DSH 用生成的 profile 能启动，发出的 system prompt 里有 τ² 的指令和 policy，没有 plan mode 的内容。
- 暴露给模型的工具恰好是 14 个 τ² airline 工具，没有多余的。
- 工具调用经桥接由官方环境执行，结果回到模型；三次 SAC 状态更新都以 lean 模式发出（不带 system prompt）。
- 同一个 DSH 会话跑了两轮用户消息，SAC 折叠 3 次，没有一次折到用户消息。
- 最终 surface 的形状是：system · 请求 1 · 状态 1 · 回复 1 · 请求 2。
- 走官方批量运行器（`run_tasks`）时以上同样成立，`analyze.py` 能读出结果文件和会话日志。
- SAC 不变量检查在旧日志上能抓到原问题：e64b 标出 4 条被折叠的后续请求，d2ac 标出 4 条被折叠的目标消息；在新插件的会话上为 0。

---

## 12. 未验证项与已知局限

- **任何真实模型的运行。** 包括 agent 能否在 τ² 工具上正常工作、用户模拟器的对话质量、评审模型的 JSON 输出、实际的 token 和耗时。
- **超过 4 条用户消息时的归档路径**，只在单元测试和 e64b 的离线仿真里走过，没有在真实运行时触发过。τ² 对话通常多于 4 个用户轮次，全量运行会第一次真正走到它。
- **`plan-mode` 在这个 DSH 版本里无法用 patch 禁用。** 它只在会话切到 plan mode 时才生效，离线自检确认它的文本没有到达模型。
- **离线自检只覆盖 airline 域**。retail 和 telecom 的工具 schema 走的是同一条导出路径，但没有单独跑过。
- **DSH 侧的 token 统计**在假 LLM 下是固定值，真实模型的用量字段是否完整，要在试跑时确认。
