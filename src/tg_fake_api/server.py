import asyncio
import json
import time
import zlib
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
        self.widgets: dict[int, dict[str, Any]] = {}
        self.description = ""
        self.managed: dict[int, str] = {}
        self.bot_queues: dict[int, dict[int, dict[str, Any]]] = {}
        self.bot_counters: dict[int, int] = defaultdict(int)
        self.bot_events: dict[int, asyncio.Event] = {}
        self.bot_traffic: list[tuple[int, int, str]] = []
        self.bot_tokens: dict[str, int] = {}
        self.drop_bot_messages: list[Callable[[int, int, str], bool]] = []
        self.revoked_tokens: set[str] = set()
        self.access: dict[int, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, str]] = {}
        self.guest_answered: dict[str, Any] = {}
        self.inline_answers: list[dict[str, Any]] = []
        self.widget_edits: list[dict[str, Any]] = []
        self.widget_answers: list[dict[str, Any]] = []
        self._widget_queries: dict[str, float] = {}
        self._query_times: dict[int, deque[float]] = defaultdict(deque)

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

    def inject_bot_message(
        self, from_bot_id: int, text: str, *, update_id: int | None = None
    ) -> int:
        identity = update_id if update_id is not None else self._update_id()
        self.inject_update(
            {
                "update_id": identity,
                "message": {
                    "message_id": identity,
                    "date": int(self.now()),
                    "chat": {"id": from_bot_id, "type": "private"},
                    "from": {"id": from_bot_id, "is_bot": True, "first_name": "Agent"},
                    "text": text,
                },
            }
        )
        return identity

    def register_bot(self, token: str) -> int:
        bot_id = int(token.split(":", 1)[0])
        self.bot_tokens[token] = bot_id
        self.bot_queues.setdefault(bot_id, {})
        self.bot_events.setdefault(bot_id, asyncio.Event())
        return bot_id

    def inject_for_bot(self, bot_id: int, update: dict[str, Any]) -> int:
        self.bot_counters[bot_id] += 1
        identity = self.bot_counters[bot_id]
        self.bot_queues[bot_id][identity] = {**deepcopy(update), "update_id": identity}
        self.bot_events[bot_id].set()
        return identity

    def _bot_of(self, token: str) -> int | None:
        return self.bot_tokens.get(token)

    async def _bot_updates(self, bot_id: int, params: dict[str, Any]) -> list[dict[str, Any]]:
        queue = self.bot_queues[bot_id]
        offset = int(params.get("offset", 0))
        for key in [key for key in queue if key < offset]:
            del queue[key]
        timeout = min(max(float(params.get("timeout", 0)), 0), 30)
        if not queue and timeout:
            event = self.bot_events[bot_id]
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError:
                pass
        return [queue[key] for key in sorted(queue)]

    def inject_managed_bot(self, user_id: int, bot_id: int, username: str) -> int:
        self.managed.setdefault(bot_id, f"{bot_id}:managed")
        identity = self._update_id()
        self.inject_update(
            {
                "update_id": identity,
                "managed_bot": {
                    "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
                    "bot": {
                        "id": bot_id,
                        "is_bot": True,
                        "first_name": "Agent",
                        "username": username,
                    },
                },
            }
        )
        return identity

    def inject_guest_message(self, user_id: int, chat_id: int, text: str) -> str:
        identity = self._update_id()
        query = f"gq{identity}"
        self.inject_update(
            {
                "update_id": identity,
                "guest_message": {
                    "message_id": identity,
                    "date": int(self.now()),
                    "chat": {"id": chat_id, "type": "group"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "Guest"},
                    "text": text,
                    "guest_query_id": query,
                },
            }
        )
        return query

    def inject_inline_query(self, user_id: int, query: str) -> str:
        identity = self._update_id()
        self.inject_update(
            {
                "update_id": identity,
                "inline_query": {
                    "id": f"iq{identity}",
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "query": query,
                    "offset": "",
                },
            }
        )
        return f"iq{identity}"

    def inject_widget_query(
        self,
        user_id: int,
        message_id: int,
        data: str,
        *,
        chat_id: int | None = None,
        query_type: str = "data",
        update_id: int | None = None,
    ) -> str | None:
        if not 1 <= len(data.encode("utf-8")) <= WIDGET_REQUEST_LIMIT:
            raise ValueError("Widget request data must contain 1-64 bytes")
        now = self.now()
        recent = self._query_times[user_id]
        while recent and now - recent[0] >= WIDGET_QUERY_WINDOW:
            recent.popleft()
        if len(recent) >= WIDGET_QUERY_RATE:
            return None
        recent.append(now)
        identity = update_id if update_id is not None else self._update_id()
        query_id = f"wq{identity}"
        self._widget_queries[query_id] = now
        self.inject_update(
            {
                "update_id": identity,
                "widget_query": {
                    "id": query_id,
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "chat": {"id": chat_id or user_id, "type": "private"},
                    "message_id": message_id,
                    "chat_instance": "1",
                    "chat_type": "private",
                    "type": {"type": query_type},
                    "data": data,
                },
            }
        )
        return query_id

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
        token = request.match_info["token"]
        self.calls.append(RecordedCall(method, deepcopy(params), token))
        if token in self.revoked_tokens:
            return web.json_response(
                {"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401
            )
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
        elif (
            method == "sendMessage"
            and self._bot_of(token) is not None
            and (int(params.get("chat_id", 0)) in self.bot_queues)
        ):
            sender = self._bot_of(token) or 0
            target = int(params["chat_id"])
            text = str(params["text"])
            self.bot_traffic.append((sender, target, text))
            dropped = next(
                (rule for rule in self.drop_bot_messages if rule(sender, target, text)), None
            )
            if dropped is not None:
                self.drop_bot_messages.remove(dropped)
                return web.json_response(
                    {
                        "ok": True,
                        "result": {
                            "message_id": 0,
                            "date": 0,
                            "chat": {"id": target},
                            "text": text,
                        },
                    }
                )
            self.bot_counters[target] += 1
            self.bot_queues[target][self.bot_counters[target]] = {
                "update_id": self.bot_counters[target],
                "message": {
                    "message_id": self.bot_counters[target],
                    "date": int(self.now()),
                    "chat": {"id": sender, "type": "private"},
                    "from": {"id": sender, "is_bot": True, "first_name": f"bot{sender}"},
                    "text": str(params["text"]),
                },
            }
            self.bot_events[target].set()
            result = {
                "message_id": self.bot_counters[target],
                "date": 1_767_225_600,
                "chat": {"id": target, "type": "private"},
                "text": params["text"],
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
        elif method == "sendWidget":
            outcome = self._send_widget(params)
            if isinstance(outcome, web.Response):
                return outcome
            result = outcome
        elif method == "editMessageWidgetData":
            edited = self._edit_widget(params)
            if edited is not None:
                return edited
            result = True
        elif method == "answerWidgetQuery":
            answered = self._answer_widget(params)
            if answered is not None:
                return answered
            result = True
        elif method == "answerCallbackQuery":
            if "callback_query_id" not in params:
                return _bad_request("callback_query_id required")
            self.callback_answers.append(params)
            result = True
        elif method == "getUpdates" and self._bot_of(token) is not None:
            result = await self._bot_updates(self._bot_of(token) or 0, params)
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
        elif method == "answerGuestQuery":
            if "guest_query_id" not in params or "result" not in params:
                return _bad_request("Bad Request: guest_query_id and result required")
            query = str(params["guest_query_id"])
            if query in self.guest_answered:
                return _bad_request("Bad Request: QUERY_ID_INVALID")
            self.guest_answered[query] = _json_param(params["result"])
            result = {"inline_message_id": f"im{query}"}
        elif method == "answerInlineQuery":
            results = _json_param(params.get("results"))
            if "inline_query_id" not in params or not isinstance(results, list):
                return _bad_request("Bad Request: inline_query_id and results required")
            if len(results) > 50:
                return _bad_request("Bad Request: RESULTS_TOO_MUCH")
            self.inline_answers.append(
                {"inline_query_id": params["inline_query_id"], "results": results}
            )
            result = True
        elif method in ("getManagedBotToken", "replaceManagedBotToken"):
            bot = int(params.get("user_id", 0))
            if bot not in self.managed:
                return _bad_request("Bad Request: bot not managed")
            if method == "replaceManagedBotToken":
                self.revoked_tokens.add(self.managed[bot])
                self.managed[bot] = f"{bot}:replaced{len(self.revoked_tokens)}"
            result = self.managed[bot]
        elif method == "setManagedBotAccessSettings":
            bot = int(params.get("user_id", 0))
            users = _json_param(params.get("added_user_ids")) or []
            if bot not in self.managed:
                return _bad_request("Bad Request: bot not managed")
            if len(users) > 10:
                return _bad_request("Bad Request: too many users")
            self.access[bot] = {
                "restricted": _flag(params.get("is_access_restricted")),
                "users": users,
            }
            result = True
        elif method == "setMyName":
            self.profiles.setdefault(token, {})["name"] = str(params.get("name", ""))
            result = True
        elif method == "setMyDescription":
            self.profiles.setdefault(token, {})["description"] = str(params.get("description", ""))
            self.description = str(params.get("description", ""))
            result = True
        elif method == "setWebhook":
            self.webhook = params
            result = True
        else:
            return web.json_response(
                {"ok": False, "error_code": 404, "description": "Method not found"}, status=404
            )
        return web.json_response({"ok": True, "result": result})

    def _send_widget(self, params: dict[str, Any]) -> dict[str, Any] | web.Response:
        if "chat_id" not in params or "html" not in params:
            return _bad_request("Bad Request: chat_id and html required")
        raw = str(params["html"]).encode("utf-8")
        if not raw or b"\x00" in raw:
            return _bad_request("Bad Request: INPUT_MEDIA_WIDGET_HTML_INVALID")
        if len(raw) > WIDGET_HTML_LIMIT or len(zlib.compress(raw, 9)) > WIDGET_COMPRESSED_LIMIT:
            return _bad_request("Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE")
        data_error = _data_error(params, "INPUT_MEDIA_")
        if data_error is not None:
            return data_error
        caption = str(params.get("caption", ""))
        if len(caption) > CAPTION_LIMIT:
            return _bad_request("Bad Request: message caption is too long")
        chat_id = int(params["chat_id"])
        message_id = self._next_message_id
        self._next_message_id += 1
        widget = {
            "chat_id": chat_id,
            "html": raw.decode("utf-8"),
            "chat_data": params.get("chat_data"),
            "public_data": params.get("public_data"),
            "caption": caption,
            "allow_queries": _flag(params.get("allow_queries")),
            "message_thread_id": params.get("message_thread_id"),
            "reply_markup": _json_param(params.get("reply_markup")),
        }
        self.widgets[message_id] = widget
        message: dict[str, Any] = {
            "message_id": message_id,
            "date": 1_767_225_600,
            "chat": {"id": chat_id, "type": "private"},
            "widget": {
                key: widget[key] for key in ("html", "chat_data", "public_data") if widget[key]
            },
        }
        if caption:
            message["caption"] = caption
        if widget["message_thread_id"] is not None:
            message["message_thread_id"] = int(widget["message_thread_id"])
        return message

    def _edit_widget(self, params: dict[str, Any]) -> web.Response | None:
        if "chat_id" not in params or "message_id" not in params:
            return _bad_request("Bad Request: chat_id and message_id required")
        widget = self.widgets.get(int(params["message_id"]))
        if widget is None or widget["chat_id"] != int(params["chat_id"]):
            return _bad_request("Bad Request: message to edit not found")
        data_error = _data_error(params, "")
        if data_error is not None:
            return data_error
        changes: dict[str, Any] = {
            key: params[key] for key in ("chat_data", "public_data") if key in params
        }
        if all(widget[key] == value for key, value in changes.items()):
            return _bad_request("Bad Request: message is not modified")
        widget.update(changes)
        self.widget_edits.append({"message_id": int(params["message_id"]), **changes})
        return None

    def _answer_widget(self, params: dict[str, Any]) -> web.Response | None:
        query_id = str(params.get("widget_query_id", ""))
        received = self._widget_queries.pop(query_id, None)
        if received is None or self.now() - received >= WIDGET_ANSWER_SECONDS:
            return _bad_request(EXPIRED_QUERY)
        if len(str(params.get("data", "")).encode("utf-8")) > WIDGET_ANSWER_LIMIT:
            self._widget_queries[query_id] = received
            return _bad_request("Bad Request: WIDGET_RESULT_TOO_LARGE")
        self.widget_answers.append(params)
        return None


WIDGET_HTML_LIMIT = 32768
WIDGET_COMPRESSED_LIMIT = 4096
WIDGET_DATA_LIMIT = 1024
CAPTION_LIMIT = 1024
WIDGET_REQUEST_LIMIT = 64
WIDGET_ANSWER_LIMIT = 1024
WIDGET_ANSWER_SECONDS = 15
WIDGET_QUERY_RATE = 5
WIDGET_QUERY_WINDOW = 5
EXPIRED_QUERY = "Bad Request: query is too old and response timeout expired or query ID is invalid"


def _data_error(params: dict[str, Any], prefix: str) -> web.Response | None:
    for key in ("chat_data", "public_data"):
        value = params.get(key)
        if value is None:
            continue
        raw = str(value).encode("utf-8")
        if b"\x00" in raw:
            return _bad_request(f"Bad Request: {prefix}WIDGET_DATA_INVALID")
        if len(raw) > WIDGET_DATA_LIMIT:
            return _bad_request(f"Bad Request: {prefix}WIDGET_DATA_TOO_LARGE")
    return None


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


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
