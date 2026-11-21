# 社区插件调研：DSH 生态里已有的上下文压缩方案

> 调研时间：2026-11-14。目的：在实现 SAC 插件前先看社区怎么做，能借鉴的直接借鉴，避免重复造轮子。
> 检索方式：GitHub API `search/repositories?q=deepseek+harness+compact` 与 `q=dsh-live-compact`。

| 项目 | ★ | 一句话 | 关键做法 |
|---|---|---|---|
| [`Tranquillitatis-Lab/dsh-live-compact`](https://github.com/Tranquillitatis-Lab/dsh-live-compact) | 260 | 边做边压：每轮结束时就地收短已完成的长输出 | **本机完成、不再请求模型**；只替换选中的一段，切点前的内容原样保留（**前缀缓存友好**）；改写前备份原文，备份失败就保留原内容；默认 2 万 token 才开始检查 |
| [`TsFreddie/dsh-compaction-instant`](https://github.com/TsFreddie/dsh-compaction-instant) | 17 | `compaction-basic` 的**直接替换**：零模型调用的确定性"会话编译器" | 毫秒级、**只用原始 token**（不改写、不臆造）；每个切点标注 `(seq N)` 指针，被裁内容可用 `recall(type:"result")` 取回；与官方后端事件/失败语义完全一致 |
| [`hoyyang/dsh-smart-compact`](https://github.com/hoyyang/dsh-smart-compact) | 10 | 上下文窗口自动管理 | 无损、**零模型**的 rollover；预算提醒；笔记 |
| [`chunsi-w/dsh-ctxopt-compaction`](https://github.com/chunsi-w/dsh-ctxopt-compaction) | 9 | DSH 上下文压缩插件 | 记忆 + prompt cache 视角 |

## 结论：社区的共识与分歧

**共识**

1. **不要另起一套压缩机制**：`dsh-compaction-instant` 明确做成 `compaction-basic` 的 drop-in 替换，
   复用同一 seam、事件与失败词汇。→ 我的插件更进一步：**不替换后端**，只作为策略层驱动后端的
   `compactRegion()`，因此永远只有一套锁/计量。
2. **确定性优先**：live-compact 与 compaction-instant 都强调"能不用模型就不用模型"——
   确定性改写没有幻觉、没有额外 token、毫秒级完成。
3. **切点前的内容必须原样保留**（前缀缓存）：这是所有方案的共同约束。

**分歧**

- **要不要模型参与摘要**：live-compact / instant 明确"零模型"；而论文的 SAC 语义状态**必须**由模型写出
  （自然语言状态是 SSSM 的核心）。两者其实是不同目标：前者追求"省"，后者追求"跨上下文的任务连续性"。
- **触发时机**：live-compact 每轮结束、instant 在阈值触发、SAC 每动作触发。

## 本项目借鉴了什么

| 借鉴点 | 来源 | 落地 |
|---|---|---|
| 只替换选中段、前缀不动 | live-compact / instant | `planSacSpan()` 从任务消息之后开始；后端 replace 保证 system+task 前缀不变 |
| 确定性事实（不靠模型回忆） | live-compact / instant 的"零模型"哲学 | **v4 的 LEDGER**：读过/改过的文件、最近命令与退出码由 harness 维护；v3 的动作摘要也是确定性文本处理 |
| `(seq N)` 指针 + 事后检索 | instant 的 `recall(type:"result")` | **v4 的 `recall` 工具**：对完整归档 transcript 做关键词检索（论文 §3.1 的解法） |
| 备份/可恢复 | live-compact 改写前备份 | 后端把被折叠的事件留在持久化日志里（surface 只是投影），因此天然可恢复 |
| 与官方后端契约一致 | instant 的 drop-in 思路 | 插件不实现第二套事务，直接复用 `compactRegion` 的 bracket/checkpoint/锁 |

## 没有采用的部分（及原因）

- **纯 LLM-free 压缩**：与论文的"语义状态"目标冲突。但实验（§5.4）证明**混合**是对的——
  v4 把确定性账本与模型叙述拼在一起，deepseek 上请求数 311→191、输入 token 725K→542K。
- **每轮结束压缩**：SAC 要求"每动作"，粒度更细；`actionsPerCompaction` 配置项允许退化成每轮。
- **默认 2 万 token 才触发**：论文的 SAC 是"每动作都压"，阈值型触发在短任务上等于没开
  （见报告 §6.5 的 EoCC 零压缩现象）。