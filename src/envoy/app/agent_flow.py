import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid7

import structlog

from envoy.app.agent_ports import (
    AgentSession,
    DelegationSource,
    Inbound,
    OutboxItem,
    PendingOwner,
    ProtocolRepository,
    StaleSession,
)
from envoy.app.invitations import (
    HUMAN,
    InvitationFlow,
    InvitationRejected,
    VerifiedPeer,
    agent_route,
    sid_of,
)
from envoy.app.journal import SessionJournal
from envoy.app.ports import SessionRepository
from envoy.crypto.keys import PublicKey, SigningKey
from envoy.domain.events import (
    ChecklistOpened,
    Closed,
    Decided,
    Delivered,
    Disclosed,
    Escalated,
    IntentParsed,
    MandateAttested,
    Outcome,
    OwnerResolved,
    PeerMessage,
    SessionEvent,
    SessionOpened,
)
from envoy.domain.ids import MandateId, OfferHash, OwnerId, SessionId, owner_for_user
from envoy.domain.intent import Propose
from envoy.domain.offer import Offer
from envoy.domain.session import IllegalTransition, SessionState, SessionView, apply, fold
from envoy.domain.telegram import (
    BotMessageUpdate,
    CallbackUpdate,
    InlineButton,
    MessageUpdate,
    ReplyMarkup,
)
from envoy.engine.engine import Accept, Counter, NegotiationEngine
from envoy.execution.checklist import plan_commitments
from envoy.mandate.commitment import commitment_digest
from envoy.mandate.compiler import CompiledMandate, compile_mandate
from envoy.mandate.ports import MandateStore, StoredMandate
from envoy.ports.clock import Clock
from envoy.ports.rng import Rng
from envoy.protocol.bodies import (
    AcceptBody,
    CapabilitiesBody,
    CommitBody,
    DecisionBody,
    DecisionRequestBody,
    DisputeBody,
    OfferBody,
    RejectBody,
    RevokeBody,
    offer_body,
    parse_body,
    version_map,
)
from envoy.protocol.delegation import Delegation, KeyRegistry, delegation_map
from envoy.protocol.envelope import (
    SERVICE_TYPES,
    WIRE_VERSION,
    DecodedEnvelope,
    Envelope,
    InvalidEnvelope,
    MessageType,
    decode_message,
    encode_message,
)
from envoy.protocol.handshake import (
    SUPPORTED_VERSIONS,
    Accepted,
    Capabilities,
    KeyChanged,
    Rejected,
    negotiate,
    verify_hello,
)
from envoy.protocol.limits import ProtocolLimits, RateLimiter, stalled
from envoy.protocol.machine import (
    Cancel,
    DecisionRequested,
    DelegationLost,
    Effect,
    Escalate,
    LocalAccept,
    LocalOffer,
    LocalReady,
    LocalReject,
    LocalRevoke,
    NegotiationState,
    OfferRef,
    OpenChecklist,
    Opened,
    PeerAccept,
    PeerDecision,
    PeerDispute,
    PeerOffer,
    PeerReady,
    PeerReject,
    PeerRevoke,
    Phase,
    ProtocolEvent,
    Schedule,
    Send,
    TimerFired,
    transition,
)
from envoy.security.authorizer import Action, Allow, AuthContext, Authorizer, Deny, Verdict
from envoy.telegram.ports import CallbackAnswerer, MessageSender, SendRejected

logger = structlog.get_logger()
REQUIRED_CAPABILITY = "issue:datetime"
LOCAL_CAPABILITIES = Capabilities.parse(["issue:datetime@1", "issue:place@1"])
RECOVERY_TYPES = frozenset({"decision_request", "decision"})
CALLBACK_PREFIX = "ag:"
DAY = timedelta(hours=24)
TERMINAL_OUTCOMES = {
    "rejected": Outcome.rejected,
    "aborted": Outcome.rejected,
    "revoked": Outcome.rejected,
    "expired": Outcome.expired,
}

type EngineFactory = Callable[[CompiledMandate], NegotiationEngine]


@dataclass
class Step:
    session: AgentSession
    stored: StoredMandate
    compiled: CompiledMandate
    events: list[SessionEvent]
    now: datetime
    view: SessionView = field(default_factory=SessionView)
    sends: list[str] = field(default_factory=list)
    acks: list[str] = field(default_factory=list)
    notices: list[tuple[str, ReplyMarkup | None]] = field(default_factory=list)
    recovery: bool = False

    def __post_init__(self) -> None:
        self.view = fold(self.events)


