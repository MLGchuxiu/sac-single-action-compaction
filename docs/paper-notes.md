# 论文精读笔记：*You don't need long context* (arXiv:2611.05306)

> Peter Belcak, Saurav Muralidharan, Jindong Jiang, Yonggan Fu, Wonmin Byeon, Hanrong Ye,
> Ryan Angilly, Ali Taghibakhshi, Shih-Yang Liu, Siru Ouyang, Greg Heinrich, Pavlo Molchanov
> NVIDIA，2026-11-03，v1。本地副本：[`paper.pdf`](../paper.pdf)，提取文本：[`paper.txt`](../paper.txt)。

## 1. 一句话结论

**长上下文不是必需的**：把上下文频繁压缩（compaction）到 16K–50K 量级，长程任务与长输入任务的
准确率几乎不掉；甚至"每个上下文只做一个动作"（SAC）也能保持任务完成能力。作者据此提出
**语义状态空间模型（SSSM, Semantic State Space Model）** 的概念。

## 2. 动机（第 1 节）

- 生成 token 的成本随**上下文深度**线性增长（注意力机制）。在 256K 窗口下，GLM 5.2 / Kimi K2.6 /
  Qwen 3 Coder Next 的每 token 成本分别是 16K 窗口的 **3.6× / 5.6× / 7×**（Figure 1、Figure 4）。
- 本地部署（DGX Spark）上，后段 token 的生成延迟约为前段的 **2×**（Figure 2）。
- 但模型窗口仍在膨胀：Table 1 里 750K–1.05M 窗口已超过《战争与和平》（约 753K token）等整本书。

## 3. 术语（2.1 节，实现时的契约）

| 术语 | 定义（论文原意） |
|---|---|
| inference step | 一次 LM 调用；agent 场景通常等于一个 agent turn |
| input context | 本次调用可见的全部 token：system、任务、历史模型输出、工具结果 |
| output context | 本次调用生成的 token（含 thinking、tool call） |
| long context | 接近当代前沿模型窗口（≈256K+）的上下文 |
| short context | 远小于窗口，本文的操作区间约 **16K–50K** |
| long-input task | 任务输入本身很长（LCW2） |
| long-horizon task | 需要很多顺序动作、历史不断累积（LCW1） |

## 4. 三种上下文管理范式（2.4.1 节，Figure 3）——本项目的实现依据

### FHA（Full-History Accumulation）
单一会话无限追加，是 mini-swe-agent / mini-agent 的默认行为。上下文随步数线性增长。

### EoCC（End-of-Conversation Compaction）
跑到**压缩阈值**时，在会话内要求模型写一份有界的"交接摘要"，然后**新开一个会话**：
`system + 原任务 + 摘要`，继续执行。论文用 `总上下文上限 / 压缩阈值` 表示配置，例如 `16K/12K`。

### SAC（Single-Action Compaction）——本项目的主角
> "Each conversation receives the task and current semantic state, performs exactly one
> environment action, observes its result, and updates the semantic state. The harness then
> discards that conversation and starts a fresh one for the next action."

即每个上下文里只有：**system + 任务 + 当前语义状态 → 一个动作 → 观察结果 → 更新状态**，
然后丢弃会话。语义状态是**自然语言**的（可解释），这是 SSSM 与 SSM 的关键区别。

论文给出的两个指标：**total length**（各段会话最终长度之和）与 **peak length**（单次调用最大上下文）。

## 5. 关键实验结果

### RQ1 长程任务（6 个 benchmark：SWE-bench Verified / Multilingual、Terminal-Bench 2.1、FRAMES、τ²-bench、BFCLv3）

- Opus 4.5 相对基线 **−3.7 ~ +5.6 pp**；GPT 5.6 Sol **−3.3 ~ +4.4 pp**；两者在 22 个阈值配置中
  各自最多 5 个配置掉超过 2pp。
