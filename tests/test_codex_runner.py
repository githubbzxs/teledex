from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            preview_edit_min_interval_seconds=0.0,
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

        self.assertEqual(parsed.status_text, "Thinking")
        self.assertEqual(parsed.commentary_id, "msg_1")
        self.assertEqual(parsed.commentary_text, "我先检查目录")
        self.assertIsNone(parsed.final_message)

    def test_parse_event_line_marks_completed_commentary_for_cleanup(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "item.completed",
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

        self.assertEqual(parsed.commentary_completed_id, "msg_1")

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

        self.assertIsNone(parsed.status_text)
        self.assertIsNone(parsed.commentary_id)
        self.assertIsNone(parsed.commentary_text)

    def test_parse_event_line_ignores_plan_updates_in_preview_body(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "plan.updated",
                    "plan_id": "turn-plan:1",
                    "text": "1. [进行中] 先检查目录",
                },
                ensure_ascii=False,
            )
        )

        self.assertIsNone(parsed.status_text)
        self.assertIsNone(parsed.commentary_id)
        self.assertIsNone(parsed.commentary_text)

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

        self.assertEqual(parsed.status_text, "Thinking")
        self.assertEqual(parsed.tool_call_id, "cmd_1")
        self.assertEqual(parsed.tool_output_text, "line1\nline2")

    def test_parse_event_line_supports_command_execution_metadata(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "id": "call_1",
                        "command": "/bin/bash -lc 'pwd'",
                        "aggregatedOutput": "",
                    },
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.tool_call_id, "call_1")
        self.assertEqual(parsed.tool_command_text, "/bin/bash -lc 'pwd'")

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

    def test_parse_event_line_captures_generated_image_path(self) -> None:
        parsed = self.runner.parse_event_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "image_generation",
                        "id": "img_1",
                        "savedPath": "/tmp/generated-image.png",
                        "status": "completed",
                    },
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed.generated_image_path, "/tmp/generated-image.png")

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

    def test_tmux_session_name_uses_directory_name_with_stable_suffix(self) -> None:
        session_name = self.runner._tmux_session_name(2, Path("/root/teledex"))

        self.assertTrue(session_name.startswith("teledex-teledex-"))

    def test_build_shell_command_syncs_service_environment_into_tmux_shell(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/root",
                "PATH": "/usr/local/bin:/usr/bin",
                "GH_TOKEN": "gh-test-token",
                "GITHUB_TOKEN": "github-test-token",
                "CUSTOM_VALUE": "hello world",
            },
            clear=True,
        ):
            shell_command = self.runner._build_shell_command(
                Path("/root/freecodex"),
                ["python3", "-c", "print('ok')"],
            )

        self.assertIn("export GH_TOKEN=gh-test-token", shell_command)
        self.assertIn("export GITHUB_TOKEN=github-test-token", shell_command)
        self.assertIn("export CUSTOM_VALUE='hello world'", shell_command)
        self.assertIn("export __TELEDEX_SYNCED_ENV_KEYS=", shell_command)
        self.assertIn("env -i", shell_command)
        self.assertIn("GH_TOKEN=gh-test-token", shell_command)
        self.assertIn("GITHUB_TOKEN=github-test-token", shell_command)
        self.assertIn("/bin/bash -lc", shell_command)
        self.assertIn("cd /root/freecodex && python3 -c", shell_command)
        self.assertIn("print(", shell_command)

    def test_format_start_log_message_avoids_embedding_shell_command_or_env(self) -> None:
        message = self.runner._format_start_log_message(
            Path("/root/freecodex"),
            "thread-123",
            {
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "approval_policy": "never",
                "sandbox_mode": "danger-full-access",
                "collaboration_mode": "default",
            },
        )

        self.assertIn("cwd=/root/freecodex", message)
        self.assertIn("thread=thread-123", message)
        self.assertIn("model=gpt-5.4", message)
        self.assertNotIn("env -i", message)
        self.assertNotIn("GH_TOKEN", message)


if __name__ == "__main__":
    unittest.main()
