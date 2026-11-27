from collections.abc import Hashable

import structlog

from envoy.app.execution_flow import ExecutionFlow
from envoy.app.locks import SessionLocks, session_key
from envoy.app.owner_flow import OwnerFlow
from envoy.app.ports import (
    BusinessConnectionRepository,
    Job,
    SessionRecord,
    SessionRepository,
    UpdateLedger,
)
from envoy.app.session_flow import SessionFlow
from envoy.app.widget_queries import WidgetQueryFlow
from envoy.app.window import ReplyWindow
from envoy.domain.ids import owner_for_user
from envoy.domain.telegram import (
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    MessageUpdate,
    Update,
    WidgetQueryUpdate,
)
from envoy.security.inbound import Dropped, InboundFilter

__all__ = ["SessionLocks", "UpdateProcessor"]

logger = structlog.get_logger()


class ConfiguredOwnerDirectory:
    def __init__(
        self, owner_ids: frozenset[int], connections: BusinessConnectionRepository
    ) -> None:
        self._owner_ids = owner_ids
        self._connections = connections

    async def is_owner(self, user_id: int) -> bool:
        return user_id in self._owner_ids

    async def connection_owner(self, connection_id: str) -> int | None:
        connection = await self._connections.get(connection_id)
        if connection is None or not connection.enabled:
            return None
        return connection.owner_user_id


class UpdateProcessor:
    def __init__(
        self,
        ledger: UpdateLedger,
        inbound: InboundFilter,
        session_flow: SessionFlow,
        owner_flow: OwnerFlow,
        execution: ExecutionFlow,
        connections: BusinessConnectionRepository,
        sessions: SessionRepository,
        window: ReplyWindow,
        locks: SessionLocks,
        widgets: WidgetQueryFlow | None = None,
    ) -> None:
        self._ledger = ledger
        self._inbound = inbound
        self._sessions_flow = session_flow
        self._owner_flow = owner_flow
        self._execution = execution
        self._connections = connections
        self._sessions = sessions
        self._window = window
        self._locks = locks
        self._widgets = widgets

    async def process(self, update: Update) -> None:
        if await self._ledger.begin(update.update_id) == "done":
            return
        result = await self._inbound.check(update)
        if isinstance(result, Dropped):
            logger.info("update_dropped", update_id=update.update_id, reason=result.reason)
            if isinstance(update, WidgetQueryUpdate) and self._widgets is not None:
                await self._widgets.reject(update)
            await self._ledger.complete(update.update_id)
            return
        await self._dispatch(result.update, result.sender)
        await self._ledger.complete(update.update_id)

    async def _dispatch(self, update: Update, sender: str) -> None:
        match update:
            case BusinessConnectionUpdate():
                await self._connections.upsert(update.connection)
            case MessageUpdate():
                text = (update.text or "").strip()
                if text.split(maxsplit=1)[:1] == ["/start"]:
                    await self._owner_flow.on_start(update.chat_id)
                else:
                    await self._owner_flow.on_text(
                        owner_for_user(update.user_id), update.chat_id, text
                    )
            case CallbackUpdate():
                if update.data.startswith("mandate_"):
                    await self._owner_flow.on_callback(owner_for_user(update.user_id), update)
                else:
                    record = await self._sessions_flow.callback_session(update)
                    key = ("callback",) if record is None else self._chat_key(record)
                    async with self._locks.for_key(key):
                        await self._sessions_flow.on_callback(update)
            case BusinessMessageUpdate():
                await self._business(update, sender)
            case WidgetQueryUpdate():
                if self._widgets is not None:
                    await self._widgets.handle(update)

    async def _business(self, update: BusinessMessageUpdate, sender: str) -> None:
        key = (update.connection_id, update.chat_id)
        async with self._locks.for_key(key):
            if sender == "counterpart":
                await self._window.record_incoming(update.connection_id, update.chat_id)
            if sender == "owner_in_business_chat" and not update.done_task_ids:
                await self._sessions_flow.on_owner_command(update)
                return
            record = await self._sessions.find_active(update.connection_id, update.chat_id)
            if record is None:
                return
            if update.done_task_ids:
                await self._execution.on_tasks_done(record, update)
                await self._sessions_flow.present(record)
            else:
                await self._sessions_flow.on_counterpart(record, update)

    @staticmethod
    def _chat_key(record: SessionRecord) -> Hashable:
        return session_key(record)

    async def resume(self, record: SessionRecord) -> None:
        async with self._locks.for_key(self._chat_key(record)):
            await self._sessions_flow.ensure_jobs(record)
            await self._sessions_flow.flush_undelivered(record)
            await self._sessions_flow.present(record)

    async def run_job(self, job: Job) -> None:
        record = await self._sessions.get(job.session)
        key = ("job", job.key) if record is None else self._chat_key(record)
        async with self._locks.for_key(key):
            await self._sessions_flow.on_job(job)
