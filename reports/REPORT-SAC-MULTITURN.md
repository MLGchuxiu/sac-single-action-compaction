# SAC 多轮会话取证与修复：用户换了目标，代理为什么不跟着换

> 日期：2026-11-25 · 对象：开发机上导出的两个 DSH 会话日志（下称 e64b、d2ac；原始日志未包含在本发布仓库中）
> 模型：一个内部部署的 128K 窗口模型 · 预设：`sac`（`statePrompt: paper`、`actionsPerCompaction: 2`、`keepLastObservation: true`、`stateInputLean: true`）
> 结论适用范围：插件 `@local/dsh-sac-compact` 1.1.0 及之前；修复版本 1.2.0
> 公开版说明：两个会话里的真实任务内容（用户原话、客户端与模型的名称、涉及凭据的细节）已改写为中性描述；折叠次数、可见率等数字与机制分析未作改动。

## 0. 结论

1. **现象属实，根因在 SAC 插件，不在模型或 DSH。** 插件只把**第一条**用户消息当作任务锚点；用户之后每发一条消息都被当成普通内容，
   在 2 个动作之后被摘要替换掉。两个会话里，后续指令分别只被 **2 / 429、2 / 294、2 / 222、2 / 39** 次模型回复看到原文
   （e64b），真正的任务 `<goal_round>` 只被 **3 / 274** 次看到（d2ac）。
2. **状态更新根本看不到用户指令。** lean 模式下状态更新的输入只有「上一份状态 + 最近两步动作」——359 次状态更新里只有 8 次的输入含用户原话
   （恰好就是把它们折叠掉的那 8 次）。于是 recap 的 Objective 跟着最近两步漂移：用户后来说「把这个外部模型加成 provider」，两次折叠之后
   Objective 变回了第一轮的调研任务，会话结束时 provider 也没有加上。
3. **d2ac 还叠加了锚点选错。** 被钉住的是一条「审批策略已修改」的系统通知；真正的任务在第 1 次折叠就被吃掉。
4. **修复（插件 1.2.0）：每条用户指令都钉在上下文里，只折叠指令之间的工作。** 状态更新以所属指令的原文为锚；更早的指令并入归档并逐字引用；
   单任务会话的折叠范围与之前逐 span 相同。
5. **验证。** 78 个测试通过；基线插件离线复现日志中 360/360 次折叠，修复后恰好只在 8 处不同（全是范围里含用户请求的折叠）；
   两个日志的反事实仿真里「折叠范围含用户请求」5 → 0；在真实 DSH 运行时跑了 3 轮冒烟（GLM-5.3-Inner），
   同一份日志用旧策略回放会在第 2 轮第 2 步吃掉新请求，新策略不会。

## 1. 用户看到的现象（e64b）

（任务内容已改写为中性描述：第 1 轮是调研某个外部服务的接入方式，之后几轮换了目标。）

| 轮 | 用户说 | 模型第一句 | 实际发生 | 这一轮的最终回复 |
|---|---|---|---|---|
| 1 | 调研某个外部服务的接入方式（第一轮任务） | — | 28 步，正常 | 给出分析 ✅ |
| 2 | ①列出可选项 ②实测能否连通并返回结果 | 「让我先查看配置…然后测试连接」 | 135 步；第 3 步就拿到了 401，之后一直在逆向它的鉴权 | 只报告「找到了连接凭据」——既没有可选项列表，也没有连通性结论 ❌ |
| 3 | 你测试了连通性吗？请告诉我返回结果 | 「好，我来测试连接性…」 | 72 步，继续逆向鉴权，被用户打断 | 没有回答 ❌ |
| 4 | 请你继续 | 「继续搜索配置信息」 | 183 步 | 报告「凭据有效」，并给了一个用户从没要求过的 JSON ❌ |
| 5 | 能不能把它添加到本 DSH 客户端的 provider 里 | 「好的，我来研究一下 DSH 的 provider 机制」 | 37 步；第 3 次折叠后目标漂回第一轮的调研任务 | 「找到了！它就在某个配置文件中」——**provider 始终没有加上** ❌ |

每一轮的第一句都是对的——模型看得到刚发来的消息。问题出在两句之后：消息被折叠掉，剩下的只有一份漂移的摘要。

## 2. 两个日志的事实

