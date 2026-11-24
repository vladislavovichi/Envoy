import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from aiohttp import web

from tg_fake_api.errors import ApiFailure
from tg_fake_api.state import RecordedCall


class FakeBotApiServer:
    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self.base_url = ""
        self.webhook: dict[str, Any] = {}
        self._updates: dict[int, dict[str, Any]] = {}
        self._failures: dict[str, deque[ApiFailure]] = defaultdict(deque)
        self._next_message_id = 1
        self._runner: web.AppRunner | None = None
        self._injected = asyncio.Event()
        self.now: Callable[[], float] = time.time
        self.topics: list[dict[str, Any]] = []
        self.checklists: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self._last_incoming: dict[tuple[str, int], float] = {}
        self._next_update_id = 1_000_000

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        app = web.Application()
        app.router.add_route("*", "/bot{token}/{method}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._runner = runner
        address = runner.addresses[0]
        self.base_url = f"http://{host}:{address[1]}"

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def inject_update(self, update: dict[str, Any]) -> None:
        self._updates[int(update["update_id"])] = deepcopy(update)
        self._injected.set()

    def _update_id(self) -> int:
        self._next_update_id += 1
        return self._next_update_id

    def inject_business_message(
        self,
        connection_id: str,
        chat_id: int,
        user_id: int,
        text: str | None = None,
        *,
        update_id: int | None = None,
        edited: bool = False,
        entities: Sequence[dict[str, Any]] = (),
        extra: dict[str, Any] | None = None,
    ) -> int:
        identity = update_id if update_id is not None else self._update_id()
        message: dict[str, Any] = {
            "message_id": identity,
            "date": int(self.now()),
            "business_connection_id": connection_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "User"},
        }
        if text is not None:
            message["text"] = text
        if entities:
            message["entities"] = list(entities)
        message.update(extra or {})
        if user_id == chat_id:
            self._last_incoming[(connection_id, chat_id)] = self.now()
        key = "edited_business_message" if edited else "business_message"
        self.inject_update({"update_id": identity, key: message})
        return identity

    def inject_callback(
        self,
        user_id: int,
        data: str,
        *,
        chat_id: int | None = None,
        message_id: int = 1,
        update_id: int | None = None,
    ) -> int:
        identity = update_id if update_id is not None else self._update_id()
        self.inject_update(
            {
                "update_id": identity,
                "callback_query": {
                    "id": f"cb{identity}",
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "chat_instance": "1",
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "date": int(self.now()),
                        "chat": {"id": chat_id or user_id, "type": "private"},
                    },
                },
            }
        )
        return identity

    def _window_open(self, connection_id: str, chat_id: int) -> bool:
        last = self._last_incoming.get((connection_id, chat_id))
        return last is not None and self.now() - last <= 24 * 3600

    def fail_next(self, method: str, error_code: int, retry_after: float | None = None) -> None:
        self._failures[method].append(ApiFailure(error_code, retry_after))

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params: dict[str, Any] = dict(request.query)
        if request.can_read_body:
            if request.content_type == "application/json":
                params.update(await request.json())
            else:
                params.update(await request.post())
        self.calls.append(RecordedCall(method, deepcopy(params)))
        if self._failures[method]:
            failure = self._failures[method].popleft()
            body: dict[str, Any] = {
                "ok": False,
                "error_code": failure.error_code,
                "description": "Injected API failure",
            }
            if failure.retry_after is not None:
                body["parameters"] = {"retry_after": failure.retry_after}
            return web.json_response(body, status=failure.error_code)
        if method == "getMe":
            result: Any = {
                "id": 123,
                "is_bot": True,
                "first_name": "Envoy",
                "username": "envoy_bot",
            }
        elif method == "sendMessage":
            if "chat_id" not in params or "text" not in params:
                return _bad_request("chat_id and text required")
            chat_id = int(params["chat_id"])
            connection = params.get("business_connection_id")
            if connection is not None and not self._window_open(str(connection), chat_id):
                return _bad_request("Bad Request: business peer usage is missing")
            result = {
                "message_id": self._next_message_id,
                "date": 1_767_225_600,
                "chat": {"id": chat_id, "type": "private"},
                "text": params["text"],
            }
            if connection is not None:
                result["business_connection_id"] = connection
            if "message_thread_id" in params:
                result["message_thread_id"] = int(params["message_thread_id"])
            self._next_message_id += 1
        elif method == "createForumTopic":
            if "chat_id" not in params or not params.get("name"):
                return _bad_request("chat_id and name required")
            topic = {
                "message_thread_id": 500 + len(self.topics),
                "name": params["name"],
                "icon_color": 7322096,
                "chat_id": int(params["chat_id"]),
            }
            self.topics.append(topic)
            result = {key: value for key, value in topic.items() if key != "chat_id"}
        elif method == "sendChecklist":
            checklist = _json_param(params.get("checklist"))
            missing = {"business_connection_id", "chat_id"} - params.keys()
            if missing or not isinstance(checklist, dict) or not checklist.get("tasks"):
                return _bad_request("business_connection_id, chat_id and checklist required")
            chat_id = int(params["chat_id"])
            connection = str(params["business_connection_id"])
            if not self._window_open(connection, chat_id):
                return _bad_request("Bad Request: business peer usage is missing")
            self.checklists.append(
                {"business_connection_id": connection, "chat_id": chat_id, **checklist}
            )
            result = {
                "message_id": self._next_message_id,
                "date": 1_767_225_600,
                "chat": {"id": chat_id, "type": "private"},
                "business_connection_id": connection,
                "checklist": checklist,
            }
            self._next_message_id += 1
        elif method == "answerCallbackQuery":
            if "callback_query_id" not in params:
                return _bad_request("callback_query_id required")
            self.callback_answers.append(params)
            result = True
        elif method == "getUpdates":
            offset = int(params.get("offset", 0))
            self._updates = {key: value for key, value in self._updates.items() if key >= offset}
            timeout = min(max(float(params.get("timeout", 0)), 0), 30)
            if not self._updates and timeout:
                self._injected.clear()
                try:
                    await asyncio.wait_for(self._injected.wait(), timeout=timeout)
                except TimeoutError:
                    pass
            result = [self._updates[key] for key in sorted(self._updates)]
        elif method == "setWebhook":
            self.webhook = params
            result = True
        else:
            return web.json_response(
                {"ok": False, "error_code": 404, "description": "Method not found"}, status=404
            )
        return web.json_response({"ok": True, "result": result})


def _bad_request(description: str) -> web.Response:
    return web.json_response(
        {"ok": False, "error_code": 400, "description": description}, status=400
    )


def _json_param(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
