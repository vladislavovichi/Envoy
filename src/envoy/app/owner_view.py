from datetime import date
from typing import Protocol

import structlog

from envoy.app.ports import OwnerTarget, OwnerViewStore, SessionRecord, WidgetRef
from envoy.app.renderer import ISSUE_LABELS, TemplateRenderer
from envoy.app.steps import SessionContext
from envoy.domain.events import Closed, Decided, Escalated, IntentParsed, Outcome, OwnerResolved
from envoy.domain.ids import SessionId
from envoy.domain.intent import Propose
from envoy.domain.offer import Offer
from envoy.domain.telegram import ReplyMarkup
from envoy.security.widget_projection import (
    CardOutcome,
    Dashboard,
    EscalationCard,
    SummaryRow,
    WidgetProjection,
)
from envoy.telegram.ports import MessageSender, SendRejected

logger = structlog.get_logger()
TEXT_LIMIT = 4096
TEXT_VERSION = 0


class OwnerView(Protocol):
    async def dashboard(self, target: OwnerTarget, session: SessionId, card: Dashboard) -> None: ...
    async def escalation(
        self, target: OwnerTarget, session: SessionId, card: EscalationCard
    ) -> None: ...
    async def summary(
        self, target: OwnerTarget, day: date, rows: tuple[SummaryRow, ...]
    ) -> None: ...


def target_for(record: SessionRecord) -> OwnerTarget:
    return OwnerTarget(record.owner_id, record.owner_chat_id, record.owner_chat_id, record.topic_id)


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            chunks.append(current)
            candidate = line
        current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


class TextOwnerView:
    def __init__(
        self, sender: MessageSender, renderer: TemplateRenderer, store: OwnerViewStore
    ) -> None:
        self._sender = sender
        self._renderer = renderer
        self._store = store

    @property
    def renderer(self) -> TemplateRenderer:
        return self._renderer

    async def _send(
        self, target: OwnerTarget, text: str, markup: ReplyMarkup | None, thread_id: int | None
    ) -> int:
        try:
            message = await self._sender.send_message(
                target.chat_id, text, reply_markup=markup, thread_id=thread_id
            )
        except SendRejected:
            if thread_id is None:
                raise
            logger.warning("owner_topic_unavailable", chat=target.chat_id)
            return await self._send(target, text, markup, None)
        return message.message_id

    async def deliver(
        self, target: OwnerTarget, text: str, markup: ReplyMarkup | None = None
    ) -> int | None:
        chunks = split_text(text)
        message_id: int | None = None
        try:
            for index, chunk in enumerate(chunks):
                last = index == len(chunks) - 1
                message_id = await self._send(
                    target, chunk, markup if last else None, target.thread_id
                )
        except SendRejected:
            logger.warning("owner_notify_failed", chat=target.chat_id)
            return None
        return message_id

    async def dashboard(self, target: OwnerTarget, session: SessionId, card: Dashboard) -> None:
        return None

    async def escalation(
        self, target: OwnerTarget, session: SessionId, card: EscalationCard
    ) -> None:
        if card.outcome != "pending":
            return
        if await self._store.get_escalation(session, card.cause) is not None:
            return
        rendered = self._renderer.owner_escalation(card)
        message_id = await self.deliver(target, rendered.text, rendered.markup())
        if message_id is not None:
            await self._store.put_escalation(
                session,
                card.cause,
                WidgetRef(target.chat_id, message_id, TEXT_VERSION, card.revision),
            )

    async def summary(self, target: OwnerTarget, day: date, rows: tuple[SummaryRow, ...]) -> None:
        await self.deliver(target, self._renderer.owner_summary(day, rows, len(rows)))


class OwnerPresenter:
    def __init__(self, view: OwnerView, renderer: TemplateRenderer) -> None:
        self._view = view
        self._projection = WidgetProjection(renderer.value, ISSUE_LABELS)

    @property
    def projection(self) -> WidgetProjection:
        return self._projection

    def dashboard(self, ctx: SessionContext) -> Dashboard:
        return self._projection.dashboard(
            revision=len(ctx.events),
            view=ctx.view,
            max_rounds=ctx.stored.mandate.limits.max_rounds,
            issues=[issue.id for issue in ctx.compiled.grid.issues],
        )

    def escalations(self, ctx: SessionContext) -> list[EscalationCard]:
        cards: list[EscalationCard] = []
        theirs: Offer | None = None
        for index, event in enumerate(ctx.events):
            if isinstance(event, IntentParsed) and isinstance(event.intent, Propose):
                theirs = event.intent.offer
            if not (isinstance(event, Escalated) and event.reason == "confirm_action"):
                continue
            if event.expires_at is None:
                continue
            outcome, revision = self._outcome(ctx, event.cause, index)
            decided = ctx.find(Decided, event.cause)
            cards.append(
                self._projection.escalation(
                    revision=revision,
                    cause=event.cause,
                    expires_at=event.expires_at,
                    offer=None if decided is None else decided.offer,
                    theirs=theirs,
                    outcome=outcome,
                )
            )
        return cards

    @staticmethod
    def _outcome(ctx: SessionContext, cause: int, start: int) -> tuple[CardOutcome, int]:
        for index in range(start + 1, len(ctx.events)):
            event = ctx.events[index]
            if isinstance(event, OwnerResolved) and event.cause == cause:
                return event.resolution, index + 1
            if isinstance(event, Closed):
                outcome: CardOutcome = "timeout" if event.outcome is Outcome.expired else "declined"
                return outcome, index + 1
        return "pending", start + 1

    async def refresh(self, ctx: SessionContext) -> None:
        record = ctx.record
        target = target_for(record)
        try:
            await self._view.dashboard(target, record.id, self.dashboard(ctx))
            for card in self.escalations(ctx):
                await self._view.escalation(target, record.id, card)
        except Exception:
            logger.exception("owner_view_failed", sid=str(record.id))
