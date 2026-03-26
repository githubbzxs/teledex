<div align="center">

# teledex

<p><strong>把 Telegram 变成持续控制 Codex 会话的轻量桥接服务</strong></p>

<p>
  <a href="./README.md">
    <img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-2563EB?style=flat" alt="简体中文文档" />
  </a>
  <a href="./docs/PLAN.md">
    <img src="https://img.shields.io/badge/%E4%BA%A7%E5%93%81%E8%AE%A1%E5%88%92-111827?style=flat" alt="产品计划" />
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
  <img src="https://img.shields.io/badge/systemd-管理部署-FFB000?style=flat" alt="systemd" />
</p>

</div>

## 概览

`teledex` 是一个面向真实项目目录的 `Telegram -> Codex` 控制桥接器。

它不追求复杂的平台化托管，而是把最核心的远程协作链路做好：你在 Telegram 里发命令或普通消息，服务把请求路由到指定的 Codex 会话和工作目录，持续更新过程预览，并在完成后把正式结果回传到 Telegram。

## 特性

- Telegram 长轮询收发消息
- 白名单鉴权，限制可访问用户
- 多会话创建、列表查看与切换
- 每个会话可绑定独立工作目录
- 基于 `codex exec` / `codex exec resume` 的持续会话
- 单条消息实时刷新 `draft` 过程预览
- `/stop` 中断当前执行任务
- SQLite 本地持久化用户、会话与运行状态

## 技术栈

<p>
  <img src="https://img.shields.io/badge/Python-服务实现-3776AB?style=flat&logo=python&logoColor=white" alt="Python 服务实现" />
  <img src="https://img.shields.io/badge/sqlite3-状态持久化-0F80CC?style=flat&logo=sqlite&logoColor=white" alt="sqlite3 状态持久化" />
  <img src="https://img.shields.io/badge/subprocess-Codex_进程桥接-4B5563?style=flat" alt="subprocess Codex 进程桥接" />
  <img src="https://img.shields.io/badge/HTML-Telegram_消息渲染-E34F26?style=flat&logo=html5&logoColor=white" alt="Telegram 消息渲染" />
  <img src="https://img.shields.io/badge/systemd-服务托管-FFB000?style=flat" alt="systemd 服务托管" />
</p>

- 服务实现：`Python 3.11+`
- 消息入口：`Telegram Bot API`
- 状态存储：`SQLite`、`sqlite3`
- Codex 执行桥接：`subprocess`、`codex` CLI
- 部署方式：本地常驻进程、`systemd`

## 项目结构

```text
src/teledex/
  __main__.py             CLI 入口
  app.py                  Telegram 主循环与命令分发
  config.py               环境变量配置解析
  storage.py              SQLite 状态存储
  codex_runner.py         Codex 进程启动与事件解析
  codex_app_server_exec.py Codex 执行包装器
  telegram_api.py         Telegram HTTP API 封装
  formatting.py           Markdown/HTML 渲染与消息切分
deploy/
  teledex.service         systemd 服务示例
docs/
  PLAN.md                 产品目标与实现规划
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

3. 按需修改 `.env.local` 中的配置，至少填写：

- `TELEGRAM_BOT_TOKEN`
- `AUTHORIZED_TELEGRAM_USER_IDS`

4. 启动服务。

```bash
set -a
source .env.local
set +a
teledex
```

如果你没有把脚本安装到当前环境，也可以直接运行：

```bash
set -a
source .env.local
set +a
PYTHONPATH=src python3 -m teledex
```

默认情况下，运行状态会保存在 `./data/teledex.sqlite3`。

## 配置项

`.env.example` 已内置常用配置，核心变量如下：

- `TELEGRAM_BOT_TOKEN`：Telegram Bot Token
- `AUTHORIZED_TELEGRAM_USER_IDS`：允许访问的 Telegram 用户 ID，多个用逗号分隔
- `TELEDEX_STATE_DIR`：本地状态目录，默认 `./data`
- `TELEDEX_POLL_TIMEOUT_SECONDS`：Telegram 长轮询超时，默认 `30`
- `TELEDEX_PREVIEW_UPDATE_INTERVAL_SECONDS`：过程预览刷新节流间隔
- `TELEDEX_CODEX_BIN`：Codex 可执行文件路径，默认 `codex`
- `TELEDEX_CODEX_EXEC_MODE`：Codex 执行模式，支持 `default`、`full-auto`、`dangerous`
- `TELEDEX_CODEX_MODEL`：可选的 Codex 模型名
- `TELEDEX_CODEX_ENABLE_SEARCH`：是否启用搜索能力
- `TELEDEX_LOG_LEVEL`：日志级别，默认 `INFO`

## Telegram 命令

- `/start`：查看帮助说明
- `/new [标题]`：新建会话
- `/sessions`：查看会话列表
- `/use <id>`：切换当前会话
- `/bind <绝对路径>`：绑定当前会话目录
- `/pwd`：查看当前会话目录
- `/stop`：停止当前任务

普通文本消息会默认发送给当前活跃会话，并在绑定目录中继续执行。

## 运行机制

- 每个授权用户都维护自己的活跃会话状态
- 每个会话都可以绑定一个真实工作目录
- 首次执行会创建 Codex 会话，后续消息优先复用已有线程
- 执行过程中会持续刷新同一条 Telegram 预览消息
- 任务结束后会发送正式结果，并将运行状态写入 SQLite

## systemd 部署

仓库自带示例服务文件：[deploy/teledex.service](./deploy/teledex.service)

典型部署流程：

```bash
cp deploy/teledex.service /etc/systemd/system/teledex.service
systemctl daemon-reload
systemctl enable --now teledex
systemctl status teledex
```

建议把 `.env.local` 与数据目录放在稳定路径下，再通过 `systemd` 托管为常驻服务。

## 适用场景

- 在手机上远程继续桌面上的 Codex 会话
- 针对多个项目目录维护独立的执行上下文
- 在长任务执行时实时查看过程预览
- 在 VPS 上常驻一个轻量的 Telegram 控制入口

## 安全说明

当前版本的访问控制主要依赖白名单用户 ID。

这意味着：

- Bot Token 需要妥善保管
- 运行账户必须只对可信目录开放访问权限
- 如果部署在公网主机上，应配合最小权限、日志审计和进程隔离一起使用

`teledex` 的定位是轻量桥接器，不是完整的多租户托管平台；生产环境使用前，建议先按自己的安全边界补充额外保护。
