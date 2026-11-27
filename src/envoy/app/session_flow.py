from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

import structlog

from envoy.app.escalation import Choice, EscalationResolver, parse_cause
from envoy.app.execution_flow import ExecutionFlow
from envoy.app.owner_view import OwnerPresenter
from envoy.app.ports import BusinessConnectionRepository, Job, SessionRecord
from envoy.app.renderer import MessageRenderer
from envoy.app.steps import SessionContext, SessionSteps
from envoy.domain.events import (
    Decided,
    Delivered,
    Disclosed,
    Escalated,
    IntentParsed,
    MandateAttested,
    Outcome,
    OwnerResolved,
    Resolution,
    SessionOpened,
)
from envoy.domain.ids import MandateId, OfferHash, SessionId, owner_for_user
from envoy.domain.intent import AcceptIntent, Propose, RejectIntent, Unclear
from envoy.domain.offer import Offer
from envoy.domain.session import SessionState
from envoy.domain.telegram import BusinessConnection, BusinessMessageUpdate, CallbackUpdate
from envoy.engine.engine import Accept, Counter, NegotiationEngine
from envoy.llm.translator import IssueDescriptor, TranslationFailed, Translator
from envoy.llm.validation import validate_intent
from envoy.mandate.compiler import CompiledMandate
from envoy.security.authorizer import Action, Allow, Deny, Escalate
from envoy.security.projection import ClarificationProjection, OfferProjection
from envoy.telegram.links import topic_link
from envoy.telegram.ports import (
    BotIdentity,
    CallbackAnswerer,
    SendRejected,
    TopicCreator,
    TopicUnavailable,
)

logger = structlog.get_logger()
SESSION_NAMESPACE = UUID("2b0c8e4f-9a61-4d7e-8f35-6c1d0a9e7b52")
UNCLEAR_LIMIT = 3
CLOSING_REASONS = {"escalation_timeout", "session_deadline"}

type EngineFactory = Callable[[CompiledMandate], NegotiationEngine]


def _closed(ctx: SessionContext) -> bool:
    return ctx.view.state is SessionState.CLOSED


def session_id_for(connection_id: str, chat_id: int, cause: int) -> SessionId:
    return SessionId(uuid5(SESSION_NAMESPACE, f"{connection_id}:{chat_id}:{cause}"))