| | e64b | d2ac |
|---|---|---|
| 预设 | 创建时 `standard`，第 4 号事件切到 `sac` | 创建即 `sac` |
| 用户输入 | 5 条普通消息（第 1 条任务 + 4 条后续） | `/goal` 创建的 1 个目标；每个 round 重新下发一次 `<goal_round>`（共 5 次，文本只差 `Round: n/256`） |
| 轮数 / 模型回复 | 7 / 457 | 5 / 274 |
| 折叠次数 | 225 | 134 |
| 折叠范围里含用户指令的折叠 | **4**（seq 388、1869、2631、4585） | **4**（seq 49 吃掉了 `<goal_round>`；831、1150、2766 吃掉后续 round 的重发） |
| 每条指令被多少次模型回复看到原文 | 457/457、**2/429、2/294、2/222、2/39** | **3/274**（之后每轮重发各 1–3 次） |
| recap 里凭空出现 JSON「输出契约」 | 27 / 225 | 19 / 134 |
| 摘要长度 | 平均 2704 字符（上限 400 词） | 平均 2670 字符 |

## 3. 根因

### 3.1 直接原因：后续用户消息被当成普通内容折叠

`lib/sac-policy.js`（1.1.0）：

```js
export function findTaskAnchorIndex(nodes) {
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index]
    if (node.type === 'user/message' && node.isCheckpoint !== true) return index   // ← 第一条
  }
  return -1
}
…
const startIndex = anchor + 1          // 锚点之后的一切都可折叠，包括用户后面发的话
```

论文的设定是「一个会话一个任务」，`task + state` 里的 task 只有一条。真实会话里用户会不断说话，而插件没有任何「轮」的概念：
新消息落在可折叠区间里，凑够 2 个动作就随区间一起被摘要替换。

> 这和开发机上一份未发布的第三轮报告里的机理 M1 是同一件事：那份报告当时写道「人类的『我再确认一下』在 SAC 里
> 不是工作记忆，而是一句会被下一次摘要重写、淡化、甚至删除的文本」，并把它归为 SAC 的结构性后果。1.2.0 起用户自己打的字不再被摘要。

e64b 第 5 轮的时间线（`replace a–b` 是日志里真实发生的折叠）：

| pre-step 发生在 | 用户刚说 | 折叠范围 | 结果 |
|---|---|---|---|
| turn 5 step 1、step 2 | （seq 4569）把这个模型加成 provider | — | 模型正常接话（看到原文的正是这两步） |
| turn 5 step 3 之前 | | `replace 4555–4574` | **4569 被吃掉**；此时 recap 的 Objective 还是对的 |
| 之后每 2 步 | | `replace 4585–4599`、`replace 4610–4620` … | 第 3 次折叠（seq 4630）起 Objective 变成第一轮的任务（「找到该客户端连接模型后端所用的凭据…」），一直到会话结束 |

### 3.2 帮凶 A：状态更新看不到任何用户指令，Objective 跟着最近两步漂移

`stateInputLean: true` 把 system prompt 和工具 schema 从状态更新请求里去掉，而 DSH 的 `compaction-basic` 传给 `summarize()` 的
`input.messages` 本来就只有「被折叠的那一段」。所以 351/359 次状态更新的输入是：`[上一份 recap, 最近两步的动作和输出] + 模板`。
模板要求写「1. Objective - one line describing the required result」，模型只能从最近两步里猜「required result」。
只要上一份 recap 里的 Objective 不够醒目、或最近两步在做一件很具体的子事，Objective 就被改写：

| e64b | 用户当时的请求 | 这一次 recap 的 Objective |
|---|---|---|
| #14（seq 387） | （刚吃掉第 2 轮请求） | 测试连通性…列出可选项并拿到返回 ✅ |
| #20（seq 528） | ①列出可选项 ②测连接性 | 弄清正确的鉴权方式（令牌格式）…从客户端打包的源码里找 |
| #80（seq 1837，第 2 轮结束） | 同上 | 从客户端安装目录里提取连接凭据 ← 第 1 轮任务的形状 |
| #81（seq 1868） | （刚吃掉第 3 轮请求） | 测试连通性…拿到返回 ✅ |
| #85（seq 1950） | 你测试了连通性吗？告诉我返回结果 | 通过从打包文件里抽取源码来确定正确的鉴权机制 |
| #208（seq 4584） | （刚吃掉第 5 轮请求） | 把这个模型加成 DSH 的 provider ✅ |
| #210（seq 4630） | 把这个模型加成 provider | 找到本地客户端连接模型后端所用的凭据 ❌ |
| #212–#225（直到结束） | 同上 | 找到该凭据所在的文件路径和内容… ❌（共 14 次） |

