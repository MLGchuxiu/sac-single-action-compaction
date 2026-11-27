# @local/dsh-sac-preset

为「单动作压缩」实验准备的**独立 Agent 预设**：`standard` 预设的完整组合 + 论文
[*You don't need long context*](https://arxiv.org/abs/2611.05306) 的 SAC 单动作压缩策略
（[`@local/dsh-sac-compact`](../dsh-sac-compact/README.md)）。

它解决的问题：DSH 的插件作用域只有两种——整个 profile（全局）或单个 agent 预设（每个会话）。
工作区只是目录记录，没有工作区级插件配置。所以「只在一个工作区里生效」的做法是：

1. 安装本 bundle，得到一个名为 **SAC 单动作压缩** 的预设；
2. 新建工作区（任意目录，例如 `/path/to/workspace`），在 GUI 里 Add workspace；
3. 只在该工作区里新建会话时选这个预设。其它会话仍用 `standard`，完全不加载 SAC。

## 与 standard 的差异

只有一处：`compaction` 隔离组里，在 `compaction-basic` 之后多了一行 `sac-compact`：

```yaml
- id: sac-compact
  name: '@local/dsh-sac-preset/sac'
  inject:
    - systemPrompt
    - tools
    - llm
  config:
    # 基础键（1.1.0 的调校）
    actionsPerCompaction: 2
    minSpanChars: 1200
    minSpanTokens: 400
    statePrompt: paper        # 论文 Appendix G.1 的状态模板
    stateMaxTokens: 2048
    keepLastObservation: true # 最新一条观测保持原样
    stateInputLean: true      # 状态更新请求不带 system prompt / 工具 schema
    stateMaxWords: 400
    contextBudgetTokens: 96000
    # 1.2.0 多轮：用户指令逐条钉住，只折叠指令之间的工作
    pinInstructions: true
    instructionSourceKinds: [user, goal]
    maxPinnedInstructions: 4
    instructionChars: 4000
    # 1.3.0 成本感知折叠：按请求大小触发，而不是按动作数
    foldTrigger: size
    foldMarginTokens: 32000   # 整个请求超过「固定提示 + 32K」才折，折到「固定提示 + 16K」
    maxFoldsPerStep: 3
    foldMinGainTokens: 400    # 模型 recap 只替换至少比它大一截的片段
    recapMaxRatio: 0.6        # recap 不超过片段的 60%
    mechanicalFoldMaxTokens: 4000  # 4K 以内的摘要行由 harness 自己写，不调模型
    mechanicalSegments: frozen     # ……只用于已结束的轮次
    replyReasoningFoldChars: 1500  # 推理很重的最终回复随轮次一起折叠
    stateMaxTokensThinking: 4096   # 思考模型首轮 recap 的输出上限
    goalDedupe: true               # 自动轮次重投的 goal 去重
    # ……其余键与 ../dsh-sac-compact/cordis.patch.yml 逐键相同
```

这段配置必须与压缩插件 bundle 自带的 [`cordis.patch.yml`](../dsh-sac-compact/cordis.patch.yml) **逐键相同**，
两处只是注释不同。[`test/preset-policy.test.js`](test/preset-policy.test.js) 读取两个文件做比较，
任何一边改了另一边没改，测试就会失败——1.3.0 发布时正是漏了预设这一边（见下面「为什么需要 1.3.0 预设」）。

其余插件（工具、plan mode、subagent、office、web 等）与 `standard` 完全一致。
完整配置说明见 [`../dsh-sac-compact/README.md`](../dsh-sac-compact/README.md)。

## 成本感知折叠（1.3.0）

1.2.0 及之前的预设「每 2 个动作折叠一次」。在 τ²-bench 的 184 题上这套触发方式是亏的：一次模型写的 recap
约花 4.2K token，只换回中位 600 token 的上下文，24% 的折叠被拒绝、24% 的 recap 被截断，最长的 36 题上
29% 的工具调用是「折掉了再读一遍」。1.3.0 改成**按代价触发**：

- 整个请求（系统提示 + 工具定义 + surface）超过「固定提示 + 32K」才折叠，折到「固定提示 + 16K」为止。
  **短对话一次都不折**，与不压缩时是同一组请求；长会话保持在水位附近。
- 模型 recap 只替换至少比它大一截的片段；已结束轮次的动作由 harness 写成精确的摘要行，不调模型。
- 多轮修复（指令逐条钉住）不变。

实测（[`reports/REPORT-SAC-PARETO.md`](../../reports/REPORT-SAC-PARETO.md)）：40 条记录的记忆测试
120/120、token −67%；13 个真实长会话重放，token 21.9 亿 → 1.89 亿。
想上下文更小，把 `foldMarginTokens` 降到 `24000` 或 `12000`。

**为什么需要 1.3.0 预设**：压缩插件 1.3.0 的代码里 `SAC_DEFAULTS` 仍是 1.2.0 的值，成本感知策略写在
bundle 的 `cordis.patch.yml` 里。预设走的是另一份配置（本 bundle 的 `cordis.patch.yml`），
2026-11-27 之前那份配置没有 1.3.0 的键，所以 GUI 里选 SAC 预设的会话一直跑的是旧行为。
本预设 1.3.0 补齐了这些键，并加了测试防止再次漂移。

## 多轮对话（1.2.0）

1.1.0 及之前，只有**第一条**用户消息是任务锚点；用户后续发的消息会被当成普通内容，
在 `actionsPerCompaction` 步之后被摘要替换掉。结果是：用户换了目标，代理仍围着头一个目标转，
摘要里的 Objective 也跟着最近两步动作漂移（两个真实会话的取证见
[`reports/REPORT-SAC-MULTITURN.md`](../../reports/REPORT-SAC-MULTITURN.md)）。

1.2.0 起，每一条用户指令（`source.kind` 为 `user`，以及 goal 插件每个 round 重新下发的
`<goal_round>`）都原样留在上下文里，只折叠指令之间的工作；状态更新时会把它所属的那条指令
原文放在最前面，Objective 从指令本身取，而不是从最近的动作里猜。最近 4 条指令保持原样，
更早的并入一段归档状态，归档里仍然逐字引用这些指令。

单个任务的会话（基准测试）不受影响：一条指令、一份滚动状态，折叠范围与之前完全相同。
多轮会话里每个被钉住的回合多占约 1.5K token（它的状态 + 最终回复）；想更省就把
`maxPinnedInstructions` 调成 1（只留手上这条指令加归档），想多留几轮原文就调大。
`pinInstructions: false` 可以退回 1.1.0 的行为。

## 安装 / 移除

用 Plugins → Add plugin 选本目录安装；或 `plugin_manager` 的 `install_bundle`。
移除时在 Plugins 里删除 `@local/dsh-sac-preset`；已选该预设的旧会话保留创建时的组合，
新会话不再提供该预设。

## 测试

```bash
node --test test/*.test.js
```

7 个用例：读取两个 `cordis.patch.yml`，断言预设配置与出厂 bundle 配置逐键相同、解析出的就是成本感知策略；
同一段 3 个动作的短对话，1.3.0 策略在水位以下不折叠，而 1.2.0 的触发方式会折叠；超过水位后工作区折叠、
收益不够时不折叠；预设注册为 `sac`，`sac-compact` 位于 `compaction` 隔离组内、在 `compaction-basic` 之后。

## 注意

- 预设组合在会话创建时固化；修改本文件后需要**完全退出并重启客户端**、再**新建会话**才生效。
- 已经由旧版本创建的会话仍然固定在旧组合（含旧配置）上：要拿到新策略，需要重新部署本 bundle、重启 DSH，
  并**新建会话**。仓库里的 `plugins/` 目录 ≠ 机器上已安装的副本（本机的 desktop profile 用软链接指向本目录，
  所以本机改完重启即可；其它机器要用部署包更新）。
- `statePrompt: paper` 会覆盖该预设隔离组内 `compaction-basic` 的 `summarize()`，只影响选了
  该预设的会话。
- 本 bundle 依赖 `@local/dsh-sac-compact`（`link:` 到仓库内 `plugins/dsh-sac-compact`，
  `sac/index.js` 也用相对路径引用它），两个目录必须保持同级；移动仓库后需重装。