from __future__ import annotations

import http.client
import unittest
import tempfile
from pathlib import Path
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

    def test_call_wraps_remote_disconnect_as_telegram_api_error(self) -> None:
        client = TelegramClient("test-token", timeout_seconds=1)

        with patch(
            "urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaises(TelegramApiError) as context:
                client.get_me()

        self.assertIn("Telegram 连接异常", str(context.exception))

    def test_download_file_wraps_os_error_as_telegram_api_error(self) -> None:
        client = TelegramClient("test-token", timeout_seconds=1)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "photo.jpg"
            with patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")):
                with self.assertRaises(TelegramApiError) as context:
                    client.download_file("photos/file.jpg", destination)

        self.assertIn("Telegram 网络异常", str(context.exception))

    def test_send_photo_uses_multipart_request(self) -> None:
        client = TelegramClient("test-token", timeout_seconds=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            photo_path = Path(temp_dir) / "image.png"
            photo_path.write_bytes(b"png")

            captured: list[tuple[str, bytes | None, dict[str, str], int | None]] = []

            def fake_send_request(method, *, data, headers, timeout):
                captured.append((method, data, headers, timeout))
                return {
                    "chat": {"id": 100},
                    "message_id": 200,
                }

            client._send_request = fake_send_request  # type: ignore[method-assign]
            message = client.send_photo(chat_id=100, photo_path=photo_path)

        self.assertEqual(message.chat_id, 100)
        self.assertEqual(message.message_id, 200)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "sendPhoto")
        self.assertIn("multipart/form-data", captured[0][2]["Content-Type"])


if __name__ == "__main__":
    unittest.main()
