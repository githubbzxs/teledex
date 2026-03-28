from __future__ import annotations

import html
import logging
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
/tnew [标题] - 新建 teledex 会话
/tsessions - 查看会话列表
/tuse <id> - 切换当前会话
/tbind <绝对路径> - 绑定当前会话目录并启动持久 tmux 终端
/tpwd - 查看当前会话目录
/tstop - 停止当前任务

除以上管理命令外，其他 `/命令` 会直接作为 Codex 原生命令发送到当前会话。
直接发送普通文本，也会继续当前活跃会话。"""

_PREVIEW_TYPING_INTERVAL_SECONDS = 4.0
_PREVIEW_HEARTBEAT_FRAMES = ("○", "●")
_PREVIEW_HISTORY_MAX_CHARS = 2000
_PREVIEW_TOOL_OUTPUT_MAX_CHARS = 2000
_PREVIEW_OUTPUT_MAX_CHARS = 2200
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
        initial_status: str = "Working",
        history_max_chars: int = _PREVIEW_HISTORY_MAX_CHARS,
        output_max_chars: int = _PREVIEW_OUTPUT_MAX_CHARS,
        tool_output_max_chars: int = _PREVIEW_TOOL_OUTPUT_MAX_CHARS,
    ) -> None:
        self._status_text = initial_status.strip() or "Working"
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
            self._commentary_order.clear()
            self._commentary_text_by_id.clear()
            self._tool_order.clear()
            self._tool_command_by_id.clear()
            self._tool_output_by_id.clear()
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
            if (
                not self._commentary_order
                and not self._target_text
                and not self._tool_order
                and self._status_text == "Thinking"
            ):
                self._status_text = "Working"
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
            else _PREVIEW_HEARTBEAT_FRAMES[-1]
        )
        sections = [
            f"{marker} {self._status_text} ({_format_elapsed_compact(self._elapsed_seconds)})"
        ]
        body = self._build_body_locked()
        if body:
            sections.extend(["", body])
        if self._footer_statusline:
            sections.extend(["", self._footer_statusline])
        return "\n".join(sections).strip()

    def _render_final_html_locked(self) -> str:
        marker = (
            _PREVIEW_HEARTBEAT_FRAMES[self._frame_index]
            if self._in_progress
            else _PREVIEW_HEARTBEAT_FRAMES[-1]
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

        tool_blocks = self._render_tool_blocks_locked()
        if tool_blocks:
            sections.append(tool_blocks)

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
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        minutes, secs = divmod(total_seconds, 60)
        return f"{minutes}m {secs:02d}s"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


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
        command_text = text.split()[0]
        command = command_text.split("@", 1)[0].lower()
        return command in {
            "/start",
            "/help",
            "/tnew",
            "/tsessions",
            "/tuse",
            "/tbind",
            "/tpwd",
            "/tstop",
        }

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
        command = command_text.split("@", 1)[0].lower()
        args = incoming.text[len(command_text) :].strip()

        if command in {"/help", "/start"}:
            self._safe_send_message(incoming.chat_id, HELP_TEXT, incoming.message_thread_id)
            return

        if command == "/tnew":
            title = args or f"会话 {len(self.storage.list_sessions(incoming.user_id)) + 1}"
            session = self.storage.create_session(incoming.user_id, title)
            self._safe_send_message(
                incoming.chat_id,
                f"已创建会话 #{session.id}\n标题：{session.title}\n接下来请先用 /tbind 绑定目录。",
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
            active_id = user.active_session_id if user else None
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
            self.storage.set_active_session(incoming.user_id, session_id)
            self._safe_send_message(
                incoming.chat_id,
                f"已切换到会话 #{session.id}\n标题：{session.title}",
                incoming.message_thread_id,
            )
            return

        if command == "/tbind":
            active_session = self.storage.get_active_session(incoming.user_id)
            if active_session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前没有活跃会话，请先用 /tnew 或 /tuse。",
                    incoming.message_thread_id,
                )
                return
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
            self.storage.bind_session_path(
                active_session.id,
                incoming.user_id,
                str(bound_path),
            )
            try:
                self.runner.reset_terminal(active_session.id)
                tmux_session_name = self.runner.ensure_terminal(active_session.id, bound_path)
                message = (
                    f"会话 #{active_session.id} 已绑定目录：\n{bound_path}\n"
                    f"持久终端：tmux `{tmux_session_name}`"
                )
            except Exception as exc:
                self.logger.exception("初始化 tmux 会话失败")
                message = (
                    f"会话 #{active_session.id} 已绑定目录：\n{bound_path}\n"
                    f"但持久 tmux 终端初始化失败：{exc}"
                )
            self._safe_send_message(incoming.chat_id, message, incoming.message_thread_id)
            return

        if command == "/tpwd":
            active_session = self.storage.get_active_session(incoming.user_id)
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
            active_session = self.storage.get_active_session(incoming.user_id)
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

    def _handle_prompt(self, incoming: IncomingMessage) -> None:
        session = self.storage.get_active_session(incoming.user_id)
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
                final_message = "Completed, but no final response was captured."

            preview_state.update_stream_text(final_message)
            preview_state.update_status("Working")
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
        next_heartbeat_at = time.monotonic()
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
            heartbeat_ticks = 0
            while next_heartbeat_at <= now:
                heartbeat_ticks += 1
                next_heartbeat_at += heartbeat_interval

            has_pending_stream = preview_state.has_pending_stream()
            if has_pending_stream or heartbeat_ticks > 0:
                text = preview_state.advance(
                    animate_steps=heartbeat_ticks,
                    elapsed_seconds=heartbeat_step_seconds * heartbeat_ticks,
                )
                if text and text != last_preview_text:
                    self._update_preview(active_run, text, prefer_html=False)
                    last_preview_text = text
                if has_pending_stream:
                    preview_state.mark_rendered()

            if stop_event.wait(_PREVIEW_LOOP_IDLE_SECONDS):
                break

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
            self._update_preview(active_run, text, prefer_html=False)
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
        if preview_state is not None:
            preview_state.update_stream_text(text)
            preview_state.complete()
            if self._render_finished_preview(active_run, preview_state):
                return
        inline_text, parse_mode = self._build_inline_result(text)
        if self._edit_preview_message(active_run, inline_text, parse_mode=parse_mode):
            return
        self._safe_send_message(
            active_run.chat_id,
            inline_text,
            active_run.message_thread_id,
            parse_mode=parse_mode,
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
