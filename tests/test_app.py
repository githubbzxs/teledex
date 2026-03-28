from __future__ import annotations

import tempfile
import unittest
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
from teledex.telegram_api import TelegramMessage, TelegramRateLimitError


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
        self.temp_dir.cleanup()

    def test_handle_prompt_preview_message_does_not_reply(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session = self.app.storage.create_session(1, "测试会话")
        self.app.storage.bind_session_path(session.id, 1, self.temp_dir.name)

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
        self.assertEqual(str(calls[0]["text"]), "○ Thinking (0m)")

    def test_send_run_result_never_replies_to_preview_message(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        calls: list[dict[str, object]] = []

        def fake_edit_preview_message(
            active_run: ActiveRun,
            text: str,
            parse_mode: str | None = None,
            respect_local_interval: bool = True,
        ) -> bool:
            calls.append(
                {
                    "chat_id": active_run.chat_id,
                    "text": text,
                    "message_thread_id": active_run.message_thread_id,
                    "parse_mode": parse_mode,
                }
            )
            return True

        self.app._edit_preview_message = fake_edit_preview_message  # type: ignore[method-assign]
        self.app._safe_send_message = lambda *args, **kwargs: None  # type: ignore[method-assign]
        self.app._send_run_result(active_run, "最终回复")

        self.assertEqual(len(calls), 1)
        self.assertIn("最终回复", str(calls[0]["text"]))
        self.assertEqual(calls[0]["parse_mode"], "HTML")

    def test_send_run_result_keeps_footer_statusline_when_preview_state_is_present(self) -> None:
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
        calls: list[dict[str, object]] = []

        def fake_edit_preview_message(
            active_run: ActiveRun,
            text: str,
            parse_mode: str | None = None,
            respect_local_interval: bool = True,
        ) -> bool:
            calls.append(
                {
                    "chat_id": active_run.chat_id,
                    "text": text,
                    "message_thread_id": active_run.message_thread_id,
                    "parse_mode": parse_mode,
                }
            )
            return True

        self.app._edit_preview_message = fake_edit_preview_message  # type: ignore[method-assign]
        self.app._safe_send_message = lambda *args, **kwargs: None  # type: ignore[method-assign]
        self.app._send_run_result(active_run, "最终回复", preview)

        self.assertEqual(len(calls), 1)
        self.assertIn("Completed", str(calls[0]["text"]))
        self.assertIn("最终回复", str(calls[0]["text"]))
        self.assertIn("gpt-5.4 default · 98% left · ~/teledex", str(calls[0]["text"]))
        self.assertEqual(calls[0]["parse_mode"], "HTML")

    def test_send_run_result_sends_completion_notice_after_preview_edit(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        edit_calls: list[dict[str, object]] = []
        notice_calls: list[dict[str, object]] = []

        def fake_edit_preview_message(
            active_run: ActiveRun,
            text: str,
            parse_mode: str | None = None,
            respect_local_interval: bool = True,
        ) -> bool:
            edit_calls.append(
                {
                    "text": text,
                    "parse_mode": parse_mode,
                }
            )
            return True

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
        ) -> TelegramMessage:
            notice_calls.append(
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

        self.app._edit_preview_message = fake_edit_preview_message  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._send_run_result(active_run, "最终回复")

        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(
            notice_calls,
            [
                {
                    "chat_id": 100,
                    "text": "已完成",
                    "message_thread_id": 9,
                    "reply_to_message_id": None,
                    "parse_mode": None,
                }
            ],
        )

    def test_send_run_result_does_not_duplicate_notice_when_falling_back_to_new_message(self) -> None:
        active_run = ActiveRun(
            run_id=1,
            session_id=1,
            user_id=1,
            chat_id=100,
            message_thread_id=9,
            prompt="任务",
            preview_message_id=456,
        )
        calls: list[dict[str, object]] = []

        def fake_edit_preview_message(
            active_run: ActiveRun,
            text: str,
            parse_mode: str | None = None,
            respect_local_interval: bool = True,
        ) -> bool:
            return False

        def fake_send_message(
            chat_id: int,
            text: str,
            message_thread_id: int | None,
            reply_to_message_id: int | None = None,
            parse_mode: str | None = None,
            defer_on_rate_limit: bool = False,
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
                message_id=790,
                message_thread_id=message_thread_id,
            )

        self.app._edit_preview_message = fake_edit_preview_message  # type: ignore[method-assign]
        self.app._safe_send_message = fake_send_message  # type: ignore[method-assign]

        self.app._send_run_result(active_run, "最终回复")

        self.assertEqual(len(calls), 1)
        self.assertIn("最终回复", str(calls[0]["text"]))

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

        def fake_edit_message_text(**kwargs) -> None:
            calls.append(str(kwargs["text"]))

        app.telegram.edit_message_text = fake_edit_message_text  # type: ignore[method-assign]
        app._safe_send_message = lambda *args, **kwargs: None  # type: ignore[method-assign]

        with patch("teledex.app.time.monotonic", return_value=12.0):
            app._send_run_result(active_run, "最终回复", preview)

        self.assertEqual(len(calls), 1)
        self.assertIn("最终回复", calls[0])

    def test_safe_send_message_can_schedule_retry_when_rate_limited(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def fake_send_message(**kwargs) -> TelegramMessage:
            raise TelegramRateLimitError("限流", retry_after_seconds=15)

        def fake_schedule_delayed_message_send(*args, **kwargs) -> None:
            calls.append((args, kwargs))

        self.app.telegram.send_message = fake_send_message  # type: ignore[method-assign]
        self.app._schedule_delayed_message_send = fake_schedule_delayed_message_send  # type: ignore[method-assign]

        result = self.app._safe_send_message(
            100,
            "最终回复",
            9,
            parse_mode="HTML",
            defer_on_rate_limit=True,
        )

        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:3], (100, "最终回复", 9))

    def test_handle_prompt_allows_other_session_to_run_in_parallel(self) -> None:
        self.app.storage.ensure_user(1, chat_id=100, message_thread_id=9)
        session_1 = self.app.storage.create_session(1, "会话一")
        session_2 = self.app.storage.create_session(1, "会话二")
        self.app.storage.bind_session_path(session_1.id, 1, self.temp_dir.name)
        self.app.storage.bind_session_path(session_2.id, 1, self.temp_dir.name)
        self.app.storage.set_active_session(1, session_2.id)
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

        self.assertEqual(calls, ["○ Thinking (0m)"])
        self.assertIn(session_1.id, self.app._active_runs)
        self.assertIn(session_2.id, self.app._active_runs)

    def test_tnew_uses_unbound_path_title_when_not_provided(self) -> None:
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
        self.assertIn("当前名称：未绑定目录 #1", calls[0])
        self.assertIn("绑定后会自动改成路径名", calls[0])

    def test_tbind_updates_session_name_to_bound_path(self) -> None:
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
                text=f"/tbind {self.temp_dir.name}",
                message_id=2,
                message_thread_id=9,
            )
        )

        updated = self.app.storage.get_session(session.id, 1)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.title, Path(self.temp_dir.name).name)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"当前名称：{Path(self.temp_dir.name).name}", calls[0])

    def test_tbind_creates_new_session_when_binding_different_directory(self) -> None:
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
                text=f"/tbind {self.temp_dir.name}",
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
        self.assertIn(f"已自动创建会话 #{sessions[1].id}", calls[0])

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
                ("start", "查看帮助"),
                ("tnew", "新建会话"),
                ("tsessions", "查看会话"),
                ("tuse", "切换会话"),
                ("tbind", "绑定目录"),
                ("tpwd", "当前目录"),
                ("tstop", "停止任务"),
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

        self.assertEqual(calls, ["○ Thinking (0m)"])
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
                    "text": "/tbind /root/demo",
                    "from": {"id": 1},
                    "chat": {"id": 100},
                    "message_thread_id": 9,
                },
            }
        )

        self.assertEqual(prompts, [])
        self.assertEqual(commands, ["/tbind /root/demo"])

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
        self.assertEqual(messages, [f"已在会话 #{session.id} 中开启新的 Codex 对话。\n目录保持不变：{self.temp_dir.name}"])

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
            [f"会话 #{session.id} 正在执行中，/new 暂时不可用，请稍后或先 /tstop。"],
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

        self.assertEqual(preview.render(), "○ Thinking (0m)")
        self.assertEqual(preview.advance(animate_steps=1, elapsed_seconds=0), "● Thinking (0m)")
        self.assertEqual(preview.advance(animate_steps=1, elapsed_seconds=60), "○ Thinking (1m)")

    def test_status_line_can_catch_up_multiple_elapsed_seconds(self) -> None:
        preview = LivePreviewState(initial_status="Thinking")

        self.assertEqual(preview.advance(animate_steps=3, elapsed_seconds=180), "● Thinking (3m)")

    def test_stream_text_is_rendered_immediately(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("abcdef")

        self.assertEqual(
            preview.render(),
            "○ Thinking (0m)\n\nabcdef",
        )

    def test_commentary_history_appends_instead_of_replacing(self) -> None:
        preview = LivePreviewState()

        preview.update_commentary("msg_1", "先看目录")
        preview.update_commentary("msg_2", "再检查配置")
        preview.update_status("Thinking")

        self.assertEqual(
            preview.render(),
            "○ Thinking (0m)\n\n先看目录\n\n再检查配置",
        )

    def test_command_output_is_rendered_in_preview(self) -> None:
        preview = LivePreviewState()
        preview.update_tool_state(
            "call_1",
            command_text="/bin/bash -lc 'pwd'",
            output_text="first line\nsecond line",
        )

        self.assertEqual(
            preview.render(),
            "○ Thinking (0m)",
        )

    def test_complete_keeps_final_status_line(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("完成内容")

        self.assertEqual(
            preview.complete(),
            "● Completed (0m)\n\n完成内容",
        )

    def test_commentary_can_be_kept_until_final_answer_starts(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("reasoning:item_1", "**Thinking**\n\nChecking files")

        self.assertEqual(
            preview.render(),
            "○ Thinking (0m)\n\n**Thinking**\n\nChecking files",
        )

    def test_footer_statusline_renders_at_bottom(self) -> None:
        preview = LivePreviewState()
        preview.update_footer_statusline("gpt-5.4 default · 100% left · ~/teledex")

        self.assertEqual(
            preview.render(),
            "○ Thinking (0m)\n\ngpt-5.4 default · 100% left · ~/teledex",
        )

    def test_final_stream_clears_transient_sections(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先检查 README")
        preview.update_tool_state("call_1", command_text="cat README.md", output_text="hello")
        preview.update_stream_text("最终输出")

        self.assertEqual(
            preview.render(),
            "○ Thinking (0m)\n\n先检查 README\n\n最终输出",
        )

    def test_complete_clears_transient_sections_and_keeps_final_output(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先检查 README")
        preview.update_tool_state("call_1", command_text="cat README.md", output_text="hello")
        preview.update_stream_text("最终输出")

        self.assertEqual(preview.complete(), "● Completed (0m)\n\n最终输出")

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
        app._run_preview_loop(active_run, preview, stop_event)  # type: ignore[arg-type]

        self.assertEqual(attempts, ["○ Thinking (0m)\n\n实时过程"])
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
        app._run_preview_loop(active_run, preview, stop_event)  # type: ignore[arg-type]

        self.assertEqual(attempts, ["● Thinking (0m)\n\n继续思考"])

    def test_final_html_only_renders_final_answer_markdown(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("## 标题\n\n- 列表项\n\n**加粗**")
        preview.complete()

        rendered = preview.render_final_html()

        self.assertIn("● Completed (0m)", rendered)
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
        self.assertTrue(rendered.endswith("..."))


if __name__ == "__main__":
    unittest.main()
