from dataclasses import asdict
from datetime import timedelta

import pytest

from envoy.app.renderer import ISSUE_LABELS, TemplateRenderer
from envoy.domain.events import Outcome
from envoy.domain.offer import Offer
from envoy.domain.session import PendingEscalation, SessionState, SessionView
from envoy.security.widget_projection import WidgetProjection, deal_status, proximity
from envoy.widgets.owner_view import dashboard_state, escalation_state
from tests.support.mandates import RESERVATION, WEIGHT_PLACE, WEIGHT_TIME, meeting_data
from tests.support.sessions import T0

RENDERER = TemplateRenderer("UTC")
PROJECTION = WidgetProjection(RENDERER.value, ISSUE_LABELS)
OURS = Offer(values={"time": T0, "place": "office", "room": "A"})
THEIRS = Offer(values={"time": T0, "place": "cafe", "room": "A"})
CANARIES = (str(RESERVATION), str(WEIGHT_TIME), str(WEIGHT_PLACE), "station", "beta", "minimum")


def test_proximity_rounds_share_of_matching_common_issues() -> None:
    assert proximity(OURS, THEIRS) == 67
    assert proximity(OURS, OURS) == 100
    assert proximity(Offer(values={"time": T0}), Offer(values={"place": "cafe"})) is None
    assert proximity(None, THEIRS) is None
    assert proximity(Offer(values={"a": 1, "b": 2}), Offer(values={"a": 1, "b": 3})) == 50


@pytest.mark.parametrize(
    ("view", "status"),
    [
        (SessionView(state=SessionState.NEGOTIATING), "counterpart"),
        (SessionView(state=SessionState.PENDING_OWNER), "owner"),
        (SessionView(state=SessionState.AGREED), "agreed"),
        (SessionView(state=SessionState.EXECUTING), "agreed"),
        (SessionView(state=SessionState.CLOSED, outcome=Outcome.fulfilled), "fulfilled"),
        (SessionView(state=SessionState.CLOSED, outcome=Outcome.expired), "expired"),
        (SessionView(state=SessionState.CLOSED), "rejected"),
    ],
)
def test_deal_status(view, status) -> None:
    assert deal_status(view) == status


def test_dashboard_rows_use_labels_formatted_values_and_round() -> None:
    view = SessionView(
        state=SessionState.NEGOTIATING, round=2, last_outgoing=OURS, last_incoming=THEIRS
    )
    card = PROJECTION.dashboard(revision=9, view=view, max_rounds=6, issues=["time", "place"])
    assert (card.revision, card.round, card.max_rounds, card.status) == (9, 2, 6, "counterpart")
    assert [(row.name, row.ours, row.theirs) for row in card.issues] == [
        ("место", "office", "cafe"),
        ("room", "A", "A"),
        ("время", "01.12.2026 12:00", "01.12.2026 12:00"),
    ]
    assert card.proximity == 67
    assert card.verified is None
    empty = PROJECTION.dashboard(revision=1, view=SessionView(), max_rounds=6, issues=["time"])
    assert empty.proximity is None
    assert [(row.ours, row.theirs) for row in empty.issues] == [(None, None)]


def test_agreed_dashboard_shows_agreed_offer_for_both_sides() -> None:
    view = SessionView(state=SessionState.AGREED, agreed=THEIRS, last_outgoing=OURS)
    card = PROJECTION.dashboard(revision=3, view=view, max_rounds=6, issues=["place"])
    assert card.proximity == 100


def test_serialized_cards_and_text_exclude_mandate_canaries() -> None:
    mandate = str(meeting_data())
    assert all(canary in mandate for canary in CANARIES[:3])
    view = SessionView(
        state=SessionState.PENDING_OWNER,
        round=1,
        last_outgoing=OURS,
        last_incoming=THEIRS,
        pending=PendingEscalation(1, "confirm_action", T0, "accept_offer", THEIRS),
    )
    dashboard = PROJECTION.dashboard(revision=2, view=view, max_rounds=6, issues=["time", "place"])
    escalation = PROJECTION.escalation(
        revision=2,
        cause=1,
        expires_at=T0 + timedelta(hours=1),
        offer=THEIRS,
        theirs=THEIRS,
        outcome="pending",
    )
    rendered = [
        dashboard_state(dashboard),
        escalation_state(escalation, T0),
        str(asdict(dashboard)),
        str(asdict(escalation)),
        RENDERER.owner_dashboard(dashboard),
        RENDERER.owner_escalation(escalation).text,
    ]
    for text in rendered:
        for canary in CANARIES:
            assert canary not in text
