from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from envoy.domain.ids import OfferHash

type Role = Literal["coordinator", "participant"]
type DecisionKind = Literal["commit", "abort"]
type Timer = Literal["reply", "ready", "decision"]


class Phase(StrEnum):
    HANDSHAKE = "HANDSHAKE"
    NEGOTIATING = "NEGOTIATING"
    AGREED = "AGREED"
    COMMITTING = "COMMITTING"
    EXECUTING = "EXECUTING"
    ABORTED = "ABORTED"
    REVOKED = "REVOKED"
    DISPUTED = "DISPUTED"
    CLOSED = "CLOSED"


TERMINAL = frozenset({Phase.EXECUTING, Phase.ABORTED, Phase.REVOKED, Phase.DISPUTED, Phase.CLOSED})


@dataclass(frozen=True)
class OfferRef:
    hash: OfferHash
    round: int


@dataclass(frozen=True)
class NegotiationState:
    role: Role
    me: bytes
    peer: bytes
    phase: Phase = Phase.HANDSHAKE
    round: int = 0
    last_sent: OfferRef | None = None
    last_received: OfferRef | None = None
    answered: frozenset[OfferHash] = frozenset()
    agreed: OfferHash | None = None
    my_ready: bool = False
    peer_ready: bool = False
    decision: DecisionKind | None = None
    revoke_requested: bool = False
    escalated: bool = False
    decision_requests: int = 0
    checklist_opened: bool = False
    outcome: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL


@dataclass(frozen=True)
class Opened:
    pass


@dataclass(frozen=True)
class LocalOffer:
    hash: OfferHash


@dataclass(frozen=True)
class PeerOffer:
    hash: OfferHash
    previous: OfferHash | None
    round: int


@dataclass(frozen=True)
class LocalAccept:
    hash: OfferHash


@dataclass(frozen=True)
class PeerAccept:
    hash: OfferHash


@dataclass(frozen=True)
class LocalReject:
    reason: str


@dataclass(frozen=True)
class PeerReject:
    reason: str


@dataclass(frozen=True)
class LocalReady:
    hash: OfferHash


@dataclass(frozen=True)
class PeerReady:
    hash: OfferHash


@dataclass(frozen=True)
class PeerDecision:
    hash: OfferHash
    decision: DecisionKind


@dataclass(frozen=True)
class DecisionRequested:
    hash: OfferHash


@dataclass(frozen=True)
class LocalRevoke:
    hash: OfferHash


@dataclass(frozen=True)
class PeerRevoke:
    hash: OfferHash


@dataclass(frozen=True)
class PeerDispute:
    hash: OfferHash


@dataclass(frozen=True)
class TimerFired:
    timer: Timer


@dataclass(frozen=True)
class DelegationLost:
    pass


type ProtocolEvent = (
    Opened
    | LocalOffer
    | PeerOffer
    | LocalAccept
    | PeerAccept
    | LocalReject
    | PeerReject
    | LocalReady
    | PeerReady
    | PeerDecision
    | DecisionRequested
    | LocalRevoke
    | PeerRevoke
    | PeerDispute
    | TimerFired
    | DelegationLost
)


@dataclass(frozen=True)
class Send:
    type: str
    hash: OfferHash | None = None
    decision: DecisionKind | None = None
    reason: str | None = None
    previous: OfferHash | None = None
    round: int = 0


@dataclass(frozen=True)
class Schedule:
    timer: Timer


@dataclass(frozen=True)
class Cancel:
    timer: Timer


@dataclass(frozen=True)
class OpenChecklist:
    hash: OfferHash


@dataclass(frozen=True)
class Escalate:
    reason: str


type Effect = Send | Schedule | Cancel | OpenChecklist | Escalate


@dataclass(frozen=True)
class Transition:
    state: NegotiationState
    effects: tuple[Effect, ...] = ()
    rejected: str | None = None


def turn_priority(agent_id: bytes) -> bytes:
    return sha256(agent_id).digest()


def wins_tie(state: NegotiationState) -> bool:
    return turn_priority(state.me) < turn_priority(state.peer)


def _ignore(state: NegotiationState, reason: str) -> Transition:
    return Transition(state, (), reason)


def _close(state: NegotiationState, phase: Phase, outcome: str, *effects: Effect) -> Transition:
    cancels = (Cancel("reply"), Cancel("ready"), Cancel("decision"))
    return Transition(replace(state, phase=phase, outcome=outcome), (*effects, *cancels))


