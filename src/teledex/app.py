from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .codex_runner import CodexProcessHandle, CodexRunner
from .config import AppConfig
from .formatting import markdown_to_telegram_html, split_markdown_message, strip_citations
from .storage import SessionRecord, Storage
from .telegram_api import (
    TelegramApiError,
    TelegramClient,
    TelegramMessage,
    is_message_not_modified_error,
)


HELP_TEXT = """teledex 可用命令：
/start - 查看帮助
/new [标题] - 新建会话
/sessions - 查看会话列表
/use <id> - 切换当前会话
/bind <绝对路径> - 绑定当前会话目录
/pwd - 查看当前会话目录
/stop - 停止当前任务

直接发送普通文本，即可继续当前活跃会话。"""

_PREVIEW_HEARTBEAT_FRAMES = ("○", "●")
_PREVIEW_HEARTBEAT_INTERVAL_SECONDS = 0.8
_PREVIEW_TYPING_INTERVAL_SECONDS = 4.0
_PREVIEW_STREAM_STEP_CHARS = 1
_PREVIEW_MAX_CHARS = 320
_PREVIEW_STREAM_INTERVAL_SECONDS = 0.04
_PREVIEW_DRAIN_TIMEOUT_SECONDS = 8.0


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
        initial_status: str = "正在准备会话...",
        max_chars: int = _PREVIEW_MAX_CHARS,
        stream_step_chars: int = _PREVIEW_STREAM_STEP_CHARS,
    ) -> None:
        self._status_text = initial_status.strip() or "正在准备会话..."
        self._target_text = ""
        self._visible_chars = 0
        self._frame_index = 0
        self._max_chars = max_chars
        self._stream_step_chars = max(1, stream_step_chars)
        self._in_progress = True
        self._lock = threading.RLock()

    def update_status(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        with self._lock:
            self._status_text = normalized

    def update_stream_text(self, text: str) -> None:
        normalized = strip_citations(text).strip()
        if not normalized:
            return
        with self._lock:
            if normalized != self._target_text:
                if not normalized.startswith(self._target_text):
                    self._visible_chars = min(self._visible_chars, len(normalized))
                self._target_text = normalized
            if self._visible_chars == 0:
                self._visible_chars = min(self._stream_step_chars, len(self._target_text))
            self._status_text = "正在输出..."
            self._in_progress = True

    def advance(self) -> str:
        with self._lock:
            self._frame_index = (self._frame_index + 1) % len(_PREVIEW_HEARTBEAT_FRAMES)
            if self._target_text and self._visible_chars < len(self._target_text):
                self._visible_chars = min(
                    len(self._target_text),
                    self._visible_chars + self._stream_step_chars,
                )
            return self._render_locked()

    def render(self) -> str:
        with self._lock:
            return self._render_locked()

    def has_pending_stream(self) -> bool:
        with self._lock:
            return bool(self._target_text) and self._visible_chars < len(self._target_text)

    def target_text(self) -> str:
        with self._lock:
            return self._target_text

    def complete(self) -> str:
        with self._lock:
            self._visible_chars = len(self._target_text)
            self._status_text = "已完成"
            self._in_progress = False
            return self._render_locked()

    def _render_locked(self) -> str:
        marker = (
            _PREVIEW_HEARTBEAT_FRAMES[self._frame_index]
            if self._in_progress
            else _PREVIEW_HEARTBEAT_FRAMES[-1]
        )
        status_line = f"{marker} {self._status_text}".strip()
        if not self._target_text:
            return status_line

        body = _truncate_preview_text(self._target_text[: self._visible_chars], self._max_chars)
        if not body:
            return status_line
        return f"{status_line}\n\n{body}"


def _truncate_preview_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


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

    def run_forever(self) -> None:
        bot = self.telegram.get_me()
        self.logger.info("Telegram bot 已连接: @%s", bot.get("username", "unknown"))
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

        if incoming.text.startswith("/"):
            self._handle_command(incoming)
            return

        self._handle_prompt(incoming)

    def _handle_command(self, incoming: IncomingMessage) -> None:
        command_text = incoming.text.split()[0]
        command = command_text.split("@", 1)[0].lower()
        args = incoming.text[len(command_text) :].strip()

        if command == "/start":
            self._safe_send_message(incoming.chat_id, HELP_TEXT, incoming.message_thread_id)
            return

        if command == "/new":
            title = args or f"会话 {len(self.storage.list_sessions(incoming.user_id)) + 1}"
            session = self.storage.create_session(incoming.user_id, title)
            self._safe_send_message(
                incoming.chat_id,
                f"已创建会话 #{session.id}\n标题：{session.title}\n接下来请先用 /bind 绑定目录。",
                incoming.message_thread_id,
            )
            return

        if command == "/sessions":
            sessions = self.storage.list_sessions(incoming.user_id)
            user = self.storage.get_user(incoming.user_id)
            if not sessions:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前还没有会话，先用 /new 创建一个。",
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

        if command == "/use":
            if not args:
                self._safe_send_message(
                    incoming.chat_id,
                    "用法：/use <id>",
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

        if command == "/bind":
            active_session = self.storage.get_active_session(incoming.user_id)
            if active_session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前没有活跃会话，请先用 /new 或 /use。",
                    incoming.message_thread_id,
                )
                return
            if not args:
                self._safe_send_message(
                    incoming.chat_id,
                    "用法：/bind <绝对路径>",
                    incoming.message_thread_id,
                )
                return
            bound_path = Path(args).expanduser()
            if not bound_path.is_absolute():
                self._safe_send_message(
                    incoming.chat_id,
                    "请提供绝对路径，例如 /bind /root/project。",
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
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{active_session.id} 已绑定目录：\n{bound_path}",
                incoming.message_thread_id,
            )
            return

        if command == "/pwd":
            active_session = self.storage.get_active_session(incoming.user_id)
            if active_session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前没有活跃会话，请先用 /new 或 /use。",
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

        if command == "/stop":
            active_session = self.storage.get_active_session(incoming.user_id)
            if active_session is None:
                self._safe_send_message(
                    incoming.chat_id,
                    "当前没有活跃会话，请先用 /new 或 /use。",
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
                "当前没有活跃会话，请先用 /new 创建，或用 /use 切换。",
                incoming.message_thread_id,
            )
            return
        if not session.bound_path:
            self._safe_send_message(
                incoming.chat_id,
                "当前会话还没有绑定目录，请先用 /bind <绝对路径>。",
                incoming.message_thread_id,
            )
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"会话 #{session.id} 正在执行中，请稍后或先 /stop。",
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
            )
            with self._active_runs_lock:
                current = self._active_runs.get(session.id)
                if current is not None:
                    current.process_handle = handle
                    if current.stop_requested:
                        self.runner.terminate(handle)

            stdout = handle.process.stdout
            if stdout is None:
                raise RuntimeError("未拿到 Codex 输出流")

            for line in stdout:
                self.runner.append_event_log(handle.event_log_file, line)
                parsed = self.runner.parse_event_line(line)
                if parsed.thread_id:
                    self.storage.update_session_thread_id(session.id, parsed.thread_id)
                if parsed.final_message:
                    final_message = parsed.final_message
                if parsed.preview_text:
                    preview_state.update_stream_text(parsed.preview_text)
                if parsed.status_text:
                    preview_state.update_status(parsed.status_text)

            return_code = handle.process.wait()
            if return_code != 0:
                if active_run.stop_requested:
                    raise InterruptedError("任务已停止")
                event_tail = self.runner.tail_event_log(handle.event_log_file) or "无事件日志"
                raise RuntimeError(
                    f"Codex 退出码异常：{return_code}\n最近事件：\n{event_tail}"
                )

            if not final_message:
                final_message = self.runner.read_output_file(handle.output_file)

            if not final_message:
                final_message = "任务已完成，但没有捕获到最终回复。"

            final_message = strip_citations(final_message)
            preview_state.update_stream_text(final_message)
            preview_state.update_status("正在整理回复...")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._drain_preview_stream(active_run, preview_state)
            self._send_run_result(active_run, final_message)
            self.storage.finish_run(
                active_run.run_id,
                status="completed",
                final_excerpt=final_message[:500],
            )
            self.storage.update_session_status(session.id, "idle")
        except InterruptedError:
            preview_state.update_status("已停止")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._update_preview(active_run, "已停止")
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
            preview_state.update_status("执行失败")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._update_preview(active_run, "执行失败")
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
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_typing_at >= _PREVIEW_TYPING_INTERVAL_SECONDS:
                self._safe_send_chat_action(
                    active_run.chat_id,
                    "typing",
                    active_run.message_thread_id,
                )
                last_typing_at = now

            text = preview_state.advance()
            if text and text != last_preview_text:
                self._update_preview(active_run, text, prefer_html=True)
                last_preview_text = text

            wait_seconds = (
                _PREVIEW_STREAM_INTERVAL_SECONDS
                if preview_state.has_pending_stream()
                else _PREVIEW_HEARTBEAT_INTERVAL_SECONDS
            )
            stop_event.wait(wait_seconds)

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
            self._update_preview(active_run, preview_state.advance(), prefer_html=True)
            time.sleep(_PREVIEW_STREAM_INTERVAL_SECONDS)

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

    def _send_run_result(self, active_run: ActiveRun, text: str) -> None:
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
        cleaned = strip_citations(text).strip()
        final_markdown = f"**● 已完成**\n\n{cleaned}" if cleaned else "**● 已完成**"
        html_text = markdown_to_telegram_html(final_markdown)
        if html_text and len(html_text) <= 3500:
            return html_text, "HTML"

        plain_limit = 3400
        plain_text = f"● 已完成\n\n{cleaned}" if cleaned else "● 已完成"
        if len(plain_text) <= plain_limit:
            return plain_text, None
        suffix = "\n\n[内容较长，已截断]"
        truncated = plain_text[: plain_limit - len(suffix) - 3].rstrip() + "..." + suffix
        return truncated, None

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
