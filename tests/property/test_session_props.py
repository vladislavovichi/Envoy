from hypothesis import given
from hypothesis import strategies as st

from envoy.domain.events import (
    ChecklistOpened,
    Closed,
    CommitmentDone,
    Decided,
    Delivered,
    Disclosed,
    Escalated,
    IntentParsed,
    Outcome,
    OwnerResolved,
)
from envoy.domain.intent import AcceptIntent, Propose, Unclear
from envoy.domain.session import SessionState, SessionView, apply, fold
from tests.support.sessions import OFFER, OTHER, opened


def legal_events(view: SessionView, cause: int) -> list:
    state = view.state
    candidates: list = []
    if state is SessionState.INIT:
        candidates.append(opened())
    if state is SessionState.NEGOTIATING:
        candidates += [
            Disclosed(),
            IntentParsed(cause=cause, intent=Propose(offer=OTHER)),
            IntentParsed(cause=cause, intent=Unclear(missing=("time",))),
            IntentParsed(cause=cause, intent=AcceptIntent()),
            Decided(cause=cause, kind="send_offer", offer=OFFER, verdict="allow"),
            Decided(cause=cause, kind="send_offer", offer=OFFER, verdict="deny"),
            Decided(cause=cause, kind="accept_offer", offer=OTHER, verdict="allow"),
            Decided(cause=cause, kind="accept_offer", offer=OTHER, verdict="escalate"),
        ]
        if view.last_decision is not None and view.last_decision.verdict == "escalate":
            candidates.append(Escalated(cause=view.last_decision.cause, reason="confirm_action"))
    if state is SessionState.PENDING_OWNER and view.pending is not None:
        candidates.append(OwnerResolved(cause=view.pending.cause, resolution="approved"))
    if state is SessionState.AGREED:
        candidates.append(ChecklistOpened(items=("a", "b")))
    if state is SessionState.EXECUTING:
        candidates.append(CommitmentDone(party="owner", index=0))
    if state not in (SessionState.INIT, SessionState.CLOSED):
        candidates.append(Escalated(cause=cause, reason="reply_window_closed"))
        if view.undelivered:
            candidates.append(Delivered(cause=view.undelivered[0].cause, message_id=cause))
    return candidates


@st.composite
def legal_sequences(draw: st.DrawFn) -> list:
    view = SessionView()
    events: list = []
    for cause in range(draw(st.integers(0, 30))):
        event = draw(st.sampled_from(legal_events(view, cause)))
        view = apply(view, event)
        events.append(event)
    return events


@given(legal_sequences())
def test_fold_of_random_legal_sequence_never_raises(events: list) -> None:
    view = fold(events)
    assert view.state is not SessionState.CLOSED
    assert all(decision.verdict == "allow" for decision in view.undelivered)


@given(legal_sequences())
def test_expired_closes_from_every_nonterminal_state(events: list) -> None:
    closed = apply(fold(events), Closed(outcome=Outcome.expired, reason="deadline"))
    assert closed.state is SessionState.CLOSED
    assert closed.outcome is Outcome.expired
    assert closed.pending is None


@given(legal_sequences())
def test_at_most_one_accept_reaches_agreed(events: list) -> None:
    accepts = [
        event
        for event in events
        if isinstance(event, Decided) and event.kind == "accept_offer" and event.verdict == "allow"
    ]
    assert len(accepts) <= 1
