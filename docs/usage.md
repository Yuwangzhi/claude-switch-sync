# 使用指南

## 日常切换

先结束正在生成的回复，再在 CC Switch 中选择供应商。跨官方订阅与第三方模式切换时，服务正常退出 Claude Desktop、合并会话索引，再重新打开原本运行的 App。正常退出会结束 App 中运行的任务。

CLI 和 Desktop 的供应商分别选择。CLI 会话正文沿用 `~/.claude/projects`；重新打开 CLI 加载当前供应商配置，在原项目目录运行 `/resume` 继续会话。Desktop 使用各自的会话列表，相关关系见 [官方会话说明](https://code.claude.com/docs/en/sessions)。

第三方供应商之间，以及第三方直连、路由和聚合之间切换，都沿用 `Claude-3p` 的会话列表。连接配置的重新加载按 App 提示操作。官方订阅与第三方之间的跨模式切换才触发索引合并。

## 数据位置与合并规则

| 数据 | 保存位置与处理方式 |
| --- | --- |
| 官方 App 会话索引 | `~/Library/Application Support/Claude/claude-code-sessions/…` |
| 第三方 App 会话索引 | `~/Library/Application Support/Claude-3p/claude-code-sessions/…` |
| 会话的模型与推理强度 | 保存在各侧会话索引中，切换后沿用该侧的选择 |
| 聊天正文 | 沿用 `~/.claude/projects` 中的原始文件 |
| 账号、密钥与供应商设置 | 由 Claude 和 CC Switch 继续管理 |
| 同步状态、日志与备份 | `~/.local/share/claude-switch-sync` |

同一会话取最后活动时间较新的索引；时间一致时采用刚退出的一侧。App 更换 `cliSessionId` 后，较新记录的 `priorCliSessionIds` 须明确包含旧 ID，且会话 ID、创建时间和项目目录一致，服务才按同一会话更新。

合并聊天进度时，各侧保留自己已有的 `model` 与 `effort` 字段。例如，官方订阅选择 Sonnet 的 Medium，第三方选择 DeepSeek 的 High，往返切换后两侧继续沿用各自的组合。会话首次出现在另一侧时，由目标客户端的默认设置决定模型与推理强度；之后的选择保存在该侧索引。原字段未设置或显式为 `null` 时，保留原状态。

历史记录中的模型使用信息随聊天进度同步，当前模型与推理选择单独保留。第三方供应商共用 `Claude-3p` 索引，因此这里的选择按官方、第三方两侧保存。

关联待确认的记录各自保留，其余会话继续同步。归档状态随选中的会话索引更新，删除标记用于维持已有的删除结果。重复会话保留给用户按需整理。

每次合并都先备份两侧索引，再以原子替换写入。写入前检查 App 已退出、文件保持稳定，完成后核对写入结果。

## 异常恢复

每次切换最多请求正常退出一次，标记会先保存到磁盘。服务重启后沿用这个标记。

如配置仍在写入、索引格式待检查或合并暂时失败，服务保留待处理状态与原因，恢复它刚关闭的 App。之后等 App 自然退出再检查。用户主动退出后，App 保持关闭。

安装器会识别两侧当前账号的索引目录。遇到多个候选账号、目录变化或应用升级后的新格式时，先根据报错检查目标目录与备份，再调整配置。

## 查看运行状态

```sh
python3 ~/.local/share/claude-switch-sync/switch_sync.py status
tail -20 ~/.local/share/claude-switch-sync/events.jsonl
```

| 字段 | 含义 |
| --- | --- |
| `completed_handoffs` | 已完成的跨模式同步次数 |
| `pending` | 等待完成的切换，成功后移除 |
| `last_error` | 最近阻止同步的原因，成功后移除 |
| `last_warning` | 部分会话关联待确认，其余会话已同步 |
| `last_handoff` | 最近合并的数量、写入数与备份编号 |

运行异常记录在 `errors.log`。服务由 LaunchAgent 在登录时启动，以文件与进程事件驱动。

原生启动助手按 `/Applications/Claude.app/Contents/MacOS/Claude` 的实际可执行文件识别 App，与同步服务的进程检测采用相同依据。系统临时登记了错误的应用标识时，仍可找到正确的 Claude 进程并请求正常退出。

## 备份与恢复

安装目录的 `backups/<时间-编号>/` 保存两侧原始索引和 `report.json`。关联待确认时，另有 `unresolved-session-ids.json` 列出对应会话。

恢复前暂停服务、退出 Claude，再按备份中的账号目录逐项核对并恢复索引。恢复对象是会话索引；CC Switch 的供应商数据库独立维护。

## 暂停、恢复与卸载

```sh
# 暂停监听
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.yuwangzhi.claude-switch-sync.plist"

# 恢复监听
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.yuwangzhi.claude-switch-sync.plist"

# 在源码仓库中执行卸载
python3 scripts/install.py --uninstall
```

卸载移除登录启动项，并将原 LaunchAgent 保存到部署备份。程序文件、运行状态、日志、备份和聊天数据继续保留在本机。

## 工具分工

Claude Switch Sync 负责本机的 App 会话索引衔接。模型列表、直连/路由/聚合连接及 WebDAV 配置同步由 CC Switch 管理；模型能力与额度由对应服务提供。
