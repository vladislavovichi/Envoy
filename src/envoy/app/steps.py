from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

import structlog

from envoy.app.journal import SessionJournal
from envoy.app.ports import Scheduler, SessionRecord, SessionRepository
from envoy.app.window import ReplyWindow
from envoy.domain.events import Closed, Outcome, SessionEvent
from envoy.domain.ids import MandateId, OfferHash
from envoy.domain.session import SessionState, SessionView, apply, fold
from envoy.domain.telegram import ReplyMarkup
from envoy.mandate.commitment import commitment_digest
from envoy.mandate.compiler import CompiledMandate, compile_mandate
from envoy.mandate.ports import MandateStore, StoredMandate
from envoy.ports.clock import Clock
from envoy.security.authorizer import Action, AuthContext, Authorizer, Verdict
from envoy.telegram.ports import MessageSender, SendRejected

logger = structlog.get_logger()
DAY = timedelta(hours=24)


@dataclass
class SessionContext:
    record: SessionRecord
    stored: StoredMandate
    compiled: CompiledMandate
    commitment: bytes
    events: list[SessionEvent] = field(default_factory=list)
    view: SessionView = field(default_factory=SessionView)

    def find[E: SessionEvent](self, event_type: type[E], cause: int) -> E | None:
        for event in self.events:
            if isinstance(event, event_type) and getattr(event, "cause", None) == cause:
                return event
        return None

    def has(self, event_type: type[SessionEvent]) -> bool:
        return any(isinstance(event, event_type) for event in self.events)


class SessionSteps:
    def __init__(
        self,
        journal: SessionJournal,
        sessions: SessionRepository,
        mandates: MandateStore,
        scheduler: Scheduler,
        sender: MessageSender,
        clock: Clock,
        authorizer: Authorizer,
        window: ReplyWindow,
    ) -> None:
        self.journal = journal
        self.sessions = sessions
        self.mandates = mandates
        self.scheduler = scheduler
        self.sender = sender
        self.clock = clock
        self.authorizer = authorizer
        self.window = window
        self._compiled: dict[MandateId, CompiledMandate] = {}

    async def load(self, record: SessionRecord) -> SessionContext:
        stored = await self.mandates.get(record.mandate_id)
        compiled = self._compiled.get(stored.id)
        if compiled is None:
            compiled = self._compiled[stored.id] = compile_mandate(stored.mandate)
        events = await self.journal.events(record.id)
        return SessionContext(
            record,
            stored,
            compiled,
            commitment_digest(stored.mandate, stored.salt),
            events,
            fold(events),
        )

    async def append(self, ctx: SessionContext, event: SessionEvent) -> SessionEvent:
        view = apply(ctx.view, event)
        await self.journal.append(ctx.record.id, event, ctx.commitment)
        ctx.events.append(event)
        ctx.view = view
        return event

    async def once[E: SessionEvent](
        self,
        ctx: SessionContext,
        cause: int,
        step: str,
        produce: Callable[[], Awaitable[E]],
        event_type: type[E],
    ) -> E:
        existing = ctx.find(event_type, cause)
        if existing is not None:
            logger.info("session_step_replayed", sid=str(ctx.record.id), step=step, cause=cause)
            return existing
        event = await produce()
        await self.append(ctx, event)
        return event

    async def notify_owner(
        self, record: SessionRecord, text: str, markup: ReplyMarkup | None = None
    ) -> None:
        try:
            await self.sender.send_message(
                record.owner_chat_id, text, reply_markup=markup, thread_id=record.topic_id
            )
        except SendRejected:
            logger.warning("owner_notify_failed", sid=str(record.id))

    async def close(self, ctx: SessionContext, outcome: Outcome, reason: str) -> None:
        if ctx.view.state is not SessionState.CLOSED:
            await self.append(ctx, Closed(outcome=outcome, reason=reason))
        await self.sessions.mark_closed(ctx.record.id, self.clock.now())
        for job in await self.scheduler.pending(ctx.record.id):
            await self.scheduler.cancel(job.key)

    async def window_open(self, record: SessionRecord) -> bool:
        return await self.window.is_open(record.connection_id, record.counterpart_chat_id)

    async def authorize(
        self, ctx: SessionContext, action: Action, approved: frozenset[OfferHash] = frozenset()
    ) -> Verdict:
        now = self.clock.now()
        auth = AuthContext(
            view=ctx.view,
            mandate=ctx.compiled,
            counterpart_id=ctx.record.counterpart_chat_id,
            now=now,
            agreed_today=await self.sessions.count_agreed_since(ctx.record.owner_id, now - DAY),
            reply_window_open=await self.window_open(ctx.record),
            approved=approved,
        )
        return self.authorizer.authorize(action, auth)
