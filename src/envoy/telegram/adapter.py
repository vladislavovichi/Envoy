import asyncio
from collections.abc import AsyncIterator
from hmac import compare_digest
from typing import Any, ClassVar

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from tenacity import AsyncRetrying, retry_if_exception_type

from envoy.domain.telegram import ReplyMarkup, SentMessage, SurfaceCapabilities, Update
from envoy.telegram.outbound import RetryAfter, TransientSendError, bounded_retry_wait


class BotApiError(Exception):
    pass


class RawMethod(TelegramMethod[Any]):
    __api_method__: ClassVar[str] = ""
    __returning__: ClassVar[Any] = Any


class AiogramAdapter:
    capabilities = SurfaceCapabilities(
        private_context=True, ephemeral=False, checklist_native=False
    )

    def __init__(self, token: str, api_base: str) -> None:
        session = AiohttpSession(api=TelegramAPIServer.from_base(api_base))
        self._bot = Bot(token, session=session)
        self._closed = False
        self._close_event = asyncio.Event()
        self._webhook_updates: asyncio.Queue[Update | None] | None = None

    async def _poll_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._close_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def call_raw(self, method: str, **params: Any) -> Any:
        request = RawMethod(**params)
        object.__setattr__(request, "__api_method__", method)
        try:
            return await self._bot(request)
        except TelegramRetryAfter as exc:
            raise RetryAfter(float(exc.retry_after)) from None
        except TelegramNetworkError, TelegramServerError:
            raise TransientSendError("Telegram transport temporarily unavailable") from None
        except TelegramAPIError:
            raise BotApiError("Telegram rejected the request") from None

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
                    for row in reply_markup.buttons
                ]
            ).model_dump(exclude_none=True)
        response = await self.call_raw("sendMessage", **params)
        return SentMessage(
            int(response["message_id"]), int(response["chat"]["id"]), str(response["text"])
        )

    @staticmethod
    def _map_update(raw: dict[str, Any]) -> Update:
        message = raw.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return Update(int(raw["update_id"]), ignored=True)
        return Update(int(raw["update_id"]), int(message["chat"]["id"]), message["text"])

    async def updates(self) -> AsyncIterator[Update]:
        if self._webhook_updates is not None:
            while not self._closed:
                update = await self._webhook_updates.get()
                if update is None:
                    return
                yield update
            return
        offset = 0
        while not self._closed:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type((RetryAfter, TransientSendError)),
                wait=lambda state: bounded_retry_wait(state, 60),
                sleep=self._poll_sleep,
                reraise=True,
            ):
                with attempt:
                    if self._closed:
                        return
                    responses = await self.call_raw("getUpdates", offset=offset, timeout=20)
            for raw in responses:
                offset = int(raw["update_id"]) + 1
                yield self._map_update(raw)

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self.call_raw("setWebhook", url=url, secret_token=secret_token)

    def webhook_app(self, secret_token: str) -> web.Application:
        if not secret_token:
            raise ValueError("Webhook secret cannot be empty")
        self._webhook_updates = asyncio.Queue()

        async def receive(request: web.Request) -> web.Response:
            supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not compare_digest(supplied, secret_token):
                raise web.HTTPUnauthorized()
            update = self._map_update(await request.json())
            if self._webhook_updates is not None:
                self._webhook_updates.put_nowait(update)
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/webhook", receive)
        return app

    async def close(self) -> None:
        self._closed = True
        self._close_event.set()
        if self._webhook_updates is not None:
            self._webhook_updates.put_nowait(None)
        await self._bot.session.close()
