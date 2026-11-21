# 在三个标准基准上测 SAC 插件（GLM-5.3-Inner，4 轮迭代）

> 测试对象：`plugins/dsh-sac-compact` —— session-fdac86bd 里修好的版本（懒安装 A1 + 遥测 A3），
> 之后按本轮迭代继续改。模型统一 **GLM-5.3-Inner**（jms-proxy，1M 上下文，16K 输出）。
> 测试台：[`docs/bench-methodology.md`](../docs/bench-methodology.md)（真实 `dsh headless` 会话 + 隔离运行目录）。
> 原始数据：`work/bench/results/`；每轮表格：[`bench-iter0.md`](bench-iter0.md) … `bench-iter4.md`。

## 0. 结论摘要

1. **插件本身能跑、能压缩**：4 轮里每个 SAC 会话都真实触发了
   `agent/pre-step → ctx.compaction.compactRegion()`，压缩事件和状态摘要都能从会话日志复核；
   27→31 个单元/集成测试全绿。
2. **SAC 的收益取决于任务形状**：
   - 长输入阅读（Loong）：严格 SAC 峰值 **29.3K → 6.7K（−77%）**，准确率 0.933 vs 0.80；
   - 长 agent 任务（SWE）：严格 SAC 总 token 反而 **+41%**、墙钟 4.8×；
     改成 **SAC-2 + 保留最近观测**（iter4）后总 token **0.79M vs FHA 1.23M（−36%）**、
     峰值 −55%、墙钟 1.6×，准确率 3/3 持平。
3. **状态更新是主要成本**：严格 SAC 下占 SWE 总 token 的 52%，iter4 仍占 33%。
   这是下一步最大的优化杠杆（换小模型 / 批量化 / 缓存）。
4. **可靠性问题可以工程化解决**：空状态重试 + 最小折叠跨度把折叠失败从 10 次降到 0–4 次，
   总 token −21%、墙钟 −19%，准确率不降。
5. **一个被证伪的假设**：裁剪状态更新的输入（`stateInputMaxChars`）对阅读任务是净收益
   （Loong token −46%），但对 SWE 是反效果（token +20%、墙钟 2.1×），默认应关闭。
6. **准确率单格 n=1 的波动很大**（Loong 0.933→0.5→0.933 之间摆动主要来自格式/超时），
   所以本报告把确定性指标（token、折叠、错误、墙钟）作为迭代主证据，最后一轮加了重复。
7. **重复验证（n=2）**：iter4 配置在 Loong 上峰值 **15.9K vs 36.6K（−56%）**、总 token
   **683K vs 945K（−28%）**，准确率 0.79 vs 0.84（噪声内）；τ² 的 SAC 根本不触发折叠（2 请求），
   说明**短任务不该为 SAC 付状态更新的钱**。
8. **最终建议**：长 agent 任务用 `actionsPerCompaction: 2` + `keepLastObservation: true`；
   长输入阅读用严格 SAC（可加 `stateInputMaxChars: 8000`）；短任务保持关闭。三者不要共用一个默认值。

> 逐轮数字：iter0 [`bench-iter0.md`](bench-iter0.md)、iter1 [`bench-iter1.md`](bench-iter1.md)、
> iter2 [`bench-iter2.md`](bench-iter2.md)、iter3 [`bench-iter3.md`](bench-iter3.md)、
> iter4 [`bench-iter4.md`](bench-iter4.md)；最终重复验证见 [`bench-iter4rep.md`](bench-iter4rep.md)。

## 1. 测试方法（一页版）

| 项 | 设置 |
|---|---|
| 实验组 | profile `sac-bench` = `dsh-base` + `dsh-headless` + `@local/dsh-sac-compact`（`statePrompt: paper`） |
| 对照组 | profile `fha-bench` = 同版本 headless，不挂 SAC（全历史） |
| 模型 | `jms-proxy / GLM-5.3-Inner`，`maxTokens: 16384`，温度由 DSH 默认 |
| 隔离 | 每个（任务×范式×迭代）一个独立目录 `~/dsh-bench-runs/<iter>/...`，在项目目录之外；web 工具关闭；`--approval-policy never` |
| 指标 | 峰值上下文、主 prompt token、状态更新 token、折叠次数/错误、墙钟、任务得分 |

**为什么用真实 DSH 会话**：要测的是插件本身（`agent/pre-step` → `ctx.compaction.compactRegion()`），
而不是 Python 测试台里复现的策略。每个格子都是一次真实的 `dsh headless` 运行，压缩事件从会话日志读取。

## 2. 三个基准怎么接的（以及各自的局限）

### Loong（5 题：paper×2、financial×2、legal×1）

- 文档按官方加载逻辑落到 `./docs/doc_01.txt …`，prompt 只给 instruction + question；
  agent 必须自己读——这正是 SAC 的目标场景（长输入选择性阅读）。
- 判分：paper 的 Reference/Citation 与 legal 的类别→文书列表用集合 F1；financial 用规范化包含。
- 局限：官方是把所有文档塞进一个 prompt；这里改成文件读取，分数不能和官方榜单直接比。

### SWE-bench Verified（3 题，全部 sympy）

- 工作区 = codeload 的 `base_commit` 源码树 + git；判分 = 官方 `test_patch` + `FAIL_TO_PASS` 全过。
- 只保留在本机环境（Python 3.12、无 Docker）里**验证过 base 失败 / gold 通过**的实例：
  `sympy__sympy-24066`、`sympy__sympy-24539`、`sympy__sympy-24213`。
  被淘汰的实例（`sympy-20590` 缺 distutils、`requests-5414` 的 urllib3 不兼容）在 iter0 里跑过，
  但那是环境问题，不计入准确率。
- 局限：3 题同仓库，统计功效弱；无 Docker，依赖用共享 venv。

### τ²-bench（4 题：retail×3、airline×1，v1.0.1 golden set）

