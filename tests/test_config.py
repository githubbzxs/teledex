from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teledex.config import AppConfig


class ConfigTestCase(unittest.TestCase):
    def test_preview_interval_defaults_to_one_second(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "AUTHORIZED_TELEGRAM_USER_IDS": "1",
                "TELEDEX_STATE_DIR": str(Path(temp_dir) / "data"),
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env()

        self.assertEqual(config.preview_update_interval_seconds, 1.0)


if __name__ == "__main__":
    unittest.main()
