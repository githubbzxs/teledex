from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teledex.config import AppConfig


class ConfigTestCase(unittest.TestCase):
    def test_preview_interval_defaults_to_five_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "AUTHORIZED_TELEGRAM_USER_IDS": "1",
                "TELEDEX_STATE_DIR": str(Path(temp_dir) / "data"),
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env()

        self.assertEqual(config.preview_update_interval_seconds, 5.0)
        self.assertEqual(config.preview_edit_min_interval_seconds, 5.0)
        self.assertEqual(config.codex_exec_mode, "default")

    def test_can_enable_discord_without_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "DISCORD_BOT_TOKEN": "discord-token",
                "AUTHORIZED_DISCORD_USER_IDS": "7",
                "TELEDEX_STATE_DIR": str(Path(temp_dir) / "data"),
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env()

        self.assertIsNone(config.telegram_bot_token)
        self.assertEqual(config.authorized_user_ids, set())
        self.assertEqual(config.discord_bot_token, "discord-token")
        self.assertEqual(config.authorized_discord_user_ids, {7})

    def test_requires_at_least_one_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "TELEDEX_STATE_DIR": str(Path(temp_dir) / "data"),
            }
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "至少配置"):
                    AppConfig.from_env()


if __name__ == "__main__":
    unittest.main()
