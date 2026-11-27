# session_replay — 用真实会话日志离线验证 SAC 策略

把一份 DSH 导出的 `session.v4.jsonl` 重放一遍，**不调用模型**，用来回答「模型在某一步到底看到了什么」
以及「换一个策略之后它会看到什么」。全部脚本只读日志；日志里的内容不会发到任何地方。

| 脚本 | 作用 |
|---|---|
| `saclog.py` | 日志重放库：按 `surfaceOp` 还原每个事件发生时的 surface，提供 `Session.surface_at(seq)` |
| `replay.mjs` | 在日志里每一个 `compaction/start` 处，用插件真实的 `planSacSpan()` 重算折叠范围并与日志比对 |
| `simulate.mjs` | 反事实仿真：忽略日志里的折叠，在每次 pre-step 用被测策略重新决定折叠，checkpoint 用日志里同时刻的摘要长度代替 |
| `compare.mjs` | 一份日志的 before/after 对比（JSON） |
| `objectives.py` | 逐次折叠打印 recap 的 Objective 行，用来看目标漂移 |
| `smoke_multiturn.sh` | **真实运行时冒烟**：用工作区本地的 `sac-default` profile（链接到工作区插件）在隔离的 DSH home 里跑同一个会话的 3 轮，每轮一个新请求；结束后用 `replay.mjs` / `simulate.mjs` 检查产出的日志 |

> 合成数据的 A/B 脚本（`ab_objective.py`）未包含在发布仓库中；它在 1.2.0 报告里的结论是「不能证明、也不能否定」。

> 在 DSH 会话里跑这些脚本时注意：环境里带着真实的 `DSH_HOME=~/.dsh`。`smoke_multiturn.sh` 会强制改用 `work/dshhome`；
> 手工调用 `dsh` 时也要自己设 `DSH_HOME`，否则会去动真实的 profile 和会话目录。

## 准备基线

被测的是工作区里的 `plugins/dsh-sac-compact`（修复后）；基线是多轮修复之前的最后一次提交 `e01ac12`：

```bash
mkdir -p work/sac-bug/old-plugin
git archive e01ac12 plugins/dsh-sac-compact | tar -x -C work/sac-bug/old-plugin
unzip -q dsh-session-<id>.zip -d work/sac-bug/sessions/<name>
```

## 用法

```bash
cd harness/session_replay
LOG=../../work/sac-bug/sessions/e64b/session.v4.jsonl

# 1) 回放保真：基线插件必须复现日志里的每一次折叠
node replay.mjs $LOG old          # -> reproduced: 226, different: 0
#    工作区插件只在「折叠范围里含用户请求」的那几处不同
node replay.mjs $LOG new

# 2) before/after 仿真
node simulate.mjs $LOG both                          # 两个策略各跑一遍
node simulate.mjs $LOG new --max-pinned 1            # 最省上下文的归档设置
node simulate.mjs $LOG new --surface 363,375,4576    # 打印某些 pre-step 时刻模型看到的 surface
node compare.mjs $LOG > compare.json

# 3) 目标漂移
python3 objectives.py $LOG | less

```

`simulate.mjs` 里 `old` 的位置可用环境变量 `SAC_OLD_PLUGIN=<dir>` 覆盖。

## 局限

- 仿真只替换「折叠策略」，模型与工具的事件仍按日志原样回放，所以它衡量的是**策略**（模型会看到什么），
  而不是修复后模型会怎么做。摘要长度取日志里同一 step 的真实摘要长度；新出现的归档折叠用中位数加上逐字引用的请求长度。
- 「看到原文的回复数」= 该用户消息在 surface 上时产生的 `assistant/message` 事件数。
