from __future__ import annotations

import html
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .codex_runner import CodexProcessHandle, CodexRunner
from .config import AppConfig
from .formatting import (
    markdown_to_telegram_html,
    split_markdown_message,
)
from .storage import SessionRecord, Storage
from .telegram_api import (
    TelegramApiError,
    TelegramClient,
    TelegramMessage,
    is_message_not_modified_error,
)


HELP_TEXT = """teledex 可用命令：
/start - 查看帮助
/tnew - 新建 teledex 会话
/tsessions - 查看会话列表
/tuse <id> - 切换当前会话
/tbind <绝对路径> - 绑定当前会话目录并启动持久 tmux 终端
/tpwd - 查看当前会话目录
/tstop - 停止当前任务

除以上管理命令外，其他 `/命令` 会直接作为 Codex 原生命令发送到当前会话。
直接发送普通文本，也会继续当前活跃会话。"""

_PREVIEW_TYPING_INTERVAL_SECONDS = 4.0
_PREVIEW_ANIMATION_INTERVAL_SECONDS = 1.0
_PREVIEW_HEARTBEAT_FRAMES = ("○", "●")
_PREVIEW_COMPLETE_FRAME = "●"
_PREVIEW_HISTORY_MAX_CHARS = 2000
_PREVIEW_TOOL_OUTPUT_MAX_CHARS = 2000
_PREVIEW_OUTPUT_MAX_CHARS = 2200
_PREVIEW_MESSAGE_MAX_CHARS = 3800
_PREVIEW_LOOP_IDLE_SECONDS = 0.1
_PREVIEW_DRAIN_TIMEOUT_SECONDS = 8.0
_BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "查看帮助"),
    ("tnew", "新建会话"),
    ("tsessions", "查看会话"),
    ("tuse", "切换会话"),
    ("tbind", "绑定目录"),
    ("tpwd", "当前目录"),
    ("tstop", "停止任务"),
)
_LOCAL_COMMANDS = {
    "/start",
    "/help",
    "/tnew",
    "/tsessions",
    "/tuse",
    "/tbind",
    "/tpwd",
    "/tstop",
}
_MIRRORED_CODEX_COMMANDS = {
    "/agent",
    "/apps",
    "/approvals",
    "/bettercodex",
    "/clean",
    "/clear",
    "/collab",
    "/compact",
    "/copy",
    "/debug-config",
    "/diff",
    "/exit",
    "/experimental",
    "/fast",
    "/feedback",
    "/fork",
    "/init",
    "/logout",
    "/mcp",
    "/model",
    "/new",
    "/permissions",
    "/personality",
    "/plan",
    "/ps",
    "/quit",
    "/realtime",
    "/rename",
    "/resume",
    "/review",
    "/rollout",
    "/sandbox-add-read-dir",
    "/settings",
    "/skills",
    "/status",
    "/statusline",
    "/subagents",
    "/theme",
}
_APPROVAL_POLICY_VALUES = ("untrusted", "on-failure", "on-request", "never")
_SANDBOX_MODE_VALUES = ("read-only", "workspace-write", "danger-full-access")
_PERSONALITY_VALUES = ("none", "friendly", "pragmatic")
_REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")
_COLLABORATION_MODE_VALUES = ("default", "plan")


def _session_title_from_path(path: Path) -> str:
    normalized = path.expanduser()
    name = normalized.name.strip()
    return name or str(normalized)


@dataclass(slots=True)
class IncomingMessage:
    chat_id: int
    user_id: int
    text: str
    message_id: int
    message_thread_id: int | None


@dataclass(slots=True)
class ActiveRun:
    run_id: int
    session_id: int
    user_id: int
    chat_id: int
    message_thread_id: int | None
    prompt: str
    preview_message_id: int | None = None
    process_handle: CodexProcessHandle | None = None
    stop_requested: bool = False


