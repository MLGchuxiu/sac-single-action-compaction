# 版本表

这个项目里有**两条互不相同的版本线**。看版本号前先确认是哪一条。

> 公开版说明：表里的 `work/…` 路径与提交号（如 `dfa3b6a`）属于开发机的本地仓库，未包含在本发布仓库中。逐版本的改动说明见 [CHANGELOG.md](../CHANGELOG.md)。

## A. 插件线（真实产品代码 `plugins/dsh-sac-compact`）

| 版本 | 代码位置 | 核心改动 | 测试 | 状态 |
|---|---|---|---|---|
| `plugin-iter0` | `work/bench/plugin_versions/iter0/` | session-fdac86bd 的 A1/A3 基线：懒安装 + 遥测 + paper 模板 | 27 | 历史基线 |
| `plugin-iter1` | `work/bench/plugin_versions/iter1/` | 空状态重试 + `minSpanTokens` | 29 | 已并入 |
| `plugin-iter2` | `work/bench/plugin_versions/iter2/` | `stateInputMaxChars` 裁剪 | 30 | **证伪**，默认 0 |
| `plugin-iter3` | `work/bench/plugin_versions/iter3/` | `keepLastObservation` | 31 | 已并入 |
| `plugin-iter4` | `work/bench/plugin_versions/iter4/` | 仅配置：`actionsPerCompaction: 2`（SAC-2） | 31 | 基准最优配置 |
| **`plugin-v9`** | `plugins/dsh-sac-compact/` | **inline 状态 + ledger + checklist + recall + 自适应预算**；版本化安装标记（热更新可重装） | 41 | 历史版本；inline 对 GLM-5.3-Inner 不适用（推理模型 tool-call 回复不带文本） |
| **`plugin-iter6`** | `plugins/dsh-sac-compact/`（2026-11-17） | 128K 目标优化：paper 模板加「输出契约 / 精确值」两节；`trimObservationOver`（只裁超大观测）；`stateMaxWords`（简短 recap）；`contextBudgetTokens`（逼近窗口时强制折叠）；inline 连续失败后粘性回退 paper（`inlineFallbackAfter`）；修复 `stateMaxTokens` 未被 `resolvePolicy` 识别的旧 bug；新测试 | **48** | 128K 优化线，即发布版 **1.1.0** |
| **`plugin-1.2.0`** | `plugins/dsh-sac-compact/`（2026-11-25） | **多轮修复**：每条用户指令（`user` / `goal`）都是锚点，只折叠指令之间的工作（`pinInstructions`、`instructionSourceKinds`、`maxPinnedInstructions`、`instructionChars`、`archiveMinTokens`）；状态更新以所属指令原文为锚，Objective 取自指令；输出契约未指定时写 `(none)`；归档状态逐字引用被释放的指令，重启后从 surface 读回；「不更小」的冻结/归档折叠最多重试两次 | **78** | 多轮修复已并入 1.3.0。单任务会话的折叠范围与 iter6 逐 span 相同（测试 `single-task surfaces plan exactly as before`）。取证：[`reports/REPORT-SAC-MULTITURN.md`](../reports/REPORT-SAC-MULTITURN.md) |
| **`plugin-1.3.0`** | `plugins/dsh-sac-compact/`（2026-11-27，当前） | **成本感知折叠**（全部可选，默认关闭 → 折叠决策与 1.2.0 逐次相同，已对 820 次日志决策重放验证）：按大小触发 + 滞回（`foldTrigger: size`、`foldAboveTokens`/`foldMarginTokens`、`foldBelowTokens`、`maxFoldsPerStep`）；收益门槛（`foldMinGainTokens`）；harness 写的摘要行折叠（`mechanicalFoldMaxTokens`、`mechanicalSegments: frozen`，JSON 结果按结构压缩而不是截断）；重的最终回复随轮折叠（`replyReasoningFoldChars`）；recap 按片段限长（`recapMaxRatio`，同时解除早折预算的二次截断）；截断检测与重试（`stateMaxTokensThinking`、`stateMaxTokensCeiling`）；独立摘要路由（`stateProvider`/`stateModel`）；goal 重投去重（`goalDedupe`）；工作区指令不进 recap（`standingSourceKinds`） | **108** | 当前。**出厂 bundle 配置即成本感知策略**（固定提示 + 32K / + 16K，摘要行只折已结束轮次，recap 按片段限长，思考模型首轮 4,096 上限）；键归零 = 1.2.0 行为（820 次日志决策逐次相同）。取证与帕累托对照：[`reports/REPORT-SAC-PARETO.md`](../reports/REPORT-SAC-PARETO.md) |
| **`preset-1.3.0`** | `plugins/dsh-sac-preset/`（2026-11-27，当前） | **预设对齐 1.3.0 出厂策略**：预设里 `sac-compact` 的配置补齐成本感知折叠的全部键，与压缩插件 bundle 的配置逐键相同（真实 `yaml` 解析器核对，键序也相同）。此前预设只有 1.1.0 / 1.2.0 的键，而代码默认值是 1.2.0 的行为，所以 GUI 里选 SAC 预设的会话一直按「每 2 个动作折叠一次」运行。新增 `test/preset-policy.test.js` 直接读取两个 YAML 做比较。离线更新包 `sac-deploy-kit-v1.3.0.zip` 挂在本仓库的 GitHub Release v1.3.0 下（由 `tools/build_sac_deploy_kit.py` 构建；端到端回归 `deploy/tests/kit_e2e.sh`，89 项）；更新前给客户端实装做快照的工具是 `tools/sac_client_backup.py` | **115**（108 + 7） | 当前。用真实 DSH 加载器合成 profile 树验证：`preset-sac` 的配置与出厂 bundle 一致 |

