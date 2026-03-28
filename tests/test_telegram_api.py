from __future__ import annotations

import unittest

from teledex.telegram_api import TelegramRateLimitError, _extract_retry_after_seconds


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


if __name__ == "__main__":
    unittest.main()