- 成本节省 **2.2×–8.8×**（Opus）/ **2.4×–5.0×**（GPT）。

### RQ2 长输入任务（LongBench v2、LOONG、AA-LCR、NIAH、RULER、Counting-Stars、BABILong）

- 做法：**把长输入落到文件系统**，用 `grep/head/sed` 做**选择性阅读**（selective input reading），
  把长输入任务降维成长程任务；compaction 只作为兜底。
- 结果普遍**持平或显著提升**：NIAH 60→100%，RULER 62.5→95.7%（Opus），BABILong +24.9pp；
  成本节省 **6.5×–54.6×**（Opus）、**4.4×–233.3×**（GPT）。
- 关键观察：文档变长时**对话长度基本不随文档长度增长**（Figure 7/8），说明"任务真正需要的信息量很小"。

### RQ3 单动作压缩（Table 2，SWE-bench Verified / Terminal-Bench 2.1）

| 模型 | mini 基线 | 最佳 EoCC | **SAC** | 峰值上下文 mini | EoCC | **SAC** |
|---|---|---|---|---|---|---|
| Claude Opus 4.5 | 70.0 / 47.2 | 74.8 / 52.8 | **70.2 / 51.8** | 32K/200K, 27K/130K | 19K/22K, 24K/31K | **8K/16K, 9K/19K** |
| GPT 5.6 Sol | 84.4 / 87.6 | 88.8 / 88.8 | **82.4 / 84.3** | 18K/173K, 17K/133K | 22K/35K, 23K/40K | **7K/15K, 8K/37K** |

结论：SAC 把峰值上下文从 **130–200K 压到 7–37K**，Opus 持平/略升，GPT 掉 2–3.3pp。
这正是"压缩频率可以推到极端而不崩溃"的证据。

## 6. 论文自己承认的失效模式（3.1 节，做实现时必须正视）

- Compaction 是**有损**的；对已压缩过的历史再压缩，损失会叠加。
- 反例（抛硬币）：如果任务需要"第 i 次抛硬币的结果"，任何有界压缩最终都会丢信息 → 必然失败。
- 论文的解法不是"更大的预算"，而是：**把每条消息都存档，并给 agent 一个选择性读取历史的工具**。
  这样"查历史"就变成了 RQ2 的长输入任务。
- 即：区分"必须进上下文的"与"可按需检索的"（need-to-know）。

## 7. 实现映射：论文 → 本项目

| 论文要素 | 本项目实现 |
|---|---|
| SAC 每动作后压缩 | `plugins/dsh-sac-compact`（DSH 插件，`agent/pre-step` 驱动 `compactRegion`） |
| 语义状态模板（附录 G.1） | 插件 `statePrompt: paper`；harness `STATE_V1` 逐字复刻 |
| 选择性输入阅读 | harness 工具 `read_file(offset,limit)` / `grep` + 大输出 spill 到文件 |
| 历史存档 + 检索（3.1 的解法） | harness `recall` 工具 + `TranscriptArchive`（v4 迭代） |
| 上下文上限 / 压缩阈值 | harness 硬约束 `130,172` 输入 / `8,192` 输出；EoCC 默认阈值 96K |
| peak / total context 指标 | `peak_prompt_tokens`（动作请求）/ `total_prompt_tokens` |
| 成本倍数 | 用 token 数直接对比（未乘单价） |

## 8. 我对论文的两点批判性观察（写进报告）

1. **SAC 的隐藏成本是"重复劳动"**：论文提到 total token spend 未必下降（附录 C 讨论），但没有给出
   "动作数/请求数"的对照。本项目的 harness 显式记录 `requests`、`actions`、`compactions`，
   可以量化"为了短上下文多付了多少次调用"。
2. **SAC 对"状态模板"高度敏感**：论文只给了一份模板，没有做模板消融。本项目的 4 轮迭代正是
   围绕状态模板（v1→v4）做消融，用数据说明模板质量比"压缩频率"更影响准确率。