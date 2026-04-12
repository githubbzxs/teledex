from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable


class DiscordApiError(RuntimeError):
    """Discord API 错误。"""


@dataclass(slots=True)
class DiscordMessage:
    chat_id: int
    message_id: int
    message_thread_id: int | None = None


class DiscordClient:
    def __init__(
        self,
        token: str,
        on_message: Callable[[int, int, int, str], None],
        logger,
    ) -> None:
        self.token = token.strip()
        if not self.token:
            raise ValueError("Discord token 不能为空")
        self._on_message = on_message
        self._logger = logger
        self._ready_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    def run_forever(self) -> None:
        try:
            import discord
        except ImportError as exc:
            raise RuntimeError("未安装 discord.py，无法启用 Discord 桥接") from exc

        bridge = self

        class _DiscordGatewayClient(discord.Client):
            async def on_ready(self) -> None:
                bridge._logger.info("Discord bot 已连接: %s", self.user)
                bridge._ready_event.set()

            async def on_message(self, message) -> None:
                if message.author == self.user or getattr(message.author, "bot", False):
                    return
                content = str(message.content or "").strip()
                if not content:
                    return
                bridge._on_message(
                    int(message.author.id),
                    int(message.channel.id),
                    int(message.id),
                    content,
                )

        async def _runner() -> None:
            intents = discord.Intents.default()
            intents.message_content = True
            client = _DiscordGatewayClient(intents=intents)
            self._client = client
            self._loop = asyncio.get_running_loop()
            await client.start(self.token)

        asyncio.run(_runner())

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> DiscordMessage:
        del reply_to_message_id

        async def _send() -> DiscordMessage:
            channel = await self._fetch_channel(chat_id)
            sent = await channel.send(text)
            return DiscordMessage(
                chat_id=int(channel.id),
                message_id=int(sent.id),
            )

        return self._run_coroutine(_send())

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        async def _edit() -> None:
            channel = await self._fetch_channel(chat_id)
            message = channel.get_partial_message(message_id)
            await message.edit(content=text)

        self._run_coroutine(_edit())

    def delete_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> None:
        async def _delete() -> None:
            channel = await self._fetch_channel(chat_id)
            message = channel.get_partial_message(message_id)
            await message.delete()

        self._run_coroutine(_delete())

    def send_typing(
        self,
        chat_id: int,
    ) -> None:
        async def _typing() -> None:
            channel = await self._fetch_channel(chat_id)
            await channel.typing()

        self._run_coroutine(_typing())

    async def _fetch_channel(self, chat_id: int):
        if self._client is None:
            raise DiscordApiError("Discord client 尚未初始化")
        channel = self._client.get_channel(chat_id)
        if channel is not None:
            return channel
        try:
            return await self._client.fetch_channel(chat_id)
        except Exception as exc:
            raise DiscordApiError(f"获取 Discord channel 失败: {exc}") from exc

    def _run_coroutine(self, coro):
        if not self._ready_event.wait(timeout=30):
            raise DiscordApiError("Discord client 尚未 ready")
        if self._loop is None:
            raise DiscordApiError("Discord event loop 尚未初始化")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=60)
        except Exception as exc:
            raise DiscordApiError(f"Discord 请求失败: {exc}") from exc
