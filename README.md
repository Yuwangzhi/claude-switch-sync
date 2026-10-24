# Claude Switch Sync

在 macOS 的 CC Switch 中切换 Claude 官方订阅与第三方供应商时，自动合并 Claude Desktop 两个配置目录里的 Code 会话索引，让切换后的 App 继续显示原有聊天。

由 macOS LaunchAgent 在登录后运行，通过文件事件监听切换。无需 AI Agent，也不每隔 120 秒复制历史。

## 切换时会发生什么

| 操作 | 本工具的动作 |
| --- | --- |
| Desktop：官方订阅 → 第三方 | 正常退出原本运行的 App，备份并合并会话索引，再重新打开 |
| Desktop：第三方 → 官方订阅 | 同样执行一次退出、备份、合并和重新打开 |
| Desktop：第三方 A → 第三方 B | 不迁移历史，不主动重启 |
| Desktop：直连、路由、聚合之间切换，仍为第三方 | 不迁移历史；是否需要重启由 App 自己提示 |
| CLI：官方订阅与第三方切换 | 检查仍使用同一个 `~/.claude/projects`，不复制正文，不关闭终端 |
| App 原本没有运行 | 只合并索引，不自动启动 App |
| 没有发生切换 | 不执行历史迁移 |

**切换前先结束正在生成的回复。** 正常退出 App 仍会结束其中运行的任务。已经运行的 CLI 不一定重新加载供应商配置，按 Claude Code 的方式重新打开或恢复会话。

正常情况下，跨官方/第三方切换会自动重启原本运行的 App，无需手动重启。CC Switch 的 Claude Code 与 Claude Desktop 是两个独立选择，本工具不会把其中一个选择强行应用到另一个。

## 同步哪些数据

- App 会话索引：`~/Library/Application Support/Claude/claude-code-sessions/…` 与 `Claude-3p/claude-code-sessions/…`。
- 会话归档状态。
- 已知删除标记用于防止旧记录被重新带回；不自动删除记录，也不清理重复会话。

聊天正文仍在 `~/.claude/projects`。本工具不拼接或改写正文，不创建符号链接，不复制登录状态、密钥、供应商设置、浏览器数据或定时任务。CLI 与 App 各自维护会话列表；本工具解决的是两个 App 配置目录之间的列表衔接，不把所有 CLI 会话自动导入 App。参见 [Claude 官方会话说明](https://code.claude.com/docs/en/sessions)。

## 如何处理索引变化和异常

同一会话的索引取最后活动时间较新的版本；时间一致时使用刚退出的一边。续聊后 App 可能更换内部 `cliSessionId`：只有较新记录的 `priorCliSessionIds` 明确包含旧 ID，且会话 ID、创建时间和项目目录一致，才按同一条聊天处理。

如果两条记录的关联无法确认，各自保留，其他会话照常同步，并在本地备份中列出待检查的会话 ID。

**同一次切换最多请求退出一次。** 这个标记先写入磁盘，再发送退出请求，后台服务重启也不会重复关闭 App。若索引尚未写完、格式异常或合并失败：

1. 保留待处理状态和错误原因。
2. 恢复本工具刚关闭的 App。
3. App 运行期间不再关闭它，也不改写索引。
4. 等 App 下次自然退出后再检查；不会在用户主动退出后重新把它打开。

每次实际合并前备份两边索引，写入采用原子替换，并检查 App 已停止、文件没有被同时改写。

## 安装

适用于 macOS。需要 Python 3.9+、Xcode Command Line Tools，以及已安装的 Claude Desktop 和 CC Switch。

已在以下组合验证：macOS、Claude Desktop **3.6556.2**、Claude Code **2.1.309**、CC Switch **4.0.15**。本工具读取 App 的本地索引格式，应用升级后应重新检查兼容性。

安装前在官方与第三方模式各启动过一次 Code 会话，使两个会话索引目录都存在。默认要求每侧能唯一识别当前账号的索引；多个账号或目录格式不明时会停止安装，不猜测目标目录。

```sh
git clone https://github.com/Yuwangzhi/claude-switch-sync.git
cd claude-switch-sync

# 只检查依赖、当前配置及索引目录
python3 scripts/install.py --check

# 编译生命周期助手，备份旧部署，安装并启动后台监听
python3 scripts/install.py
```

无需 `sudo`。仓库若为私有，需要先通过自己的 GitHub 账户完成认证。

运行文件安装到 `~/.local/share/claude-switch-sync`；LaunchAgent 安装到 `~/Library/LaunchAgents/com.yuwangzhi.claude-switch-sync.plist`。已有状态、备份和日志会保留。安装目录与当前仓库独立，拉取新代码后重新执行安装命令即可更新。

## 日常检查

```sh
python3 ~/.local/share/claude-switch-sync/switch_sync.py status
tail -20 ~/.local/share/claude-switch-sync/events.jsonl
```

| 状态字段 | 含义 |
| --- | --- |
| `completed_handoffs` | 已完成的跨官方/第三方同步次数 |
| `pending` | 尚未完成的切换；正常完成后移除 |
| `last_error` | 最近阻止同步的原因；成功后移除 |
| `last_warning` | 少数会话无法确认关联，各自保留，其他会话已同步 |
| `last_handoff` | 最近一次合并的数量、写入数及备份编号 |

备份在安装目录的 `backups/<时间-编号>/`，包含两侧原始索引和 `report.json`。如果有未决关联，另有 `unresolved-session-ids.json`。这些都是本机数据，不进入 Git 仓库。

需要恢复时先停用服务并退出 Claude，再检查所需备份；不要在 App 运行期间覆盖索引，也不要用整份 CC Switch 数据库回滚聊天问题。

## 停用或卸载

```sh
# 暂停监听
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.yuwangzhi.claude-switch-sync.plist"

# 恢复监听
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.yuwangzhi.claude-switch-sync.plist"

# 卸载登录启动项，保留程序、日志、备份和全部聊天数据
python3 scripts/install.py --uninstall
```

卸载会把原 LaunchAgent 移到安装目录的部署备份中，便于恢复。

## 测试

```sh
python3 -m unittest -v test_switch_sync.py
```

21 项测试覆盖双向合并、所有模式组合的边界判断、会话 ID 换代、分支冲突、归档/删除状态、并发写入，以及同步失败后恢复 App、服务重启不重复退出等场景。测试使用临时目录和模拟的应用生命周期，不读取真实凭据，不打开或关闭真实 Claude。

实机修复记录见 [验证说明](docs/validation-2026-10-24.md)。

## 适用边界

这是独立的本地辅助工具，不是 Anthropic 或 CC Switch 官方功能。不管理模型列表、推理请求、余额、浏览器权限或 WebDAV 云同步，也不负责把官方订阅加入第三方路由。供应商是否支持直连、路由或聚合由 CC Switch 与上游接口决定。
