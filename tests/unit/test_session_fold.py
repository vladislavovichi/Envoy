from datetime import timedelta
from uuid import UUID

import pytest
from polyfactory.factories.pydantic_factory import ModelFactory

from envoy.domain.events import (
    EVENT_TYPES,
    Closed,
    Decided,
    Delivered,
    Disclosed,
    Escalated,
    IntentParsed,
    JournalEntryType,
    Outcome,
    OwnerResolved,
    decode_event,
    encode_event,
)
from envoy.domain.ids import OwnerId, owner_stream
from envoy.domain.intent import AcceptIntent, Propose, RejectIntent, Unclear
from envoy.domain.session import (
    TRANSITIONS,
    IllegalTransition,
    SessionState,
    SessionView,
    apply,
    fold,
)
from tests.support.sessions import OFFER, OTHER, T0, happy_path, opened


def test_happy_path_reaches_closed_fulfilled() -> None:
    view = fold(happy_path())
    assert view.state is SessionState.CLOSED
    assert view.outcome is Outcome.fulfilled
    assert view.agreed == OTHER
    assert view.commitments_done == {("owner", 0), ("counterpart", 1)}


def test_second_accept_is_illegal() -> None:
    view = fold(happy_path()[:11])
    assert view.state is SessionState.AGREED
    with pytest.raises(IllegalTransition):
        apply(view, Decided(cause=9, kind="accept_offer", offer=OTHER, verdict="allow"))


def test_closed_is_terminal() -> None:
    view = fold(happy_path())
    for event in [Disclosed(), Closed(outcome=Outcome.expired, reason="x")]:
        with pytest.raises(IllegalTransition) as error:
            apply(view, event)
        assert error.value.state is SessionState.CLOSED


@pytest.mark.parametrize("length", [0, 1, 4, 8, 11])
def test_fulfilled_only_from_executing(length: int) -> None:
    view = fold(happy_path()[:length])
    with pytest.raises(IllegalTransition):
        apply(view, Closed(outcome=Outcome.fulfilled, reason="x"))


def test_pending_owner_cleared_on_resolution() -> None:
    view = fold(happy_path()[:8])
    assert view.state is SessionState.PENDING_OWNER
    assert view.pending is not None
    assert view.pending.offer == OTHER
    assert view.pending.kind == "accept_offer"
    assert view.pending.expires_at == T0 + timedelta(hours=1)
    with pytest.raises(IllegalTransition):
        apply(view, OwnerResolved(cause=99, resolution="approved"))
    resolved = apply(view, OwnerResolved(cause=2, resolution="declined"))
    assert resolved.pending is None
    assert resolved.state is SessionState.NEGOTIATING


def test_undelivered_tracks_allow_without_delivered() -> None:
    view = fold(happy_path()[:4])
    assert [decision.cause for decision in view.undelivered] == [1]
    view = apply(view, Delivered(cause=1, message_id=5))
    assert view.undelivered == ()
    view = apply(view, Decided(cause=4, kind="send_offer", offer=OFFER, verdict="deny"))
    assert view.undelivered == ()
    with pytest.raises(IllegalTransition):
        apply(view, Delivered(cause=4, message_id=6))


def test_disclosed_flag_set_by_event() -> None:
    view = fold(happy_path()[:2])
    assert not view.disclosed
    assert apply(view, Disclosed()).disclosed


def test_round_and_offers_tracked() -> None:
    view = fold(happy_path()[:6])
    assert view.round == 1
    assert view.last_outgoing == OFFER
    assert view.last_incoming == OTHER
    assert view.history == (OTHER,)


def test_consecutive_unclear_counted_and_reset() -> None:
    view = fold([opened()])
    for cause in range(3):
        view = apply(view, IntentParsed(cause=cause, intent=Unclear(missing=("time",))))
    assert view.clarifications == 3
    view = apply(view, IntentParsed(cause=4, intent=AcceptIntent()))
    assert view.clarifications == 0


def test_confirm_escalation_outside_negotiation_is_illegal() -> None:
    view = fold(happy_path()[:11])
    with pytest.raises(IllegalTransition):
        apply(view, Escalated(cause=3, reason="confirm_action"))
    assert apply(view, Escalated(cause=3, reason="reply_window_closed")).state is (
        SessionState.AGREED
    )


def build_event(model: type) -> object:
    overrides: dict[str, object] = {}
    fields = model.model_fields
    if "offer" in fields:
        overrides["offer"] = OFFER
    if "intent" in fields:
        overrides["intent"] = Propose(offer=OTHER)
    if "deadline" in fields:
        overrides["deadline"] = T0
    if "expires_at" in fields:
        overrides["expires_at"] = T0
    return ModelFactory.create_factory(model).build(**overrides)


@pytest.mark.parametrize("entry_type", list(JournalEntryType))
def test_event_codec_roundtrip_every_type(entry_type: JournalEntryType) -> None:
    event = build_event(EVENT_TYPES[entry_type])
    encoded_type, payload = encode_event(event)
    assert encoded_type is entry_type
    assert decode_event(encoded_type, payload) == event


@pytest.mark.parametrize("intent", [AcceptIntent(), RejectIntent(), Unclear(missing=("a", "b"))])
def test_intent_variants_roundtrip(intent) -> None:
    event = IntentParsed(cause=1, intent=intent)
    assert decode_event(*encode_event(event)) == event


def test_offer_types_survive_roundtrip() -> None:
    event = Decided(cause=1, kind="send_offer", offer=OFFER, verdict="allow")
    decoded = decode_event(*encode_event(event))
    assert decoded.offer == OFFER
    assert decoded.offer.hash() == OFFER.hash()


def test_owner_stream_is_stable_and_distinct() -> None:
    first, second = OwnerId(UUID(int=1)), OwnerId(UUID(int=2))
    assert owner_stream(first) == owner_stream(first)
    assert owner_stream(first) != owner_stream(second)


def test_initial_view_is_init() -> None:
    assert SessionView().state is SessionState.INIT


def test_transition_table_snapshot(snapshot) -> None:
    table = {
        state.value: sorted(kind.value for kind in kinds) for state, kinds in TRANSITIONS.items()
    }
    assert table == snapshot
