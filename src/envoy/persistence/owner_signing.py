from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from envoy.app.owner_signing import (
    ActivateMandate,
    EnrollKey,
    Mutation,
    NoMutation,
    RevokeDelegations,
    SigningRejected,
    StoredChallenge,
    StoreDelegation,
    check_redeemable,
)
from envoy.domain.canonical import decode, encode
from envoy.domain.ids import OwnerId
from envoy.mandate.ports import MandateNotFound, MandateStateError
from envoy.mandate.signatures import Purpose
from envoy.persistence.models import OwnerDelegationRow, OwnerKeyRow, SigningChallengeRow
from envoy.persistence.state_stores import PgMandateStore, activate_signed_in
from envoy.protocol.delegation import Delegation, delegation_from_map, delegation_map

type Sessions = async_sessionmaker[AsyncSession]


def _challenge(row: SigningChallengeRow) -> StoredChallenge:
    return StoredChallenge(
        id=row.id,
        owner=OwnerId(row.owner_id),
        purpose=cast(Purpose, row.purpose),
        nonce=row.nonce,
        bytes_to_sign=row.bytes_to_sign,
        expires_at=row.expires_at,
        context=MappingProxyType(decode(row.context)),
        signature=row.signature,
        result=None if row.result is None else MappingProxyType(decode(row.result)),
    )


class PgOwnerSigningStore:
    def __init__(self, sessions: Sessions, mandates: PgMandateStore) -> None:
        self._sessions = sessions
        self._mandates = mandates

    async def owner_key(self, owner: OwnerId) -> bytes | None:
        async with self._sessions() as db:
            row = await db.get(OwnerKeyRow, owner)
            return None if row is None else row.public_key

    async def put_challenge(self, challenge: StoredChallenge) -> None:
        async with self._sessions.begin() as db:
            inserted = await db.scalar(
                insert(SigningChallengeRow)
                .values(
                    id=challenge.id,
                    owner_id=challenge.owner,
                    purpose=challenge.purpose,
                    nonce=challenge.nonce,
                    bytes_to_sign=challenge.bytes_to_sign,
                    context=encode(dict(challenge.context)),
                    expires_at=challenge.expires_at,
                )
                .on_conflict_do_nothing()
                .returning(SigningChallengeRow.id)
            )
            if inserted is None:
                raise SigningRejected("duplicate_challenge")

    async def get_challenge(self, id: str) -> StoredChallenge | None:
        async with self._sessions() as db:
            row = await db.get(SigningChallengeRow, id)
            return None if row is None else _challenge(row)

    async def _apply(self, db: AsyncSession, mutation: Mutation) -> None:
        match mutation:
            case EnrollKey(owner=owner, public_key=key):
                await db.execute(
                    insert(OwnerKeyRow)
                    .values(owner_id=owner, public_key=key)
                    .on_conflict_do_nothing()
                )
                stored = await db.get(OwnerKeyRow, owner, with_for_update=True)
                if stored is None or stored.public_key != key:
                    raise SigningRejected("already_enrolled")
            case ActivateMandate():
                try:
                    await activate_signed_in(
                        db,
                        self._mandates.record_of,
                        mutation.owner,
                        mutation.mandate_id,
                        mutation.expected_version,
                        mutation.signature,
                    )
                except (MandateStateError, MandateNotFound) as error:
                    raise SigningRejected(str(error)) from None
            case StoreDelegation(delegation=delegation):
                await db.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(str(delegation.owner_id))))
                )
                position = await db.scalar(
                    select(func.count())
                    .select_from(OwnerDelegationRow)
                    .where(OwnerDelegationRow.owner_id == delegation.owner_id)
                )
                db.add(
                    OwnerDelegationRow(
                        owner_id=delegation.owner_id,
                        position=int(position or 0),
                        agent_key=delegation.agent_key,
                        body=encode(delegation_map(delegation)),
                        revoked=False,
                    )
                )
            case RevokeDelegations(owner=owner, agent_key=key):
                await db.execute(
                    update(OwnerDelegationRow)
                    .where(
                        OwnerDelegationRow.owner_id == owner, OwnerDelegationRow.agent_key == key
                    )
                    .values(revoked=True)
                )
            case NoMutation():
                pass

    async def redeem(
        self,
        id: str,
        signature: bytes,
        mutation: Mutation,
        result: Mapping[str, Any],
        now: datetime,
    ) -> StoredChallenge:
        async with self._sessions.begin() as db:
            row = await db.get(SigningChallengeRow, id, with_for_update=True)
            challenge = None if row is None else _challenge(row)
            if not check_redeemable(challenge, signature, now):
                assert challenge is not None
                return challenge
            assert row is not None
            await self._apply(db, mutation)
            row.signature = signature
            row.result = encode(dict(result))
            await db.flush()
            return _challenge(row)

    async def current(self, owner: OwnerId) -> Delegation | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(OwnerDelegationRow)
                .where(OwnerDelegationRow.owner_id == owner, OwnerDelegationRow.revoked.is_(False))
                .order_by(OwnerDelegationRow.position.desc())
                .limit(1)
            )
            return None if row is None else delegation_from_map(decode(row.body))
