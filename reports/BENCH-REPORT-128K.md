# SAC 插件 × GLM-5.3-Inner × 128K 上下文：优化迭代报告

> 目标：让 **GLM-5.3-Inner** 在 **128K 上下文预算**下，在下载的三个标准测试集
> （LOONG / SWE-bench Verified / τ²-bench）上**得分最高、总 token 消耗最少**。
> 对象：`plugins/dsh-sac-compact`（SAC 单动作压缩插件）及其推荐配置。
> 迭代窗口：2026-11-17 00:00 – 08:00（Asia/Shanghai）。模型：`jms-proxy / GLM-5.3-Inner`
> （1M window，输出上限本轮测 16K / 32K 两档）。

## 0. 结论摘要

1. **找到了三条历史线索**：本机 12 个 DSH 历史会话的普查（未包含在本仓库）、两条版本线
   （插件线 `plugin-iter0..iter4 / v9 / iter6`；测试台线 `harness-v1..v13`，`docs/VERSIONS.md`）、
   以及三个已下载校验的测试集（`benchmarks/README.md`：LOONG 1,600 / SWE-bench Verified 500 / τ² 184）。
2. **v9 的 `inline` 状态管线不适用于 GLM-5.3-Inner**：该模型在带 tool-call 的回复里**不输出文本块**
   （全部内容在 reasoning 里），插件只能每步回退到 paper 摘要器。v9port2 会话日志复核：52 次折叠中
   12 次走 fallback（23%），fallback 相关 token 占总 token 18.5%。→ 128K 优化线改为 **paper 模式**。
3. **插件级最大优化是“摘要请求瘦身”**：`stateInputLean: true` 让状态更新请求不再重放
   会话的 system prompt + 22 个工具 schema。消融实测：单次折叠摘要 prompt 从 ~11K 降到 ~5K
   （−55%），SWE 总 token −24.6%，得分不变（§4.1）。配合 `trimObservationOver`（只裁超大观测）
   与 `stateMaxWords`（recap 限长），SWE 单任务总 token 从 684K 降到 328K（−52%），峰值 49K→15K。
4. **折叠阈值必须低**：`minSpanTokens=400` + `actionsPerCompaction=2` 明显优于
   16K/28K 阈值（后者让每请求常驻 20–32K 上下文，总 token +47%）。
5. **128K 合规**：优化后所有 SAC 会话峰值 6.5K–50.4K，远低于 131,072；插件新增
   `contextBudgetTokens=96000` 硬保险，任何会话逼近窗口都会强制折叠。
   同窗口 FHA 对照出现过 **106.6K 峰值**，在更长任务上会越界。
6. **最终配置**（见 §7）在 **全量 12 任务 × n=5**（120 格）上相对同窗口 FHA：
   任务加权均分 **0.853 vs 0.837**；总 token **7.35M vs 17.74M（−58.6%）**；
   峰值 **50,385 vs 129,575**（FHA 距 128K 仅 1.1%；历史 iter12 同任务出现过 **140,048 越界**）。
   SWE 3/3 全对且 token 14.97M→4.82M（−67.8%）；Loong 0.885 vs 0.848；τ² 持平（无折叠）。
   **敏感性**：FHA 有 2 格在 1800s 超时（都在 sympy-24066，合计 4.75M token），剔除后
   token 对比为 7.35M vs 12.99M（**−43.4%**）；得分/峰值结论不变。
7. **额外确认**：legal-L2-0 专项 n=10（FHA 0.307 vs SAC 0.467）；lean 消融（SWE token −24.6%）；
   FHA 在 sympy-24066 上 5 次运行 2 次 1800s 超时，SAC 5/5 全部 <1500s 完成 ——
   128K 约束下 SAC 同时解决“越界”和“跑不完”两个问题。

## 1. 测试集与约束

| 测试集 | 规模 | 本测试台用法 | 判分 |
|---|---|---|---|
| LOONG | 1,600 题 | 5 题：paper×2（137 KB）、financial×2、legal×1（8 份判决书） | 结构化答案集合 F1；span 答案规范化包含/token F1 |
| SWE-bench Verified | 500 实例 | 3 题（sympy 24066 / 24213 / 24539），无 Docker，共享 venv | 官方 `test_patch` + `FAIL_TO_PASS`，全过 = resolved |
| τ²-bench | 184 golden | 4 题（retail×3、airline×1）静态单轮计划变体 | 工具名序列 LCS(0.7) + communicate_info(0.3) |