d2ac 同样：第 5–13 次折叠的 Objective 是某个具体子任务的实现方式（把一个组件改写成独立服务），
第 88 次起是「产出一个包含两个字段的 JSON」（一个用户从未要求的输出契约变成了目标），
第 118 次、第 128 次分别是「修某个文件里的正则错误」「让服务常驻」这类当时手上的子步骤。
每轮 `<goal_round>` 重新下发时 Objective 会回到正题一次（#35、#48、#124），一两步之后再次漂走。

### 3.3 帮凶 B：锚点选错（d2ac）

`findTaskAnchorIndex` 取「第一条非 checkpoint 的 user/message」，不管它是谁发的。d2ac 里用户先改了权限模式，DSH 注入了
一条 `source.kind = user-approval` 的通知「The user changed the approval policy…」——它成了锚点；真正的任务
`<goal_round>`（`source.kind = goal`）排在它后面，第 1 次折叠（`replace 19–34`）就被吃掉。
e64b 没有这条通知，所以第一条任务恰好是锚点，也恰好被永久钉住——这正是「代理总是回到第 1 个目标」的另一半原因：
**上下文里唯一永远原样保留的用户话，就是第 1 条。**

### 3.4 帮凶 C：输出契约被凭空发明

模板第 7 节要求写「the exact response format the task requires … Restate it so the next instance cannot get it wrong」。
两个会话里用户都没有要求过任何格式，模型仍然在 46 份 recap 里写出 JSON 契约（例如一个带两个字段名的结构），
之后每一份 recap 都忠实地继承它，第 4 轮的最终回复也真的给了一段 JSON。

### 3.5 为什么模型至少还能看到一两次

每个 turn 的第一个 pre-step 发生在 `step/start`，而用户消息是在它之后才进 surface 的（日志顺序 `turn/start → step/start → user/message →
assistant/message`）。所以新消息总能被当轮第一步看到；之后的 pre-step 一旦凑够 2 个动作就把它折叠。
如果用户在代理运行中途插话（steering 消息在下一步开头进 surface），旧策略在下一个 pre-step 就可能把它吃掉——只被看到 1 次。

## 4. 修复（插件 1.2.0）

改动在 `plugins/dsh-sac-compact/`（`lib/sac-policy.js`、`index.js`、`cordis.patch.yml`）和 `plugins/dsh-sac-preset/cordis.patch.yml`。

**策略层（`sac-policy.js`）**

- 每条**用户指令**都是锚点：`source.kind` 为 `user`，或 goal 插件的 `goal`。`runtime-context`、`skill-catalog`、`time-context`、
  `tool-jobs`、`user-approval`、`working-directory` 这些系统通知不是锚点，照旧可折叠（也修掉了 3.3）。
- surface 被切成「指令之间的工作段」：最新的叫 active（正在做的工作），之前的叫 frozen（已结束的回合），最老的指令之前叫 archive。
  `planSacSpan()` 返回 `{kind: active|frozen|archive, anchorIndex, released, …}`，**折叠范围永远不含指令**。
- 优先级：active 先；frozen / archive 只在 active 没东西可折的步骤里做，并且要等模型对新请求至少做过 1 个动作——
  **回答用户的第一步永远不被一次折叠拖慢**。硬预算（`contextBudgetTokens`）触发时不受此限。
- frozen 段：每有 1 个遗留动作就折一次（active 仍按 `actionsPerCompaction` 攒批），保留该回合的最终回复；`keepLastObservation`
  只对 active 生效（已结束的回合没有「下一步」可留）。只有 checkpoint 的段永不重折。
- archive：超过 `maxPinnedInstructions`（默认 4）的最老指令连同它的状态并入一段归档；归档必须真的释放一条指令、并且要大到
  折完一定更小（`archiveMinTokens`，默认按 `stateMaxWords` / `instructionChars` 推导）才会折。
- `<goal_round>` 的重发不算新锚点：去掉 `Round: n/m` 这一行后文本相同的，是同一个目标；改过目标文本的才算新指令。
- `pinInstructions: false` 完整退回 1.1.0 的行为。

**状态更新（`index.js`）**

- pre-step 把这次折叠的 `{kind, anchor, released}` 记在 `state.pendingFold`；`summarize()` 把 anchor 指令**原文**放在被折叠片段之前，
  模板改为「The first user message above is the request this work belongs to … 1. Objective - … never from whatever the last actions
  happened to be doing」。frozen 段的折叠以它自己那条指令为锚，而不是最新那条。
- 归档折叠用单独的 `ARCHIVE_STATE_PROMPT`（记录每个旧请求的结果和仍然有用的事实，不提下一步），并由插件自己——不靠模型——
  在 checkpoint 里逐字写出被释放的指令（`[SAC EARLIER USER REQUESTS]`，最新在前，`instructionChars` 上限）。应用重启后这份列表
  从 surface 上的 checkpoint 读回。