def _decide(state: NegotiationState, decision: DecisionKind) -> Transition:
    assert state.agreed is not None
    send = Send("decision", state.agreed, decision)
    if decision == "commit":
        executing = replace(state, decision="commit", phase=Phase.EXECUTING, checklist_opened=True)
        return Transition(
            replace(executing, outcome="committed"),
            (send, OpenChecklist(state.agreed), Cancel("ready")),
        )
    return _close(replace(state, decision="abort"), Phase.ABORTED, "aborted", send)


def _negotiating(state: NegotiationState, event: ProtocolEvent) -> Transition:
    match event:
        case LocalOffer(hash=offer):
            if state.last_sent is not None and state.last_received is None:
                return _ignore(state, "awaiting_reply")
            if state.last_received is not None and offer == state.last_received.hash:
                return _ignore(state, "counter_equals_received")
            if state.last_received is not None and state.last_received.hash in state.answered:
                return _ignore(state, "already_answered")
            answered = state.answered
            kind = "propose"
            previous = None
            if state.last_received is not None:
                previous = state.last_received.hash
                answered = answered | {previous}
                kind = "counter"
            sent = OfferRef(offer, state.round + 1)
            return Transition(
                replace(
                    state,
                    round=sent.round,
                    last_sent=sent,
                    last_received=None,
                    answered=answered,
                ),
                (Send(kind, offer, previous=previous, round=sent.round), Schedule("reply")),
            )
        case PeerOffer(hash=offer, previous=previous, round=round):
            if offer in state.answered or (
                state.last_received is not None and state.last_received.hash == offer
            ):
                return _ignore(state, "duplicate_offer")
            crossing = state.last_sent is not None and previous != state.last_sent.hash
            if crossing and wins_tie(state):
                return _ignore(state, "lost_tie")
            return Transition(
                replace(
                    state,
                    last_sent=None,
                    last_received=OfferRef(offer, max(round, state.round)),
                    round=max(round, state.round),
                ),
                (Cancel("reply"),),
            )
        case LocalAccept(hash=offer):
            received = state.last_received
            if received is None or received.hash != offer or offer in state.answered:
                return _ignore(state, "not_last_unanswered")
            return Transition(
                replace(
                    state,
                    phase=Phase.AGREED,
                    agreed=offer,
                    answered=state.answered | {offer},
                    last_received=None,
                ),
                (Send("accept", offer), Cancel("reply"), Schedule("ready")),
            )
        case PeerAccept(hash=offer):
            if state.last_sent is None or state.last_sent.hash != offer:
                return _ignore(state, "not_last_unanswered")
            return Transition(
                replace(state, phase=Phase.AGREED, agreed=offer, last_sent=None),
                (Cancel("reply"), Schedule("ready")),
            )
        case LocalReject(reason=reason):
            return _close(state, Phase.CLOSED, "rejected", Send("reject", reason=reason))
        case PeerReject():
            return _close(state, Phase.CLOSED, "rejected")
        case TimerFired(timer="reply"):
            return _close(state, Phase.CLOSED, "expired", Send("reject", reason="timeout"))
        case DelegationLost():
            return _close(state, Phase.CLOSED, "rejected", Send("reject", reason="delegation"))
        case _:
            return _ignore(state, "unexpected")


def _matches(state: NegotiationState, offer: OfferHash) -> bool:
    return state.agreed is not None and offer == state.agreed