class SessionFlow:
    def __init__(
        self,
        steps: SessionSteps,
        connections: BusinessConnectionRepository,
        execution: ExecutionFlow,
        translator: Translator,
        engines: EngineFactory,
        topics: TopicCreator,
        answerer: CallbackAnswerer,
        identity: BotIdentity,
        messages: MessageRenderer,
        presenter: OwnerPresenter,
        timezone: str,
        agent_name: str,
    ) -> None:
        self._steps = steps
        self._connections = connections
        self._execution = execution
        self._translator = translator
        self._engines = engines
        self._topics = topics
        self._answerer = answerer
        self._identity = identity
        self._messages = messages
        self._presenter = presenter
        self.resolver = EscalationResolver(steps, self)
        self._timezone = timezone
        self._agent_name = agent_name

    async def open(
        self, connection: BusinessConnection, chat_id: int, cause: int
    ) -> SessionId | None:
        steps = self._steps
        owner = owner_for_user(connection.owner_user_id)
        identity = session_id_for(connection.id, chat_id, cause)
        record = await steps.sessions.get(identity)
        if record is None:
            active = await steps.sessions.find_active(connection.id, chat_id)
            if active is not None:
                await steps.notify_owner(active, "В этом чате уже идёт сессия Envoy.")
                return active.id
            stored = await steps.mandates.active(owner)
            if stored is None:
                await steps.sender.send_message(
                    connection.user_chat_id,
                    "Нет активного мандата. Опишите здесь, когда и где вам удобно встретиться, "
                    "и подтвердите черновик — затем снова напишите /envoy в чате.",
                )
                return None
            try:
                topic = await self._topics.create_topic(
                    connection.user_chat_id, f"Встреча · чат {chat_id}"
                )
            except TopicUnavailable:
                topic = None
            record = SessionRecord(
                id=identity,
                owner_id=owner,
                mandate_id=stored.id,
                connection_id=connection.id,
                counterpart_chat_id=chat_id,
                owner_chat_id=connection.user_chat_id,
                topic_id=topic,
                deadline=steps.clock.now() + stored.mandate.limits.session_deadline,
            )
            await steps.sessions.create(record)
        ctx = await steps.load(record)
        if not ctx.has(SessionOpened):
            await steps.append(
                ctx,
                SessionOpened(
                    owner_id=record.owner_id,
                    mandate_id=record.mandate_id,
                    connection_id=record.connection_id,
                    counterpart_chat_id=record.counterpart_chat_id,
                    owner_chat_id=record.owner_chat_id,
                    topic_id=record.topic_id,
                    deadline=record.deadline,
                ),
            )
            await steps.append(
                ctx,
                MandateAttested(mandate_id=MandateId(ctx.stored.id), version=ctx.stored.version),
            )
            await steps.scheduler.schedule(self._deadline_job(record))
            await self._announce(record)
        if not await self._disclose(ctx, cause):
            return record.id
        if ctx.view.round == 0 and ctx.view.last_decision is None:
            decision = self._engines(ctx.compiled).decide(None, (), 0)
            if isinstance(decision, Counter):
                await self._act(ctx, cause, Action("send_offer", decision.offer))
        for pending in list(ctx.view.undelivered):
            await self._deliver(ctx, pending)
        await self._presenter.refresh(ctx)
        return record.id

    async def present(self, record: SessionRecord) -> None:
        await self._presenter.refresh(await self._steps.load(record))

    @staticmethod
    def _deadline_job(record: SessionRecord) -> Job:
        return Job(
            f"{record.id}:deadline",
            "session_deadline",
            record.id,
            record.deadline,
            {"phase": "negotiation"},
        )

    @staticmethod
    def _escalation_job(record: SessionRecord, cause: int, expires: datetime) -> Job:
        return Job(
            f"{record.id}:escalation:{cause}",
            "escalation_timeout",
            record.id,
            expires,
            {"cause": cause},
        )

    async def ensure_jobs(self, record: SessionRecord) -> None:
        ctx = await self._steps.load(record)
        state = ctx.view.state
        scheduler = self._steps.scheduler
        if state in (SessionState.NEGOTIATING, SessionState.PENDING_OWNER):
            await scheduler.schedule(self._deadline_job(record))
        pending = ctx.view.pending
        if pending is not None and pending.expires_at is not None:
            await scheduler.schedule(
                self._escalation_job(record, pending.cause, pending.expires_at)
            )
        if state is SessionState.EXECUTING:
            await self._execution.schedule(ctx)

    async def _announce(self, record: SessionRecord) -> None:
        text = f"Начал переговоры в чате {record.counterpart_chat_id}."
        if record.topic_id is not None:
            link = topic_link(await self._identity.bot_username(), record.topic_id)
            text = f"{text} Ход сессии: {link}"
        await self._steps.sender.send_message(record.owner_chat_id, text)

    async def _disclose(self, ctx: SessionContext, cause: int) -> bool:
        if ctx.view.disclosed:
            return True
        record = ctx.record
        if await self._steps.window_open(record):
            owner_name = "владельца аккаунта"
            try:
                await self._steps.sender.send_message(
                    record.counterpart_chat_id,
                    self._messages.disclosure(owner_name, self._agent_name),
                    business_connection_id=record.connection_id,
                )
            except SendRejected:
                pass
            else:
                await self._steps.append(ctx, Disclosed())
                return True
        if ctx.find(Escalated, cause) is None:
            await self._steps.append(ctx, Escalated(cause=cause, reason="reply_window_closed"))
            await self._steps.notify_owner(
                record, self._messages.owner_notice("reply_window_closed")
            )
        return False

    async def _act(
        self,
        ctx: SessionContext,
        cause: int,
        action: Action,
        approved: frozenset[OfferHash] = frozenset(),
    ) -> None:
        steps = self._steps
        decided = ctx.find(Decided, cause)
        if decided is None:
            verdict = await steps.authorize(ctx, action, approved)
            match verdict:
                case Allow() | Escalate(reason="reply_window_closed"):
                    kind = "allow"
                case Escalate():
                    kind = "escalate"
                case Deny():
                    kind = "deny"
            decided = Decided(
                cause=cause,
                kind=action.kind,
                offer=action.offer,
                verdict=kind,
                reason=verdict.reason if isinstance(verdict, Deny) else action.reason,
                missing=action.missing,
            )
            await steps.append(ctx, decided)
            if decided.verdict == "allow" and decided.kind == "accept_offer":
                await steps.sessions.mark_agreed(ctx.record.id, steps.clock.now())
            if isinstance(verdict, Deny):
                logger.info("authorizer_denied", sid=str(ctx.record.id), reason=verdict.reason)
                await steps.notify_owner(ctx.record, self._messages.owner_notice(verdict.reason))
        if decided.verdict == "escalate":
            await self._escalate(ctx, decided)
        elif decided.verdict == "allow":
            await self._deliver(ctx, decided)

    async def _escalate(self, ctx: SessionContext, decided: Decided) -> None:
        if ctx.find(Escalated, decided.cause) is not None:
            return
        expires = self._steps.clock.now() + ctx.compiled.escalation.timeout
        await self._steps.append(
            ctx, Escalated(cause=decided.cause, reason="confirm_action", expires_at=expires)
        )
        await self._steps.scheduler.schedule(
            self._escalation_job(ctx.record, decided.cause, expires)
        )

    def _text(self, decided: Decided) -> str:
        match decided.kind:
            case "send_offer" | "accept_offer" if decided.offer is not None:
                offer = Offer(
                    values={
                        issue: value
                        for issue, value in OfferProjection().project(decided.offer).items()
                        if isinstance(value, (str, int)) and not isinstance(value, bool)
                    }
                )
                if decided.kind == "send_offer":
                    return self._messages.offer(offer)
                return self._messages.accept(offer)
            case "ask_clarification":
                missing = ClarificationProjection().project(decided.missing)["missing"]
                return self._messages.clarification(
                    [str(item) for item in missing] if isinstance(missing, list) else []
                )
            case _:
                return self._messages.rejection(decided.reason)

    async def _deliver(self, ctx: SessionContext, decided: Decided) -> None:
        if decided not in ctx.view.undelivered or ctx.view.state is SessionState.CLOSED:
            return
        record = ctx.record
        if not await self._steps.window_open(record):
            await self._park(ctx, decided.cause)
            return
        try:
            message = await self._steps.sender.send_message(
                record.counterpart_chat_id,
                self._text(decided),
                business_connection_id=record.connection_id,
            )
        except SendRejected:
            await self._park(ctx, decided.cause)
            return
        await self._steps.append(ctx, Delivered(cause=decided.cause, message_id=message.message_id))
        await self._after_delivery(ctx, decided)

    async def _park(self, ctx: SessionContext, cause: int) -> None:
        if ctx.find(Escalated, cause) is None:
            await self._steps.append(ctx, Escalated(cause=cause, reason="reply_window_closed"))
            await self._steps.notify_owner(
                ctx.record, self._messages.owner_notice("reply_window_closed")
            )

    async def _after_delivery(self, ctx: SessionContext, decided: Decided) -> None:
        if decided.kind == "accept_offer":
            await self._execution.open(ctx, decided.cause)
        elif decided.kind == "reject_session" and ctx.view.state is not SessionState.CLOSED:
            outcome = Outcome.expired if decided.reason in CLOSING_REASONS else Outcome.rejected
            await self._steps.close(ctx, outcome, decided.reason or "rejected")
            await self._steps.notify_owner(ctx.record, "Сессия закрыта без сделки.")

    async def flush_undelivered(self, record: SessionRecord) -> None:
        ctx = await self._steps.load(record)
        if ctx.view.state in (SessionState.INIT, SessionState.CLOSED):
            return
        await self.recover_resolution(ctx)
        if _closed(ctx):
            return
        if not ctx.view.disclosed:
            if not await self._disclose(ctx, 0):
                return
        for decided in list(ctx.view.undelivered):
            await self._deliver(ctx, decided)
        if ctx.view.state is SessionState.AGREED and ctx.view.last_decision is not None:
            if not ctx.view.undelivered:
                await self._execution.open(ctx, ctx.view.last_decision.cause)

    async def recover_resolution(self, ctx: SessionContext) -> None:
        for event in reversed(ctx.events):
            if isinstance(event, Decided):
                return
            if isinstance(event, OwnerResolved):
                await self.proceed(ctx, event.cause, -event.cause)
                return

    async def on_counterpart(self, record: SessionRecord, update: BusinessMessageUpdate) -> None:
        await self._on_counterpart(record, update)
        await self.present(record)

    async def _on_counterpart(self, record: SessionRecord, update: BusinessMessageUpdate) -> None:
        await self.flush_undelivered(record)
        ctx = await self._steps.load(record)
        if ctx.view.state is not SessionState.NEGOTIATING or not ctx.view.disclosed:
            return
        cause = update.update_id
        parsed = ctx.find(IntentParsed, cause)
        if parsed is None:
            if ctx.find(Escalated, cause) is not None:
                return
            descriptors = [
                IssueDescriptor(issue.id, issue.kind) for issue in ctx.compiled.grid.issues
            ]
            try:
                intent = await self._translator.parse_intent(
                    update.text or "", descriptors, self._steps.clock.now(), self._timezone
                )
            except TranslationFailed:
                await self._steps.append(ctx, Escalated(cause=cause, reason="translator_failed"))
                await self._steps.notify_owner(
                    record, self._messages.owner_notice("translator_failed")
                )
                return
            parsed = IntentParsed(cause=cause, intent=validate_intent(intent, ctx.compiled.grid))
            await self._steps.append(ctx, parsed)
        action = await self._action_for(ctx, cause, parsed)
        if action is not None:
            await self._act(ctx, cause, action)

    async def _action_for(
        self, ctx: SessionContext, cause: int, parsed: IntentParsed
    ) -> Action | None:
        view = ctx.view
        match parsed.intent:
            case Propose(offer=offer):
                decision = self._engines(ctx.compiled).decide(offer, view.history, view.round)
                match decision:
                    case Accept():
                        return Action("accept_offer", decision.offer)
                    case Counter():
                        return Action("send_offer", decision.offer)
                    case _:
                        return Action("reject_session", reason=decision.reason)
            case AcceptIntent():
                if view.last_outgoing is None:
                    return Action("ask_clarification", missing=ctx.compiled.grid.ids)
                return Action("accept_offer", view.last_outgoing)
            case RejectIntent():
                return Action("reject_session", reason="counterpart_rejected")
            case Unclear(missing=missing):
                if view.clarifications >= UNCLEAR_LIMIT:
                    if ctx.find(Escalated, cause) is None and ctx.find(Decided, cause) is None:
                        await self._steps.append(
                            ctx, Escalated(cause=cause, reason="counterpart_unclear")
                        )
                        await self._steps.notify_owner(
                            ctx.record, self._messages.owner_notice("counterpart_unclear")
                        )
                    return None
                return Action("ask_clarification", missing=missing or ctx.compiled.grid.ids)

    async def on_owner_command(self, update: BusinessMessageUpdate) -> None:
        command = (update.text or "").strip().split(maxsplit=1)
        name = command[0].lower() if command else ""
        if name == "/envoy":
            connection = await self._connections.get(update.connection_id)
            if connection is None:
                connection = BusinessConnection(
                    update.connection_id, update.user_id, update.user_id
                )
            await self.open(connection, update.chat_id, update.update_id)
        elif name == "/stop":
            record = await self._steps.sessions.find_active(update.connection_id, update.chat_id)
            if record is not None:
                ctx = await self._steps.load(record)
                await self._steps.close(ctx, Outcome.rejected, "owner_stopped")
                await self._steps.notify_owner(record, "Сессия остановлена по вашей команде.")
                await self._presenter.refresh(ctx)

    async def _find_pending(self, cause: int) -> SessionContext | None:
        for record in await self._steps.sessions.list_active():
            ctx = await self._steps.load(record)
            if ctx.view.pending is not None and ctx.view.pending.cause == cause:
                return ctx
        return None

    async def record_resolution(self, ctx: SessionContext, resolution: Resolution) -> int | None:
        pending = ctx.view.pending
        if pending is None:
            return None
        await self._steps.append(ctx, OwnerResolved(cause=pending.cause, resolution=resolution))
        return pending.cause

    async def proceed(self, ctx: SessionContext, cause: int, trigger: int) -> None:
        resolved = ctx.find(OwnerResolved, cause)
        if resolved is None or ctx.view.state is SessionState.CLOSED:
            return
        decided = ctx.find(Decided, cause)
        if resolved.resolution == "approved" and decided is not None:
            approved = frozenset() if decided.offer is None else frozenset({decided.offer.hash()})
            await self._act(ctx, trigger, Action(decided.kind, decided.offer), approved)
            return
        reason = "escalation_timeout" if resolved.resolution == "timeout" else "owner_declined"
        await self._act(ctx, trigger, Action("reject_session", reason=reason))
        if not _closed(ctx):
            outcome = Outcome.expired if resolved.resolution == "timeout" else Outcome.rejected
            await self._steps.close(ctx, outcome, reason)

    async def resolve(self, ctx: SessionContext, resolution: Resolution, trigger: int) -> None:
        cause = await self.record_resolution(ctx, resolution)
        if cause is not None:
            await self.proceed(ctx, cause, trigger)

    @staticmethod
    def callback_command(data: str) -> tuple[Choice, int] | None:
        action, _, raw = data.partition(":")
        cause = parse_cause(raw)
        if cause is None:
            return None
        if action == "approve":
            return "approve", cause
        if action == "decline":
            return "decline", cause
        return None

    async def callback_session(self, update: CallbackUpdate) -> SessionRecord | None:
        command = self.callback_command(update.data)
        if command is None:
            return None
        ctx = await self._find_pending(command[1])
        return None if ctx is None else ctx.record

    async def on_callback(self, update: CallbackUpdate) -> None:
        command = self.callback_command(update.data)
        if command is None:
            await self._answerer.answer_callback(update.callback_id, "Неизвестная команда")
            return
        choice, cause = command
        ctx = await self._find_pending(cause)
        if ctx is None:
            await self._answerer.answer_callback(update.callback_id, "Решение уже не требуется")
            return
        result = await self.resolver.resolve(
            ctx.record.id, update.user_id, cause, choice, trigger=update.update_id
        )
        match result:
            case "recorded":
                text = "Одобрено" if choice == "approve" else "Отклонено"
            case "expired":
                text = "Время на решение истекло"
            case _:
                text = "Решение уже не требуется"
        await self._answerer.answer_callback(update.callback_id, text)

    async def on_job(self, job: Job) -> None:
        record = await self._steps.sessions.get(job.session)
        if record is None:
            await self._steps.scheduler.complete(job.key)
            return
        ctx = await self._steps.load(record)
        match job.kind:
            case "escalation_timeout":
                cause = job.payload.get("cause")
                pending = ctx.view.pending
                if isinstance(cause, int) and pending is not None and pending.cause == cause:
                    await self.resolve(ctx, "timeout", -cause)
                    await self._steps.notify_owner(
                        record, "Время на решение истекло — сделка отклонена."
                    )
            case "session_deadline":
                if job.payload.get("phase") == "execution":
                    await self._execution.on_deadline(ctx)
                elif ctx.view.state in (SessionState.NEGOTIATING, SessionState.PENDING_OWNER):
                    await self._steps.close(ctx, Outcome.expired, "session_deadline")
                    await self._steps.notify_owner(record, "Срок сессии истёк без договорённости.")
            case "commitment_reminder":
                await self._execution.on_reminder(ctx)
        await self._steps.scheduler.complete(job.key)
        await self._presenter.refresh(ctx)