- 输出契约：用户没要求格式时写 `(none)`，不要发明。
- 后端自己发起的折叠（压力恢复、`/compact`）没有计划：以范围内最新的指令为锚，范围内的每条指令都逐字带进新 checkpoint。
- 被后端判为「not smaller」的 frozen / archive 折叠每段最多试 2 次，不再每步重试一次白付一次模型调用。
- 遥测新增 `frozenFolds`、`archiveFolds`、`carriedRequests`、`refusedFolds`；日志行新增 `segment=… request=… released=…`。

## 5. 验证

### 5.1 回放保真与逐点差异（不调用模型）

`harness/session_replay/replay.mjs` 在日志里每一个 `compaction/start` 处，用插件真实的 `planSacSpan()` 重算折叠范围：

| 日志 | 基线插件（`e01ac12`） | 修复后 |
|---|---|---|
| e64b（226 次折叠） | 复现 226 / 226 | 222 处相同，**4 处不同**：正是范围里含用户请求的 4 次 |
| d2ac（134 次折叠） | 复现 134 / 134 | 130 处相同，**4 处不同**：含 `<goal_round>` 的 4 次 |

例如 e64b seq 386：日志 `replace 351–373`（含用户消息 364）→ 现在计划 `replace 351–355`；d2ac seq 47：日志 `replace 19–34`
（含任务本身）→ 现在计划 `replace 20–34`。

### 5.2 反事实仿真（不调用模型）

`simulate.mjs` 忽略日志里的折叠，在每次 pre-step 用被测策略重新决定；checkpoint 大小取日志里同一 step 的真实摘要长度。
模型的动作按日志原样回放，所以它衡量的是「模型会看到什么」。

| | 旧策略 | 新策略（默认 4 条钉住） |
|---|---|---|
| **e64b** 折叠次数 | 225 | 226（224 active + 2 frozen） |
| 折叠范围含用户请求 | **4** | **0** |
| 第 2/3/4/5 轮请求被看到原文的回复数 | 2/429、2/294、2/222、2/39 | **429/429、294/294、222/222、39/39** |
| surface token：均值 / 峰值 / 末尾 | 6037 / 46041 / 4649 | 9027 / 49779 / 8486 |
| **d2ac** 折叠次数 | 134 | 134 |
| 折叠范围含目标文本 | **1** | **0** |
| `<goal_round>` 被看到原文的回复数 | 3/274 | **274/274** |
| surface token：均值 / 峰值 / 末尾 | 5921 / 27060 / 4053 | 6248 / 27392 / 4386 |

e64b 用 `maxPinnedInstructions: 1` 再跑一遍可以走到归档路径：228 次折叠（224 active + 4 archive），4 次归档各释放一条请求并逐字引用，
五条请求在它们之后的**全部** 1441 次回复里都在上下文中（原样或被引用）。

### 5.3 单元与集成测试

`node --test test/*.test.js` → **78 / 78**（原 48 个全部保留并通过；新增 30 个）。其中一条把 0–5 个动作 × 有无结尾回复的单任务 surface
逐个比对 `pinInstructions` 开 / 关两种策略的计划：`deepEqual`。

### 5.4 真实运行时冒烟（调用模型）

`harness/session_replay/smoke_multiturn.sh`：用工作区本地的 `sac-default` profile（bundle 链接到工作区里的插件）在隔离的 DSH home
和一个三文件的小工作区里跑同一个会话的 3 轮，每轮一个新请求（读三个文件 → 新建 `summary.txt` → 改成另一行）。GLM-5.3-Inner，约 15 万 token。

- 三轮都按当轮请求完成，最终回复正确。
- 4 次折叠的范围分别是 `10–23`、`32–35`、`50–63`、`72–75`，**没有一次包含用户消息**；第 2、3 轮开始后各有一次 frozen 折叠，
  发生在模型对新请求做完第一个动作之后。
- 之后的 surface 正是设计里的形状：`system · 请求1 · 状态1 · 回复1 · 请求2 · 状态2 · 回复2 · 请求3 · 工作区`。
- 第 3 轮里对第 2 轮遗留动作的那次折叠，Objective 仍是请求 2（「在当前目录新建 summary.txt…」），没有被最新请求带偏；
  输出契约一栏是 `(none) — 用户未指定格式`。
- 同一份日志交给旧策略仿真：第 2 轮请求会在 seq 65 被折叠，只被 2/7 次回复看到——在一份全新的真实会话里复现了这个缺陷。

