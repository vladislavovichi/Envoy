import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
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
from aiohttp import web
from tenacity import AsyncRetrying, retry_if_exception_type

from envoy.domain.telegram import (
    InlineButton,
    ReplyMarkup,
    SentMessage,
    SurfaceCapabilities,
    Update,
)
from envoy.telegram.mapping import ALLOWED_UPDATES, map_update
from envoy.telegram.outbound import RetryAfter, TransientSendError, bounded_retry_wait
from envoy.telegram.ports import QueryExpired, SendRejected, TopicUnavailable, WidgetRejected

NOT_MODIFIED = "message is not modified"
EXPIRED_QUERY_MARKERS = ("query is too old", "query id is invalid", "query_id_invalid")


class BotApiError(SendRejected):
    def __init__(self, description: str = "") -> None:
        self.description = description
        super().__init__("Telegram rejected the request")


class RawMethod(TelegramMethod[Any]):
    __api_method__: ClassVar[str] = ""
    __returning__: ClassVar[Any] = Any


class AiogramAdapter:
    capabilities = SurfaceCapabilities(private_context=True, ephemeral=False, checklist_native=True)

    def __init__(self, token: str, api_base: str) -> None:
        session = AiohttpSession(api=TelegramAPIServer.from_base(api_base))
        self._bot = Bot(token, session=session)
        self._closed = False
        self._close_event = asyncio.Event()
        self._webhook_updates: asyncio.Queue[Update | None] | None = None
        self._username: str | None = None

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
        except TelegramAPIError as exc:
            raise BotApiError(str(exc.message)) from None

    @staticmethod
    def _markup(reply_markup: ReplyMarkup) -> dict[str, Any]:
        rows = []
        for row in reply_markup.buttons:
            buttons = []
            for button in row:
                if not isinstance(button, InlineButton):
                    button = InlineButton(button[0], button[1])
                raw = {"text": button.text, "callback_data": button.data}
                if button.style is not None:
                    raw["style"] = button.style
                buttons.append(raw)
            rows.append(buttons)
        return {"inline_keyboard": rows}

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        business_connection_id: str | None = None,
        thread_id: int | None = None,
    ) -> SentMessage:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = self._markup(reply_markup)
        if business_connection_id is not None:
            params["business_connection_id"] = business_connection_id
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        response = await self.call_raw("sendMessage", **params)
        return SentMessage(
            int(response["message_id"]), int(response["chat"]["id"]), str(response["text"])
        )

    async def create_topic(self, chat_id: int, title: str) -> int:
        try:
            response = await self.call_raw("createForumTopic", chat_id=chat_id, name=title[:128])
        except BotApiError:
            raise TopicUnavailable("Topic cannot be created") from None
        return int(response["message_thread_id"])

    async def send_checklist(
        self, chat_id: int, business_connection_id: str, title: str, items: Sequence[str]
    ) -> SentMessage:
        response = await self.call_raw(
            "sendChecklist",
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            checklist={
                "title": title[:255],
                "tasks": [
                    {"id": index + 1, "text": item[:100]} for index, item in enumerate(items)
                ],
                "others_can_mark_tasks_as_done": True,
            },
        )
        return SentMessage(int(response["message_id"]), int(response["chat"]["id"]), title)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text is not None:
            params["text"] = text[:200]
        await self.call_raw("answerCallbackQuery", **params)

    async def send_widget(
        self,
        chat_id: int,
        html: str,
        state: str,
        caption: str,
        *,
        thread_id: int | None,
        allow_queries: bool,
        reply_markup: ReplyMarkup | None = None,
    ) -> int:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "html": html,
            "chat_data": state,
            "caption": caption,
            "allow_queries": allow_queries,
        }
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if reply_markup is not None:
            params["reply_markup"] = self._markup(reply_markup)
        try:
            response = await self.call_raw("sendWidget", **params)
        except BotApiError as exc:
            raise WidgetRejected(exc.description) from None
        return int(response["message_id"])

    async def edit_widget(self, chat_id: int, message_id: int, state: str) -> None:
        try:
            await self.call_raw(
                "editMessageWidgetData", chat_id=chat_id, message_id=message_id, chat_data=state
            )
        except BotApiError as exc:
            if NOT_MODIFIED in exc.description.lower():
                return
            raise WidgetRejected(exc.description) from None

    async def answer_widget(self, query_id: str, data: str, *, deadline: datetime) -> None:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise QueryExpired("Widget query answer deadline passed")
        try:
            async with asyncio.timeout(remaining):
                await self.call_raw("answerWidgetQuery", widget_query_id=query_id, data=data)
        except TimeoutError:
            raise QueryExpired("Widget query answer deadline passed") from None
        except BotApiError as exc:
            if any(marker in exc.description.lower() for marker in EXPIRED_QUERY_MARKERS):
                raise QueryExpired("Widget query expired") from None
            raise WidgetRejected(exc.description) from None

    async def bot_username(self) -> str:
        if self._username is None:
            response = await self.call_raw("getMe")
            self._username = str(response["username"])
        return self._username

    @staticmethod
    def _map_update(raw: dict[str, Any]) -> Update:
        return map_update(raw)

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
                    responses = await self.call_raw(
                        "getUpdates",
                        offset=offset,
                        timeout=20,
                        allowed_updates=list(ALLOWED_UPDATES),
                    )
            for raw in responses:
                offset = int(raw["update_id"]) + 1
                yield self._map_update(raw)

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self.call_raw(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=list(ALLOWED_UPDATES),
        )

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