`plugin-v9` 的配置（bundle、preset、`sac-bench` profile 一致）：

```yaml
statePrompt: inline
keepLastObservation: true
minSpanChars: 1200
minSpanTokens: 1200
stateBudget: 2048
adaptiveBudget: true
ledger: true
ledgerChars: 2400
ledgerFullCommands: true
recall: true
checklist: true
```

## B. 测试台线（Python `harness/sac/`，不是产品代码）

| 版本 | 新增机制 | 验证范围 | 与论文的关系 |
|---|---|---|---|
| `harness-v1` | 论文附录 G.1 模板 + 单独摘要回合 | 7 任务 × 2 模型 | 最接近论文 |
| `v2` | 固定六段 + 预算 | 7 任务 | 实现选择 |
| `v3` | + ACTION DIGEST | 7 任务 | 论文没有 |
| `v4` | + ledger / recall / adaptive / ANCHORS | 7 任务 | 论文没有 |
| `v5` | + checklist | 8 任务 | 论文没有 |
| `v6` | + inline（模型自己写 `<state>`） | 8 任务 | **架构偏离**：论文是单独摘要回合 |
| `v7` | + read cache | 8 任务 | 论文没有 |
| `v8` | `finish_guard`（实际是空操作，只启用 full ledger） | 8 任务 | 名字与实现不符 |
| `v9` | v6 管线 + full ledger | 8 任务 | 论文 recap 内容 + 工程管线 |

> v10–v13：研究性扩展（观测账本、image_pin、结构化状态、证据新鲜度、跨方法门控），机制保留在
> `harness/sac/prompts.py` 中；其验证任务的数据未获公开授权，相关任务与报告未包含在本仓库。

## C. 128K 优化线（2026-11-17）

- 目标：GLM-5.3-Inner（1M window，`maxTokens 16384`）在 **128K 上下文预算**下，三个标准基准
  （Loong / SWE-bench Verified / τ²-bench）得分最高、总 token 最少。
- 测试台：`work/dshhome`（工作区内 DSH_HOME，profile 可写）+ `/tmp/dsh-bench-runs`；
  `harness/bench/runner.py` 支持 `arm -> profile` 映射（`fha`/`sac`/任意变体名）。
- 迭代记录：`work/goal_round1/ITERATIONS.md`；结果 `work/bench/results/iter5_*`、`iter6_*` …
- 关键发现：折叠阈值必须低（minSpanTokens≈400）才能把 SWE 上下文压在 ~10K；阈值 16K/28K
  会让每请求常驻 20–32K、总 token +47%。inline 模式在 GLM 上大量 fallback（v9port2 仅 76.9% 命中），
  故 128K 优化线以 `statePrompt: paper` 为主。
- 插件改动（`plugin-iter6`，48 测试）：`stateInputLean`（摘要请求去 system/tools，≈4.8K tok/折叠）、
  `trimObservationOver`（只裁超大观测）、`stateMaxWords`（recap 限长）、`contextBudgetTokens`
  （96K 强制折叠）、`inlineFallbackAfter`（粘性回退）、paper 模板加输出契约/精确值两节。
- 最终配置：`paper + APC2 + keepLastObservation + trim4000 + 400词 + lean + contextBudget96000`；
  全量 12 任务 × n=5：**任务加权均分 0.853 vs FHA 0.837，
  token 7.35M vs 17.74M（−58.6%），峰值 50.4K vs 129.6K（FHA 距 128K 仅 1.1%，历史出现过 140K 越界）**。
  报告：[`reports/BENCH-REPORT-128K.md`](../reports/BENCH-REPORT-128K.md)。

