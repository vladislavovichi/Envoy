from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from envoy.domain.canonical import decode, encode
from envoy.domain.ids import MandateId, OfferHash, OwnerId, SessionId
from envoy.domain.offer import Offer
from envoy.protocol.delegation import Delegation
from envoy.protocol.machine import NegotiationState, OfferRef, Phase

type PendingKind = Literal["accept_offer", "confirm_accept"]


class StaleSession(Exception):
    pass


@dataclass(frozen=True)
class OutboxItem:
    seq: int
    type: str
    text: str
    deadline: datetime
    next_at: datetime
    attempts: int = 0
    recovery: bool = False


@dataclass(frozen=True)
class PendingOwner:
    cause: int
    kind: PendingKind
    hash: OfferHash


@dataclass(frozen=True)
class AgentSession:
    id: SessionId
    bot_id: int
    peer_bot_id: int
    owner_id: OwnerId
    owner_chat_id: int
    mandate_id: MandateId
    mandate_version: int
    state: NegotiationState
    deadline: datetime
    created_at: datetime
    peer_agent_key: bytes = b""
    peer_delegation_expires: datetime | None = None
    my_seq: int = 0
    peer_seq: int = 0
    peer_ack: int = 0
    my_recovery: int = 0
    peer_recovery: int = 0
    offers: Mapping[OfferHash, Offer] = field(default_factory=lambda: MappingProxyType({}))
    peer_history: tuple[tuple[OfferHash, float], ...] = ()
    timers: Mapping[str, datetime] = field(default_factory=lambda: MappingProxyType({}))
    outbox: tuple[OutboxItem, ...] = ()
    capabilities: tuple[str, ...] = ()
    pending: PendingOwner | None = None
    agreed_at: datetime | None = None
    closed: bool = False
    version: int = 0
    human_connection: str | None = None
    human_chat: int | None = None

    @property
    def phase(self) -> Phase:
        return self.state.phase

    def with_offer(self, offer: Offer) -> AgentSession:
        return replace(self, offers=MappingProxyType({**self.offers, offer.hash(): offer}))

    def next_due(self) -> datetime | None:
        moments = list(self.timers.values())
        moments += [item.next_at for item in self.outbox if item.next_at < item.deadline]
        return min(moments) if moments else None


@dataclass(frozen=True)
class Inbound:
    bot_id: int
    update_id: int
    sender: int
    nonce: bytes | None = None
    nonce_expires: datetime | None = None
    text: str | None = None


class ProtocolRepository(Protocol):
    async def get(self, id: SessionId) -> AgentSession | None: ...
    async def active_with(self, bot_id: int, peer_bot_id: int) -> list[AgentSession]: ...
    async def seen(self, bot_id: int, update_id: int) -> bool: ...
    async def nonce_seen(self, sender: int, nonce: bytes, now: datetime) -> bool: ...
    async def mark(self, inbound: Inbound) -> None: ...
    async def save(
        self, session: AgentSession, *, inbound: Inbound | None = None
    ) -> AgentSession: ...
    async def due(self, now: datetime) -> list[AgentSession]: ...
    async def count_agreed_since(self, owner: OwnerId, since: datetime) -> int: ...


class DelegationSource(Protocol):
    async def current(self, owner: OwnerId) -> Delegation | None: ...


class InMemoryDelegations:
    def __init__(self) -> None:
        self._delegations: dict[OwnerId, Delegation] = {}

    def put(self, delegation: Delegation) -> None:
        self._delegations[delegation.owner_id] = delegation

    async def current(self, owner: OwnerId) -> Delegation | None:
        return self._delegations.get(owner)


def _ref(ref: OfferRef | None) -> list[Any] | None:
    return None if ref is None else [ref.hash, ref.round]


def _unref(raw: Any) -> OfferRef | None:
    return None if raw is None else OfferRef(OfferHash(raw[0]), int(raw[1]))


def state_map(state: NegotiationState) -> dict[str, Any]:
    return {
        "role": state.role,
        "me": state.me,
        "peer": state.peer,
        "phase": state.phase.value,
        "round": state.round,
        "last_sent": _ref(state.last_sent),
        "last_received": _ref(state.last_received),
        "answered": sorted(state.answered),
        "agreed": state.agreed,
        "my_ready": state.my_ready,
        "peer_ready": state.peer_ready,
        "decision": state.decision,
        "revoke_requested": state.revoke_requested,
        "escalated": state.escalated,
        "decision_requests": state.decision_requests,
        "checklist_opened": state.checklist_opened,
        "outcome": state.outcome,
    }


def state_from_map(raw: Mapping[str, Any]) -> NegotiationState:
    return NegotiationState(
        role=raw["role"],
        me=raw["me"],
        peer=raw["peer"],
        phase=Phase(raw["phase"]),
        round=raw["round"],
        last_sent=_unref(raw["last_sent"]),
        last_received=_unref(raw["last_received"]),
        answered=frozenset(OfferHash(item) for item in raw["answered"]),
        agreed=None if raw["agreed"] is None else OfferHash(raw["agreed"]),
        my_ready=raw["my_ready"],
        peer_ready=raw["peer_ready"],
        decision=raw["decision"],
        revoke_requested=raw["revoke_requested"],
        escalated=raw["escalated"],
        decision_requests=raw["decision_requests"],
        checklist_opened=raw["checklist_opened"],
        outcome=raw["outcome"],
    )


