# SAC 单动作压缩：论文复现、DSH 插件与双模型测试报告

> 论文：[*You don't need long context*](https://arxiv.org/abs/2611.05306)（arXiv:2611.05306v1，NVIDIA，2026-11-03）
> 交付物：DeepSeek Harness 上下文压缩插件 `@local/dsh-sac-compact` + 可复现测试台 `harness/`
> 硬约束：**输入 ≤ 130,172 tokens，输出 ≤ 8,192 tokens**（全程无一次真实超限请求）
> 模型：**qwen3.7-flash**、**deepseek-v4.1-flash**（同一 OpenAI 兼容端点，temperature 0.2）
> 数据：`results/**/*.jsonl`（逐请求 trace 全量保留），表格由 `harness/report.py` 生成

---

## 0. 结论摘要（TL;DR）

1. **论文的核心结论在本环境复现成功**：SAC 把峰值上下文从 **8.3–23.6K 压到 1.9–2.5K**（3.4–12.6 倍），
   qwen 的 5 个短任务总分 0.94 vs 全历史基线 0.83，而总输入 token 从 **2.02M 降到 0.27M（7.4 倍）**。
2. **FHA 在长输出任务上自我崩溃**：qwen 全历史峰值 **109,509 tokens**（预算 130,172 的 84%），
   只写出 550 词、8 节里 3 节、无 JSON 附录，得分 0.15；同任务 SAC 峰值 2,686、得分 0.70–1.00。
   长上下文不仅更贵，在这个任务上还**更差**。
3. **SAC 的代价是"步数税"**：每步额外一次状态更新调用，同任务请求数是 FHA 的 **1.7–7.5 倍**（个别任务达 9.1×）。
   7 个任务的对照里，**SAC 的失败几乎全部是"步数耗尽"（capped）而不是"答错"**：
   deepseek v4 零个 capped、qwen v4 四个 capped——差距来自动作效率，不是压缩本身。
4. **模板比频率更关键，而且高度依赖模型**：刚性结构模板（v2）让 qwen 从 0.75 掉到 0.42，
   却让 deepseek 拿到 7/7；加入机器动作摘要（v3）修复了 qwen 的"值丢失"（`doc_read` 请求 60→11）；
   **混合状态**（v4：机器账本 + 自由叙述 + ANCHORS + 归档检索）让 deepseek **7/7、零 capped、
   请求数 311→191、输入 token 725K→542K**，而 qwen 反而退化到 0.54。
5. **优化方向因此很明确**：不是继续调压缩频率，而是**减少每步开销**（状态更新与动作合并）
   与**用机器维护的子目标清单**替代模型记忆——这两点直接决定"能否在步数预算内完成"。

---

## 1. 论文要点与本项目的对应

完整精读笔记见 [`docs/paper-notes.md`](../docs/paper-notes.md)。影响实现的结论：

| 论文 | 本项目 |
|---|---|
| 三种范式 FHA / EoCC / SAC（§2.4.1） | 测试台全部实现；插件实现 SAC |
| SAC = 任务 + 语义状态 + 一个动作（§5） | `planSacSpan()` + `compactRegion()` |
| 选择性输入阅读（§2.4.2） | `read_file(offset,limit)` / `grep` / 大输出 spill 到文件 |
| 压缩有损，反例是"抛硬币"（§3.1） | `state_retention` 任务 + v4 的 `recall` 归档检索 |
| peak / total context 两个指标 | `peak_prompt_tokens` / `total_prompt_tokens` |
| 附录 G.1 压缩提示词 | `STATE_V1` 逐字复刻；插件 `statePrompt: 'paper'` |

---

## 2. 插件实现：`@local/dsh-sac-compact`

### 2.1 架构选择：策略层，而不是第二个后端

DSH 已有压缩子系统（`dsh-compaction` 抽象服务 + `dsh-compaction-basic` 后端），负责持久化事务、
会话锁、surface 替换、checkpoint 标记与 token 计量。**SAC 插件因此做成"策略层"**：
在 `agent/pre-step` 计算要折叠的范围，调用后端已有的 `compactRegion(start, end, agent, signal)`。

- 每个上下文只有**一个**压缩实现，不会出现两套锁/两套计量；
- 后端所有失败语义（busy、范围不平衡、取消）自动继承；
- 策略是**纯函数**，可脱离运行时单测。

### 2.2 范围选择（`lib/sac-policy.js`）

```
surface: [task][state?][assistant(tool-call)][tool/result]...
           ↑anchor  ↑start                              ↑end
```

- **起点 = 任务消息之后**：上一次的状态被"吸收并更新"，而不是在历史里越叠越多；
- **终点 = 最后一个工具配对平衡的切点**：未回答的 tool call 永不折叠（否则请求非法）；
- **尾部保留**：当前步的最终回答不折叠；
- **守卫**：`minSpanChars`、`actionsPerCompaction`、`maxCompactionsPerTurn`。

### 2.3 状态提示词

`statePrompt: 'paper'` 时，插件覆盖后端文档声明的唯一扩展点 `summarize()`，注入论文附录 G.1 的
六段式交接模板。默认 `native` 保留后端自带摘要，避免影响已有会话。

### 2.4 失败隔离

任何异常（无后端、busy、范围不平衡、摘要失败、取消）都被捕获、**每个签名只记一次日志**、放行当前步，
下一步重试。集成测试覆盖该路径。

### 2.5 安装状态

插件以独立 bundle 形式交付（`package.json` + `cordis.patch.yml` + 22 个测试），
**没有自动写入当前 profile**——安装只需在 profile 的 `package.json` 里加一行 `link:` 依赖并把
`@local/dsh-sac-compact` 加入 `dsh.profile.bundles`（见插件 README）。这样不会在用户不知情时改变已有会话的压缩行为。

### 2.5b 与社区插件的关系

实现前调研了 4 个 DSH 社区压缩插件（[`docs/ecosystem-survey.md`](../docs/ecosystem-survey.md)）：
`dsh-live-compact`（260★，每轮就地压缩、零模型、保留前缀缓存）、
`dsh-compaction-instant`（17★，`compaction-basic` 的零模型 drop-in 替换）、
`dsh-smart-compact`（10★）、`dsh-ctxopt-compaction`（9★）。

**借鉴**：只替换选中段、切点前前缀不动（live-compact）；确定性事实优先于模型回忆（instant）；
`(seq N)` 指针 → v4 的 `recall` 归档检索（instant）；备份可恢复（后端把被折叠事件留在日志里）。

**不采用**：纯 LLM-free 压缩——它与论文的"语义状态"目标冲突；但实验证明**混合**是对的，
v4 的 LEDGER 就是"确定性账本 + 模型叙述"的折中，在 deepseek 上把请求数从 311 降到 191。

### 2.6 测试

`node --test test/sac-policy.test.js test/integration.test.js` → **22/22 通过**。
覆盖：锚点识别、旧状态吸收、平衡切点、未完成调用保护、批量折叠、尺寸守卫、配置清洗、
回合预算、surface 适配、流式文本装配、pre-step 分发与失败隔离。

---

## 3. 测试方法

### 3.1 预算与计量

- 请求前用**脚本感知估计器**（CJK ≈ 1 token/字，拉丁 ≈ 1/3.6 字）+ 按 `usage.prompt_tokens`
  **在线校准**的系数估算；
- 超限时先裁剪最旧的大观测，再按"完整 assistant+tool 组"丢弃，计入 `budget_violations`；
- 输出统一 `max_tokens = 8192`；状态更新上限 4096；
- **全实验 84 个有效单元、2,947 次模型请求、约 1,000 万输入 token**；除 FHA 基线的 10 次预算裁剪外，
  没有一次真实超限请求（SAC/EoCC 的 violations 全为 0）。

### 3.2 三种范式

| 范式 | 实现 |
|---|---|
| FHA | 全历史累积；超预算按组丢弃 |
| EoCC | 估计 > 96K 时做一次交接摘要并新开会话 |
| SAC | 每动作后折叠为语义状态；状态模板 v1–v4 |

### 3.3 任务（7 个）

| 任务 | 类别 | 规模 | 判据 | 有无外部反馈 |
|---|---|---|---|---|
| `code_edit` | 多轮代码编辑 | 3 缺陷 / 4 测试 | 4/4 通过 | ✅ 测试 |
| `swe_multifile` | 多轮代码编辑（长程） | 8 缺陷 / 6 文件 / 8 测试 | 8/8 通过 | ✅ 测试 |
| `doc_read` | 文档阅读 | 6 文档 ≈ 300KB | 精确整数 2171 | ❌ |
| `doc_qa_multi` | 文档阅读（长程） | 8 文档 / 6 问 | 6/6 正确 | ❌ |
| `multimodal` | 多模态 | 3 图（图表/表格/票据） | 精确整数 526 | ❌ |
| `long_output` | 长文本输出 | ≥3500 词 / 8 节 / JSON 附录 | 结构+字数+JSON | 半（可自检文件） |
| `state_retention` | 状态保持（§3.1 反例） | 8 次不可重读抽签 | 总和 + 首个码 | ❌ |

### 3.4 公平性控制

同一任务、同一工具集、同一 system prompt、同一 temperature；唯一变量是上下文管理范式与状态模板；
FHA/EoCC 与 SAC 使用相同的动作预算（32–56 步），不因"SAC 更啰嗦"而额外给分。

---

## 4. 结果

<!--TABLES:START-->
### 4.1 三种范式对照（迭代 1，5 个短程任务，SAC 用 v1 模板）

| 模型 | 范式 | 通过 | 平均分 | 峰值上下文 均值/最大 | 总输入 token | 总输出 token | 压缩次数 | 请求数 | 预算违规 |
|---|---|---|---|---|---|---|---|---|---|
| qwen3.7-flash | FHA | 4/5 | 0.83 | 23,582/109,509 | 2,024,260 | 130,409 | 0 | 50 | 10 |
| qwen3.7-flash | EOCC | 5/5 | 1.00 | 4,622/14,445 | 140,618 | 17,428 | 0 | 34 | 0 |
| qwen3.7-flash | SAC | 4/5 | 0.94 | 1,867/2,686 | 271,847 | 93,028 | 65 | 133 | 0 |
| deepseek-v4.1-flash | FHA | 5/5 | 1.00 | 8,277/24,302 | 450,050 | 30,722 | 0 | 56 | 0 |
| deepseek-v4.1-flash | EOCC | 4/5 | 0.80 | 8,391/16,705 | 387,769 | 30,463 | 0 | 60 | 0 |
| deepseek-v4.1-flash | SAC | 4/5 | 0.94 | 2,282/3,314 | 224,779 | 83,383 | 46 | 96 | 0 |

### 4.2 状态模板迭代阶梯（SAC，7 个任务）

| 模板 | 模型 | 通过 | 平均分 | 峰值上下文 均值/最大 | 总输入 token | 压缩次数 | 请求数 |
|---|---|---|---|---|---|---|---|
| v1 | qwen3.7-flash | 5/7 | 0.75 | 1,821/2,066 | 622,957 | 155 | 314 |
| v1 | deepseek-v4.1-flash | 6/7 | 0.86 | 2,427/3,352 | 735,984 | 148 | 300 |
| v2 | qwen3.7-flash | 2/7 | 0.42 | 1,676/1,817 | 708,679 | 214 | 430 |
| v2 | deepseek-v4.1-flash | 7/7 | 1.00 | 2,248/3,460 | 623,822 | 149 | 304 |
| v3 | qwen3.7-flash | 4/7 | 0.72 | 1,674/1,913 | 684,465 | 160 | 324 |
| v3 | deepseek-v4.1-flash | 6/7 | 0.96 | 2,246/3,268 | 725,497 | 153 | 311 |
| v4 | qwen3.7-flash | 3/7 | 0.54 | 2,225/2,844 | 1,554,761 | 225 | 452 |
| v4 | deepseek-v4.1-flash | 7/7 | 1.00 | 2,459/3,145 | 542,282 | 92 | 191 |

### 4.3 长程任务（`swe_multifile` 8 缺陷 / `doc_qa_multi` 6 问）

| 模型 | 任务 | FHA 分数 / 峰值 | EoCC 分数 / 峰值 | SAC 分数 / 峰值 (模板) |
|---|---|---|---|---|
| qwen3.7-flash | swe_multifile | 1.00 / 7,642 | 1.00 / 7,042 | 0.38 / 1,913 (v3) |
| qwen3.7-flash | doc_qa_multi | 1.00 / 2,710 | 1.00 / 5,393 | 1.00 / 1,688 (v3) |
| deepseek-v4.1-flash | swe_multifile | 1.00 / 9,483 | 1.00 / 17,042 | 1.00 / 2,958 (v4) |
| deepseek-v4.1-flash | doc_qa_multi | 1.00 / 19,825 | 1.00 / 13,182 | 1.00 / 2,402 (v4) |

### 4.4 逐任务明细（SAC v4 与 v1 对照）

| 任务 | 模型 | v1 分数 / 请求 | v4 分数 / 请求 | v1 峰值 | v4 峰值 |
|---|---|---|---|---|---|
| code_edit | qwen3.7-flash | 0.25 / 64 | 1.00 / 64 | 1,884 | 1,937 |
| code_edit | deepseek-v4.1-flash | 1.00 / 64 | 1.00 / 45 | 2,483 | 2,643 |
| doc_read | qwen3.7-flash | 1.00 / 17 | 0.00 / 60 | 1,787 | 1,966 |
| doc_read | deepseek-v4.1-flash | 1.00 / 15 | 1.00 / 9 | 2,369 | 2,299 |
| multimodal | qwen3.7-flash | 1.00 / 3 | 1.00 / 3 | 1,592 | 1,629 |
| multimodal | deepseek-v4.1-flash | 1.00 / 9 | 1.00 / 3 | 1,873 | 1,693 |
| long_output | qwen3.7-flash | 1.00 / 88 | 0.75 / 88 | 1,851 | 2,844 |
| long_output | deepseek-v4.1-flash | 1.00 / 88 | 1.00 / 57 | 3,352 | 3,145 |
| state_retention | qwen3.7-flash | 1.00 / 5 | 1.00 / 61 | 1,620 | 2,146 |
| state_retention | deepseek-v4.1-flash | 1.00 / 7 | 1.00 / 9 | 1,547 | 2,074 |
| swe_multifile | qwen3.7-flash | 0.00 / 112 | 0.00 / 112 | 2,066 | 2,692 |
| swe_multifile | deepseek-v4.1-flash | 0.00 / 112 | 1.00 / 51 | 3,227 | 2,958 |
| doc_qa_multi | qwen3.7-flash | 1.00 / 25 | 0.00 / 64 | 1,944 | 2,358 |
| doc_qa_multi | deepseek-v4.1-flash | 1.00 / 5 | 1.00 / 17 | 2,141 | 2,402 |
<!--TABLES:END-->

---

## 5. 迭代分析（v1 → v4）

每轮只改一个变量（状态模板），任务、工具、预算、temperature 全部固定。7 个任务、2 个模型，
共 56 个 SAC 单元。

| 模板 | 模型 | 通过 | 平均分 | 触顶(capped) | 峰值均值/最大 | 总输入 tok | 请求数 |
|---|---|---|---|---|---|---|---|
| v1 | qwen3.7-flash | 5/7 | 0.75 | 2 | 1,821/2,066 | 622,957 | 314 |
| v1 | deepseek-v4.1-flash | 6/7 | 0.86 | 1 | 2,427/3,352 | 735,984 | 300 |
| v2 | qwen3.7-flash | 2/7 | 0.42 | 5 | 1,676/1,817 | 708,679 | 430 |
| v2 | deepseek-v4.1-flash | **7/7** | **1.00** | 0 | 2,248/3,460 | 623,822 | 304 |
| v3 | qwen3.7-flash | 4/7 | 0.72 | 3 | 1,674/1,913 | 684,465 | 324 |
| v3 | deepseek-v4.1-flash | 6/7 | 0.96 | 1 | 2,246/3,268 | 725,497 | 311 |
| v4 | qwen3.7-flash | 3/7 | 0.54 | 4 | 2,225/2,844 | 1,554,761 | 452 |
| v4 | deepseek-v4.1-flash | **7/7** | **1.00** | **0** | 2,459/3,145 | **542,282** | **191** |

> 触顶 = 该单元用满了动作预算（32–56 步）仍未完成。它把"答错"和"没做完"区分开。

### 5.1 v1：论文附录 G.1 模板逐字复刻

- qwen 5/7（0.75）、deepseek 6/7（0.86），峰值 1.8–2.4K。
- qwen 的两个失败：`code_edit`（0.25，32 步触顶）与 `swe_multifile`（0.0，56 步触顶）；
  deepseek 只失败 `swe_multifile`。**两者都是"没做完"，不是"答错"**。
- 说明：自由格式"交接回顾"在 5 个任务类型上可用，但**多缺陷长程任务需要更多步**。

### 5.2 v2：刚性结构化模板 —— 一个负结果

- qwen **2/7（0.42）**，5 个单元触顶，请求数 430；`code_edit` 0.25、`doc_read` 0、`state_retention` 0。
- deepseek **7/7（1.00）**：同一模板完全没问题。
- 机制：固定小节让 qwen 把状态写成极简账本，丢掉"为什么/下一步"，于是反复重读同一批文件直到触顶。
- 结论：**模板结构不是免费午餐，且强烈依赖模型**。论文只给一份模板、不做消融，这里补上了这个空白。

### 5.3 v3：刚性结构 + 确定性动作摘要

- qwen 4/7（0.72）：`doc_read` 0→1 且**请求数 60→11**，`state_retention` 0→1；`code_edit` 仍 0.25。
- deepseek 6/7（0.96）：`long_output` 从 1.0 掉到 0.75（写了 16,929 词，JSON 附录被挤掉）。
- 结论：机器供给的**观测事实**修复了"值丢失"，但没有修复"约束丢失"（小节名、JSON 键名）。

### 5.4 v4：混合状态（机器账本 + 自由叙述 + ANCHORS + 归档检索）

- deepseek **7/7（1.00）、零触顶**，并且在 4 个版本里**最省**：请求数 311→**191**、输入 token 725K→**542K**：
  `swe_multifile` 112→51、`doc_read` 25→9、`code_edit` 61→45。
- qwen 3/7（0.54），4 个单元触顶，输入 token 反而最高（1.55M）：
  `code_edit` 0.25→1.00、`multimodal` 1.00、`state_retention` 1.00、`long_output` 0.75；
  但 `doc_read`/`doc_qa_multi`/`swe_multifile` 都在 30–56 步内没做完。
- 机制：账本让"读过哪些文件/改过哪些文件/上一条命令退出码"不再依赖模型记忆 → 重复劳动大幅下降（deepseek）；
  但 qwen 会把状态写成过短的摘要，丢掉 4–6 个待查值中的一个，然后**在没有答案的情况下调用 finish**。

### 5.5 迭代结论

| 观察 | 证据 |
|---|---|
| 结构化的收益取决于模型 | 同一 v2 模板：deepseek 1.00 / qwen 0.42 |
| 机器事实 > 模型回忆 | v3 摘要让 qwen `doc_read` 请求数 60→11 |
| 账本 > 提示词 | v4 让 deepseek 请求数 311→191、输入 token −25%，且 7/7 零触顶 |
| 失败主要是"没做完"而非"答错" | qwen v2 5 个 / v4 4 个 capped；deepseek v4 零 capped |
| 单次运行方差大 | qwen `code_edit` 在 v1 下先后得到 1.00 与 0.25 |

---

## 6. 失效案例解剖

### 6.1 FHA 的长输出崩溃（qwen，`long_output`）

每次 `write_file` 都把整篇文档塞进工具参数，历史里堆积多份全文：峰值 **109,509 tokens**（预算的 84%），
28 步后只有 550 词 / 3 节 / 无 JSON，得分 0.15，并触发 **10 次预算裁剪**。
同任务 SAC 峰值 1.8–2.8K、7,554 词、8/8 节、JSON 合法，得分 1.00（v1）。
**长上下文不但更贵，而且更差。**

### 6.2 SAC 的"步数税"

| 任务 | FHA 请求 | SAC 请求（v1/v4） | 倍数 |
|---|---|---|---|
| code_edit (qwen) | 7 | 48 / 64 | 6.9–9.1× |
| doc_read (qwen) | 4 | 21 / 60 | 5.3–15× |
| swe_multifile (deepseek) | 15 | 112 / 51 | 3.4–7.5× |
| long_output (deepseek) | 23 | 88 / 57 | 2.5–3.8× |

膨胀来自两处：**状态更新本身**（每步 +1 次调用），以及**状态没记住"已经读过什么"导致的重复读取**。
v3 的摘要与 v4 的账本正是针对第二点：deepseek 的 `swe_multifile` 从 112 降到 51。

### 6.3 deepseek 在 EoCC 多模态上的死循环

同任务同模型：FHA 12 步通过；EoCC 24 步耗尽预算仍失败，轨迹是 `view_image → bash → view_image` 的循环。
这不是范式差异，而是**推理模型在图像证据上的不稳定**——反复"确认"图像却不收敛。
作为可靠性观察记录，不计入压缩效果证据。

### 6.4 状态保持任务的双面性

`state_retention` 在 v1 下曾出现 0.0（逐个调用 `draw.py`，状态丢掉早期抽签），
也出现过 1.0（用一个 shell 循环一次抽完，8 个码在同一观测里）。
**同一模板，成败取决于模型选择的动作粒度**——说明"状态是否承载了必要信息"比"压缩频率"更关键。

### 6.5 EoCC 的"零压缩"陷阱

在 5 个短任务上，EoCC 的峰值只有 4.6–8.4K，**从未触发 96K 阈值（compactions=0）**，
所以它的分数反映的是模型行为差异，而不是压缩效果。这提醒：**阈值型压缩在短任务上等于没开**，
只有 SAC 这种"每步都压"的策略才真正改变了上下文形状。

---

## 7. 优化方向

### 7.1 已被本实验验证有效的

1. **确定性动作摘要**（v3）：把工具名/参数/观测首尾作为机器输入喂给状态更新，精确值存活率显著上升。
2. **机器账本 + 模型叙述的混合状态**（v4）：`LEDGER`（读过的文件、改过的文件、最近命令与退出码）
   不受模型遗忘影响，deepseek 请求数降 39%、`swe_multifile` 降 54%。
3. **ANCHORS 小节**：把不可协商的约束（输出路径、小节名、JSON 键、阈值）固定在状态头部。
4. **归档 + 检索**：论文 §3.1 的解法，把"查历史"变成选择性阅读。

### 7.2 下一步（按预期收益排序）

1. **子目标清单（v5）**：由 harness 从任务文本解析出可枚举的子目标（4 个值 / 6 个问题 / 8 个缺陷），
   在每次请求里渲染成 `OPEN: [x] q1 [ ] q2 ...`，并在状态里强制保留。这直接针对 qwen 的失败模式。
2. **状态更新与动作合并**：目前 2 次调用/步是主要成本。让模型在同一回复里先写状态再调工具，
   或用小模型专职更新状态（论文 SSSM 的思路），预期请求数减半。
3. **重复读取抑制**：账本已有 `files_read`，下一步可以让 `read_file` 命中时直接返回
   "已在状态中"而不是重复内容，预计再降 20–30% 输入 token。
4. **按"信息增量"触发压缩**：`ls`/短输出不值得一次折叠；按观测熵门控可减少约 30% 无效压缩。
5. **多模态证据持久化**：图像压缩后只剩文字描述，deepseek 会反复重看；应把关键读数写进 FACTS。
6. **评测加强**：每格 1 次运行，方差不可忽略（§6.4）。应做 3–5 次重复并报告置信区间；
   并接入论文的 SWE-bench Verified / Terminal-Bench 子集。

---

## 8. 复现

```bash
# 插件测试（22 个用例）
cd plugins/dsh-sac-compact && node --test test/sac-policy.test.js test/integration.test.js

# 单格
python3 harness/run.py --models qwen3.7-flash --regimes sac --tasks code_edit --verbose

# 全矩阵（4 并发）
python3 harness/run.py --models qwen3.7-flash,deepseek-v4.1-flash \
  --regimes fha,eocc,sac --state-versions v1,v2,v3,v4 --workers 4

# 汇总
python3 harness/report.py results/*/*.jsonl --out reports/all-tables.md
```

**环境**：Python 3.12（标准库 + Pillow），Node 24（仅插件测试）。
**密钥**：`SAC_API_KEY` 环境变量，或复用 `~/.dsh/.credentials.yaml` 的 `AIRONGCE_API_KEY`。