## 命名约定

- 插件版本用 `plugin-iterN` / `plugin-v9`。
- 测试台版本用 `harness-vN`。
- 不再用裸 `vN` 同时指两条线；旧报告里的 `v4` 等按上下文分别指 `harness-v4` 或 `plugin-iter4`。

## D. τ²-bench 多轮测试台（2026-11-26）

被测：插件 1.2.0（git `dfa3b6a`，快照 `work/bench/plugin_versions/multiturn-1.2.0-pin4/`），`maxPinnedInstructions: 4`。
方案与环境见 [`TEST-PLAN-TAU2-MULTITURN.md`](TEST-PLAN-TAU2-MULTITURN.md)；**真实测试尚未运行**。

| 组件 | 位置 | 状态 |
|---|---|---|
| `@local/dsh-tau2-bridge` 0.1.0 | `plugins/dsh-tau2-bridge/` | 4 个测试；在真实 DSH 运行时 + 假 LLM 的离线自检里跑通 |
| `dsh_agent`、官方 `tau2` 命令行包装 | `harness/tau2/` | 离线自检 34 项通过（桩 DSH 11 项 + 真实 DSH 23 项） |
| DSH profile `tau2-sac` / `tau2-fha` | `work/dshhome/profiles/`（`make_profiles.py` 生成） | 预检通过 |
| τ²-bench v1.0.1 环境 | `work/tau2/venv`，数据在 `benchmarks/tau2-bench/` | 184 题可加载；补下载 11 个官方文件并校验 |

## E. 成本感知折叠线（2026-11-27）

- 出发点：1.2.0 在 τ² 184 题上每次折叠花 ≈4.2K token、换回中位 600 token 的上下文；24% 的尝试被拒、24% 的 recap 被截断、
  25% 的 recap 再被预算截断；长对话题上 29% 的工具调用是重复的。
- 1.3.0 的机制见 A 表与 [CHANGELOG](../CHANGELOG.md)；实验脚本、各臂配置与结果在开发机的 `work/sac_pareto/`（`make_arms.py` 是各臂配置的唯一来源，未发布）。
- 结果：τ² 36 题子集上 1.3.0 出厂配置与 FHA 是同一组请求（108 个会话只折 2 次；token 0.94×、账面 0.92×），分数差在 A/A 噪声带内
  （FHA 自己四个 trial 29/28/25/26）；40 条记录的长程记忆测试 1.3.0 5/5 次 120/120、token −66%、峰值 −72%，SAC 1.2.0 只有 74/120 且重读全部文件；
  13 个真实会话重放：FHA 21.9 亿 token → 1.89 亿，模型 recap 503 次（1.2.0 2,558 次）。
- 最大的反例：把当前轮工作区也做成摘要行（每步都折）会让代理重复劳动（τ² 重复调用 53–57%，SWE 请求数 ×2.5–7），所以摘要行只用于已结束的轮次。
- 第 4 轮（40 / 80 条记录的长程记忆测试，qwen3.8-27b 与 GLM-5.3-Inner）：1.3.0 出厂水位 32K 在 40 条记录上 6/6 次 120/120（token −67%），
  80 条记录上 2/2 次 240/240（−81%）；SAC 1.2.0 在 qwen 上 74/120 并重读全部文件，在 GLM 上 80 条记录陷入重读循环。
  由此修了两处：recap 的字数契约跟着片段走（固定 400 词会让摘要器把累积的事实表换成计数）；`recapMaxRatio` 生效时重试不再压字数，并要求把已有的精确值整份带下去。
  另加 `digestShrinkToBudget`（摘要行放不下时每个结果少留一点，而不是整轮交给模型）。仍未解决：GLM 写的 recap 在要带很多精确值的长任务上有时丢掉累积的事实表，
  代理重读自愈（80 条记录 3 次里 1 次、60 条 × 4 回合 4 次里 1 次）；下一步是把已有的 `ledger` 接到 1.3.0 策略上。
- qwen3.8-27b 所在服务商账户的额度在 03:30 左右用尽，之后的验证只在 GLM 上做；qwen 上的 FHA 第 5 个 trial 与 SAC 1.2.0 第 2–3 个 trial 作废。
- 结论与推荐配置见 [`reports/REPORT-SAC-PARETO.md`](../reports/REPORT-SAC-PARETO.md)。
