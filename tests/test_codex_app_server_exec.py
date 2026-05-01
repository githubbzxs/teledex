from __future__ import annotations

import unittest
from pathlib import Path

from teledex.codex_app_server_exec import (
    _build_turn_start_params,
    _build_footer_statusline,
    _execution_overrides,
    _extract_reasoning_effort,
    _extract_status_line_items,
)


class CodexAppServerExecTestCase(unittest.TestCase):
    def test_execution_overrides_keeps_local_permissions_in_default_mode(self) -> None:
        self.assertEqual(_execution_overrides("default"), {})

    def test_extract_reasoning_effort_supports_snake_case_config_field(self) -> None:
        self.assertEqual(
            _extract_reasoning_effort({"model_reasoning_effort": "high"}),
            "high",
        )

    def test_extract_status_line_items_uses_tui_config(self) -> None:
        self.assertEqual(
            _extract_status_line_items(
                {
                    "tui": {
                        "status_line": [
                            "model-with-reasoning",
                            "context-remaining",
                        ]
                    }
                }
            ),
            ("model-with-reasoning", "context-remaining"),
        )

    def test_build_footer_statusline_mirrors_tui_status_line_items(self) -> None:
        line = _build_footer_statusline(
            {
                "cwd": Path("/root/teledex"),
                "model": "gpt-5.4",
                "reasoning_effort": "xhigh",
                "service_tier": "fast",
                "status_line_items": (
                    "model-with-reasoning",
                    "context-remaining",
                ),
                "context_remaining_percent": 82,
                "thread_id": "thread-1",
            }
        )

        self.assertEqual(line, "gpt-5.4 xhigh fast · 82% left")

    def test_build_footer_statusline_defaults_to_codex_default_items(self) -> None:
        line = _build_footer_statusline(
            {
                "cwd": Path("/root/teledex"),
                "model": "gpt-5.4",
                "reasoning_effort": "medium",
                "service_tier": None,
                "status_line_items": (),
                "context_remaining_percent": 100,
                "thread_id": "thread-1",
            }
        )

        self.assertEqual(line, "gpt-5.4 medium · 100% left · ~/teledex")

    def test_build_turn_start_params_requests_chinese_runtime_summaries(self) -> None:
        class Args:
            model = None
            service_tier = None
            reasoning_effort = None
            personality = None
            collaboration_mode = None

        params = _build_turn_start_params("thread-1", "继续处理", Args(), None, None)

        text = params["input"][0]["text"]
        self.assertIn("思考过程摘要", text)
        self.assertIn("必须使用简体中文", text)
        self.assertIn("User message:\n继续处理", text)


if __name__ == "__main__":
    unittest.main()
