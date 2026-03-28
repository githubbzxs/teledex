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
    _next_preview_deadline,
    _normalize_preview_interval,
)
from teledex.config import AppConfig
from teledex.telegram_api import TelegramMessage


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
        self.assertEqual(str(calls[0]["text"]), "○ Working (0s)")

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
        self.app._send_run_result(active_run, "最终回复", preview)

        self.assertEqual(len(calls), 1)
        self.assertIn("Completed", str(calls[0]["text"]))
        self.assertIn("最终回复", str(calls[0]["text"]))
        self.assertIn("gpt-5.4 default · 98% left · ~/teledex", str(calls[0]["text"]))
        self.assertEqual(calls[0]["parse_mode"], "HTML")

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

    def test_handle_update_routes_unknown_slash_command_to_prompt(self) -> None:
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

        self.assertEqual(prompts, ["/new"])
        self.assertEqual(commands, [])

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


class LivePreviewStateTestCase(unittest.TestCase):
    def test_preview_deadline_catches_up_without_accumulating_drift(self) -> None:
        self.assertEqual(_normalize_preview_interval(0.0), 0.2)
        self.assertEqual(_normalize_preview_interval(1.0), 1.0)
        self.assertEqual(_next_preview_deadline(10.0, 10.2, 1.0), 11.0)
        self.assertEqual(_next_preview_deadline(10.0, 12.3, 1.0), 13.0)

    def test_status_line_tracks_elapsed_with_circle_animation(self) -> None:
        preview = LivePreviewState(initial_status="Thinking")

        self.assertEqual(preview.render(), "○ Thinking (0s)")
        self.assertEqual(preview.advance(animate_steps=1, elapsed_seconds=1), "● Thinking (1s)")

    def test_status_line_can_catch_up_multiple_elapsed_seconds(self) -> None:
        preview = LivePreviewState(initial_status="Thinking")

        self.assertEqual(preview.advance(animate_steps=3, elapsed_seconds=3), "● Thinking (3s)")

    def test_stream_text_is_rendered_immediately(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("abcdef")

        self.assertEqual(
            preview.render(),
            "○ Working (0s)\n\nabcdef",
        )

    def test_commentary_history_appends_instead_of_replacing(self) -> None:
        preview = LivePreviewState()

        preview.update_commentary("msg_1", "先看目录")
        preview.update_commentary("msg_2", "再检查配置")
        preview.update_status("Working")

        self.assertEqual(
            preview.render(),
            "○ Working (0s)\n\n先看目录\n\n再检查配置",
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
            "○ Working (0s)\n\n/bin/bash -lc 'pwd'\nfirst line\nsecond line",
        )

    def test_complete_keeps_final_status_line(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("完成内容")

        self.assertEqual(
            preview.complete(),
            "● Completed (0s)\n\n完成内容",
        )

    def test_commentary_can_be_kept_until_final_answer_starts(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("reasoning:item_1", "**Thinking**\n\nChecking files")

        self.assertEqual(
            preview.render(),
            "○ Working (0s)\n\n**Thinking**\n\nChecking files",
        )

    def test_footer_statusline_renders_at_bottom(self) -> None:
        preview = LivePreviewState()
        preview.update_footer_statusline("gpt-5.4 default · 100% left · ~/teledex")

        self.assertEqual(
            preview.render(),
            "○ Working (0s)\n\ngpt-5.4 default · 100% left · ~/teledex",
        )

    def test_final_stream_clears_transient_sections(self) -> None:
        preview = LivePreviewState()
        preview.update_commentary("msg_1", "先检查 README")
        preview.update_tool_state("call_1", command_text="cat README.md", output_text="hello")
        preview.update_stream_text("最终输出")

        self.assertEqual(preview.render(), "○ Working (0s)\n\n最终输出")

    def test_final_html_only_renders_final_answer_markdown(self) -> None:
        preview = LivePreviewState()
        preview.update_stream_text("## 标题\n\n- 列表项\n\n**加粗**")
        preview.complete()

        rendered = preview.render_final_html()

        self.assertIn("● Completed (0s)", rendered)
        self.assertIn("<b>标题</b>", rendered)
        self.assertIn("• 列表项", rendered)
        self.assertIn("<b>加粗</b>", rendered)


if __name__ == "__main__":
    unittest.main()
