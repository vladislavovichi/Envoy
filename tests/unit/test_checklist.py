from dataclasses import replace

import pytest

from envoy.domain.offer import Offer
from envoy.domain.session import SessionView
from envoy.execution.checklist import is_complete, meeting_time, plan_commitments
from tests.support.mandates import SLOTS

OFFER = Offer(values={"time": SLOTS[2], "place": "cafe"})


def test_plan_has_one_item_per_party_with_time_and_place() -> None:
    plan = plan_commitments(OFFER, "meeting")
    assert [item.party for item in plan] == ["owner", "counterpart"]
    for item in plan:
        assert "cafe" in item.text
        assert "01.12.2026 11:00" in item.text
    assert "MSK" in plan_commitments(OFFER, "meeting", "MSK")[0].text
    with pytest.raises(ValueError, match="Unsupported"):
        plan_commitments(OFFER, "purchase")


def test_is_complete_only_when_all_done() -> None:
    plan = plan_commitments(OFFER, "meeting")
    view = SessionView()
    assert not is_complete(view, plan)
    view = replace(view, commitments_done=frozenset({("owner", 0)}))
    assert not is_complete(view, plan)
    view = replace(view, commitments_done=frozenset({("owner", 0), ("owner", 1)}))
    assert not is_complete(view, plan)
    view = replace(view, commitments_done=frozenset({("owner", 0), ("counterpart", 1)}))
    assert is_complete(view, plan)
    assert not is_complete(view, ())


def test_meeting_time_extracts_datetime() -> None:
    assert meeting_time(OFFER) == SLOTS[2]
    assert meeting_time(Offer(values={"time": "soon"})) is None
