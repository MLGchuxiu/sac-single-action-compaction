# SAC 单动作压缩：让 Agent 不再依赖长上下文

**简体中文** | [English](README.en.md)

> 复现并工程化论文 [*You don't need long context*](https://arxiv.org/abs/2611.05306)（arXiv:2611.05306，NVIDIA，2026-11）中最激进的上下文管理范式——**单动作压缩（Single-Action Compaction, SAC）**：每个上下文只保留「任务 + 当前语义状态 + 下一个动作」，动作完成即折叠进状态。
> 本仓库包含三样东西：**一个可安装的 DeepSeek Harness 插件**、**一套逐字复现论文机制的测试台**、以及**三个标准基准上的独立验证报告**。

| | |
|---|---|
| 论文 | Belcak et al., *You don't need long context*, arXiv:2611.05306（本地副本：[paper.pdf](paper.pdf) / [paper.txt](paper.txt)，精读笔记：[docs/paper-notes.md](docs/paper-notes.md)） |
| 插件 | [`plugins/dsh-sac-compact`](plugins/dsh-sac-compact/README.md)（v1.1.0，48 个测试）+ [`plugins/dsh-sac-preset`](plugins/dsh-sac-preset/README.md)（GUI 预设） |
| 测试台 | [`harness/`](harness/)：模型客户端、token 计量、工具沙箱、5 类任务、13 个状态模板版本 |
| 基准 | Loong / SWE-bench Verified / τ²-bench（[benchmarks/](benchmarks/README.md)，均已下载校验），4 轮迭代 + 128K 优化线 |
| 测试过的模型 | qwen3.7-flash、deepseek-v4.1-flash、GLM-5.3-Inner（即本地部署的 GLM 5.3） |
| 硬约束 | 模型输入上限 **130,172** tokens，输出上限 **8,192** tokens（复现线口径） |

---

## 1. 问题：长上下文的真实代价

Transformer 生成一个 token 的计算成本随它在上下文中的**深度线性增长**（注意力的固有性质）。这件事在零售价目表上是隐形的——API 按 token 统一计价——但在真实的 GPU 服务上真实存在：

- 论文 **Figure 1**（H100 按需价 $3.85/GPU·h，Nebius，2026-09）：以 **256K** 而非 **16K** 窗口服务时，每 100 万输出 token 的成本——GLM 5.2 为 **3.6×**，Kimi K2.6 为 **5.6×**，Qwen 3 Coder Next 为 **7×**。这个差距超过了许多正交优化手段（激进量化、结构化稀疏）。
- 论文 **Figure 2**（DGX Spark 本地部署）：后段 token 的生成延迟约为前段的 **2×**，且内存占用同步上升。
- 而模型窗口仍在膨胀（论文 Table 1：前沿模型 750K–1.05M，已超过整本《战争与和平》），agent 任务的上下文又随动作数线性增长——**窗口越大，填得越满，每个 token 越贵**。

论文的回应是一个反问：**这些窗口真的需要被填满吗？** 它的答案是否定的，并且把压缩频率推到了极端——单动作压缩——依然不崩溃。本项目把这个论断完整复现了一遍，并把它做成了可以日常安装使用的插件。

## 2. 原理：从「全历史」到「单动作」

### 2.1 三种上下文管理范式（论文 §2.4.1，Figure 3）

```
FHA   全历史累积（baseline，mini-swe-agent 默认）
      [任务][a1][r1][a2][r2][a3][r3][a4][r4] ……              ← 上下文无限增长

EoCC  会话末压缩（阈值触发）
      [任务][a1][r1][a2][r2] ──阈值──▶ [摘要]
      [任务][摘要][a3][r3][a4][r4] ──阈值──▶ [摘要'] ……      ← 上下文被锯齿状控制

SAC   单动作压缩（本项目主角）
      [任务][状态S0] [a1][r1] ▶ 更新为 S1
      [任务][状态S1] [a2][r2] ▶ 更新为 S2
      [任务][状态S2] [a3][r3] ▶ 更新为 S3 ……                 ← 每个动作后都折叠
```

（`a` = 动作/工具调用，`r` = 观察结果）

- **FHA（Full-History Accumulation）**：单一会话无限追加，上下文随步数线性增长，最终撞上窗口或预算。
- **EoCC（End-of-Conversation Compaction）**：到达压缩阈值时让模型写一份有界的交接摘要，然后**新开会话**（`system + 原任务 + 摘要`）继续。论文用「总上下文上限/压缩阈值」表示配置，如 `16K/12K`。
- **SAC（Single-Action Compaction）**：压缩频率的极限。论文原文：

  > "Each conversation receives the task and current semantic state, performs exactly one
  > environment action, observes its result, and updates the semantic state. The harness then
  > discards that conversation and starts a fresh one for the next action."

  即每个上下文里只有：**system + 任务 + 当前语义状态 → 一个动作 → 观察结果 → 更新状态**，然后整个上下文被丢弃。

### 2.2 语义状态：SAC 为什么能工作

SAC 赌的是一件事：**长程任务真正需要跨步骤携带的信息量很小**，且可以被自然语言紧凑表达。论文的 RQ2 实验侧面证实了这一点——把长输入任务降维成「选择性阅读」后，**对话长度基本不随文档长度增长**（论文 Figure 7/8）。

于是 SAC 用一份**语义状态（semantic state）**取代原始历史：

- 它是**自然语言**的——人可以直接读、可以 diff、可以审计。这是论文提出 **SSSM（语义状态空间模型）** 概念与经典 SSM 的关键区别：状态转移不是隐向量，而是可解释的文本。
- 它是**有界的**——本项目里默认 ≤2,048 tokens（自适应规则见 §4.3），不随任务步数膨胀。
- 它是**由模型自己维护的**——每次折叠时由模型把「旧状态 + 刚完成的动作 + 观察」归纳成「新状态」。

### 2.3 论文的实证结论（我们复现的目标）

- **RQ1（长程任务）**：Opus 4.5 相对基线 −3.7~+5.6 pp，GPT 5.6 Sol −3.3~+4.4 pp——频繁压缩几乎不伤准确率，成本节省 **2.2×–8.8×**。
- **RQ2（长输入任务）**：把长输入落到文件系统用 `grep/head/sed` 选择性阅读后，NIAH 60→100%、RULER 62.5→95.7%（Opus）、BABILong +24.9 pp，成本节省最高 **233×**。
- **RQ3（单动作压缩，论文 Table 2，SWE-bench Verified / Terminal-Bench 2.1）**：

  | 模型 | mini 基线 | **SAC** | 峰值上下文：mini → **SAC** |
  |---|---|---|---|
  | Claude Opus 4.5 | 70.0 / 47.2 | **70.2 / 51.8** | 32K, 27K → **8K, 9K** |
  | GPT 5.6 Sol | 84.4 / 87.6 | **82.4 / 84.3** | 18K, 17K → **7K, 8K** |

  SAC 把峰值上下文从 **130–200K 压到 7–37K**，Opus 持平甚至略升，GPT 只掉 2–3.3 pp。「压缩频率可以推到极端而不崩溃」由此得证。

### 2.4 论文自己承认的失效模式（§3.1，实现必须正视）

- **压缩是有损的**，对已压缩历史的再压缩会让损失叠加。
- 论文给出的反例（抛硬币）：若任务需要「第 *i* 次抛硬币的结果」，任何**有界**压缩最终都必然丢掉它——这不是预算问题，是信息论下界。
- 论文的解法不是更大的预算，而是**架构性的**：把每条消息都存档，并给 agent 一个**选择性读取历史的工具**，让「查历史」变成又一个长输入任务（即 RQ2 的降维）。区分「必须进上下文的」与「可按需检索的」（need-to-know basis）。

本项目的 `recall` 工具、`ledger` 账本正是这一解法的直接实现（§4.4）。

## 3. 这个仓库是什么

三件事，各自独立可用：

1. **工程化**：[`plugins/dsh-sac-compact`](plugins/dsh-sac-compact/README.md) —— 真实 DeepSeek Harness 插件。挂在 `agent/pre-step` 上，把 SAC 作为策略层叠加在已挂载的压缩后端之上，48 个单元/集成测试全绿。日常会话即装即用。
2. **复现**：[`harness/`](harness/) —— 论文机制的逐字复现与消融实验台。硬预算 130,172 输入 / 8,192 输出 tokens；三种范式（FHA/EoCC/SAC）同台对比；13 个状态模板版本（v1–v13）的演化记录（v10–v13 为研究性扩展，其验证任务数据未公开）。
3. **独立验证**：[`benchmarks/`](benchmarks/README.md) + [`reports/`](reports/BENCH-REPORT.md) —— 把插件放进真实 `dsh headless` 会话，在 Loong、SWE-bench Verified、τ²-bench 上与同模型 FHA 基线对比，共 4 轮迭代 + 一条 128K 优化线（12 任务 × n=5）。**全部数字可由仓库内脚本复现。**

## 4. 算法设计

### 4.1 总体架构：策略层，不是第二个压缩后端

```
agent/pre-step（每个模型请求前触发）
   │
   ├─ describeSurface(session)        把会话表面投影成有序节点描述
   │      [{seq, type, isCheckpoint, toolCallCount, textChars}, …]
   │
   ├─ planSacSpan(nodes, policy)      纯函数：决定本轮折叠范围
   │      → {startIndex, endIndex, actions, estimatedTokens} | null
   │
   └─ ctx.compaction.compactRegion(start, end, agent, signal)
          └─ 由已挂载的后端（dsh-compaction-basic）负责：
             持久化事务、锁、checkpoint 消息、token 计量
```

关键决策：插件**不实现第二个压缩后端**，而是通过后端的公开 API `compactRegion()` 驱动它。这样一个会话里永远只有一个压缩实现在活动，后端的全部持久化与并发保证原封不动地适用；插件只做**决策**（何时折、折哪里、状态怎么写）。

### 4.2 折叠范围规划 `planSacSpan()`（[lib/sac-policy.js](plugins/dsh-sac-compact/lib/sac-policy.js)）

这是 SAC 的核心算法，一个**纯函数**——输入是会话表面的节点描述，输出是折叠范围或 `null`。不依赖 Cordis、会话或 LLM，因此可以脱离运行时做穷举单元测试。四条规则：

1. **任务锚点（task anchor）**：范围从「第一条非 checkpoint 的用户消息」**之后**开始。锚点之前的内容（system、任务本身）永不折叠。同时这意味着上一次折叠写入的状态消息会被**吸收**（absorb）——新状态是「旧状态 + 新动作」的归并，而不是两份状态的堆叠。
2. **工具配对平衡切点（balanced cut）**：范围的右端只能落在「没有未应答工具调用」的边界上。扫描时维护 `openToolCalls` 计数（assistant 消息的每个 tool call +1，tool result −1），只有归零的位置才成为候选切点。**一条未收到回复的工具调用永远不会被折叠掉**——这是正确性的硬保证。
3. **批量与保留**：`actionsPerCompaction` 控制每次折叠吸收几个完整动作（1 = 论文的严格 SAC，2 = 本项目基准上的最优配置 SAC-2）；`keepTrailingReply` 把不带工具调用的最终回复留在折叠范围外（用户可见的答案不进状态）；`keepLastObservation` 把最新一条观察留在原文（终结「折掉了→再读一遍」的循环，代价是峰值上下文略高）。
4. **尺寸守卫与硬保险**：`minSpanChars` / `minSpanTokens`——状态重写本身要花几百 token，折叠太小的范围反而更大，直接跳过；`contextBudgetTokens`——整个会话表面逼近模型窗口（如 128K 配 96K）时**强制折叠**，作为配置失误的最后保险。

### 4.3 语义状态的生成：三种模式

| 模式 | 状态由谁写 | 额外调用 | 说明 |
|---|---|---|---|
| `native` | 后端默认摘要器 | 1 次/折叠 | 最保守，不改变会话内任何摘要行为 |
| `paper` | 论文附录 G.1 交接模板 | 1 次/折叠 | 六段式：Objective / Established facts / Applied changes / Verification / Dead ends / Remaining plan；本项目补了「输出契约」「逐字值」两节，防止状态改写关键数字 |
| `inline` | **模型自己**，在动作回复的开头写 `<state>…</state>` 块 | **0 次** | v9 管线。摘要钩子直接抽取该块做 checkpoint，省掉整次模型调用 |

**自适应预算**：状态 token 上限不是常数，而是 `min(stateBudget, 640 + 48 × foldedActions)`——任务早期事实少、状态小，折叠多了以后逐步放宽，避免「后期状态被早期的小预算卡死」。

**对推理模型的适配（实测发现）**：GLM-5.3-Inner 这类推理模型在带 tool-call 的回复里**不输出文本块**，`inline` 模式大面积失效。为此加入 `inlineFallbackAfter`——连续 N 次抽不到 `<state>` 块后**粘性回退**到 `paper` 模式，不再反复尝试。

### 4.4 让状态可审计的三件套（v9 管线，均可独立开关）

论文 §3.1 只给了「存档 + 检索」的方向（§2.4），这里把它落成三个具体机制：

- **ledger（账本）**：由**机器**（非模型）维护的折叠动作全量清单——工具名、完整参数、结果摘要，默认上限 2,400 字符，前置进每份状态。作用：模型无法**静默**丢掉一个动作；真实会话实验中，加上全量账本后状态保真度从 **0.19 升到 0.90**（[REPORT-2.md](reports/REPORT-2.md)）。
- **checklist（清单）**：任务开始时生成 3–8 条子目标，注册 `check(index)` 工具供模型自查打勾，清单随每份状态重渲染。对抗「忘了还剩什么没做」。
- **recall（召回）**：注册 `recall(query)` 工具，对**完整归档会话日志**（包括已被折叠掉的历史）做检索，返回带序号的片段。这正是论文「查历史 = 长输入任务」解法的直接实现——被折叠≠被删除。

### 4.5 Token 计量与硬预算（测试台）

复现线的每个请求都受**硬预算**约束：脚本感知的估计器（CJK ≈ 1 token/字，拉丁 ≈ 1 token/3.6 字）先估，再按服务端 `usage.prompt_tokens` **在线校准**系数。超限时的应急顺序：先裁最旧的超大观察，再按「完整 assistant+tool 组」丢弃，每次都计入 `budget_violations` / `emergency_trims` 指标。输出统一 `max_tokens=8192`。这样「峰值上下文」不是估计值，而是可审计的实测序列。

### 4.6 失败语义：SAC 永远不能弄坏对话

压缩是旁路优化，不是关键路径。任何失败——后端未挂载、锁忙、范围不平衡、摘要器报错、signal 中止——都被捕获、按消息去重后记录**一次**，然后本步带着未压缩的历史照常进行，下一步再重试。`maxCompactionsPerTurn`（默认 200）防止失控循环。每次折叠在会话日志里留一行可事后审计的记录：

```
dsh-sac-compact: folded 1 action(s) (seqs 12-15, ~812 tokens) into semantic state | folds=3 folded_tokens=2410 summaries=3 summary_chars=1902 installed=pre-step
```

另有 `telemetrySnapshot(agent)` 暴露全部计数器（steps / compactions / foldedTokens / skipped+lastSkipReason / installAttempts / summarizeCalls…）。

## 5. 相对论文的改进

论文证明了「可行」，本项目回答的是「**怎么做得好、什么时候会坏**」。以下每一条都是论文没做、或没给数据的：

1. **状态模板消融（v1–v13）**。论文只提供一份状态模板（附录 G.1），没有做模板敏感性分析。本项目的多轮版本（v1–v13）围绕模板做了系统消融（六段固定结构 → ACTION DIGEST → ledger/recall/adaptive/ANCHORS → checklist → inline → …），结论：**模板质量对准确率的影响大于压缩频率本身**（详见 [docs/VERSIONS.md](docs/VERSIONS.md) 的两条版本线）。
2. **inline 状态更新**。论文的 SAC 每个动作要付**两次**模型调用（动作 + 状态更新各一次）。v6 把状态更新并进动作调用（模型在回复开头自写 `<state>` 块）后：**请求数 −68%，输入 token −62%**，两个模型 8/8 任务全绿——把 SAC 最大的开销砍掉了大半。
3. **机器维护的账本**。论文只说「状态由模型维护」；实测模型会把动作**静默写丢**（真实会话保真度测试中 v1 仅 0.19）。加入确定性 ledger 后升到 **0.90**。这是「模型写状态」与「机器记账」的混合方案。
4. **recall 工具**。论文 §3.1 的失效模式分析（有损压缩的信息论下界）给出了解法方向但未实现；本项目的 `recall(query)` 直接落地，并验证了「折叠 ≠ 丢失」。
5. **批量折叠与观察保留的工程平衡**。严格 SAC（每动作一折）在 SWE 上导致 4.8× 墙钟；`actionsPerCompaction: 2` + `keepLastObservation` 把墙钟降到 1.6×，同时总 token 仍比 FHA **−36%**、峰值 **−55%**。论文没有探讨这个频率-成本-延迟的权衡面。
6. **128K 窗口的工程化四件套**（v1.1.0）：`stateInputLean`（状态更新请求不再重放 system prompt + 22 个工具 schema，实测每折省 ≈4.8K tokens）、`trimObservationOver`（只裁超大观察，短工具输出逐字保留）、`stateMaxWords`（recap 限长——它每个后续请求都要重发）、`contextBudgetTokens`（逼近窗口强制折叠）。合计：12 任务 × n=5，总 token **−58.6%**，峰值 **−61%**，加权得分还**略升**（§6.2）。
7. **成本显式化**。论文承认附录 C 讨论 total token 未必下降，但没给「多付了多少次调用」的对照。本测试台显式记录 `requests / actions / compactions` 三个计数，把「为了短上下文付出的重复劳动」变成可量化指标（Loong 严格 SAC：token +44% 但峰值 6.7K vs 29.3K——这类权衡在报告里全部摆明）。

## 6. 验证结果

> 全部结果可由仓库内脚本复现：`cd harness && python3 -m bench.matrix --bench loong,tau2,swe --arms sac,fha --iteration iterN --workers 4`。

### 6.1 标准基准线（GLM-5.3-Inner，真实 `dsh headless` 会话，4 轮迭代）

> 模型说明：**GLM-5.3-Inner 即本地部署的 GLM 5.3**（经本地网关以 OpenAI 兼容端点接入，1M 上下文窗口）。文中的推理模型行为结论均来自该部署。

| 基准 | 配置 | SAC vs FHA |
|---|---|---|
| Loong（长输入阅读） | 严格 SAC | 峰值 **6.7K vs 29.3K**，准确率 0.933 vs 0.80，总 token +44% |
| SWE-bench Verified（3 实例） | 严格 SAC | 3/3 vs 3/3，峰值 8.8K vs 14.1K，墙钟 4.8× |
| SWE-bench Verified | **SAC-2 + 保留最近观察**（iter4） | 3/3 vs 3/3，总 token **0.79M vs 1.23M（−36%）**，峰值 −55%，墙钟 1.6× |
| τ²-bench（静态计划变体） | SAC-2 | 0.70 vs 0.57（噪声内），折叠 0 次 |

报告：[BENCH-REPORT.md](reports/BENCH-REPORT.md)（含 v9 抽测与逐轮数据 [bench-all-iterations.md](reports/bench-all-iterations.md)）。

### 6.2 128K 优化线（2026-11-17，全量 12 任务 × n=5，120 格）

| 指标 | FHA（全历史） | 优化 SAC | 变化 |
|---|---|---|---|
| 任务加权均分 | 0.837 | **0.853** | +0.016 |
| 总 token | 17,744,686 | **7,348,882** | **−58.6%** |
| 峰值上下文（最大） | 129,575（3 次全量中 2 次 >128K 越界） | **50,385** | **−61%** |
| SWE 3 题 token | 14.97M | **4.82M** | −68%，峰值 130K→37K |
| Loong 5 题均分 | 0.848 | **0.885** | +0.037 |

报告：[BENCH-REPORT-128K.md](reports/BENCH-REPORT-128K.md)；逐轮记录：[work/goal_round1/ITERATIONS.md](work/goal_round1/ITERATIONS.md)。

### 6.3 复现线（qwen3.7-flash / deepseek-v4.1-flash）

迭代 1 的 5 任务速览（完整 7 任务 × 3 范式 × 4 轮迭代见 [REPORT.md](reports/REPORT.md)）：

| 模型 | 范式 | 通过 | 平均分 | 峰值上下文 | 总输入 token |
|---|---|---|---|---|---|
| qwen3.7-flash | FHA | 4/5 | 0.83 | 23.6K / **109.5K** | 2,024,260 |
| qwen3.7-flash | **SAC**（v1） | 4/5 | 0.94 | **1.9K** / 2.7K | **271,847** |
| deepseek-v4.1-flash | FHA | 5/5 | 1.00 | 8.3K / 24.3K | 450,050 |
| deepseek-v4.1-flash | **SAC**（v1） | 4/5 | 0.94 | **2.3K** / 3.3K | **224,779** |

第二轮（[REPORT-2.md](reports/REPORT-2.md)）把 SAC 放到极端场景下验证：

- **百万级输入**（992K tokens / 1,287 文件的编程任务）：所有范式都能用 **3–4K 上下文**解决——再次印证「长输入 ≠ 长上下文」；
- **真实工作会话回放**（原始数据不在本仓库）：FHA/EoCC 均无法守住 130K 硬预算，**只有 SAC 全程不超限**；机器命令账本把状态保真度从 0.19 提升到 **0.90**。

## 7. 快速开始

```bash
# 插件测试（48 个用例）
cd plugins/dsh-sac-compact && node --test test/sac-policy.test.js test/integration.test.js

# 单格测试
python3 harness/run.py --models qwen3.7-flash --regimes sac --tasks code_edit --verbose

# 完整矩阵（2 模型 × 3 范式 × 5 任务）
python3 harness/run.py --models qwen3.7-flash,deepseek-v4.1-flash \
    --regimes fha,eocc,sac --state-versions v1 --workers 4 --out results/iter1
python3 harness/report.py results/iter1/*.jsonl --out reports/iter1.md

# 标准基准矩阵（真实 dsh headless 会话）
cd harness && python3 -m bench.matrix --bench loong,tau2,swe --arms sac,fha --iteration iter0 --workers 6
```

**在 GUI 里按工作区启用**：装 [`plugins/dsh-sac-preset`](plugins/dsh-sac-preset/README.md)（独立预设，只在选定会话生效），新建会话时选「SAC 单动作压缩」即可，其它会话不受影响。验证方法：看会话日志出现 `compaction/start → compaction/summary → compaction/end` 与 `dsh-sac-compact: folded N action(s)` 行。

安装（profile 级）：

```jsonc
// ~/.dsh/profiles/<profile>/package.json
{
  "dependencies": { "@local/dsh-sac-compact": "link:/absolute/path/to/plugins/dsh-sac-compact" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@local/dsh-sac-compact"] } }
}
```

128K 窗口推荐配置（就是当前默认值，实测即最优）：`statePrompt: paper` + `actionsPerCompaction: 2` + `keepLastObservation` + `trimObservationOver: 4000` + `stateMaxWords: 400` + `stateInputLean` + `contextBudgetTokens: 96000`。全部 22 个配置项说明见[插件 README](plugins/dsh-sac-compact/README.md#configuration)。

## 8. 仓库结构

| 路径 | 内容 |
|---|---|
| [`paper.pdf`](paper.pdf) / [`paper.txt`](paper.txt) | 论文原件与提取文本 |
| [`plugins/dsh-sac-compact/`](plugins/dsh-sac-compact/README.md) | **SAC 插件**：策略层 + 状态管线 + 48 测试 |
| [`plugins/dsh-sac-preset/`](plugins/dsh-sac-preset/README.md) | GUI Agent 预设（按会话启用 SAC） |
| [`harness/sac/`](harness/sac/agent.py) | 复现测试台核心（agent 循环、prompts、任务、token 计量） |
| [`harness/bench/`](harness/bench/runner.py) | 三基准适配器 + 并行矩阵运行器 |
| [`benchmarks/`](benchmarks/README.md) | 论文同款测试集（SWE-bench Verified 500 / τ²-bench 184 / LOONG 1,600） |
| [`reports/`](reports/BENCH-REPORT.md) | 四轮基准报告 + 复现报告 + 128K 优化报告 |
| [`docs/`](docs/paper-notes.md) | 论文精读 / 版本表 / 基准方法论 / 生态调研 |
| [`work/`](work/goal_round1/ITERATIONS.md) | 128K 优化线逐轮数据 |

版本命名注意：**插件线**（plugin-iter0–iter6 / plugin-v9）与**测试台线**（harness-v1–v13）是两条独立的线，看版本号前先对 [docs/VERSIONS.md](docs/VERSIONS.md)。

## 9. 局限

- **有损是本质的**（论文 §3.1）：需要逐字历史回忆的任务（如「第 i 次抛硬币结果」）任何有界状态都会失败；必须配合 `recall` / 外部存档。
- 单个动作的观察本身超过窗口时，压缩救不了，需要配合观测裁剪类插件（如 `dsh-compaction-tool-result-pruner`）。
- `inline` 模式在推理模型上不可用（tool-call 回复无文本块），已自动粘性回退 `paper`。
- 严格 SAC 的墙钟代价（更多次、更小的请求）在 SWE 类任务上显著（4.8×），用 SAC-2 + `keepLastObservation` 可压到 1.6×，这是频率与延迟的权衡而非缺陷。
- τ²-bench 采用静态单轮变体（非官方多轮编排分数）；SWE-bench 实例选用共享 venv 可跑的纯 Python 子集。方法与局限详见 [docs/bench-methodology.md](docs/bench-methodology.md)。

## 10. 引用

论文：

> Belcak, P., Muralidharan, S., Jiang, J., Fu, Y., Byeon, W., Ye, H., Angilly, R., Taghibakhshi, A., Liu, S.-Y., Ouyang, S., Heinrich, G., & Molchanov, P. (2026).
> *You don't need long context.* arXiv:2611.05306. https://arxiv.org/abs/2611.05306

本仓库的全部实验、失效分析与插件实现均在上述论文公开机制的基础上完成。
