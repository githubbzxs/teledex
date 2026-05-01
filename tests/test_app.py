from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from teledex.app import (
    ActiveRun,
    IncomingMessage,
    LivePreviewState,
    TeledexApp,
    _format_elapsed_compact,
    _next_preview_deadline,
    _normalize_preview_interval,
)
from teledex.config import AppConfig
from teledex.telegram_api import TelegramApiError, TelegramMessage, TelegramRateLimitError


class _FakeThread:
    def __init__(self, target, args, daemon) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self) -> None:
        return


class AppMessagingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            telegram_bot_token="test-token",
            authorized_user_ids={1},
            state_dir=Path(self.temp_dir.name),
            poll_timeout_seconds=30,
            preview_update_interval_seconds=1.0,
            preview_edit_min_interval_seconds=0.0,
            codex_bin="codex",
            codex_exec_mode="default",
            codex_model=None,
            codex_enable_search=False,
            codex_persist_extended_history=True,
            tmux_bin="tmux",
            tmux_shell="/bin/bash",
            log_level="INFO",
        )
        self.app = TeledexApp(self.config)

    def tearDown(self) -> None:
        self.app.storage.close()
        self.temp_dir.cleanup()

    def test_safe_send_message_routes_to_discord_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token=None,
                    authorized_user_ids=set(),
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=1.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                    discord_bot_token="discord-token",
                    authorized_discord_user_ids={7},
                )
            )
        calls: list[dict[str, object]] = []

        class _FakeDiscord:
            def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None):
                calls.append(
                    {
                        "chat_id": chat_id,
                        "text": text,
                        "reply_to_message_id": reply_to_message_id,
                    }
                )
                return TelegramMessage(chat_id=chat_id, message_id=888, message_thread_id=None)

        app.discord = _FakeDiscord()  # type: ignore[assignment]

        sent = app._safe_send_message(
            chat_id=-123456789012345678,
            text="hello discord",
            message_thread_id=None,
            user_id=-7,
            platform="discord",
        )

        self.assertIsNotNone(sent)
        self.assertEqual(
            calls,
            [
                {
                    "chat_id": 123456789012345678,
                    "text": "hello discord",
                    "reply_to_message_id": None,
                }
            ],
        )
        app.storage.close()

    def test_handle_discord_message_routes_start_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token=None,
                    authorized_user_ids=set(),
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=1.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                    discord_bot_token="discord-token",
                    authorized_discord_user_ids={7},
                )
            )
        calls: list[dict[str, object]] = []

        def fake_send_message(*args, **kwargs):
            chat_id = kwargs.get("chat_id", args[0] if args else None)
            text = kwargs.get("text", args[1] if len(args) > 1 else None)
            calls.append({"chat_id": chat_id, "text": text})
            return TelegramMessage(chat_id=int(chat_id), message_id=999, message_thread_id=None)

        app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        app._handle_discord_message(
            raw_user_id=7,
            raw_chat_id=123456789012345678,
            raw_message_id=223456789012345678,
            text="/start",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["chat_id"], -123456789012345678)
        self.assertIn("teledex commands", str(calls[0]["text"]))
        app.storage.close()

    def test_handle_prompt_preview_message_does_not_reply(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "测试会话")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)

        calls: list[dict[str, object]] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "message_thread_id": message_thread_id,
                    "reply_to_message_id": reply_to_message_id,
                    "parse_mode": parse_mode,
                }
            )
            return TelegramMessage(
                chat_id=chat_id,
                message_id=321,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        incoming = IncomingMessage(
            chat_id=100,
            user_id=1,
            text="请处理这个任务",
            message_id=123,
            message_thread_id=9,
        )

        with patch("teledex.app.threading.Thread", _FakeThread):
            self.app._handle_prompt(incoming)

        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["reply_to_message_id"])
        self.assertEqual(str(calls[0]["text"]), "思考时间：00:00\n\nstatusline：○ 正在思考...")

    def test_send_run_result_deletes_preview_and_sends_new_final_message(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        deleted: list[dict[str, int]] = []
        sent: list[dict[str, object]] = []

        def fake_delete_message(**kwargs) -> None:
            deleted.append(
                {
                    "chat_id": int(kwargs["chat_id"]),
                    "message_id": int(kwargs["message_id"]),
                }
            )

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
            user_id: int | None = None,
        ) -> TelegramMessage:
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "message_thread_id": message_thread_id,
                    "reply_to_message_id": reply_to_message_id,
                    "parse_mode": parse_mode,
                }
            )
            return TelegramMessage(
                chat_id=chat_id,
                message_id=789,
                message_thread_id=message_thread_id,
            )

        self.app.telegram.delete_message = fake_delete_message  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        self.app._send_run_result(active_run, "最终回复")

        self.assertEqual(deleted, [{"chat_id": 100, "message_id": 456}])
        self.assertEqual(active_run.preview_message_id, None)
        self.assertEqual(len(sent), 1)
        self.assertIn("最终回复", str(sent[0]["text"]))
        self.assertEqual(sent[0]["parse_mode"], "HTML")
        self.assertIsNone(sent[0]["reply_to_message_id"])

    def test_send_run_result_ignores_preview_state_footer_and_only_sends_final_message(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        preview = LivePreviewState()
        preview.update_footer_statusline("gpt-5.4 default · 98% left · ~/teledex")
        sent: list[dict[str, object]] = []

        self.app.telegram.delete_message = lambda **kwargs: None  # type: ignore[method-assign]

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
            user_id: int | None = None,
        ) -> TelegramMessage:
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "message_thread_id": message_thread_id,
                    "parse_mode": parse_mode,
                }
            )
            return TelegramMessage(
                chat_id=chat_id,
                message_id=789,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        self.app._send_run_result(active_run, "最终回复", preview)

        self.assertEqual(len(sent), 1)
        self.assertIn("最终回复", str(sent[0]["text"]))
        self.assertNotIn("Completed", str(sent[0]["text"]))
        self.assertNotIn("gpt-5.4 default · 98% left · ~/teledex", str(sent[0]["text"]))
        self.assertEqual(sent[0]["parse_mode"], "HTML")

    def test_send_run_result_still_sends_final_message_when_preview_delete_fails(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        sent: list[dict[str, object]] = []

        def fake_delete_message(**kwargs) -> None:
            raise TelegramApiError("删除失败")

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
            user_id: int | None = None,
        ) -> TelegramMessage:
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "message_thread_id": message_thread_id,
                    "reply_to_message_id": reply_to_message_id,
                    "parse_mode": parse_mode,
                }
            )
            return TelegramMessage(
                chat_id=chat_id,
                message_id=789,
                message_thread_id=message_thread_id,
            )

        self.app.telegram.delete_message = fake_delete_message  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._send_run_result(active_run, "最终回复")

        self.assertEqual(len(sent), 1)
        self.assertIn("最终回复", str(sent[0]["text"]))
        self.assertEqual(active_run.preview_message_id, 456)

    def test_send_run_result_schedules_preview_delete_retry_when_rate_limited(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        scheduled: list[dict[str, object]] = []
        sent: list[str] = []

        def fake_delete_message(**kwargs) -> None:
            raise TelegramRateLimitError("限流", retry_after_seconds=12)

        def fake_schedule_delayed_preview_delete(
            active_run: ActiveRun,
            attempts_remaining: int = 2,
        ) -> None:
            scheduled.append(
                {
                    "run_id": active_run.run_id,
                    "attempts_remaining": attempts_remaining,
                }
            )

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
            user_id: int | None = None,
        ) -> TelegramMessage:
            sent.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=790,
                message_thread_id=message_thread_id,
            )

        self.app.telegram.delete_message = fake_delete_message  # type: ignore[method-assign]
        self.app._schedule_delayed_preview_delete = fake_schedule_delayed_preview_delete  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._send_run_result(active_run, "最终回复")

        self.assertEqual(
            scheduled,
            [
                {
                    "run_id": 1,
                    "attempts_remaining": 2,
                }
            ],
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("最终回复", sent[0])

    def test_edit_preview_message_pauses_when_telegram_is_rate_limited(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )

        def fake_edit_message_text(**kwargs) -> None:
            raise TelegramRateLimitError("限流", retry_after_seconds=12)

        self.app.telegram.edit_message_text = fake_edit_message_text  # type: ignore[method-assign]

        updated = self.app._edit_preview_message(active_run, "预览内容")

        self.assertFalse(updated)
        self.assertGreater(self.app._telegram_rate_limit_remaining_seconds(), 0)

    def test_edit_preview_message_respects_local_min_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=5.0,
                    preview_edit_min_interval_seconds=5.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
            preview_last_edit_at=10.0,
        )
        calls: list[str] = []

        def fake_edit_message_text(**kwargs) -> None:
            calls.append(str(kwargs["text"]))

        app.telegram.edit_message_text = fake_edit_message_text  # type: ignore[method-assign]

        with patch("teledex.app.time.monotonic", return_value=12.0):
            updated = app._edit_preview_message(active_run, "预览内容")

        self.assertFalse(updated)
        self.assertEqual(calls, [])

        with patch("teledex.app.time.monotonic", return_value=15.1):
            updated = app._edit_preview_message(active_run, "预览内容")

        self.assertTrue(updated)
        self.assertEqual(calls, ["预览内容"])

    def test_edit_preview_message_respects_global_min_interval_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=1.0,
                    preview_edit_min_interval_seconds=5.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )
        first_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务一",
            preview_message_id=456,
        )
        second_run = ActiveRun(
            run_id=2,
            session_id=2,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务二",
            preview_message_id=789,
        )
        calls: list[str] = []

        def fake_edit_message_text(**kwargs) -> None:
            calls.append(f"{kwargs['message_id']}:{kwargs['text']}")

        app.telegram.edit_message_text = fake_edit_message_text  # type: ignore[method-assign]

        with patch("teledex.app.time.monotonic", return_value=10.0):
            self.assertTrue(app._edit_preview_message(first_run, "预览一"))
        with patch("teledex.app.time.monotonic", return_value=12.0):
            self.assertFalse(app._edit_preview_message(second_run, "预览二"))
        with patch("teledex.app.time.monotonic", return_value=15.1):
            self.assertTrue(app._edit_preview_message(second_run, "预览二"))

        self.assertEqual(calls, ["456:预览一", "789:预览二"])

    def test_send_run_result_bypasses_local_preview_interval_for_final_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=1.0,
                    preview_edit_min_interval_seconds=5.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
            preview_last_edit_at=10.0,
        )
        preview = LivePreviewState()
        calls: list[str] = []

        def fake_delete_message(**kwargs) -> None:
            calls.append(f"delete:{kwargs['message_id']}")

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
            user_id: int | None = None,
        ) -> TelegramMessage:
            calls.append(str(text))
            return TelegramMessage(
                chat_id=chat_id,
                message_id=800,
                message_thread_id=message_thread_id,
            )

        app.telegram.delete_message = fake_delete_message  # type: ignore[method-assign]
        app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        with patch("teledex.app.time.monotonic", return_value=12.0):
            app._send_run_result(active_run, "最终回复", preview)

        self.assertEqual(calls[0], "delete:456")
        self.assertIn("最终回复", calls[1])

    def test_send_run_result_splits_long_final_message_and_keeps_repo_file_links(self) -> None:
        repo_dir = Path(self.temp_dir.name) / "repo"
        file_path = repo_dir / "src" / "demo" / "app.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("print('hello')\n", encoding="utf-8")

        init_result = subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if init_result.returncode != 0:
            subprocess.run(
                ["git", "init"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "checkout", "-B", "main"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/demo.git"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "demo")
        self.app.storage.bind_session_path(session.id, 1, str(repo_dir))
        active_run = ActiveRun(
            run_id=1,
            session_id=session.id,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        sent: list[dict[str, object]] = []

        self.app.telegram.delete_message = lambda **kwargs: None  # type: ignore[method-assign]

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
            user_id: int | None = None,
            platform: str | None = None,
        ) -> TelegramMessage:
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "message_thread_id": message_thread_id,
                    "reply_to_message_id": reply_to_message_id,
                    "parse_mode": parse_mode,
                    "platform": platform,
                }
            )
            return TelegramMessage(
                chat_id=chat_id,
                message_id=789 + len(sent),
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        long_text = (
            f"查看文件：[app.py]({file_path}#L1)\n\n"
            + ("这是一段用于触发 Telegram 长消息分片的说明。\n" * 220)
        )

        self.app._send_run_result(active_run, long_text)

        self.assertGreater(len(sent), 1)
        self.assertTrue(all(item["parse_mode"] == "HTML" for item in sent))
        self.assertIn(
            '<a href="https://github.com/example/demo/blob/main/src/demo/app.py#L1">app.py</a>',
            str(sent[0]["text"]),
        )
        self.assertNotIn("[Truncated for length]", "".join(str(item["text"]) for item in sent))

    def test_build_final_result_message_renders_repo_file_reference_as_github_link(self) -> None:
        repo_dir = Path(self.temp_dir.name) / "repo"
        file_path = repo_dir / "src" / "demo" / "app.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("print('hello')\n", encoding="utf-8")

        init_result = subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if init_result.returncode != 0:
            subprocess.run(
                ["git", "init"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "checkout", "-B", "main"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/demo.git"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "demo")
        self.app.storage.bind_session_path(session.id, 1, str(repo_dir))

        final_text, parse_mode = self.app._build_final_result_message(
            f"查看文件：[app.py]({file_path}#L1)",
            session_id=session.id,
        )

        self.assertEqual(parse_mode, "HTML")
        self.assertIn(
            '<a href="https://github.com/example/demo/blob/main/src/demo/app.py#L1">app.py</a>',
            final_text,
        )

    def test_safe_send_message_can_schedule_retry_when_rate_limited(self) -> None:
        def fake_send_message(**kwargs) -> TelegramMessage:
            raise TelegramRateLimitError("限流", retry_after_seconds=15)

        self.app.telegram.send_message = fake_send_message  # type: ignore[method-assign]

        result = self.app._safe_send_message(
            100,
            "最终回复",
            9,
            parse_mode="HTML",
            defer_on_rate_limit=True,
            user_id=1,
        )

        self.assertIsNone(result)
        pending = self.app.storage.list_due_pending_telegram_messages(
            due_before=(datetime.now(tz=UTC) + timedelta(minutes=1)).isoformat(timespec="seconds"),
            limit=10,
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].user_id, 1)
        self.assertEqual(pending[0].chat_id, 100)
        self.assertEqual(pending[0].text, "最终回复")
        self.assertEqual(pending[0].parse_mode, "HTML")

    def test_process_pending_telegram_messages_sends_due_message(self) -> None:
        sent: list[dict[str, object]] = []
        pending_id = self.app.storage.enqueue_pending_telegram_message(
            user_id=1,
            chat_id=100,
            text="补发结果",
            message_thread_id=9,
            reply_to_message_id=None,
            parse_mode="HTML",
            due_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        )

        def fake_send_message(**kwargs) -> TelegramMessage:
            sent.append(kwargs)
            return TelegramMessage(
                chat_id=int(kwargs["chat_id"]),
                message_id=801,
                message_thread_id=int(kwargs["message_thread_id"]),
            )

        self.app.telegram.send_message = fake_send_message  # type: ignore[method-assign]

        processed = self.app._process_pending_telegram_messages_once()

        self.assertTrue(processed)
        self.assertEqual(len(sent), 1)
        self.assertEqual(str(sent[0]["text"]), "补发结果")
        self.assertIsNone(self.app.storage.get_next_pending_telegram_message_due_at())
        row = self.app.storage._conn.execute(
            "SELECT 1 FROM pending_telegram_messages WHERE id = ?",
            (pending_id,),
        ).fetchone()
        self.assertIsNone(row)

    def test_process_pending_telegram_messages_reschedules_when_rate_limited(self) -> None:
        pending_id = self.app.storage.enqueue_pending_telegram_message(
            user_id=1,
            chat_id=100,
            text="补发结果",
            message_thread_id=9,
            reply_to_message_id=None,
            parse_mode=None,
            due_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        )

        def fake_send_message(**kwargs) -> TelegramMessage:
            raise TelegramRateLimitError("限流", retry_after_seconds=20)

        self.app.telegram.send_message = fake_send_message  # type: ignore[method-assign]

        processed = self.app._process_pending_telegram_messages_once()

        self.assertTrue(processed)
        row = self.app.storage._conn.execute(
            "SELECT due_at FROM pending_telegram_messages WHERE id = ?",
            (pending_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        due_at = datetime.fromisoformat(str(row["due_at"]))
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        self.assertGreaterEqual(
            due_at,
            datetime.now(tz=UTC) + timedelta(seconds=20),
        )

    def test_safe_delete_preview_message_can_schedule_retry_when_rate_limited(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def fake_delete_message(**kwargs) -> None:
            raise TelegramRateLimitError("限流", retry_after_seconds=15)

        def fake_schedule_delayed_preview_delete(*args, **kwargs) -> None:
            calls.append((args, kwargs))

        self.app.telegram.delete_message = fake_delete_message  # type: ignore[method-assign]
        self.app._schedule_delayed_preview_delete = fake_schedule_delayed_preview_delete  # type: ignore[method-assign]

        result = self.app._safe_delete_preview_message(
            active_run,
            defer_on_rate_limit=True,
        )

        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0].run_id, 1)

    def test_handle_prompt_allows_other_session_to_run_in_parallel(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session_1 = self.app.storage.create_session(1, "会话一")
        session_2 = self.app.storage.create_session(1, "会话二")
        self.app.storage.bind_session_path(session_1.id, 1, self.temp_dir.name)
        self.app.storage.bind_session_path(session_2.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session_2.id, chat_id=100, message_thread_id=9)
        self.app._active_runs[session_1.id] = ActiveRun(
            run_id=1,
            session_id=session_1.id,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="session 1",
            preview_message_id=111,
        )

        calls: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=654,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        incoming = IncomingMessage(
            chat_id=100,
            user_id=1,
            text="并行跑第二个会话",
            message_id=321,
            message_thread_id=9,
        )

        with patch("teledex.app.threading.Thread", _FakeThread):
            self.app._handle_prompt(incoming)

        self.assertEqual(calls, ["思考时间：00:00\n\nstatusline：○ 正在思考..."])
        self.assertIn(session_1.id, self.app._active_runs)
        self.assertIn(session_2.id, self.app._active_runs)

    def test_handle_prompt_interrupts_current_run_for_follow_up_message_in_same_session(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "会话一")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)
        current_handle = object()
        self.app._active_runs[session.id] = ActiveRun(
            run_id=1,
            session_id=session.id,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="第一条消息",
            preview_message_id=111,
            process_handle=current_handle,  # type: ignore[arg-type]
        )

        calls: list[str] = []
        terminated_handles: list[object] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=654,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        self.app.runner.terminate = terminated_handles.append  # type: ignore[method-assign]
        self.app._handle_prompt(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text="第二条消息",
                message_id=322,
                message_thread_id=9,
            )
        )

        self.assertEqual(calls, ["思考时间：00:00\n\nstatusline：○ 正在思考..."])
        self.assertIn(session.id, self.app._active_runs)
        self.assertTrue(self.app._active_runs[session.id].stop_requested)
        self.assertTrue(self.app._active_runs[session.id].superseded_by_follow_up)
        self.assertEqual(len(self.app._queued_runs.get(session.id, [])), 1)
        self.assertEqual(self.app._queued_runs[session.id][0].prompt, "第二条消息")
        self.assertEqual(terminated_handles, [current_handle])

    def test_handle_prompt_requires_bound_session_in_new_chat_context(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "会话一")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)

        calls: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=655,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        incoming = IncomingMessage(
            chat_id=100,
            user_id=1,
            text="这是一个全新聊天里的第一句话",
            message_id=322,
            message_thread_id=10,
        )

        with patch("teledex.app.threading.Thread", _FakeThread):
            self.app._handle_prompt(incoming)

        self.assertEqual(calls, ["No directory is bound yet. Use /bind <absolute-path> first."])
        self.assertNotIn(session.id, self.app._active_runs)

    def test_legacy_session_commands_return_migration_hint(self) -> None:
        calls: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=111,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._handle_command(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text="/tnew",
                message_id=1,
                message_thread_id=9,
            )
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            "That management command has been removed. Use /bind <absolute-path> instead. A session will be created automatically if needed, or switched if the directory is already bound.",
        )

    def test_bind_updates_session_name_to_bound_path(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "会话一")
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)
        calls: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=222,
                message_thread_id=message_thread_id,
            )

        self.app.runner.reset_terminal = lambda session_id, cwd=None: None  # type: ignore[method-assign]
        self.app.runner.ensure_terminal = lambda session_id, cwd: "teledex-1"  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._handle_command(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text=f"/bind {self.temp_dir.name}",
                message_id=2,
                message_thread_id=9,
            )
        )

        updated = self.app.storage.get_session(session.id, 1)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.title, Path(self.temp_dir.name).name)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"Current name: {Path(self.temp_dir.name).name}", calls[0])

    def test_bind_creates_new_session_when_binding_different_directory(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        current_dir = tempfile.TemporaryDirectory()
        self.addCleanup(current_dir.cleanup)
        session = self.app.storage.create_session(1, "会话一")
        self.app.storage.bind_session_path(session.id, 1, current_dir.name)
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)
        calls: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=333,
                message_thread_id=message_thread_id,
            )

        self.app.runner.reset_terminal = lambda session_id, cwd=None: None  # type: ignore[method-assign]
        self.app.runner.ensure_terminal = lambda session_id, cwd: "teledex-test-123456"  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._handle_command(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text=f"/bind {self.temp_dir.name}",
                message_id=2,
                message_thread_id=9,
            )
        )

        sessions = self.app.storage.list_sessions(1)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].bound_path, current_dir.name)
        self.assertEqual(sessions[1].bound_path, self.temp_dir.name)
        self.assertEqual(sessions[1].title, Path(self.temp_dir.name).name)
        active = self.app.storage.get_active_session(1, chat_id=100, message_thread_id=9)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.id, sessions[1].id)
        self.assertIn(f"Created session #{sessions[1].id}", calls[0])

    def test_sync_bot_commands_registers_management_commands(self) -> None:
        commands: list[tuple[tuple[str, str], ...]] = []

        def fake_set_my_commands(values: tuple[tuple[str, str], ...]) -> None:
            commands.append(values)

        self.app.telegram.set_my_commands = fake_set_my_commands  # type: ignore[method-assign]

        self.app._sync_bot_commands()

        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0],
            (
                ("start", "Show help"),
                ("bind", "Bind directory"),
                ("stop", "Stop task"),
            ),
        )

    def test_handle_update_treats_double_slash_as_codex_prompt(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        prompts: list[str] = []
        commands: list[str] = []

        def fake_handle_prompt(incoming: IncomingMessage) -> None:
            prompts.append(incoming.text)

        def fake_handle_command(incoming: IncomingMessage) -> None:
            commands.append(incoming.text)

        self.app._handle_prompt = fake_handle_prompt  # type: ignore[method-assign]
        self.app._handle_command = fake_handle_command  # type: ignore[method-assign]

        self.app._handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 123,
                    "text": "//status",
                    "from": {"id": 1},
                    "chat": {"id": 100},
                    "message_thread_id": 9,
                },
            }
        )

        self.assertEqual(prompts, ["/status"])
        self.assertEqual(commands, [])

    def test_handle_prompt_uses_thread_scoped_active_session(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session_1 = self.app.storage.create_session(1, "会话一")
        session_2 = self.app.storage.create_session(1, "会话二")
        self.app.storage.bind_session_path(session_1.id, 1, self.temp_dir.name)
        self.app.storage.bind_session_path(session_2.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session_1.id, chat_id=100, message_thread_id=9)
        self.app.storage.set_active_session(1, session_2.id, chat_id=100, message_thread_id=10)
        self.app._active_runs[session_2.id] = ActiveRun(
            run_id=2,
            session_id=session_2.id,
            user_id=1,
            chat_id=100,
            message_thread_id=10,
            prompt="session 2",
            preview_message_id=222,
        )

        calls: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            calls.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=987,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        incoming = IncomingMessage(
            chat_id=100,
            user_id=1,
            text="在话题 9 继续",
            message_id=999,
            message_thread_id=9,
        )

        with patch("teledex.app.threading.Thread", _FakeThread):
            self.app._handle_prompt(incoming)

        self.assertEqual(calls, ["思考时间：00:00\n\nstatusline：○ 正在思考..."])
        self.assertIn(session_1.id, self.app._active_runs)
        self.assertIn(session_2.id, self.app._active_runs)

    def test_handle_update_routes_new_command_to_codex_command_handler(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        prompts: list[str] = []
        commands: list[str] = []
        codex_commands: list[str] = []

        def fake_handle_prompt(incoming: IncomingMessage) -> None:
            prompts.append(incoming.text)

        def fake_handle_command(incoming: IncomingMessage) -> None:
            commands.append(incoming.text)

        def fake_handle_codex_command(incoming: IncomingMessage) -> None:
            codex_commands.append(incoming.text)

        self.app._handle_prompt = fake_handle_prompt  # type: ignore[method-assign]
        self.app._handle_command = fake_handle_command  # type: ignore[method-assign]
        self.app._handle_codex_command = fake_handle_codex_command  # type: ignore[method-assign]

        self.app._handle_update(
            {
                "update_id": 2,
                "message": {
                    "message_id": 456,
                    "text": "/new",
                    "from": {"id": 1},
                    "chat": {"id": 100},
                    "message_thread_id": 9,
                },
            }
        )

        self.assertEqual(prompts, [])
        self.assertEqual(commands, [])
        self.assertEqual(codex_commands, ["/new"])

    def test_handle_update_routes_other_builtin_codex_command_to_codex_handler(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        prompts: list[str] = []
        commands: list[str] = []
        codex_commands: list[str] = []

        def fake_handle_prompt(incoming: IncomingMessage) -> None:
            prompts.append(incoming.text)

        def fake_handle_command(incoming: IncomingMessage) -> None:
            commands.append(incoming.text)

        def fake_handle_codex_command(incoming: IncomingMessage) -> None:
            codex_commands.append(incoming.text)

        self.app._handle_prompt = fake_handle_prompt  # type: ignore[method-assign]
        self.app._handle_command = fake_handle_command  # type: ignore[method-assign]
        self.app._handle_codex_command = fake_handle_codex_command  # type: ignore[method-assign]

        self.app._handle_update(
            {
                "update_id": 3,
                "message": {
                    "message_id": 457,
                    "text": "/model",
                    "from": {"id": 1},
                    "chat": {"id": 100},
                    "message_thread_id": 9,
                },
            }
        )

        self.assertEqual(prompts, [])
        self.assertEqual(commands, [])
        self.assertEqual(codex_commands, ["/model"])

    def test_handle_update_keeps_unknown_non_builtin_slash_command_as_prompt(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        prompts: list[str] = []
        commands: list[str] = []
        codex_commands: list[str] = []

        def fake_handle_prompt(incoming: IncomingMessage) -> None:
            prompts.append(incoming.text)

        def fake_handle_command(incoming: IncomingMessage) -> None:
            commands.append(incoming.text)

        def fake_handle_codex_command(incoming: IncomingMessage) -> None:
            codex_commands.append(incoming.text)

        self.app._handle_prompt = fake_handle_prompt  # type: ignore[method-assign]
        self.app._handle_command = fake_handle_command  # type: ignore[method-assign]
        self.app._handle_codex_command = fake_handle_codex_command  # type: ignore[method-assign]

        self.app._handle_update(
            {
                "update_id": 4,
                "message": {
                    "message_id": 458,
                    "text": "/not-a-real-codex-command",
                    "from": {"id": 1},
                    "chat": {"id": 100},
                    "message_thread_id": 9,
                },
            }
        )

        self.assertEqual(prompts, ["/not-a-real-codex-command"])
        self.assertEqual(commands, [])
        self.assertEqual(codex_commands, [])

    def test_handle_update_routes_local_management_command(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        prompts: list[str] = []
        commands: list[str] = []

        def fake_handle_prompt(incoming: IncomingMessage) -> None:
            prompts.append(incoming.text)

        def fake_handle_command(incoming: IncomingMessage) -> None:
            commands.append(incoming.text)

        self.app._handle_prompt = fake_handle_prompt  # type: ignore[method-assign]
        self.app._handle_command = fake_handle_command  # type: ignore[method-assign]

        self.app._handle_update(
            {
                "update_id": 3,
                "message": {
                    "message_id": 789,
                    "text": "/bind /root/demo",
                    "from": {"id": 1},
                    "chat": {"id": 100},
                    "message_thread_id": 9,
                },
            }
        )

        self.assertEqual(prompts, [])
        self.assertEqual(commands, ["/bind /root/demo"])

    def test_handle_update_skips_duplicate_processed_message(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        prompts: list[str] = []

        def fake_handle_prompt(incoming: IncomingMessage) -> None:
            prompts.append(incoming.text)

        self.app._handle_prompt = fake_handle_prompt  # type: ignore[method-assign]
        update = {
            "update_id": 30,
            "message": {
                "message_id": 901,
                "text": "重复消息",
                "from": {"id": 1},
                "chat": {"id": 100},
                "message_thread_id": 9,
            },
        }

        self.app._handle_update(update)
        self.app._handle_update(update)

        self.assertEqual(prompts, ["重复消息"])

    def test_app_init_recovers_interrupted_runs_and_reads_saved_offset(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "会话")
        self.app.storage.update_session_status(session.id, "running")
        run_id = self.app.storage.create_run(session.id, 1, "未完成任务")
        self.app.storage.set_telegram_update_offset(88)
        self.app.storage.close()

        recovered_app = TeledexApp(self.config)
        try:
            self.assertEqual(recovered_app._update_offset, 88)
            recovered_session = recovered_app.storage.get_session(session.id, 1)
            self.assertIsNotNone(recovered_session)
            assert recovered_session is not None
            self.assertEqual(recovered_session.status, "idle")
            row = recovered_app.storage._conn.execute(
                "SELECT status, error_message, ended_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            self.assertEqual(row["status"], "stopped")
            self.assertEqual(row["error_message"], "服务重启，已回收未完成任务")
            self.assertIsNotNone(row["ended_at"])
        finally:
            recovered_app.storage.close()

    def test_handle_codex_new_command_clears_current_thread_binding(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "teledex")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)
        self.app.storage.update_session_thread_id(session.id, "thread-123")
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)

        messages: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            messages.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=999,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        self.app._handle_codex_command(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text="/new",
                message_id=123,
                message_thread_id=9,
            )
        )

        updated = self.app.storage.get_session(session.id, 1)
        assert updated is not None
        self.assertIsNone(updated.codex_thread_id)
        self.assertEqual(updated.bound_path, self.temp_dir.name)
        self.assertEqual(messages, [f"Started a new Codex conversation in session #{session.id}.\nDirectory unchanged: {self.temp_dir.name}"])

    def test_handle_codex_new_command_rejects_running_session(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "teledex")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)
        self.app.storage.update_session_thread_id(session.id, "thread-123")
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)
        self.app._active_runs[session.id] = ActiveRun(
            run_id=1,
            session_id=session.id,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
        )

        messages: list[str] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            messages.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=1000,
                message_thread_id=message_thread_id,
            )

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        self.app._handle_codex_command(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text="/new",
                message_id=124,
                message_thread_id=9,
            )
        )

        updated = self.app.storage.get_session(session.id, 1)
        assert updated is not None
        self.assertEqual(updated.codex_thread_id, "thread-123")
        self.assertEqual(
            messages,
            [f"Session #{session.id} is running. /new is unavailable until it finishes, or stop it first with /stop."],
        )

    def test_legacy_twipe_command_does_not_clear_user_state(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "teledex")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session.id, chat_id=100, message_thread_id=9)
        self.app.storage.create_run(session.id, 1, "测试任务")
        runtime_dir = self.config.state_dir / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        stale_file = runtime_dir / "stale.txt"
        stale_file.write_text("old", encoding="utf-8")

        messages: list[str] = []
        reset_calls: list[tuple[int, str]] = []

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
        ) -> TelegramMessage:
            messages.append(text)
            return TelegramMessage(
                chat_id=chat_id,
                message_id=1001,
                message_thread_id=message_thread_id,
            )

        def fake_reset_terminal(session_id: int, cwd: Path | None = None) -> None:
            reset_calls.append((session_id, str(cwd) if cwd is not None else ""))

        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]
        self.app.runner.reset_terminal = fake_reset_terminal  # type: ignore[method-assign]
        self.app._handle_command(
            IncomingMessage(
                chat_id=100,
                user_id=1,
                text="/twipe",
                message_id=125,
                message_thread_id=9,
            )
        )

        self.assertEqual(reset_calls, [])
        self.assertEqual(len(self.app.storage.list_sessions(1)), 1)
        self.assertTrue(stale_file.exists())
        self.assertEqual(
            messages,
            [
                "That management command has been removed."
            ],
        )


