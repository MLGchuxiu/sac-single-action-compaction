# 测试集下载：τ²-bench / LOONG / SWE-bench Verified

> 2026-11-15 下载并校验，对应论文《You don't need long context》(arXiv:2611.05306) 的三个测试集。
> 下载时的网络环境：huggingface.co 不可达（走 hf-mirror.com 镜像），GitHub 直连不稳
> （tau2 源码经 jsdelivr CDN + GitHub API 补齐，全部文件做了 SHA-1 校验）。

| 目录 | 测试集 | 版本锚点 | 规模 |
|---|---|---|---|
| [`swe-bench-verified/`](swe-bench-verified/) | SWE-bench Verified | HF 数据集 revision `c104f840` | **500** 实例 |
| [`tau2-bench/`](tau2-bench/) | τ²-bench | GitHub tag **v1.0.1**（commit `fc0055d`） | **184** golden set |
| [`loong/`](loong/) | LOONG | GitHub commit `6d2115b` + 官方 doc.zip | **1,600** 题 |

三者合计约 155MB。全部完成条数级完整性校验（详见各节"校验"）。

---

## 1. SWE-bench Verified（500 实例）

来源：<https://hf-mirror.com/datasets/princeton-nlp/SWE-bench_Verified>（huggingface.co 的镜像）。
SWE-bench 论文：arXiv:2310.06770；Verified 子集由 OpenAI 人工校验（500/2294）。

### 文件

| 文件 | 说明 |
|---|---|
| `test-00000-of-00001.parquet` | HF 原始 parquet（2.1MB，保留作凭证） |
| `swe-bench-verified.jsonl` | 转换后的 JSONL（8.1MB，**给 harness 直接读**，500 行） |

字段：`instance_id`、`repo`、`base_commit`、`patch`（金标补丁）、`test_patch`（测试补丁）、
`problem_statement`（issue 原文）、`hints_text`、`version`、`FAIL_TO_PASS`、`PASS_TO_PASS`、
`environment_setup_commit`、`difficulty`（`<15 min` / `15 min - 1 h` / `>1 h`）。

### 校验

- 行数 = 500，全部 12 个仓库（django 231、sympy 75、sphinx 44、matplotlib 34、scikit-learn 32…）；
- 每条都有非空 `FAIL_TO_PASS`；
- parquet 与远端 `Content-Length` 逐字节一致（2,096,679 B）。

### 使用提示

- 官方评测（跑 FAIL_TO_PASS/PASS_TO_PASS 测试）需要 Docker + SWE-bench 评测 harness；
  对 SAC 插件测试，实例数据（problem_statement + 测试补丁 + 金标补丁）即任务输入。
- NVIDIA 论文 §5 的 SAC 实验就在这个集合 + Terminal-Bench 2.1 上做的
  （Opus 4.5：SAC 70.2% vs 基线 70.0%；GPT 5.6 Sol：82.4% vs 84.4%）。

---

## 2. τ²-bench（184 题 golden set）

来源：GitHub `sierra-research/tau2-bench`，**tag v1.0.1**（commit `fc0055d`，2026-07 发布，
含 75+ 任务修复）。论文：arXiv:2506.07982。

### 与 NVIDIA 论文的 184 对应关系

NVIDIA 论文用的是 v1.0.1 数据（其发布晚于 v1.0.1）上的 **184-task golden set**：

| 域 | 题池 | 题数 | 文件 |
|---|---|---|---|
| retail | base | **114** | `data/tau2/domains/retail/tasks.json` |
| airline | base | **50** | `data/tau2/domains/airline/tasks.json` |
| telecom | small | **20** | `data/tau2/domains/telecom/tasks_small.json` |
| 合计 | | **184** | |

