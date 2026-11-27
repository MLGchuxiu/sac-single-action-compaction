# 给 agent 的说明（内网更新 SAC 插件到 v__VERSION__）

**怎么用**：把下面「=====」之间的全文作为一条消息，发给内网机器上的 DeepSeek Harness agent（任何预设的会话都可以，`standard` 即可）。
把第一行的 `<包目录>` 换成解压后的目录。agent 会自己调用包里的 `update-sac.cmd` / `diagnose-sac.cmd`，全程离线。
不想让 agent 做、自己动手的，看 `README.md`。

=====

你是这台内网机器上的运维执行者。任务：把 DeepSeek Harness 的 SAC 插件（单动作压缩）更新到 v__VERSION__。
部署包已解压到 `<包目录>`，里面有 `update-sac.cmd`、`update-sac.js`、`diagnose-sac.cmd`、`diagnose-sac.js`、`kit-manifest.json`、`plugins\`。
（macOS / Linux 上把 `update-sac.cmd` 换成 `./update-sac.sh`，`diagnose-sac.cmd` 换成 `./diagnose-sac.sh`。）

必须遵守的规则
1. 完全离线。不要用 npm / pnpm 安装任何东西；不要在「插件」面板里按包名安装（那会联网并失败）；不要联网下载。
2. 只使用包里自带的脚本。不要手工编辑 profile 的 package.json、pnpm-lock.yaml 或 cordis.patch.yml；不要手工创建 junction / symlink；不要修改 update-sac.js 和 diagnose-sac.js。
3. 每一步执行后，完整读出命令的输出和退出码，再决定下一步。退出码与下面写的不一致时立即停止，把输出原样报告给我，不要自行绕过。
4. 不要删除 `%USERPROFILE%\.dsh\sac-backups\` 下的任何备份；不要结束（kill）DeepSeek Harness 的进程——重启客户端由我手动做。
5. 除这些脚本外不要写入 `~/.dsh` 下的任何文件。

步骤
0. 定位与完整性：确认 `<包目录>` 存在并列出其中的文件。如果有 `kit-manifest.json`，用它核对 `update-sac.js` 和 `diagnose-sac.js` 的 SHA-256（manifest 里是 files 数组，每项有 path 和 sha256）。核对不上就停止并报告。
1. 只读检查：运行 `update-sac.cmd -check`（或 `node update-sac.js -check`）。
   期望退出码 10（需要更新）或 0（已是最新）。把以 STATUS 开头的行全部记下来，尤其是 dsh.version、registered、dsh-sac-compact.version、dsh-sac-preset.version、preset-patch、mount。退出码是 0 就跳到第 6 步只做复核。
2. 更新前诊断：运行 `diagnose-sac.cmd`（只读）。保存完整输出。看 G. client capabilities 一节：
   - 如果出现 "no agent-preset mechanism"，这台机器要用全局方式：后面第 4 步的命令加上 `-global`；
   - 如果出现 "does NOT expose compactRegion()"，这个客户端版本无法运行 SAC，停止并报告。
3. 运行中的客户端：更新会替换磁盘上的文件，正在运行的客户端要完全退出并重启后才会用上新版本。
   脚本检测到客户端在运行时只提醒、不阻止，并会要求确认。因为你就运行在这个客户端里，所以第 4 步用 `-yes` 继续即可，完成后按第 6 步提醒我重启。
4. 执行更新：运行 `update-sac.cmd -yes`（需要全局方式时：`update-sac.cmd -yes -global`）。
   期望退出码 0。读输出确认：出现 "all N files match the kit (SHA-256)" 和 "self-test: N test(s) passed"（N 应为 __TESTS__），最后一行给出备份目录路径。
   - 退出码 2：更新失败并且已经自动回滚，停止并报告全部输出；
   - 退出码 4：已装好但自检测试失败，停止并报告，不要重启客户端，等我决定是否 `-rollback`。
5. 更新后复核（只读）：
   - `update-sac.cmd -check`：期望退出码 0，输出含 "up to date"；
   - `diagnose-sac.cmd`：期望 VERDICT 一节没有任何 [FAIL]；C 节应有 "config = the policy this kit ships"；全局方式下 G 节的 "no agent-preset mechanism" 属于已知情况，可以忽略。
   两项不满足就停止并报告。
6. 交接给我：
   - 请我完全退出并重新启动 DeepSeek Harness（关闭窗口不算，托盘图标也要退出）；
   - 预设方式：请新建会话并选择预设「SAC 单动作压缩」；全局方式：任何新会话都会生效，不需要选预设；
   - 说明：v__VERSION__ 只有在请求变大后（固定提示 + 32K token）才折叠，短对话里不会出现 `dsh-sac-compact: folded` 日志，这是正常的，不是故障；
   - 告诉我撤销命令：`update-sac.cmd -rollback`。
7. 只有在我明确要求时才回滚：`update-sac.cmd -rollback`，期望退出码 0。

汇报格式：一张表（步骤 / 实际执行的命令 / 退出码 / 关键输出行），加上更新前后的插件版本号、使用的挂载方式（预设或全局）、备份目录的完整路径。任何一步失败，附上该命令的完整原始输出。

=====
