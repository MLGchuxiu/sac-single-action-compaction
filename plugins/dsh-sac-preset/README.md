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
  name: '@local/dsh-sac-compact'
  config:
    enabled: true
    actionsPerCompaction: 1   # 严格 SAC：每个动作折叠一次
    minSpanChars: 1200
    statePrompt: paper        # 论文 Appendix G.1 的状态模板
    stateMaxTokens: 2048
```

其余插件（工具、plan mode、subagent、office、web 等）与 `standard` 完全一致。

## 安装 / 移除

用 Plugins → Add plugin 选本目录安装；或 `plugin_manager` 的 `install_bundle`。
移除时在 Plugins 里删除 `@local/dsh-sac-preset`；已选该预设的旧会话保留创建时的组合，
新会话不再提供该预设。

## 注意

- 预设组合在会话创建时固化；修改本文件后需要新建会话才生效。
- `statePrompt: paper` 会覆盖该预设隔离组内 `compaction-basic` 的 `summarize()`，只影响选了
  该预设的会话。
- 本 bundle 依赖 `@local/dsh-sac-compact`（`link:` 到仓库内 `plugins/dsh-sac-compact`），
  移动仓库后需重装。