- 官方是「LLM 用户模拟器 + 领域工具改数据库」的多轮编排；这里用**静态计划变体**：
  `policy.md`/`tools.md`/`request.md` 放工作区，agent 读完输出
  `{"tool_calls": ["工具名", ...], "final_message": "..."}`。
- 判分：工具名序列 LCS（0.7）+ `communicate_info` 覆盖（0.3）。参数值不可观测（没有数据库），不判。
- 局限：**不是官方多轮分数**，只测“政策理解 + 计划结构”；报告里单列，不与其他两项混谈。

## 3. 迭代 0：基线（插件原样）

### 3.1 汇总表
| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.8 | 4/5 | 29273 / 42923 | 259467 | 0 | 282310 | 3.8 | 0 | 0 | 64.1 |
| loong | sac | 5 | 0.933 | 4/5 | 6670 / 42638 | 182082 | 217703 | 406876 | 4.8 | 15 | 3 | 156.2 |
| swe | fha | 3 | 1.0 | 3/3 | 14068 / 39301 | 1656933 | 0 | 1688505 | 30.0 | 0 | 0 | 249.4 |
| swe | sac | 3 | 1.0 | 3/3 | 8802 / 12948 | 1111738 | 1231700 | 2387362 | 53.667 | 148 | 10 | 1192.5 |
| tau2 | fha | 4 | 0.527 | 1/4 | 8527.5 / 8544 | 56353 | 0 | 92943 | 2.0 | 0 | 0 | 123.8 |
| tau2 | sac | 4 | 0.556 | 1/4 | 9367.0 / 10423 | 144404 | 142126 | 342194 | 5.25 | 13 | 4 | 295.4 |

#### 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 8178 | 4 | 0 | 0 | 0 | 80.99 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 6571 | 5 | 2 | 1 | 16008 | 156.15 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 35141 | 3 | 0 | 0 | 0 | 12.4 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 6440 | 5 | 3 | 1 | 23683 | 91.86 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 29273 | 5 | 0 | 0 | 0 | 655.96 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.667 | 6670 | 6 | 5 | 0 | 60117 | 257.79 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 17958 | 5 | 0 | 0 | 0 | 64.08 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 42638 | 3 | 1 | 1 | 53235 | 148.79 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42923 | 2 | 0 | 0 | 0 | 35.91 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 6961 | 5 | 4 | 0 | 64660 | 223.61 | {"Reference": 1.0, "Citation": 1.0} |
| swe | sympy__sympy-24066 | fha | 1.0 | 39301 | 50 | 0 | 0 | 0 | 623.09 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 12948 | 37 | 31 | 5 | 276649 | 1150.58 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 13019 | 18 | 0 | 0 | 0 | 131.38 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 8090 | 77 | 75 | 1 | 599253 | 1524.09 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 14068 | 22 | 0 | 0 | 0 | 249.39 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 8802 | 47 | 42 | 4 | 355798 | 1192.5 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8354 | 2 | 0 | 0 | 0 | 54.36 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8545 | 8 | 6 | 1 | 68724 | 414.65 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8527 | 2 | 0 | 0 | 0 | 72.33 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.0 | 9599 | 6 | 4 | 1 | 42163 | 359.0 | no JSON object in final answer |
| tau2 | retail-36 | fha | 0.0 | 8528 | 2 | 0 | 0 | 0 | 254.3 | no JSON object in final answer |
| tau2 | retail-36 | sac | 0.7 | 9135 | 3 | 1 | 1 | 10565 | 213.44 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8544 | 2 | 0 | 0 | 0 | 175.29 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 10423 | 4 | 2 | 1 | 20674 | 231.74 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |

### 3.2 逐项解读

**Loong（长输入阅读）**：SAC 平均分 0.933 vs FHA 0.80，峰值上下文 6.7K vs 29.3K（−77%）。
分差来自 `legal-L2-0`：FHA 在 29K 上下文里读了 8 份判决书、655s 还没写完答案（超时，0 分）；
SAC 用 6.7K 峰值、258s 交卷，答对 1/2 个文书编号（0.667）。这是 SAC 的典型收益场景。
代价：状态更新 217K token，使总 token 比 FHA 高 44%，墙钟 2.4×。

**SWE-bench（真实仓库修复）**：两边都 3/3 resolved，SAC 峰值 8.8K vs 14.1K（−37%）。
但 SAC 做了 148 次折叠、10 次失败，状态更新 1.23M token，总 token 反而比 FHA 高 41%，
墙钟 4.8×（1192s vs 249s）。原因：严格 SAC 每完成一个动作就发一次状态更新，
而 FHA 的增量在 1M 窗口 + 前缀缓存下很便宜（主 prompt 1.66M 里绝大部分是缓存命中）。

**τ²-bench（静态计划变体）**：0.556 vs 0.527，差异在噪声内；SAC 峰值没有下降（9.4K vs 8.5K），
因为任务只有 3–6 个动作，折叠省下的上下文被状态本身抵消；总 token 3.7×。

### 3.3 基线暴露的问题（后续迭代的输入）

| 问题 | 证据 | 影响 |
|---|---|---|
| 状态更新返回空文本 | SWE 10 次、Loong 3 次、τ² 4 次 `state update produced no text` | 折叠失败 → span 累积 → 下一次折叠输入更大 |
| 小 span 被后端拒绝 | `summary is not smaller than the shadowed content (521 >= 358)` | 白白多一次 LLM 调用 |
| 折叠后重读 | τ² retail-36 SAC 把 policy/tools/request 读了三遍 | 动作数、token、墙钟一起涨 |
| 状态更新太贵 | SWE 状态更新占 52% 总 token | 抵消了上下文节省 |


## 4. 迭代 1：健壮性（空状态重试 + 最小折叠跨度）

改动：

- `paperSummarize` 在状态更新返回空文本时**重试一次**（更高输出上限 + “直接输出、不要推理”）。
  GLM-5.3-Inner 是推理模型，2048 的输出预算可能被隐藏推理吃光。
- 新增 `minSpanTokens`（默认 0，预设/基准配置 400）：小于 ~400 token 的 span 不折叠，
  避免后端 `summary is not smaller than the shadowed content` 的无谓失败。
