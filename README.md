<div align="center">

# teledex

<p><strong>Lightweight Telegram bridge for persistent Codex sessions</strong></p>

<p>
  <a href="./README.md">
    <img src="https://img.shields.io/badge/English-111827?style=flat" alt="English README" />
  </a>
  <a href="./README.zh-CN.md">
    <img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-2563EB?style=flat" alt="简体中文文档" />
  </a>
</p>

<p>
  teledex turns Telegram into a practical remote control layer for Codex with persistent sessions, bound working directories, live draft previews, and final result delivery.
</p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Telegram_Bot_API-26A5E4?style=flat&logo=telegram&logoColor=white" alt="Telegram Bot API" />
  <img src="https://img.shields.io/badge/Codex_CLI-111827?style=flat" alt="Codex CLI" />
  <img src="https://img.shields.io/badge/SQLite-0F80CC?style=flat&logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/systemd-Deployment-FFB000?style=flat" alt="systemd deployment" />
</p>

</div>

## Overview

`teledex` is a `Telegram -> Codex` control bridge designed around real project directories and persistent execution context.

Instead of trying to become a full platform, it focuses on the core remote workflow: send a command or plain message from Telegram, route it into the correct Codex session and working directory, keep one preview message updated during execution, and send the final result back when the run is done.

## Features

- Telegram long-polling message flow
- Whitelist-based access control
- Multiple sessions with create, list, and switch actions
- Per-session working directory binding
- Persistent `tmux` terminal per bound directory
- Codex runs executed inside the persistent terminal context
- Live `draft` preview updates inside a single Telegram message
- `/tstop` support for interrupting the current run
- SQLite persistence for users, sessions, and run state

## Tech Stack

<p>
  <img src="https://img.shields.io/badge/Python-Service-3776AB?style=flat&logo=python&logoColor=white" alt="Python service" />
  <img src="https://img.shields.io/badge/sqlite3-State_Storage-0F80CC?style=flat&logo=sqlite&logoColor=white" alt="sqlite3 state storage" />
  <img src="https://img.shields.io/badge/tmux-Persistent_Terminal-4B5563?style=flat" alt="tmux persistent terminal" />
  <img src="https://img.shields.io/badge/HTML-Telegram_Rendering-E34F26?style=flat&logo=html5&logoColor=white" alt="Telegram rendering" />
  <img src="https://img.shields.io/badge/systemd-Service_Management-FFB000?style=flat" alt="systemd service management" />
</p>

- Service implementation: `Python 3.11+`
- Messaging interface: `Telegram Bot API`
- State storage: `SQLite`, `sqlite3`
- Codex bridge: `tmux`, `codex` CLI, app-server
- Deployment: local long-running process, `systemd`

## Project Structure

```text
src/teledex/
  __main__.py              CLI entry
  app.py                   Telegram loop and command dispatch
  config.py                Environment variable parsing
  storage.py               SQLite persistence layer
  codex_runner.py          tmux terminal startup and Codex event parsing
  codex_app_server_exec.py Codex execution wrapper
  telegram_api.py          Telegram HTTP API client
  formatting.py            Markdown/HTML rendering and message splitting
deploy/
  teledex.service          systemd service example
```

## Quick Start

1. Install the project.

```bash
pip install -e .
```

2. Copy the environment template.

```bash
cp .env.example .env.local
```

3. Fill in the required values in `.env.local`.

- `TELEGRAM_BOT_TOKEN`
- `AUTHORIZED_TELEGRAM_USER_IDS`

4. Start the service.

```bash
set -a
source .env.local
set +a
teledex
```

If you prefer to run it without installing the script entrypoint:

```bash
set -a
source .env.local
set +a
PYTHONPATH=src python3 -m teledex
```

By default, runtime state is stored in `./data/teledex.sqlite3`.

## Configuration

The repository ships with a ready-to-copy `.env.example`. Core variables:

- `TELEGRAM_BOT_TOKEN`: Telegram Bot Token
- `AUTHORIZED_TELEGRAM_USER_IDS`: allowed Telegram user IDs, comma-separated
- `TELEDEX_STATE_DIR`: local state directory, default `./data`
- `TELEDEX_POLL_TIMEOUT_SECONDS`: Telegram long-poll timeout, default `30`
- `TELEDEX_PREVIEW_UPDATE_INTERVAL_SECONDS`: preview refresh interval
- `TELEDEX_CODEX_BIN`: path to the Codex executable, default `codex`
- `TELEDEX_CODEX_EXEC_MODE`: Codex execution mode, supports `default`, `full-auto`, `dangerous`
- `TELEDEX_CODEX_MODEL`: optional Codex model override
- `TELEDEX_CODEX_ENABLE_SEARCH`: whether search is enabled
- `TELEDEX_TMUX_BIN`: path to the tmux executable, default `tmux`
- `TELEDEX_TMUX_SHELL`: shell used to start the tmux session, defaults to `SHELL` or `/bin/bash`
- `TELEDEX_LOG_LEVEL`: log level, default `INFO`

## Telegram Commands

- `/start`: show help text
- `/tnew [title]`: create a teledex session
- `/tsessions`: list sessions
- `/tuse <id>`: switch the active session
- `/tbind <absolute-path>`: bind the working directory and start the persistent tmux terminal
- `/tpwd`: show the bound directory
- `/tstop`: stop the current task

All other `/commands` are forwarded directly into the active Codex session.

Plain text messages are sent to the current active session and executed inside its bound directory.

## Runtime Model

- Each authorized user keeps an active session pointer
- Each session can be bound to a real project directory
- Binding a directory starts a persistent `tmux` terminal for that session
- The first run creates a Codex thread, and later messages try to reuse it inside the same terminal context
- One Telegram preview message is refreshed continuously while the task runs
- Final output and run status are written back after completion

## systemd Deployment

The repository includes a sample service file at [deploy/teledex.service](./deploy/teledex.service).

Typical deployment flow:

```bash
cp deploy/teledex.service /etc/systemd/system/teledex.service
systemctl daemon-reload
systemctl enable --now teledex
systemctl status teledex
```

For production use, keep `.env.local` and the data directory in a stable path and let `systemd` manage the process lifecycle.

## Use Cases

- Continue desktop Codex sessions from your phone
- Keep separate execution context for multiple project directories
- Watch long-running tasks through live preview updates
- Run a lightweight Telegram control layer on a VPS

## Security Note

Current access control mainly depends on an allowed-user whitelist.

That means you should protect:

- your Bot Token
- filesystem permissions of the account running teledex
- exposure of trusted working directories
- host-level process and network isolation when deployed publicly

`teledex` is intentionally a lightweight bridge, not a multi-tenant platform. Add extra controls if you plan to expose it beyond a trusted environment.
