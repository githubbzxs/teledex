from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from teledex.codex_runner import CodexRunner
from teledex.config import AppConfig


class CodexRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            telegram_bot_token="test-token",
            authorized_user_ids={1},
            state_dir=Path(self.temp_dir.name),
            poll_timeout_seconds=30,
            preview_update_interval_seconds=1.0,
            codex_bin="codex",
            codex_exec_mode="full-auto",
            codex_model="gpt-test",
            codex_enable_search=False,
            codex_persist_extended_history=True,
            tmux_bin="tmux",
            tmux_shell="/bin/bash",
            log_level="INFO",
        )
        self.runner = CodexRunner(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_event_line_supports_agent_message_delta_updates(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "item.updated",
                    "item": {
                        "type": "agent_message",
                        "id": "msg_1",
                        "text": "正在流式输出",
                    },
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.preview_text, "正在流式输出")
        self.assertIsNone(parsed.final_message)

    def test_parse_event_line_supports_commentary_agent_message(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "item.updated",
                    "item": {
                        "type": "agent_message",
                        "id": "msg_1",
                        "phase": "commentary",
                        "text": "我先检查目录",
                    },
                },
                ensure_ascii=False,
            )
        )

        self.assertIsNone(parsed.status_text)
        self.assertEqual(parsed.commentary_id, "msg_1")
        self.assertEqual(parsed.commentary_text, "我先检查目录")
        self.assertIsNone(parsed.final_message)

    def test_parse_event_line_supports_turn_failed(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "turn.failed",
                    "message": "执行失败",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.status_text, "Failed")

    def test_parse_event_line_supports_reasoning_summary_updates(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "reasoning.updated",
                    "item_id": "reasoning_1",
                    "text": "**Thinking**\n\n先检查目录，再确认配置。",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.status_text, "Thinking")
        self.assertEqual(parsed.commentary_id, "reasoning:reasoning_1")
        self.assertEqual(parsed.commentary_text, "**Thinking**\n\n先检查目录，再确认配置。")

    def test_parse_event_line_supports_command_output(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "command.output",
                    "item_id": "cmd_1",
                    "text": "line1\nline2",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.status_text, "Working")
        self.assertEqual(parsed.tool_output_text, "line1\nline2")

    def test_parse_event_line_only_marks_final_message_on_completed_agent_message(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "id": "msg_2",
                        "phase": "final_answer",
                        "text": "最终回复",
                    },
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.preview_text, "最终回复")
        self.assertEqual(parsed.final_message, "最终回复")

    def test_parse_event_line_supports_footer_statusline_updates(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "statusline.updated",
                    "footer_statusline": "gpt-5.4 default · 98% left · ~/teledex",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(
            parsed.footer_statusline,
            "gpt-5.4 default · 98% left · ~/teledex",
        )

    def test_parse_event_line_preserves_footer_statusline_on_thread_started(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "thread-1",
                    "footer_statusline": "gpt-test default · 100% left · ~/teledex",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.thread_id, "thread-1")
        self.assertEqual(
            parsed.footer_statusline,
            "gpt-test default · 100% left · ~/teledex",
        )

    def test_build_command_uses_app_server_helper(self) -> None:
        output_file = Path(self.temp_dir.name) / "last.txt"
        event_log_file = Path(self.temp_dir.name) / "events.jsonl"
        status_file = Path(self.temp_dir.name) / "status.json"
        prompt_file = Path(self.temp_dir.name) / "prompt.txt"
        command = self.runner._build_command(
            cwd=Path("/root/teledex"),
            thread_id="thread-123",
            output_file=output_file,
            event_log_file=event_log_file,
            status_file=status_file,
            prompt_file=prompt_file,
        )

        self.assertGreaterEqual(len(command), 3)
        self.assertEqual(command[1], "-u")
        self.assertTrue(command[2].endswith("codex_app_server_exec.py"))
        self.assertIn("--event-log-file", command)
        self.assertIn(str(event_log_file), command)
        self.assertIn("--status-file", command)
        self.assertIn(str(status_file), command)
        self.assertIn("--prompt-file", command)
        self.assertIn(str(prompt_file), command)
        self.assertIn("--thread-id", command)
        self.assertIn("thread-123", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-test", command)
        self.assertIn("--persist-extended-history", command)


if __name__ == "__main__":
    unittest.main()