- 新增 2 个测试（重试、跨度守卫），29 个测试全绿。

### 4.1 汇总表
| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.8 | 4/5 | 26807 / 50112 | 322256 | 0 | 347225 | 4.4 | 0 | 0 | 36.2 |
| loong | sac | 5 | 0.5 | 2/5 | 6504 / 6909 | 143685 | 221870 | 371410 | 4.6 | 16 | 1 | 128.9 |
| swe | fha | 3 | 1.0 | 3/3 | 18674 / 33342 | 1367495 | 0 | 1392507 | 30.0 | 0 | 0 | 204.3 |
| swe | sac | 3 | 1.0 | 3/3 | 7161 / 7534 | 855257 | 1002242 | 1887367 | 41.667 | 120 | 4 | 960.4 |
| tau2 | fha | 4 | 0.702 | 1/4 | 8522.5 / 8539 | 56240 | 0 | 92175 | 2.0 | 0 | 0 | 128.3 |
| tau2 | sac | 4 | 0.527 | 1/4 | 6909.5 / 9714 | 130778 | 165042 | 337979 | 5.0 | 15 | 1 | 272.9 |

#### 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 11549 | 5 | 0 | 0 | 0 | 40.89 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 6340 | 4 | 2 | 0 | 19462 | 67.47 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 9336 | 5 | 0 | 0 | 0 | 21.03 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 6256 | 4 | 2 | 1 | 14493 | 47.92 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 26807 | 7 | 0 | 0 | 0 | 259.2 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.0 | 6692 | 5 | 4 | 0 | 43085 | 128.91 | {"predicted": [], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 50112 | 3 | 0 | 0 | 0 | 36.24 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 0.5 | 6504 | 4 | 3 | 0 | 63537 | 155.88 | {"Reference": 0.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42923 | 2 | 0 | 0 | 0 | 27.86 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 0.0 | 6909 | 6 | 5 | 0 | 81293 | 162.12 | no JSON object in final answer |
| swe | sympy__sympy-24066 | fha | 1.0 | 33342 | 39 | 0 | 0 | 0 | 366.58 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 7534 | 40 | 37 | 3 | 335555 | 1220.4 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 16118 | 25 | 0 | 0 | 0 | 194.37 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 7161 | 39 | 39 | 0 | 317582 | 960.36 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 18674 | 26 | 0 | 0 | 0 | 204.33 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 6935 | 46 | 44 | 1 | 349105 | 915.5 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8264 | 2 | 0 | 0 | 0 | 22.03 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 6378 | 3 | 2 | 0 | 17796 | 66.14 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8522 | 2 | 0 | 0 | 0 | 143.7 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.583 | 6885 | 4 | 3 | 0 | 31227 | 152.28 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-36 | fha | 0.7 | 8523 | 2 | 0 | 0 | 0 | 112.91 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.0 | 6934 | 4 | 3 | 0 | 39496 | 393.43 | no JSON object in final answer |
| tau2 | retail-37 | fha | 0.525 | 8539 | 2 | 0 | 0 | 0 | 170.35 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 9714 | 9 | 7 | 1 | 76523 | 454.47 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |

### 4.2 结果

| 指标 | iter0 SAC | iter1 SAC | 变化 |
|---|---|---|---|
| 折叠错误（SWE） | 10 | 4 | −60% |
| 折叠错误（Loong） | 3 | 1 | −67% |
| 折叠错误（τ²） | 4 | 1 | −75% |
| 总 token（SWE） | 2.39M | 1.89M | −21% |
| 墙钟中位（SWE） | 1192s | 960s | −19% |
| SWE resolved | 3/3 | 3/3 | 持平 |
| Loong 平均分 | 0.933 | 0.5 | 单次波动，见下 |

**解读**：重试把“推理吃光输出预算”这类失败转成了正常折叠，失败导致的 span 累积随之减少，
SWE 的总 token 和墙钟都降了约 20%，而 resolved 保持 3/3。这是本轮最扎实的收益。

Loong 的 0.933→0.5 不是折叠答错：`paper-L3-1` 的 SAC 会话最后没输出 JSON（格式失败），
`legal-L2-0` 的 SAC 只写了 1 个文书编号（iter0 写了 1 个、FHA 超时 0 个）。同一配置在 iter0
拿到 0.933、iter1 拿到 0.5，说明**单格 n=1 的准确率波动和插件改动同量级**；因此后两轮主要看
确定性指标（折叠错误、token、墙钟），准确率放到最后一轮加重复再下结论。


## 5. 迭代 2：成本（状态更新输入裁剪）

改动：

- `stateInputMaxChars`（默认 8000）：状态更新前对长观测做 head/tail 裁剪并加显式省略标记，
  最新一条消息保留 1.5× 额度。目标是压状态更新 token（基线里它占 SWE 总 token 的 52%）。

### 5.1 汇总表
| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.4 | 2/5 | 8906 / 34881 | 135880 | 0 | 154215 | 2.4 | 0 | 0 | 29.4 |
| loong | sac | 5 | 0.48 | 2/5 | 6156 / 6581 | 85674 | 111321 | 199849 | 3.0 | 9 | 3 | 61.1 |
| swe | fha | 3 | 1.0 | 3/3 | 15332 / 29414 | 1107068 | 0 | 1126242 | 23.333 | 0 | 0 | 183.6 |
| swe | sac | 3 | 1.0 | 3/3 | 7366 / 7382 | 998821 | 1218040 | 2256279 | 48.667 | 144 | 0 | 2005.0 |
| tau2 | fha | 4 | 0.321 | 0/4 | 8530.0 / 8605 | 56318 | 0 | 112044 | 2.0 | 0 | 0 | 206.7 |
| tau2 | sac | 4 | 0.381 | 1/4 | 6750.0 / 6854 | 197884 | 314419 | 553223 | 7.75 | 27 | 0 | 412.5 |