- 约束：模型输入上限目标 **128K**（131,072；插件保险 96K 强制折叠），输出上限 16K（默认）/32K（对照）。
- 三个测试集在 `benchmarks/`，均已校验；本报告所有数字来自真实 `dsh headless` 会话。

## 2. 测试台

- 实验组 `sac-bench[-变体]`（`dsh-base` + `dsh-headless` + `@local/dsh-sac-compact`），
  对照组 `fha-bench[-32]`（同 headless，不挂 SAC）。
- 本轮把 `DSH_HOME` 放到工作区内（`work/dshhome`，profile 与 session 日志可写、可复现），
  运行工作区放 `/tmp/dsh-bench-runs`（agent 看不到 `benchmarks/` 金标）。
- 指标从会话日志提取：每请求 prompt/completion、峰值上下文、折叠次数/错误、摘要 token、墙钟、任务分。
- 复现：`work/goal_round1/run_matrix.sh --bench loong,swe,tau2 --arms <arms> --iteration <name>`。

## 3. 诊断：GLM-5.3-Inner 上的 SAC 成本结构

（数据来自 `work/goal_round1/ANALYSIS.md`、`work/bench/results/{iter4,iter4rep,v9port,v9port2}*`）

1. **inline 不可用**：GLM 的 tool-call 回复只有 reasoning + tool-call，无 text → `<state>` 提取失败；
   插件 nudge 3 次额度在早期用完，后续 8 次折叠无提醒，只能走 paper fallback。
2. **状态更新是主成本**：paper 模式每次折叠 ≈ 9–15K prompt（折叠段本身 + system/tools 前缀）
   + 1.6–2.4K completion（其中大部分是推理 token）。SWE 上折叠 8–14 次 → 80–200K token。
3. **固定开销巨大**：22 个工具 schema 约 20.8 KB（字符/3.5 估算 ≈5.9K tokens/请求，约占 v9port2 每请求 prompt 的 69%）。
   这是 harness 侧问题，插件能做的是不额外增加工具（关闭 recall/checklist）。
4. **输出截断**：16K 输出上限下，长推理回合会 `stopReason=length` → 空答案 0 分（历史 67 格中 7 个 0 分，
   其中 3 个确认是空答案/输出上限截断）。
5. **τ² 静态变体的分数上限是结构性的**：gold 的 communicate_info 在 DB 里，工作区不可见，
   retail 上限 0.7；且 τ² 只有 2 步、从不触发折叠 → SAC 对它零成本、零收益。

## 4. 插件改动（`plugins/dsh-sac-compact`，48 个测试全绿）

| 改动 | 配置 | 作用 |
|---|---|---|
| **摘要请求瘦身** | `stateInputLean: true` | 状态更新请求不再携带会话 system prompt / 工具 schema / toolHistory（实测 ≈4.8K tok/折叠） |
| **只裁超大观测** | `trimObservationOver: 4000` | 单条观测 >4000 字符才头尾裁剪；小测试输出保持逐字精确 |
| **recap 限长** | `stateMaxWords: 400` | 状态只保留 400 词；状态每请求重发，直接省每请求 token |
| **128K 硬保险** | `contextBudgetTokens: 96000` | 表面估算 token 达阈值即强制折叠（忽略 minSpan 守卫） |
| **inline 粘性回退** | `inlineFallbackAfter: 2` | 连续 2 次提取失败后本会话改用 paper，不再浪费 nudge/尝试 |
| **模板补两节** | （内置） | paper 模板新增 “7. Output contract / 8. Verbatim values”，修格式与逐字值错误 |
| 默认配置 | paper + APC2 + keepLastObservation + 上述开关 | 取代 v9 的 inline+ledger+checklist+recall 默认 |

代码位置：`index.js`（`trimOversized`、`stateInputLean`、`stateMaxWords`、`contextBudgetTokens`、
`inlineFallbackAfter`）、`lib/sac-policy.js`（策略字段与 sanitization）、`test/`（46→48 用例）。

### 4.1 消融：`stateInputLean` 的贡献（iter13，SWE 3 题 × n=2）

| 臂 | 总 token | 每格 token | 每折叠 summary prompt | 每折叠 summary completion | 得分 |
|---|---|---|---|---|---|
| **lean 开（最终配置）** | **2,167,703** | 361,283 | **4,960** | 1,000 | 1.0（6/6） |
| lean 关 | 2,875,483 | 479,247 | 10,948 | 990 | 1.0（6/6） |

