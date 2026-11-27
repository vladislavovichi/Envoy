from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from types import MappingProxyType
from uuid import UUID, uuid7

import pytest

from envoy.app.agent_ports import (
    AgentSession,
    Inbound,
    InMemoryProtocolRepository,
    OutboxItem,
    PendingOwner,
    StaleSession,
)
from envoy.crypto.aead import ContentCipher
from envoy.domain.ids import MandateId, OfferHash, OwnerId, SessionId
from envoy.domain.offer import Offer
from envoy.persistence.protocol_repo import PgProtocolRepository
from envoy.ports.rng import SystemRng
from envoy.protocol.machine import NegotiationState, OfferRef, Phase
from tests.support.sessions import T0

OWNER = OwnerId(UUID(int=1))
OFFER = Offer(values={"time": T0, "place": "кафе"})


@pytest.fixture(params=["memory", "postgres"])
def repo(request):
    if request.param == "memory":
        return InMemoryProtocolRepository()
    cipher = ContentCipher(SystemRng())
    return PgProtocolRepository(
        request.getfixturevalue("pg_session_factory"), cipher, cipher.generate_key()
    )


def session(**changes) -> AgentSession:
    hash = OFFER.hash()
    values = {
        "id": SessionId(uuid7()),
        "bot_id": 501,
        "peer_bot_id": 502,
        "owner_id": OWNER,
        "owner_chat_id": 1,
        "mandate_id": MandateId(UUID(int=7)),
        "mandate_version": 2,
        "state": NegotiationState(
            role="coordinator",
            me=b"a",
            peer=b"b",
            phase=Phase.COMMITTING,
            round=3,
            last_sent=OfferRef(hash, 3),
            answered=frozenset({OfferHash(sha256(b"x").digest())}),
            agreed=hash,
            my_ready=True,
        ),
        "deadline": T0 + timedelta(days=2),
        "created_at": T0,
        "peer_agent_key": bytes(32),
        "peer_delegation_expires": T0 + timedelta(days=30),
        "my_seq": 5,
        "peer_seq": 4,
        "offers": MappingProxyType({hash: OFFER}),
        "peer_history": ((hash, 0.75),),
        "timers": MappingProxyType({"ready": T0 + timedelta(minutes=1)}),
        "outbox": (
            OutboxItem(
                5, "commit", "envoy/1:x", T0 + timedelta(minutes=10), T0 + timedelta(seconds=5)
            ),
        ),
        "capabilities": ("issue:datetime@1",),
        "pending": PendingOwner(4, "confirm_accept", hash),
    }
    values.update(changes)
    return AgentSession(**values)


async def test_round_trip_and_optimistic_versions(repo) -> None:
    original = session()
    saved = await repo.save(original)
    assert saved.version == 1
    loaded = await repo.get(original.id)
    assert loaded == saved
    with pytest.raises(StaleSession):
        await repo.save(original)
    second = await repo.save(replace(loaded, my_seq=6))
    assert (second.version, (await repo.get(original.id)).my_seq) == (2, 6)
    assert await repo.get(SessionId(uuid7())) is None


async def test_inbound_marks_update_nonce_and_evidence_atomically(repo) -> None:
    saved = await repo.save(session())
    inbound = Inbound(501, 1, 502, bytes(16), T0 + timedelta(minutes=10), "envoy/1:evidence")
    await repo.save(saved, inbound=inbound)
    assert await repo.seen(501, 1)
    assert not await repo.seen(502, 1)
    assert await repo.nonce_seen(502, bytes(16), T0)
    assert not await repo.nonce_seen(502, bytes(16), T0 + timedelta(minutes=11))
    assert not await repo.nonce_seen(503, bytes(16), T0)
    with pytest.raises(StaleSession):
        await repo.save(saved, inbound=Inbound(501, 2, 502, bytes([1]) * 16, T0, "x"))
    assert not await repo.seen(501, 2)
    await repo.mark(Inbound(501, 3, 502))
    assert await repo.seen(501, 3)
    if isinstance(repo, PgProtocolRepository):
        assert await repo.evidence(saved.id) == ["envoy/1:evidence"]


async def test_due_active_and_agreed_queries(repo) -> None:
    due = await repo.save(session())
    idle = await repo.save(session(timers=MappingProxyType({}), outbox=()))
    closed = await repo.save(
        session(closed=True, agreed_at=T0, timers=MappingProxyType({}), outbox=())
    )
    assert {item.id for item in await repo.due(T0 + timedelta(seconds=6))} == {due.id}
    assert await repo.due(T0) == []
    assert {item.id for item in await repo.active_with(501, 502)} == {due.id, idle.id}
    assert await repo.active_with(502, 501) == []
    assert closed.id not in {item.id for item in await repo.active_with(501, 502)}
    assert await repo.count_agreed_since(OWNER, T0 - timedelta(hours=1)) == 1
    assert await repo.count_agreed_since(OWNER, T0 + timedelta(hours=1)) == 0
    expired = session(
        timers=MappingProxyType({}),
        outbox=(OutboxItem(1, "ping", "t", T0, T0 + timedelta(seconds=5)),),
    )
    await repo.save(expired)
    assert expired.id not in {item.id for item in await repo.due(T0 + timedelta(days=1))}
