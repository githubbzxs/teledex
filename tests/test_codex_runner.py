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
        self.assertEqual(parsed.final_message, "正在流式输出")

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

        self.assertEqual(parsed.status_text, "执行失败")

    def test_build_command_uses_app_server_helper(self) -> None:
        output_file = Path(self.temp_dir.name) / "last.txt"
        command = self.runner._build_command(
            prompt="你好",
            cwd=Path("/root/teledex"),
            thread_id="thread-123",
            output_file=output_file,
        )

        self.assertGreaterEqual(len(command), 3)
        self.assertEqual(command[1], "-u")
        self.assertTrue(command[2].endswith("codex_app_server_exec.py"))
        self.assertIn("--thread-id", command)
        self.assertIn("thread-123", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-test", command)


if __name__ == "__main__":
    unittest.main()
