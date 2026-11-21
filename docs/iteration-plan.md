# 四轮迭代计划与判据

每轮只改**一个**变量，其余保持固定，用同一套任务与同一组预算（输入 130,172 / 输出 8,192）重跑，
以便把"准确率 / 上下文峰值 / 调用次数"的变化归因到该变量上。

| 轮次 | 变量 | 具体改动 | 预期 | 判据 |
|---|---|---|---|---|
| **R1** | 基线：论文附录 G.1 模板逐字复刻 | `STATE_V1`（Objective / Facts / Changes / Verification / Dead ends / Plan），SAC 每动作压缩 | 短任务与 FHA/EoCC 持平；`state_retention` 失败 | 拿到 FHA/EoCC/SAC 的峰值上下文与 token 账 |
| **R2** | 状态**结构** | `STATE_V2`：固定小节 FACTS/CHANGES/VERIFIED/DEAD_ENDS/NEXT，强制"精确值"；预算 2048；新增两个长程任务（8 缺陷多文件、6 问多文档），三种范式全跑 | `state_retention` 提升；长任务暴露 FHA 上下文膨胀 | 长任务上 FHA 峰值 ≥ 5× SAC；SAC 准确率不掉 |
| **R3** | 状态**输入** | `STATE_V3`：v2 的刚性结构 + **确定性动作摘要**（工具名、参数、观测首尾）注入状态更新 | 精确值丢失率下降 | `doc_read`/`state_retention` 通过且请求数下降 |
| **R4** | **混合状态**：机器账本 + 模型叙述 | `STATE_V4` 回到 v1 的自由格式（实验证明刚性结构伤 qwen），加 ANCHORS 小节、动作摘要、`recall` 归档检索，并在状态**前面拼接 harness 维护的 LEDGER**（读过哪些文件/改过哪些文件/最近命令与退出码） | 重复读取下降、精确值不再丢失 | 长程任务准确率 ≥ v1，请求数下降，峰值仍 < 8K |

## 对照设计

- **FHA**（全历史）是论文的 mini-swe-agent 基线；超过 130,172 时按"完整 assistant+tool 组"丢弃，
  并计入 `budget_violations`（这是基线在硬预算下的真实代价）。
- **EoCC** 阈值固定 96K（论文常见配置 96K/72K 的比例），压缩后新开会话。
- **SAC** 每动作压缩；`state_version` 是唯一在轮次间变化的变量。

## 任务矩阵

| 任务 | 类别 | 规模 | 判据 |
|---|---|---|---|
| `code_edit` | 多轮代码编辑 | 3 缺陷 / 4 测试 | 4/4 通过 |
| `swe_multifile` | 多轮代码编辑（长程） | 8 缺陷 / 8 测试 / 6 文件 | 8/8 通过 |
| `doc_read` | 文档阅读 | 6 文档 ≈ 300KB | 精确整数 2171 |
| `doc_qa_multi` | 文档阅读（长程） | 8 文档 / 6 问 | 6/6 正确 |
| `multimodal` | 多模态 | 3 图（图表/表格/票据） | 精确整数 526 |
| `long_output` | 长文本输出 | ≥3500 词 / 8 节 / JSON 附录 | 结构+字数+JSON 校验 |
| `state_retention` | 状态保持（论文 §3.1 反例） | 8 次不可重读抽签 | 总和 + 首个码 |

## 记录字段

每次运行记录：`ok/score`、`actions`、`requests`、`compactions`、`peak_prompt_tokens`（动作请求）、
`peak_request_tokens`（含状态更新）、`usage.prompt_tokens/completion_tokens/reasoning_tokens`、
`budget_violations`、`emergency_trims`、`state_tokens[]`、`wall_s`、逐请求 trace。