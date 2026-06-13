from __future__ import annotations

import json
import urllib.parse
import unittest
from unittest.mock import patch

from teledex.telegram_api import (
    TelegramApiError,
    TelegramClient,
    TelegramRateLimitError,
    _extract_retry_after_seconds,
)


class TelegramApiTestCase(unittest.TestCase):
    def test_extract_retry_after_seconds_from_dict_payload(self) -> None:
        retry_after = _extract_retry_after_seconds(
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 437},
            }
        )

        self.assertEqual(retry_after, 437)

    def test_extract_retry_after_seconds_from_json_text(self) -> None:
        retry_after = _extract_retry_after_seconds(
            '{"ok":false,"error_code":429,"parameters":{"retry_after":12}}'
        )

        self.assertEqual(retry_after, 12)

    def test_rate_limit_error_preserves_retry_after_seconds(self) -> None:
        error = TelegramRateLimitError("限流", retry_after_seconds=7)

        self.assertEqual(error.retry_after_seconds, 7)

    def test_call_wraps_timeout_error_as_telegram_api_error(self) -> None:
        client = TelegramClient("test-token", timeout_seconds=1)

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TelegramApiError) as context:
                client.get_me()

        self.assertIn("Telegram 请求超时", str(context.exception))

    def test_send_rich_message_serializes_payload(self) -> None:
        client = TelegramClient("test-token", timeout_seconds=1)
        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "chat": {"id": 100},
                            "message_id": 321,
                            "message_thread_id": 9,
                        },
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["data"] = request.data
            return _FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            message = client.send_rich_message(
                chat_id=100,
                rich_message={"markdown": "| A | B |\n|---|---|\n| 1 | 2 |"},
                message_thread_id=9,
                reply_to_message_id=88,
            )

        payload = urllib.parse.parse_qs(bytes(captured["data"]).decode("utf-8"))
        self.assertTrue(str(captured["url"]).endswith("/sendRichMessage"))
        self.assertEqual(message.message_id, 321)
        self.assertEqual(
            json.loads(payload["rich_message"][0])["markdown"],
            "| A | B |\n|---|---|\n| 1 | 2 |",
        )
        self.assertEqual(json.loads(payload["reply_parameters"][0]), {"message_id": 88})


if __name__ == "__main__":
    unittest.main()
