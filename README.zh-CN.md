<div align="center">

# teledex

<p><strong>把 Telegram 变成持续控制 Codex 会话的轻量桥接服务</strong></p>

<p>
  <a href="./README.md">
    <img src="https://img.shields.io/badge/English-111827?style=flat" alt="English README" />
  </a>
  <a href="./README.zh-CN.md">
    <img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-2563EB?style=flat" alt="简体中文文档" />
  </a>
</p>

<p>
  teledex 让你在 Telegram 里持续驱动 Codex，会话不断线、目录可绑定、执行过程可预览、结果自动回传。
</p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Telegram_Bot_API-26A5E4?style=flat&logo=telegram&logoColor=white" alt="Telegram Bot API" />
  <img src="https://img.shields.io/badge/Codex_CLI-111827?style=flat" alt="Codex CLI" />
  <img src="https://img.shields.io/badge/SQLite-0F80CC?style=flat&logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/systemd-%E9%83%A8%E7%BD%B2%E6%89%98%E7%AE%A1-FFB000?style=flat" alt="systemd 部署托管" />
</p>

</div>

## 概览

`teledex` 是一个面向真实项目目录的 `Telegram -> Codex` 控制桥接器。

它不追求复杂的平台化托管，而是专注于最核心的远程协作链路：你在 Telegram 里发命令或普通消息，服务把请求路由到指定的 Codex 会话和工作目录，执行期间持续刷新一条预览消息，完成后再把正式结果回传到 Telegram。

## 特性

- Telegram 长轮询收发消息
- 白名单鉴权，限制可访问用户
- 多会话创建、列表查看与切换
- 每个会话可绑定独立工作目录
- 每个目录绑定一个持久 `tmux` 终端
- 通过 `tmux` 终端复用 Codex 会话与目录上下文
- 基于 app-server 结构化通知的实时预览桥接
- 单条消息实时刷新 `draft` 过程预览
- `/tstop` 中断当前执行任务
- SQLite 本地持久化用户、会话与运行状态

## 技术栈

<p>
  <img src="https://img.shields.io/badge/Python-%E6%9C%8D%E5%8A%A1%E5%AE%9E%E7%8E%B0-3776AB?style=flat&logo=python&logoColor=white" alt="Python 服务实现" />
  <img src="https://img.shields.io/badge/sqlite3-%E7%8A%B6%E6%80%81%E5%AD%98%E5%82%A8-0F80CC?style=flat&logo=sqlite&logoColor=white" alt="sqlite3 状态存储" />
  <img src="https://img.shields.io/badge/tmux-%E6%8C%81%E4%B9%85%E7%BB%88%E7%AB%AF-4B5563?style=flat" alt="tmux 持久终端" />
  <img src="https://img.shields.io/badge/HTML-Telegram_%E6%B6%88%E6%81%AF%E6%B8%B2%E6%9F%93-E34F26?style=flat&logo=html5&logoColor=white" alt="Telegram 消息渲染" />
  <img src="https://img.shields.io/badge/systemd-%E6%9C%8D%E5%8A%A1%E6%89%98%E7%AE%A1-FFB000?style=flat" alt="systemd 服务托管" />
</p>

- 服务实现：`Python 3.11+`
- 消息入口：`Telegram Bot API`
- 状态存储：`SQLite`、`sqlite3`
- Codex 执行桥接：`tmux`、`codex` CLI、app-server
- 部署方式：本地常驻进程、`systemd`

## 项目结构

```text
src/teledex/
  __main__.py              CLI 入口
  app.py                   Telegram 主循环与命令分发
  config.py                环境变量配置解析
  storage.py               SQLite 状态存储
  codex_runner.py          tmux 持久终端与 Codex 事件解析
  codex_app_server_exec.py Codex 执行包装器
  telegram_api.py          Telegram HTTP API 封装
  formatting.py            Markdown/HTML 渲染与消息切分
deploy/
  teledex.service          systemd 服务示例
```

## 快速开始

1. 安装项目。

```bash
pip install -e .
```

2. 复制环境变量模板。

```bash
cp .env.example .env.local
```

3. 在 `.env.local` 中填写至少以下内容。