class LivePreviewStateTestCase(unittest.TestCase):
    def test_format_elapsed_compact_uses_minute_granularity(self) -> None:
        self.assertEqual(_format_elapsed_compact(0), "0m")
        self.assertEqual(_format_elapsed_compact(59), "0m")
        self.assertEqual(_format_elapsed_compact(60), "1m")
        self.assertEqual(_format_elapsed_compact(3660), "1h 01m")

    def test_preview_deadline_catches_up_without_accumulating_drift(self) -> None:
        self.assertEqual(_normalize_preview_interval(0.0), 0.2)
        self.assertEqual(_normalize_preview_interval(1.0), 1.0)
        self.assertEqual(_next_preview_deadline(10.0, 10.2, 1.0), 11.0)
        self.assertEqual(_next_preview_deadline(10.0, 12.3, 1.0), 13.0)

    def test_status_line_tracks_elapsed_with_circle_animation(self) -> None:
        preview = LivePreviewState(initial_status="Thinking")

        self.assertEqual(preview.render(), "思考时间：00:00\n\nstatusline：○ 正在思考...")
        self.assertEqual(preview.advance(animate_steps=1, elapsed_seconds=0), "思考时间：00:00\n\nstatusline：● 正在思考...")
        self.assertEqual(preview.advance(animate_steps=1, elapsed_seconds=60), "思考时间：01:00\n\nstatusline：○ 正在思考...")

    def test_status_line_can_catch_up_multiple_elapsed_seconds(self) -> None:
        preview = LivePreviewState(initial_status="Thinking")

        self.assertEqual(preview.advance(animate_steps=3, elapsed_seconds=180), "思考时间：03:00\n\nstatusline：● 正在思考...")

    def test_status_line_accumulates_elapsed_even_when_animation_is_paused(self) -> None:
        preview = LivePreviewState(initial_status="Thinking")

        self.assertEqual(preview.advance(animate_steps=0, elapsed_seconds=60), "思考时间：01:00\n\nstatusline：○ 正在思考...")

    def test_stream_text_is_rendered_immediately(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("abcdef")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n输出预览：\nabcdef\n\nstatusline：○ 正在输出...",
        )

    def test_commentary_history_appends_instead_of_replacing(self) -> None:
        preview = LivePreviewState()

        preview.update_commentary("msg_1", "先看目录")
        preview.update_commentary("msg_2", "再检查配置")
        preview.update_status("Thinking")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n思考过程：\n先看目录\n\n再检查配置\n\nstatusline：○ 正在思考...",
        )

    def test_command_output_is_hidden_from_preview(self) -> None:
        preview = LivePreviewState()
        preview.update_tool_state(
            "call_1",
            command_text="/bin/bash -lc 'pwd'",
            output_text="first line\nsecond line",
        )

        self.assertEqual(preview.render(), "思考时间：00:00\n\nstatusline：○ 正在准备会话...")

    def test_preview_hides_fenced_code_blocks_in_commentary(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary(
            "msg_1",
            "先检查逻辑\n\n```python\nprint('hello')\n```\n\n再继续",
        )

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n思考过程：\n先检查逻辑\n\n再继续\n\nstatusline：○ 正在准备会话...",
        )

    def test_preview_replaces_code_only_commentary_with_generic_status(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary(
            "msg_1",
            "```python\nprint('hello')\n```",
        )

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n思考过程：\n正在梳理实现细节\n\nstatusline：○ 正在准备会话...",
        )

    def test_preview_hides_fenced_code_blocks_in_stream_text(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("先说明\n\n```ts\nconst a = 1;\n```\n\n后说明")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n输出预览：\n先说明\n\n后说明\n\nstatusline：○ 正在输出...",
        )

    def test_complete_keeps_final_status_line(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("完成内容")

        self.assertEqual(
            preview.complete(),
            "思考时间：00:00\n\n输出预览：\n完成内容\n\nstatusline：● 已完成",
        )

    def test_commentary_can_be_kept_until_final_answer_starts(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("reasoning:item_1", "**Thinking**\n\nChecking files")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n思考过程：\n**Thinking**\n\nChecking files\n\nstatusline：○ 正在准备会话...",
        )

    def test_collaboration_active_hides_commentary_and_tool_state(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先看目录")
        preview.update_tool_state("call_1", command_text="pwd", output_text="/root/teledex")
        preview.set_collaboration_active(True)

        self.assertEqual(preview.render(), "思考时间：00:00\n\nstatusline：○ 正在准备会话...")

    def test_collaboration_active_keeps_final_stream_visible(self) -> None:
        preview = LivePreviewState()
        preview.set_collaboration_active(True)
        preview.update_commentary("msg_1", "这段不该显示")
        preview.update_stream_text("主线程最终输出")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n输出预览：\n主线程最终输出\n\nstatusline：○ 正在输出...",
        )

    def test_footer_statusline_renders_at_bottom(self) -> None:
        preview = LivePreviewState()
        preview.update_footer_statusline("gpt-5.4 default · 100% left · ~/teledex")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\nstatusline：○ 正在准备会话...\n\ngpt-5.4 default · 100% left · ~/teledex",
        )

    def test_final_stream_clears_transient_sections(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先检查 README")
        preview.update_tool_state("call_1", command_text="cat README.md", output_text="hello")
        preview.update_stream_text("最终输出")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n输出预览：\n最终输出\n\nstatusline：○ 正在输出...",
        )

    def test_complete_clears_transient_sections_and_keeps_final_output(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先检查 README")
        preview.update_tool_state("call_1", command_text="cat README.md", output_text="hello")
        preview.update_stream_text("最终输出")

        self.assertEqual(preview.complete(), "思考时间：00:00\n\n输出预览：\n最终输出\n\nstatusline：● 已完成")

    def test_commentary_completed_keeps_process_text_before_final_output(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先检查 README")
        preview.clear_commentary("msg_1")

        self.assertEqual(
            preview.render(),
            "思考时间：00:00\n\n思考过程：\n先检查 README\n\nstatusline：○ 正在准备会话...",
        )

    def test_collaboration_delta_from_session_log_line_uses_parent_thread_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=1.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )

            spawn_line = (
                '{"type":"event_msg","payload":{"type":"collab_agent_spawn_end",'
                '"sender_thread_id":"thread-parent"}}'
            )
            close_line = (
                '{"type":"event_msg","payload":{"type":"collab_close_end",'
                '"sender_thread_id":"thread-parent"}}'
            )
            other_line = (
                '{"type":"event_msg","payload":{"type":"collab_agent_spawn_end",'
                '"sender_thread_id":"thread-other"}}'
            )

            self.assertEqual(
                app._collaboration_delta_from_session_log_line(spawn_line, "thread-parent"),
                1,
            )
            self.assertEqual(
                app._collaboration_delta_from_session_log_line(close_line, "thread-parent"),
                -1,
            )
            self.assertEqual(
                app._collaboration_delta_from_session_log_line(other_line, "thread-parent"),
                0,
            )

            app.storage.close()

    def test_drain_preview_stream_retries_when_preview_edit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=1.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先看目录")
        attempts: list[str] = []

        def fake_update_preview(
            active_run: ActiveRun,
            text: str,
            prefer_html: bool = False,
        ) -> bool:
            attempts.append(text)
            return len(attempts) >= 2

        app._update_preview = fake_update_preview  # type: ignore[method-assign]
        app._drain_preview_stream(active_run, preview)

        self.assertGreaterEqual(len(attempts), 2)
        self.assertFalse(preview.has_pending_stream())

    def test_preview_loop_flushes_pending_stream_without_waiting_for_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=60.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "实时过程")
        class _StopEvent:
            def __init__(self) -> None:
                self._set = False

            def is_set(self) -> bool:
                return self._set

            def wait(self, timeout: float) -> bool:
                self._set = True
                return True

            def set(self) -> None:
                self._set = True

        stop_event = _StopEvent()
        attempts: list[str] = []

        def fake_update_preview(
            active_run: ActiveRun,
            text: str,
            prefer_html: bool = False,
        ) -> bool:
            attempts.append(text)
            stop_event.set()
            return True

        app._update_preview = fake_update_preview  # type: ignore[method-assign]
        app._safe_send_chat_action = lambda *args, **kwargs: None  # type: ignore[method-assign]
        app._run_preview_loop(active_run, preview, stop_event)  # type: ignore[arg-type]

        self.assertEqual(attempts, ["思考时间：00:00\n\n思考过程：\n实时过程\n\nstatusline：○ 正在准备会话..."])
        self.assertFalse(preview.has_pending_stream())

    def test_preview_loop_does_not_advance_animation_while_flushing_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=60.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )

        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        preview = LivePreviewState()
        preview.advance(animate_steps=1, elapsed_seconds=0)
        preview.update_commentary("msg_1", "继续思考")

        class _StopEvent:
            def __init__(self) -> None:
                self._set = False

            def is_set(self) -> bool:
                return self._set

            def wait(self, timeout: float) -> bool:
                self._set = True
                return True

            def set(self) -> None:
                self._set = True

        stop_event = _StopEvent()
        attempts: list[str] = []

        def fake_update_preview(
            active_run: ActiveRun,
            text: str,
            prefer_html: bool = False,
        ) -> bool:
            attempts.append(text)
            stop_event.set()
            return True

        app._update_preview = fake_update_preview  # type: ignore[method-assign]
        app._safe_send_chat_action = lambda *args, **kwargs: None  # type: ignore[method-assign]
        app._run_preview_loop(active_run, preview, stop_event)  # type: ignore[arg-type]

        self.assertEqual(attempts, ["思考时间：00:00\n\n思考过程：\n继续思考\n\nstatusline：● 正在准备会话..."])

    def test_preview_loop_skips_typing_when_preview_message_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = TeledexApp(
                AppConfig(
                    telegram_bot_token="test-token",
                    authorized_user_ids={1},
                    state_dir=Path(temp_dir),
                    poll_timeout_seconds=30,
                    preview_update_interval_seconds=60.0,
                    preview_edit_min_interval_seconds=0.0,
                    codex_bin="codex",
                    codex_exec_mode="default",
                    codex_model=None,
                    codex_enable_search=False,
                    codex_persist_extended_history=True,
                    tmux_bin="tmux",
                    tmux_shell="/bin/bash",
                    log_level="INFO",
                )
            )

        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "实时过程")

        class _StopEvent:
            def __init__(self) -> None:
                self._set = False

            def is_set(self) -> bool:
                return self._set

            def wait(self, timeout: float) -> bool:
                self._set = True
                return True

            def set(self) -> None:
                self._set = True

        stop_event = _StopEvent()
        chat_actions: list[str] = []

        def fake_update_preview(
            active_run: ActiveRun,
            text: str,
            prefer_html: bool = False,
        ) -> bool:
            stop_event.set()
            return True

        def fake_send_chat_action(*args, **kwargs) -> None:
            chat_actions.append("typing")

        app._update_preview = fake_update_preview  # type: ignore[method-assign]
        app._safe_send_chat_action = fake_send_chat_action  # type: ignore[method-assign]
        app._run_preview_loop(active_run, preview, stop_event)  # type: ignore[arg-type]

        self.assertEqual(chat_actions, [])

    def test_final_html_only_renders_final_answer_markdown(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("## 标题\n\n- 列表项\n\n**加粗**")
        preview.complete()

        rendered = preview.render_final_html()

        self.assertIn("statusline：● 已完成", rendered)
        self.assertIn("<b>标题</b>", rendered)
        self.assertIn("• 列表项", rendered)
        self.assertIn("<b>加粗</b>", rendered)

    def test_render_clips_preview_to_telegram_safe_length(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "A" * 2200)
        preview.update_tool_state("call_1", command_text="cmd", output_text="B" * 2200)
        preview.update_stream_text("C" * 2200)

        rendered = preview.render()

        self.assertLessEqual(len(rendered), 3800)
        self.assertNotIn("cmd", rendered)
        self.assertNotIn("BBB", rendered)


if __name__ == "__main__":
    unittest.main()
