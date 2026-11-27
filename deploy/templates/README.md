# SAC 单动作压缩插件 · 离线更新包 v__VERSION__

> **适用**：DeepSeek Harness 0.2.x（Windows / macOS），**完全离线**，不需要 npm / pnpm / python / 网络 / 管理员权限
> **版本**：`@local/dsh-sac-compact` __VERSION__ + `@local/dsh-sac-preset` __VERSION__（成本感知折叠，__TESTS__ 个测试全过）
> **可以重复执行**：已是最新时什么都不改；每次改动前先把现状备份，失败自动回滚

## 包内容

| 文件 | 说明 |
|---|---|
| `update-sac.cmd` + `update-sac.js` | **Windows 更新脚本**：双击或命令行。`.cmd` 只负责找 node.exe，`update-sac.js` 自带全部插件文件 |
| `update-sac.sh` | **macOS 更新脚本**（同样调用 `update-sac.js`） |
| `diagnose-sac.cmd` / `diagnose-sac.sh` + `diagnose-sac.js` | **只读诊断**：版本 → profile 注册 → 链接 → 插件文件 → **当前生效的配置** → 客户端能力 → 预设覆盖 |
| `AGENT.md` | **给 agent 的说明**：让内网机器上的 DSH agent 代为更新时，把其中的提示词整段粘给它 |
| `kit-manifest.json` | 包内每个文件的 SHA-256 |
| `plugins/dsh-sac-compact/`、`plugins/dsh-sac-preset/` | 插件源文件（查阅 / 手动部署用；脚本内嵌的就是这些文件） |

## 三步更新（Windows）