注意：τ²-bench 原论文 Table 1 写的是 retail 115 / airline 50 / telecom 114（full 2285）——
v1.0.0 的"75+ 任务修复"移除了 1 道 retail 题（115→114）。telecom 的 `tasks.json`（2,285 题）
是生成器全集，各题池由 `split_tasks.json` 过滤（small 20 / train 74 / test 40 / base 114 / full 2285）；
**不要直接把 tasks.json 当测试集用**。

### 目录结构

```
tau2-bench/
├── data/tau2/domains/
│   ├── retail/    tasks.json(114) db.json policy.md split_tasks.json
│   ├── airline/   tasks.json(50)  db.json policy.md split_tasks.json
│   └── telecom/   tasks_small.json(20) tasks.json(2285) db.toml user_db.toml
│                  main_policy*.md tech_support_*.md workflows/
├── src/tau2/      官方运行器（环境/工具/用户模拟器/评测器，252 个文件）
├── docs/          官方文档（evaluation.md 讲判分规则）
└── README.md LICENSE CHANGELOG.md pyproject.toml
```

### 校验

- 340 个应有文件中需要的 **301 个全部到位，逐文件 git blob SHA-1 校验通过**（对照 GitHub
  tree API 清单），jsdelivr CDN 对 .md 的压缩已用 GitHub API 原件替换；
- 未包含（与文本基准无关，如需可从 tag v1.0.1 自取）：
  `tasks_voice.json` / `audio_difficulty.json`（语音基准）、`retail/task_issues/`
  （仅 `view_simulations.py` 调试用）、`telecom/tasks_full.json`（与 tasks.json 字节级重复）、
  `src/experiments/`、`data/tau2/results/`（604MB 基线运行日志）。

### 使用提示

- 官方跑法：`uv sync` 后 `tau2 run --domain airline --agent-llm <model> --user-llm <model> --num-trials 1`；
  判分 = 数据库终态 + 自然语言断言（LLM judge），指标 pass^k；
- 对 SAC 测试最有价值的是它的**双控制**设定：用户模拟器也有工具（telecom），对话中
  产生的事实必须保持到终态——正是语义状态模板的试金石；
- 自研 harness 接入时：每题 = `tasks.json` 里一条（`description` + `initial_state` +
  `evaluation_criteria` + `user_scenario`），工具实现是 `src/tau2/domains/<域>/tools.py`。

---

## 3. LOONG（1,600 题多文档 QA）

来源：GitHub `MozerWang/Loong`（commit `6d2115b`）+ 官方文档语料
`http://alibaba-research.oss-cn-beijing.aliyuncs.com/loong/doc.zip`（33,915,268 B）。
论文：arXiv:2406.17419（EMNLP 2024）。ModelScope 镜像：<https://modelscope.cn/datasets/iic/Loong>。

### 题目构成（`data/loong.jsonl`，1,600 行）

| 维度 | 分布 |
|---|---|
| 任务类型（`level`） | 1 定位 Spotlight Locating **250** / 2 比较 Comparison **300** / 3 聚类 Clustering **641** / 4 链式推理 Chain of Reasoning **409** |
| 长度档（`set`） | 1（10–50K）**323** / 2（50–100K）**564** / 3（100–200K）**481** / 4（>200K）**232** |
| 文档类型（`type`） | paper **400** / financial **700** / legal **500** |
| 语言（`language`） | zh **905** / en **695** |

四档长度分布 323/564/481/232 与 NVIDIA 论文 Figure 8 **逐位一致**——即论文用的同一版本。

### 文档语料（`data/doc/`）

| 类型 | 数量 | 形态 |
|---|---|---|
| paper | 764 个 .md | arXiv 论文全文（文件名 = arXiv id） |
| financial | 574 个 .txt | 2024 年 10-K 财报 |
| legal | 629 案例合并在 `legal/legal.json` | 中文判决文书 |

平均每题引用约 10.2 份文档（1,600 题 × 共 **16,280 个引用全部可解析，0 缺失**）。
注意三类文档的引用解析方式不同（见 `src/utils/prompt.py`）：paper 是直接文件名、
financial 按公司名 glob、legal 是 legal.json 的键。

