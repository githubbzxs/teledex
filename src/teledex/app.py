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

        preview = self._safe_send_message(
            incoming.chat_id,
            "正在准备会话...",
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
            args=(session, active_run),
            daemon=True,
        )
        worker.start()

    def _execute_run(self, session: SessionRecord, active_run: ActiveRun) -> None:
        final_message: str | None = None
        last_preview_text = "正在准备会话..."
        last_preview_at = 0.0
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
                if parsed.status_text:
                    now = time.monotonic()
                    if (
                        parsed.status_text != last_preview_text
                        and now - last_preview_at
                        >= self.config.preview_update_interval_seconds
                    ):
                        self._update_preview(active_run, parsed.status_text)
                        last_preview_text = parsed.status_text
                        last_preview_at = now

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

            self._update_preview(active_run, "正在整理回复...")
            final_message = strip_citations(final_message)
            self._send_run_result(active_run, final_message)
            self.storage.finish_run(
                active_run.run_id,
                status="completed",
                final_excerpt=final_message[:500],
            )
            self.storage.update_session_status(session.id, "idle")
        except InterruptedError:
            self._update_preview(active_run, "Stopped.")
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
            self._update_preview(active_run, "Failed.")
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

    def _update_preview(self, active_run: ActiveRun, text: str) -> None:
        if active_run.preview_message_id is None:
            return
        try:
            self.telegram.edit_message_text(
                chat_id=active_run.chat_id,
                message_id=active_run.preview_message_id,
                text=text,
                message_thread_id=active_run.message_thread_id,
            )
        except TelegramApiError as exc:
            if is_message_not_modified_error(exc):
                return
            self.logger.exception("更新预览消息失败")

    def _send_run_result(self, active_run: ActiveRun, text: str) -> None:
        self._send_long_message(
            active_run.chat_id,
            text,
            active_run.message_thread_id,
            prefer_html=True,
        )

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