→ lean 让**单次折叠的摘要 prompt 从 ~11K 降到 ~5K**，SWE 总 token **−24.6%**，得分不变。
这是本轮插件级收益最大的单项改动。

## 5. 迭代记录（6 任务子集）

子集：loong paper-L3-0 / legal-L2-0，swe sympy-24066 / 24213，tau2 retail-37 / airline-1。

| 迭代 | 配置 | SWE 总 token | SWE 峰值 | Loong 分 | 结论 |
|---|---|---|---|---|---|
| iter5 | p2：paper APC2 minSpan400 | 748K | 19K | 0.70 | 低阈值最优；16K/28K 阈值 +47% token |
| iter6 | +新模板（无限长） | 961K | 24K | 0.50 | recap completion/fold +25.5%（1,555→1,952）→ 必须限长 |
| iter6 | +180 词 | 1,020K | 19K | 0.70 | 省 summary，但难任务折叠/请求变多 |
| iter7 | +trim4K +180 词 | 978K | 22K | 0.50 | |
| iter7 | +trim4K +180 词 +lean | 922K | 20K | 0.50 | lean 省 ≈4.8K/折叠 |
| **iter7** | **+trim4K +400 词 +lean** | **696K** | **18K** | **0.50** | **本轮最优**；24066 328K / 24213 368K |
| iter6 | FHA（对照，同窗口） | 2,250K | **106.6K** | 0.50 | FHA 峰值逼近 128K（该列 FHA 为最大值，SAC 为均值） |

后续两组配置扫描（详见 [`work/goal_round1/ITERATIONS.md`](../work/goal_round1/ITERATIONS.md)）：
- **iter10**（6 任务 × 4 臂）：APC1 在难任务 24066 超时 0 分，淘汰；APC3、`trimObservationOver=2500`、
  非自适应 `stateBudget=1024` 均无稳定收益。
- **iter11**（SWE 3 题 × APC2/APC3 × n=3）：两臂都 9/9 全对；APC3 合计 token 3.14M vs APC2 3.40M
  （−7.8%），但 24066 反而更贵、24213 更省——任务间方差大于节奏差异。**最终保留 APC2**。

## 6. 全量验证（12 任务 × 2 臂 × n=5 = 120 格）

臂：`fha32`（对照，全历史，32K 输出上限）vs `n2t4L4b32`（最终 SAC 配置，32K 输出上限）。
iter8(n=1)+iter9(n=2)+iter14(n=1)+iter17(n=1) 合并为**每格 n=5**；同一测试台、同一模型、同一窗口。

### 6.1 分基准

| 基准 | 臂 | n | 均分 | 满分率 | 峰值中位 | 峰值最大 | 总 token | 每格 token | 折叠 | 折叠错误 | 请求 | 墙钟中位 | 超时 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Loong(5) | fha32 | 25 | 0.848 | 20/25 | 35,050 | 60,802 | 2,270,937 | 90,837 | 0 | 0 | 110 | 35s | 0 |
| Loong(5) | **SAC** | 25 | **0.885** | 20/25 | 35,056 | 50,385 | **2,057,183** | 82,287 | 23 | 9 | 119 | 53s | 0 |
| SWE(3) | fha32 | 15 | 1.000 | 15/15 | 51,349 | **129,575** | **14,971,648** | 998,109 | 0 | 0 | 385 | 409s | 2 |
| SWE(3) | **SAC** | 15 | 1.000 | 15/15 | 14,978 | 36,861 | **4,816,972** | 321,131 | 157 | 0 | 355 | 509s | 0 |
| τ²(4) | fha32 | 20 | 0.702 | 5/20 | 8,517 | 14,926 | 502,101 | 25,105 | 0 | 0 | 42 | 131s | 0 |
| τ²(4) | **SAC** | 20 | 0.702 | 5/20 | 8,543 | 23,891 | **474,727** | 23,736 | 0 | 0 | 41 | 117s | 0 |

> 折叠错误：SAC 共 9 次失败（全在 Loong 的短折叠），均为后端 “summary is not smaller than the
> shadowed content”；失败时该步不折叠、不影响对话。

### 6.2 全量合计

| 臂 | 任务加权均分 | 总 token | 每格 token | 峰值最大 | 折叠 | 超时 |
|---|---|---|---|---|---|---|
| fha32（全历史） | 0.8373 | 17,744,686 | 295,744 | **129,575** | 0 | 2/60 |
| **n2t4L4b32（优化 SAC）** | **0.8528** | **7,348,882（−58.6%）** | 122,481 | **50,385（−61%）** | 180 | 0/60 |

