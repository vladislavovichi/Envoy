from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid7

from envoy.app.agent_flow import AgentFlow
from envoy.app.agent_ports import InMemoryDelegations, InMemoryProtocolRepository
from envoy.app.pipeline import default_authorizer, default_engine
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, OwnerId, SessionId, owner_for_user
from envoy.domain.offer import Offer
from envoy.domain.session import fold
from envoy.domain.telegram import BotMessageUpdate, ReplyMarkup, SentMessage
from envoy.journal.memory import InMemoryJournalStore
from envoy.mandate.commitment import commitment_digest
from envoy.mandate.ports import InMemoryMandateStore
from envoy.mandate.schema import Mandate
from envoy.ports.clock import FixedClock
from envoy.ports.ids import SequentialIdGenerator
from envoy.ports.rng import SeededRng
from envoy.protocol.bodies import offer_body, version_map
from envoy.protocol.delegation import (
    Delegation,
    InMemoryKeyRegistry,
    delegation_map,
    sign_delegation,
)
from envoy.protocol.envelope import (
    WIRE_VERSION,
    Envelope,
    decode_message,
    encode_message,
)
from tests.support.journal import build_journal
from tests.support.mandates import SLOTS, meeting_data
from tests.support.sessions import T0

AGENT_DEFAULTS: dict[str, Any] = {
    "limits": {"max_rounds": 16},
    "strategy": {"kind": "time_dependent", "params": {"beta": 2.0}},
}


def mirrored_mandate(**overrides: Any) -> Mandate:
    data = meeting_data(**{**AGENT_DEFAULTS, **overrides})
    first, last = SLOTS[0].timestamp(), SLOTS[-1].timestamp()
    data["utility"]["value_functions"] = {
        "time": {"points": [[first, 0.0], [last, 1.0]]},
        "place": {"points": [[0, 0.2], [3, 1.0]]},
    }
    data["hard_constraints"] = []
    return Mandate.model_validate(data)


def primary_mandate(**overrides: Any) -> Mandate:
    data = meeting_data(**{**AGENT_DEFAULTS, **overrides})
    data["hard_constraints"] = []
    return Mandate.model_validate(data)


class SimulatedCrash(BaseException):
    pass


@dataclass
class Router:
    clock: FixedClock
    operators: dict[int, Operator] = field(default_factory=dict)
    queue: list[BotMessageUpdate] = field(default_factory=list)
    counters: dict[int, int] = field(default_factory=dict)
    owner_messages: dict[int, list[tuple[str, ReplyMarkup | None]]] = field(default_factory=dict)
    drops: list[Any] = field(default_factory=list)
    log: list[tuple[int, int, str]] = field(default_factory=list)

    def sender(self, bot_id: int) -> RoutedSender:
        return RoutedSender(self, bot_id)

    def route(self, from_bot: int, chat_id: int, text: str, markup: ReplyMarkup | None) -> None:
        if chat_id not in self.operators and chat_id not in self.counters:
            self.owner_messages.setdefault(chat_id, []).append((text, markup))
            return
        self.log.append((from_bot, chat_id, text))
        for predicate in list(self.drops):
            if predicate(from_bot, chat_id, text):
                self.drops.remove(predicate)
                return
        self.counters[chat_id] = self.counters.get(chat_id, 0) + 1
        self.queue.append(
            BotMessageUpdate(
                bot_id=chat_id,
                update_id=self.counters[chat_id],
                from_bot_id=from_bot,
                chat_id=from_bot,
                text=text,
                received_at=self.clock.now(),
            )
        )

    async def pump(self, limit: int = 500) -> None:
        for _ in range(limit):
            if not self.queue:
                return
            update = self.queue.pop(0)
            operator = self.operators.get(update.bot_id)
            if operator is not None:
                await operator.flow.receive(update)
        raise AssertionError("message loop did not settle")

    async def tick(self, seconds: float = 6) -> None:
        self.clock.advance(timedelta(seconds=seconds))
        for operator in self.operators.values():
            await operator.flow.run_due(self.clock.now())
        await self.pump()

    async def busy(self) -> bool:
        for operator in self.operators.values():
            for session in await operator.sessions():
                if session.outbox:
                    return True
        return bool(self.queue)

    async def settle(self, ticks: int = 120, seconds: float = 6) -> None:
        await self.pump()
        for _ in range(ticks):
            if not await self.busy():
                return
            await self.tick(seconds)
        raise AssertionError("federation did not settle")

    def drop_next(self, predicate) -> None:
        self.drops.append(predicate)