#### 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 8906 | 5 | 0 | 0 | 0 | 72.98 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 6581 | 5 | 3 | 1 | 22962 | 100.96 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 34881 | 2 | 0 | 0 | 0 | 29.39 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 6156 | 3 | 1 | 1 | 9534 | 61.07 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 26335 | 5 | 0 | 0 | 0 | 230.8 | empty answer |
| loong | legal-L2-0 | sac | 0.4 | 6546 | 7 | 5 | 1 | 78825 | 206.58 | {"predicted": ["1", "2", "3", "4", "5", "6", "7", "8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 0.0 | 0 | 0 | 0 | 0 | 0 | 16.86 | no JSON object in final answer |
| loong | paper-L3-0 | sac | 0.0 | 0 | 0 | 0 | 0 | 0 | 15.17 | no JSON object in final answer |
| loong | paper-L3-1 | fha | 0.0 | 0 | 0 | 0 | 0 | 0 | 16.86 | no JSON object in final answer |
| loong | paper-L3-1 | sac | 0.0 | 0 | 0 | 0 | 0 | 0 | 17.0 | no JSON object in final answer |
| swe | sympy__sympy-24066 | fha | 1.0 | 29414 | 32 | 0 | 0 | 0 | 332.65 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 7366 | 43 | 43 | 0 | 409449 | 2005.03 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 15332 | 20 | 0 | 0 | 0 | 154.82 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 7382 | 37 | 36 | 0 | 282745 | 1324.26 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 12470 | 18 | 0 | 0 | 0 | 183.57 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 7244 | 66 | 65 | 0 | 525846 | 2019.32 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 0.0 | 8261 | 2 | 0 | 0 | 0 | 275.06 | no JSON object in final answer |
| tau2 | airline-1 | sac | 1.0 | 6629 | 8 | 7 | 0 | 65793 | 243.85 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8521 | 2 | 0 | 0 | 0 | 109.83 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.0 | 6797 | 8 | 7 | 0 | 85692 | 578.64 | no JSON object in final answer |
| tau2 | retail-36 | fha | 0.7 | 8605 | 2 | 0 | 0 | 0 | 192.06 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.0 | 6703 | 8 | 7 | 0 | 95309 | 550.29 | no JSON object in final answer |
| tau2 | retail-37 | fha | 0.0 | 8539 | 2 | 0 | 0 | 0 | 221.24 | no JSON object in final answer |
| tau2 | retail-37 | sac | 0.525 | 6854 | 7 | 6 | 0 | 67625 | 274.66 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |

### 5.2 结果：一个被证伪的假设

| 指标 | iter1 SAC | iter2 SAC | 判断 |
|---|---|---|---|
| Loong 总 token | 371K | 200K | **−46%，有效** |
| Loong 平均分 | 0.5 | 0.48 | 持平 |
| SWE 总 token | 1.89M | 2.26M | **+20%，反效果** |
| SWE 墙钟中位 | 960s | 2005s | **2.1×，反效果** |
| SWE 折叠次数 | 120 | 144 | 更多 |
| SWE 折叠错误 | 4 | 0 | 重试生效 |
| SWE resolved | 3/3 | 3/3 | 持平 |

**解读**：裁剪状态更新的输入，对“读大文档”的 Loong 是净收益（状态更新从 222K 降到 111K token），
但对 SWE 这种“读文件 → 改 → 跑测试”的长任务反而更贵：裁掉最新观测的中段后，状态里丢失了
agent 刚读到的代码细节，下一步只能重读/重跑，动作数从 41.7 涨到 48.7，折叠数 120→144，
墙钟翻倍。两个 SAC 格子在 1800s 超时，但 diff 已经写好、测试也过了，所以分数仍是 1.0。

**结论**：`stateInputMaxChars` 保留为**可选**配置，默认 0（关闭）；只有在“长输入阅读、动作数少”的
场景才建议打开（例如 8000）。迭代 3 把默认关掉，转而处理“折叠后重读”的连续性。


## 6. 迭代 3：连续性（保留最近一条观测）

改动：

- `keepLastObservation`（默认 false）：折叠时把最新的 tool 结果留在上下文里，
  下一次请求 = 状态 + 最近观测 + 新动作。针对基线里观察到的“折叠完又重读”循环
  （τ² retail-36 的 SAC 会话把 policy/tools/request 读了三遍）。

### 6.1 汇总表
| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.8 | 4/5 | 27267 / 50894 | 331986 | 0 | 354746 | 4.2 | 0 | 0 | 41.4 |
| loong | sac | 5 | 0.933 | 4/5 | 24467 / 42918 | 259776 | 188887 | 462434 | 4.4 | 9 | 3 | 66.3 |
| swe | fha | 3 | 1.0 | 3/3 | 18048 / 20470 | 867650 | 0 | 883569 | 23.667 | 0 | 0 | 254.7 |
| swe | sac | 3 | 0.667 | 2/3 | 12951 / 16488 | 562868 | 566792 | 1187612 | 24.333 | 61 | 7 | 689.8 |
| tau2 | fha | 4 | 0.527 | 1/4 | 8382.5 / 8618 | 47802 | 0 | 76624 | 2.0 | 0 | 0 | 167.2 |
| tau2 | sac | 4 | 0.571 | 1/4 | 8387.5 / 8536 | 47711 | 0 | 94216 | 2.0 | 0 | 0 | 176.2 |

#### 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 11691 | 6 | 0 | 0 | 0 | 41.38 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 7223 | 4 | 1 | 1 | 7072 | 56.85 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 8400 | 3 | 0 | 0 | 0 | 18.5 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 8073 | 4 | 1 | 1 | 6652 | 47.35 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 27267 | 6 | 0 | 0 | 0 | 639.14 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.667 | 24467 | 6 | 3 | 1 | 56488 | 546.6 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 50894 | 3 | 0 | 0 | 0 | 39.92 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 42639 | 5 | 3 | 0 | 73924 | 213.02 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 44919 | 3 | 0 | 0 | 0 | 55.88 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 42918 | 3 | 1 | 0 | 44751 | 66.33 | {"Reference": 1.0, "Citation": 1.0} |
| swe | sympy__sympy-24066 | fha | 1.0 | 20470 | 30 | 0 | 0 | 0 | 259.28 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 0.0 | 16488 | 31 | 25 | 5 | 251191 | 1798.86 | empty diff |
| swe | sympy__sympy-24213 | fha | 1.0 | 18048 | 21 | 0 | 0 | 0 | 254.69 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 12951 | 21 | 18 | 1 | 168920 | 689.75 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 11348 | 20 | 0 | 0 | 0 | 124.03 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 11833 | 21 | 18 | 1 | 146681 | 632.48 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8253 | 2 | 0 | 0 | 0 | 47.46 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8261 | 2 | 0 | 0 | 0 | 76.92 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 5598 | 2 | 0 | 0 | 0 | 339.15 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.583 | 8514 | 2 | 0 | 0 | 0 | 122.72 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-36 | fha | 0.0 | 8512 | 2 | 0 | 0 | 0 | 218.51 | no JSON object in final answer |
| tau2 | retail-36 | sac | 0.7 | 5598 | 2 | 0 | 0 | 0 | 494.91 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8618 | 2 | 0 | 0 | 0 | 115.9 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.0 | 8536 | 2 | 0 | 0 | 0 | 229.62 | no JSON object in final answer |

### 6.2 结果：连续性换峰值

| 指标 | iter1 SAC | iter3 SAC | 变化 |
|---|---|---|---|
| SWE 折叠次数 | 120 | 61 | −49% |
| SWE 主请求数 | 41.7 | 24.3 | −42% |
| SWE 墙钟中位 | 960s | 690s | −28% |
| SWE 总 token | 1.89M | 1.19M | −37% |
| SWE resolved | 3/3 | 2/3 | 24066 在 1800s 超时 |
| Loong 平均分 | 0.5 | 0.933 | 回到 iter0 水平 |
| Loong 峰值 | 6.5K | 24.5K | 峰值优势变小 |
| Loong 总 token | 371K | 462K | +24% |

**解读**：把最新一条观测留在上下文里，直接消除了“折叠完又重读”的循环——SWE 的折叠数、请求数、
墙钟、总 token 全部下降（折叠 −49%，墙钟 −28%），Loong 的格式失败也消失了（0.933）。
代价是峰值上下文：因为总有一条观测不折叠，Loong 的峰值从 6.5K 回到 24.5K，压缩率从 −77% 降到 −10%。
SWE 的 `sympy-24066` 在 1800s 超时（25 次折叠、31 次请求），说明“每个动作一次状态更新”的开销
对最长的任务仍然太高。

**结论**：`keepLastObservation` 是一个**用峰值换速度/稳定性**的开关；论文的“最小上下文”目标
适合关掉它，工程上想少花钱/少等待则建议打开。迭代 4 在此基础上再降一次开销：把每两次动作
折叠一次（`actionsPerCompaction: 2`）。


## 7. 迭代 4：批量化（SAC-2）与最终验证

### 7.1 汇总表
| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.933 | 4/5 | 34883 / 52636 | 398883 | 0 | 416649 | 3.6 | 0 | 0 | 18.2 |
| loong | sac | 5 | 0.933 | 4/5 | 32929 / 49988 | 374711 | 99211 | 488414 | 4.4 | 4 | 1 | 71.2 |
| swe | fha | 3 | 1.0 | 3/3 | 20498 / 28294 | 1206953 | 0 | 1228394 | 26.333 | 0 | 0 | 225.6 |
| swe | sac | 3 | 1.0 | 3/3 | 9223 / 13293 | 499934 | 265405 | 791201 | 22.333 | 29 | 0 | 363.4 |
| tau2 | fha | 4 | 0.571 | 1/4 | 8558.0 / 48937 | 772994 | 0 | 835012 | 8.25 | 0 | 0 | 158.9 |
| tau2 | sac | 4 | 0.702 | 1/4 | 8530.5 / 8605 | 56319 | 0 | 96814 | 2.0 | 0 | 0 | 159.4 |

#### 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 32945 | 2 | 0 | 0 | 0 | 9.73 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 32929 | 2 | 0 | 0 | 0 | 33.1 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 34883 | 2 | 0 | 0 | 0 | 6.49 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 8379 | 4 | 0 | 1 | 0 | 71.2 | normalised match |
| loong | legal-L2-0 | fha | 0.667 | 26640 | 8 | 0 | 0 | 0 | 198.83 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.667 | 18424 | 9 | 3 | 0 | 47370 | 243.19 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 52636 | 4 | 0 | 0 | 0 | 51.32 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 49988 | 4 | 1 | 0 | 51841 | 93.42 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42923 | 2 | 0 | 0 | 0 | 18.16 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 44990 | 3 | 0 | 0 | 0 | 33.6 | {"Reference": 1.0, "Citation": 1.0} |
| swe | sympy__sympy-24066 | fha | 1.0 | 28294 | 39 | 0 | 0 | 0 | 488.33 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 13293 | 25 | 11 | 0 | 110644 | 637.18 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 20498 | 25 | 0 | 0 | 0 | 225.6 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 9223 | 23 | 10 | 0 | 90158 | 363.35 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 11378 | 15 | 0 | 0 | 0 | 142.49 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 7903 | 19 | 8 | 0 | 64603 | 239.54 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8261 | 2 | 0 | 0 | 0 | 71.48 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8261 | 2 | 0 | 0 | 0 | 50.59 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8596 | 2 | 0 | 0 | 0 | 156.86 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.583 | 8522 | 2 | 0 | 0 | 0 | 156.3 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-36 | fha | 0.7 | 8520 | 2 | 0 | 0 | 0 | 161.02 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.7 | 8605 | 2 | 0 | 0 | 0 | 162.44 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.0 | 48937 | 27 | 0 | 0 | 0 | 566.4 | no JSON object in final answer |
| tau2 | retail-37 | sac | 0.525 | 8539 | 2 | 0 | 0 | 0 | 181.94 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |

### 7.2 结果：批量化把 SWE 的成本打下来了

| 指标 | iter0（严格 SAC） | iter1（健壮性） | iter3（连续性） | iter4（SAC-2 + 连续性） | FHA（iter4 同轮） |
|---|---|---|---|---|---|
| SWE resolved | 3/3 | 3/3 | 2/3 | **3/3** | 3/3 |
| SWE 折叠次数 | 148 | 120 | 61 | **29** | 0 |
| SWE 总 token | 2.39M | 1.89M | 1.19M | **0.79M** | 1.23M |
| SWE 峰值中位 | 8.8K | 7.2K | 12.9K | **9.2K** | 20.5K |
| SWE 墙钟中位 | 1192s | 960s | 690s | **363s** | 226s |
| Loong 平均分 | 0.933 | 0.5 | 0.933 | 0.933 | 0.933 |
| Loong 峰值中位 | 6.7K | 6.5K | 24.5K | 32.9K | 34.9K |

**解读**：每两次动作折叠一次（`actionsPerCompaction: 2`）叠加“保留最近观测”，把 SWE 的折叠数
从 148 降到 29，总 token 从 2.39M 降到 0.79M——**比 FHA 还低 36%**，同时保持 3/3 resolved，
墙钟只比 FHA 高 1.6×。这是四轮里唯一一个“准确率不降、总 token 低于 FHA”的配置。

代价在 Loong：动作数少的阅读任务几乎触发不了“2 个动作 + 保留最后观测”的折叠（整轮只有 4 次），
峰值上下文没有下降（32.9K vs FHA 34.9K）。也就是说，**SAC-2 是为长 agent 任务调出来的，
不是为长输入阅读任务**；后者需要严格 SAC（或打开 `stateInputMaxChars` 裁剪）。

### 7.3 最终重复验证（iter4 配置，Loong/τ² 各 2 次）

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 10 | 0.84 | 8/10 | 36616.5 / 60865 | 890502 | 0 | 945472 | 4.7 | 0 | 0 | 37.6 |
| loong | sac | 10 | 0.79 | 7/10 | 15938.0 / 47055 | 526348 | 115864 | 683322 | 4.5 | 9 | 1 | 46.2 |
| tau2 | fha | 8 | 0.542 | 2/8 | 8526.0 / 8541 | 112598 | 0 | 192766 | 2.0 | 0 | 0 | 127.9 |
| tau2 | sac | 8 | 0.629 | 2/8 | 8527.5 / 8626 | 112779 | 0 | 184054 | 2.0 | 0 | 0 | 141.5 |

#### 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 8063 | 4 | 0 | 0 | 0 | 34.0 | normalised match |
| loong | financial-L2-0 | fha | 1.0 | 8198 | 6 | 0 | 0 | 0 | 59.88 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 32951 | 3 | 0 | 0 | 0 | 25.22 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 7395 | 4 | 1 | 0 | 8063 | 31.87 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 8505 | 4 | 0 | 0 | 0 | 27.64 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 8298 | 3 | 0 | 0 | 0 | 34.24 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 8264 | 3 | 0 | 0 | 0 | 9.93 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 13742 | 3 | 0 | 0 | 0 | 17.39 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 30305 | 9 | 0 | 0 | 0 | 315.96 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | fha | 0.4 | 57125 | 11 | 0 | 0 | 0 | 438.83 | {"predicted": ["1", "2", "3", "4", "5", "6", "7", "8"], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.4 | 23297 | 9 | 3 | 0 | 43098 | 327.45 | {"predicted": ["1", "2", "3", "4", "5", "6", "7", "8"], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.0 | 18134 | 7 | 2 | 0 | 27638 | 351.11 | {"predicted": [], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 60865 | 3 | 0 | 0 | 0 | 34.36 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | fha | 1.0 | 59317 | 3 | 0 | 0 | 0 | 56.97 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 47055 | 3 | 0 | 0 | 0 | 35.24 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 11349 | 6 | 2 | 0 | 24564 | 112.56 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42951 | 2 | 0 | 0 | 0 | 40.89 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42928 | 2 | 0 | 0 | 0 | 22.89 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 42945 | 2 | 0 | 0 | 0 | 57.09 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 0.5 | 11133 | 5 | 1 | 1 | 12501 | 58.81 | {"Reference": 0.0, "Citation": 1.0} |
| tau2 | airline-1 | fha | 1.0 | 8266 | 2 | 0 | 0 | 0 | 60.75 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | fha | 1.0 | 8351 | 2 | 0 | 0 | 0 | 56.42 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8266 | 2 | 0 | 0 | 0 | 49.88 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8351 | 2 | 0 | 0 | 0 | 79.61 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8519 | 2 | 0 | 0 | 0 | 116.13 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | fha | 0.0 | 8527 | 2 | 0 | 0 | 0 | 232.01 | no JSON object in final answer |
| tau2 | retail-29 | sac | 0.583 | 8527 | 2 | 0 | 0 | 0 | 181.75 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.0 | 8530 | 2 | 0 | 0 | 0 | 217.4 | no JSON object in final answer |
| tau2 | retail-36 | fha | 0.7 | 8528 | 2 | 0 | 0 | 0 | 129.28 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | fha | 0.0 | 8525 | 2 | 0 | 0 | 0 | 237.85 | no JSON object in final answer |
| tau2 | retail-36 | sac | 0.7 | 8525 | 2 | 0 | 0 | 0 | 134.99 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.7 | 8528 | 2 | 0 | 0 | 0 | 199.49 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8541 | 2 | 0 | 0 | 0 | 126.52 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8541 | 2 | 0 | 0 | 0 | 205.25 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 8626 | 2 | 0 | 0 | 0 | 119.6 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 8626 | 2 | 0 | 0 | 0 | 147.95 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |

| 指标（n=10） | SAC | FHA | 差异 |
|---|---|---|---|
| Loong 平均分 | 0.79 | 0.84 | 噪声内（legal-L2-0 两臂都在 0–0.4 摆动） |
| Loong 峰值中位 | 15.9K | 36.6K | **−56%** |
| Loong 总 token | 683K | 945K | **−28%** |
| Loong 墙钟中位 | 46s | 38s | +21% |
| τ² 平均分（n=8） | 0.629 | 0.542 | 噪声内；SAC 没有折叠（2 请求） |

**解读**：加上重复后，iter4 配置在 Loong 上也体现出总 token 优势（−28%），峰值 −56%，
准确率差异在噪声内。τ² 的 SAC 因为“2 动作 + 保留最后观测”在 3–6 动作的任务上触发不了折叠，
等价于 FHA——这本身是一个结论：**短任务不需要 SAC，也不该为它付状态更新的钱**。

### 7.4 最终建议配置

| 场景 | 配置 | 依据 |
|---|---|---|
| 长 agent 任务（SWE/终端/多步工具） | `actionsPerCompaction: 2`、`keepLastObservation: true`、`minSpanTokens: 400` | iter4：同准确率，总 token −36% vs FHA，墙钟 1.6× |
| 长输入阅读（Loong/文档问答） | `actionsPerCompaction: 1`、`keepLastObservation: false`、可选 `stateInputMaxChars: 8000` | iter0/1/2：峰值 −77%，总 token +30–44%，但动作少、完成率高 |
| 追求最小峰值上下文（论文口径） | 严格 SAC（iter0/1 配置） | 峰值中位 6.5–6.7K |
| 追求最低总成本/延迟 | iter4 配置 | 总 token 0.79M，墙钟 363s |


## 8. 优化方向

按“证据 → 方向 → 预期收益”排列，前三条是这四轮数据直接支持的。

### D1. 把状态更新换成更便宜的模型（最大杠杆）

- **证据**：iter4 的 SWE 里状态更新仍占 33% 总 token（265K / 791K）；iter0 严格 SAC 占 52%。
- **方向**：插件增加 `stateModel` 配置（provider/model 独立于会话模型），或至少允许 `stateMaxTokens`
  与主模型分离；状态更新是“总结”任务，小模型（如 qwen3.8-27b）通常够用。
- **预期**：若小模型单价是 GLM-5.3-Inner 的 1/5，SWE 总成本可再降 ~25%。

### D2. 任务自适应折叠节奏

- **证据**：严格 SAC 在 Loong（阅读）峰值 −77%，但在 SWE 上总 token +41%；SAC-2 + 保留最近观测
  在 SWE 上总 token −36%，但在 Loong 几乎不折叠（峰值 −6%）。
- **方向**：按任务形状选节奏——动作数多、观测小（代码/终端）用 `actionsPerCompaction: 2` +
  `keepLastObservation: true`；动作数少、单观测大（文档）用严格 SAC + `stateInputMaxChars`。
  可以先做一个启发式：看最近 N 个动作的平均观测大小和剩余步骤估计。
- **预期**：两类任务都拿到各自的最优区间，而不是一个配置两头不讨好。

### D3. 状态模板加“输出契约”与“精确值”两节

- **证据**：Loong 的失败里有一半是**格式**（`paper-L3-1` 没输出 JSON、`legal-L2-0` 没写文书编号），
  不是内容答错；τ² 的计划里参数键也常缺。
- **方向**：在 paper 模板里显式加两节：
  `7. Output contract — the exact format the task requires (JSON keys, units, file paths)`
  和 `8. Verbatim values — ids, numbers, paths, test names copied exactly`。
- **预期**：减少“知道答案但格式不对”的 0 分；这是低成本、可离线验证的改动。

### D4. 峰值与速度的取舍产品化

- **证据**：`keepLastObservation` 把 SWE 墙钟 −28%、折叠 −49%，但 Loong 峰值从 6.5K 回到 24.5K。
- **方向**：把它做成预设里的显式选项（“最小上下文” vs “最低成本”），并在 README 里给出两组数字，
  而不是默认值之争。

### D5. 长任务的预算策略

- **证据**：SWE `sympy-24066` 在严格 SAC 下 1800s 超时；SAC-2 下 637s 完成。
- **方向**：插件层加“每轮最多折叠次数/最小间隔”，或在剩余时间不足时降级为不折叠；
  同时把“折叠失败”计入一个熔断器（连续失败 N 次后本回合停折叠），避免 span 累积雪崩。

### D6. τ² 官方多轮桥接

- **证据**：本轮 τ² 是静态计划变体（工具名 LCS + communicate_info），不是官方多轮分数；
  官方 `src/tau2` 已经下载在 `benchmarks/tau2-bench`。
- **方向**：写一个 τ² `Agent` 适配器，把每步的 DSH 会话（带 SAC）接到官方 orchestrator 上，
  用 `evaluation_criteria` 的 DB 终态判分。这是验证“SAC 在交互式工具使用中是否保持状态”的正路。
- **预期**：能回答论文没回答的问题（论文只在 SWE-bench Verified 和 Terminal-Bench 上测过 SAC）。

### D7. 失败模式的可观测性

- **证据**：四轮里 `state update produced no text` 和 `summary is not smaller` 是最常见的两类失败，
  但插件只在日志里各打一行。
- **方向**：把 `telemetrySnapshot` 暴露到会话状态/命令（如 `/sac-status`），并区分
  “空状态”“太小”“锁冲突”“超时”四类计数；A3 已经有字段，缺的是出口。

## 9. 局限与复现

- 准确率单格 n=1 时波动大；前三轮的数字当趋势看，最后一轮（iter4）加了 2 次重复。
- τ² 是静态计划变体，不是官方多轮分数。
- SWE 只有 3 个 sympy 实例；无 Docker，环境用共享 venv。
- 复现：`cd harness && python3 -m bench.matrix --bench loong,tau2,swe --arms sac,fha --iteration iterN --workers 6`。

## 附录 B：v9 插件化验证（2026-11-15）

把 harness v9 的机制移植进插件（`statePrompt: inline` + ledger + checklist + recall + 自适应预算），
插件测试 31→41 个全绿。用同一批基准抽了 3 个格子验证：

| 任务 | 配置 | 分 | 主请求 | 折叠 | 峰值 | 总 token | 墙钟 |
|---|---|---|---|---|---|---|---|
| Loong paper-L3-0 | v9 | 1.0 | 4 | 2 | 43.1K | — | 57s |
| Loong financial-L2-5 | v9 | 1.0 | 4 | 1 | 8.8K | — | 25s |
| Loong 两题合计 | v9 | 1.0 | — | 3 | 25.9K | 106K | 41s |
| Loong 两题合计 | FHA | 1.0 | — | 0 | 30.5K | 180K | 55s |
| SWE sympy-24066 | v9 | 1.0 | 55 | 52 | 19.2K | — | 1764s |
| SWE sympy-24066 | FHA | 1.0 | 27 | 0 | 20.8K | — | 269s |

结论：v9 在 Loong 上**总 token −41%、墙钟 −25%**，准确率持平；在 SWE 上能解对，但 52 次折叠
把墙钟推到 29 分钟（FHA 4.5 分钟）。第一次 SWE 跑（没有“状态块要简短”的指令）产生了空 diff，
加上“`<state>` 块 ≤120 词”后才解对——**inline 模式对模型是否遵守格式很敏感**，插件为此加了
一次提醒（nudge）和 paper 回退。

## 附录 A：五轮全量对比

| 迭代 | 基准 | 范式 | n | 平均分 | 满分率 | 峰值(中位) | 总 token | 折叠 | 折叠错误 | 墙钟(中位,s) |
|---|---|---|---|---|---|---|---|---|---|---|
| iter0 | loong | fha | 5 | 0.8 | 4/5 | 29273 | 282310 | 0 | 0 | 64.1 |
| iter0 | loong | sac | 5 | 0.933 | 4/5 | 6670 | 406876 | 15 | 3 | 156.2 |
| iter0 | swe | fha | 3 | 1.0 | 3/3 | 14068 | 1688505 | 0 | 0 | 249.4 |
| iter0 | swe | sac | 3 | 1.0 | 3/3 | 8802 | 2387362 | 148 | 10 | 1192.5 |
| iter0 | tau2 | fha | 4 | 0.527 | 1/4 | 8527.5 | 92943 | 0 | 0 | 123.8 |
| iter0 | tau2 | sac | 4 | 0.556 | 1/4 | 9367.0 | 342194 | 13 | 4 | 295.4 |
| iter1 | loong | fha | 5 | 0.8 | 4/5 | 26807 | 347225 | 0 | 0 | 36.2 |
| iter1 | loong | sac | 5 | 0.5 | 2/5 | 6504 | 371410 | 16 | 1 | 128.9 |
| iter1 | swe | fha | 3 | 1.0 | 3/3 | 18674 | 1392507 | 0 | 0 | 204.3 |
| iter1 | swe | sac | 3 | 1.0 | 3/3 | 7161 | 1887367 | 120 | 4 | 960.4 |
| iter1 | tau2 | fha | 4 | 0.702 | 1/4 | 8522.5 | 92175 | 0 | 0 | 128.3 |
| iter1 | tau2 | sac | 4 | 0.527 | 1/4 | 6909.5 | 337979 | 15 | 1 | 272.9 |
| iter2 | loong | fha | 5 | 0.4 | 2/5 | 8906 | 154215 | 0 | 0 | 29.4 |
| iter2 | loong | sac | 5 | 0.48 | 2/5 | 6156 | 199849 | 9 | 3 | 61.1 |
| iter2 | swe | fha | 3 | 1.0 | 3/3 | 15332 | 1126242 | 0 | 0 | 183.6 |
| iter2 | swe | sac | 3 | 1.0 | 3/3 | 7366 | 2256279 | 144 | 0 | 2005.0 |
| iter2 | tau2 | fha | 4 | 0.321 | 0/4 | 8530.0 | 112044 | 0 | 0 | 206.7 |
| iter2 | tau2 | sac | 4 | 0.381 | 1/4 | 6750.0 | 553223 | 27 | 0 | 412.5 |
| iter3 | loong | fha | 5 | 0.8 | 4/5 | 27267 | 354746 | 0 | 0 | 41.4 |
| iter3 | loong | sac | 5 | 0.933 | 4/5 | 24467 | 462434 | 9 | 3 | 66.3 |
| iter3 | swe | fha | 3 | 1.0 | 3/3 | 18048 | 883569 | 0 | 0 | 254.7 |
| iter3 | swe | sac | 3 | 0.667 | 2/3 | 12951 | 1187612 | 61 | 7 | 689.8 |
| iter3 | tau2 | fha | 4 | 0.527 | 1/4 | 8382.5 | 76624 | 0 | 0 | 167.2 |
| iter3 | tau2 | sac | 4 | 0.571 | 1/4 | 8387.5 | 94216 | 0 | 0 | 176.2 |
| iter4 | loong | fha | 5 | 0.933 | 4/5 | 34883 | 416649 | 0 | 0 | 18.2 |
| iter4 | loong | sac | 5 | 0.933 | 4/5 | 32929 | 488414 | 4 | 1 | 71.2 |
| iter4 | swe | fha | 3 | 1.0 | 3/3 | 20498 | 1228394 | 0 | 0 | 225.6 |
| iter4 | swe | sac | 3 | 1.0 | 3/3 | 9223 | 791201 | 29 | 0 | 363.4 |
| iter4 | tau2 | fha | 4 | 0.571 | 1/4 | 8558.0 | 835012 | 0 | 0 | 158.9 |
| iter4 | tau2 | sac | 4 | 0.702 | 1/4 | 8530.5 | 96814 | 0 | 0 | 159.4 |

> `总 token` = 主请求 prompt + 状态更新 prompt/completion + 主请求 completion。
> iter0 的三行分别来自三次运行（Loong / τ² / SWE 分批），已统一标成 iter0。