> 任务加权均分 = 先按基准内任务平均，再按 12 个任务等权平均（避免重复次数影响权重）；
> 直接按 60 格平均为 FHA 0.8373 vs SAC 0.8529。
> **敏感性**：FHA 的 2 个超时格（都在 sympy-24066，合计 4.75M token）剔除后，
> token 对比为 7.35M vs 12.99M（**−43.4%**）；得分与峰值结论不变。

### 6.3 分任务（各任务 n=5 均值；峰值列为该任务 n=5 的**最大值**）

| 基准 | 任务 | FHA 分 | SAC 分 | FHA token | SAC token | Δtoken | FHA 峰值max | SAC 峰值max |
|---|---|---|---|---|---|---|---|---|
| Loong | financial-L2-0 | 1.000 | 1.000 | 34,403 | 33,236 | −3% | 33,133 | 32,952 |
| Loong | financial-L2-5 | 1.000 | 1.000 | 35,908 | 39,696 | +11% | 35,050 | 35,128 |
| Loong | legal-L2-0 | 0.240 | **0.427** | 186,308 | 135,143 | −27% | 60,802 | 45,105 |
| Loong | paper-L3-0 | 1.000 | 1.000 | 130,306 | 115,304 | −12% | 53,426 | 50,385 |
| Loong | paper-L3-1 | 1.000 | 1.000 | 67,261 | 88,055 | +31% | 44,457 | 47,035 |
| SWE | sympy-24066 | 1.000 | 1.000 | 2,142,306 | 508,019 | **−76%** | **129,575** | 36,861 |
| SWE | sympy-24213 | 1.000 | 1.000 | 689,881 | 284,421 | **−59%** | 70,453 | 24,588 |
| SWE | sympy-24539 | 1.000 | 1.000 | 162,141 | 170,953 | +5% | 19,898 | 14,978 |
| τ² | airline-1 | 1.000 | 1.000 | 16,892 | 16,002 | −5% | 8,327 | 8,374 |
| τ² | retail-29 | 0.583 | 0.583 | 24,023 | 23,210 | −3% | 8,596 | 8,550 |
| τ² | retail-36 | 0.700 | 0.700 | 29,504 | 32,774 | +11% | 8,586 | 23,891 |
| τ² | retail-37 | 0.525 | 0.525 | 30,000 | 22,958 | −23% | 14,926 | 8,650 |

> τ² 全部 0 折叠，两臂差异是采样噪声（同一 prompt、同一模型，仅输出随机）。
> 小任务上 SAC 偶尔更贵（financial-L2-5、paper-L3-1 的 +11%/+31%），因为折叠的固定开销
> 在短任务里摊不回来；SAC 的总 token 优势集中在长轨迹（SWE 24066/24213，−59%/−76%）。
>
> **legal-L2-0 专项**（该题单次 0.0/0.4/0.667/1.0 都出现过，是全场方差最大的任务）：
> 追加 iter16 的 n=5 后合计 **n=10**——FHA 均分 **0.307**、SAC **0.467（+0.16）**；
> token 173,841→134,735（−22.5%），峰值 60,802→33,451。分数优势在 n=10 下依然成立。

### 6.4 额外验证

- **独立确认**（iter14，全新 n=1）：SAC 1.57M token vs FHA 4.14M（−62.0%），峰值 47K vs 87K，
  均分 0.851 vs 0.817。
- **lean 消融**（iter13，SWE n=2）：lean 开 2.17M vs 关 2.88M（**−24.6%**），
  每折叠摘要 prompt 4,960 vs 10,948，得分不变。
- **FHA 越界证据**：n=5 口径下 FHA 峰值 129,575（距 128K 仅 1.1%）；历史 iter12 中同任务出现过
  **140,048 > 131,072 的越界格**。SAC 全数据集峰值 50,385，0 格越界。
