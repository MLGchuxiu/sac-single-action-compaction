# 更新日志 / Changelog

本文件记录两个包的逐版本改动：`@local/dsh-sac-compact`（压缩插件，[`plugins/dsh-sac-compact`](plugins/dsh-sac-compact/README.md)）
和 `@local/dsh-sac-preset`（GUI 预设，[`plugins/dsh-sac-preset`](plugins/dsh-sac-preset/README.md)）。两条版本线的对应关系见
[docs/VERSIONS.md](docs/VERSIONS.md)。

- **[1.3.0](#130--2026-11-28)** — 成本感知折叠 + 预设对齐出厂策略 + 离线更新包
  - 内含 **[1.2.0](#120--2026-11-25)** — 多轮会话修复（上一个发布版本是 1.1.0，所以 1.2.0 的改动也在这里）
- **[English](#english)**

---

## 1.3.0 — 2026-11-28

> 代码定稿于 2026-11-27。取证与实验全文：[`reports/REPORT-SAC-PARETO.md`](reports/REPORT-SAC-PARETO.md)。

**一句话**：折叠从「按动作数」改成「按代价」——短对话一次都不折（与不压缩时是同一组请求），长会话才折，而且折得完整、不再让代理「折掉了再读一遍」。
同时修复了 GUI 预设的配置漂移：此前在 GUI 里选 SAC 预设的会话其实一直跑的是旧行为。

### 为什么要改：1.2.0 的问题不是折得不够，而是折得太勤、折得太差

把 1.2.0 在 τ²-bench 184 题（qwen3.8-27b，思考模式）上的每一次折叠拆开看：

| 事实 | 数字 | 含义 |
|---|---|---|
| 折叠尝试 | 820 次：620 次成功、186 次被后端判为「摘要不比原文小」、13 次没有产出正文、1 次其它 | **24% 的尝试白付一次模型调用** |
| 被替换的片段 / 写出的 recap | 中位 1,212 / 613 token | recap 约占被替换片段的 51% |
| 每次状态更新 | ≈4.2K token（输入 ≈2.2K + 推理输出 ≈2.0K），全部是新输入 | — |
| 每次折叠的收益 | 上下文省 ≈600 token，约 3.8 个请求受益 ≈2.3K token，且多数命中缓存 | **花 4.2K 新 token，省 2.3K 多为缓存的 token** |
| 输出触顶 | 620 次「成功」里 340 次输出正好停在 2,048 的上限，其中 147 份 recap 被截断 | 1.2.0 不看结束原因，截断的 recap 被静默接受 |
| 预算二次截断 | 620 份 checkpoint 里 156 份带 `[state truncated to budget]` | 自适应预算把已经写好的 recap 再掐头去尾一次 |
| 前缀缓存 | 不压缩时 82–86% 的输入命中缓存；1.2.0 只有 57–72% | 折叠让前缀缓存失效 |
| 重复劳动 | 对话最长的 36 题上，27–29% 的工具调用是「折掉了再读一遍」 | 「折掉 → 再读一遍」正是它更贵的原因 |
| 账面成本 | 184 题上折算账面 token 是不压缩的 1.75×，而平均上下文只有 0.87× | 上下文变小，账单变大 |

一句话：「每 2 个动作折叠一次」对 300 步的编码会话是对的，对 10 步的对话是错的。

对照 13 篇相关文献（Folio、ReFold、CADOC、TurnCost、Compact-Bench 等，见报告第 2 节）归纳出四条设计原则：
**(1) 不该折的时候不折；(2) 能由 harness 写的就别叫模型写；(3) 模型写的 recap 必须完整、且不比片段大；(4) 触发点由成本决定，而不是由动作数决定。**

### 改进了什么：11 个机制，每个都是一个配置键

| 机制 | 配置键 | 改进 / 修复了什么 |
|---|---|---|
| **按大小触发 + 滞回** | `foldTrigger: size`、`foldMarginTokens`（或 `foldAboveTokens`）、`foldBelowTokens`、`maxFoldsPerStep` | 不再按动作数折叠。整个请求（系统提示 + 工具定义 + surface）超过高水位才折，折到低水位为止。**短对话不再为折叠付费**。水位相对「固定提示」写，所以同一套配置既适合 5K 的客服提示，也适合 20K 的编码代理提示 |
| **收益门槛** | `foldMinGainTokens`、`recapExpectedTokens` | 模型写的 recap 只替换「至少比它大一截」的片段。修掉了「片段比 recap 还小、被后端拒绝」的 24% |
| **机械折叠（harness 写摘要行，不调模型）** | `mechanicalFoldMaxTokens`、`mechanicalSegments`、`mechanicalMinGainTokens`、`digestResultChars`、`digestReplyChars`、`digestMaxChars` | 片段的逐字摘要行够小时，由 harness 自己写 checkpoint：此前的状态原样带下去，每个动作一行（`- tool(args)` / `  → result`），回复原文引用。**每次折叠 4.2K token → 0**；隐藏的推理被顺手丢掉（服务商会在之后每个请求里回放它）。JSON 结果按结构压缩而不是截断，保留项的键、id、数字保持精确。摘要行不阻塞，所以能在模型对新请求迈出第一步之前就释放上一轮 |
| **重的最终回复随轮折叠** | `replyReasoningFoldChars` | 一个 13K token 的最终回复节点（95% 是隐藏推理）此前为了 300 字的可见文本整段留在上下文里；现在随该轮的摘要行一起折，可见文本保留 |
| **recap 按片段限长** | `recapMaxRatio` | recap 不得超过其片段的一定比例（按后端自己的 token 计量）。交给模型的字数上限和对其输出的硬上限都跟着走，**后端不再拒绝「摘要不比原文小」**；有了这个上限，早折时的状态预算不再二次截断（它此前把 620 份 recap 里的 156 份掐头去尾） |
| **截断检测与重试** | `stateMaxTokensThinking`、`stateMaxTokensCeiling` | 状态更新读取流的结束原因。在输出上限处被截断或没有返回正文的 recap，会带着更紧的字数限制和翻倍的上限重试一次，翻倍后的上限在本会话内保持。代理的消息自带推理时，首次尝试就从更大的上限起步。修掉了「24% 的截断 recap 被静默接受」 |
| **recap 的字数契约跟着片段走** | `recapMaxRatio` 生效时 | 交给摘要器的字数上限是片段的一半（不超过 4 × `stateMaxWords`、不超过预算），而不是固定的 400 词；截断重试不再压低字数（输出上限已经翻倍）；提示里要求把已有的精确值整份带下去。修掉了「固定 400 词让摘要器把累积到 60 条的记录表换成『已提取 60 条』」 |
| **摘要行收缩到预算** | `digestShrinkToBudget` | 一轮结束时有 20 个读取结果，摘要行按每结果 2,000 字符放不进预算，此前只能整轮交给模型写；现在每个结果只留开头，仍然由 harness 写 |
| **独立的摘要路由** | `stateProvider`、`stateModel` | recap 可以交给更便宜的非推理模型写（实测每次约 4–7 秒，思考模型 21–75 秒） |
| **goal 重投去重** | `goalDedupe` | goal 模式每个自动轮次重投一次 `<goal_round>`，一个 27 轮的会话不压缩时 surface 上有 27 份目标副本；现在重投的那一轮即使没有工具动作也折成摘要行，副本至多 2 份 |
| **其它** | `standingSourceKinds` | 工作区指令（AGENTS.md，harness 在折叠后会重新补上）不进状态更新的输入；子代理的收尾消息在摘要行里整段保留；每条等待中的用户指令一到，就把上一轮当作已结束 |

**出厂配置就是这套策略**（[`cordis.patch.yml`](plugins/dsh-sac-compact/cordis.patch.yml) 末尾的那组键）：固定提示 + 32K / + 16K、收益门槛 400、recap ≤ 60%、4K 以内的摘要行由 harness 写、摘要行只折已结束的轮次、思考模型首轮 4,096 上限。

### 修复：GUI 预设的配置漂移（`@local/dsh-sac-preset` 1.3.0）

压缩插件 1.3.0 的成本感知策略写在插件自己的 bundle 配置里，而**预设走的是另一份配置**（预设 bundle 的 `cordis.patch.yml`）。更新前那份配置只有 1.1.0 / 1.2.0 的键，而代码里的默认值（`SAC_DEFAULTS`）仍是 1.2.0 的行为——
所以在 GUI 里选「SAC 单动作压缩」预设的会话，一直按「每 2 个动作折叠一次」运行，正是上面测出来不划算的那套。

- 预设升到 **1.3.0**：`sac-compact` 的 47 个配置键与压缩插件 bundle **逐键相同，键序也相同**（用真实的 YAML 解析器核对）。
- 新增 [`test/preset-policy.test.js`](plugins/dsh-sac-preset/test/preset-policy.test.js)（7 个用例）直接读取两个 YAML 做比较：任何一边改了另一边没改，测试就失败。
- 用真实 DSH 加载器（`dsh --dump-config`）合成 profile 树验证：`preset-sac` 的配置与出厂配置一致。

### 实测效果

| 场景 | 不压缩（FHA） | SAC 1.2.0 | **SAC 1.3.0（出厂水位 32K）** |
|---|---|---|---|
| τ²-bench 36 题子集，在线，qwen3.8-27b，1–4 个 trial | 成功 75.0%（108/144），峰值 prompt 38.3K | 账面 token **1.88×**，27% 重复调用，170 次折叠里 50 次失败 | 108 个会话里**只折了 2 次**；总 token 0.94×、账面 0.92×；折叠失败 0。分数 71.3%（77/108），与 FHA 的差在 A/A 噪声带内（FHA 自己四个 trial 是 29 / 28 / 25 / 26 题） |
| 40 条记录的记忆测试（读 40 步后回答 120 个事实 + 总额） | 120/120；3.28M token；平均 7.5 万、峰值 14.4 万 | qwen3.8-27b 上 **74/120、总额答错**，把 40 个文件全部重读（40 次折叠累计阻塞 22 分钟） | **6/6 次 120/120**（qwen ×4、GLM ×2）；1.08M token（**−67%**）；平均 2.3 万、峰值 3.5–4.1 万；**0 次重复读取** |
| 80 条记录（GLM-5.3-Inner） | 240/240；1,157 万 token；峰值 26.9 万 | **陷入重读循环**：49 分钟、387 个请求、238 次重复读取，被停掉时仍没有答案 | 三次里两次 **240/240 且不重读**：211–228 万 token（**−81%**），峰值 3.8–3.9 万 |
| 60 条记录 × 4 个用户回合（GLM-5.3-Inner） | 全对；638–685 万 token | — | 四次里三次全对且不重读（127–198 万 token）；「改口」的指令每次都被遵守 |
| SWE-bench Verified（3 实例）+ LOONG（5 题），GLM，每格 1 次 | 6/8，974K token，780 s | 7/8，1.29M token，35 次折叠，1,839 s | **7/8**，1.13M token，**3 次折叠**，737 s |
| 13 个真实长会话 / goal 会话（影子重放） | 21.9 亿 token，峰值 138 万 | 1.19 亿 token，2,558 次模型 recap | **1.89 亿 token（−91%）**，峰值 5.2 万，**503 次模型 recap**；最新指令与最近 3 条指令在 5,269 个模型步里 0 步不可见 |
| goal 模式真实运行（6 轮的小任务） | 目标副本 5 份 | 14 次折叠里 **11 次失败**，目标副本 5 份 | 折叠失败 **0**，目标副本 ≤ 2，surface 收在 10 个节点 |

读法：**短对话上压缩换不回钱**（FHA 的前缀缓存让重发历史只按一折计价，而每次折叠要付一次不带缓存的 recap，并让其后的缓存失效），所以 1.3.0 在这类负载上与 FHA 是同一组请求，分数相同是应有之义；**长会话上压缩是必需的**，此时 1.3.0 省得和 1.2.0 相近，但它记得住、调模型的次数只有约 1/5、没有一次折叠失败、没有一次 recap 被截断。

在线实测里 τ² 一行以「出厂水位 + 非推理模型写 recap」为代表；SWE / LOONG 一行是出厂配置原样。

### 最重要的反例：摘要行只用于已结束的轮次

第 1 轮实验把**当前轮的工作区**也做成摘要行（每步都折、不调模型）。结果 τ² 上 53–57% 的工具调用在重复自己、模型步数是不压缩的 1.7–2.0 倍，SWE-bench 上把 17 个请求能做完的任务拖到 43–120 个请求。
摘要行剪掉的是代理还要用的中间结果（文件内容、测试输出），而模型写的 recap 会把它们提炼成「已确认事实」保留下来。
所以出厂配置是 `mechanicalSegments: frozen`：摘要行只折已结束的轮次，当前轮仍然交给模型写 recap。

### 兼容性与回退

- **代码默认值仍是 1.2.0 的行为。** 所有新机制默认关闭；成本感知策略只在 bundle 的 `cordis.patch.yml` 里启用。宿主如果不带 bundle 配置直接挂载插件，拿到的还是 1.2.0 的行为。
- **1.3.0 的代码 + 1.2.0 的配置 = 1.2.0。** 在 820 次日志折叠决策上逐次重放，差 0 次。
- **回到 1.2.0 的行为**：`foldTrigger: actions`，并把 `foldMarginTokens`、`foldMinGainTokens`、`recapMaxRatio`、`mechanicalFoldMaxTokens`、`replyReasoningFoldChars`、`stateMaxTokensThinking` 归零（写法见 [`cordis.patch.yml`](plugins/dsh-sac-compact/cordis.patch.yml) 末尾的注释）。
- `contextBudgetTokens`（出厂 96000）仍是硬保险；1.2.0 的多轮修复（指令逐条钉住）不变。
- 想要更小的上下文：把 `foldMarginTokens` 降到 `24000`（长会话再省约 13% token，模型 recap 多 45%）或 `12000`（上下文最小，但 τ² 上开始每题都折，模型 recap 约为出厂的 3 倍，且有下面「已知局限」里的边界）。
- 想要折叠更快：`stateProvider` / `stateModel` 指向一个非推理模型。

### 升级须知

- **预设组合在会话创建时固化。** 更新后要**完全退出并重启客户端，再新建会话**；已有会话仍然固定在旧组合（含旧配置）上。
- 仓库里的 `plugins/` 不等于机器上已安装的副本。其它机器用离线更新包 `sac-deploy-kit-v1.3.0.zip`（挂在本仓库的 GitHub Release v1.3.0 下）：
  `update-sac.cmd -check` 只看不改（退出码 10 = 需要更新）→ `update-sac.cmd` 更新（agent / 脚本运行加 `-yes`）→ `update-sac.cmd -rollback` 撤销上一次更新。macOS 把 `update-sac.cmd` 换成 `./update-sac.sh`。
- 1.3.0 按请求大小折叠：**短对话里看不到 `dsh-sac-compact: folded` 日志是正常的**。

### 离线更新包（`deploy/`、`tools/`）

给不能联网的内网机器用，Windows / macOS 都可以，不需要 npm / pnpm / python / 网络 / 管理员权限。

- **更新过程**：检查现状 → 备份现有插件文件与 profile 注册信息 → 逐个文件写入并做 SHA-256 校验 → **从目标机器客户端自带的 standard 预设现场生成 SAC 预设**（预设必须完整枚举客户端的插件，写死一份换个版本就加载失败）→ 注册 + 链接 + lockfile → 校验（含「当前生效的配置必须等于出厂策略」）并跑一遍插件自带的测试。任何一步失败都会用备份自动还原。
- **只读诊断** `diagnose-sac.cmd` / `diagnose-sac.sh`：版本 → profile 注册 → 链接 → 插件文件 → 当前生效的配置 → 客户端能力 → 预设覆盖。
- **给 agent 的说明** `AGENT.md`：把其中的提示词整段粘给内网机器上的 DSH agent 即可代为更新。
- **顺手修了旧安装器的一个 bug**：v1.1.0 部署包的 lockfile 注入从未生效（正则没考虑 pnpm 在 `importers:` 后留的空行）。
- **回归**：[`deploy/tests/kit_e2e.sh`](deploy/tests/kit_e2e.sh) 在一次性的 DSH 目录里覆盖全新安装、从 v1.1.0 升级、重复执行、回滚、撤销回滚、失败自动回滚、把平台模拟成 win32 的流程、macOS 启动器，以及真实 DSH 加载器合成 profile 树，共 89 项。
- **重新打包**：`python3 tools/build_sac_deploy_kit.py`（会先跑测试，并用真正的 YAML 解析器核对预设与 bundle 两份配置）。
- 更新前给客户端实装做快照：`tools/sac_client_backup.py`。

### 已知局限

如实列出，细节见报告第 6 节：

- **样本小。** τ² 子集只有 36 题（刻意选的是对话最长的题），每臂 1–4 个 trial；同一配置两次运行之间就有 5–11 题不一致，所以分数只能下「噪声带内」的结论。SWE / LOONG 每格只跑 1 次。
- **长会话的数字来自影子重放**：模型和工具的事件按原日志回放，只换折叠策略。它回答的是「请求会变成多大、花多少次 recap」，不含「模型在短上下文里会不会做得不一样」。
- **端点与模型单一**：τ² 全部在 qwen3.8-27b（思考模式）上，SWE / LOONG 在 GLM-5.3-Inner 上；非推理模型写 recap 只在一家服务商的 deepseek-v4.1-flash 上验证过。别的模型上「思考模型写 recap 会截断」「每步摘要行有害」这两条需要重测。
- **「要带很多精确值的长任务」是模型写 recap 的已知边界。** 80 条记录的测试里，GLM 写的 recap 有时会把累积的事实表丢掉一部分，代理随后重读自愈（3 次里 1 次；60 条 × 4 回合的测试里 4 次里 1 次），那两次的 token 与不压缩相当。最直接的下一步是把插件里已有的 `ledger`（harness 写的结果开头清单）接到 1.3.0 的策略上。
- **`maxPinnedInstructions` 不是硬上限**（184 个会话结束时 48 个钉着 5–9 条）；`recall` 工具在 1.3.0 策略下的收益、128K 窗口下的实测，这一轮都没有重跑。
- **更新包的 Windows 专属部分（junction、`tasklist`、`.cmd`）没有在真机上测过**，只通过了把平台模拟成 win32 的流程测试。第一次在内网跑请先 `-check`。

### 测试

| 包 | 1.1.0 | 1.3.0 |
|---|---|---|
| `@local/dsh-sac-compact` | 48 | **108**（`sac-policy` 18、`integration` 30、`multiturn-policy` 16、`multiturn-integration` 14、`adaptive` 30） |
| `@local/dsh-sac-preset` | 0 | **7** |
| `@local/dsh-tau2-bridge` | — | 4 |

```bash
cd plugins/dsh-sac-compact && node --test test/*.test.js
cd ../dsh-sac-preset    && node --test test/*.test.js
```

### 仓库里新增的内容

| 路径 | 内容 |
|---|---|
| [`reports/REPORT-SAC-PARETO.md`](reports/REPORT-SAC-PARETO.md) | 1.3.0 的取证与帕累托对照：1.2.0 的 820 次折叠逐次取证、文献对照、11 个机制、四轮实验、水位扫描与推荐配置 |
| [`plugins/dsh-tau2-bridge/`](plugins/dsh-tau2-bridge/README.md)、[`harness/tau2/`](harness/tau2/) | τ²-bench 官方多轮编排器的测试台：把 τ² 的领域工具与 policy 接进 DSH，让 DSH + SAC 作为被测 agent |
| [`harness/session_replay/`](harness/session_replay/README.md) | 离线重放工具：用导出的会话日志复现模型每一步看到的 surface、逐点比对折叠范围、做 before/after 仿真（不调用模型） |
| [`deploy/`](deploy/templates/README.md)、[`tools/`](tools/build_sac_deploy_kit.py) | 离线更新包的模板、端到端回归与构建脚本 |
| [`docs/TEST-PLAN-TAU2-MULTITURN.md`](docs/TEST-PLAN-TAU2-MULTITURN.md) | τ²-bench 184 题官方跑法、环境、指标与判读 |

### 发布版说明

延续本仓库既有的公开标准：未获公开授权的验证任务数据、真实会话的原始日志、内网端点与本机路径都不包含在发布仓库中。
1.2.0 取证报告里的真实任务内容已改写为中性描述，数字与结论未变；报告里提到的提交号与 `work/…` 路径属于开发机的本地仓库。

---

## 1.2.0 — 2026-11-25

> 取证、根因、修复与验证全文：[`reports/REPORT-SAC-MULTITURN.md`](reports/REPORT-SAC-MULTITURN.md)；
> τ²-bench 184 题官方脚本对照：[`reports/REPORT-TAU2-SAC120-MULTITURN.md`](reports/REPORT-TAU2-SAC120-MULTITURN.md)。

**一句话**：论文的设定是一个会话一个任务，真实会话不是。1.1.0 及之前只把**第一条**用户消息当任务锚点，用户后面说的话被当成普通内容折进摘要——
表现为「用户换了目标，代理仍围着头一个目标转」。1.2.0 起**每一条用户指令都钉在上下文里，只折叠指令之间的工作**。

### 修复了什么

两个真实的多轮会话里，后续指令分别只被 **2 / 429、2 / 294、2 / 222、2 / 39** 次模型回复看到原文，真正的任务只被 **3 / 274** 次看到。根因有四条，逐条修掉：

| 问题 | 修复 |
|---|---|
| **后续用户消息被当成普通内容折叠**：新消息落在可折叠区间里，凑够 2 个动作就随区间一起被摘要替换 | **每条用户指令都是锚点**（`source.kind` 为 `user`，或 goal 插件的 `goal`）。surface 被切成「指令之间的工作段」：最新的叫 active，之前的叫 frozen（已结束的回合），最老的指令之前叫 archive。**折叠范围永远不含指令**（`pinInstructions`、`instructionSourceKinds`） |
| **状态更新根本看不到用户指令**：lean 模式下状态更新的输入只有「上一份状态 + 最近两步动作」，359 次状态更新里只有 8 次的输入含用户原话，于是 recap 的 Objective 跟着最近两步漂移 | 状态更新**以所属指令的原文为锚**（指令原文放在被折叠片段之前），模板改为「Objective 取自这条指令，绝不取自最近的动作」。frozen 段的折叠以它自己那条指令为锚 |
| **锚点选错**：取「第一条 user/message」不管是谁发的，一条系统通知成了锚点，真正的任务在第 1 次折叠就被吃掉 | `runtime-context`、`skill-catalog`、`time-context`、`tool-jobs`、`user-approval`、`working-directory` 这些 harness 通知不是锚点，照旧可折叠 |
| **输出契约被凭空发明**：两个会话里用户都没有要求过格式，模型仍在 46 份 recap 里写出 JSON 契约，之后的每一份 recap 都忠实地继承它 | 用户没要求格式时写 `(none)`，不要发明 |

其余改动：

- **归档**：最多 `maxPinnedInstructions`（默认 4）条指令保持原样；更早的连同它的状态并入一段归档，归档里仍然**逐字引用**这些指令（机器维护的 `[SAC EARLIER USER REQUESTS]` 段，`instructionChars` 上限），所以用户写下的话永远不会被摘要掉。应用重启后这份列表从 surface 上的 checkpoint 读回。
- **goal 轮次的重发不算新锚点**：`<goal_round>` 去掉 `Round: n/m` 这一行后文本相同的，是同一个目标；改过目标文本的才算新指令。
- **回答用户的第一步永远不被一次折叠拖慢**：frozen / archive 的折叠只在 active 没东西可折的步骤里做，并且要等模型对新请求至少做过 1 个动作（硬预算触发时不受此限）。
- **重试熔断**：被后端判为「not smaller」的 frozen / archive 折叠每段最多试 2 次，不再每步重试一次白付一次模型调用。
- **后端自己发起的折叠**（压力恢复、`/compact`）没有计划：以范围内最新的指令为锚，范围内的每条指令都逐字带进新 checkpoint。
- **遥测**新增 `frozenFolds`、`archiveFolds`、`carriedRequests`、`refusedFolds`；日志行新增 `segment=active|frozen|archive request=<seq> released=<n>`。
- **逃生口**：`pinInstructions: false` 完整退回 1.1.0 的单锚点行为。

### 怎么验证的

- **78 个测试**（原 48 个全部保留并通过，新增 30 个）。其中一条把 0–5 个动作 × 有无结尾回复的单任务 surface 逐个比对两种策略的计划：`deepEqual`。
- **回放保真**：基线插件在两份日志里复现 360 / 360 次折叠；修复后恰好只在 8 处不同——正是范围里含用户请求的那 8 次。
- **反事实仿真**（不调用模型）：两份日志里「折叠范围含用户请求」的次数 **5 → 0**；后续请求被看到原文的回复数 2/429、2/294、2/222、2/39 → **全部**；goal 被 3/274 → **274/274**。
- **真实运行时冒烟**：3 轮的小会话里 4 次折叠没有一次包含用户消息；之后的 surface 正是设计里的形状 `system · 请求1 · 状态1 · 回复1 · 请求2 · 状态2 · 回复2 · 请求3 · 工作区`。
- **τ²-bench 184 题（官方脚本）**：521 条后续用户消息 **100%** 在到达之后的每一个模型步都原样在请求里（旧版 1.1.0 把 347 条折进了摘要，平均只在 54.1% 的模型步里可见）。
  成功率 152 / 184，与不压缩的 150 没有可分辨的差异（+1.1 pp，p = 0.86）。逐轮评审里「忘了用户已经说过的、重复旧内容、或者接着做用户已经放弃的请求」这类记忆类失误：SAC 1.2.0 **0.14%**，论文式的 EoCC 2.56%。

### 代价，以及它引出了 1.3.0

- **单任务会话不受影响**：一条指令、一份滚动状态，折叠范围与之前逐 span 相同，成本不变。
- **多轮会话**：每个被钉住的回合多占「它的状态 + 它的最终回复」，约 1.5K token。想更省就把 `maxPinnedInstructions` 调成 1（只留手上这条指令加归档）。
- **但折叠本身变贵了**：τ² 184 题上账面 token 是不压缩的 1.75×，24% 的折叠尝试失败。把它逐次拆开看，就是 1.3.0 的出发点（见上）。

---

## English

This file records the changes of two packages: `@local/dsh-sac-compact` (the compaction plugin) and `@local/dsh-sac-preset` (the GUI preset). The sections above are the full Chinese changelog; what follows is the English summary of the same facts.

### 1.3.0 — 2026-11-28

**In one sentence**: folding is now triggered by cost instead of by action count — a short dialogue never folds (it sends the same requests as full history), a long session folds and keeps the request near the water mark, and recaps are complete instead of making the agent re-read what was folded away. It also fixes a configuration drift in the GUI preset, which had kept running the old behaviour.

**Why.** On the 184-task τ²-bench run (qwen3.8-27b, thinking mode) every fold of 1.2.0 was a bad trade: 820 attempts, of which 186 were refused as "not smaller" and 13 returned no text (24% paid a model call for nothing); a model-written recap cost ≈4.2K tokens and gave back a median 600 tokens of context; 147 of the 620 "successful" recaps were cut off at the output cap and accepted silently, and 156 more were cut again by the early-fold budget; folding invalidated the prefix cache (57–72% cache hits against 82–86% for full history); on the 36 longest dialogues 27–29% of the tool calls repeated an earlier call. Billed cost was 1.75× full history for an average context of 0.87×. Folding after every two actions is right for a 300-step coding session and wrong for a ten-step dialogue.

Four design principles, drawn from 13 papers: (1) do not fold when a fold does not pay; (2) whatever the harness can write, do not ask the model to; (3) a model-written recap must be complete and no larger than its span; (4) the trigger is cost, not action count.

**What changed** (11 mechanisms, each a config key; see the table above for the keys):

- **Size trigger with hysteresis** — nothing folds while the whole request is under the high-water mark; above it the policy folds down to the low-water mark. The marks are relative to the fixed prompt (system prompt + tool schemas), so one setting fits a 5K customer-service prompt and a 20K coding-agent prompt.
- **Gain guard** — a model recap only replaces a span that frees at least the recap plus a margin.
- **Mechanical folds** — when the verbatim digest of a span is small enough the harness writes the checkpoint itself (earlier state carried forward, one line per action, structured JSON results compacted instead of cut) and makes no model call. A mechanical fold costs no waiting, so it may free an earlier turn before the model's first step on a new request.
- **Heavy final replies fold** with their turn's digest; the visible text stays.
- **Bounded recaps** (`recapMaxRatio`) — the recap must stay under a share of its span, so the backend no longer refuses folds as "not smaller", and the early-fold budget no longer cuts a complete recap.
- **Cut-off detection and retry** — the state update reads the stream's finish reason; a cut-off or empty recap is retried once with a doubled output cap that stays up for the session. Thinking models start from a larger cap.
- **The recap's word contract follows the span** — half the span in words (never more than 4 × `stateMaxWords`, never more than the budget) instead of a fixed 400, and a cut-off retry no longer asks for fewer words. A fixed 400-word contract had made the summarizer replace an accumulated table of 60 records with a count of it.
- **Digest shrink to budget**, a **separate summarizer route** (`stateProvider` / `stateModel`, a non-reasoning model writes the recaps in about 5 s instead of 20–70 s), **goal-round dedupe** (`goalDedupe`: a goal re-delivered 27 times leaves at most 2 copies on the surface instead of 27), and **standing instructions** kept out of the state update (`standingSourceKinds`).

The shipped bundle config **is** this policy (the keys at the end of `cordis.patch.yml`): fixed prompt + 32K / + 16K, gain 400, recap ≤ 60%, harness-written digests up to 4K for finished turns only, 4,096 first cap for thinking models.

**Fixed: the GUI preset's configuration drift.** The 1.3.0 policy lives in the compaction bundle's config, but the preset carries a separate copy of the `sac-compact` config, and that copy still had only the 1.1.0 / 1.2.0 keys. Because the code defaults are still the 1.2.0 behaviour, sessions created with the "SAC" preset in the GUI kept folding after every two actions — the policy the measurements had just shown to be a bad trade. The preset is now 1.3.0: its 47 config keys are identical to the bundle's, in the same order, and a new test (`preset-policy.test.js`, 7 cases) reads both YAML files and fails on any drift. Verified with the real DSH loader (`dsh --dump-config`).

**Measured.**

| Scenario | Full history (FHA) | SAC 1.2.0 | SAC 1.3.0 (shipped, 32K) |
|---|---|---|---|
| τ²-bench 36-task subset, online, qwen3.8-27b | 75.0% (108/144), peak prompt 38.3K | billed 1.88×, 27% repeated calls, 50 of 170 folds failed | 2 folds in 108 sessions; tokens 0.94×, billed 0.92×, 0 failed folds; score 71.3% (77/108), within the A/A noise band (FHA's own four trials: 29 / 28 / 25 / 26) |
| 40-record memory test (120 facts + a total) | 120/120; 3.28M tokens; peak 144K | **74/120** on qwen3.8-27b, wrong total, re-read all 40 files | **120/120 in 6 of 6 runs**; 1.08M tokens (**−67%**); peak 35–41K; **no repeated reads** |
| 80 records (GLM-5.3-Inner) | 240/240; 11.57M tokens; peak 269K | stuck in a re-read loop (387 requests, 238 repeated reads, no answer) | 240/240 with no re-read in 2 of 3 runs: 2.11–2.28M tokens (**−81%**), peak 38–39K |
| 60 records × 4 user turns (GLM-5.3-Inner) | all correct; 6.38–6.85M | — | clean in 3 of 4 runs (1.27–1.98M); a changed instruction was obeyed every time |
| SWE-bench Verified (3) + LOONG (5), GLM, n = 1 | 6/8, 974K tokens, 780 s | 7/8, 1.29M, 35 folds, 1,839 s | **7/8**, 1.13M, **3 folds**, 737 s |
| 13 real long / goal sessions (shadow replay) | 2.19B tokens, peak 1.38M | 119M tokens, 2,558 model recaps | **189M tokens (−91%)**, peak 52K, **503 model recaps**; latest instruction visible in all 5,269 model steps |

**The most important counter-example.** Digesting the *working* state after every step (no model call at all) made the agent redo its work: 53–57% repeated tool calls on τ²-bench and 1.7–2.0× the model steps, and a 17-request SWE task stretched to 43–120 requests. A digest cuts the intermediate results the agent still needs, whereas a model recap keeps them as established facts. The shipped config therefore digests **finished turns only** (`mechanicalSegments: frozen`).

**Compatibility.** Every mechanism is off in the code defaults (`SAC_DEFAULTS` are still the 1.2.0 values), so a host that mounts the plugin without the bundle config gets the old behaviour. 1.3.0 code with the 1.2.0 configuration reproduced all 820 logged fold decisions with 0 differences. To get the 1.2.0 behaviour back: `foldTrigger: actions` and zero the margin, gain, ratio, digest and thinking-cap keys (the recipe is in the comments at the end of `cordis.patch.yml`). `contextBudgetTokens` (96000) remains the hard safety net.

**Upgrade notes.** A preset composition is fixed when a session is created: fully quit and restart the client, then create a new session. On other machines use the offline kit `sac-deploy-kit-v1.3.0.zip` attached to the GitHub Release (`update-sac.cmd -check` → `update-sac.cmd` → `update-sac.cmd -rollback`; `./update-sac.sh` on macOS). Not seeing a `dsh-sac-compact: folded` line in a short dialogue is expected.

**Known limitations.** Small samples (36 τ² tasks, 1–4 trials per arm, so scores are only "within noise"; SWE / LOONG one run per cell). The long-session numbers come from shadow replay. One model family per benchmark; the non-reasoning summarizer route was validated on one provider only. On tasks that must carry many exact values for a long time, a GLM-written recap sometimes drops part of the accumulated table and the agent re-reads to recover (1 of 3 runs at 80 records); wiring the existing `ledger` into the 1.3.0 policy is the likely fix. `maxPinnedInstructions` is not a hard cap (48 of 184 sessions ended with 5–9 pinned). The Windows-specific parts of the deploy kit were exercised through a simulated win32 platform, not on a real Windows machine.

**Tests.** `@local/dsh-sac-compact` 48 → **108**; `@local/dsh-sac-preset` **7**; `@local/dsh-tau2-bridge` 4.

### 1.2.0 — 2026-11-25

**In one sentence**: the paper assumes one task per conversation; a real session does not. Up to 1.1.0 only the *first* user message was the task anchor, so every later message was ordinary foldable content and was summarized away after two actions — the agent kept working on the first task after the user changed the goal. From 1.2.0 **every user instruction is pinned and only the work between instructions is folded**.

In two real multi-turn sessions the later requests were seen verbatim by only 2 / 429, 2 / 294, 2 / 222 and 2 / 39 model replies, and the actual goal by 3 / 274. Four causes, each fixed:

| Problem | Fix |
|---|---|
| Later user messages were folded as ordinary content | Every user instruction is an anchor (`source.kind` `user`, or the goal plugin's `goal`). The surface is split into the work segments between instructions — active, frozen (finished turns), archive — and **a folded span never contains an instruction** (`pinInstructions`, `instructionSourceKinds`) |
| The state update never saw the instruction (lean mode sends only the folded span), so the recap's Objective drifted with the last two actions | The state update is **anchored on the request it belongs to** (quoted verbatim in front of the span); the objective is taken from that request, never from the last actions |
| The anchor was chosen without checking who sent the message, so a harness notice became the anchor and the task was folded at the first fold | Harness notices (`runtime-context`, `skill-catalog`, `time-context`, `tool-jobs`, `user-approval`, `working-directory`) are not anchors and fold as before |
| The summarizer invented an output contract the user never asked for (46 recaps carried a JSON contract that later recaps inherited) | The paper template writes `(none)` when the user did not specify a format |

Also: at most `maxPinnedInstructions` (default 4) requests stay verbatim and older ones merge into an archive state that still **quotes them verbatim** (a machine-maintained section, `instructionChars`), so nothing the user wrote is ever paraphrased away; a re-delivered `<goal_round>` is not a new anchor; the model's first reply to a new request is never held up by a fold; spans refused as "not smaller" get two attempts instead of one call per step; folds the backend starts on its own (pressure recovery, `/compact`) quote every request inside the span into the new checkpoint; new telemetry (`frozenFolds`, `archiveFolds`, `carriedRequests`, `refusedFolds`) and a `segment=… request=…` log field. `pinInstructions: false` restores the 1.1.0 behaviour.

**Verified.** 78 tests (was 48). The baseline plugin reproduces 360 / 360 logged folds; the fixed plugin differs in exactly 8 places, the folds whose span contained a user request. In counterfactual simulation "folds containing a user request" went 5 → 0, and every later request was visible to every reply. On τ²-bench (184 tasks, official scripts) all 521 follow-up user messages were verbatim in every model step (1.1.0 folded 347 of them away), with no distinguishable score difference from full history (152 vs 150); memory-class mistakes were 0.14% against 2.56% for the paper-style EoCC.

**Cost.** Single-task sessions are unchanged. In a multi-turn session each pinned turn costs about 1.5K tokens (its state plus its final reply); set `maxPinnedInstructions: 1` to keep only the request in hand plus the archive. Folding itself, however, had become expensive — billed tokens 1.75× full history on τ²-bench and 24% failed fold attempts — which is where 1.3.0 starts.