class AgentFlow:
    def __init__(
        self,
        repo: ProtocolRepository,
        registry: KeyRegistry,
        delegations: DelegationSource,
        mandates: MandateStore,
        journal: SessionJournal,
        sender: MessageSender,
        authorizer: Authorizer,
        engines: EngineFactory,
        clock: Clock,
        rng: Rng,
        agent_key: SigningKey,
        bot_id: int,
        owner_user_id: int,
        *,
        human_sessions: SessionRepository | None = None,
        answerer: CallbackAnswerer | None = None,
        invitations: InvitationFlow | None = None,
        limits: ProtocolLimits | None = None,
        new_session_id: Callable[[], UUID] = uuid7,
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._delegations = delegations
        self._mandates = mandates
        self._journal = journal
        self._sender = sender
        self._authorizer = authorizer
        self._engines = engines
        self._clock = clock
        self._rng = rng
        self._key = agent_key
        self._bot_id = bot_id
        self._owner_user = owner_user_id
        self._owner = owner_for_user(owner_user_id)
        self._human_sessions = human_sessions
        self._answerer = answerer
        self._invitations = invitations
        self._limits = limits or ProtocolLimits()
        self._limiter = RateLimiter(self._limits)
        self._new_id = new_session_id
        self._locks: dict[SessionId, asyncio.Lock] = {}
        self._compiled: dict[MandateId, CompiledMandate] = {}
        self._key_alerts: set[tuple[int, bytes]] = set()

    @property
    def bot_id(self) -> int:
        return self._bot_id

    def _lock(self, sid: SessionId) -> asyncio.Lock:
        lock = self._locks.get(sid)
        if lock is None:
            lock = self._locks[sid] = asyncio.Lock()
        return lock

    def _compile(self, stored: StoredMandate) -> CompiledMandate:
        compiled = self._compiled.get(stored.id)
        if compiled is None:
            compiled = self._compiled[stored.id] = compile_mandate(stored.mandate)
        return compiled

    async def _delegation(self, now: datetime) -> tuple[Delegation, StoredMandate] | None:
        delegation = await self._delegations.current(self._owner)
        stored = await self._mandates.active(self._owner)
        if delegation is None or stored is None or now >= delegation.expires_at:
            return None
        if (delegation.mandate_id, delegation.version) != (stored.id, stored.version):
            return None
        if delegation.agent_key != self._key.public().to_bytes():
            return None
        if delegation.bot_id != self._bot_id:
            return None
        return delegation, stored

    def _nonce(self) -> bytes:
        return self._rng.token_bytes(16)

    def _ttl(self) -> timedelta:
        return self._limits.nonce_window

    def _message(
        self,
        session: AgentSession,
        kind: MessageType,
        body: Mapping[str, Any],
        now: datetime,
        *,
        seq: int,
        chain: Mapping[str, Any] | None = None,
    ) -> str:
        ts = int(now.timestamp())
        return encode_message(
            Envelope(
                v=WIRE_VERSION,
                sid=session.id,
                seq=seq,
                ack=session.peer_seq,
                type=kind,
                body=body,
                ts=ts,
                exp=ts + int(self._ttl().total_seconds()),
                nonce=self._nonce(),
                chain=chain,
            ),
            self._key,
        )

    def _enqueue(
        self,
        step: Step,
        kind: MessageType,
        body: Mapping[str, Any],
        *,
        chain: Mapping[str, Any] | None = None,
    ) -> None:
        session = step.session
        recovery = kind in RECOVERY_TYPES
        seq = (session.my_recovery if recovery else session.my_seq) + 1
        text = self._message(session, kind, body, step.now, seq=seq, chain=chain)
        item = OutboxItem(
            seq=seq,
            type=kind,
            text=text,
            deadline=step.now + self._ttl(),
            next_at=step.now + self._limits.retry_interval,
            attempts=1,
            recovery=recovery,
        )
        if recovery:
            session = replace(session, my_recovery=seq)
        else:
            session = replace(session, my_seq=seq)
        kept = tuple(entry for entry in session.outbox if entry.type != kind or not recovery)
        retained = (*kept, item) if kind != "decision" else kept
        step.session = replace(session, outbox=retained)
        step.sends.append(text)
        self._journal_event(step, PeerMessage(direction="out", text=text))

    def _ack(self, step: Step) -> None:
        session = step.session
        step.acks.append(self._message(session, "ack", {}, step.now, seq=max(session.my_seq, 1)))

    def _journal_event(self, step: Step, event: SessionEvent) -> bool:
        try:
            view = apply(step.view, event)
        except IllegalTransition:
            logger.info(
                "agent_journal_event_skipped", sid=str(step.session.id), event=type(event).__name__
            )
            return False
        step.events.append(event)
        step.view = view
        return True

    async def _load(self, session: AgentSession, now: datetime) -> Step:
        stored = await self._mandates.get(session.mandate_id)
        events = await self._journal.events(session.id)
        return Step(session, stored, self._compile(stored), events, now)

    async def _persist(self, step: Step, base: int, inbound: Inbound | None) -> AgentSession:
        if step.session.state.terminal:
            outbox = tuple(item for item in step.session.outbox if not item.recovery)
            step.session = replace(step.session, outbox=outbox)
        commitment = commitment_digest(step.stored.mandate, step.stored.salt)
        for event in step.events[base:]:
            await self._journal.append(step.session.id, event, commitment)
        saved = await self._repo.save(step.session, inbound=inbound)
        await self._transmit(saved, step)
        return saved

    async def _transmit(self, session: AgentSession, step: Step) -> None:
        for text in [*step.sends, *step.acks]:
            try:
                await self._sender.send_message(session.peer_bot_id, text)
            except SendRejected:
                logger.warning("agent_send_rejected", sid=str(session.id))
        for text, markup in step.notices:
            try:
                await self._sender.send_message(session.owner_chat_id, text, reply_markup=markup)
            except SendRejected:
                logger.warning("agent_owner_notice_failed", sid=str(session.id))

    async def _agreed_today(self, now: datetime) -> int:
        total = await self._repo.count_agreed_since(self._owner, now - DAY)
        if self._human_sessions is not None:
            total += await self._human_sessions.count_agreed_since(self._owner, now - DAY)
        return total

    async def _authorize(
        self, step: Step, action: Action, approved: frozenset[OfferHash] = frozenset()
    ) -> Verdict:
        ctx = AuthContext(
            view=step.view,
            mandate=step.compiled,
            counterpart_id=step.session.peer_bot_id,
            now=step.now,
            agreed_today=await self._agreed_today(step.now),
            reply_window_open=True,
            approved=approved,
        )
        return self._authorizer.authorize(action, ctx)

    def _apply(self, step: Step, event: ProtocolEvent) -> None:
        result = transition(step.session.state, event)
        step.session = replace(step.session, state=result.state)
        for effect in result.effects:
            self._effect(step, effect)
        if result.rejected is not None:
            logger.info("protocol_event_ignored", sid=str(step.session.id), reason=result.rejected)
        self._settle(step)

    def _timer_delay(self, step: Step, timer: str) -> timedelta:
        wait = self._limits.reply_wait
        if timer == "ready":
            return max(wait, step.compiled.escalation.timeout)
        return wait

    def _effect(self, step: Step, effect: Effect) -> None:
        session = step.session
        match effect:
            case Send():
                self._send_effect(step, effect)
            case Schedule(timer=timer):
                due = step.now + self._timer_delay(step, timer)
                step.session = replace(
                    session, timers=MappingProxyType({**session.timers, timer: due})
                )
            case Cancel(timer=timer):
                timers = {key: value for key, value in session.timers.items() if key != timer}
                step.session = replace(session, timers=MappingProxyType(timers))
            case OpenChecklist(hash=hash):
                self._open_checklist(step, hash)
            case Escalate(reason=reason):
                step.notices.append((f"Envoy: переговоры с агентом — {reason}.", None))

    def _send_effect(self, step: Step, send: Send) -> None:
        session = step.session
        match send.type:
            case "propose" | "counter":
                assert send.hash is not None
                offer = session.offers[send.hash]
                self._enqueue(step, send.type, offer_body(offer, send.round, send.previous))
            case "accept":
                self._enqueue(step, "accept", {"offer_hash": send.hash})
            case "commit":
                self._enqueue(step, "commit", {"stage": "ready", "offer_hash": send.hash})
            case "decision" if step.recovery:
                self._enqueue(
                    step,
                    "decision",
                    {
                        "offer_hash": send.hash,
                        "decision": send.decision,
                        "cursor": session.peer_recovery,
                        "decided_at": int(step.now.timestamp()),
                    },
                )
            case "decision":
                self._enqueue(
                    step,
                    "commit",
                    {"stage": "decision", "offer_hash": send.hash, "decision": send.decision},
                )
            case "decision_request":
                self._enqueue(
                    step,
                    "decision_request",
                    {"offer_hash": send.hash, "cursor": session.peer_recovery},
                )
            case "revoke":
                self._enqueue(step, "revoke", {"offer_hash": send.hash})
            case "dispute":
                self._enqueue(step, "dispute", {"entries": [send.hash or bytes(32)]})
            case "reject":
                self._enqueue(step, "reject", {"reason": send.reason or "rejected"})

    def _open_checklist(self, step: Step, hash: OfferHash) -> None:
        offer = step.session.offers.get(hash)
        if offer is None:
            return
        items = tuple(commitment.text for commitment in plan_commitments(offer, "meeting"))
        if step.view.state is SessionState.AGREED:
            self._journal_event(step, ChecklistOpened(items=items))
        terms = ", ".join(f"{issue}: {value}" for issue, value in sorted(offer.values.items()))
        lines = [f"Сделка с агентом закреплена: {terms}.", *(f"• {item}" for item in items)]
        step.notices.append(("\n".join(lines), None))

    def _settle(self, step: Step) -> None:
        state = step.session.state
        if not state.terminal or step.session.closed:
            return
        if state.phase in (Phase.EXECUTING, Phase.DISPUTED):
            return
        outcome = TERMINAL_OUTCOMES.get(state.outcome or "rejected", Outcome.rejected)
        self._journal_event(step, Closed(outcome=outcome, reason=state.outcome or "closed"))
        step.session = replace(step.session, closed=True, timers=MappingProxyType({}))
        step.notices.append(("Переговоры с агентом завершены без сделки.", None))

    async def open(self, peer_bot_id: int, owner: OwnerId) -> SessionId:
        if owner != self._owner:
            raise PermissionError("Agent bot serves a different owner")
        now = self._clock.now()
        own = await self._delegation(now)
        if own is None:
            raise PermissionError("No valid owner delegation for the active mandate")
        delegation, stored = own
        active = await self._repo.active_with(self._bot_id, peer_bot_id)
        if len(active) >= self._limits.sessions_per_pair:
            raise PermissionError("Too many concurrent sessions with this agent")
        sid = SessionId(self._new_id())
        session = AgentSession(
            id=sid,
            bot_id=self._bot_id,
            peer_bot_id=peer_bot_id,
            owner_id=self._owner,
            owner_chat_id=self._owner_user,
            mandate_id=stored.id,
            mandate_version=stored.version,
            state=NegotiationState(role="coordinator", me=delegation.agent_id.bytes, peer=b""),
            deadline=now + stored.mandate.limits.session_deadline,
            created_at=now,
        )
        async with self._lock(sid):
            step = Step(session, stored, self._compile(stored), [], now)
            self._open_journal(step)
            self._enqueue(step, "hello", self._hello(delegation), chain=delegation_map(delegation))
            await self._persist(step, 0, None)
        return sid

    def _open_journal(self, step: Step) -> None:
        session = step.session
        opening: list[SessionEvent] = [
            SessionOpened(
                owner_id=session.owner_id,
                mandate_id=session.mandate_id,
                connection_id=f"agent:{session.peer_bot_id}",
                counterpart_chat_id=session.peer_bot_id,
                owner_chat_id=session.owner_chat_id,
                topic_id=None,
                deadline=session.deadline,
            ),
            MandateAttested(
                mandate_id=session.mandate_id,
                version=session.mandate_version,
                attestation=step.stored.attestation,
            ),
            Disclosed(),
        ]
        for event in opening:
            self._journal_event(step, event)

    @staticmethod
    def _hello(delegation: Delegation, token: str | None = None) -> dict[str, Any]:
        return {
            "agent_id": delegation.agent_id.bytes,
            "owner_id": delegation.owner_id.bytes,
            "min_version": version_map(SUPPORTED_VERSIONS[0]),
            "max_version": version_map(SUPPORTED_VERSIONS[1]),
            "token": token,
        }

    async def receive(self, update: BotMessageUpdate) -> None:
        if update.bot_id != self._bot_id or await self._repo.seen(update.bot_id, update.update_id):
            return
        now = self._clock.now()
        marker = Inbound(update.bot_id, update.update_id, update.from_bot_id)
        if not update.text.startswith("envoy/"):
            await self._repo.mark(marker)
            return
        pair = (self._bot_id, update.from_bot_id)
        try:
            decoded = decode_message(update.text)
        except InvalidEnvelope as error:
            logger.info("protocol_message_invalid", reason=error.reason)
            await self._repo.mark(marker)
            return
        envelope = decoded.envelope
        session_key = (self._bot_id, envelope.sid)
        if not self._limiter.allow(update.from_bot_id, pair, session_key, now):
            logger.info("protocol_rate_limited", sender=update.from_bot_id)
            await self._repo.mark(marker)
            return
        stamp = int(now.timestamp())
        skew = int(self._limits.max_clock_skew.total_seconds())
        if envelope.exp < stamp or envelope.ts > stamp + skew:
            await self._repo.mark(marker)
            return
        if await self._repo.nonce_seen(update.from_bot_id, envelope.nonce, now):
            await self._repo.mark(marker)
            return
        inbound = Inbound(
            update.bot_id,
            update.update_id,
            update.from_bot_id,
            envelope.nonce,
            datetime.fromtimestamp(envelope.exp, now.tzinfo) + self._limits.max_clock_skew,
            update.text,
        )
        async with self._lock(envelope.sid):
            session = await self._repo.get(envelope.sid)
            try:
                if session is None:
                    await self._first_contact(update, decoded, now, inbound)
                else:
                    await self._existing(session, update, decoded, now, inbound)
            except StaleSession:
                logger.warning("protocol_session_conflict", sid=str(envelope.sid))

    async def _reject_unknown(
        self,
        peer: int,
        decoded: DecodedEnvelope,
        reason: str,
        supported: tuple[str, ...],
        now: datetime,
    ) -> None:
        envelope = decoded.envelope
        ts = int(now.timestamp())
        text = encode_message(
            Envelope(
                v=WIRE_VERSION,
                sid=envelope.sid,
                seq=1,
                ack=envelope.seq,
                type="reject",
                body={"reason": reason, "supported": list(supported)},
                ts=ts,
                exp=ts + int(self._ttl().total_seconds()),
                nonce=self._nonce(),
            ),
            self._key,
        )
        try:
            await self._sender.send_message(peer, text)
        except SendRejected:
            logger.warning("agent_send_rejected", sid=str(envelope.sid))

    async def _first_contact(
        self, update: BotMessageUpdate, decoded: DecodedEnvelope, now: datetime, inbound: Inbound
    ) -> None:
        envelope = decoded.envelope
        if envelope.type != "hello" or envelope.seq != 1:
            await self._repo.mark(inbound)
            return
        result = await verify_hello(
            decoded, from_bot_id=update.from_bot_id, now=now, registry=self._registry
        )
        match result:
            case Rejected(authenticated=False):
                await self._repo.mark(inbound)
                return
            case Rejected(reason=reason, supported=supported):
                await self._repo.mark(inbound)
                await self._reject_unknown(update.from_bot_id, decoded, reason, supported, now)
                return
            case KeyChanged(presented=presented):
                await self._repo.mark(inbound)
                await self._key_changed(update.from_bot_id, presented.agent_key)
                return
        assert isinstance(result, Accepted)
        if result.hello.token is not None:
            await self._join(update, decoded, result, now, inbound, result.hello.token)
            return
        if self._human_sessions is not None and await self._human_sessions.get(envelope.sid):
            await self._repo.mark(inbound)
            await self._reject_unknown(update.from_bot_id, decoded, "session_exists", (), now)
            return
        own = await self._delegation(now)
        active = await self._repo.active_with(self._bot_id, update.from_bot_id)
        if own is None or len(active) >= self._limits.sessions_per_pair:
            reason = "no_delegation" if own is None else "too_many_sessions"
            await self._repo.mark(inbound)
            await self._reject_unknown(update.from_bot_id, decoded, reason, (), now)
            return
        delegation, stored = own
        session = AgentSession(
            id=envelope.sid,
            bot_id=self._bot_id,
            peer_bot_id=update.from_bot_id,
            owner_id=self._owner,
            owner_chat_id=self._owner_user,
            mandate_id=stored.id,
            mandate_version=stored.version,
            state=NegotiationState(
                role="participant",
                me=delegation.agent_id.bytes,
                peer=result.delegation.agent_id.bytes,
            ),
            deadline=now + stored.mandate.limits.session_deadline,
            created_at=now,
            peer_agent_key=result.pin.agent_key,
            peer_delegation_expires=result.delegation.expires_at,
            peer_seq=1,
        )
        step = Step(session, stored, self._compile(stored), [], now)
        self._open_journal(step)
        self._journal_event(step, PeerMessage(direction="in", text=update.text))
        self._enqueue(step, "hello", self._hello(delegation), chain=delegation_map(delegation))
        self._enqueue(step, "capabilities", {"items": list(LOCAL_CAPABILITIES.items())})
        step.notices.append(
            (
                f"Envoy: агент бота {update.from_bot_id} начал переговоры "
                "от имени своего владельца.",
                None,
            )
        )
        await self._persist(step, 0, inbound)

    async def _join(
        self,
        update: BotMessageUpdate,
        decoded: DecodedEnvelope,
        accepted: Accepted,
        now: datetime,
        inbound: Inbound,
        token: str,
    ) -> None:
        envelope = decoded.envelope

        async def refuse(reason: str) -> None:
            await self._repo.mark(inbound)
            await self._reject_unknown(update.from_bot_id, decoded, reason, (), now)

        if self._invitations is None or self._human_sessions is None:
            await refuse("invitations_disabled")
            return
        if sid_of(token) != envelope.sid:
            await refuse("token_mismatch")
            return
        record = await self._human_sessions.get(envelope.sid)
        own = await self._delegation(now)
        if record is None or record.closed_at is not None or record.owner_id != self._owner:
            await refuse("unknown_session")
            return
        if own is None:
            await refuse("no_delegation")
            return
        user = await self._invitations.expected_user(token)
        try:
            await self._invitations.bind_agent(
                token, VerifiedPeer(update.from_bot_id, -1 if user is None else user)
            )
        except InvitationRejected as error:
            await refuse(error.reason)
            return
        stored = await self._mandates.get(record.mandate_id)
        compiled = self._compile(stored)
        events = await self._journal.events(record.id)
        view = fold(events)
        offers = {offer.hash(): offer for offer in view.history}
        if view.last_outgoing is not None:
            offers[view.last_outgoing.hash()] = view.last_outgoing
        state = NegotiationState(
            role="participant",
            me=own[0].agent_id.bytes,
            peer=accepted.delegation.agent_id.bytes,
            phase=Phase.NEGOTIATING,
            round=view.round,
            last_sent=(
                None
                if view.last_outgoing is None
                else OfferRef(view.last_outgoing.hash(), view.round)
            ),
            answered=frozenset(offer.hash() for offer in view.history),
        )
        session = AgentSession(
            id=record.id,
            bot_id=self._bot_id,
            peer_bot_id=update.from_bot_id,
            owner_id=self._owner,
            owner_chat_id=self._owner_user,
            mandate_id=record.mandate_id,
            mandate_version=stored.version,
            state=state,
            deadline=record.deadline,
            created_at=now,
            peer_agent_key=accepted.pin.agent_key,
            peer_delegation_expires=accepted.delegation.expires_at,
            peer_seq=1,
            offers=MappingProxyType(offers),
            peer_history=tuple(
                (offer.hash(), compiled.utility.value(offer)) for offer in view.history
            ),
            timers=MappingProxyType({"reply": now + self._limits.reply_wait}),
            human_connection=record.connection_id,
            human_chat=record.counterpart_chat_id,
        )
        step = Step(session, stored, compiled, events, now)
        base = len(events)
        self._journal_event(step, PeerMessage(direction="in", text=update.text))
        self._enqueue(step, "hello", self._hello(own[0]), chain=delegation_map(own[0]))
        self._enqueue(step, "capabilities", {"items": list(LOCAL_CAPABILITIES.items())})
        step.notices.append(
            (
                f"Envoy: агент собеседника из чата {record.counterpart_chat_id} подключился "
                "к переговорам; история и условия сохранены.",
                None,
            )
        )
        await self._persist(step, base, inbound)

    async def _switch_to_human(self, step: Step) -> bool:
        session = step.session
        if self._invitations is None or session.human_chat is None:
            return False
        if session.state.my_ready or session.state.terminal:
            return False
        switched = await self._invitations.repo.switch(
            session.id, agent_route(session.peer_bot_id), HUMAN
        )
        if not switched:
            return False
        step.session = replace(
            session,
            state=replace(session.state, phase=Phase.CLOSED, outcome="switched"),
            closed=True,
            timers=MappingProxyType({}),
            outbox=(),
        )
        text = "Агент не ответил вовремя — продолжаем переговоры здесь, с вами."
        try:
            await self._sender.send_message(
                session.human_chat, text, business_connection_id=session.human_connection
            )
        except SendRejected:
            logger.warning("agent_switch_notice_failed", sid=str(session.id))
        step.notices.append(("Envoy: агент собеседника молчит, переговоры вернулись в чат.", None))
        return True

    async def _key_changed(self, bot_id: int, presented: bytes) -> None:
        if (bot_id, presented) in self._key_alerts:
            return
        self._key_alerts.add((bot_id, presented))
        fingerprint = PublicKey.from_bytes(presented).fingerprint()
        text = (
            f"Envoy: агент бота {bot_id} предъявил новый ключ {fingerprint} без доказательства "
            "смены. Переговоры не начаты; сверьте отпечаток по внешнему каналу."
        )
        try:
            await self._sender.send_message(self._owner_user, text)
        except SendRejected:
            logger.warning("agent_owner_notice_failed", bot=bot_id)

    async def _existing(
        self,
        session: AgentSession,
        update: BotMessageUpdate,
        decoded: DecodedEnvelope,
        now: datetime,
        inbound: Inbound,
    ) -> None:
        envelope = decoded.envelope
        if update.from_bot_id != session.peer_bot_id or session.bot_id != self._bot_id:
            await self._repo.mark(inbound)
            return
        step = await self._load(session, now)
        base = len(step.events)
        if not session.peer_agent_key:
            await self._peer_hello(step, update, decoded, inbound, base)
            return
        if not decoded.verify(PublicKey.from_bytes(session.peer_agent_key)):
            await self._repo.mark(inbound)
            return
        step.session = self._acknowledged(session, envelope.ack)
        if envelope.type in RECOVERY_TYPES:
            await self._recovery(step, decoded, inbound, base)
            return
        if envelope.type in SERVICE_TYPES or envelope.seq <= session.peer_seq:
            if envelope.type not in SERVICE_TYPES:
                self._ack(step)
            await self._persist(step, base, inbound)
            return
        if envelope.seq != session.peer_seq + 1:
            await self._repo.mark(replace(inbound, nonce=None, text=None))
            return
        step.session = replace(step.session, peer_seq=envelope.seq)
        if envelope.type not in SERVICE_TYPES:
            self._journal_event(step, PeerMessage(direction="in", text=update.text))
        before = len(step.sends)
        await self._dispatch(step, decoded)
        if envelope.type not in SERVICE_TYPES and len(step.sends) == before:
            self._ack(step)
        await self._persist(step, base, inbound)

    @staticmethod
    def _acknowledged(session: AgentSession, ack: int) -> AgentSession:
        peer_ack = max(session.peer_ack, ack)
        outbox = tuple(item for item in session.outbox if item.recovery or item.seq > peer_ack)
        return replace(session, peer_ack=peer_ack, outbox=outbox)

    async def _peer_hello(
        self,
        step: Step,
        update: BotMessageUpdate,
        decoded: DecodedEnvelope,
        inbound: Inbound,
        base: int,
    ) -> None:
        envelope = decoded.envelope
        if envelope.type != "hello" or envelope.seq != 1:
            await self._repo.mark(replace(inbound, nonce=None, text=None))
            return
        result = await verify_hello(
            decoded, from_bot_id=update.from_bot_id, now=step.now, registry=self._registry
        )
        if isinstance(result, KeyChanged):
            await self._repo.mark(inbound)
            await self._key_changed(update.from_bot_id, result.presented.agent_key)
            return
        if not isinstance(result, Accepted):
            await self._repo.mark(inbound)
            return
        session = replace(
            step.session,
            peer_agent_key=result.pin.agent_key,
            peer_delegation_expires=result.delegation.expires_at,
            peer_seq=1,
            state=replace(step.session.state, peer=result.delegation.agent_id.bytes),
        )
        step.session = self._acknowledged(session, envelope.ack)
        self._journal_event(step, PeerMessage(direction="in", text=update.text))
        self._enqueue(step, "capabilities", {"items": list(LOCAL_CAPABILITIES.items())})
        await self._persist(step, base, inbound)

    async def _recovery(
        self, step: Step, decoded: DecodedEnvelope, inbound: Inbound, base: int
    ) -> None:
        envelope = decoded.envelope
        if envelope.seq <= step.session.peer_recovery:
            await self._repo.mark(inbound)
            return
        step.session = replace(step.session, peer_recovery=envelope.seq)
        try:
            body = parse_body(envelope.type, envelope.body)
        except InvalidEnvelope:
            await self._repo.mark(inbound)
            return
        step.recovery = True
        if isinstance(body, DecisionRequestBody):
            self._apply(step, DecisionRequested(OfferHash(body.offer_hash)))
        elif isinstance(body, DecisionBody):
            self._apply(step, PeerDecision(OfferHash(body.offer_hash), body.decision))
        step.recovery = False
        await self._persist(step, base, inbound)

    async def _dispatch(self, step: Step, decoded: DecodedEnvelope) -> None:
        envelope = decoded.envelope
        if envelope.type in SERVICE_TYPES:
            return
        if envelope.crit:
            self._apply(step, LocalReject("unsupported"))
            return
        try:
            body = parse_body(envelope.type, envelope.body)
        except InvalidEnvelope:
            self._apply(step, LocalReject("bad_body"))
            return
        match body:
            case CapabilitiesBody():
                await self._capabilities(step, body)
            case OfferBody():
                await self._peer_offer(step, body, envelope.seq)
            case AcceptBody():
                self._apply(step, PeerAccept(OfferHash(body.offer_hash)))
                if step.session.phase is Phase.AGREED:
                    await self._confirm_peer_accept(step, OfferHash(body.offer_hash), envelope.seq)
            case RejectBody():
                self._apply(step, PeerReject(body.reason))
            case CommitBody(stage="ready"):
                self._apply(step, PeerReady(OfferHash(body.offer_hash)))
            case CommitBody(decision=decision) if decision is not None:
                self._apply(step, PeerDecision(OfferHash(body.offer_hash), decision))
            case RevokeBody():
                self._apply(step, PeerRevoke(OfferHash(body.offer_hash)))
            case DisputeBody():
                agreed = step.session.state.agreed
                if agreed is not None:
                    self._apply(step, PeerDispute(agreed))
            case _:
                return

    async def _capabilities(self, step: Step, body: CapabilitiesBody) -> None:
        chosen = negotiate(LOCAL_CAPABILITIES, Capabilities.parse(body.items))
        step.session = replace(step.session, capabilities=chosen.items())
        if REQUIRED_CAPABILITY not in chosen.chosen:
            self._apply(step, Opened())
            self._apply(step, LocalReject("unsupported"))
            return
        if step.session.phase is not Phase.HANDSHAKE:
            return
        self._apply(step, Opened())
        if step.session.state.role == "coordinator":
            await self._respond(step, None, 0)

    def _engine(self, step: Step) -> NegotiationEngine:
        return self._engines(step.compiled)

    def _own_offers(self, step: Step) -> list[Offer]:
        peers = {hash for hash, _ in step.session.peer_history}
        return [offer for hash, offer in step.session.offers.items() if hash not in peers]

    def _peer_offers(self, step: Step) -> list[Offer]:
        return [step.session.offers[hash] for hash, _ in step.session.peer_history]

    async def _peer_offer(self, step: Step, body: OfferBody, cause: int) -> None:
        offer = body.public_offer()
        hash = offer.hash()
        utility = step.compiled.utility.value(offer)
        history = (*step.session.peer_history, (hash, utility))
        if stalled(history, self._limits.stall_rounds):
            step.session = replace(step.session, peer_history=history)
            self._apply(step, LocalReject("stalled"))
            return
        step.session = replace(step.session.with_offer(offer), peer_history=history)
        previous = None if body.previous is None else OfferHash(body.previous)
        self._apply(step, PeerOffer(hash, previous, body.round))
        received = step.session.state.last_received
        if received is None or received.hash != hash:
            return
        self._journal_event(step, IntentParsed(cause=cause, intent=Propose(offer=offer)))
        await self._respond(step, offer, cause)

    async def _respond(self, step: Step, incoming: Offer | None, cause: int) -> None:
        if await self._delegation_lost(step):
            return
        rounds = self._limits.rounds(step.compiled.limits.max_rounds)
        decision = self._engine(step).decide(
            incoming,
            self._peer_offers(step),
            step.session.state.round,
            exclude=self._own_offers(step),
        )
        match decision:
            case Accept(offer=offer):
                await self._accept(step, offer, cause)
            case Counter(offer=offer):
                if step.session.state.round >= rounds:
                    self._apply(step, LocalReject("round_limit"))
                    return
                verdict = await self._authorize(step, Action("send_offer", offer))
                if not isinstance(verdict, Allow):
                    self._apply(step, LocalReject(self._reason(verdict)))
                    return
                decided = self._decided(step, cause, "send_offer", offer, "allow")
                step.session = step.session.with_offer(offer)
                self._apply(step, LocalOffer(offer.hash()))
                if decided:
                    self._journal_event(
                        step, Delivered(cause=cause, message_id=step.session.my_seq)
                    )
            case _:
                self._apply(step, LocalReject(decision.reason))

    @staticmethod
    def _reason(verdict: Verdict) -> str:
        return verdict.reason if isinstance(verdict, Deny) else "not_allowed"

    def _decided(self, step: Step, cause: int, kind: Any, offer: Offer, verdict: Any) -> bool:
        if step.view.state is not SessionState.NEGOTIATING:
            return False
        return self._journal_event(
            step, Decided(cause=cause, kind=kind, offer=offer, verdict=verdict)
        )

    async def _accept(self, step: Step, offer: Offer, cause: int, approved: bool = False) -> None:
        verdict = await self._authorize(
            step,
            Action("accept_offer", offer),
            frozenset({offer.hash()}) if approved else frozenset(),
        )
        match verdict:
            case Allow():
                decided = self._decided(step, cause, "accept_offer", offer, "allow")
                self._apply(step, LocalAccept(offer.hash()))
                await self._ready(step, offer, cause, decided)
            case Deny(reason=reason):
                self._apply(step, LocalReject(reason))
            case _:
                self._escalate(step, offer, cause, "accept_offer")

    async def _confirm_peer_accept(
        self, step: Step, hash: OfferHash, cause: int, approved: bool = False
    ) -> None:
        offer = step.session.offers.get(hash)
        if offer is None:
            return
        verdict = await self._authorize(
            step, Action("accept_offer", offer), frozenset({hash}) if approved else frozenset()
        )
        match verdict:
            case Allow():
                decided = self._decided(step, cause, "accept_offer", offer, "allow")
                await self._ready(step, offer, cause, decided)
            case Deny():
                self._apply(step, LocalRevoke(hash))
            case _:
                self._escalate(step, offer, cause, "confirm_accept")

    async def _ready(self, step: Step, offer: Offer, cause: int, decided: bool) -> None:
        if decided:
            self._journal_event(step, Delivered(cause=cause, message_id=step.session.my_seq))
        step.session = replace(step.session, agreed_at=step.now, pending=None)
        self._apply(step, LocalReady(offer.hash()))

    def _escalate(self, step: Step, offer: Offer, cause: int, kind: Any) -> None:
        if step.view.state is SessionState.NEGOTIATING:
            self._journal_event(
                step, Decided(cause=cause, kind="accept_offer", offer=offer, verdict="escalate")
            )
            self._journal_event(
                step,
                Escalated(
                    cause=cause,
                    reason="confirm_action",
                    expires_at=step.now + step.compiled.escalation.timeout,
                ),
            )
        step.session = replace(step.session, pending=PendingOwner(cause, kind, offer.hash()))
        terms = ", ".join(f"{issue}: {value}" for issue, value in sorted(offer.values.items()))
        data = f"{CALLBACK_PREFIX}{{}}:{step.session.id.hex}:{cause}"
        step.notices.append(
            (
                f"Нужно решение по сделке с агентом: {terms}.",
                ReplyMarkup(
                    (
                        (
                            InlineButton("Одобрить", data.format("a"), "success"),
                            InlineButton("Отклонить", data.format("d"), "danger"),
                        ),
                    )
                ),
            )
        )

    async def _delegation_lost(self, step: Step) -> bool:
        session = step.session
        own = await self._delegation(step.now)
        peer_expired = (
            session.peer_delegation_expires is not None
            and step.now >= session.peer_delegation_expires
        )
        lost = own is None or own[1].id != session.mandate_id or peer_expired
        if lost and not session.state.terminal and session.state.decision is None:
            self._apply(step, DelegationLost())
            return True
        return False

    async def on_owner_decision(self, data: str, user_id: int) -> str:
        parts = data.removeprefix(CALLBACK_PREFIX).split(":")
        if len(parts) != 3 or parts[0] not in ("a", "d") or not parts[2].isdigit():
            return "Неизвестная команда"
        if owner_for_user(user_id) != self._owner:
            return "Решение уже не требуется"
        try:
            sid = SessionId(UUID(hex=parts[1]))
        except ValueError:
            return "Неизвестная команда"
        cause = int(parts[2])
        async with self._lock(sid):
            session = await self._repo.get(sid)
            if session is None or session.pending is None or session.pending.cause != cause:
                return "Решение уже не требуется"
            step = await self._load(session, self._clock.now())
            base = len(step.events)
            pending = session.pending
            expires = step.view.pending.expires_at if step.view.pending is not None else None
            if expires is not None and step.now >= expires:
                return "Время на решение истекло"
            approve = parts[0] == "a"
            if step.view.pending is not None:
                self._journal_event(
                    step,
                    OwnerResolved(cause=cause, resolution="approved" if approve else "declined"),
                )
            step.session = replace(step.session, pending=None)
            offer = step.session.offers[pending.hash]
            if approve and pending.kind == "accept_offer":
                await self._accept(step, offer, -cause, approved=True)
            elif approve:
                await self._confirm_peer_accept(step, pending.hash, -cause, approved=True)
            elif pending.kind == "accept_offer":
                self._apply(step, LocalReject("owner_declined"))
            else:
                self._apply(step, LocalRevoke(pending.hash))
            await self._persist(step, base, None)
        return "Одобрено" if approve else "Отклонено"

    async def on_callback(self, update: CallbackUpdate) -> None:
        text = await self.on_owner_decision(update.data, update.user_id)
        if self._answerer is not None:
            await self._answerer.answer_callback(update.callback_id, text)

    async def on_command(self, update: MessageUpdate) -> None:
        parts = (update.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            reply = "Использование: /agent <числовой ID бота-агента собеседника>"
        else:
            try:
                await self.open(int(parts[1]), owner_for_user(update.user_id))
            except PermissionError as error:
                reply = f"Не удалось начать переговоры с агентом: {error}."
            else:
                reply = "Отправил приглашение агенту собеседника."
        try:
            await self._sender.send_message(update.chat_id, reply)
        except SendRejected:
            logger.warning("agent_owner_notice_failed", chat=update.chat_id)

    async def pending_escalation(self, sid: SessionId) -> tuple[int, OfferHash, datetime] | None:
        session = await self._repo.get(sid)
        if session is None or session.pending is None or session.owner_id != self._owner:
            return None
        view = fold(await self._journal.events(sid))
        expires = view.pending.expires_at if view.pending is not None else None
        if expires is None:
            expires = session.created_at + self._limits.reply_wait
        return session.pending.cause, session.pending.hash, expires

    async def resolve_signed(self, sid: SessionId, cause: int, approve: bool) -> str:
        data = f"{CALLBACK_PREFIX}{'a' if approve else 'd'}:{sid.hex}:{cause}"
        return await self.on_owner_decision(data, self._owner_user)

    def fingerprint(self) -> str:
        return self._key.public().fingerprint()

    async def run_due(self, now: datetime) -> None:
        for session in await self._repo.due(now):
            async with self._lock(session.id):
                current = await self._repo.get(session.id)
                if current is None:
                    continue
                try:
                    await self._tick(current, now)
                except StaleSession:
                    logger.warning("protocol_session_conflict", sid=str(session.id))

    async def _tick(self, session: AgentSession, now: datetime) -> None:
        step = await self._load(session, now)
        base = len(step.events)
        resend: list[str] = []
        outbox: list[OutboxItem] = []
        for item in session.outbox:
            if item.deadline <= now:
                continue
            if item.next_at <= now:
                resend.append(item.text)
                item = replace(
                    item, attempts=item.attempts + 1, next_at=now + self._limits.retry_interval
                )
            outbox.append(item)
        step.session = replace(session, outbox=tuple(outbox))
        for timer, due in sorted(session.timers.items(), key=lambda entry: entry[1]):
            if due <= now and timer in step.session.timers:
                timers = {key: value for key, value in step.session.timers.items() if key != timer}
                step.session = replace(step.session, timers=MappingProxyType(timers))
                if timer == "reply" and await self._switch_to_human(step):
                    break
                self._apply(step, TimerFired(timer))  # type: ignore[arg-type]
        if step.session.phase is Phase.NEGOTIATING and now >= step.session.deadline:
            self._apply(step, LocalReject("session_deadline"))
        elif step.session.phase in (Phase.NEGOTIATING, Phase.AGREED):
            await self._delegation_lost(step)
        step.sends[:0] = resend
        await self._persist(step, base, None)
