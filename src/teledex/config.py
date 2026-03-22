from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_set(value: str | None) -> set[int]:
    if not value:
        return set()
    result: set[int] = set()
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        result.add(int(item))
    return result


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str
    authorized_user_ids: set[int]
    state_dir: Path
    poll_timeout_seconds: int
    preview_update_interval_seconds: float
    codex_bin: str
    codex_exec_mode: str
    codex_model: str | None
    codex_enable_search: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "AppConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("缺少环境变量 TELEGRAM_BOT_TOKEN")

        authorized_user_ids = _parse_int_set(
            os.environ.get("AUTHORIZED_TELEGRAM_USER_IDS")
        )
        if not authorized_user_ids:
            raise ValueError("缺少环境变量 AUTHORIZED_TELEGRAM_USER_IDS")

        state_dir = Path(os.environ.get("TELEDEX_STATE_DIR", "data")).expanduser()
        poll_timeout_seconds = int(os.environ.get("TELEDEX_POLL_TIMEOUT_SECONDS", "30"))
        preview_update_interval_seconds = float(
            os.environ.get("TELEDEX_PREVIEW_UPDATE_INTERVAL_SECONDS", "2.0")
        )
        codex_bin = os.environ.get("TELEDEX_CODEX_BIN", "codex").strip() or "codex"
        codex_exec_mode = (
            os.environ.get("TELEDEX_CODEX_EXEC_MODE", "full-auto").strip().lower()
        )
        if codex_exec_mode not in {"default", "full-auto", "dangerous"}:
            raise ValueError(
                "TELEDEX_CODEX_EXEC_MODE 仅支持 default、full-auto、dangerous"
            )

        codex_model = os.environ.get("TELEDEX_CODEX_MODEL", "").strip() or None
        codex_enable_search = _parse_bool(os.environ.get("TELEDEX_CODEX_ENABLE_SEARCH"))
        log_level = os.environ.get("TELEDEX_LOG_LEVEL", "INFO").strip().upper() or "INFO"

        return cls(
            telegram_bot_token=token,
            authorized_user_ids=authorized_user_ids,
            state_dir=state_dir,
            poll_timeout_seconds=poll_timeout_seconds,
            preview_update_interval_seconds=preview_update_interval_seconds,
            codex_bin=codex_bin,
            codex_exec_mode=codex_exec_mode,
            codex_model=codex_model,
            codex_enable_search=codex_enable_search,
            log_level=log_level,
        )