@dataclass
class RoutedSender:
    router: Router
    bot_id: int
    crash_when: Any = None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        business_connection_id: str | None = None,
        thread_id: int | None = None,
    ) -> SentMessage:
        if self.crash_when is not None and self.crash_when(chat_id, text):
            self.crash_when = None
            raise SimulatedCrash
        self.router.route(self.bot_id, chat_id, text, reply_markup)
        return SentMessage(1, chat_id, text)


@dataclass
class Operator:
    bot_id: int
    owner_user: int
    flow: AgentFlow
    repo: InMemoryProtocolRepository
    journal_store: InMemoryJournalStore
    journal: Any
    agent: SigningKey
    owner_key: SigningKey
    sender: RoutedSender
    registry: InMemoryKeyRegistry
    delegations: InMemoryDelegations

    @property
    def owner_id(self) -> OwnerId:
        return owner_for_user(self.owner_user)

    async def sessions(self):
        return [
            await self.repo.get(sid)
            for sid in {SessionId(item) for item in _session_ids(self.repo)}
        ]

    async def only(self):
        sessions = await self.sessions()
        assert len(sessions) == 1, sessions
        return sessions[0]

    async def view(self, sid: SessionId):
        return fold(await self.journal.events(sid))


def _session_ids(repo: InMemoryProtocolRepository) -> list[UUID]:
    return list(repo._sessions)


async def build_operator(
    router: Router, bot_id: int, owner_user: int, mandate: Mandate, seed: int, **flow_options
) -> Operator:
    clock = router.clock
    owner_key = SigningKey.from_bytes(bytes([seed]) * 32)
    agent = SigningKey.from_bytes(bytes([seed + 50]) * 32)
    journal, store, _ = build_journal(clock, agent)
    mandates = InMemoryMandateStore(SequentialIdGenerator())
    owner = owner_for_user(owner_user)
    draft = await mandates.save_draft(owner, mandate, bytes([seed]) * 32)
    stored = await mandates.activate(draft.id, b"s" * 64)
    delegations = InMemoryDelegations()
    delegations.put(
        sign_delegation(
            Delegation(
                owner_id=owner,
                owner_key=owner_key.public().to_bytes(),
                agent_id=AgentId(UUID(int=10_000 + seed)),
                bot_id=bot_id,
                agent_key=agent.public().to_bytes(),
                mandate_id=stored.id,
                version=stored.version,
                commitment=commitment_digest(stored.mandate, stored.salt),
                expires_at=T0 + timedelta(days=30),
            ),
            owner_key,
        )
    )
    repo = InMemoryProtocolRepository()
    registry = InMemoryKeyRegistry()
    sender = router.sender(bot_id)
    flow = AgentFlow(
        repo,
        registry,
        delegations,
        mandates,
        journal,
        sender,
        default_authorizer(),
        default_engine,
        clock,
        SeededRng(seed),
        agent,
        bot_id,
        owner_user,
        **flow_options,
    )
    operator = Operator(
        bot_id,
        owner_user,
        flow,
        repo,
        store,
        journal,
        agent,
        owner_key,
        sender,
        registry,
        delegations,
    )
    router.operators[bot_id] = operator
    return operator