### 5.5 没能验证到的部分

- **日志所属机器上的那个模型在新模板下的表现没有直接验证**，这个模型这里调不到。另做了一个合成数据的 A/B
  （`ab_objective.py`，未包含在发布仓库中：三条虚构请求各 4 次折叠的链，新旧模板各 24 次，`deepseek-v4.1-flash`，85 次调用、约 19 万 token，其中第一轮 36 次
  因为解析脚本有缺陷作废）。结果要如实说：在这个模型上**新旧模板的 Objective 都没有丢**——它不复现日志里那种漂移，
  所以这个 A/B 既不能证明、也不能否定模板改动对 Objective 的作用。模板改动的依据是第 3.2 节那 359 份真实 recap。能看出的差别是：
  输出契约一栏，新模板 24 条里 20 条以字面的 `(none)` 开头，其余 4 条（同一条链）写的是「用中文回答…，没有 JSON 结构」；
  旧模板 24 条里 10 条以 none 开头，其余各写各的说明——两者都没有发明 JSON 结构，也就是说这个模型本来就不犯 3.4 节的毛病。
  另外新模板下 6 条链里有 1 条用中文写 Objective（请求原文是中文，被放在了输入最前面）。
- 归档路径只在仿真和单元测试里走过，真实运行时的冒烟只有 3 条请求，没有触发归档。
- 多轮会话的上下文成本只在这两份日志上量过（见下）。

## 6. 成本与取舍

- 单任务会话（基准测试那种）：一条指令、一份滚动状态，折叠范围逐 span 不变，成本不变。
- 多轮会话：每个被钉住的回合多占「它的状态 + 它的最终回复」，约 1.5K token。e64b 上均值 6.0K → 9.0K token；d2ac 只多了一条
  被钉住的目标文本（+0.3K）。峰值由单条超大工具输出决定，与策略无关。
- 想更省：`maxPinnedInstructions: 1`（手上这条指令 + 归档，归档里逐字保留更早的请求）；想多留几轮原文就调大。
- 钉住的指令和 frozen 段的状态不能被压缩；硬预算触发时 frozen 段连最终回复一起折，但永远不折指令。

## 7. 部署与注意事项

1. 修复在仓库的 `plugins/` 里。两个会话跑在另一台机器部署的副本上（日志里能看到 `sac-deploy-kit-v1.1.0`），**那份副本没有变**；
   需要用新的 `plugins/dsh-sac-compact/`（`index.js`、`lib/sac-policy.js`、`cordis.patch.yml`、`package.json`）和
   `plugins/dsh-sac-preset/cordis.patch.yml`、`package.json` 重新打包部署。
2. 预设组合在会话创建时固化：已有会话仍是旧行为，要**新建会话**。
3. 想确认修复生效：DSH 日志里的折叠行（`dsh-sac-compact: folded …`）会带 `segment=active|frozen|archive request=<seq>`；
   更可靠的办法是把导出的会话日志交给 `node harness/session_replay/replay.mjs <log> new`，每条用户消息都应当是
   「folded by compaction -」（从未被折叠）。

## 8. 复现

见 [`harness/session_replay/README.md`](../harness/session_replay/README.md)。日志里的密钥、内网地址等没有抄进本报告，摘录里用
`…` 或 `<…>` 代替。

## 附：改动文件

| 文件 | 内容 |
|---|---|
| `plugins/dsh-sac-compact/lib/sac-policy.js` | 指令判定与去重、分段规划（active / frozen / archive）、归档收缩阈值、5 个新配置键 |
| `plugins/dsh-sac-compact/index.js` | `describeSurface` 带 `sourceKind` / 指令文本；`describeFold`、`resolveFold`；带锚点的状态更新；归档模板；逐字请求列表与重启恢复；重试熔断；遥测 |
| `plugins/dsh-sac-compact/test/multiturn-policy.test.js`、`multiturn-integration.test.js` | 30 个新测试 |
| `plugins/dsh-sac-compact/README.md`、`cordis.patch.yml`、`package.json`（1.2.0） | 文档与显式配置 |
| `plugins/dsh-sac-preset/README.md`、`cordis.patch.yml`、`package.json`（1.2.0） | 同上 |
| `harness/session_replay/` | `saclog.py`、`replay.mjs`、`simulate.mjs`、`compare.mjs`、`objectives.py`、`smoke_multiturn.sh`（`ab_objective.py` 未包含在发布仓库中） |
| `docs/VERSIONS.md`、`README.md` | 版本表与入口 |
