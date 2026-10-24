# Claude Switch Sync

**切换模型，接着聊。**

Claude 官方订阅与第三方模式各有一份 Desktop 会话列表。Claude Switch Sync 把两边的 Code 会话历史衔接起来：在 CC Switch 中切到 DeepSeek 等第三方供应商，再切回官方订阅，之前的对话都能继续出现在 App 中。

一次供应商选择，自动完成退出、备份、同步与重新打开。后台服务随 macOS 登录启动，直接响应切换事件，让聊天历史跟着使用过程持续积累。

## 一次切换，双向衔接

例如，官方订阅额度用完后，可以这样继续工作：

1. 结束当前回复，在 CC Switch 的 **Claude Desktop** 页面选择第三方供应商。
2. 服务正常退出 Claude，备份两边索引，合并会话列表与归档状态。
3. Claude 自动重新打开，在历史列表中选择原来的对话。

切回官方订阅时，同样执行这套流程。App 原本处于关闭状态时，服务更新索引，供下次打开时加载。

| 使用场景 | 历史衔接方式 |
| --- | --- |
| Desktop：官方订阅 ↔ 第三方 | 自动合并两侧会话索引，并重新打开原本运行的 App |
| Desktop：第三方供应商之间切换 | 沿用同一份第三方会话列表 |
| Desktop：第三方直连、路由、聚合之间切换 | 沿用第三方会话列表，按 App 提示加载连接配置 |
| Claude Code CLI | 沿用 `~/.claude/projects`，重新打开 CLI 加载供应商配置后，通过 `/resume` 继续会话 |

CC Switch 分别管理 Claude Code CLI 和 Claude Desktop 的供应商，按正在使用的客户端选择对应页面即可。

## 让历史持续积累

- **保留最新进度。** 同一会话采用最近活动的索引，识别 App 续聊过程中记录的新旧会话 ID 关联。
- **每次同步先备份。** 两侧原始索引与合并结果留存在本机，方便追溯和恢复。
- **正文沿用原位置。** 聊天正文保存在 `~/.claude/projects`；同步服务负责衔接 App 会话列表和归档状态，账号与供应商配置继续由原客户端管理。
- **同步与启动相互配合。** App 完成退出后更新索引；遇到待处理记录时恢复 App，保留现场，随后在合适的退出时机继续同步。

## 快速开始

准备 macOS、Python **3.9+**、Xcode Command Line Tools、Claude Desktop 和 CC Switch。先在官方与第三方模式下分别创建过 Code 会话，使两侧索引目录就绪。

```sh
git clone https://github.com/Yuwangzhi/claude-switch-sync.git
cd claude-switch-sync

# 检查依赖、当前配置与会话索引
python3 scripts/install.py --check

# 安装并启动后台服务
python3 scripts/install.py
```

安装使用当前用户权限，自动备份已有部署。运行文件位于 `~/.local/share/claude-switch-sync`，登录启动项位于 `~/Library/LaunchAgents/com.yuwangzhi.claude-switch-sync.plist`。

更新时，在仓库目录执行：

```sh
git pull --ff-only
python3 scripts/install.py
```

## 验证

在 Claude Desktop **3.6556.2**、Claude Code **2.1.309**、CC Switch **4.0.15** 的 macOS 环境中，已完成 **164 条会话索引**的同步与 App 重新打开验证。**21 项回归测试**覆盖双向合并、会话 ID 变化、归档状态、并发写入及异常恢复。

```sh
python3 -m unittest -v test_switch_sync.py
```

## 使用与维护

- [使用指南](docs/usage.md)：同步规则、运行状态、备份恢复、暂停与卸载。
- [验证记录](docs/validation-2026-10-24.md)：测试范围与实机结果。
- [Claude 官方会话说明](https://code.claude.com/docs/en/sessions)：CLI 会话保存与恢复方式。