def restart(router: Router, operator: Operator, **flow_options) -> Operator:
    flow = operator.flow
    fresh = AgentFlow(
        operator.repo,
        operator.registry,
        operator.delegations,
        flow._mandates,
        operator.journal,
        operator.sender,
        default_authorizer(),
        default_engine,
        router.clock,
        SeededRng(operator.bot_id),
        operator.agent,
        operator.bot_id,
        operator.owner_user,
        **flow_options,
    )
    restarted = Operator(
        operator.bot_id,
        operator.owner_user,
        fresh,
        operator.repo,
        operator.journal_store,
        operator.journal,
        operator.agent,
        operator.owner_key,
        operator.sender,
        operator.registry,
        operator.delegations,
    )
    router.operators[operator.bot_id] = restarted
    return restarted


async def two_operators(a_mandate=None, b_mandate=None, **flow_options):
    router = Router(FixedClock(T0))
    a = await build_operator(router, 501, 1, a_mandate or primary_mandate(), 11, **flow_options)
    b = await build_operator(router, 502, 2, b_mandate or mirrored_mandate(), 22, **flow_options)
    return router, a, b


def message_type(text: str) -> str:
    return decode_message(text).envelope.type


@dataclass
class ManualPeer:
    router: Router
    bot_id: int = 777
    seed: int = 77
    seq: int = 0
    sid: SessionId = field(default_factory=lambda: SessionId(uuid7()))
    nonce_counter: int = 0

    def __post_init__(self) -> None:
        self.owner = SigningKey.from_bytes(bytes([self.seed]) * 32)
        self.agent = SigningKey.from_bytes(bytes([self.seed + 50]) * 32)
        self.router.counters.setdefault(self.bot_id, 0)

    def delegation(self) -> Delegation:
        return sign_delegation(
            Delegation(
                owner_id=OwnerId(UUID(int=self.seed)),
                owner_key=self.owner.public().to_bytes(),
                agent_id=AgentId(UUID(int=20_000 + self.seed)),
                bot_id=self.bot_id,
                agent_key=self.agent.public().to_bytes(),
                mandate_id=SessionId(UUID(int=1)),
                version=1,
                commitment=bytes(32),
                expires_at=T0 + timedelta(days=30),
            ),
            self.owner,
        )

    def text(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        seq: int | None = None,
        ack: int = 0,
        chain=None,
        ts_offset: int = 0,
        ttl: int = 600,
        sid: SessionId | None = None,
    ) -> str:
        if seq is None:
            self.seq += 1
            seq = self.seq
        self.nonce_counter += 1
        ts = int(self.router.clock.now().timestamp()) + ts_offset
        return encode_message(
            Envelope(
                v=WIRE_VERSION,
                sid=sid or self.sid,
                seq=seq,
                ack=ack,
                type=kind,
                body=body,
                ts=ts,
                exp=ts + ttl,
                nonce=self.nonce_counter.to_bytes(16, "big"),
                chain=chain,
            ),
            self.agent,
        )

    def hello(self, sid: SessionId | None = None, token: str | None = None) -> str:
        delegation = self.delegation()
        return self.text(
            "hello",
            {
                "agent_id": delegation.agent_id.bytes,
                "owner_id": delegation.owner_id.bytes,
                "min_version": version_map(WIRE_VERSION),
                "max_version": version_map(WIRE_VERSION),
                "token": token,
            },
            chain=delegation_map(delegation),
            seq=1 if sid is not None else None,
            sid=sid,
        )

    def capabilities(self, **kwargs) -> str:
        return self.text("capabilities", {"items": ["issue:datetime@1", "issue:place@1"]}, **kwargs)

    def propose(self, offer: Offer, round: int, previous=None, **kwargs) -> str:
        return self.text(
            "counter" if previous else "propose", offer_body(offer, round, previous), **kwargs
        )

    def send(self, to_bot: int, text: str) -> None:
        self.router.route(self.bot_id, to_bot, text, None)

    def inbox(self) -> list[str]:
        return [text for sender, chat, text in self.router.log if chat == self.bot_id]
