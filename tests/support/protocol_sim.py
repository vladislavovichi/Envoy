from dataclasses import dataclass, field
from hashlib import sha256

from envoy.domain.ids import OfferHash
from envoy.protocol.machine import (
    Cancel,
    DecisionRequested,
    Effect,
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

SIDES = ("A", "B")


def offer(label: str) -> OfferHash:
    return OfferHash(sha256(label.encode()).digest())


def peer_event(send: Send) -> ProtocolEvent | None:
    match send.type:
        case "propose" | "counter":
            assert send.hash is not None
            return PeerOffer(send.hash, send.previous, send.round)
        case "accept":
            assert send.hash is not None
            return PeerAccept(send.hash)
        case "commit":
            assert send.hash is not None
            return PeerReady(send.hash)
        case "decision":
            assert send.hash is not None
            assert send.decision is not None
            return PeerDecision(send.hash, send.decision)
        case "decision_request":
            assert send.hash is not None
            return DecisionRequested(send.hash)
        case "revoke":
            assert send.hash is not None
            return PeerRevoke(send.hash)
        case "reject":
            return PeerReject(send.reason or "")
        case "dispute":
            assert send.hash is not None
            return PeerDispute(send.hash)
        case _:
            return None


@dataclass
class Network:
    states: dict[str, NegotiationState]
    inflight: list[tuple[str, ProtocolEvent]] = field(default_factory=list)
    timers: dict[str, set[str]] = field(default_factory=lambda: {"A": set(), "B": set()})
    checklists: dict[str, list[OfferHash]] = field(default_factory=lambda: {"A": [], "B": []})
    escalations: dict[str, list[str]] = field(default_factory=lambda: {"A": [], "B": []})
    sent: dict[str, list[Send]] = field(default_factory=lambda: {"A": [], "B": []})

    @classmethod
    def start(cls, a_id: bytes = b"agent-a", b_id: bytes = b"agent-b") -> Network:
        net = cls(
            {
                "A": NegotiationState(role="coordinator", me=a_id, peer=b_id),
                "B": NegotiationState(role="participant", me=b_id, peer=a_id),
            }
        )
        for side in SIDES:
            net.apply(side, Opened())
        return net

    @staticmethod
    def other(side: str) -> str:
        return "B" if side == "A" else "A"

    def apply(self, side: str, event: ProtocolEvent) -> None:
        result = transition(self.states[side], event)
        self.states[side] = result.state
        for effect in result.effects:
            self._effect(side, effect)

    def _effect(self, side: str, effect: Effect) -> None:
        match effect:
            case Send():
                self.sent[side].append(effect)
                event = peer_event(effect)
                if event is not None:
                    self.inflight.append((self.other(side), event))
            case Schedule(timer=timer):
                self.timers[side].add(timer)
            case Cancel(timer=timer):
                self.timers[side].discard(timer)
            case OpenChecklist(hash=hash):
                self.checklists[side].append(hash)
            case _:
                self.escalations[side].append(effect.reason)

    def deliver(self, index: int) -> None:
        side, event = self.inflight.pop(index)
        self.apply(side, event)

    def deliver_all(self) -> None:
        while self.inflight:
            self.deliver(0)

    def drop(self, index: int) -> None:
        self.inflight.pop(index)

    def duplicate(self, index: int) -> None:
        self.inflight.append(self.inflight[index])

    def fire(self, side: str, timer: str) -> None:
        if timer in self.timers[side]:
            self.timers[side].discard(timer)
            self.apply(side, TimerFired(timer))  # type: ignore[arg-type]

    def restart(self, side: str) -> None:
        state = self.states[side]
        self.timers[side] = set()
        if state.phase is Phase.NEGOTIATING and state.last_sent is not None:
            self.timers[side].add("reply")
        if state.phase in (Phase.AGREED, Phase.COMMITTING):
            if state.role == "coordinator" or not state.my_ready:
                self.timers[side].add("ready")
            else:
                self.timers[side].add("decision")

    def local(self, side: str, action: str, label: str = "x") -> None:
        state = self.states[side]
        match action:
            case "offer":
                self.apply(side, LocalOffer(offer(label)))
            case "accept" if state.last_received is not None:
                self.apply(side, LocalAccept(state.last_received.hash))
            case "ready" if state.agreed is not None:
                self.apply(side, LocalReady(state.agreed))
            case "revoke" if state.agreed is not None:
                self.apply(side, LocalRevoke(state.agreed))
            case "reject":
                self.apply(side, LocalReject("owner"))
            case _:
                pass

    def settle(self, steps: int = 400) -> None:
        for _ in range(steps):
            if self.inflight:
                self.deliver(0)
                continue
            fired = False
            for side in SIDES:
                for timer in sorted(self.timers[side]):
                    self.fire(side, timer)
                    fired = True
                    break
                if fired:
                    break
            if not fired:
                return

    def check_safety(self) -> None:
        a, b = self.states["A"], self.states["B"]
        if a.phase is Phase.EXECUTING and b.phase is Phase.EXECUTING:
            assert a.agreed == b.agreed
        for left, right in ((a, b), (b, a)):
            if left.phase is Phase.EXECUTING or (
                left.phase is Phase.DISPUTED and left.decision == "commit"
            ):
                assert right.phase not in (Phase.ABORTED, Phase.REVOKED), (left, right)
        for side in SIDES:
            assert len(self.checklists[side]) <= 1
            accepted = {send.hash for send in self.sent[side] if send.type == "accept"}
            countered = {send.previous for send in self.sent[side] if send.type == "counter"}
            assert not accepted & countered
            assert len(accepted) <= 1


def offer_ref(label: str, round: int = 1) -> OfferRef:
    return OfferRef(offer(label), round)
