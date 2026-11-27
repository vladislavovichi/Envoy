from dataclasses import replace
from hashlib import sha256

import pytest

from envoy.domain.ids import OfferHash
from envoy.protocol.machine import (
    DecisionRequested,
    DelegationLost,
    LocalOffer,
    LocalReady,
    LocalReject,
    LocalRevoke,
    NegotiationState,
    OfferRef,
    Opened,
    PeerDecision,
    PeerDispute,
    PeerReady,
    PeerRevoke,
    Phase,
    Send,
    TimerFired,
    transition,
)

H = OfferHash(sha256(b"agreed").digest())
X = OfferHash(sha256(b"other").digest())


def state(role="participant", **changes) -> NegotiationState:
    values = {"role": role, "me": b"a", "peer": b"b", "phase": Phase.AGREED, "agreed": H}
    values.update(changes)
    return NegotiationState(**values)


@pytest.mark.parametrize(
    ("start", "event", "reason"),
    [
        (
            state(
                phase=Phase.NEGOTIATING,
                agreed=None,
                last_received=OfferRef(H, 1),
                answered=frozenset({H}),
            ),
            LocalOffer(X),
            "already_answered",
        ),
        (state(), LocalReady(X), "ready_rejected"),
        (state(my_ready=True), LocalReady(H), "ready_rejected"),
        (state(revoke_requested=True), LocalReady(H), "ready_rejected"),
        (state(), PeerReady(X), "ready_mismatch"),
        (state("coordinator"), PeerDecision(H, "commit"), "decision_rejected"),
        (state(), PeerDecision(X, "commit"), "decision_rejected"),
        (state(), PeerDecision(H, "commit"), "commit_without_ready"),
        (state(), LocalRevoke(X), "revoke_mismatch"),
        (state(), PeerRevoke(X), "revoke_mismatch"),
        (state(), TimerFired("decision"), "no_decision_wait"),
        (state("coordinator"), TimerFired("decision"), "no_decision_wait"),
        (state(my_ready=True, phase=Phase.COMMITTING), TimerFired("ready"), "ready_sent"),
        (state(my_ready=True, phase=Phase.COMMITTING), DelegationLost(), "ready_sent"),
        (state(), LocalReject("x"), "use_revoke"),
        (state("coordinator"), DecisionRequested(H), "decision_pending"),
        (state(), Opened(), "unexpected"),
        (state(phase=Phase.ABORTED), DecisionRequested(H), "not_coordinator"),
        (state(phase=Phase.ABORTED), PeerDispute(H), "terminal"),
        (
            state(phase=Phase.ABORTED, decision="abort"),
            PeerDecision(H, "abort"),
            "duplicate_decision",
        ),
        (state(phase=Phase.ABORTED), PeerDecision(X, "commit"), "terminal"),
        (state(phase=Phase.ABORTED), LocalRevoke(H), "terminal"),
        (state(phase=Phase.ABORTED), TimerFired("reply"), "terminal"),
    ],
)
def test_guarded_transitions_are_ignored(start, event, reason) -> None:
    result = transition(start, event)
    assert (result.state, result.effects, result.rejected) == (start, (), reason)


def test_delegation_loss_in_each_phase() -> None:
    negotiating = transition(state(phase=Phase.NEGOTIATING, agreed=None), DelegationLost())
    assert negotiating.state.phase is Phase.CLOSED
    assert Send("reject", reason="delegation") in negotiating.effects
    coordinator = transition(state("coordinator"), DelegationLost())
    assert (coordinator.state.phase, coordinator.state.decision) == (Phase.ABORTED, "abort")
    participant = transition(state(), DelegationLost())
    assert participant.state.phase is Phase.REVOKED


def test_participant_revoke_after_ready_is_a_request_and_coordinator_revoke_aborts() -> None:
    ready = state(my_ready=True, phase=Phase.COMMITTING)
    requested = transition(ready, LocalRevoke(H))
    assert requested.state.revoke_requested
    assert requested.state.phase is Phase.COMMITTING
    late = transition(requested.state, PeerDecision(H, "commit"))
    assert late.state.phase is Phase.EXECUTING
    assert any(getattr(effect, "reason", "") == "revoke_too_late" for effect in late.effects)
    coordinator = transition(state("coordinator"), LocalRevoke(H))
    assert coordinator.state.phase is Phase.ABORTED
    peer_revoked = transition(state("coordinator"), PeerRevoke(H))
    assert peer_revoked.state.decision == "abort"


def test_terminal_coordinator_answers_requests_and_conflicts_escalate() -> None:
    committed = state("coordinator", phase=Phase.EXECUTING, decision="commit")
    assert transition(committed, DecisionRequested(H)).effects == (Send("decision", H, "commit"),)
    assert transition(committed, DecisionRequested(X)).effects == (Send("decision", X, "abort"),)
    executing = state(phase=Phase.EXECUTING, decision="commit")
    conflict = transition(executing, PeerDecision(H, "abort"))
    assert conflict.state.phase is Phase.DISPUTED
    disputed = transition(executing, PeerDispute(H))
    assert disputed.state.phase is Phase.DISPUTED
    assert transition(replace(executing, agreed=X), PeerDispute(H)).rejected == "terminal"
