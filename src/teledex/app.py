from __future__ import annotations

import html
import logging
import re
import shutil
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
    TelegramRateLimitError,
    is_message_not_modified_error,
)


HELP_TEXT = """teledex commands:
/start - Show help
/tbind <absolute-path> - Bind a directory; creates a session if needed or switches to the existing bound session
/tpwd - Show the current session directory
/tstop - Stop the current task
/twipe - Clear all teledex state for the current user

Other slash commands are forwarded to the active Codex session as native Codex commands.
Plain text and photos also continue in the active session."""

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
    ("start", "Show help"),
    ("tbind", "Bind directory"),
    ("tpwd", "Current directory"),
    ("tstop", "Stop task"),
    ("twipe", "Clear state"),
)
_LOCAL_COMMANDS = {
    "/start",
    "/help",
    "/tbind",
    "/tpwd",
    "/tstop",
    "/twipe",
}
_LEGACY_LOCAL_COMMANDS = {
    "/tnew",
    "/tsessions",
    "/tuse",
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
_NO_BOUND_DIRECTORY_MESSAGE = "No directory is bound yet. Use /tbind <absolute-path> first."
_NO_SESSION_DIRECTORY_MESSAGE = (
    "The current session has no bound directory yet. Use /tbind <absolute-path> first."
)
_DEFAULT_IMAGE_PROMPT = "Please inspect the attached image."
_IMAGE_DOWNLOAD_FAILED_MESSAGE = "I couldn't download that image from Telegram. Please try again."


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
    input_items: list[dict[str, object]] | None = None


@dataclass(slots=True)
class ActiveRun:
    run_id: int
    session_id: int
    user_id: int
    chat_id: int
    message_thread_id: int | None
    prompt: str
    preview_message_id: int | None = None
    preview_last_edit_at: float = 0.0
    preview_state: LivePreviewState | None = None
    process_handle: CodexProcessHandle | None = None
    stop_requested: bool = False
    superseded_by_follow_up: bool = False
    input_items: list[dict[str, object]] | None = None
    generated_image_paths: list[str] | None = None


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
            if not self._target_text:
                self._commentary_order.clear()
                self._commentary_text_by_id.clear()
                self._tool_order.clear()
                self._tool_command_by_id.clear()
                self._tool_output_by_id.clear()
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
            if not self._target_text:
                return
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
            if self._in_progress and elapsed_seconds > 0:
                self._elapsed_seconds += max(0, elapsed_seconds)
            if animate_steps > 0 and self._in_progress:
                self._frame_index = (
                    self._frame_index + animate_steps
                ) % len(_PREVIEW_HEARTBEAT_FRAMES)
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
        if self._target_text:
            output_text = _truncate_preview_text(
                _sanitize_preview_text(self._target_text),
                self._output_max_chars,
            )
            if output_text:
                sections.append(output_text)
        else:
            commentary = self._render_commentary_locked()
            if commentary:
                sections.append(commentary)
            tool_blocks = self._render_tool_blocks_locked()
            if tool_blocks:
                sections.append(tool_blocks)

        return "\n\n".join(section for section in sections if section).strip()

    def _render_commentary_locked(self) -> str:
        if not self._commentary_order:
            return ""
        entries = [
            _sanitize_preview_text(self._commentary_text_by_id[item_id])
            for item_id in self._commentary_order
            if self._commentary_text_by_id.get(item_id)
        ]
        filtered_entries = [entry for entry in entries if entry]
        if not filtered_entries:
            return ""
        return _truncate_preview_middle("\n\n".join(filtered_entries), self._history_max_chars)

    def _render_tool_blocks_locked(self) -> str:
        return ""


def _truncate_preview_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _sanitize_preview_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    sanitized = re.sub(r"```[\s\S]*?```", "", normalized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    sanitized = sanitized.strip()
    if sanitized:
        return sanitized
    return "Working through implementation details"


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
        minutes, remainder = divmod(total_seconds, 60)
        return f"{minutes}m {remainder:02d}s"
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
        recovered_runs = self.storage.reconcile_interrupted_runs("服务重启，已回收未完成任务")
        self.telegram = TelegramClient(self.config.telegram_bot_token)
        self.runner = CodexRunner(config)
        self.logger = logging.getLogger("teledex")
        self._active_runs: dict[int, ActiveRun] = {}
        self._queued_runs: dict[int, list[ActiveRun]] = {}
        self._session_workers: dict[int, threading.Thread] = {}
        self._active_runs_lock = threading.RLock()
        self._update_offset: int | None = self.storage.get_telegram_update_offset()
        self._telegram_rate_limit_lock = threading.RLock()
        self._telegram_rate_limit_until = 0.0
        self._preview_edit_lock = threading.RLock()
        if recovered_runs > 0:
            self.logger.warning("服务启动时回收了 %s 个遗留运行中的会话。", recovered_runs)

    def _is_local_command(self, text: str) -> bool:
        return self._extract_command(text) in (_LOCAL_COMMANDS | _LEGACY_LOCAL_COMMANDS)

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
                    next_offset = int(update["update_id"]) + 1
                    self._handle_update(update)
                    self._update_offset = next_offset
                    self.storage.set_telegram_update_offset(next_offset)
            except TelegramRateLimitError as exc:
                delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
                self.logger.warning("Telegram 轮询触发限流，%s 秒后重试。", delay)
                time.sleep(delay)
            except TelegramApiError:
                self.logger.exception("Telegram 轮询失败")
                time.sleep(3)
            except Exception:
                self.logger.exception("主循环异常")
                time.sleep(3)

    def _remember_telegram_rate_limit(self, retry_after_seconds: int) -> int:
        delay = max(1, retry_after_seconds) + 1
        with self._telegram_rate_limit_lock:
            self._telegram_rate_limit_until = max(
                self._telegram_rate_limit_until,
                time.monotonic() + delay,
            )
        return delay

    def _telegram_rate_limit_remaining_seconds(self) -> float:
        with self._telegram_rate_limit_lock:
            return max(0.0, self._telegram_rate_limit_until - time.monotonic())

    def _wait_for_telegram_rate_limit(self, max_wait_seconds: float | None = None) -> bool:
        remaining = self._telegram_rate_limit_remaining_seconds()
        if remaining <= 0:
            return True
        if max_wait_seconds is not None and remaining > max_wait_seconds:
            return False
        time.sleep(remaining)
        return True

    def _schedule_delayed_message_send(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        attempts_remaining: int = 2,
    ) -> None:
        worker = threading.Thread(
            target=self._delayed_send_message,
            args=(
                chat_id,
                text,
                message_thread_id,
                reply_to_message_id,
                parse_mode,
                attempts_remaining,
            ),
            daemon=True,
        )
        worker.start()

    def _delayed_send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None,
        reply_to_message_id: int | None,
        parse_mode: str | None,
        attempts_remaining: int,
    ) -> None:
        if attempts_remaining <= 0:
            self.logger.error("Telegram 延迟消息重试次数已耗尽。")
            return
        if not self._wait_for_telegram_rate_limit(max_wait_seconds=900):
            self.logger.error("Telegram 限流窗口过长，放弃延迟发送消息。")
            return
        try:
            self.telegram.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode,
            )
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning(
                "Telegram 延迟消息仍被限流，%s 秒后继续重试，剩余 %s 次。",
                delay,
                attempts_remaining - 1,
            )
            self._schedule_delayed_message_send(
                chat_id,
                text,
                message_thread_id,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode,
                attempts_remaining=attempts_remaining - 1,
            )
        except TelegramApiError:
            self.logger.exception("延迟发送 Telegram 消息失败")

    def _build_incoming_message(self, message: dict, user_id: int) -> IncomingMessage | None:
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        caption = str(message.get("caption") or "").strip()
        prompt_text = text or caption
        input_items: list[dict[str, object]] = []
        if prompt_text:
            input_items.append(
                {
                    "type": "text",
                    "text": prompt_text,
                    "text_elements": [],
                }
            )

        photo_path = self._download_message_photo(message)
        if photo_path is not None:
            input_items.append(
                {
                    "type": "localImage",
                    "path": str(photo_path),
                }
            )
            if not prompt_text:
                prompt_text = _DEFAULT_IMAGE_PROMPT
                input_items.insert(
                    0,
                    {
                        "type": "text",
                        "text": prompt_text,
                        "text_elements": [],
                    },
                )

        if not prompt_text:
            return None
        return IncomingMessage(
            chat_id=int(chat.get("id")),
            user_id=user_id,
            text=prompt_text,
            message_id=int(message.get("message_id")),
            message_thread_id=(
                int(message["message_thread_id"])
                if message.get("message_thread_id") is not None
                else None
            ),
            input_items=input_items or None,
        )

    def _download_message_photo(self, message: dict) -> Path | None:
        photos = message.get("photo")
        if not isinstance(photos, list) or not photos:
            return None
        best_photo = self._select_best_photo_size(photos)
        if best_photo is None:
            return None
        file_id = str(best_photo.get("file_id") or "").strip()
        if not file_id:
            return None
        file_info = self.telegram.get_file(file_id)
        remote_path = str(file_info.get("file_path") or "").strip()
        if not remote_path:
            raise RuntimeError("Telegram did not return a file path for the photo.")
        suffix = Path(remote_path).suffix or ".jpg"
        download_dir = self.config.state_dir / "runtime" / "telegram"
        download_dir.mkdir(parents=True, exist_ok=True)
        local_path = download_dir / f"message-{int(message.get('message_id', 0))}-{file_id}{suffix}"
        return self.telegram.download_file(remote_path, local_path)

    def _select_best_photo_size(self, photos: list[object]) -> dict | None:
        best_photo: dict | None = None
        best_score = -1
        for candidate in photos:
            if not isinstance(candidate, dict):
                continue
            file_size = int(candidate.get("file_size") or 0)
            width = int(candidate.get("width") or 0)
            height = int(candidate.get("height") or 0)
            score = max(file_size, width * height)
            if score > best_score:
                best_score = score
                best_photo = candidate
        return best_photo

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        from_user = message.get("from") or {}
        user_id = int(from_user.get("id", 0))
        try:
            incoming = self._build_incoming_message(message, user_id)
        except TelegramApiError:
            self.logger.exception("下载 Telegram 图片失败")
            self._safe_send_message(
                int((message.get("chat") or {}).get("id")),
                _IMAGE_DOWNLOAD_FAILED_MESSAGE,
                (
                    int(message["message_thread_id"])
                    if message.get("message_thread_id") is not None
                    else None
                ),
            )
            return
        except Exception:
            self.logger.exception("处理 Telegram 图片失败")
            self._safe_send_message(
                int((message.get("chat") or {}).get("id")),
                _IMAGE_DOWNLOAD_FAILED_MESSAGE,
                (
                    int(message["message_thread_id"])
                    if message.get("message_thread_id") is not None
                    else None
                ),
            )
            return
        if incoming is None:
            return
        update_id = int(update["update_id"]) if update.get("update_id") is not None else None

        if self.storage.has_processed_message(incoming.chat_id, incoming.message_id):
            self.logger.info(
                "忽略重复 Telegram 消息：chat=%s message_id=%s update_id=%s",
                incoming.chat_id,
                incoming.message_id,
                update_id,
            )
            return

        handled = False
        if user_id not in self.config.authorized_user_ids:
            self._safe_send_message(
                incoming.chat_id,
                "You are not authorized to use this bot.",
                incoming.message_thread_id,
            )
            handled = True
        else:
            self.storage.ensure_user(
                user_id=incoming.user_id,
                chat_id=incoming.chat_id,
                message_thread_id=incoming.message_thread_id,
            )

            if incoming.text.startswith("//"):
                self._handle_prompt(self._normalize_incoming_message(incoming))
            elif incoming.text.startswith("/") and self._is_local_command(incoming.text):
                self._handle_command(incoming)
            elif incoming.text.startswith("/") and self._is_mirrored_codex_command(incoming.text):
                self._handle_codex_command(incoming)
            else:
                self._handle_prompt(incoming)
            handled = True

        if handled:
            self.storage.mark_message_processed(
                chat_id=incoming.chat_id,
                message_id=incoming.message_id,
                user_id=incoming.user_id,
                message_thread_id=incoming.message_thread_id,
                update_id=update_id,
                text=incoming.text,
            )

    def _normalize_incoming_message(self, incoming: IncomingMessage) -> IncomingMessage:
        if not incoming.text.startswith("//"):
            return incoming
        updated_items: list[dict[str, object]] = []
        for index, item in enumerate(incoming.input_items or []):
            updated = dict(item)
            if index == 0 and updated.get("type") == "text" and isinstance(updated.get("text"), str):
                updated["text"] = "/" + incoming.text[2:]
            updated_items.append(updated)
        return IncomingMessage(
            chat_id=incoming.chat_id,
            user_id=incoming.user_id,
            text="/" + incoming.text[2:],
            message_id=incoming.message_id,
            message_thread_id=incoming.message_thread_id,
            input_items=updated_items or incoming.input_items,
        )

    def _handle_command(self, incoming: IncomingMessage) -> None:
        command_text = incoming.text.split()[0]
        command = self._extract_command(incoming.text)
        args = incoming.text[len(command_text) :].strip()

        if command in {"/help", "/start"}:
            self._safe_send_message(incoming.chat_id, HELP_TEXT, incoming.message_thread_id)
            return

        if command in _LEGACY_LOCAL_COMMANDS:
            self._safe_send_message(
                incoming.chat_id,
                "That management command has been removed. Use /tbind <absolute-path> instead. "
                "If the directory has no session yet, teledex will create one automatically; "
                "otherwise it will switch to the existing bound session.",
                incoming.message_thread_id,
            )
            return

        if command == "/tbind":
            if not args:
                self._safe_send_message(
                    incoming.chat_id,
                    "Usage: /tbind <absolute-path>",
                    incoming.message_thread_id,
                )
                return
            bound_path = Path(args).expanduser()
            if not bound_path.is_absolute():
                self._safe_send_message(
                    incoming.chat_id,
                    "Please provide an absolute path, for example /tbind /root/project.",
                    incoming.message_thread_id,
                )
                return
            if not bound_path.exists() or not bound_path.is_dir():
                self._safe_send_message(
                    incoming.chat_id,
                    f"Directory not found: {bound_path}",
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
                self.runner.reset_session_runtime(target_session.id)
                self.runner.reset_terminal(target_session.id, bound_path)
                tmux_session_name = self.runner.ensure_terminal(target_session.id, bound_path)
                action_text = (
                    f"Created session #{target_session.id} and bound directory:"
                    if created_new_session
                    else f"Session #{target_session.id} is now bound to:"
                )
                message = (
                    f"{action_text}\n{bound_path}\n"
                    f"Title: {_session_title_from_path(bound_path)}\n"
                    f"Persistent terminal: tmux `{tmux_session_name}`"
                )
            except Exception as exc:
                self.logger.exception("初始化 tmux 会话失败")
                action_text = (
                    f"Created session #{target_session.id} and bound directory:"
                    if created_new_session
                    else f"Session #{target_session.id} is now bound to:"
                )
                message = (
                    f"{action_text}\n{bound_path}\n"
                    f"Title: {_session_title_from_path(bound_path)}\n"
                    f"But the persistent tmux terminal failed to initialize: {exc}"
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
                    _NO_BOUND_DIRECTORY_MESSAGE,
                    incoming.message_thread_id,
                )
                return
            path_text = active_session.bound_path or "No directory is currently bound to this session."
            self._safe_send_message(
                incoming.chat_id,
                f"Current session: #{active_session.id}\nDirectory: {path_text}",
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
                    _NO_BOUND_DIRECTORY_MESSAGE,
                    incoming.message_thread_id,
                )
                return
            if self._stop_session_run(active_session.id):
                self._safe_send_message(
                    incoming.chat_id,
                    f"Sent a stop signal to the current task in session #{active_session.id}.",
                    incoming.message_thread_id,
                )
            else:
                self._safe_send_message(
                    incoming.chat_id,
                    f"Session #{active_session.id} does not have a running task.",
                    incoming.message_thread_id,
                )
            return

        if command == "/twipe":
            self._handle_wipe_command(incoming)
            return

        self._safe_send_message(
            incoming.chat_id,
            f"Unknown command: {command}\n\n{HELP_TEXT}",
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
                _NO_BOUND_DIRECTORY_MESSAGE,
                incoming.message_thread_id,
            )
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"Session #{session.id} is running right now, so /new is temporarily unavailable. "
                "Wait a moment or use /tstop first.",
                incoming.message_thread_id,
            )
            return

        self._reset_session_thread(session.id)
        suffix = (
            f"Directory unchanged: {session.bound_path}"
            if session.bound_path
            else _NO_SESSION_DIRECTORY_MESSAGE
        )
        self._safe_send_message(
            incoming.chat_id,
            f"Started a new Codex conversation in session #{session.id}.\n{suffix}",
            incoming.message_thread_id,
        )

    def _handle_codex_clear_command(self, incoming: IncomingMessage, args: str = "") -> None:
        session = self._get_active_session_or_notify(incoming)
        if session is None:
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"Session #{session.id} is running right now, so /clear is temporarily unavailable. "
                "Wait a moment or use /tstop first.",
                incoming.message_thread_id,
            )
            return
        self._reset_session_thread(session.id)
        self._safe_send_message(
            incoming.chat_id,
            f"Cleared the current Codex conversation for session #{session.id}.\n"
            "Telegram history stays intact, and the next message will start a fresh conversation.",
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
                    "There are no resumable Codex threads in the current directory.",
                    incoming.message_thread_id,
                )
                return
            lines = ["Recent Codex threads in the current directory:"]
            for index, thread in enumerate(threads, start=1):
                name = f" [{thread.name}]" if thread.name else ""
                preview = thread.preview or "No preview"
                lines.append(f"{index}. {thread.thread_id}{name}\n{preview}")
            lines.append("\nUsage: /resume <index-or-thread_id>")
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
                "That thread could not be found. Run `/resume` first to see the list, then resume "
                "by index or full thread_id.",
                incoming.message_thread_id,
                parse_mode="HTML",
            )
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"Session #{session.id} is running right now, so /resume is temporarily unavailable. "
                "Wait a moment or use /tstop first.",
                incoming.message_thread_id,
            )
            return
        self.storage.update_session_thread_id(session.id, thread.thread_id)
        self.runner.reset_session_runtime(session.id)
        self.storage.update_session_status(session.id, "idle")
        title_text = f"\nName: {thread.name}" if thread.name else ""
        self._safe_send_message(
            incoming.chat_id,
            f"Session #{session.id} resumed Codex thread: {thread.thread_id}{title_text}",
            incoming.message_thread_id,
        )

    def _handle_codex_fork_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if self._is_session_running(session.id):
            self._safe_send_message(
                incoming.chat_id,
                f"Session #{session.id} is running right now, so /fork is temporarily unavailable. "
                "Wait a moment or use /tstop first.",
                incoming.message_thread_id,
            )
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "The current session does not have a Codex thread to fork yet. Start a conversation first.",
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
            raise RuntimeError("Fork did not return a new thread_id.")
        self.storage.update_session_thread_id(session.id, new_thread_id)
        self.runner.reset_session_runtime(session.id)
        self._safe_send_message(
            incoming.chat_id,
            f"Session #{session.id} forked to a new Codex thread: {new_thread_id}",
            incoming.message_thread_id,
        )

    def _handle_codex_rename_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "The current session does not have an active Codex thread yet. Start a conversation first.",
                incoming.message_thread_id,
            )
            return
        name = args.strip()
        if not name:
            self._safe_send_message(
                incoming.chat_id,
                "Usage: /rename <new-title>",
                incoming.message_thread_id,
            )
            return
        self.runner.set_thread_name(Path(session.bound_path), session.codex_thread_id, name)
        self._safe_send_message(
            incoming.chat_id,
            f"The current Codex thread was renamed to: {name}",
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
                "AGENTS.md already exists in this directory, and native Codex /init will also skip overwriting it.",
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
            lines = ["Available models:"]
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
            lines.append("\nUsage: /model <model> [effort]")
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
                f"Unsupported reasoning effort: {effort}\nAllowed values: {', '.join(_REASONING_EFFORT_VALUES)}",
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
                f"Fast mode is currently: {'on' if current else 'off'}",
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
                "Usage: /fast [on|off|status]",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"service_tier": "fast" if enabled else None},
            f"Fast mode set to: {'on' if enabled else 'off'}",
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
                f"Current personality: {current}\nAllowed values: default, {', '.join(_PERSONALITY_VALUES)}",
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
                f"Unsupported personality: {value}\nAllowed values: default, {', '.join(_PERSONALITY_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"personality": normalized},
            f"Personality updated to: {normalized or 'default'}",
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
                f"Current approval policy: {current}\nAllowed values: default, {', '.join(_APPROVAL_POLICY_VALUES)}",
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
                f"Unsupported approval policy: {value}\nAllowed values: default, {', '.join(_APPROVAL_POLICY_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"approval_policy": normalized},
            f"Approval policy updated to: {normalized or 'default'}",
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
                f"Current sandbox mode: {current}\nAllowed values: default, {', '.join(_SANDBOX_MODE_VALUES)}",
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
                f"Unsupported sandbox mode: {value}\nAllowed values: default, {', '.join(_SANDBOX_MODE_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"sandbox_mode": normalized},
            f"Sandbox mode updated to: {normalized or 'default'}",
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
                "Usage: /plan [on|off]",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"collaboration_mode": normalized},
            f"Collaboration mode updated to: {normalized}",
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
                f"Current collaboration mode: {current}\nUsage: /collab list or /collab <default|plan>",
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
            text = "Available collaboration modes:\n" + "\n".join(f"- {item}" for item in available)
            self._safe_send_message(incoming.chat_id, text, incoming.message_thread_id)
            return
        if value not in _COLLABORATION_MODE_VALUES:
            self._safe_send_message(
                incoming.chat_id,
                f"Unsupported collaboration mode: {value}\nAllowed values: {', '.join(_COLLABORATION_MODE_VALUES)}",
                incoming.message_thread_id,
            )
            return
        self._apply_session_codex_settings(
            incoming,
            session,
            {"collaboration_mode": value},
            f"Collaboration mode updated to: {value}",
        )

    def _handle_codex_status_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._get_active_session_or_notify(incoming)
        if session is None:
            return
        lines = [
            f"Session #{session.id}",
            f"Directory: {session.bound_path or 'Not bound'}",
            f"Thread: {session.codex_thread_id or 'Not created'}",
            f"Status: {session.status}",
            f"Model: {session.codex_settings.get('model') or self.config.codex_model or 'default'}",
            f"Effort: {session.codex_settings.get('reasoning_effort') or 'default'}",
            f"Fast: {'on' if session.codex_settings.get('service_tier') == 'fast' else 'off'}",
            f"Personality: {session.codex_settings.get('personality') or 'default'}",
            f"Approval: {session.codex_settings.get('approval_policy') or 'default'}",
            f"Sandbox: {session.codex_settings.get('sandbox_mode') or 'default'}",
            f"Collab: {session.codex_settings.get('collaboration_mode') or 'default'}",
        ]
        self._safe_send_message(incoming.chat_id, "\n".join(lines), incoming.message_thread_id)

    def _handle_codex_debug_config_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        config = self.runner.read_config(Path(session.bound_path))
        effective = config.get("config") if isinstance(config, dict) else {}
        layers = config.get("layers") if isinstance(config, dict) else []
        lines = ["Codex config summary:"]
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
            lines.append("Config layers:")
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
            self._safe_send_message(incoming.chat_id, "No MCP servers are currently visible.", incoming.message_thread_id)
            return
        lines = ["MCP servers:"]
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
            self._safe_send_message(incoming.chat_id, "No visible apps are currently available.", incoming.message_thread_id)
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
            self._safe_send_message(incoming.chat_id, "No skills were detected in the current directory.", incoming.message_thread_id)
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
            self._safe_send_message(incoming.chat_id, "No experimental feature list is available right now.", incoming.message_thread_id)
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
                "The current directory is not a Git repository, or the diff could not be calculated.",
                incoming.message_thread_id,
            )
            return
        text = (result.stdout.strip() + "\n\n" + detail.stdout.strip()).strip() or "There are no current changes."
        self._send_long_message(incoming.chat_id, text, incoming.message_thread_id)

    def _handle_codex_rollout_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "The current session does not have an active Codex thread yet.",
                incoming.message_thread_id,
            )
            return
        payload = self.runner.read_thread(Path(session.bound_path), session.codex_thread_id)
        thread = payload.get("thread") if isinstance(payload, dict) else {}
        path_text = str(thread.get("path") or "").strip()
        self._safe_send_message(
            incoming.chat_id,
            path_text or "The current thread does not have a rollout path yet.",
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
                "There is no final reply available to copy yet.",
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
                "The current session does not have a Codex thread to compact yet.",
                incoming.message_thread_id,
            )
            return
        self.runner.compact_thread(Path(session.bound_path), session.codex_thread_id)
        self._safe_send_message(
            incoming.chat_id,
            "Triggered compact on the current Codex thread.",
            incoming.message_thread_id,
        )

    def _handle_codex_clean_command(self, incoming: IncomingMessage, args: str) -> None:
        session = self._require_bound_session_or_notify(incoming)
        if session is None:
            return
        if not session.codex_thread_id:
            self._safe_send_message(
                incoming.chat_id,
                "The current session does not have an active Codex thread yet.",
                incoming.message_thread_id,
            )
            return
        self.runner.clean_background_terminals(Path(session.bound_path), session.codex_thread_id)
        self._safe_send_message(
            incoming.chat_id,
            "Requested cleanup for the current thread's background terminals.",
            incoming.message_thread_id,
        )

    def _handle_unsupported_codex_command(self, incoming: IncomingMessage, command: str) -> None:
        self._safe_send_message(
            incoming.chat_id,
            (
                f"{command} was recognized as a native Codex command, but this Telegram bridge does not "
                "have a non-interactive implementation for it yet.\nIt will no longer be forwarded to the model as plain text."
            ),
            incoming.message_thread_id,
        )

    def _handle_wipe_command(self, incoming: IncomingMessage) -> None:
        sessions = self.storage.list_sessions(incoming.user_id)
        stopped_runs = 0
        cancelled_queued_runs = 0
        reset_terminals = 0
        for session in sessions:
            if self._stop_session_run(session.id):
                stopped_runs += 1
            cancelled_queued_runs += self._cancel_queued_runs(session.id, "用户已清空状态")
            self.runner.reset_session_runtime(session.id)
            if not session.bound_path:
                continue
            try:
                self.runner.reset_terminal(session.id, Path(session.bound_path))
                reset_terminals += 1
            except Exception:
                self.logger.warning(
                    "清空会话 #%s 的 tmux 终端失败：%s",
                    session.id,
                    session.bound_path,
                    exc_info=True,
                )

        runtime_deleted = self._clear_runtime_artifacts()
        summary = self.storage.wipe_user_data(incoming.user_id)
        lines = [
            "Cleared teledex state for the current user.",
            f"Deleted sessions: {summary.sessions_deleted}",
            f"Deleted runs: {summary.runs_deleted}",
            f"Deleted context bindings: {summary.contexts_deleted}",
            f"Reset persistent terminals: {reset_terminals}",
            f"Deleted runtime artifacts: {runtime_deleted}",
        ]
        if stopped_runs > 0:
            lines.append(f"Interrupted running tasks: {stopped_runs}")
        if cancelled_queued_runs > 0:
            lines.append(f"Cancelled queued tasks: {cancelled_queued_runs}")
        lines.append("The next message will start again like a fresh session.")
        self._safe_send_message(
            incoming.chat_id,
            "\n".join(lines),
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
                _NO_BOUND_DIRECTORY_MESSAGE,
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
                _NO_SESSION_DIRECTORY_MESSAGE,
                incoming.message_thread_id,
            )
            return None
        return session

    def _reset_session_thread(self, session_id: int) -> None:
        self.storage.clear_session_thread_id(session_id)
        self.runner.reset_session_runtime(session_id)

    def _clear_runtime_artifacts(self) -> int:
        runtime_dir = self.config.state_dir / "runtime"
        if not runtime_dir.exists():
            return 0
        deleted = 0
        for path in runtime_dir.iterdir():
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                self.logger.warning("清理运行时文件失败：%s", path, exc_info=True)
        return deleted

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
                f"Session #{session.id} is running right now. Wait a moment or use /tstop before changing Codex settings.",
                incoming.message_thread_id,
            )
            return
        self.storage.update_session_codex_settings(session.id, dict(updates))
        self._reset_session_thread(session.id)
        self._safe_send_message(
            incoming.chat_id,
            f"{success_message}\nReset the Codex thread for the current session. The next message will use the new settings.",
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
            f"Current model: {session.codex_settings.get('model') or self.config.codex_model or 'default'}\n"
            f"Current effort: {session.codex_settings.get('reasoning_effort') or 'default'}\n"
            "Usage: /model list or /model <model> [effort]"
        )

    def _format_model_status_message(self, model: str, effort: str | None) -> str:
        target_model = "default" if model == "default" else model
        target_effort = "default" if effort in {None, 'default'} else effort
        return f"Model updated to: {target_model}\nReasoning effort: {target_effort}"

    def _handle_prompt(self, incoming: IncomingMessage) -> None:
        session = self.storage.get_active_session(
            incoming.user_id,
            incoming.chat_id,
            incoming.message_thread_id,
        )
        if session is None:
            self._safe_send_message(
                incoming.chat_id,
                _NO_BOUND_DIRECTORY_MESSAGE,
                incoming.message_thread_id,
            )
            return
        if not session.bound_path:
            self._safe_send_message(
                incoming.chat_id,
                _NO_SESSION_DIRECTORY_MESSAGE,
                incoming.message_thread_id,
            )
            return
        preview_state = LivePreviewState(initial_status="Thinking")
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
            preview_state=preview_state,
            input_items=incoming.input_items,
            generated_image_paths=[],
        )
        worker: threading.Thread | None = None
        interrupted_handle: CodexProcessHandle | None = None
        replaced_runs: list[ActiveRun] = []
        with self._active_runs_lock:
            current_run = self._active_runs.get(session.id)
            if current_run is None:
                self._active_runs[session.id] = active_run
                current_worker = self._session_workers.get(session.id)
                if not self._thread_is_alive(current_worker):
                    worker = threading.Thread(
                        target=self._run_session_queue,
                        args=(session.id,),
                        daemon=True,
                    )
                    self._session_workers[session.id] = worker
            else:
                current_run.stop_requested = True
                current_run.superseded_by_follow_up = True
                interrupted_handle = current_run.process_handle
                replaced_runs = self._queued_runs.pop(session.id, [])
                self._queued_runs[session.id] = [active_run]
        self.storage.update_session_status(session.id, "running")
        for replaced_run in replaced_runs:
            self._finish_pending_run_as_stopped(replaced_run, "A follow-up message took over the current task.")
        if interrupted_handle is not None:
            self.runner.terminate(interrupted_handle)
        if worker is not None:
            worker.start()

    def _run_session_queue(self, session_id: int) -> None:
        current_worker = threading.current_thread()
        try:
            while True:
                with self._active_runs_lock:
                    active_run = self._active_runs.get(session_id)
                if active_run is None:
                    break
                session = self.storage.get_session(session_id)
                if session is None:
                    self._cancel_run_without_session(active_run)
                else:
                    preview_state = active_run.preview_state or LivePreviewState()
                    preview_state.update_status("Thinking")
                    self._execute_run(session, active_run, preview_state)

                next_run: ActiveRun | None = None
                with self._active_runs_lock:
                    current_run = self._active_runs.get(session_id)
                    if current_run is active_run:
                        self._active_runs.pop(session_id, None)
                    queue = self._queued_runs.get(session_id)
                    if queue:
                        next_run = queue.pop(0)
                        self._active_runs[session_id] = next_run
                        if not queue:
                            self._queued_runs.pop(session_id, None)
                    else:
                        self._queued_runs.pop(session_id, None)
                if next_run is None:
                    break
                self.storage.update_session_status(session_id, "running")
        finally:
            with self._active_runs_lock:
                if self._session_workers.get(session_id) is current_worker:
                    self._session_workers.pop(session_id, None)

    def _cancel_run_without_session(self, active_run: ActiveRun) -> None:
        preview_state = active_run.preview_state or LivePreviewState(initial_status="Stopped")
        preview_state.finish("Stopped")
        self._render_finished_preview(active_run, preview_state)
        self.storage.finish_run(
            active_run.run_id,
            status="stopped",
            error_message="The session no longer exists.",
        )

    def _thread_is_alive(self, worker: threading.Thread | None) -> bool:
        if worker is None:
            return False
        is_alive = getattr(worker, "is_alive", None)
        if callable(is_alive):
            return bool(is_alive())
        return False

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
                raise RuntimeError("The session is not bound to a directory.")

            handle = self.runner.start(
                prompt=active_run.prompt,
                input_items=active_run.input_items,
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
                if parsed.generated_image_path:
                    image_paths = active_run.generated_image_paths
                    if image_paths is not None and parsed.generated_image_path not in image_paths:
                        image_paths.append(parsed.generated_image_path)
                if parsed.commentary_id and parsed.commentary_text:
                    preview_state.update_commentary(
                        parsed.commentary_id,
                        parsed.commentary_text,
                    )
                if parsed.commentary_completed_id:
                    preview_state.clear_commentary(parsed.commentary_completed_id)
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
                    raise InterruptedError("The task was stopped.")
                event_tail = self.runner.tail_event_log(handle.event_log_file) or "No event log available."
                raise RuntimeError(
                    f"Codex exited with an unexpected code: {status.exit_code}\nRecent events:\n{event_tail}"
                )

            if not final_message:
                final_message = self.runner.read_output_file(handle.output_file)

            if not final_message:
                final_message = "Completed, but no final reply was captured."

            self._stop_preview_loop(preview_stop_event, preview_worker)
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
            stop_reason = (
                "A follow-up message took over the current task."
                if active_run.superseded_by_follow_up
                else "Stopped by the user."
            )
            if not active_run.superseded_by_follow_up:
                self._safe_send_message(
                    active_run.chat_id,
                    f"The task in session #{session.id} was stopped.",
                    active_run.message_thread_id,
                )
            self.storage.finish_run(
                active_run.run_id,
                status="stopped",
                error_message=stop_reason,
            )
            self.storage.update_session_status(session.id, "idle")
        except Exception as exc:
            self.logger.exception("执行会话 #%s 失败", session.id)
            preview_state.finish("Failed")
            self._stop_preview_loop(preview_stop_event, preview_worker)
            self._render_finished_preview(active_run, preview_state)
            self._safe_send_message(
                active_run.chat_id,
                f"Session #{session.id} failed: {exc}",
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

    def _is_session_running(self, session_id: int) -> bool:
        with self._active_runs_lock:
            if session_id in self._active_runs:
                return True
            return bool(self._queued_runs.get(session_id))

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

    def _cancel_queued_runs(self, session_id: int, reason: str) -> int:
        with self._active_runs_lock:
            queued_runs = self._queued_runs.pop(session_id, [])
        for active_run in queued_runs:
            self._finish_pending_run_as_stopped(active_run, reason)
        return len(queued_runs)

    def _finish_pending_run_as_stopped(self, active_run: ActiveRun, reason: str) -> None:
        preview_state = active_run.preview_state or LivePreviewState(initial_status="Stopped")
        preview_state.finish("Stopped")
        self._render_finished_preview(active_run, preview_state)
        self.storage.finish_run(
            active_run.run_id,
            status="stopped",
            error_message=reason,
        )

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
        respect_local_interval: bool = True,
    ) -> bool:
        if active_run.preview_message_id is None:
            return False
        if self._telegram_rate_limit_remaining_seconds() > 0:
            return False
        if not self._acquire_preview_edit_slot(
            active_run,
            respect_local_interval=respect_local_interval,
        ):
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
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning("Telegram 预览更新触发限流，%s 秒内暂停预览发送。", delay)
            return False
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
            respect_local_interval=False,
        ):
            return True
        return self._edit_preview_message(
            active_run,
            preview_state.render(),
            respect_local_interval=False,
        )

    def _send_run_result(
        self,
        active_run: ActiveRun,
        text: str,
        preview_state: LivePreviewState | None = None,
    ) -> None:
        del preview_state
        self._safe_delete_preview_message(active_run, defer_on_rate_limit=True)
        for image_path in active_run.generated_image_paths or []:
            self._safe_send_photo(
                active_run.chat_id,
                Path(image_path),
                active_run.message_thread_id,
                defer_on_rate_limit=True,
            )
        final_text, parse_mode = self._build_final_result_message(text)
        self._safe_send_message(
            active_run.chat_id,
            final_text,
            active_run.message_thread_id,
            parse_mode=parse_mode,
            defer_on_rate_limit=True,
        )

    def _build_final_result_message(self, text: str) -> tuple[str, str | None]:
        plain_limit = 3500
        cleaned = text.strip()
        html_text = markdown_to_telegram_html(cleaned)
        if html_text and len(html_text) <= 3500:
            return html_text, "HTML"
        plain_text = cleaned or "Completed, but there was no final reply to display."
        if len(plain_text) <= plain_limit:
            return plain_text, None
        suffix = "\n\n[Truncated for length]"
        truncated = plain_text[: plain_limit - len(suffix) - 3].rstrip() + "..." + suffix
        return truncated, None

    def _acquire_preview_edit_slot(
        self,
        active_run: ActiveRun,
        respect_local_interval: bool,
    ) -> bool:
        if not respect_local_interval:
            return True
        min_interval = max(0.0, self.config.preview_edit_min_interval_seconds)
        if min_interval <= 0:
            return True
        now = time.monotonic()
        with self._preview_edit_lock:
            if now - active_run.preview_last_edit_at < min_interval:
                return False
            active_run.preview_last_edit_at = now
            return True

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
        if self._telegram_rate_limit_remaining_seconds() > 0:
            return
        try:
            self.telegram.send_chat_action(
                chat_id=chat_id,
                action=action,
                message_thread_id=message_thread_id,
            )
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning("Telegram chat action 触发限流，%s 秒内暂停发送。", delay)
        except TelegramApiError:
            self.logger.debug("发送 Telegram chat action 失败", exc_info=True)

    def _schedule_delayed_preview_delete(
        self,
        active_run: ActiveRun,
        attempts_remaining: int = 2,
    ) -> None:
        worker = threading.Thread(
            target=self._delayed_delete_preview_message,
            args=(active_run, attempts_remaining),
            daemon=True,
        )
        worker.start()

    def _delayed_delete_preview_message(
        self,
        active_run: ActiveRun,
        attempts_remaining: int,
    ) -> None:
        if attempts_remaining <= 0:
            self.logger.error("Telegram 预览消息删除重试次数已耗尽。")
            return
        if not self._wait_for_telegram_rate_limit(max_wait_seconds=900):
            self.logger.error("Telegram 限流窗口过长，放弃延迟删除预览消息。")
            return
        preview_message_id = active_run.preview_message_id
        if preview_message_id is None:
            return
        try:
            self.telegram.delete_message(
                chat_id=active_run.chat_id,
                message_id=preview_message_id,
            )
            active_run.preview_message_id = None
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning(
                "Telegram 延迟删除预览消息仍被限流，%s 秒后继续重试，剩余 %s 次。",
                delay,
                attempts_remaining - 1,
            )
            self._schedule_delayed_preview_delete(
                active_run,
                attempts_remaining=attempts_remaining - 1,
            )
        except TelegramApiError:
            self.logger.exception("延迟删除 Telegram 预览消息失败")

    def _safe_delete_preview_message(
        self,
        active_run: ActiveRun,
        defer_on_rate_limit: bool = False,
    ) -> bool:
        preview_message_id = active_run.preview_message_id
        if preview_message_id is None:
            return True
        if self._telegram_rate_limit_remaining_seconds() > 0:
            if defer_on_rate_limit:
                self._schedule_delayed_preview_delete(active_run)
            return False
        try:
            self.telegram.delete_message(
                chat_id=active_run.chat_id,
                message_id=preview_message_id,
            )
            active_run.preview_message_id = None
            return True
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning("Telegram 删除预览消息触发限流，%s 秒内暂停发送。", delay)
            if defer_on_rate_limit:
                self._schedule_delayed_preview_delete(active_run)
            return False
        except TelegramApiError:
            self.logger.exception("删除 Telegram 预览消息失败")
            return False

    def _schedule_delayed_photo_send(
        self,
        chat_id: int,
        photo_path: Path,
        message_thread_id: int | None,
        attempts_remaining: int = 2,
    ) -> None:
        worker = threading.Thread(
            target=self._delayed_send_photo,
            args=(chat_id, photo_path, message_thread_id, attempts_remaining),
            daemon=True,
        )
        worker.start()

    def _delayed_send_photo(
        self,
        chat_id: int,
        photo_path: Path,
        message_thread_id: int | None,
        attempts_remaining: int,
    ) -> None:
        if attempts_remaining <= 0:
            self.logger.error("Telegram 图片发送重试次数已耗尽。")
            return
        if not self._wait_for_telegram_rate_limit(max_wait_seconds=900):
            self.logger.error("Telegram 限流窗口过长，放弃延迟发送图片。")
            return
        try:
            self.telegram.send_photo(
                chat_id=chat_id,
                photo_path=photo_path,
                message_thread_id=message_thread_id,
            )
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning(
                "Telegram 延迟图片发送仍被限流，%s 秒后继续重试，剩余 %s 次。",
                delay,
                attempts_remaining - 1,
            )
            self._schedule_delayed_photo_send(
                chat_id,
                photo_path,
                message_thread_id,
                attempts_remaining=attempts_remaining - 1,
            )
        except TelegramApiError:
            self.logger.exception("延迟发送 Telegram 图片失败")

    def _safe_send_photo(
        self,
        chat_id: int,
        photo_path: Path,
        message_thread_id: int | None,
        defer_on_rate_limit: bool = False,
    ) -> TelegramMessage | None:
        if not photo_path.exists() or not photo_path.is_file():
            self.logger.warning("图片文件不存在，跳过发送：%s", photo_path)
            return None
        if self._telegram_rate_limit_remaining_seconds() > 0:
            if defer_on_rate_limit:
                self._schedule_delayed_photo_send(chat_id, photo_path, message_thread_id)
            return None
        try:
            return self.telegram.send_photo(
                chat_id=chat_id,
                photo_path=photo_path,
                message_thread_id=message_thread_id,
            )
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning("Telegram 图片发送触发限流，%s 秒内暂停发送。", delay)
            if defer_on_rate_limit:
                self._schedule_delayed_photo_send(chat_id, photo_path, message_thread_id)
            return None
        except TelegramApiError:
            self.logger.exception("发送 Telegram 图片失败")
            return None

    def _safe_send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        defer_on_rate_limit: bool = False,
    ) -> TelegramMessage | None:
        if self._telegram_rate_limit_remaining_seconds() > 0:
            if defer_on_rate_limit:
                self._schedule_delayed_message_send(
                    chat_id,
                    text,
                    message_thread_id,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=parse_mode,
                )
            return None
        try:
            return self.telegram.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode,
            )
        except TelegramRateLimitError as exc:
            delay = self._remember_telegram_rate_limit(exc.retry_after_seconds)
            self.logger.warning("Telegram 消息发送触发限流，%s 秒内暂停发送。", delay)
            if defer_on_rate_limit:
                self._schedule_delayed_message_send(
                    chat_id,
                    text,
                    message_thread_id,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=parse_mode,
                )
            return None
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
