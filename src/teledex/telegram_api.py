from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class TelegramApiError(RuntimeError):
    """Telegram API 错误。"""


class TelegramRateLimitError(TelegramApiError):
    """Telegram API 限流错误。"""

    def __init__(self, detail: str, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(detail)


@dataclass(slots=True)
class TelegramMessage:
    chat_id: int
    message_id: int
    message_thread_id: int | None


class TelegramClient:
    def __init__(self, token: str, timeout_seconds: int = 60) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.timeout_seconds = timeout_seconds

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout_seconds + 10)
        return list(result)

    def send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> TelegramMessage:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        result = self._call("sendMessage", payload)
        return TelegramMessage(
            chat_id=int(result["chat"]["id"]),
            message_id=int(result["message_id"]),
            message_thread_id=(
                int(result["message_thread_id"])
                if result.get("message_thread_id") is not None
                else None
            ),
        )

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        self._call("editMessageText", payload)

    def send_chat_action(
        self,
        chat_id: int,
        action: str,
        message_thread_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "action": action,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        self._call("sendChatAction", payload)

    def set_my_commands(
        self,
        commands: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    ) -> None:
        payload = {
            "commands": json.dumps(
                [
                    {
                        "command": command,
                        "description": description,
                    }
                    for command, description in commands
                ],
                ensure_ascii=False,
            )
        }
        self._call("setMyCommands", payload)

    def _call(
        self, method: str, payload: dict[str, Any] | None = None, timeout: int | None = None
    ) -> Any:
        data = None
        headers = {}
        if payload is not None:
            encoded = urllib.parse.urlencode(payload).encode("utf-8")
            data = encoded
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url=f"{self.base_url}{method}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout if timeout is not None else self.timeout_seconds,
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retry_after = _extract_retry_after_seconds(detail)
            if retry_after is not None:
                raise TelegramRateLimitError(
                    f"Telegram HTTP 错误: {detail}",
                    retry_after_seconds=retry_after,
                ) from exc
            raise TelegramApiError(f"Telegram HTTP 错误: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TelegramApiError(f"Telegram 连接失败: {exc}") from exc

        if not body.get("ok"):
            retry_after = _extract_retry_after_seconds(body)
            if retry_after is not None:
                raise TelegramRateLimitError(
                    f"Telegram API 返回失败: {body}",
                    retry_after_seconds=retry_after,
                )
            raise TelegramApiError(f"Telegram API 返回失败: {body}")
        return body["result"]


def _extract_retry_after_seconds(body: str | dict[str, Any]) -> int | None:
    payload: dict[str, Any] | None
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None
    else:
        payload = body

    if not isinstance(payload, dict):
        return None
    if int(payload.get("error_code", 0) or 0) != 429:
        return None
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    if retry_after is None:
        return None
    try:
        return max(1, int(retry_after))
    except (TypeError, ValueError):
        return None


def is_message_not_modified_error(error: Exception) -> bool:
    text = str(error)
    return "message is not modified" in text.lower()
