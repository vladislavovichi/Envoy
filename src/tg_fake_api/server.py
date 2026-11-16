import asyncio
from collections import defaultdict, deque
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
                return web.json_response(
                    {"ok": False, "error_code": 400, "description": "chat_id and text required"},
                    status=400,
                )
            result = {
                "message_id": self._next_message_id,
                "date": 1_767_225_600,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "text": params["text"],
            }
            self._next_message_id += 1
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
