from datetime import timedelta
from zoneinfo import ZoneInfo

import structlog

from envoy.app.ports import Job, SessionRecord
from envoy.app.renderer import MessageRenderer
from envoy.app.steps import SessionContext, SessionSteps
from envoy.domain.events import ChecklistOpened, CommitmentDone, Escalated, Outcome, PartyRole
from envoy.domain.session import SessionState
from envoy.domain.telegram import BusinessMessageUpdate
from envoy.execution.checklist import Commitment, is_complete, meeting_time, plan_commitments
from envoy.security.authorizer import Action, Allow, Deny
from envoy.telegram.ports import ChecklistSender, SendRejected

logger = structlog.get_logger()
REMINDER_LEAD = timedelta(hours=1)
EXECUTION_GRACE = timedelta(hours=24)


class ExecutionFlow:
    def __init__(
        self,
        steps: SessionSteps,
        checklists: ChecklistSender,
        renderer: MessageRenderer,
        timezone: str,
    ) -> None:
        self._steps = steps
        self._checklists = checklists
        self._renderer = renderer
        self._zone = ZoneInfo(timezone)

    def plan(self, ctx: SessionContext) -> tuple[Commitment, ...]:
        offer = ctx.view.agreed
        if offer is None:
            return ()
        time = meeting_time(offer)
        local = (
            offer
            if time is None
            else offer.model_copy(
                update={"values": {**offer.values, "time": time.astimezone(self._zone)}}
            )
        )
        return plan_commitments(local, "meeting")

    async def open(self, ctx: SessionContext, cause: int) -> None:
        if ctx.view.state is not SessionState.AGREED or ctx.view.agreed is None:
            return
        verdict = await self._steps.authorize(ctx, Action("open_checklist"))
        if isinstance(verdict, Deny):
            await self._steps.notify_owner(ctx.record, self._renderer.owner_notice(verdict.reason))
            return
        if not isinstance(verdict, Allow):
            await self._park(ctx, cause)
            return
        plan = self.plan(ctx)
        record = ctx.record
        try:
            await self._checklists.send_checklist(
                record.counterpart_chat_id,
                record.connection_id,
                "Встреча",
                [commitment.text for commitment in plan],
            )
        except SendRejected:
            await self._park(ctx, cause)
            return
        await self._steps.append(
            ctx, ChecklistOpened(items=tuple(commitment.text for commitment in plan))
        )
        await self.schedule(ctx)
        await self._steps.notify_owner(record, self._renderer.deal_summary(ctx.view.agreed))

    async def _park(self, ctx: SessionContext, cause: int) -> None:
        if ctx.find(Escalated, cause) is None:
            await self._steps.append(ctx, Escalated(cause=cause, reason="reply_window_closed"))
            await self._steps.notify_owner(
                ctx.record, self._renderer.owner_notice("reply_window_closed")
            )

    async def schedule(self, ctx: SessionContext) -> None:
        offer = ctx.view.agreed
        time = None if offer is None else meeting_time(offer)
        now = self._steps.clock.now()
        anchor = time if time is not None else now
        record = ctx.record
        await self._steps.scheduler.schedule(
            Job(
                f"{record.id}:reminder",
                "commitment_reminder",
                record.id,
                max(anchor - REMINDER_LEAD, now),
            )
        )
        await self._steps.scheduler.schedule(
            Job(
                f"{record.id}:execution_deadline",
                "session_deadline",
                record.id,
                anchor + EXECUTION_GRACE,
                {"phase": "execution"},
            )
        )

    async def on_tasks_done(self, record: SessionRecord, update: BusinessMessageUpdate) -> None:
        ctx = await self._steps.load(record)
        if ctx.view.state is not SessionState.EXECUTING:
            return
        party: PartyRole = "owner" if update.user_id == record.owner_chat_id else "counterpart"
        size = len(ctx.view.checklist)
        for task_id in update.done_task_ids:
            index = task_id - 1
            if 0 <= index < size and (party, index) not in ctx.view.commitments_done:
                await self._steps.append(ctx, CommitmentDone(party=party, index=index))
        if is_complete(ctx.view, self.plan(ctx)):
            await self._steps.close(ctx, Outcome.fulfilled, "all_commitments_done")
            await self._steps.notify_owner(record, "Встреча состоялась: все пункты отмечены.")

    async def on_deadline(self, ctx: SessionContext) -> None:
        if ctx.view.state in (SessionState.AGREED, SessionState.EXECUTING):
            await self._steps.close(ctx, Outcome.expired, "execution_deadline")
            await self._steps.notify_owner(
                ctx.record, "Срок исполнения истёк: не все пункты чек-листа отмечены."
            )

    async def on_reminder(self, ctx: SessionContext) -> None:
        if ctx.view.state is not SessionState.EXECUTING or ctx.view.agreed is None:
            return
        text = f"Напоминание: {self._renderer.deal_summary(ctx.view.agreed)}"
        await self._steps.notify_owner(ctx.record, text)
        if await self._steps.window_open(ctx.record):
            try:
                await self._steps.sender.send_message(
                    ctx.record.counterpart_chat_id,
                    f"Напоминаю о встрече. {self._renderer.accept(ctx.view.agreed)}",
                    business_connection_id=ctx.record.connection_id,
                )
            except SendRejected:
                logger.warning("reminder_not_delivered", sid=str(ctx.record.id))