- **FHA 超时**：sympy-24066 的 FHA 运行 5 次里 2 次 1800s 超时；SAC 同任务 5/5 全部 <1500s 完成。
- **出厂默认值验证**（iter18/iter19，直接跑 `plugins/dsh-sac-compact` 的 `cordis.patch.yml` 默认值，
  不经过基准 profile 覆盖，16K 输出上限）：
  - 全量 12 任务 n=1：SAC 1.30M token / 均分 0.759 / 峰值 48.8K vs FHA 2.33M / 0.683 / 56.2K（token −44%）；
    34 次折叠全部 paper 路径、0 次 inline fallback。
  - SWE 3 题 n=2：SAC **1.24M vs FHA 7.70M（−84%）**，峰值 **20.9K vs 139.3K（FHA 再次越过 128K）**；
    SAC 5/6 全对（24066 的 1 次运行因 16K 输出上限截断未解出），FHA 6/6 全对。
  - Loong 5 题 n=2：SAC 674K / 均分 0.900 / 峰值 55.3K vs FHA 861K / 0.840 / 48.9K（token −21.8%）。
  说明**交付的插件默认配置本身**就是这套优化配置；同时印证 §7 的建议——128K 部署应把输出上限提到 32K。

## 7. 最终推荐配置

> 插件配置（下）与 harness 设置（`maxTokens: 32768`，输出上限）分开：前者是插件默认值，
> 后者是 128K 部署建议。16K 输出上限下同样成立（iter8 的 n2t4L4 臂），但 32K 能减少
> “推理撞上限→空答案 0 分”的格子。

```yaml
# plugins/dsh-sac-compact/cordis.patch.yml（推荐默认）
statePrompt: paper
actionsPerCompaction: 2
keepLastObservation: true
minSpanChars: 1200
minSpanTokens: 400
charsPerToken: 3.5
stateMaxTokens: 2048
stateBudget: 2048
adaptiveBudget: true
stateInputMaxChars: 0
trimObservationOver: 4000
stateMaxWords: 400
stateInputLean: true
contextBudgetTokens: 96000
inlineFallbackAfter: 2
ledger: false
recall: false
checklist: false
```

## 8. 128K 合规性

- 约束：131,072 tokens。最终 SAC 配置额外带 `contextBudgetTokens: 96000` 硬保险：
  表面估算一旦 ≥96K 立即强制折叠（忽略 minSpan 守卫），保证任何会话不会因压缩策略而越界。
- 实测（120 格，n=5）：**SAC 峰值最大 50,385（Loong paper-L3-0，文档本身 137KB）**，
  SWE 最大 36,861，τ² 最大 23,891 —— 全部 < 128K，且多数 < 36K。
- 对照 FHA：n=5 口径下同任务（sympy-24066）峰值最大 **129,575（距 128K 仅 1.1%）**，
  另有 116,059、102,949 两格 >100K；另在 iter12、iter19 中同任务分别出现过
  **140,048 / 139,339 > 131,072 的越界格**（3 次独立全量运行里 2 次越界）。
  该任务 FHA 峰值分布（n=5）：102.9K / 116.1K / 92.3K / 87.1K / **129.6K**。
  也就是说：**在这套测试集上 FHA 会越过 128K，SAC 把峰值压到 1/3–1/4**。
- 插件单测覆盖强制折叠路径（`iter6: contextBudgetTokens forces a fold the size guard would skip`）。

| 配置 | 峰值中位（Loong/SWE） | 峰值最大（n=5） | 峰值 >100K | 峰值 >128K |
|---|---|---|---|---|
| FHA（全历史） | 35,050 / 51,349 | **129,575**（历史 140,048） | 3 格（n=5）；历史 4 格 | **历史 1 格（sympy-24066）** |
| 优化 SAC | 35,056 / 14,978 | **50,385** | 0 格 | 0 格 |

## 9. 结论与后续建议

1. 插件级：paper + APC2 + keepLast + trim + 400 词 + lean + 96K 保险，是本次在三个测试集上
   得分不降、token 显著下降的最优配置。
2. harness 级（非插件，收益最大）：工具表瘦身（22→6 个工具，约 −36% token）、
   输出上限 32K（回收截断 0 分）、τ² 把 DB 值放进工作区（修 0.3 结构性丢分）。
3. 后续：τ² 官方多轮桥接（D6）；状态更新换小模型（D1）；telemetry 出口（D7）。

## 10. 复现

```bash
# 插件测试
cd plugins/dsh-sac-compact && node --test test/sac-policy.test.js test/integration.test.js

# 基准（全量，n=5：fha32 对照 vs 最终配置 n2t4L4b32）
work/goal_round1/run_matrix.sh --bench loong,swe,tau2 \
  --arms fha32,n2t4L4b32 --repeats 5 --iteration final --workers 6

# 汇总
python3 work/goal_round1/summarize_results.py work/bench/results/final_*.jsonl --tasks
python3 work/goal_round1/final_tables.py   # 本报告 §6 的表格（iter8+9+14+17）
```