### 使用提示

- 官方流水线：`src/step1_load_data.py`（拼 prompt）→ `step2_model_generate.py`（生成）→
  `step3_model_evaluate.py`（GPT-4 judge）→ `step4_cal_metric.py`（Avg Score + Perfect Rate）；
- 对 SAC 测试：这是 **LCW2 长输入**场景——文档当资源放文件系统、给模型选择性读取工具，
  正好测"每动作压缩下状态里保留了哪些证据"（论文 §4 的做法）；
- `prompt_template` 字段是官方拼上下文的模板（含 `{docs}` 占位符）。

---

## 下载方式备查（网络受限环境）

| 资源 | 用的通道 |
|---|---|
| SWE-bench Verified parquet | `hf-mirror.com`（HF 官方镜像，`/datasets/<id>/resolve/main/...`） |
| tau2 数据 + 源码 | git partial clone（`--filter=blob:none`）+ jsdelivr CDN（`cdn/fastly.jsdelivr.net/gh/<repo>@v1.0.1/<path>`）+ GitHub contents API（.md 原件） |
| Loong | git clone + 阿里云 OSS（国内直连，稳定） |
| pyarrow（parquet→JSONL 转换） | 系统 Python + 清华 PyPI 镜像（DSH 自带 Python 受 macOS 签名限制不能加载 pyarrow） |

---

## 补充下载（2026-11-26）

为按官方方式跑多轮仿真（[`docs/TEST-PLAN-TAU2-MULTITURN.md`](../docs/TEST-PLAN-TAU2-MULTITURN.md)）补齐了
第一批没有下载的文件。全部取自同一个 tag **v1.0.1**（commit `fc0055d`），经 GitHub Git Blobs API 取原始字节，
逐文件按 git blob SHA-1（`sha1("blob <len>\0" + 内容)`）校验：

| 文件 | blob SHA-1（前 12 位） | 字节 |
|---|---|---|
| `data/tau2/user_simulator/simulation_guidelines.md` | `b55a28a1d5ea` | 1,449 |
| `data/tau2/user_simulator/simulation_guidelines_tools.md` | `a7235b1ddf8d` | 2,747 |
| `data/tau2/user_simulator/simulation_guidelines_voice.md` | `0fddef3ec6e1` | 6,540 |
| `data/tau2/user_simulator/simulation_guidelines_voice_tools.md` | `344f00b6b368` | 7,912 |
| `examples/agents/README.md` | `ceb30282daa7` | 1,938 |
| `examples/agents/minimal_text_agent.py` | `620c510acec0` | 5,140 |
| `examples/agents/custom_agent_eval.py` | `e9618432ec3b` | 5,963 |
| `examples/agents/react_agent.py` | `52db1fed0bd2` | 7,818 |
| `.env.example` | `7443177316f3` | 1,067 |
| `.python-version` | `e4fba2183587` | 5 |
| `uv.lock` | `09f0b4a86b4a` | 466,363 |

其中真正影响官方运行的是 `data/tau2/user_simulator/simulation_guidelines.md` 和
`simulation_guidelines_tools.md`：LLM 用户模拟器的 system prompt 由它们拼成，第一批只下了静态适配器用得到的部分，
缺了它们官方的 `user_simulator` 无法运行。`examples/agents/` 是官方的自定义 agent 示例，`uv.lock` 是官方锁文件。

官方跑法：在 `benchmarks/tau2-bench/` 里 `uv sync`（或 `pip install -e .`，Python 3.12–3.13），
然后 `tau2 run --domain airline --agent llm_agent --agent-llm openai/gpt-4.1`。
本仓库的准备好的环境在 `work/tau2/venv`（`pip install -e benchmarks/tau2-bench`，与 `uv.lock` 解析出的版本可能略有出入）。
