from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.app.invitations import Invitation, check_claim
from envoy.domain.ids import SessionId
from envoy.persistence.models import AgentAuthorizationRow, InvitationRow, SessionRouteRow

type Sessions = async_sessionmaker[AsyncSession]


def _invitation(row: InvitationRow) -> Invitation:
    return Invitation(
        row.token_hash,
        SessionId(row.session_id),
        row.counterpart_user_id,
        row.expires_at,
        row.used_at,
    )


class PgInvitationRepository:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def issue(self, invitation: Invitation) -> None:
        async with self._sessions.begin() as db:
            db.add(
                InvitationRow(
                    token_hash=invitation.token_hash,
                    session_id=invitation.sid,
                    counterpart_user_id=invitation.counterpart_user_id,
                    expires_at=invitation.expires_at,
                    used_at=invitation.used_at,
                )
            )

    async def get(self, hash: bytes) -> Invitation | None:
        async with self._sessions() as db:
            row = await db.get(InvitationRow, hash)
            return None if row is None else _invitation(row)

    async def claim(
        self, hash: bytes, now: datetime, route: str, expected_user: int | None
    ) -> Invitation:
        async with self._sessions.begin() as db:
            row = await db.get(InvitationRow, hash, with_for_update=True)
            invitation = check_claim(None if row is None else _invitation(row), now, expected_user)
            assert row is not None
            row.used_at = now
            statement = insert(SessionRouteRow).values(session_id=invitation.sid, route=route)
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[SessionRouteRow.session_id], set_={"route": route}
                )
            )
            await db.flush()
            return _invitation(row)

    async def route(self, sid: SessionId) -> str | None:
        async with self._sessions() as db:
            row = await db.get(SessionRouteRow, sid)
            return None if row is None else row.route

    async def switch(self, sid: SessionId, expected: str, route: str) -> bool:
        async with self._sessions.begin() as db:
            result = await db.execute(
                update(SessionRouteRow)
                .where(SessionRouteRow.session_id == sid, SessionRouteRow.route == expected)
                .values(route=route)
            )
            return bool(getattr(result, "rowcount", 0))

    async def authorize(self, user_id: int, bot_id: int) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                insert(AgentAuthorizationRow)
                .values(user_id=user_id, bot_id=bot_id)
                .on_conflict_do_nothing()
            )

    async def authorized(self, user_id: int, bot_id: int) -> bool:
        async with self._sessions() as db:
            return await db.get(AgentAuthorizationRow, (user_id, bot_id)) is not None