def _committing(state: NegotiationState, event: ProtocolEvent) -> Transition:
    coordinator = state.role == "coordinator"
    match event:
        case LocalReady(hash=offer):
            if not _matches(state, offer) or state.my_ready or state.revoke_requested:
                return _ignore(state, "ready_rejected")
            ready = replace(state, my_ready=True, phase=Phase.COMMITTING)
            if coordinator:
                if ready.peer_ready:
                    return _decide(ready, "commit")
                return Transition(ready, (Send("commit", offer),))
            return Transition(ready, (Send("commit", offer), Cancel("ready"), Schedule("decision")))
        case PeerReady(hash=offer):
            if not _matches(state, offer):
                return _ignore(state, "ready_mismatch")
            ready = replace(state, peer_ready=True)
            if coordinator and ready.my_ready and ready.decision is None:
                return _decide(ready, "commit")
            return Transition(ready)
        case PeerDecision(hash=offer, decision=decision):
            if coordinator or not _matches(state, offer):
                return _ignore(state, "decision_rejected")
            if decision == "commit":
                if not state.my_ready:
                    return _ignore(state, "commit_without_ready")
                late = (Escalate("revoke_too_late"),) if state.revoke_requested else ()
                return Transition(
                    replace(
                        state,
                        decision="commit",
                        phase=Phase.EXECUTING,
                        checklist_opened=True,
                        outcome="committed",
                    ),
                    (OpenChecklist(offer), Cancel("decision"), Cancel("ready"), *late),
                )
            return _close(replace(state, decision="abort"), Phase.ABORTED, "aborted")
        case LocalRevoke(hash=offer):
            if not _matches(state, offer):
                return _ignore(state, "revoke_mismatch")
            if coordinator:
                return _decide(state, "abort")
            if not state.my_ready:
                return _close(state, Phase.REVOKED, "revoked", Send("revoke", offer))
            return Transition(replace(state, revoke_requested=True), (Send("revoke", offer),))
        case PeerRevoke(hash=offer):
            if not _matches(state, offer):
                return _ignore(state, "revoke_mismatch")
            if coordinator:
                return _decide(state, "abort")
            return _close(replace(state, decision="abort"), Phase.ABORTED, "aborted")
        case TimerFired(timer="ready"):
            if coordinator:
                return _decide(state, "abort")
            if not state.my_ready:
                return _close(state, Phase.REVOKED, "expired", Send("revoke", state.agreed))
            return _ignore(state, "ready_sent")
        case TimerFired(timer="decision"):
            if coordinator or not state.my_ready:
                return _ignore(state, "no_decision_wait")
            effects: list[Effect] = [
                Send("decision_request", state.agreed),
                Schedule("decision"),
            ]
            if not state.escalated:
                effects.insert(0, Escalate("decision_overdue"))
            return Transition(
                replace(state, escalated=True, decision_requests=state.decision_requests + 1),
                tuple(effects),
            )
        case DelegationLost():
            if coordinator:
                return _decide(state, "abort")
            if not state.my_ready:
                return _close(state, Phase.REVOKED, "revoked", Send("revoke", state.agreed))
            return _ignore(state, "ready_sent")
        case _:
            return _ignore(state, "unexpected")


def _settled(state: NegotiationState, event: ProtocolEvent) -> Transition:
    match event:
        case DecisionRequested(hash=offer):
            if state.role != "coordinator":
                return _ignore(state, "not_coordinator")
            if state.decision == "commit" and _matches(state, offer):
                return Transition(state, (Send("decision", offer, "commit"),))
            return Transition(state, (Send("decision", offer, "abort"),))
        case LocalRevoke(hash=offer) | PeerRevoke(hash=offer):
            if state.phase is Phase.EXECUTING and _matches(state, offer):
                return Transition(
                    replace(state, phase=Phase.DISPUTED, outcome="disputed"),
                    (Send("dispute", offer), Escalate("revoked_after_commit")),
                )
            return _ignore(state, "terminal")
        case PeerDispute(hash=offer):
            if state.phase is Phase.EXECUTING and _matches(state, offer):
                return Transition(
                    replace(state, phase=Phase.DISPUTED, outcome="disputed"),
                    (Escalate("peer_dispute"),),
                )
            return _ignore(state, "terminal")
        case PeerDecision(hash=offer, decision=decision):
            if _matches(state, offer) and decision == state.decision:
                return _ignore(state, "duplicate_decision")
            if _matches(state, offer) and state.decision is not None:
                return Transition(
                    replace(state, phase=Phase.DISPUTED, outcome="disputed"),
                    (Escalate("conflicting_decision"),),
                )
            return _ignore(state, "terminal")
        case _:
            return _ignore(state, "terminal")


def transition(state: NegotiationState, event: ProtocolEvent) -> Transition:
    if state.phase is Phase.HANDSHAKE:
        if isinstance(event, Opened):
            return Transition(replace(state, phase=Phase.NEGOTIATING))
        return _ignore(state, "handshake_pending")
    if state.terminal:
        return _settled(state, event)
    if state.phase is Phase.NEGOTIATING:
        return _negotiating(state, event)
    if isinstance(event, PeerOffer | LocalOffer | LocalAccept | PeerAccept):
        return _ignore(state, "already_agreed")
    if isinstance(event, PeerReject) and state.agreed is not None:
        return _committing(state, PeerRevoke(state.agreed))
    if isinstance(event, LocalReject):
        return _ignore(state, "use_revoke")
    if isinstance(event, DecisionRequested) and state.role == "coordinator":
        return _ignore(state, "decision_pending")
    return _committing(state, event)
