import asyncio
import re
from datetime import timedelta

import structlog

from envoy.app.escalation import Choice, EscalationResolver, ResolveResult
from envoy.app.locks import SessionLocks, session_key
from envoy.app.owner_view import TEXT_VERSION, TextOwnerView
from envoy.app.ports import OwnerViewStore, SessionRecord, SessionRepository
from envoy.domain.ids import SessionId, owner_for_user
from envoy.domain.telegram import WidgetQueryUpdate
from envoy.ports.clock import Clock
from envoy.telegram.ports import QueryExpired, SendRejected, WidgetTransport

logger = structlog.get_logger()
DECISION_BUDGET = timedelta(seconds=10)
ANSWER_DEADLINE = timedelta(seconds=15)
ESCALATION_QUERY = re.compile(r"([ad]):(0|[1-9][0-9]*)")
LINK_QUERY = re.compile(r"l:(0|[1-9][0-9]*):(0|[1-9][0-9]*)")
ANSWERS: dict[str, str] = {
    "recorded": "ok",
    "stale": "stale",
    "expired": "expired",
    "forbidden": "no",
    "invalid": "no",
    "busy": "wait",
    "pending": "wait",
    "failed": "error",
    "sent": "ok",
}


class WidgetQueryFlow:
    def __init__(
        self,
        transport: WidgetTransport,
        sessions: SessionRepository,
        store: OwnerViewStore,
        resolver: EscalationResolver,
        locks: SessionLocks,
        clock: Clock,
        text: TextOwnerView,
    ) -> None:
        self._transport = transport
        self._sessions = sessions
        self._store = store
        self._resolver = resolver
        self._locks = locks
        self._clock = clock
        self._text = text
        self._linked: set[str] = set()

    async def reject(self, update: WidgetQueryUpdate) -> None:
        await self._answer(update, "invalid")

    async def handle(self, update: WidgetQueryUpdate) -> None:
        data = update.data or ""
        escalation = ESCALATION_QUERY.fullmatch(data)
        if escalation is not None:
            choice: Choice = "approve" if escalation.group(1) == "a" else "decline"
            await self._escalation(update, choice, int(escalation.group(2)))
            return
        link = LINK_QUERY.fullmatch(data)
        if link is not None:
            await self._answer(update, await self._link(update, int(link[1]), int(link[2])))
            return
        await self._answer(update, "invalid")

    async def _answer(self, update: WidgetQueryUpdate, result: str) -> None:
        try:
            await self._transport.answer_widget(
                update.query_id, ANSWERS[result], deadline=update.received_at + ANSWER_DEADLINE
            )
        except QueryExpired:
            logger.warning("widget_query_expired", update_id=update.update_id)
        except SendRejected:
            logger.warning("widget_answer_rejected", update_id=update.update_id)

    async def _bound(self, update: WidgetQueryUpdate, cause: int) -> SessionRecord | None:
        if update.chat_id is None or update.message_id is None:
            return None
        found = await self._store.find_escalation(update.chat_id, update.message_id)
        if found is None or found[1] != cause:
            return None
        ref = await self._store.get_escalation(*found)
        if ref is None or ref.version == TEXT_VERSION:
            return None
        if (ref.chat_id, ref.message_id) != (update.chat_id, update.message_id):
            return None
        record = await self._sessions.get(found[0])
        if record is None or record.owner_chat_id != update.chat_id:
            return None
        if record.owner_id != owner_for_user(update.user_id):
            return None
        return record

    async def _escalation(self, update: WidgetQueryUpdate, choice: Choice, cause: int) -> None:
        record = await self._bound(update, cause)
        if record is None:
            await self._answer(update, "invalid")
            return
        budget = (update.received_at + DECISION_BUDGET - self._clock.now()).total_seconds()
        if budget <= 0:
            await self._answer(update, "busy")
            return
        answered: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._decide(record.id, record, update, choice, cause, budget, answered)
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(answered), timeout=budget)
        except TimeoutError:
            result = "pending"
        await self._answer(update, result)
        await task

    async def _decide(
        self,
        session: SessionId,
        record: SessionRecord,
        update: WidgetQueryUpdate,
        choice: Choice,
        cause: int,
        budget: float,
        answered: asyncio.Future[str],
    ) -> None:
        lock = self._locks.for_key(session_key(record))
        try:
            async with asyncio.timeout(budget):
                await lock.acquire()
        except TimeoutError:
            answered.set_result("busy")
            return
        try:
            result: ResolveResult
            result, ctx = await self._resolver.record(session, update.user_id, cause, choice)
            answered.set_result(result)
            if ctx is not None:
                await self._resolver.proceed(ctx, cause, update.update_id)
        except BaseException:
            if not answered.done():
                answered.set_result("failed")
            raise
        finally:
            lock.release()

    async def _link(self, update: WidgetQueryUpdate, revision: int, index: int) -> str:
        if update.chat_id is None or update.message_id is None:
            return "invalid"
        summary = await self._store.find_summary(update.chat_id, update.message_id)
        if summary is None or summary.target.user_id != update.user_id:
            return "invalid"
        if summary.target.chat_id != update.chat_id:
            return "invalid"
        if revision != summary.ref.revision:
            return "stale"
        if index >= len(summary.rows) or summary.rows[index].topic_url is None:
            return "invalid"
        if update.query_id in self._linked:
            return "sent"
        self._linked.add(update.query_id)
        row = summary.rows[index]
        await self._text.deliver(summary.target, f"{row.label}: {row.topic_url}")
        return "sent"