class LivePreviewState:
    def __init__(
        self,
        initial_status: str = "Thinking",
        history_max_chars: int = _PREVIEW_HISTORY_MAX_CHARS,
        output_max_chars: int = _PREVIEW_OUTPUT_MAX_CHARS,
        tool_output_max_chars: int = _PREVIEW_TOOL_OUTPUT_MAX_CHARS,
    ) -> None:
        self._status_text = initial_status.strip() or "Thinking"
        self._target_text = ""
        self._commentary_order: list[str] = []
        self._commentary_text_by_id: dict[str, str] = {}
        self._tool_order: list[str] = []
        self._tool_command_by_id: dict[str, str] = {}
        self._tool_output_by_id: dict[str, str] = {}
        self._footer_statusline = ""
        self._frame_index = 0
        self._history_max_chars = max(1, history_max_chars)
        self._output_max_chars = max(1, output_max_chars)
        self._tool_output_max_chars = max(1, tool_output_max_chars)
        self._in_progress = True
        self._flush_requested = False
        self._elapsed_seconds = 0
        self._lock = threading.RLock()

    def update_status(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        with self._lock:
            if normalized == self._status_text:
                return
            self._status_text = normalized
            self._flush_requested = True

    def update_stream_text(self, text: str) -> None:
        normalized = text.replace("\r\n", "\n").rstrip()
        if not normalized:
            return
        with self._lock:
            if normalized == self._target_text:
                return
            self._target_text = normalized
            self._in_progress = True
            self._flush_requested = True

    def update_commentary(self, item_id: str, text: str) -> None:
        normalized = text.replace("\r\n", "\n").rstrip()
        normalized_id = item_id.strip()
        if not normalized or not normalized_id:
            return
        with self._lock:
            previous = self._commentary_text_by_id.get(normalized_id)
            if normalized_id not in self._commentary_text_by_id:
                self._commentary_order.append(normalized_id)
            if previous == normalized:
                return
            self._commentary_text_by_id[normalized_id] = normalized
            self._in_progress = True
            self._flush_requested = True

    def clear_commentary(self, item_id: str) -> None:
        normalized_id = item_id.strip()
        if not normalized_id:
            return
        with self._lock:
            if normalized_id not in self._commentary_text_by_id:
                return
            self._commentary_text_by_id.pop(normalized_id, None)
            self._commentary_order = [
                current_id
                for current_id in self._commentary_order
                if current_id != normalized_id
            ]
            self._flush_requested = True

    def update_tool_state(
        self,
        item_id: str | None,
        command_text: str | None = None,
        output_text: str | None = None,
    ) -> None:
        normalized_id = (item_id or "tool:fallback").strip()
        normalized_command = (command_text or "").strip()
        normalized_output = (output_text or "").replace("\r\n", "\n").rstrip()
        if not normalized_id or (not normalized_command and not normalized_output):
            return
        with self._lock:
            if normalized_id not in self._tool_order:
                self._tool_order.append(normalized_id)
            changed = False
            if normalized_command and normalized_command != self._tool_command_by_id.get(normalized_id, ""):
                self._tool_command_by_id[normalized_id] = normalized_command
                changed = True
            if normalized_output and normalized_output != self._tool_output_by_id.get(normalized_id, ""):
                self._tool_output_by_id[normalized_id] = normalized_output
                changed = True
            if not changed:
                return
            self._in_progress = True
            self._flush_requested = True

    def update_footer_statusline(self, text: str) -> None:
        normalized = " ".join(text.split()).strip()
        if not normalized:
            return
        with self._lock:
            if normalized == self._footer_statusline:
                return
            self._footer_statusline = normalized
            self._flush_requested = True

    def advance(self, animate_steps: int = 0, elapsed_seconds: int = 0) -> str:
        with self._lock:
            if animate_steps > 0 and self._in_progress:
                self._frame_index = (
                    self._frame_index + animate_steps
                ) % len(_PREVIEW_HEARTBEAT_FRAMES)
                self._elapsed_seconds += max(0, elapsed_seconds)
            return self._render_locked()

    def render(self) -> str:
        with self._lock:
            return self._render_locked()

    def render_final_html(self) -> str:
        with self._lock:
            return self._render_final_html_locked()

    def has_pending_stream(self) -> bool:
        with self._lock:
            return self._flush_requested

    def mark_rendered(self) -> None:
        with self._lock:
            self._flush_requested = False

    def target_text(self) -> str:
        with self._lock:
            return self._target_text

    def finish(self, status_text: str) -> str:
        with self._lock:
            self._status_text = status_text.strip() or self._status_text
            self._commentary_order.clear()
            self._commentary_text_by_id.clear()
            self._tool_order.clear()
            self._tool_command_by_id.clear()
            self._tool_output_by_id.clear()
            self._in_progress = False
            self._flush_requested = True
            return self._render_locked()

    def complete(self) -> str:
        return self.finish("Completed")

    def _render_locked(self) -> str:
        marker = (
            _PREVIEW_HEARTBEAT_FRAMES[self._frame_index]
            if self._in_progress
            else _PREVIEW_COMPLETE_FRAME
        )
        sections = [
            f"{marker} {self._status_text} ({_format_elapsed_compact(self._elapsed_seconds)})"
        ]
        body = self._build_body_locked()
        if body:
            sections.extend(["", body])
        if self._footer_statusline:
            sections.extend(["", self._footer_statusline])
        rendered = "\n".join(sections).strip()
        return _truncate_preview_text(rendered, _PREVIEW_MESSAGE_MAX_CHARS)

    def _render_final_html_locked(self) -> str:
        marker = (
            _PREVIEW_HEARTBEAT_FRAMES[self._frame_index]
            if self._in_progress
            else _PREVIEW_COMPLETE_FRAME
        )
        sections = [
            html.escape(
                f"{marker} {self._status_text} ({_format_elapsed_compact(self._elapsed_seconds)})"
            )
        ]
        if self._target_text:
            sections.extend(
                [
                    "",
                    markdown_to_telegram_html(self._target_text) or html.escape(self._target_text),
                ]
            )
        if self._footer_statusline:
            sections.extend(["", html.escape(self._footer_statusline)])
        return "\n".join(sections).strip()

    def _build_body_locked(self) -> str:
        sections: list[str] = []
        commentary = self._render_commentary_locked()
        if commentary:
            sections.append(commentary)

        output_text = _truncate_preview_text(
            self._target_text,
            self._output_max_chars,
        )
        if output_text:
            sections.append(output_text)

        return "\n\n".join(section for section in sections if section).strip()

    def _render_commentary_locked(self) -> str:
        if not self._commentary_order:
            return ""
        entries = [
            self._commentary_text_by_id[item_id]
            for item_id in self._commentary_order
            if self._commentary_text_by_id.get(item_id)
        ]
        return _truncate_preview_middle("\n\n".join(entries), self._history_max_chars)

    def _render_tool_blocks_locked(self) -> str:
        blocks: list[str] = []
        for item_id in self._tool_order:
            output_text = _truncate_preview_tail(
                self._tool_output_by_id.get(item_id, ""),
                self._tool_output_max_chars,
            )
            parts = [
                part
                for part in (
                    self._tool_command_by_id.get(item_id, ""),
                    output_text,
                )
                if part
            ]
            if parts:
                blocks.append("\n".join(parts).strip())
        return "\n\n".join(blocks).strip()


def _truncate_preview_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _truncate_preview_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 7:
        return _truncate_preview_text(text, max_chars)
    separator = "\n...\n"
    head_chars = max(1, (max_chars - len(separator)) // 2)
    tail_chars = max(1, max_chars - len(separator) - head_chars)
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    return f"{head}{separator}{tail}"


def _truncate_preview_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 4:
        return text[-max_chars:]
    return "...\n" + text[-(max_chars - 4) :].lstrip()


def _format_elapsed_compact(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes}m"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m"


def _normalize_preview_interval(seconds: float) -> float:
    return max(0.2, float(seconds))


def _next_preview_deadline(
    previous_deadline: float,
    now: float,
    interval_seconds: float,
) -> float:
    deadline = previous_deadline
    if deadline <= 0:
        deadline = now + interval_seconds
    while deadline <= now:
        deadline += interval_seconds
    return deadline


class TeledexApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.config.state_dir / "teledex.sqlite3")
        self.telegram = TelegramClient(self.config.telegram_bot_token)
        self.runner = CodexRunner(config)
        self.logger = logging.getLogger("teledex")
        self._active_runs: dict[int, ActiveRun] = {}
        self._active_runs_lock = threading.RLock()
        self._update_offset: int | None = None

    def _is_local_command(self, text: str) -> bool:
        return self._extract_command(text) in _LOCAL_COMMANDS

    def _is_mirrored_codex_command(self, text: str) -> bool:
        return self._extract_command(text) in _MIRRORED_CODEX_COMMANDS

    def _extract_command(self, text: str) -> str:
        command_text = text.split()[0]
        return command_text.split("@", 1)[0].lower()

    def run_forever(self) -> None:
        bot = self.telegram.get_me()
        self.logger.info("Telegram bot 已连接: @%s", bot.get("username", "unknown"))
        self._sync_bot_commands()
        while True:
            try:
                updates = self.telegram.get_updates(
                    offset=self._update_offset,
                    timeout_seconds=self.config.poll_timeout_seconds,
                )
                for update in updates:
                    self._update_offset = int(update["update_id"]) + 1
                    self._handle_update(update)
            except TelegramApiError:
                self.logger.exception("Telegram 轮询失败")
                time.sleep(3)
            except Exception:
                self.logger.exception("主循环异常")
                time.sleep(3)

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        from_user = message.get("from") or {}
        user_id = int(from_user.get("id", 0))
        chat = message.get("chat") or {}
        incoming = IncomingMessage(
            chat_id=int(chat.get("id")),
            user_id=user_id,
            text=text.strip(),
            message_id=int(message.get("message_id")),
            message_thread_id=(
                int(message["message_thread_id"])
                if message.get("message_thread_id") is not None
                else None
            ),
        )

        if user_id not in self.config.authorized_user_ids:
            self._safe_send_message(
                incoming.chat_id,
                "未授权用户，无法使用该 bot。",
                incoming.message_thread_id,
            )
            return

        self.storage.ensure_user(
            user_id=incoming.user_id,
            chat_id=incoming.chat_id,
            message_thread_id=incoming.message_thread_id,
        )

        if incoming.text.startswith("//"):
            self._handle_prompt(self._normalize_incoming_message(incoming))
            return

        if incoming.text.startswith("/") and self._is_local_command(incoming.text):
            self._handle_command(incoming)
            return

        if incoming.text.startswith("/") and self._is_mirrored_codex_command(incoming.text):
            self._handle_codex_command(incoming)
            return

        self._handle_prompt(incoming)

    def _normalize_incoming_message(self, incoming: IncomingMessage) -> IncomingMessage:
        if not incoming.text.startswith("//"):
            return incoming
        return IncomingMessage(
            chat_id=incoming.chat_id,
            user_id=incoming.user_id,
            text="/" + incoming.text[2:],
            message_id=incoming.message_id,
            message_thread_id=incoming.message_thread_id,
        )

    def _handle_command(self, incoming: IncomingMessage) -> None:
        command_text = incoming.text.split()[0]
        command = self._extract_command(incoming.text)
        args = incoming.text[len(command_text) :].strip()

        if command in {"/help", "/start"}:
            self._safe_send_message(incoming.chat_id, HELP_TEXT, incoming.message_thread_id)
            return

        if command == "/tnew":
            title = args or f"未绑定目录 #{len(self.storage.list_sessions(incoming.user_id)) + 1}"
            session = self.storage.create_session(incoming.user_id, title)
            self.storage.set_active_session(
                incoming.user_id,
                session.id,
                chat_id=incoming.chat_id,
                message_thread_id=incoming.message_thread_id,
            )
            self._safe_send_message(
                incoming.chat_id,
                f"已创建会话 #{session.id}\n当前名称：{session.title}\n接下来请先用 /tbind 绑定目录，绑定后会自动改成路径名。",
                incoming.message_thread_id,
            )
            return

        if command == "/tsessions":
            sessions = self.storage.list_sessions(incoming.user_id)
            user = self.storage.get_user(incoming.user_id)
            if not sessions:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前还没有会话，先用 /tnew 创建一个。",
                    incoming.message_thread_id,
                )
                return

            lines = ["你的会话列表："]
            active_session = self.storage.get_active_session(
                incoming.user_id,
                incoming.chat_id,
                incoming.message_thread_id,
            )
            active_id = active_session.id if active_session else (user.active_session_id if user else None)
            for session in sessions:
                active_mark = " <- 当前" if session.id == active_id else ""
                path = session.bound_path or "未绑定目录"
                thread_state = "已创建 Codex 会话" if session.codex_thread_id else "尚未执行"
                lines.append(
                    f"#{session.id} [{session.status}] {session.title}{active_mark}\n"
                    f"目录：{path}\n"
                    f"Codex：{thread_state}"
                )
            self._send_long_message(
                incoming.chat_id,
                "\n\n".join(lines),
                incoming.message_thread_id,
            )
            return

        if command == "/tuse":
            if not args:
                self._safe_send_message(
                    incoming.chat_id,
                    "用法：/tuse <id>",
                    incoming.message_thread_id,
                )
                return
            try:
                session_id = int(args)
            except ValueError:
                self._safe_send_message(
                    incoming.chat_id,
                    "会话 ID 必须是数字。",
                    incoming.message_thread_id,
                )
                return
            session = self.storage.get_session(session_id, incoming.user_id)
            if session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    f"找不到会话 #{session_id}。",
                    incoming.message_thread_id,
                )
                return
            self.storage.set_active_session(
                incoming.user_id,
                session_id,
                chat_id=incoming.chat_id,
                message_thread_id=incoming.message_thread_id,
            )
            self._safe_send_message(
                incoming.chat_id,
                f"已切换到会话 #{session.id}\n标题：{session.title}",
                incoming.message_thread_id,
            )
            return

        if command == "/tbind":
            if not args:
                self._safe_send_message(
                    incoming.chat_id,
                    "用法：/tbind <绝对路径>",
                    incoming.message_thread_id,
                )
                return
            bound_path = Path(args).expanduser()
            if not bound_path.is_absolute():
                self._safe_send_message(
                    incoming.chat_id,
                    "请提供绝对路径，例如 /tbind /root/project。",
                    incoming.message_thread_id,
                )
                return
            if not bound_path.exists() or not bound_path.is_dir():
                self._safe_send_message(
                    incoming.chat_id,
                    f"目录不存在：{bound_path}",
                    incoming.message_thread_id,
                )
                return
            active_session = self.storage.get_active_session(
                incoming.user_id,
                incoming.chat_id,
                incoming.message_thread_id,
            )
            normalized_path = str(bound_path)
            target_session = self.storage.get_session_by_bound_path(
                incoming.user_id,
                normalized_path,
            )
            created_new_session = False
            if target_session is None:
                if active_session is None or (
                    active_session.bound_path is not None
                    and active_session.bound_path != normalized_path
                ):
                    target_session = self.storage.create_session(
                        incoming.user_id,
                        _session_title_from_path(bound_path),
                    )
                    created_new_session = True
                else:
                    target_session = active_session
                self.storage.bind_session_path(
                    target_session.id,
                    incoming.user_id,
                    normalized_path,
                )
            self.storage.set_active_session(
                incoming.user_id,
                target_session.id,
                chat_id=incoming.chat_id,
                message_thread_id=incoming.message_thread_id,
            )
            try:
                self.runner.reset_terminal(target_session.id, bound_path)
                tmux_session_name = self.runner.ensure_terminal(target_session.id, bound_path)
                action_text = (
                    f"已自动创建会话 #{target_session.id} 并绑定目录："
                    if created_new_session
                    else f"会话 #{target_session.id} 已绑定目录："
                )
                message = (
                    f"{action_text}\n{bound_path}\n"
                    f"当前名称：{_session_title_from_path(bound_path)}\n"
                    f"持久终端：tmux `{tmux_session_name}`"
                )
            except Exception as exc:
                self.logger.exception("初始化 tmux 会话失败")
                action_text = (
                    f"已自动创建会话 #{target_session.id} 并绑定目录："
                    if created_new_session
                    else f"会话 #{target_session.id} 已绑定目录："
                )
                message = (
                    f"{action_text}\n{bound_path}\n"
                    f"当前名称：{_session_title_from_path(bound_path)}\n"
                    f"但持久 tmux 终端初始化失败：{exc}"
                )
            self._safe_send_message(incoming.chat_id, message, incoming.message_thread_id)
            return

        if command == "/tpwd":
            active_session = self.storage.get_active_session(
                incoming.user_id,
                incoming.chat_id,
                incoming.message_thread_id,
            )
            if active_session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前没有活跃会话，请先用 /tnew 或 /tuse。",
                    incoming.message_thread_id,
                )
                return
            path_text = active_session.bound_path or "当前会话还没有绑定目录。"
            self._safe_send_message(
                incoming.chat_id,
                f"当前会话：#{active_session.id}\n目录：{path_text}",
                incoming.message_thread_id,
            )
            return

        if command == "/tstop":
            active_session = self.storage.get_active_session(
                incoming.user_id,
                incoming.chat_id,
                incoming.message_thread_id,
            )
            if active_session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前没有活跃会话，请先用 /tnew 或 /tuse。",
                    incoming.message_thread_id,
                )
                return
            if self._stop_session_run(active_session.id):
                self._safe_send_message(
                    incoming.chat_id,
                    f"已向会话 #{active_session.id} 的当前任务发送停止信号。",
                    incoming.message_thread_id,
                )
            else:
                self._safe_send_message(
                    incoming.chat_id,
                    f"会话 #{active_session.id} 当前没有运行中的任务。",
                    incoming.message_thread_id,
                )
            return

        self._safe_send_message(
            incoming.chat_id,
            f"未知命令：{command}\n\n{HELP_TEXT}",
            incoming.message_thread_id,
        )

    def _handle_codex_command(self, incoming: IncomingMessage) -> None:
        command_text = incoming.text.split()[0]
        command = self._extract_command(incoming.text)
        args = incoming.text[len(command_text) :].strip()

        handler_map = {
            "/new": self._handle_codex_new_command,
            "/clear": self._handle_codex_clear_command,
            "/resume": self._handle_codex_resume_command,
            "/fork": self._handle_codex_fork_command,
            "/rename": self._handle_codex_rename_command,
            "/init": self._handle_codex_init_command,
            "/review": self._handle_codex_review_command,
            "/model": self._handle_codex_model_command,
            "/fast": self._handle_codex_fast_command,
            "/personality": self._handle_codex_personality_command,
            "/approvals": self._handle_codex_approvals_command,
            "/permissions": self._handle_codex_permissions_command,
            "/plan": self._handle_codex_plan_command,
            "/collab": self._handle_codex_collab_command,
            "/status": self._handle_codex_status_command,
            "/debug-config": self._handle_codex_debug_config_command,
            "/mcp": self._handle_codex_mcp_command,
            "/apps": self._handle_codex_apps_command,
            "/skills": self._handle_codex_skills_command,
            "/experimental": self._handle_codex_experimental_command,
            "/diff": self._handle_codex_diff_command,
            "/rollout": self._handle_codex_rollout_command,
            "/copy": self._handle_codex_copy_command,
            "/compact": self._handle_codex_compact_command,
            "/clean": self._handle_codex_clean_command,
        }
        handler = handler_map.get(command)
        if handler is not None:
            handler(incoming, args)
            return
        self._handle_unsupported_codex_command(incoming, command)

    def _handle_codex_new_command(self, incoming: IncomingMessage, args: str = "") -> None:
        session = self.storage.get_active_session(
            incoming.user_id,
            incoming.chat_id,
            incoming.message_thread_id,
        )
        if session is None:
            self._safe_send_message(
                incoming.chat_id,
                "当前没有活跃会话，请先用 /tnew 创建，或用 /tuse 切换。",
                incoming.message_thread_id,
            )
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，/new 暂时不可用，请稍后或先 /tstop。",
                incoming.message_thread_id,
            )
            return

        self._reset_session_thread(session.id)
        suffix = (
            f"目录保持不变：{session.bound_path}"
            if session.bound_path
            else "当前会话还没有绑定目录，请先用 /tbind <绝对路径>。"
        )
        self._safe_send_message(
            incoming.chat_id,
            f"已在会话 #{session.id} 中开启新的 Codex 对话。\n{suffix}",
            incoming.message_thread_id,
        )

    def _handle_codex_clear_command(self, incoming: IncomingMessage, args: str = "") -> None:
        session = self._get_active_session_or_notify(incoming)
        if session is None:
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，/clear 暂时不可用，请稍后或先 /tstop。",
                incoming.message_thread_id,
            )
            return
        self._reset_session_thread(session.id)
        self._safe_send_message(
            incoming.chat_id,
            f"会话 #{session.id} 已清空当前 Codex 对话。\nTelegram 里的历史消息不会删除，下一条消息会从新对话开始。",
            incoming.message_thread_id,
        )

    def _handle_codex_resume_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        cwd = Path(session.bound_path)
        threads = self.runner.list_threads(cwd, limit=12)
        if not args:
            if not threads:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前目录下没有可恢复的 Codex 线程。",
                    incoming.message_thread_id,
                )
                return
            lines = ["当前目录最近的 Codex 线程："]
            for index, thread in enumerate(threads, start=1):
                name = f" [{thread.name}]" if thread.name else ""
                preview = thread.preview or "无预览"
                lines.append(f"{index}. {thread.thread_id}{name}\n{preview}")
            lines.append("\n用法：/resume <编号或thread_id>")
            self._send_long_message(
                incoming.chat_id,
                "\n\n".join(lines),
                incoming.message_thread_id,
            )
            return
        thread = self._resolve_thread_reference(args, threads)
        if thread is None:
            self._safe_send_message(
                incoming.chat_id,
                "没有找到对应的线程。先直接 `/resume` 查看列表，再用编号或完整 thread_id 恢复。",
                incoming.message_thread_id,
                parse_mode="HTML",
            )
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，/resume 暂时不可用，请稍后或先 /tstop。",
                incoming.message_thread_id,
            )
            return
        self.storage.update_session_thread_id(session.id, thread.thread_id)
        self.storage.update_session_status(session.id, "idle")
        title_text = f"\n名称：{thread.name}" if thread.name else ""
        self._safe_send_message(
            incoming.chat_id,
            f"会话 #{session.id} 已恢复到 Codex 线程：{thread.thread_id}{title_text}",
            incoming.message_thread_id,
        )

    def _handle_codex_fork_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，/fork 暂时不可用，请稍后或先 /tstop。",
                incoming.message_thread_id,
            )
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有可 fork 的 Codex 线程，先发起一次对话再试。",
                incoming.message_thread_id,
            )
            return
        forked = self.runner.fork_thread(
            Path(session.bound_path),
            session.codex_thread_id,
            session.codex_settings,
        )
        thread = forked.get("thread") if isinstance(forked, dict) else {}
        new_thread_id = str(thread.get("id") or "").strip()
        if not new_thread_id:
            raise RuntimeError("fork 后未返回新的 thread_id")
        self.storage.update_session_thread_id(session.id, new_thread_id)
        self._safe_send_message(
            incoming.chat_id,
            f"会话 #{session.id} 已 fork 到新的 Codex 线程：{new_thread_id}",
            incoming.message_thread_id,
        )

    def _handle_codex_rename_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有活动的 Codex 线程，先发起一次对话再试。",
                incoming.message_thread_id,
            )
            return
        name = args.strip()
        if not name:
            self._safe_send_message(
                incoming.chat_id,
                "用法：/rename <新标题>",
                incoming.message_thread_id,
            )
            return
        self.runner.set_thread_name(Path(session.bound_path), session.codex_thread_id, name)
        self._safe_send_message(
            incoming.chat_id,
            f"当前 Codex 线程已重命名为：{name}",
            incoming.message_thread_id,
        )

    def _handle_codex_init_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        init_target = Path(session.bound_path) / "AGENTS.md"
        if init_target.exists():
            self._safe_send_message(
                incoming.chat_id,
                "当前目录已经存在 AGENTS.md，Codex 原生 /init 也会跳过覆盖。",
                incoming.message_thread_id,
            )
            return
        prompt = (
            "create an AGENTS.md file with instructions for Codex in the current project root. "
            "keep it concise and practical."
        )
        forwarded = IncomingMessage(
            chat_id=incoming.chat_id,
            user_id=incoming.user_id,
            text=prompt,
            message_id=incoming.message_id,
            message_thread_id=incoming.message_thread_id,
        )
        self._handle_prompt(forwarded)

    def _handle_codex_review_command(self, incoming: IncomingMessage, args: str) -> None:
        prompt = args.strip() or "review my current changes and find issues"
        forwarded = IncomingMessage(
            chat_id=incoming.chat_id,
            user_id=incoming.user_id,
            text=prompt,
            message_id=incoming.message_id,
            message_thread_id=incoming.message_thread_id,
        )
        self._handle_prompt(forwarded)

    def _handle_codex_model_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        cwd = Path(session.bound_path)
        raw = args.strip()
        if not raw or raw == "show":
            self._safe_send_message(
                incoming.chat_id,
                self._format_model_status(session),
                incoming.message_thread_id,
            )
            return
        if raw == "list":
            models = self.runner.list_models(cwd)
            lines = ["可用模型："]
            for item in models:
                if not isinstance(item, dict):
                    continue
                model = str(item.get("model") or "").strip()
                if not model:
                    continue
                efforts = item.get("supportedReasoningEfforts") or []
                effort_labels = [
                    str(option.get("reasoningEffort") or "").strip()
                    for option in efforts
                    if isinstance(option, dict) and str(option.get("reasoningEffort") or "").strip()
                ]
                suffix = f" | effort: {', '.join(effort_labels)}" if effort_labels else ""
                lines.append(f"- {model}{suffix}")
            lines.append("\n用法：/model <model> [effort]")
            self._send_long_message(
                incoming.chat_id,
                "\n".join(lines),
                incoming.message_thread_id,
            )
            return
        parts = raw.split()
        model = parts[0].strip()
        effort = parts[1].strip().lower() if len(parts) > 1 else None
        if effort and effort not in _REASONING_EFFORT_VALUES:
            self._safe_send_message(
                incoming.chat_id,
                f"不支持的 reasoning effort：{effort}\n可选值：{', '.join(_REASONING_EFFORT_VALUES)}",
                incoming.message_thread_id,
            )
            return
        updates = {
            "model": None if model == "default" else model,
            "reasoning_effort": None if effort in {None, "default"} else effort,
        }
        self._apply_session_codex_settings(
            incoming,
            session,
            updates,
            self._format_model_status_message(model, effort),
        )

    def _handle_codex_fast_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        current = str(session.codex_settings.get("service_tier") or "").strip().lower() == "fast"
        action = args.strip().lower()
        if action in {"", "toggle"}:
            enabled = not current
        elif action == "status":
            self._safe_send_message(
                incoming.chat_id,
                f"Fast 模式当前为：{'on' if current else 'off'}",
                incoming.message_thread_id,
            )
            return
        elif action == "on":
            enabled = True
        elif action == "off":
            enabled = False
        else:
            self._safe_send_message(
                incoming.chat_id,
                "用法：/fast [on|off|status]",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"service_tier": "fast" if enabled else None},
            f"Fast 模式已设为：{'on' if enabled else 'off'}",
        )

    def _handle_codex_personality_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        value = args.strip().lower()
        if not value or value == "show":
            current = str(session.codex_settings.get("personality") or "default")
            self._safe_send_message(
                incoming.chat_id,
                f"当前 personality：{current}\n可选值：default, {', '.join(_PERSONALITY_VALUES)}",
                incoming.message_thread_id,
            )
            return
        if value == "default":
            normalized = None
        elif value in _PERSONALITY_VALUES:
            normalized = value
        else:
            self._safe_send_message(
                incoming.chat_id,
                f"不支持的 personality：{value}\n可选值：default, {', '.join(_PERSONALITY_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"personality": normalized},
            f"Personality 已更新为：{normalized or 'default'}",
        )

    def _handle_codex_approvals_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        value = args.strip().lower()
        if not value or value == "show":
            current = str(session.codex_settings.get("approval_policy") or "default")
            self._safe_send_message(
                incoming.chat_id,
                f"当前 approval policy：{current}\n可选值：default, {', '.join(_APPROVAL_POLICY_VALUES)}",
                incoming.message_thread_id,
            )
            return
        if value == "default":
            normalized = None
        elif value in _APPROVAL_POLICY_VALUES:
            normalized = value
        else:
            self._safe_send_message(
                incoming.chat_id,
                f"不支持的 approval policy：{value}\n可选值：default, {', '.join(_APPROVAL_POLICY_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"approval_policy": normalized},
            f"Approval policy 已更新为：{normalized or 'default'}",
        )

    def _handle_codex_permissions_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        value = args.strip().lower()
        if not value or value == "show":
            current = str(session.codex_settings.get("sandbox_mode") or "default")
            self._safe_send_message(
                incoming.chat_id,
                f"当前 sandbox mode：{current}\n可选值：default, {', '.join(_SANDBOX_MODE_VALUES)}",
                incoming.message_thread_id,
            )
            return
        if value == "default":
            normalized = None
        elif value in _SANDBOX_MODE_VALUES:
            normalized = value
        else:
            self._safe_send_message(
                incoming.chat_id,
                f"不支持的 sandbox mode：{value}\n可选值：default, {', '.join(_SANDBOX_MODE_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"sandbox_mode": normalized},
            f"Sandbox mode 已更新为：{normalized or 'default'}",
        )

    def _handle_codex_plan_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        value = args.strip().lower()
        if value in {"", "on", "plan"}:
            normalized = "plan"
        elif value in {"off", "default"}:
            normalized = "default"
        else:
            self._safe_send_message(
                incoming.chat_id,
                "用法：/plan [on|off]",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"collaboration_mode": normalized},
            f"Collaboration mode 已更新为：{normalized}",
        )

    def _handle_codex_collab_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        cwd = Path(session.bound_path)
        value = args.strip().lower()
        if not value or value == "show":
            current = str(session.codex_settings.get("collaboration_mode") or "default")
            self._safe_send_message(
                incoming.chat_id,
                f"当前 collaboration mode：{current}\n用法：/collab list 或 /collab <default|plan>",
                incoming.message_thread_id,
            )
            return
        if value == "list":
            modes = self.runner.list_collaboration_modes(cwd)
            available = [
                str(item.get("name") or "").strip()
                for item in modes
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            text = "可用 collaboration mode：\n" + "\n".join(f"- {item}" for item in available)
            self._safe_send_message(incoming.chat_id, text, incoming.message_thread_id)
            return
        if value not in _COLLABORATION_MODE_VALUES:
            self._safe_send_message(
                incoming.chat_id,
                f"不支持的 collaboration mode：{value}\n可选值：{', '.join(_COLLABORATION_MODE_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"collaboration_mode": value},
            f"Collaboration mode 已更新为：{value}",
        )

    def _handle_codex_status_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._get_active_session_or_notify(incoming)
        if session is None:
            return
        lines = [
            f"会话 #{session.id}",
            f"目录：{session.bound_path or '未绑定目录'}",
            f"线程：{session.codex_thread_id or '未创建'}",
            f"状态：{session.status}",
            f"模型：{session.codex_settings.get('model') or self.config.codex_model or 'default'}",
            f"effort：{session.codex_settings.get('reasoning_effort') or 'default'}",
            f"Fast：{'on' if session.codex_settings.get('service_tier') == 'fast' else 'off'}",
            f"Personality：{session.codex_settings.get('personality') or 'default'}",
            f"Approval：{session.codex_settings.get('approval_policy') or 'default'}",
            f"Sandbox：{session.codex_settings.get('sandbox_mode') or 'default'}",
            f"Collab：{session.codex_settings.get('collaboration_mode') or 'default'}",
        ]
        self._safe_send_message(incoming.chat_id, "\n".join(lines), incoming.message_thread_id)

    def _handle_codex_debug_config_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        config = self.runner.read_config(Path(session.bound_path))
        effective = config.get("config") if isinstance(config, dict) else {}
        layers = config.get("layers") if isinstance(config, dict) else []
        lines = ["Codex 配置摘要："]
        if isinstance(effective, dict):
            for key in (
                "model",
                "model_reasoning_effort",
                "service_tier",
                "approval_policy",
                "sandbox_mode",
                "web_search",
                "profile",
            ):
                if key in effective:
                    lines.append(f"- {key}: {effective.get(key)}")
        if isinstance(layers, list) and layers:
            lines.append("")
            lines.append("配置层：")
            for layer in layers:
                if not isinstance(layer, dict):
                    continue
                lines.append(f"- {layer.get('name')}: v{layer.get('version')}")
        self._send_long_message(
            incoming.chat_id,
            "\n".join(lines),
            incoming.message_thread_id,
        )

    def _handle_codex_mcp_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        servers = self.runner.list_mcp_servers(Path(session.bound_path))
        if not servers:
            self._safe_send_message(incoming.chat_id, "当前没有 MCP 服务。", incoming.message_thread_id)
            return
        lines = ["MCP 服务："]
        for server in servers:
            if not isinstance(server, dict):
                continue
            name = str(server.get("name") or "").strip() or "unknown"
            tools = server.get("tools") or {}
            resources = server.get("resources") or []
            lines.append(f"- {name} | tools={len(tools)} | resources={len(resources)}")
        self._send_long_message(incoming.chat_id, "\n".join(lines), incoming.message_thread_id)

    def _handle_codex_apps_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        apps = self.runner.list_apps(Path(session.bound_path), session.codex_thread_id)
        if not apps:
            self._safe_send_message(incoming.chat_id, "当前没有可见 Apps。", incoming.message_thread_id)
            return
        lines = ["Apps："]
        for app in apps:
            if not isinstance(app, dict):
                continue
            name = str(app.get("name") or "").strip()
            if not name:
                continue
            description = str(app.get("description") or "").strip()
            enabled = "enabled" if app.get("isEnabled", False) else "disabled"
            lines.append(f"- {name} [{enabled}] {description}".strip())
        self._send_long_message(incoming.chat_id, "\n".join(lines), incoming.message_thread_id)

    def _handle_codex_skills_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        skills = self.runner.list_skills(Path(session.bound_path))
        if not skills:
            self._safe_send_message(incoming.chat_id, "当前目录没有检测到 Skills。", incoming.message_thread_id)
            return
        lines = ["Skills："]
        for item in skills:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
            if name:
                lines.append(f"- {name} {description}".strip())
        self._send_long_message(incoming.chat_id, "\n".join(lines), incoming.message_thread_id)

    def _handle_codex_experimental_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        features = self.runner.list_experimental_features(Path(session.bound_path))
        if not features:
            self._safe_send_message(incoming.chat_id, "当前没有实验特性列表。", incoming.message_thread_id)
            return
        lines = ["Experimental Features："]
        for item in features:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            stage = str(item.get("stage") or "").strip()
            enabled = "on" if item.get("enabled") else "off"
            lines.append(f"- {name} [{stage}] {enabled}")
        self._send_long_message(incoming.chat_id, "\n".join(lines), incoming.message_thread_id)

    def _handle_codex_diff_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        cwd = Path(session.bound_path)
        result = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--stat", "--", "."],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--", "."],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and detail.returncode != 0:
            self._safe_send_message(
                incoming.chat_id,
                "当前目录不是 Git 仓库，或无法计算 diff。",
                incoming.message_thread_id,
            )
            return
        text = (result.stdout.strip() + "\n\n" + detail.stdout.strip()).strip() or "当前没有改动。"
        self._send_long_message(incoming.chat_id, text, incoming.message_thread_id)

    def _handle_codex_rollout_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有活动的 Codex 线程。",
                incoming.message_thread_id,
            )
            return
        payload = self.runner.read_thread(Path(session.bound_path), session.codex_thread_id)
        thread = payload.get("thread") if isinstance(payload, dict) else {}
        path_text = str(thread.get("path") or "").strip()
        self._safe_send_message(
            incoming.chat_id,
            path_text or "当前线程还没有 rollout 路径。",
            incoming.message_thread_id,
        )

    def _handle_codex_copy_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._get_active_session_or_notify(incoming)
        if session is None:
            return
        excerpt = self.storage.get_last_completed_run_excerpt(session.id)
        if not excerpt:
            self._safe_send_message(
                incoming.chat_id,
                "当前还没有可复制的最终回复。",
                incoming.message_thread_id,
            )
            return
        self._send_long_message(incoming.chat_id, excerpt, incoming.message_thread_id, prefer_html=True)

    def _handle_codex_compact_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有可 compact 的 Codex 线程。",
                incoming.message_thread_id,
            )
            return
        self.runner.compact_thread(Path(session.bound_path), session.codex_thread_id)
        self._safe_send_message(
            incoming.chat_id,
            "已触发当前 Codex 线程的 compact。",
            incoming.message_thread_id,
        )

    def _handle_codex_clean_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有活动的 Codex 线程。",
                incoming.message_thread_id,
            )
            return
        self.runner.clean_background_terminals(Path(session.bound_path), session.codex_thread_id)
        self._safe_send_message(
            incoming.chat_id,
            "已请求清理当前线程的后台终端。",
            incoming.message_thread_id,
        )

    def _handle_unsupported_codex_command(self, incoming: IncomingMessage, command: str) -> None:
        self._safe_send_message(
            incoming.chat_id,
            (
                f"{command} 已被识别为 Codex 内建命令，但当前 Telegram 桥接还没有对应的无弹窗实现。\n"
                "它不会再被当成普通文本发给模型。"
            ),
            incoming.message_thread_id,
        )

    def _get_active_session_or_notify(self, incoming: IncomingMessage) -> SessionRecord | None:
        session = self.storage.get_active_session(
            incoming.user_id,
            incoming.chat_id,
            incoming.message_thread_id,
        )
        if session is None:
            self._safe_send_message(
                incoming.chat_id,
                "当前没有活跃会话，请先用 /tnew 创建，或用 /tuse 切换。",
                incoming.message_thread_id,
            )
        return session

    def _require_bound_session_or_notify(self, incoming: IncomingMessage) -> SessionRecord | None:
        session = self._get_active_session_or_notify(incoming)
        if session is None:
            return None
        if not session.bound_path:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有绑定目录，请先用 /tbind <绝对路径>。",
                incoming.message_thread_id,
            )
            return None
        return session

    def _reset_session_thread(self, session_id: int) -> None:
        self.storage.clear_session_thread_id(session_id)

    def _apply_session_codex_settings(
        self,
        incoming: IncomingMessage,
        session: SessionRecord,
        updates: dict[str, object | None],
        success_message: str,
    ) -> None:
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，请稍后或先 /tstop 后再改 Codex 设置。",
                incoming.message_thread_id,
            )
            return
        self.storage.update_session_codex_settings(session.id, dict(updates))
        self._reset_session_thread(session.id)
        self._safe_send_message(
            incoming.chat_id,
            f"{success_message}\n已为当前会话重置 Codex 线程，下一条消息会按新设置生效。",
            incoming.message_thread_id,
        )

    def _resolve_thread_reference(
        self,
        raw: str,
        threads: list,
    ) -> object | None:
        value = raw.strip()
        if not value:
            return None
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(threads):
                return threads[index]
        for thread in threads:
            if getattr(thread, "thread_id", "") == value:
                return thread
        return None

    def _format_model_status(self, session: SessionRecord) -> str:
        return (
            f"当前模型：{session.codex_settings.get('model') or self.config.codex_model or 'default'}\n"
            f"当前 effort：{session.codex_settings.get('reasoning_effort') or 'default'}\n"
            "用法：/model list 或 /model <model> [effort]"
        )

    def _format_model_status_message(self, model: str, effort: str | None) -> str:
        target_model = "default" if model == "default" else model
        target_effort = "default" if effort in {None, 'default'} else effort
        return f"模型已更新为：{target_model}\nReasoning effort：{target_effort}"

    def _handle_prompt(self, incoming: IncomingMessage) -> None:
        session = self.storage.get_active_session(
            incoming.user_id,
            incoming.chat_id,
            incoming.message_thread_id,
        )
        if session is None:
            self._safe_send_message(
                incoming.chat_id,
                "当前没有活跃会话，请先用 /tnew 创建，或用 /tuse 切换。",
                incoming.message_thread_id,
            )
            return
        if not session.bound_path:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有绑定目录，请先用 /tbind <绝对路径>。",
                incoming.message_thread_id,
            )
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，请稍后或先 /tstop。",
                incoming.message_thread_id,
            )
            return

        preview_state = LivePreviewState()
        preview = self._safe_send_message(
            incoming.chat_id,
            preview_state.render(),
            incoming.message_thread_id,
        )

        run_id = self.storage.create_run(
            session_id=session.id,
            user_id=incoming.user_id,
            prompt=incoming.text,
            preview_chat_id=preview.chat_id if preview else incoming.chat_id,
            preview_message_id=preview.message_id if preview else None,
        )
        active_run = ActiveRun(
            run_id=run_id,
            session_id=session.id,
            user_id=incoming.user_id,
            chat_id=incoming.chat_id,
            message_thread_id=incoming.message_thread_id,
            prompt=incoming.text,
            preview_message_id=preview.message_id if preview else None,
        )
        with self._active_runs_lock:
            self._active_runs[session.id] = active_run
        self.storage.update_session_status(session.id, "running")

        worker = threading.Thread(
            target=self._execute_run,
            args=(session, active_run, preview_state),
            daemon=True,
        )
        worker.start()

    def _execute_run(
        self,
        session: SessionRecord,
        active_run: ActiveRun,
        preview_state: LivePreviewState,
    ) -> None:
        final_message: str | None = None
        preview_stop_event = threading.Event()
        preview_worker = threading.Thread(
            target=self._run_preview_loop,
            args=(active_run, preview_state, preview_stop_event),
            daemon=True,
        )
        preview_worker.start()
        try:
            if session.bound_path is None:
                raise RuntimeError("会话未绑定目录")

            handle = self.runner.start(
                prompt=active_run.prompt,
                cwd=Path(session.bound_path),
                thread_id=session.codex_thread_id,
                runtime_dir=self.config.state_dir / "runtime",
                session_id=session.id,
                settings=session.codex_settings,
            )
            with self._active_runs_lock:
                current = self._active_runs.get(session.id)
                if current is not None:
                    current.process_handle = handle
                    if current.stop_requested:
                        self.runner.terminate(handle)

            def _handle_event_line(line: str) -> None:
                nonlocal final_message
                parsed = self.runner.parse_event_line(line)
                if parsed.thread_id:
                    self.storage.update_session_thread_id(session.id, parsed.thread_id)
                if parsed.final_message:
                    final_message = parsed.final_message
                if parsed.commentary_id and parsed.commentary_text:
                    preview_state.update_commentary(
                        parsed.commentary_id,
                        parsed.commentary_text,
                    )
                if parsed.tool_call_id or parsed.tool_command_text or parsed.tool_output_text:
                    preview_state.update_tool_state(
                        parsed.tool_call_id,
                        command_text=parsed.tool_command_text,
                        output_text=parsed.tool_output_text,
                    )
                if parsed.footer_statusline:
                    preview_state.update_footer_statusline(parsed.footer_statusline)
                if parsed.preview_text:
                    preview_state.update_stream_text(parsed.preview_text)
                if parsed.status_text:
                    preview_state.update_status(parsed.status_text)

            status = self.runner.wait(handle, _handle_event_line)
            if status.exit_code != 0:
                if active_run.stop_requested:
                    raise InterruptedError("任务已停止")
                event_tail = self.runner.tail_event_log(handle.event_log_file) or "无事件日志"
                raise RuntimeError(
                    f"Codex 退出码异常：{status.exit_code}\n最近事件：\n{event_tail}"
                )

            if not final_message:
                final_message = self.runner.read_output_file(handle.output_file)

            if not final_message:
                final_message = "已完成，但没有捕获到最终回复。"

            preview_state.update_stream_text(final_message)
            preview_state.update_status("Thinking")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._drain_preview_stream(active_run, preview_state)
            self._send_run_result(active_run, final_message, preview_state)
            self.storage.finish_run(
                active_run.run_id,
                status="completed",
                final_excerpt=final_message[:500],
            )
            self.storage.update_session_status(session.id, "idle")
        except InterruptedError:
            preview_state.finish("Stopped")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._render_finished_preview(active_run, preview_state)
            self._safe_send_message(
                active_run.chat_id,
                f"会话 #{session.id} 的任务已停止。",
                active_run.message_thread_id,
            )
            self.storage.finish_run(
                active_run.run_id,
                status="stopped",
                error_message="用户主动停止",
            )
            self.storage.update_session_status(session.id, "idle")
        except Exception as exc:
            self.logger.exception("执行会话 #%s 失败", session.id)
            preview_state.finish("Failed")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._render_finished_preview(active_run, preview_state)
            self._safe_send_message(
                active_run.chat_id,
                f"会话 #{session.id} 执行失败：{exc}",
                active_run.message_thread_id,
            )
            self.storage.finish_run(
                active_run.run_id,
                status="failed",
                error_message=str(exc),
            )
            self.storage.update_session_status(session.id, "error")
        finally:
            self._stop_preview_loop(preview_stop_event, preview_worker)
            if active_run.process_handle is not None:
                try:
                    active_run.process_handle.output_file.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("清理 Codex 输出文件失败：%s", active_run.process_handle.output_file)
                try:
                    active_run.process_handle.event_log_file.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("清理 Codex 事件日志失败：%s", active_run.process_handle.event_log_file)
                try:
                    active_run.process_handle.status_file.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("清理 Codex 状态文件失败：%s", active_run.process_handle.status_file)
                try:
                    active_run.process_handle.prompt_file.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("清理 Codex 提示词文件失败：%s", active_run.process_handle.prompt_file)
            with self._active_runs_lock:
                self._active_runs.pop(session.id, None)

    def _is_session_running(self, session_id: int) -> bool:
        with self._active_runs_lock:
            return session_id in self._active_runs

    def _stop_session_run(self, session_id: int) -> bool:
        with self._active_runs_lock:
            active_run = self._active_runs.get(session_id)
            if active_run is None:
                return False
            active_run.stop_requested = True
            handle = active_run.process_handle
        if handle is not None:
            self.runner.terminate(handle)
        return True

    def _run_preview_loop(
        self,
        active_run: ActiveRun,
        preview_state: LivePreviewState,
        stop_event: threading.Event,
    ) -> None:
        last_preview_text = ""
        last_typing_at = 0.0
        heartbeat_interval = _normalize_preview_interval(
            self.config.preview_update_interval_seconds
        )
        heartbeat_step_seconds = max(1, int(round(heartbeat_interval)))
        animation_interval = _PREVIEW_ANIMATION_INTERVAL_SECONDS
        now = time.monotonic()
        next_heartbeat_at = now + heartbeat_interval
        next_animation_at = now + animation_interval
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_typing_at >= _PREVIEW_TYPING_INTERVAL_SECONDS:
                self._safe_send_chat_action(
                    active_run.chat_id,
                    "typing",
                    active_run.message_thread_id,
                )
                last_typing_at = now

            now = time.monotonic()
            has_pending_stream = preview_state.has_pending_stream()
            animation_due = next_animation_at <= now
            heartbeat_due = next_heartbeat_at <= now
            if not has_pending_stream and not animation_due and not heartbeat_due:
                wait_seconds = min(
                    _PREVIEW_LOOP_IDLE_SECONDS,
                    max(0.0, min(next_animation_at, next_heartbeat_at) - now),
                )
                if stop_event.wait(wait_seconds):
                    break
                continue

            animation_ticks = 0
            while next_animation_at <= now:
                animation_ticks += 1
                next_animation_at += animation_interval
            heartbeat_ticks = 0
            while next_heartbeat_at <= now:
                heartbeat_ticks += 1
                next_heartbeat_at += heartbeat_interval

            if has_pending_stream or animation_ticks > 0 or heartbeat_ticks > 0:
                animate_steps = 0 if has_pending_stream else animation_ticks
                text = preview_state.advance(
                    animate_steps=animate_steps,
                    elapsed_seconds=heartbeat_step_seconds * heartbeat_ticks,
                )
                preview_synced = text == last_preview_text
                if text and text != last_preview_text:
                    preview_synced = self._update_preview(
                        active_run,
                        text,
                        prefer_html=False,
                    )
                    if preview_synced:
                        last_preview_text = text
                if has_pending_stream and preview_synced:
                    preview_state.mark_rendered()
                    next_animation_at = time.monotonic() + animation_interval

    def _stop_preview_loop(
        self,
        stop_event: threading.Event,
        worker: threading.Thread,
    ) -> None:
        stop_event.set()
        if worker.is_alive():
            worker.join(timeout=1.5)

    def _drain_preview_stream(
        self,
        active_run: ActiveRun,
        preview_state: LivePreviewState,
    ) -> None:
        deadline = time.monotonic() + _PREVIEW_DRAIN_TIMEOUT_SECONDS
        while preview_state.has_pending_stream() and time.monotonic() < deadline:
            text = preview_state.advance(animate_steps=0, elapsed_seconds=0)
            if not text:
                preview_state.mark_rendered()
                break
            if self._update_preview(active_run, text, prefer_html=False):
                preview_state.mark_rendered()
            time.sleep(_PREVIEW_LOOP_IDLE_SECONDS)

    def _update_preview(
        self,
        active_run: ActiveRun,
        text: str,
        prefer_html: bool = False,
    ) -> bool:
        if prefer_html:
            rendered_html = markdown_to_telegram_html(text)
            if rendered_html and self._edit_preview_message(
                active_run,
                rendered_html,
                parse_mode="HTML",
            ):
                return True
            return self._edit_preview_message(active_run, text)
        return self._edit_preview_message(active_run, text)

    def _edit_preview_message(
        self,
        active_run: ActiveRun,
        text: str,
        parse_mode: str | None = None,
    ) -> bool:
        if active_run.preview_message_id is None:
            return False
        try:
            self.telegram.edit_message_text(
                chat_id=active_run.chat_id,
                message_id=active_run.preview_message_id,
                text=text,
                message_thread_id=active_run.message_thread_id,
                parse_mode=parse_mode,
            )
            return True
        except TelegramApiError as exc:
            if is_message_not_modified_error(exc):
                return True
            self.logger.exception("更新预览消息失败")
            return False

    def _render_finished_preview(
        self,
        active_run: ActiveRun,
        preview_state: LivePreviewState,
    ) -> bool:
        rendered_html = preview_state.render_final_html()
        if self._edit_preview_message(
            active_run,
            rendered_html,
            parse_mode="HTML",
        ):
            return True
        return self._edit_preview_message(active_run, preview_state.render())

    def _send_run_result(
        self,
        active_run: ActiveRun,
        text: str,
        preview_state: LivePreviewState | None = None,
    ) -> None:
        preview_updated = False
        if preview_state is not None:
            preview_state.update_stream_text(text)
            preview_state.complete()
            if self._render_finished_preview(active_run, preview_state):
                preview_updated = True
        if not preview_updated:
            inline_text, parse_mode = self._build_inline_result(text)
            if self._edit_preview_message(active_run, inline_text, parse_mode=parse_mode):
                preview_updated = True
            else:
                self._safe_send_message(
                    active_run.chat_id,
                    inline_text,
                    active_run.message_thread_id,
                    parse_mode=parse_mode,
                )
                return
        self._send_completion_notice(active_run)

    def _send_completion_notice(self, active_run: ActiveRun) -> None:
        self._safe_send_message(
            active_run.chat_id,
            "已完成",
            active_run.message_thread_id,
        )

    def _build_inline_result(self, text: str) -> tuple[str, str | None]:
        plain_limit = 3400
        cleaned = text.strip()
        html_text = markdown_to_telegram_html(cleaned)
        if html_text and len(html_text) <= 3500:
            return html_text, "HTML"
        plain_text = f"Completed\n\n{cleaned}" if cleaned else "Completed"
        if len(plain_text) <= plain_limit:
            return plain_text, None
        suffix = "\n\n[Truncated for length]"
        truncated = plain_text[: plain_limit - len(suffix) - 3].rstrip() + "..." + suffix
        return truncated, None

    def _sync_bot_commands(self) -> None:
        try:
            self.telegram.set_my_commands(_BOT_COMMANDS)
        except TelegramApiError:
            self.logger.warning("同步 Telegram slash 命令失败", exc_info=True)

    def _safe_send_chat_action(
        self,
        chat_id: int,
        action: str,
        message_thread_id: int | None,
    ) -> None:
        try:
            self.telegram.send_chat_action(
                chat_id=chat_id,
                action=action,
                message_thread_id=message_thread_id,
            )
        except TelegramApiError:
            self.logger.debug("发送 Telegram chat action 失败", exc_info=True)

    def _safe_send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> TelegramMessage | None:
        try:
            return self.telegram.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode,
            )
        except TelegramApiError:
            self.logger.exception("发送 Telegram 消息失败")
            return None

    def _send_long_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None,
        reply_to_message_id: int | None = None,
        prefer_html: bool = False,
    ) -> None:
        parts = (
            split_markdown_message(text, 3500)
            if prefer_html
            else self._split_message(text, 3500)
        )
        for index, part in enumerate(parts):
            current_reply_to = reply_to_message_id if index == 0 else None
            if prefer_html:
                html_text = markdown_to_telegram_html(part)
                sent = self._safe_send_message(
                    chat_id,
                    html_text,
                    message_thread_id,
                    reply_to_message_id=current_reply_to,
                    parse_mode="HTML",
                )
                if sent is not None:
                    continue
            self._safe_send_message(
                chat_id,
                part,
                message_thread_id,
                reply_to_message_id=current_reply_to,
            )

    def _split_message(self, text: str, max_length: int) -> list[str]:
        if len(text) <= max_length:
            return [text]

        parts: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_length:
                parts.append(remaining)
                break
            chunk = remaining[:max_length]
            split_at = chunk.rfind("\n")
            if split_at < max_length // 2:
                split_at = chunk.rfind(" ")
            if split_at < max_length // 2:
                split_at = max_length
            parts.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [part for part in parts if part]
