from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teledex.app import ActiveRun, IncomingMessage, LivePreviewState, TeledexApp
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
        self.assertTrue(str(calls[0]["text"]).startswith("○ "))

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
        self.assertEqual(calls[0]["text"], "最终回复")
        self.assertEqual(calls[0]["parse_mode"], "HTML")


class LivePreviewStateTestCase(unittest.TestCase):
    def test_heartbeat_marker_toggles(self) -> None:
        preview = LivePreviewState(initial_status="正在思考...")

        self.assertEqual(preview.render(), "○ 正在思考...")
        self.assertEqual(preview.advance(), "● 正在思考...")
        self.assertEqual(preview.advance(), "○ 正在思考...")

    def test_stream_text_reveals_incrementally(self) -> None:
        preview = LivePreviewState(stream_step_chars=2)
        preview.update_stream_text("abcdef")

        self.assertEqual(preview.render(), "○ ab")
        self.assertEqual(preview.advance(), "● abcd")
        self.assertEqual(preview.advance(), "○ abcdef")


if __name__ == "__main__":
    unittest.main()