- `TELEGRAM_BOT_TOKEN`
- `AUTHORIZED_TELEGRAM_USER_IDS`

4. 启动服务。

```bash
set -a
source .env.local
set +a
teledex
```

如果你暂时不想安装脚本入口，也可以直接运行：

```bash
set -a
source .env.local
set +a
PYTHONPATH=src python3 -m teledex
```

默认情况下，运行状态会保存在 `./data/teledex.sqlite3`。

## 配置项

仓库内已经提供可直接复制的 `.env.example`，核心变量如下：

- `TELEGRAM_BOT_TOKEN`：Telegram Bot Token
- `AUTHORIZED_TELEGRAM_USER_IDS`：允许访问的 Telegram 用户 ID，多个用逗号分隔
- `TELEDEX_STATE_DIR`：本地状态目录，默认 `./data`
- `TELEDEX_POLL_TIMEOUT_SECONDS`：Telegram 长轮询超时，默认 `30`
- `TELEDEX_PREVIEW_UPDATE_INTERVAL_SECONDS`：过程预览刷新间隔，默认 `1.0`
- `TELEDEX_CODEX_BIN`：Codex 可执行文件路径，默认 `codex`
- `TELEDEX_CODEX_EXEC_MODE`：Codex 执行模式，支持 `default`、`full-auto`、`dangerous`
- `TELEDEX_CODEX_MODEL`：可选的 Codex 模型覆盖
- `TELEDEX_CODEX_ENABLE_SEARCH`：是否启用搜索能力
- `TELEDEX_CODEX_PERSIST_EXTENDED_HISTORY`：是否持久化更完整的线程历史，默认 `true`
- `TELEDEX_TMUX_BIN`：tmux 可执行文件路径，默认 `tmux`
- `TELEDEX_TMUX_SHELL`：tmux 会话默认 shell，默认读取当前 `SHELL`，缺省为 `/bin/bash`
- `TELEDEX_LOG_LEVEL`：日志级别，默认 `INFO`

## Telegram 命令

- `/start`：查看帮助说明
- `/tnew [标题]`：新建 teledex 会话
- `/tsessions`：查看会话列表
- `/tuse <id>`：切换当前会话
- `/tbind <绝对路径>`：绑定当前会话目录并启动持久 tmux 终端
- `/tpwd`：查看当前会话目录
- `/tstop`：停止当前任务

除以上管理命令外，其他 `/命令` 会直接转发给当前 Codex 会话。

普通文本消息会默认发送给当前活跃会话，并在其绑定目录中继续执行。

## 运行模型

- 每个授权用户都维护自己的活跃会话指针
- 每个会话都可以绑定一个真实项目目录
- 绑定目录时会先启动一个持久 `tmux` 终端
- 首次执行会创建 Codex 线程，后续消息会在同一终端里尽量复用已有线程
- 默认开启 `persistExtendedHistory`，让后续 resume 更完整保留上下文
- 执行过程中会持续刷新同一条 Telegram 预览消息
- 预览会分开展示思考摘要、工具输出与最终回复流
- 完成后会回传正式输出，并写入运行状态

## systemd 部署

仓库自带示例服务文件：[deploy/teledex.service](./deploy/teledex.service)。

典型部署流程：

```bash
cp deploy/teledex.service /etc/systemd/system/teledex.service
systemctl daemon-reload
systemctl enable --now teledex
systemctl status teledex
```

生产使用时，建议把 `.env.local` 与数据目录放在稳定路径下，再让 `systemd` 负责进程生命周期管理。

## 适用场景

- 在手机上远程继续桌面上的 Codex 会话
- 为多个项目目录维护独立执行上下文
- 在长任务执行期间实时查看过程预览
- 在 VPS 上常驻一个轻量的 Telegram 控制入口

## 安全说明

当前版本的访问控制主要依赖允许访问用户的白名单。

因此你需要重点保护：

- Bot Token
- 运行账户的文件系统权限
- 受信任工作目录的暴露范围
- 公网部署时的进程与网络隔离

`teledex` 的定位是轻量桥接器，不是完整的多租户平台。如果要对外暴露到不受信任环境，建议额外补充更严格的安全控制。
