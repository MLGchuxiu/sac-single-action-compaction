# 基准测试台：用真实 DSH 会话测 SAC 插件

> 目的：把 `plugins/dsh-sac-compact`（session-fdac86bd 修好的版本：懒安装 A1 + 遥测 A3）
> 放进**真实的 `dsh headless` 会话**里，在 τ²-bench、Loong、SWE-bench Verified 上跑，
> 与不挂 SAC 的同模型基线（FHA）对比。模型统一为 **GLM-5.3-Inner**（jms-proxy）。

## 1. 为什么不用 Python 测试台

`harness/sac/*` 是论文机制的复现（v9–v13 只存在于那里）。这次的要求是测**插件本身**，
所以每个任务都是一个真实的 DSH agent 会话：`dsh --profile sac-bench headless --json "<task>"`。
插件通过 `agent/pre-step` → `ctx.compaction.compactRegion()` 生效，压缩事件写进会话日志。

## 2. 两个 profile（唯一变量是 SAC）

| profile | 组成 | 用途 |
|---|---|---|
| `sac-bench` | `dsh-base` + `dsh-headless` + `@local/dsh-sac-compact` | 实验组 |
| `fha-bench` | `dsh-base` + `dsh-headless` | 对照组（全历史） |

两者都打了同样的 patch：

- `agent-default-model` = `jms-proxy / GLM-5.3-Inner`（上下文 1,000,000）；
- `tool-web` / `web` 关闭——基准跑测必须离线、可复现；
- `sac-compact` 额外声明 `inject: [llm]`：GUI 预设把它挂在 agent 上下文里，
  而 profile 根挂载需要显式注入 `llm`，否则 `statePrompt: paper` 会报
  `cannot get property "llm" without inject`（这是本次搭台发现的一个部署差异）。

沙箱：`workspace-write`，workspaceRoot = 本次运行目录；`--approval-policy never`。

## 3. 隔离

- 每个（任务 × 范式 × 迭代）一个独立工作目录，位于 **`~/dsh-bench-runs/<iter>/<bench>/<task>__<arm>__r<n>`**，
  刻意放在项目目录之外：agent 探索 cwd 时不会读到 `benchmarks/` 里的标准答案。
- 每个目录用 APFS clone 从模板复制，运行结束不回写模板。
- 会话日志按 cwd 分目录存放在 `~/.dsh/sessions/`，互不干扰。
- web 工具关闭，模型不能联网取答案。

## 4. 三个基准的适配

### 4.1 Loong（长输入选择性阅读）

- 6 个任务：paper L3 ×2、financial L2 ×2、legal L2 ×1、legal L3 ×1（22–140 KB）。
- 文档按官方加载逻辑落到 `./docs/doc_01.txt …`（financial 的 glob 规则、legal 的
  `content+result` 规则、paper 的首行标题规则都照搬）；prompt 只给 instruction + question。
- 判分：结构化答案（paper 的 Reference/Citation、legal 的类别→文书列表）用集合 F1；
  span 答案（financial）用规范化包含 + token F1。

### 4.2 SWE-bench Verified（真实仓库修复）

- 3 个实例：`sympy__sympy-24066`、`sympy__sympy-20590`、`psf__requests-5414`。
- 工作区 = codeload 下载的 base_commit 源码树 + `git init`（用于取 diff）。
- 判分 = 基准自己的口径：把官方 `test_patch` 打在 agent 的树上，用 venv 跑
  `FAIL_TO_PASS`；全部通过 = resolved（1.0）。测试补丁打不上时退化为文件级定位分。
- 没有 Docker：不重建镜像，依赖用共享 venv + `PYTHONPATH=<repo>`。因此只选纯 Python、
  依赖轻的实例；这一点在报告里明确标注。

### 4.3 τ²-bench（政策约束下的工具规划）

- 6 个任务：retail ×3、airline ×2、telecom ×1（v1.0.1 golden set）。
- τ² 的官方跑法是「LLM 用户模拟器 + 领域工具改数据库」的多轮编排；这里用**静态单轮变体**：
  把 `policy.md`、`tools.md`、`request.md` 放进工作区，agent 读完后输出
  `{"tool_calls":[...],"final_message":"..."}`。
- 判分：没有数据库可查，参数值不可观测，所以按**工具名序列的 LCS**（0.6）+ **参数键齐全度**（0.2）
  + **communicate_info 覆盖**（0.2）打分。`nl_assertions` 只记录不判。
- 局限：这不是官方多轮分数，只测“政策理解 + 计划结构”；报告里单列。

## 5. 指标

从 CLI 事件流（`--json` 的 `status/step_end.usage`）和会话日志（`compaction/*`）提取：

| 指标 | 来源 |
|---|---|
| 主请求数 / 每请求 prompt tokens / 峰值上下文 | `step_end.usage.inputTokens + cacheReadTokens` |
| 折叠次数 / 折叠错误 | `compaction/end`（有 `error` 记失败） |
| 状态更新 token | `compaction/summary.usage` |
| 墙钟 | 会话事件首尾时间戳 + 进程墙钟 |

## 6. 复现

```bash
cd harness
python3 -m bench.matrix --bench loong,tau2,swe --arms sac,fha --iteration iter0 --workers 6
python3 -m bench.report work/bench/results/iter0_*.jsonl --out ../reports/bench-iter0.md
```

插件版本快照在 `work/bench/plugin_versions/iterN/`（`index.js`、`sac-policy.js`、profile patch）。
## 7. 128K 优化线（2026-11-17 起）

为在沙箱内可复现地跑基准，测试台新增三件事（见 `harness/bench/runner.py`、`work/goal_round1/`）：

- **工作区内 DSH_HOME**：`work/dshhome/{profiles,sessions}`，profile 与 session 日志都可写；
  运行工作区仍放 `/tmp/dsh-bench-runs`（在项目目录之外，agent 看不到 `benchmarks/` 金标）。
  启动脚本：`work/goal_round1/run_matrix.sh`（导出 `DSH_HOME`、`BENCH_RUN_ROOT`）。
- **arm → profile 映射**：`--arms fha32,n2t4L4,...` 自动映射到 `fha-bench-32` / `sac-bench-<arm>`，
  因此一次矩阵可以同时比较多个 SAC 配置与不同输出上限。`work/goal_round1/make_profile.py`
  按 `key=value` 生成变体 profile（含 `modelMaxTokens`）。
- **重复与断点续跑**：`--repeats N` 每格跑 N 次；结果 JSONL 按 key 去重，可续跑。
  汇总：`python3 work/goal_round1/summarize_results.py work/bench/results/<iter>_*.jsonl [--tasks]`。

指标口径：`total = prompt + completion + summary_prompt + summary_completion`；
`inline_folds / paper_folds` 区分状态更新是模型自写还是摘要调用；峰值取每请求
`inputTokens + cacheReadTokens` 的最大值。