1. 解压到一个固定目录（例如 `D:\tools\sac-deploy-kit-v__VERSION__\`）；
2. **完全退出 DeepSeek Harness**（托盘图标也要退出），然后双击 `update-sac.cmd`；
3. 按提示按 `y` 确认 → 结束后按回车关窗 → 重新启动客户端，**新建会话**并选预设 **「SAC 单动作压缩」**。

不想退出客户端也可以更新（文件已被运行中的客户端读入内存，重启后才切换到新版本），脚本会给出提醒。

命令行用法（macOS 把 `update-sac.cmd` 换成 `./update-sac.sh`）：

| 命令 | 作用 | 退出码 |
|---|---|---|
| `update-sac.cmd -check` | **只看不改**：现在装的是什么、哪些会变 | 0 已是最新 · 10 需要更新 |
| `update-sac.cmd` | 安装 / 更新（会问一次 `y/N`） | 0 完成 |
| `update-sac.cmd -yes` | 同上，不提问（agent / 脚本用） | 0 完成 |
| `update-sac.cmd -rollback` | **撤销上一次更新** | 0 完成 · 3 没有备份 |
| `update-sac.cmd -uninstall [-purge]` | 卸载（`-purge` 连插件文件一起删） | 0 完成 |
| `update-sac.cmd -profile headless` | 指定 profile（默认 `desktop`） | |
| `update-sac.cmd -help` | 全部选项 | |

其它退出码：`1` 参数错误 · `2` 中止（前置条件不满足 / 校验失败，**此时已自动回滚**）· `4` 已装好但自检测试失败（先别用，跑诊断）。

## 更新脚本做了什么

1. **检查现状**：profile 里注册的是哪个 bundle、`node_modules\@local\...` 链接指向哪里、文件与包内版本差多少；
2. **备份**：把现有插件文件、profile 的 `package.json` / `pnpm-lock.yaml` / `cordis.patch.yml` 复制到 `<用户目录>\.dsh\sac-backups\<时间>-update-from-<旧版本>\`，并写入 `state.json`（注册方式、链接目标、每个文件的哈希）；
3. **写入新文件**：逐个文件写入并按 SHA-256 校验；旧版本有而新版本没有的 `.js/.json/.yml/.md` 会被清掉；
4. **生成预设**：从**这台机器客户端自带的 standard 预设**现场生成 SAC 预设（预设必须完整枚举客户端的插件，写死一份换个版本就加载失败），把 `sac-compact` 插到隔离的 compaction 组里；
5. **注册 + 链接 + lockfile**：与旧部署包相同的做法（`link:` 依赖、NTFS junction，不需要管理员）；
6. **校验**：文件哈希、版本号、注册方式、链接、**当前生效的配置必须等于出厂策略**；最后用同一个 node 跑一遍插件自带的测试。

任何一步失败都会用第 2 步的备份**自动还原**。

## 这个版本改了什么（1.1.0 → __VERSION__）

- **1.2.0 多轮修复**：每条用户指令都钉在上下文里，只折叠指令之间的工作（旧版会把后续指令摘要掉，代理「换了目标还围着旧目标转」）。
- **1.3.0 成本感知折叠**：整个请求超过「固定提示 + 32K」才折叠，折到「固定提示 + 16K」。**短对话一次都不折**（和不压缩时是同一组请求）；已结束轮次的动作由 harness 写成精确摘要行，不调模型。
- **预设终于带上新配置**：旧部署包装的预设里只有 1.1.0 的配置键，所以 GUI 里选 SAC 预设的会话一直按「每 2 个动作折叠一次」跑。本包的预设配置与插件出厂配置逐键相同，并由测试保证。

## 两种挂载方式

| 方式 | 命令 | 效果 |
|---|---|---|
| **预设**（默认，客户端支持时） | `update-sac.cmd` | 新建会话时选「SAC 单动作压缩」预设，只有这些会话启用；其它会话不受影响 |
| **全局**（客户端没有预设机制时，例如 0.2.2-rc.1） | `update-sac.cmd -global` | 该 profile 下**所有**会话都启用 SAC，不需要选预设 |

脚本默认**沿用这台机器现在的方式**；首次安装时自动判断客户端是否支持预设，并在输出里说明选了哪种和原因。不确定就先跑 `diagnose-sac.cmd` 看 **G. client capabilities**。

## 回滚

```bat
update-sac.cmd -rollback
```

把最近一次备份（插件文件、注册、链接、lockfile）原样放回；回滚前的状态会另存到 `.dsh\sac-backups-undo\`，所以回滚本身也可以再撤销（命令会打印出来）。回滚后同样要重启客户端并新建会话。

## 遇到问题：先跑诊断

双击 `diagnose-sac.cmd`（macOS：`./diagnose-sac.sh`），它**不改任何东西**，把窗口全文复制回来即可定位。重点看：

- **C 节** `config = the policy this kit ships` —— 当前生效的配置对不对；
- **G 节** `agent presets are supported` —— 有预设机制吗；`exposes compactRegion()` —— SAC 依赖的压缩 API 在吗；
- **H 节** —— 预设列的插件这台客户端都有吗（客户端升级后重跑一次更新即可重新生成）；
- **VERDICT** —— 没有 `[FAIL]` 就是好的。

| 现象 | 处理 |
|---|---|
| `update-sac.cmd` 提示找不到 node.exe | 按提示把 `...\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\node\bin\node.exe` 拖进窗口 |
| 预设列表里没有「SAC 单动作压缩」 | 完全退出并重启客户端；预设只能在**新建会话**时选；跑诊断看 G 节，没有预设机制就 `-global` |
| `app.asar was not found` | 客户端装在非常规位置：`update-sac.cmd -app-asar "C:\...\resources\app.asar"`（右键快捷方式 → 打开文件所在位置 → 同级 `resources`） |
| 退出码 4（自检失败） | 插件已装好但测试没过：跑 `diagnose-sac.cmd`，把输出发回来；要退回就 `-rollback` |
| 会话里一直没有 `dsh-sac-compact: folded` | 1.3.0 按请求大小折叠，短对话不折是正常的。想看折叠，把 `foldMarginTokens` 调小，或跑一个长任务 |

## 手动更新（脚本完全不可用时）

1. 把 `plugins\` 下两个目录拷到固定位置并**保持同级**（如 `D:\dsh-plugins\`），覆盖旧文件前先备份旧目录；
2. 确认 `%USERPROFILE%\.dsh\profiles\desktop\package.json` 的 `dsh.profile.bundles` 里有 `@local/dsh-sac-preset`、`dependencies` 里有 `"@local/dsh-sac-preset": "link:D:/dsh-plugins/dsh-sac-preset"`；
3. 确认 `%USERPROFILE%\.dsh\profiles\desktop\node_modules\@local\dsh-sac-preset` 指向 `D:\dsh-plugins\dsh-sac-preset`（`mklink /J`，免管理员）；
4. 重启客户端并新建会话。

---

*v__VERSION__ · 打包自开发机的实装版本 · `kit-manifest.json` 可校验包内文件*
