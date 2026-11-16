import asyncio
from dataclasses import dataclass
from math import isfinite

from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from envoy.domain.telegram import ReplyMarkup, SentMessage
from envoy.ports.clock import Clock
from envoy.telegram.ports import MessageSender


class RetryAfter(Exception):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__("Telegram rate limit exceeded")


class TransientSendError(Exception):
    pass


def bounded_retry_wait(state: RetryCallState, maximum: float) -> float:
    error = state.outcome.exception() if state.outcome is not None else None
    if isinstance(error, RetryAfter):
        seconds = error.seconds if isfinite(error.seconds) else maximum
        return min(max(seconds, 0.001), maximum)
    return min(float(wait_exponential(multiplier=0.1, min=0.001)(state)), maximum)


@dataclass(frozen=True)
class PendingMessage:
    text: str
    reply_markup: ReplyMarkup | None
    result: asyncio.Future[SentMessage]


class OutboundQueue:
    def __init__(
        self,
        sender: MessageSender,
        global_rate: float,
        per_chat_rate: float,
        max_retry_after: float,
        clock: Clock,
    ) -> None:
        if any(
            not isfinite(value) or value <= 0
            for value in [global_rate, per_chat_rate, max_retry_after]
        ):
            raise ValueError("Rates and maximum retry delay must be finite and positive")
        self._sender = sender
        self._global = AsyncLimiter(1, 1 / global_rate)
        self._admission_interval = min(1 / global_rate, 1 / per_chat_rate, 0.05)
        self._admission_changed = asyncio.Event()
        self._per_chat_rate = per_chat_rate
        self._max_retry_after = max_retry_after
        self._clock = clock
        self._limiters: dict[int, AsyncLimiter] = {}
        self._queues: dict[int, asyncio.Queue[PendingMessage]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._closing = False
        self._closed = asyncio.Event()

    async def submit(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        if self._closing:
            raise RuntimeError("Outbound queue is closed")
        result: asyncio.Future[SentMessage] = asyncio.get_running_loop().create_future()
        queue = self._queues.setdefault(chat_id, asyncio.Queue())
        queue.put_nowait(PendingMessage(text, reply_markup, result))
        worker = self._workers.get(chat_id)
        if worker is None or worker.done():
            self._workers[chat_id] = asyncio.create_task(self._drain(chat_id, queue))
        return await result

    def _wait(self, state: RetryCallState) -> float:
        return bounded_retry_wait(state, self._max_retry_after)

    async def _admit(self, limiter: AsyncLimiter) -> None:
        while not (limiter.has_capacity() and self._global.has_capacity()):
            self._admission_changed.clear()
            try:
                await asyncio.wait_for(
                    self._admission_changed.wait(), timeout=self._admission_interval
                )
            except TimeoutError:
                pass
        await self._global.acquire()
        await limiter.acquire()
        self._admission_changed.set()

    async def _drain(self, chat_id: int, queue: asyncio.Queue[PendingMessage]) -> None:
        limiter = self._limiters.setdefault(chat_id, AsyncLimiter(1, 1 / self._per_chat_rate))
        while not queue.empty():
            pending = queue.get_nowait()
            try:
                if pending.result.cancelled():
                    continue
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type((RetryAfter, TransientSendError)),
                    stop=stop_after_attempt(5),
                    wait=self._wait,
                    reraise=True,
                ):
                    with attempt:
                        await self._admit(limiter)
                        message = await self._sender.send_message(
                            chat_id, pending.text, reply_markup=pending.reply_markup
                        )
                        if not pending.result.done():
                            pending.result.set_result(message)
            except Exception as exc:
                if not pending.result.done():
                    pending.result.set_exception(exc)
            finally:
                queue.task_done()

    async def run(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._closing = True
        await asyncio.gather(*self._workers.values())
        self._closed.set()
