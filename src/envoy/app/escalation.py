import re
from typing import Literal, Protocol

from envoy.app.ports import SessionRecord
from envoy.app.steps import SessionContext, SessionSteps
from envoy.domain.events import Resolution
from envoy.domain.ids import SessionId, owner_for_user

type Choice = Literal["approve", "decline"]
type ResolveResult = Literal["recorded", "stale", "expired", "forbidden"]

CANONICAL_CAUSE = re.compile(r"0|[1-9][0-9]*")


def parse_cause(raw: str) -> int | None:
    return int(raw) if CANONICAL_CAUSE.fullmatch(raw) else None


class ResolutionFlow(Protocol):
    async def record_resolution(
        self, ctx: SessionContext, resolution: Resolution
    ) -> int | None: ...
    async def proceed(self, ctx: SessionContext, cause: int, trigger: int) -> None: ...
    async def present(self, record: SessionRecord) -> None: ...


class EscalationResolver:
    def __init__(self, steps: SessionSteps, flow: ResolutionFlow) -> None:
        self._steps = steps
        self._flow = flow

    async def record(
        self, session: SessionId, user_id: int, cause: int, choice: Choice
    ) -> tuple[ResolveResult, SessionContext | None]:
        record = await self._steps.sessions.get(session)
        if record is None:
            return "stale", None
        if record.owner_id != owner_for_user(user_id):
            return "forbidden", None
        ctx = await self._steps.load(record)
        pending = ctx.view.pending
        if pending is None or pending.cause != cause:
            return "stale", None
        if pending.expires_at is not None and self._steps.clock.now() >= pending.expires_at:
            return "expired", None
        resolution: Resolution = "approved" if choice == "approve" else "declined"
        await self._flow.record_resolution(ctx, resolution)
        return "recorded", ctx

    async def proceed(self, ctx: SessionContext, cause: int, trigger: int) -> None:
        await self._flow.proceed(ctx, cause, trigger)
        await self._flow.present(ctx.record)

    async def resolve(
        self, session: SessionId, user_id: int, cause: int, choice: Choice, *, trigger: int
    ) -> ResolveResult:
        result, ctx = await self.record(session, user_id, cause, choice)
        if ctx is not None:
            await self.proceed(ctx, cause, trigger)
        return result
