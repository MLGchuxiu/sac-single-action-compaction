# 版本表

这个项目里有**两条互不相同的版本线**。看版本号前先确认是哪一条。

## A. 插件线（真实产品代码 `plugins/dsh-sac-compact`）

| 版本 | 代码位置 | 核心改动 | 测试 | 状态 |
|---|---|---|---|---|
| `plugin-iter0` | `work/bench/plugin_versions/iter0/` | session-fdac86bd 的 A1/A3 基线：懒安装 + 遥测 + paper 模板 | 27 | 历史基线 |
| `plugin-iter1` | `work/bench/plugin_versions/iter1/` | 空状态重试 + `minSpanTokens` | 29 | 已并入 |
| `plugin-iter2` | `work/bench/plugin_versions/iter2/` | `stateInputMaxChars` 裁剪 | 30 | **证伪**，默认 0 |
| `plugin-iter3` | `work/bench/plugin_versions/iter3/` | `keepLastObservation` | 31 | 已并入 |
| `plugin-iter4` | `work/bench/plugin_versions/iter4/` | 仅配置：`actionsPerCompaction: 2`（SAC-2） | 31 | 基准最优配置 |
| **`plugin-v9`** | `plugins/dsh-sac-compact/`（当前） | **inline 状态 + ledger + checklist + recall + 自适应预算**；版本化安装标记（热更新可重装） | 41 | 当前客户端；inline 对 GLM-5.3-Inner 不适用（推理模型 tool-call 回复不带文本） |
| **`plugin-iter6`** | `plugins/dsh-sac-compact/`（2026-11-17） | 128K 目标优化：paper 模板加「输出契约 / 精确值」两节；`trimObservationOver`（只裁超大观测）；`stateMaxWords`（简短 recap）；`contextBudgetTokens`（逼近窗口时强制折叠）；inline 连续失败后粘性回退 paper（`inlineFallbackAfter`）；修复 `stateMaxTokens` 未被 `resolvePolicy` 识别的旧 bug；新测试 | **48** | **本轮优化线**（快照 `work/bench/plugin_versions/final/`） |

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

> v10–v13：在一份内部业务文档任务上做的研究性扩展（观测账本、image_pin、结构化状态、
> 证据新鲜度、跨方法门控）。因任务数据属内部资料，相关任务、代码与报告**未包含在公开仓库**。

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