def encode_session(session: AgentSession) -> bytes:
    return encode(
        {
            "id": session.id,
            "bot_id": session.bot_id,
            "peer_bot_id": session.peer_bot_id,
            "owner_id": session.owner_id,
            "owner_chat_id": session.owner_chat_id,
            "mandate_id": session.mandate_id,
            "mandate_version": session.mandate_version,
            "state": state_map(session.state),
            "deadline": session.deadline,
            "created_at": session.created_at,
            "peer_agent_key": session.peer_agent_key,
            "peer_delegation_expires": session.peer_delegation_expires,
            "my_seq": session.my_seq,
            "peer_seq": session.peer_seq,
            "peer_ack": session.peer_ack,
            "my_recovery": session.my_recovery,
            "peer_recovery": session.peer_recovery,
            "offers": [[hash, dict(offer.values)] for hash, offer in session.offers.items()],
            "peer_history": [[hash, utility] for hash, utility in session.peer_history],
            "timers": dict(session.timers),
            "outbox": [
                [
                    item.seq,
                    item.type,
                    item.text,
                    item.deadline,
                    item.next_at,
                    item.attempts,
                    item.recovery,
                ]
                for item in session.outbox
            ],
            "capabilities": list(session.capabilities),
            "pending": (
                None
                if session.pending is None
                else [session.pending.cause, session.pending.kind, session.pending.hash]
            ),
            "agreed_at": session.agreed_at,
            "closed": session.closed,
            "version": session.version,
            "human_connection": session.human_connection,
            "human_chat": session.human_chat,
        }
    )


def decode_session(data: bytes) -> AgentSession:
    raw = decode(data)
    pending = raw["pending"]
    return AgentSession(
        id=SessionId(cast(UUID, raw["id"])),
        bot_id=raw["bot_id"],
        peer_bot_id=raw["peer_bot_id"],
        owner_id=OwnerId(raw["owner_id"]),
        owner_chat_id=raw["owner_chat_id"],
        mandate_id=MandateId(raw["mandate_id"]),
        mandate_version=raw["mandate_version"],
        state=state_from_map(raw["state"]),
        deadline=raw["deadline"],
        created_at=raw["created_at"],
        peer_agent_key=raw["peer_agent_key"],
        peer_delegation_expires=raw["peer_delegation_expires"],
        my_seq=raw["my_seq"],
        peer_seq=raw["peer_seq"],
        peer_ack=raw["peer_ack"],
        my_recovery=raw["my_recovery"],
        peer_recovery=raw["peer_recovery"],
        offers=MappingProxyType(
            {OfferHash(hash): Offer(values=values) for hash, values in raw["offers"]}
        ),
        peer_history=tuple(
            (OfferHash(hash), float(utility)) for hash, utility in raw["peer_history"]
        ),
        timers=MappingProxyType(dict(raw["timers"])),
        outbox=tuple(OutboxItem(*item) for item in raw["outbox"]),
        capabilities=tuple(raw["capabilities"]),
        pending=(
            None if pending is None else PendingOwner(pending[0], pending[1], OfferHash(pending[2]))
        ),
        agreed_at=raw["agreed_at"],
        closed=raw["closed"],
        version=raw["version"],
        human_connection=raw.get("human_connection"),
        human_chat=raw.get("human_chat"),
    )


class InMemoryProtocolRepository:
    def __init__(self) -> None:
        self._sessions: dict[SessionId, bytes] = {}
        self._updates: set[tuple[int, int]] = set()
        self._nonces: dict[tuple[int, bytes], datetime] = {}
        self.evidence: list[tuple[SessionId, int, str]] = []

    async def get(self, id: SessionId) -> AgentSession | None:
        data = self._sessions.get(id)
        return None if data is None else decode_session(data)

    async def active_with(self, bot_id: int, peer_bot_id: int) -> list[AgentSession]:
        sessions = [decode_session(data) for data in self._sessions.values()]
        return [
            session
            for session in sessions
            if (session.bot_id, session.peer_bot_id) == (bot_id, peer_bot_id) and not session.closed
        ]

    async def seen(self, bot_id: int, update_id: int) -> bool:
        return (bot_id, update_id) in self._updates

    async def nonce_seen(self, sender: int, nonce: bytes, now: datetime) -> bool:
        expires = self._nonces.get((sender, nonce))
        return expires is not None and expires > now

    def _apply_inbound(self, inbound: Inbound, session: SessionId | None) -> None:
        self._updates.add((inbound.bot_id, inbound.update_id))
        if inbound.nonce is not None and inbound.nonce_expires is not None:
            self._nonces[(inbound.sender, inbound.nonce)] = inbound.nonce_expires
        if inbound.text is not None and session is not None:
            self.evidence.append((session, inbound.update_id, inbound.text))

    async def mark(self, inbound: Inbound) -> None:
        self._apply_inbound(inbound, None)

    async def save(self, session: AgentSession, *, inbound: Inbound | None = None) -> AgentSession:
        current = self._sessions.get(session.id)
        stored_version = 0 if current is None else decode_session(current).version
        if stored_version != session.version:
            raise StaleSession(str(session.id))
        saved = replace(session, version=session.version + 1)
        self._sessions[session.id] = encode_session(saved)
        if inbound is not None:
            self._apply_inbound(inbound, session.id)
        return saved

    async def due(self, now: datetime) -> list[AgentSession]:
        sessions = [decode_session(data) for data in self._sessions.values()]
        return [
            session
            for session in sessions
            if (moment := session.next_due()) is not None and moment <= now
        ]

    async def count_agreed_since(self, owner: OwnerId, since: datetime) -> int:
        sessions = [decode_session(data) for data in self._sessions.values()]
        return sum(
            1
            for session in sessions
            if session.owner_id == owner
            and session.agreed_at is not None
            and session.agreed_at >= since
        )
